"""MC 聊天池桥接：让 QQ 群和 Minecraft(Paper) 服务器共用同一份聊天记录。

架构
====

Paper 侧安装 McQqChatPool 插件，它把「聊天池」以自增 ID 的形式存在内存里，
并开一个只监听本机的 HTTP 接口。本插件负责两端：

* **MC → QQ（定时拉取）**：`_poll_loop` 每 poll_interval_seconds 秒调用
  `GET /api/v1/messages?pool=<池>&since=<游标>`，把新消息按 `mc_to_qq_format`
  渲染后发到该池绑定的 QQ 群，并把新游标记进 state.json。
  默认首次运行只记录游标、不回放历史（skip_backlog_on_first_poll），
  免得插件一启动就把服务器里攒了半天的消息全刷进群里。

* **QQ → MC（群内命令）**：群里发 `#mc <文本>` 时，`on_group_message` 命中前缀，
  用 `POST /api/v1/messages` 把消息投进聊天池，服务器再广播给池内的在线玩家。
  命令本身通过 `event.stop_event()` 结束事件，不会触发 LLM 回复。

为什么默认排除 source=qq
------------------------

服务器会把「QQ 群注入的消息」也写进池子里（这样游戏内玩家和后续拉取的客户端
都能看到完整上下文）。如果拉取时不排除它，群里的消息会被拉回来再发一遍，
形成自我复制的刷屏循环。因此 `poll_exclude_sources` 默认为 `qq`。

批量与合并转发
==============

一轮拉到的消息如果够多（>= ``merge_forward_threshold``），就不再逐条刷屏，而是打包成
QQ 的「合并转发」卡片（NapCat 的 ``send_group_forward_msg`` / ``send_private_forward_msg``）。
卡片里每个 node 的显示名是 MC 发送者名，内容与逐条发送时完全一致（同一个
``mc_to_qq_format`` 渲染结果）；超过 ``merge_forward_max_nodes`` 会自动拆成多张卡片依次发。
合并转发失败时退化成逐条发送，确保消息不丢。

全量读取历史
============

``read_all_history`` 打开后，某个池**首次**同步不再跳过历史，而是从最早的保留消息开始
分页拉取（``history_page_size`` 一页）并转发，总量受 ``history_max_messages`` 限制，
完成后把游标推进到该池最新位置，之后恢复增量。群里也可以用 ``#mc all [池] [条数]``
随时把历史以合并转发导出到当前会话。

落盘
====

`state.json` 只存两样东西：每个池的拉取游标，以及用 `#mc bind` 建立的群↔池绑定
（与配置里的桥接关系是并集，配置始终有效）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 新版公开路径
    from astrbot.api.event import MessageChain
except ImportError:  # pragma: no cover - 旧版本回退
    from astrbot.core.message.message_event_result import MessageChain

try:
    from astrbot.api.message_components import Plain
except ImportError:  # pragma: no cover - 旧版本回退
    from astrbot.core.message.components import Plain

try:  # 合并转发组件（Node/Nodes）：4.x 起放在 message_components 里
    from astrbot.api.message_components import Node, Nodes
except ImportError:  # pragma: no cover - 旧版本回退
    try:
        from astrbot.core.message.components import Node, Nodes  # type: ignore[no-redef]
    except ImportError:  # 实在没有就退化成逐条发送
        Node = None  # type: ignore[assignment,misc]
        Nodes = None  # type: ignore[assignment,misc]

try:
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover - 旧版本回退
    from astrbot.core.star.star_tools import StarTools


PLUGIN_NAME = "astrbot_plugin_mc_chatpool"
PLACEHOLDER = re.compile(r"\{(\w+)\}")

#: 一条消息最多重试投递多少次；超过就放弃这条并继续推进游标，避免卡死。
#: 用于兜住「平台连接抖动」这类瞬时故障；平台压根没连上时不计入次数。
MAX_DELIVERY_RETRIES = 10

#: 这些异常表示「平台还没握手完」，属于暂时不可用：挂起重试，不消耗重试次数。
#: ApiNotAvailable 是 AstrBot aiocqhttp 适配器在反向 WS 未就绪时抛出的。
DEFERRED_EXCEPTIONS = {"ApiNotAvailable", "ConnectionError", "ClientConnectionError"}

#: 合并转发要调用的 OneBot 11 action（NapCat 支持）。群聊与私聊是两个不同的 action，
#: AstrBot 的 aiocqhttp 适配器内部也是这么分流的。
FORWARD_ACTIONS = {
    True: "send_group_forward_msg",
    False: "send_private_forward_msg",
}

#: mc_to_qq_format 的默认值（逐条发送与合并转发共用，保证观感一致）。
DEFAULT_FORMAT = "[MC] {sender}: {content}"

HELP_TEXT = (
    "🕹 MC 聊天池命令\n"
    "#mc <文本>            — 发到本群绑定的聊天池\n"
    "#mc send <池> <文本>   — 发到指定聊天池\n"
    "#mc status            — 查看服务器与聊天池状态\n"
    "#mc pools             — 列出所有聊天池\n"
    "#mc history [池] [条数] — 查看最近几条游戏内消息\n"
    "#mc all [池] [条数]    — 把该池历史以合并转发导出到本会话\n"
    "#mc bind <池>          — 把本群绑定到某个聊天池（管理员）\n"
    "#mc unbind [池]        — 解除绑定（管理员）\n"
    "#mc help              — 显示这条帮助"
)


@register(
    PLUGIN_NAME,
    "ruosha",
    "Minecraft 聊天池桥接：定时把服务器聊天池里的消息发到 QQ 群，群内 #mc 命令把消息送回服务器。",
    "1.0.0",
)
class McChatPoolBridge(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

        self._poll_task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None

        # state.json：{"cursors": {池: id}, "bindings": {池: [群号]}}
        self._state: dict = {"cursors": {}, "bindings": {}}
        self._state_file: Path | None = None

        # 配置里声明的桥接关系（池 -> 群号列表）
        self._config_bridges: dict[str, list[str]] = {}

        # 自动探测出来的平台适配器 ID 缓存
        self._platform_id_cache: str | None = None

        # 投递失败次数：消息 ID -> 已重试次数
        self._failed_ids: dict[int, int] = {}

        # 已经因为「平台没连上」提醒过的池，避免每轮都刷日志
        self._deferred_warned: dict[str, bool] = {}

    # ================================================================= 生命周期

    async def initialize(self) -> None:
        self._state_file = self._resolve_state_file()
        self._load_state()
        self._config_bridges = self._parse_bridges()

        if not self._config_bridges:
            logger.warning(
                "[mc_chatpool] 还没有配置任何「聊天池 = 群号」绑定，"
                "请在插件配置的 bridges 里填写，例如：global = 123456789"
            )
        else:
            summary = ", ".join(f"{pool}->{','.join(groups)}" for pool, groups in self._config_bridges.items())
            logger.info(f"[mc_chatpool] 已配置的桥接：{summary}")

        if self._as_bool("enable_poll", True):
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info(
                f"[mc_chatpool] 已启动定时拉取：{self._base_url()} 每 "
                f"{self._as_int('poll_interval_seconds', 5, 1, 3600)} 秒拉一次。"
            )
        else:
            logger.info("[mc_chatpool] enable_poll=false，只处理群内命令，不主动拉取消息。")

        logger.info(f"[mc_chatpool] 插件已就绪，数据文件：{self._state_file}")

    async def terminate(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关停时吞掉所有异常
                pass
            self._poll_task = None

        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

        self._save_state(force=True)
        logger.info("[mc_chatpool] 插件已停止。")

    # =============================================================== 配置读取

    def _str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _as_bool(self, key: str, default: bool = False) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _as_int(self, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    # ---- 「全量读取历史」与「合并转发」的配置读取 ----

    def _read_all_history(self) -> bool:
        """首次同步是否把池子里保留的历史全部读出来（而不是只记游标跳过）。"""
        return self._as_bool("read_all_history", False)

    def _history_page_size(self) -> int:
        """全量同步时每页拉多少条（服务端接口的 limit 上限就是 200）。"""
        return self._as_int("history_page_size", 200, 1, 200)

    def _history_max_messages(self) -> int:
        """单次全量同步最多处理多少条；0 = 不限制。"""
        return self._as_int("history_max_messages", 2000, 0, 1_000_000)

    def _merge_threshold(self) -> int:
        """一轮要发的消息达到这个数量，就改用合并转发。"""
        return self._as_int("merge_forward_threshold", 6, 1, 100_000)

    def _merge_max_nodes(self) -> int:
        """单张合并转发卡片最多放多少条消息，超出拆成多张。"""
        return self._as_int("merge_forward_max_nodes", 100, 1, 1000)

    def _merge_title(self) -> str:
        """合并转发卡片的外显标题（NapCat 的 summary 字段）。"""
        return self._str("merge_forward_title", "MC 聊天记录")

    def _base_url(self) -> str:
        url = self._str("mc_base_url", "http://127.0.0.1:8123")
        return url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        token = self._str("mc_token", "")
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=self._as_int("http_timeout_seconds", 8, 1, 120))

    def _prefixes(self) -> list[str]:
        """命令前缀，例如 ["#mc", "/mc"]。"""
        raw = self._str("command_prefix", "#mc") or "#mc"
        prefixes = {raw}
        if raw.startswith("#"):
            prefixes.add("/" + raw[1:])
        elif raw.startswith("/"):
            prefixes.add("#" + raw[1:])
        return sorted(prefixes, key=len, reverse=True)

    def _group_whitelist(self) -> set[str]:
        raw = self.config.get("group_whitelist") or []
        if isinstance(raw, str):
            raw = re.split(r"[,，、\s]+", raw)
        return {str(item).strip() for item in raw if str(item).strip()}

    def _admins(self) -> set[str]:
        raw = self.config.get("admin_qq_ids") or []
        if isinstance(raw, str):
            raw = re.split(r"[,，、\s]+", raw)
        return {str(item).strip() for item in raw if str(item).strip()}

    def _platform_id(self) -> str:
        """解析「MC → QQ」要用的平台适配器 ID。

        为什么需要自动探测：AstrBot 里平台实例的 ID 是用户在 WebUI 里起的名字，
        跟适配器类型没有必然关系（例如本部署的 aiocqhttp 实例 ID 就叫 ``default``）。
        写死成 ``aiocqhttp`` 会得到 ``cannot find platform for session ...``，
        消息静默发不出去。因此 platform_id 填 auto（默认）时按实例元数据探测。
        """
        configured = self._str("platform_id", "")
        if configured and configured.lower() != "auto":
            return configured
        if self._platform_id_cache:
            return self._platform_id_cache

        detected = ""
        try:
            manager = getattr(self.context, "platform_manager", None)
            insts = list(manager.get_insts()) if manager is not None and hasattr(manager, "get_insts") else []
            for inst in insts:
                meta = inst.meta()
                pid = str(getattr(meta, "id", "") or "")
                name = str(getattr(meta, "name", "") or "")
                if "webchat" in pid.lower() or "webchat" in name.lower():
                    continue
                lowered = f"{pid} {name}".lower()
                if any(key in lowered for key in ("aiocqhttp", "onebot", "napcat")):
                    detected = pid or name
                    break
                if not detected:
                    detected = pid or name
        except Exception as exc:  # noqa: BLE001 - 探测失败就退回配置值
            logger.debug(f"[mc_chatpool] 探测平台 ID 失败：{exc}")

        if detected:
            self._platform_id_cache = detected
            logger.info(f"[mc_chatpool] 自动探测到平台 ID = {detected}")
            return detected

        # 平台实例还没注册完（AstrBot 是先加载插件、后加载平台）：
        # 这里刻意不缓存，等下一轮再探测一次。
        return "aiocqhttp"

    def _platform_running(self, platform_id: str) -> bool | None:
        """平台适配器是否已经连上。

        @return True=已连接，False=还没连上，None=查不到状态（不阻塞发送）。
        """
        try:
            manager = getattr(self.context, "platform_manager", None)
            insts = list(manager.get_insts()) if manager is not None and hasattr(manager, "get_insts") else []
            for inst in insts:
                meta = inst.meta()
                pid = str(getattr(meta, "id", "") or "")
                name = str(getattr(meta, "name", "") or "")
                if platform_id not in (pid, name):
                    continue
                status = getattr(inst, "status", None)
                if status is None:
                    return None
                return getattr(status, "name", str(status)).upper() == "RUNNING"
        except Exception as exc:  # noqa: BLE001 - 状态查不到就不拦
            logger.debug(f"[mc_chatpool] 读取平台状态失败：{exc}")
        return None

    def _bot_for(self, platform_id: str, event: AstrMessageEvent | None = None):
        """取 aiocqhttp 的 bot 实例，拿不到就返回 None。

        为什么要单独取 bot：合并转发卡片的外显标题（NapCat 的 ``summary``）是 action 的
        **顶层参数**，而 AstrBot 的 ``Node``/``Nodes`` 组件只带 nickname/content，不携带它。
        想让标题生效就得自己 ``call_action``，而 call_action 需要 bot 实例
        （命令路径用 ``event.bot``，轮询路径用平台适配器上的 ``.bot``）。
        取不到时退回原生组件链，让框架自己路由（此时标题不生效）。
        """
        if event is not None:
            bot = getattr(event, "bot", None)
            if bot is not None and hasattr(bot, "call_action"):
                return bot
        try:
            inst = None
            getter = getattr(self.context, "get_platform_inst", None)
            if callable(getter):
                inst = getter(platform_id)
            if inst is None:
                manager = getattr(self.context, "platform_manager", None)
                insts = list(manager.get_insts()) if manager is not None and hasattr(manager, "get_insts") else []
                for candidate in insts:
                    meta = candidate.meta()
                    pid = str(getattr(meta, "id", "") or "")
                    name = str(getattr(meta, "name", "") or "")
                    if platform_id in (pid, name):
                        inst = candidate
                        break
            if inst is None:
                return None
            bot = getattr(inst, "bot", None)
            if bot is None and hasattr(inst, "get_client"):
                bot = inst.get_client()
            return bot if bot is not None and hasattr(bot, "call_action") else None
        except Exception as exc:  # noqa: BLE001 - 取不到就当作没有
            logger.debug(f"[mc_chatpool] 获取 bot 实例失败：{exc}")
            return None

    # =============================================================== 数据落盘

    def _resolve_state_file(self) -> Path:
        try:
            data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as exc:  # noqa: BLE001 - 取不到就退到插件目录
            logger.warning(f"[mc_chatpool] 获取插件数据目录失败（{exc}），改用插件目录。")
            data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "state.json"

    def _load_state(self) -> None:
        if self._state_file is None or not self._state_file.is_file():
            return
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mc_chatpool] 读取 state.json 失败，按空状态继续：{exc}")
            return

        cursors = raw.get("cursors")
        if isinstance(cursors, dict):
            self._state["cursors"] = {
                str(pool): int(value)
                for pool, value in cursors.items()
                if isinstance(value, (int, float, str)) and str(value).lstrip("-").isdigit()
            }
        bindings = raw.get("bindings")
        if isinstance(bindings, dict):
            cleaned: dict[str, list[str]] = {}
            for pool, groups in bindings.items():
                if isinstance(groups, list):
                    cleaned[str(pool)] = [str(g) for g in groups if str(g).strip()]
            self._state["bindings"] = cleaned

    def _save_state(self, force: bool = False) -> None:
        if self._state_file is None:
            return
        try:
            self._state_file.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mc_chatpool] 写入 state.json 失败：{exc}")

    # =============================================================== 桥接关系

    def _parse_bridges(self) -> dict[str, list[str]]:
        """解析配置里的 bridges：既支持「池 = 群号, 群号」行格式，也支持 JSON。"""
        raw = self._str("bridges", "")
        if not raw:
            return {}

        parsed: dict[str, list[str]] = {}

        text = raw.strip()
        if text.startswith("["):
            try:
                for item in json.loads(text):
                    pool = str(item.get("pool", "")).strip().lower()
                    groups = item.get("groups") or item.get("group") or []
                    if isinstance(groups, (str, int)):
                        groups = [groups]
                    if pool:
                        parsed.setdefault(pool, []).extend(str(g).strip() for g in groups if str(g).strip())
                return parsed
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[mc_chatpool] bridges 看起来是 JSON 但解析失败（{exc}），改按行格式解析。")

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            if "=" not in line:
                logger.warning(f"[mc_chatpool] 跳过无法识别的桥接配置行：{line!r}（应为「池名 = 群号」）")
                continue
            pool_part, groups_part = line.split("=", 1)
            pool = pool_part.strip().lower()
            groups = [g for g in re.split(r"[,，、\s]+", groups_part.strip()) if g]
            if pool and groups:
                parsed.setdefault(pool, []).extend(groups)
        return parsed

    def _bridges(self) -> dict[str, list[str]]:
        """配置绑定与 #mc bind建立的绑定取并集。"""
        merged: dict[str, list[str]] = {pool: list(groups) for pool, groups in self._config_bridges.items()}
        for pool, groups in self._state.get("bindings", {}).items():
            merged.setdefault(pool, [])
            for group in groups:
                if group not in merged[pool]:
                    merged[pool].append(group)
        return merged

    def _pools_of_group(self, group_id: str) -> list[str]:
        return [pool for pool, groups in self._bridges().items() if group_id in groups]

    def _pool_for_group(self, group_id: str) -> str:
        pools = self._pools_of_group(group_id)
        return pools[0] if pools else self._str("default_pool", "global") or "global"

    # ================================================================= HTTP

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # trust_env=False：127.0.0.1 的请求绝不能走系统代理
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    async def _api_get(self, path: str, params: dict | None = None) -> dict | None:
        session = await self._get_session()
        url = self._base_url() + path
        try:
            async with session.get(
                url, params=params, headers=self._headers(), timeout=self._timeout()
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.warning(f"[mc_chatpool] GET {path} 返回 {resp.status}：{text[:200]}")
                    return None
                return json.loads(text)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning(f"[mc_chatpool] GET {path} 失败：{exc}")
            return None

    async def _api_post(self, path: str, payload: dict) -> dict | None:
        session = await self._get_session()
        url = self._base_url() + path
        try:
            async with session.post(
                url, json=payload, headers=self._headers(), timeout=self._timeout()
            ) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except ValueError:
                    logger.warning(f"[mc_chatpool] POST {path} 返回了非 JSON 内容（{resp.status}）：{text[:200]}")
                    return None
                if resp.status != 200:
                    logger.warning(f"[mc_chatpool] POST {path} 返回 {resp.status}：{text[:200]}")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning(f"[mc_chatpool] POST {path} 失败：{exc}")
            return None

    # ================================================================= 拉取

    async def _poll_loop(self) -> None:
        interval = self._as_int("poll_interval_seconds", 5, 1, 3600)
        while True:
            started = time.monotonic()
            try:
                await self._poll_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 轮询线程绝不能被单次异常打死
                logger.error(f"[mc_chatpool] 拉取消息时出错：{exc}", exc_info=True)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.5, interval - elapsed))

    async def _poll_all(self) -> None:
        bridges = self._bridges()
        if not bridges:
            return
        dirty = False
        for pool, groups in bridges.items():
            if not groups:
                continue
            dirty |= await self._poll_pool(pool, groups)
        if dirty:
            self._save_state()

    async def _poll_pool(self, pool: str, groups: list[str]) -> bool:
        """拉取一个池的新消息；返回游标是否有变化。"""
        limit = self._as_int("poll_batch_limit", 50, 1, 200)
        exclude = self._str("poll_exclude_sources", "qq")

        cursor = self._state["cursors"].get(pool)
        if cursor is None:
            if self._read_all_history():
                # 全量读取：不回放？要回放——把服务器保留的历史全部读出来发一遍。
                return await self._sync_full_history(pool, groups)
            if not self._as_bool("skip_backlog_on_first_poll", True):
                cursor = 0
            else:
                data = await self._api_get("/api/v1/messages", {"pool": pool, "limit": 1})
                if not data:
                    return False
                cursor = int(data.get("cursor") or data.get("pool_cursor") or 0)
                self._state["cursors"][pool] = cursor
                logger.info(f"[mc_chatpool] 首次连接：聊天池 {pool} 从游标 {cursor} 开始（跳过历史消息）")
                return True

        changed = False
        # 积压较多时连续拉几轮，尽快追平
        for _ in range(5):
            params = {"pool": pool, "since": cursor, "limit": limit}
            if exclude:
                params["exclude_source"] = exclude

            data = await self._api_get("/api/v1/messages", params)
            if not data or not data.get("ok"):
                return changed

            messages = data.get("messages") or []
            if not messages:
                break

            # 只有确认送达的消息才允许把游标推过去：没送达的留在原地，
            # 下一轮会重试，避免「机器人刚启动、平台还没注册好」这类竞态把消息丢掉。
            delivered_upto, retry_later = await self._deliver_round(pool, groups, messages, cursor)
            if delivered_upto != cursor:
                cursor = delivered_upto
                self._state["cursors"][pool] = cursor
                changed = True
            if retry_later or not data.get("has_more"):
                break

        return changed

    async def _deliver_round(
        self, pool: str, groups: list[str], messages: list[dict], cursor: int
    ) -> tuple[int, bool]:
        """把本轮拉到的消息投递出去。

        条数达到 ``merge_forward_threshold`` 时整批走合并转发（失败会自动退化成逐条），
        否则维持原来的逐条发送。

        @return (已确认送达的最大消息 id, 是否本轮就此打住、留到下一轮重试)
        """
        if len(messages) >= self._merge_threshold():
            outcome = await self._deliver_messages(pool, groups, messages)
            if outcome is True:
                for message in messages:
                    self._failed_ids.pop(int(message.get("id") or 0), None)
                self._deferred_warned[pool] = False
                return int(messages[-1].get("id") or cursor), False
            if outcome is None:
                self._warn_deferred(pool, cursor)
                return cursor, True
            if self._note_failure(pool, messages, cursor, batch=True):
                return int(messages[-1].get("id") or cursor), False
            return cursor, True

        delivered_upto = cursor
        for message in messages:
            message_id = int(message.get("id") or 0)
            outcome = await self._deliver(pool, groups, message)
            if outcome is True:
                self._failed_ids.pop(message_id, None)
                self._deferred_warned[pool] = False
                delivered_upto = message_id
                continue

            if outcome is None:
                # 平台还没连上：不算失败、不消耗重试次数，游标停住等下一轮。
                self._warn_deferred(pool, delivered_upto)
                return delivered_upto, True

            if self._note_failure(pool, [message], delivered_upto, batch=False):
                # 到重试上限了：放弃这条，继续处理后面的
                delivered_upto = message_id
                continue
            return delivered_upto, True

        return delivered_upto, False

    def _warn_deferred(self, pool: str, cursor: int) -> None:
        """平台还没连上时的提示，每个池只提示一次，避免每轮刷日志。"""
        if self._deferred_warned.get(pool):
            return
        self._deferred_warned[pool] = True
        logger.info(
            f"[mc_chatpool] 平台尚未连接，暂停转发聊天池 {pool}（游标停在 {cursor}），"
            "连上后会自动补发这些消息。"
        )

    def _note_failure(self, pool: str, messages: list[dict], delivered_upto: int, batch: bool) -> bool:
        """记一次投递失败（单条或整批）。

        @return True=已经到重试上限，放弃这些消息并继续推进游标；False=下一轮再试
        """
        ids = [int(message.get("id") or 0) for message in messages]
        failures = (max(self._failed_ids.get(i, 0) for i in ids) + 1) if ids else 1
        for message_id in ids:
            self._failed_ids[message_id] = failures

        what = f"消息 #{ids[0]}～#{ids[-1]}" if batch and len(ids) > 1 else f"消息 #{ids[0]}"
        if failures < MAX_DELIVERY_RETRIES:
            logger.warning(
                f"[mc_chatpool] {what} 未送达（第 {failures}/{MAX_DELIVERY_RETRIES} 次），"
                f"游标停在 {delivered_upto}，下一轮重试。"
            )
            return False

        logger.error(f"[mc_chatpool] {what} 连续 {failures} 次投递失败，放弃这些消息并继续。")
        for message_id in ids:
            self._failed_ids.pop(message_id, None)
        return True

    def _message_text(self, pool: str, message: dict) -> str:
        """按 mc_to_qq_format 渲染一条消息。

        逐条发送、合并转发卡片里的内容、#mc history / #mc all 都走它，
        保证占位符与观感完全一致。
        """
        return self._render(
            self._str("mc_to_qq_format", DEFAULT_FORMAT),
            {
                "pool": pool,
                "sender": message.get("sender", "?"),
                "sender_id": message.get("sender_id", ""),
                "content": message.get("content", ""),
                "id": message.get("id", ""),
                "source": message.get("source", "mc"),
                "time": message.get("time", ""),
            },
        )

    async def _deliver(self, pool: str, groups: list[str], message: dict) -> bool | None:
        """把一条游戏内消息逐条发到该池绑定的所有群。

        @return True=全部送达；False=尝试过但失败（可以计一次重试）；
                None=平台还没连上，压根没尝试（不计重试，游标停住等连接）
        """
        text = self._message_text(pool, message)
        if not text.strip():
            return True

        platform = self._platform_id()
        if self._platform_running(platform) is False:
            # NapCat 的反向 WS 在 AstrBot 重启后大约 30 秒才重连，
            # 这段时间里发什么都会失败，所以直接挂起，等连上再补发。
            return None

        delay = self._as_int("send_interval_ms", 300, 0, 10000) / 1000.0
        all_sent = True
        for group_id in groups:
            umo = f"{platform}:GroupMessage:{group_id}"
            try:
                sent = await self.context.send_message(umo, MessageChain().message(text))
            except Exception as exc:  # noqa: BLE001 - 单个群失败不影响其它群
                name = type(exc).__name__
                if name in DEFERRED_EXCEPTIONS:
                    # NapCat 的反向 WS 还没握手完：不是真失败，挂起等下一轮即可，
                    # 不消耗重试次数，也不刷 WARN 日志。
                    logger.info(f"[mc_chatpool] 平台还没准备好（{name}），稍后自动补发这条消息。")
                    return None
                logger.warning(
                    f"[mc_chatpool] 向群 {group_id} 发送消息失败：{name}: {exc!r}"
                )
                all_sent = False
                continue
            if sent is False:
                # AstrBot 找不到匹配的平台实例时会返回 False 并只打一条核心 WARN，
                # 这里补一条本插件的日志，顺便把探测结果作废，下次重新探测。
                self._platform_id_cache = None
                logger.warning(
                    f"[mc_chatpool] 平台 '{platform}' 下找不到会话 {umo}，这条消息没有送达。"
                    "请检查 platform_id 配置（填 auto 会自动探测）或确认机器人已连接。"
                )
                all_sent = False
            else:
                logger.info(f"[mc_chatpool] 已转发到 {umo}：{text}")
            if delay > 0:
                await asyncio.sleep(delay)
        return all_sent

    # =============================================================== 合并转发

    def _to_node(self, pool: str, message: dict):
        """把一条游戏内消息转成合并转发卡片里的一个 node。

        显示名用 MC 发送者名，内容用 mc_to_qq_format 的渲染结果——与逐条发送时
        完全一致，所以同一条消息在两种模式下观感相同。
        """
        if Node is None:  # pragma: no cover - 老版本 AstrBot 没有合并转发组件
            return None
        sender = str(message.get("sender") or "?")
        sender_id = str(message.get("sender_id") or "")
        # user_id 是 node 的「发送者 QQ 号」。MC 玩家的 sender_id 是 UUID，
        # 塞给协议端有可能让转发报文构造失败；实际显示的是昵称，
        # 所以只在确实像 QQ 号时才带上，否则用组件默认的 "0"。
        uin = sender_id if sender_id.isdigit() else "0"
        return Node(content=[Plain(self._message_text(pool, message))], name=sender, uin=uin)

    async def _send_forward(
        self,
        platform: str,
        umo: str,
        session_id: str,
        is_group: bool,
        nodes: list,
        bot,
        sender,
    ) -> bool | None:
        """把 nodes 以合并转发发出去（超过 merge_forward_max_nodes 会拆成多张卡片）。

        路由：

        * 群聊 → ``send_group_forward_msg``，私聊 → ``send_private_forward_msg``；
        * 能拿到 bot 就自己 call_action——卡片标题（summary）是 action 的顶层参数，
          AstrBot 的 Node/Nodes 组件不携带它，只有这条路径能设标题；
        * 拿不到 bot 就退回 AstrBot 原生 ``Nodes`` 组件链，由框架自己按会话类型路由
          （代价是没有标题）。

        @param bot     aiocqhttp 的 bot 实例（可为 None）
        @param sender  原生组件链的发送函数，签名 (MessageChain) -> awaitable
        @return True=发出去了；False=失败（调用方负责兜底）；None=平台还没连上
        """
        if Nodes is None or not nodes:
            return False
        if self._platform_running(platform) is False:
            return None

        title = self._merge_title()
        max_nodes = self._merge_max_nodes()
        delay = self._as_int("send_interval_ms", 300, 0, 10000) / 1000.0
        cards = [nodes[i:i + max_nodes] for i in range(0, len(nodes), max_nodes)]

        for index, card in enumerate(cards, start=1):
            try:
                if bot is not None:
                    payload = await Nodes(card).to_dict()
                    if is_group:
                        payload["group_id"] = session_id
                    else:
                        payload["user_id"] = session_id
                    if title:
                        # NapCat / OneBot 11 的扩展字段：合并转发卡片的外显标题（摘要）
                        payload["summary"] = title
                    action = FORWARD_ACTIONS[bool(is_group)]
                    await bot.call_action(action, **payload)
                    logger.info(
                        f"[mc_chatpool] 已合并转发 {len(card)} 条到 {session_id}"
                        f"（{action}，标题：{title or '（默认）'}）"
                    )
                else:
                    sent = await sender(MessageChain([Nodes(card)]))
                    if sent is False:
                        logger.warning(
                            f"[mc_chatpool] 平台 '{platform}' 下找不到会话 {umo}，合并转发没有送达。"
                        )
                        return False
                    logger.info(f"[mc_chatpool] 已用原生组件合并转发 {len(card)} 条到 {session_id}")
            except Exception as exc:  # noqa: BLE001 - 失败交给调用方兜底
                name = type(exc).__name__
                if name in DEFERRED_EXCEPTIONS:
                    logger.info(f"[mc_chatpool] 平台还没准备好（{name}），稍后自动补发这批消息。")
                    return None
                logger.warning(f"[mc_chatpool] 合并转发到 {session_id} 失败：{name}: {exc!r}")
                return False

            if delay > 0 and index < len(cards):
                await asyncio.sleep(delay)
        return True

    async def _deliver_merged(self, pool: str, groups: list[str], messages: list[dict]) -> bool | None:
        """把一批消息以合并转发发到该池绑定的所有群。

        任何一步失败都退回 ``_deliver_each`` 逐条发送，保证消息不丢。

        @return True=全部送达；False=有失败；None=平台还没连上
        """
        platform = self._platform_id()
        if self._platform_running(platform) is False:
            return None
        nodes = [node for node in (self._to_node(pool, m) for m in messages) if node is not None]
        if not nodes:
            return True

        bot = self._bot_for(platform)
        for group_id in groups:
            umo = f"{platform}:GroupMessage:{group_id}"
            outcome = await self._send_forward(
                platform,
                umo,
                group_id,
                True,
                nodes,
                bot,
                (lambda chain, target=umo: self.context.send_message(target, chain)),
            )
            if outcome is None:
                return None
            if outcome is False:
                logger.warning(
                    f"[mc_chatpool] 聊天池 {pool} 的 {len(messages)} 条消息合并转发到群 "
                    f"{group_id} 失败，改为逐条发送兜底。"
                )
                # 兜底时如果仍有部分失败，下一轮会把整批重发一遍：宁可重复也不丢消息，
                # 与「游标只按确认送达推进」的既有策略一致。
                return await self._deliver_each(pool, groups, messages)
        return True

    async def _deliver_each(self, pool: str, groups: list[str], messages: list[dict]) -> bool | None:
        """一批消息逐条发送（合并转发的兜底路径）。

        @return True=全部送达；False=有失败；None=平台还没连上
        """
        all_sent = True
        for message in messages:
            outcome = await self._deliver(pool, groups, message)
            if outcome is None:
                return None
            if outcome is False:
                all_sent = False
        return all_sent

    async def _deliver_messages(self, pool: str, groups: list[str], messages: list[dict]) -> bool | None:
        """一批消息的投递入口：条数够多就合并转发，否则维持逐条发送。"""
        if len(messages) >= self._merge_threshold():
            return await self._deliver_merged(pool, groups, messages)
        return await self._deliver_each(pool, groups, messages)

    # =============================================================== 历史读取

    async def _fetch_history(self, pool: str, limit: int, page_size: int) -> tuple[list[dict], int]:
        """从该池最早保留的消息开始，分页拉取整段历史。

        @param limit     最多拉多少条，0 = 不限制
        @param page_size 每页条数（服务端接口上限 200）
        @return (按 id 升序的消息列表, 该池当前最大 id)
        """
        exclude = self._str("poll_exclude_sources", "qq")
        collected: list[dict] = []
        since = 0
        pool_cursor = 0
        while True:
            params = {"pool": pool, "since": since, "limit": page_size}
            if exclude:
                params["exclude_source"] = exclude
            data = await self._api_get("/api/v1/messages", params)
            if not data or not data.get("ok"):
                break

            pool_cursor = max(pool_cursor, int(data.get("pool_cursor") or 0))
            messages = data.get("messages") or []
            if not messages:
                break

            if limit:
                remaining = limit - len(collected)
                if remaining <= 0:
                    break
                messages = messages[:remaining]
            collected.extend(messages)

            newest = int(messages[-1].get("id") or since)
            if newest <= since:  # 游标没前进：防死循环
                break
            since = newest
            if not data.get("has_more") or (limit and len(collected) >= limit):
                break
        return collected, pool_cursor

    async def _sync_full_history(self, pool: str, groups: list[str]) -> bool:
        """read_all_history：首次同步就把该池保留的历史全部转发一遍。

        分页拉取（``history_page_size``）→ 按 ``merge_forward_max_nodes`` 一批批投递
        （每批都会走合并转发）→ 完成后把游标推进到该池最新位置，之后恢复增量。
        总条数受 ``history_max_messages`` 限制（0 = 不限制），免得池子太大把群刷爆。

        中途失败不推进到最新：剩下的会按普通增量补发，不会丢。

        @return 游标是否有变化（与 _poll_pool 的约定一致）
        """
        budget = self._history_max_messages()
        messages, pool_cursor = await self._fetch_history(pool, budget, self._history_page_size())
        if not messages:
            # 没有历史可回放：直接把游标定到当前位置，之后按增量走。
            cursor = pool_cursor or 0
            self._state["cursors"][pool] = cursor
            logger.info(f"[mc_chatpool] 聊天池 {pool} 没有可回放的历史，游标从 {cursor} 开始。")
            return True

        logger.info(f"[mc_chatpool] 首次全量同步聊天池 {pool}：共 {len(messages)} 条历史，开始转发…")
        # 每批取 max_nodes 与 threshold 的较大者：保证「每批都够阈值」从而真的走合并转发，
        # 而 _send_forward 里再按 max_nodes 拆成多张卡片。
        chunk_size = max(1, self._merge_max_nodes(), self._merge_threshold())
        sent = 0
        cursor = 0
        for start in range(0, len(messages), chunk_size):
            chunk = messages[start:start + chunk_size]
            outcome = await self._deliver_messages(pool, groups, chunk)
            if outcome is None:
                logger.info(
                    f"[mc_chatpool] 平台尚未连接，聊天池 {pool} 的全量同步停在第 {sent} 条，稍后继续。"
                )
                self._save_state(force=True)
                return True
            if outcome is False:
                logger.warning(
                    f"[mc_chatpool] 聊天池 {pool} 的全量同步在第 {sent} 条处失败，"
                    "剩下的会按增量补发。"
                )
                self._save_state(force=True)
                return True

            sent += len(chunk)
            cursor = int(chunk[-1].get("id") or cursor)
            # 每批都落盘：中途被打断也能从断点继续
            self._state["cursors"][pool] = cursor
            self._save_state()
            await asyncio.sleep(0)

        if budget and len(messages) >= budget and pool_cursor > cursor:
            logger.warning(
                f"[mc_chatpool] 聊天池 {pool} 的历史超过 history_max_messages={budget}，"
                f"还有 {pool_cursor - cursor} 条没有回放（避免刷屏），游标直接推进到最新。"
            )
        cursor = max(cursor, pool_cursor)
        self._state["cursors"][pool] = cursor
        self._save_state(force=True)
        logger.info(f"[mc_chatpool] 聊天池 {pool} 全量同步完成：转发 {sent} 条，游标推进到 {cursor}。")
        return True

    @staticmethod
    def _render(template: str, values: dict) -> str:
        """单次扫描替换 {占位符}，替换进去的内容不会被二次解析。"""

        def replace(match: re.Match) -> str:
            return str(values.get(match.group(1), match.group(0)))

        return PLACEHOLDER.sub(replace, template or "")

    # =============================================================== 群内命令

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        try:
            await self._handle_group_message(event)
        except Exception as exc:  # noqa: BLE001 - 不能把整条消息管道带崩
            logger.error(f"[mc_chatpool] 处理群消息失败：{exc}", exc_info=True)

    async def _handle_group_message(self, event: AstrMessageEvent) -> None:
        if not self._as_bool("enable_group_to_mc", True):
            return

        group_id = str(event.get_group_id() or "")
        raw = (event.message_str or "").strip()
        if not group_id or not raw:
            return

        # 机器人自己发的消息不处理，避免和自动转发形成回环
        if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
            return

        whitelist = self._group_whitelist()
        if whitelist and group_id not in whitelist:
            return

        matched = None
        for prefix in self._prefixes():
            if raw == prefix or raw.startswith(prefix + " ") or raw.startswith(prefix + "　"):
                matched = prefix
                break
        if matched is None:
            return

        body = raw[len(matched):].strip()
        event.stop_event()

        if not body or body in {"help", "帮助", "?"}:
            await self._reply(event, HELP_TEXT)
            return

        command, _, rest = body.partition(" ")
        command = command.strip().lower()
        rest = rest.strip()

        if command == "status":
            await self._reply(event, await self._status_text())
        elif command == "pools":
            await self._reply(event, await self._pools_text())
        elif command == "history":
            await self._reply(event, await self._history_text(group_id, rest))
        elif command == "all":
            await self._export_all(event, group_id, rest)
        elif command == "bind":
            await self._reply(event, await self._bind(event, group_id, rest))
        elif command == "unbind":
            await self._reply(event, await self._unbind(event, group_id, rest))
        elif command == "send":
            pool, _, text = rest.partition(" ")
            if not pool or not text.strip():
                await self._reply(event, "用法：#mc send <聊天池> <文本>")
                return
            await self._reply(event, await self._send_to_mc(event, pool.strip().lower(), text.strip()))
        else:
            # 没写子命令：整段都当成要发到群绑定池的消息
            await self._reply(event, await self._send_to_mc(event, self._pool_for_group(group_id), body))

    async def _status_text(self) -> str:
        data = await self._api_get("/api/v1/health")
        if not data or not data.get("ok"):
            return f"❌ 连不上 Minecraft 服务器的聊天池接口（{self._base_url()}），请检查服务器是否在运行、token 是否正确。"
        pools = data.get("pools") or []
        return (
            "🟢 Minecraft 服务器在线\n"
            f"玩家：{data.get('online_players')} / {data.get('max_players')}\n"
            f"聊天池：{len(pools)} 个（{', '.join(str(p) for p in pools) or '无'}）\n"
            f"已产生的消息总数：{data.get('next_message_id')}\n"
            f"接口地址：{self._base_url()}"
        )

    async def _pools_text(self) -> str:
        data = await self._api_get("/api/v1/pools")
        if not data or not data.get("ok"):
            return "❌ 无法获取聊天池列表，请检查服务器接口是否可用。"
        pools = data.get("pools") or []
        if not pools:
            return "服务器上还没有任何聊天池。"

        bridges = self._bridges()
        lines = ["📋 服务器聊天池："]
        for pool in pools:
            name = str(pool.get("name", "?"))
            bound = bridges.get(name) or []
            bind_text = f" → 群 {', '.join(bound)}" if bound else " （未绑定群）"
            lines.append(
                f"• {name}（{pool.get('display_name', name)}）"
                f"在线 {pool.get('online', 0)} / 订阅 {pool.get('members', 0)}"
                f"，历史 {pool.get('history', 0)} 条{bind_text}"
            )
        return "\n".join(lines)

    async def _history_text(self, group_id: str, rest: str) -> str:
        parts = rest.split()
        pool = self._pool_for_group(group_id)
        count = 5
        if parts:
            if parts[0].isdigit():
                count = int(parts[0])
            else:
                pool = parts[0].lower()
                if len(parts) > 1 and parts[1].isdigit():
                    count = int(parts[1])
        count = max(1, min(count, 20))

        data = await self._api_get("/api/v1/messages", {"pool": pool, "limit": count})
        if not data or not data.get("ok"):
            return f"❌ 拉取聊天池 {pool} 的历史消息失败。"
        messages = data.get("messages") or []
        if not messages:
            return f"聊天池 {pool} 还没有消息。"

        lines = [f"🕘 聊天池 {pool} 最近 {len(messages)} 条："]
        for message in messages:
            lines.append(self._message_text(pool, message))
        return "\n".join(lines)

    def _parse_all_args(self, group_id: str, rest: str) -> tuple[str, int]:
        """解析 ``#mc all [池] [条数]`` 的参数。

        @return (池名, 条数)；条数 0 表示「全部」（再受 history_max_messages 限制）
        """
        pool = self._pool_for_group(group_id)
        count = 0
        parts = rest.split()
        if parts:
            if parts[0].isdigit():
                count = int(parts[0])
            else:
                pool = parts[0].lower()
                if len(parts) > 1 and parts[1].isdigit():
                    count = int(parts[1])
        return pool, max(0, count)

    async def _export_all(self, event: AstrMessageEvent, group_id: str, rest: str) -> None:
        """#mc all [池] [条数]：把该池历史以合并转发导出到当前会话（群或私聊）。

        与自动转发的区别：这里**无论多少条都用合并转发**（导出语义），
        并且只影响当前会话，不动任何池的拉取游标。
        """
        pool, count = self._parse_all_args(group_id, rest)
        budget = self._history_max_messages()  # 0 = 不限制
        exclude = self._str("poll_exclude_sources", "qq")

        if count and count <= 200:
            # 「最后 N 条」直接用接口的 tail 模式（不带 since）：服务端返回的就是最新的 N 条。
            params = {"pool": pool, "limit": count}
            if exclude:
                params["exclude_source"] = exclude
            data = await self._api_get("/api/v1/messages", params)
            if not data or not data.get("ok"):
                await self._reply(event, f"❌ 拉取聊天池 {pool} 的历史失败。")
                return
            messages = data.get("messages") or []
        else:
            # 「全部」或超过接口单页上限：从最早保留的消息开始分页，再取最后 N 条。
            messages, _ = await self._fetch_history(pool, budget, self._history_page_size())
            if count:
                messages = messages[-count:]

        if not messages:
            await self._reply(event, f"聊天池 {pool} 没有可导出的历史消息。")
            return

        is_group = bool(event.get_group_id())
        session_id = str(event.get_group_id() or event.get_sender_id() or "")
        platform = self._platform_id()
        nodes = [node for node in (self._to_node(pool, m) for m in messages) if node is not None]
        if not nodes:
            await self._reply(event, "❌ 当前 AstrBot 版本不支持合并转发组件，无法导出。")
            return

        outcome = await self._send_forward(
            platform,
            event.unified_msg_origin,
            session_id,
            is_group,
            nodes,
            self._bot_for(platform, event),
            event.send,
        )
        if outcome is True:
            await self._reply(event, f"📤 已导出聊天池 {pool} 的 {len(messages)} 条历史（合并转发）。")
            return
        if outcome is None:
            await self._reply(event, "⏳ 机器人还没连上，稍后再试。")
            return

        logger.warning("[mc_chatpool] #mc all 合并转发失败，改为逐条发送兜底。")
        for message in messages:
            await self._reply(event, self._message_text(pool, message))

    async def _bind(self, event: AstrMessageEvent, group_id: str, rest: str) -> str:
        if not self._is_admin(event):
            return "⛔ 只有管理员（或配置里的 admin_qq_ids）能修改绑定关系。"
        pool = rest.strip().lower()
        if not pool:
            return "用法：#mc bind <聊天池名>"

        data = await self._api_get("/api/v1/pools")
        if not data or not data.get("ok"):
            return "❌ 无法获取聊天池列表，绑定失败。"
        names = {str(item.get("name", "")).lower() for item in (data.get("pools") or [])}
        if names and pool not in names:
            return f"❌ 服务器上没有聊天池 {pool}。可用：{', '.join(sorted(names))}"

        bindings = self._state.setdefault("bindings", {})
        groups = bindings.setdefault(pool, [])
        if group_id in groups:
            return f"本群已经绑定到聊天池 {pool} 了。"
        groups.append(group_id)
        self._save_state(force=True)
        return f"✅ 已把本群绑定到聊天池 {pool}，之后这个池的游戏内消息会发到本群。"

    async def _unbind(self, event: AstrMessageEvent, group_id: str, rest: str) -> str:
        if not self._is_admin(event):
            return "⛔ 只有管理员（或配置里的 admin_qq_ids）能修改绑定关系。"
        bindings = self._state.setdefault("bindings", {})
        targets = [rest.strip().lower()] if rest.strip() else list(bindings.keys())

        removed = []
        for pool in targets:
            groups = bindings.get(pool)
            if groups and group_id in groups:
                groups.remove(group_id)
                if not groups:
                    bindings.pop(pool, None)
                removed.append(pool)
        self._save_state(force=True)

        if not removed:
            return "本群没有通过命令绑定过聊天池（如果配置的 bridges 里写死了本群，请去配置里改）。"

        still = [pool for pool in removed if group_id in (self._config_bridges.get(pool) or [])]
        text = f"✅ 已解除绑定：{', '.join(removed)}"
        if still:
            text += f"\n⚠️ 但配置 bridges 里仍然绑定了：{', '.join(still)}，需要改插件配置才会彻底断开。"
        return text

    async def _send_to_mc(self, event: AstrMessageEvent, pool: str, text: str) -> str:
        text = text.strip()
        if not text:
            return "用法：#mc <文本> 或 #mc send <聊天池> <文本>"

        limit = self._as_int("max_message_length", 300, 1, 2000)
        truncated = False
        if len(text) > limit:
            text = text[:limit]
            truncated = True

        nickname = event.get_sender_name() or str(event.get_sender_id() or "QQ用户")
        sender = self._render(
            self._str("qq_sender_format", "{nickname}"),
            {
                "nickname": nickname,
                "qq": str(event.get_sender_id() or ""),
                "group_id": str(event.get_group_id() or ""),
                "pool": pool,
            },
        )

        data = await self._api_post(
            "/api/v1/messages",
            {
                "pool": pool,
                "sender": sender,
                "sender_id": str(event.get_sender_id() or ""),
                "content": text,
                "source": "qq",
            },
        )
        if data is None:
            return "❌ 服务器没有响应，请检查 Paper 服务器与 http.token 配置。"
        if not data.get("ok"):
            return f"❌ 发送失败：{data.get('error', '未知错误')}"

        suffix = "（已截断）" if truncated else ""
        return (
            f"✅ 已发送到聊天池 {data.get('pool', pool)}（#{data.get('id')}）{suffix}\n"
            f"游戏内在线可见人数：{data.get('delivered', 0)}"
        )

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "")
        if sender and sender in self._admins():
            return True
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001 - 平台不提供就只认配置
            return False

    async def _reply(self, event: AstrMessageEvent, text: str) -> None:
        if not text or not self._as_bool("reply_to_group", True):
            return
        chain = MessageChain().message(text)
        try:
            await event.send(chain)
        except Exception as exc:  # noqa: BLE001 - 回退到核心 API
            logger.debug(f"[mc_chatpool] event.send 失败（{exc}），改用 context.send_message。")
            try:
                await self.context.send_message(event.unified_msg_origin, chain)
            except Exception as exc2:  # noqa: BLE001
                logger.warning(f"[mc_chatpool] 回复群消息失败：{exc2}")
