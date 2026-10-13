"""astrbot_plugin_mc_chatpool 的离线自测。

不需要 AstrBot 运行时：这里把 `astrbot.*` 与 `aiohttp` 都用最小桩件顶替，
然后直接加载 ../main.py，验证「桥接解析 / 占位符渲染 / 群命令 / 轮询游标」这些纯逻辑。

用法：
    python tests/test_offline.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
MAIN_PY = PLUGIN_DIR / "main.py"

PASSED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name} {detail}")
        raise AssertionError(f"{name} {detail}")


# --------------------------------------------------------------------------- 桩件


class FakeLogger:
    def info(self, *args, **kwargs) -> None: ...
    def debug(self, *args, **kwargs) -> None: ...
    def warning(self, *args, **kwargs) -> None: ...
    def error(self, *args, **kwargs) -> None: ...


class FakeMessageChain:
    def __init__(self, components=None):
        self.components = list(components or [])

    def message(self, text):
        self.components.append(("text", text))
        return self

    @property
    def text(self) -> str:
        """纯文本部分：Nodes 之类的非文本组件不贡献文本（与 AstrBot 行为一致）。"""
        parts = []
        for item in self.components:
            if isinstance(item, tuple):
                if item[0] == "text":
                    parts.append(item[1])
            else:
                parts.append(getattr(item, "text", ""))
        return "".join(parts)


class FakeNode:
    """Node 组件的替身：只保留合并转发用得到的字段。"""

    def __init__(self, content=None, **kwargs):
        self.content = list(content or [])
        self.name = kwargs.get("name", "")
        self.uin = str(kwargs.get("uin", "0"))

    def text(self) -> str:
        return "".join(getattr(item, "text", "") for item in self.content)


class FakeNodes:
    """Nodes 组件的替身，to_dict() 的形状与 AstrBot 的保持一致。"""

    def __init__(self, nodes=None, **kwargs):
        self.nodes = list(nodes or [])

    async def to_dict(self) -> dict:
        return {
            "messages": [
                {
                    "type": "node",
                    "data": {
                        "user_id": node.uin,
                        "nickname": node.name,
                        "content": [
                            {"type": "text", "data": {"text": getattr(item, "text", "")}}
                            for item in node.content
                        ],
                    },
                }
                for node in self.nodes
            ]
        }


class FakeBot:
    """aiocqhttp bot 的替身：记录 call_action 的调用，可选择抛异常。"""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def call_action(self, action: str, **payload):
        self.calls.append((action, payload))
        if self.fail:
            raise RuntimeError("napcat 拒绝了这次 call_action")
        return {"message_id": 1}


def cards_in(chains) -> list:
    """从若干 MessageChain 里挑出合并转发组件。"""
    cards = []
    for chain in chains:
        for component in chain.components:
            if isinstance(component, FakeNodes):
                cards.append(component)
    return cards


def merged_cards(context) -> list:
    """取出 context 里以 Nodes 组件发出的合并转发卡片。"""
    return cards_in([chain for _, chain in getattr(context, "chains", [])])


def sent_cards(event) -> list:
    """取出 event.send() 发出的合并转发卡片。"""
    return cards_in(getattr(event, "sent_chains", []))


class FakeFilter:
    class EventMessageType:
        GROUP_MESSAGE = "GroupMessage"
        PRIVATE_MESSAGE = "FriendMessage"

    @staticmethod
    def event_message_type(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    @staticmethod
    def command(*args, **kwargs):
        def decorator(func):
            return func

        return decorator


class FakeStar:
    def __init__(self, context=None):
        self.context = context


class FakeStarTools:
    data_root = Path(tempfile.mkdtemp(prefix="mcchatpool-test-"))

    @staticmethod
    def get_data_dir(name: str | None = None) -> Path:
        target = FakeStarTools.data_root / str(name or "unknown")
        target.mkdir(parents=True, exist_ok=True)
        return target


def fake_register(*args, **kwargs):
    def decorator(cls):
        return cls

    return decorator


class FakeAiohttp:
    """只提供 main.py 用到的几个名字。"""

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    class ClientError(Exception):
        pass

    class ClientSession:  # pragma: no cover - 自测里不会真的建会话
        def __init__(self, *args, **kwargs):
            self.closed = False

        async def close(self):
            self.closed = True


def install_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = FakeLogger()

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event_mod.filter = FakeFilter()
    event_mod.MessageChain = FakeMessageChain

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = type("Context", (), {})
    star_mod.Star = FakeStar
    star_mod.register = fake_register
    star_mod.StarTools = FakeStarTools

    components_mod = types.ModuleType("astrbot.api.message_components")
    components_mod.Plain = type("Plain", (), {"__init__": lambda self, text="": setattr(self, "text", text)})
    components_mod.Node = FakeNode
    components_mod.Nodes = FakeNodes

    core = types.ModuleType("astrbot.core")
    core_msg = types.ModuleType("astrbot.core.message")
    core_msg_res = types.ModuleType("astrbot.core.message.message_event_result")
    core_msg_res.MessageChain = FakeMessageChain
    core_comp = types.ModuleType("astrbot.core.message.components")
    core_comp.Plain = components_mod.Plain
    core_comp.Node = FakeNode
    core_comp.Nodes = FakeNodes
    core_star = types.ModuleType("astrbot.core.star")
    core_star_tools = types.ModuleType("astrbot.core.star.star_tools")
    core_star_tools.StarTools = FakeStarTools

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_mod,
        "astrbot.api.star": star_mod,
        "astrbot.api.message_components": components_mod,
        "astrbot.core": core,
        "astrbot.core.message": core_msg,
        "astrbot.core.message.message_event_result": core_msg_res,
        "astrbot.core.message.components": core_comp,
        "astrbot.core.star": core_star,
        "astrbot.core.star.star_tools": core_star_tools,
        "aiohttp": FakeAiohttp,
    }
    sys.modules.update(modules)
    astrbot.api = api


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("mc_chatpool_main", MAIN_PY)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- 测试替身


class FakeEvent:
    def __init__(self, text: str, group_id: str = "123456789", sender_id: str = "10001", sender_name: str = "小明"):
        self.message_str = text
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._self_id = "1000000000"   # 机器人自身的 QQ 号（测试用假值）
        self.unified_msg_origin = f"aiocqhttp:GroupMessage:{group_id}"
        self.stopped = False
        self.sent: list[str] = []
        self.sent_chains: list = []    # event.send() 发出的完整 chain
        self._admin = False

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_self_id(self):
        return self._self_id

    def stop_event(self):
        self.stopped = True

    def is_admin(self):
        return self._admin

    async def send(self, chain):
        self.sent.append(chain.text)
        self.sent_chains.append(chain)
        return True


class FakeContext:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []
        self.chains: list = []          # 完整 chain，便于检查合并转发组件

    async def send_message(self, umo, chain):
        self.messages.append((umo, chain.text))
        self.chains.append((umo, chain))
        return True


# --------------------------------------------------------------------------- 用例


def main() -> int:
    install_stubs()
    module = load_plugin_module()
    plugin = module.McChatPoolBridge(FakeContext(), config={
        "mc_base_url": "http://127.0.0.1:8123",
        "mc_token": "token-abc",
        "bridges": (
            "# 全服池 -> 两个群\n"
            "global = 123456789, 987654321\n"
            "tech = 555555\n"
            "这行没有等号，应该被忽略\n"
        ),
        "default_pool": "global",
        "poll_exclude_sources": "qq",
        "max_message_length": 20,
        "admin_qq_ids": ["99999"],
    })
    plugin._state_file = FakeStarTools.get_data_dir(module.PLUGIN_NAME) / "state.json"
    plugin._config_bridges = plugin._parse_bridges()

    # 1. bridges 行格式
    check(
        "bridges 行格式解析",
        plugin._config_bridges == {"global": ["123456789", "987654321"], "tech": ["555555"]},
        str(plugin._config_bridges),
    )

    # 2. bridges JSON 格式
    plugin.config["bridges"] = '[{"pool":"Global","groups":["1","2"]},{"pool":"tech","group":"3"}]'
    check("bridges JSON 格式解析", plugin._parse_bridges() == {"global": ["1", "2"], "tech": ["3"]})
    plugin.config["bridges"] = "global = 123456789, 987654321\ntech = 555555\n"

    # 3. 占位符渲染（内容里的 {content} 不应被二次替换）
    rendered = plugin._render("[MC] {sender}: {content}", {"sender": "Steve", "content": "我写了 {content} 这几个字"})
    check("占位符渲染与二次替换防护", rendered == "[MC] Steve: 我写了 {content} 这几个字", rendered)

    # 4. 群 -> 池 的映射与状态绑定合并
    plugin._state["bindings"] = {"tech": ["777777"]}
    bridges = plugin._bridges()
    check("配置与命令绑定取并集", "777777" in bridges["tech"] and "123456789" in bridges["global"], str(bridges))
    check("按群反查池", plugin._pools_of_group("777777") == ["tech"], str(plugin._pools_of_group("777777")))
    check("未绑定群落到默认池", plugin._pool_for_group("000000") == "global")

    # 5. #mc help
    event = FakeEvent("#mc help")
    asyncio.run(plugin._handle_group_message(event))
    check("help 命中且事件被终止", event.stopped and any("MC 聊天池命令" in m for m in event.sent), str(event.sent))

    # 6. 普通文本 -> 默认池
    posted: list[tuple[str, dict]] = []

    async def fake_post(path, payload):
        posted.append((path, payload))
        return {"ok": True, "id": 42, "pool": payload["pool"], "delivered": 3}

    plugin._api_post = fake_post
    event = FakeEvent("#mc 大家好啊")
    asyncio.run(plugin._handle_group_message(event))
    check(
        "群消息投递到默认池",
        posted and posted[0][1]["pool"] == "global" and posted[0][1]["content"] == "大家好啊"
        and posted[0][1]["sender"] == "小明" and posted[0][1]["source"] == "qq",
        str(posted),
    )
    check("回执里带投递结果", any("delivered" not in m and "3" in m for m in event.sent), str(event.sent))

    # 7. #mc send <池> <文本>
    posted.clear()
    event = FakeEvent("#mc send tech 红石机器又炸了")
    asyncio.run(plugin._handle_group_message(event))
    check("指定池发送", posted and posted[0][1]["pool"] == "tech", str(posted))

    # 8. 截断
    posted.clear()
    event = FakeEvent("#mc " + "很长的一段话" * 10)
    asyncio.run(plugin._handle_group_message(event))
    check("超长消息被截断到 20 字", len(posted[0][1]["content"]) == 20, str(len(posted[0][1]["content"])))
    check("回执提示已截断", any("已截断" in m for m in event.sent), str(event.sent))

    # 9. sync_server 名替换
    plugin.config["qq_sender_format"] = "{nickname}(QQ{qq})"
    posted.clear()
    asyncio.run(plugin._handle_group_message(FakeEvent("#mc 测试名字")))
    check("QQ 成员名模板生效", posted[0][1]["sender"] == "小明(QQ10001)", str(posted[0][1]["sender"]))
    plugin.config["qq_sender_format"] = "{nickname}"

    # 10. 非前缀消息不处理
    event = FakeEvent("今天天气不错")
    asyncio.run(plugin._handle_group_message(event))
    check("无前缀消息被忽略", not event.stopped and not event.sent)

    # 11. 机器人自己的消息被忽略
    event = FakeEvent("#mc 我自己发的")
    event._sender_id = event._self_id
    asyncio.run(plugin._handle_group_message(event))
    check("机器人自身消息被忽略", not event.stopped)

    # 12. status
    async def fake_get(path, params=None):
        if path == "/api/v1/health":
            return {"ok": True, "online_players": 2, "max_players": 20, "pools": ["global", "tech"], "next_message_id": 88}
        if path == "/api/v1/pools":
            return {"ok": True, "pools": [
                {"name": "global", "display_name": "全服", "online": 2, "members": 3, "history": 10},
                {"name": "tech", "display_name": "技术", "online": 1, "members": 2, "history": 4},
            ]}
        return None

    plugin._api_get = fake_get
    event = FakeEvent("#mc status")
    asyncio.run(plugin._handle_group_message(event))
    check("status 输出服务器概况", event.sent and "Minecraft 服务器在线" in event.sent[0] and "88" in event.sent[0], str(event.sent))

    event = FakeEvent("#mc pools")
    asyncio.run(plugin._handle_group_message(event))
    check("pools 列出池与绑定", event.sent and "global" in event.sent[0] and "123456789" in event.sent[0], str(event.sent))

    # 13. bind / unbind 权限
    event = FakeEvent("#mc bind tech", group_id="777777")
    asyncio.run(plugin._handle_group_message(event))
    check("非管理员不能 bind", event.sent and "⛔" in event.sent[0], str(event.sent))

    event = FakeEvent("#mc bind tech", group_id="777777", sender_id="99999")
    asyncio.run(plugin._handle_group_message(event))
    check("配置里的管理员可以 bind", "777777" in plugin._state["bindings"].get("tech", []), str(plugin._state["bindings"]))

    event = FakeEvent("#mc bind 不存在的池", group_id="777777", sender_id="99999")
    asyncio.run(plugin._handle_group_message(event))
    check("bind 不存在的池会报错", event.sent and "❌" in event.sent[0], str(event.sent))

    event = FakeEvent("#mc unbind", group_id="777777", sender_id="99999")
    asyncio.run(plugin._handle_group_message(event))
    check("unbind 生效", "777777" not in plugin._state["bindings"].get("tech", []), str(plugin._state["bindings"]))

    # 14. 落盘
    plugin._save_state(force=True)
    saved = json.loads(plugin._state_file.read_text(encoding="utf-8"))
    check("state.json 可读回", "cursors" in saved and "bindings" in saved, str(saved))

    # 15. 轮询：首次只记游标不回放
    calls: list[tuple[str, dict]] = []

    async def first_get(path, params=None):
        calls.append((path, dict(params or {})))
        return {"ok": True, "cursor": 17, "pool_cursor": 17, "messages": []}

    plugin._api_get = first_get
    plugin._state["cursors"] = {}
    plugin.config["skip_backlog_on_first_poll"] = True
    plugin.context.messages.clear()
    changed = asyncio.run(plugin._poll_pool("global", ["123456789"]))
    check("首次拉取只记录游标", changed and plugin._state["cursors"]["global"] == 17, str(plugin._state["cursors"]))
    check("首次拉取不回放历史", not plugin.context.messages)
    check("首次拉取用 tail 模式（不带 since）", "since" not in calls[0][1], str(calls))

    # 16. 增量拉取并转发
    calls.clear()

    async def second_get(path, params=None):
        calls.append((path, dict(params or {})))
        return {
            "ok": True,
            "cursor": 19,
            "pool_cursor": 19,
            "has_more": False,
            "messages": [
                {"id": 18, "pool": "global", "source": "mc", "sender": "Steve", "sender_id": "uuid-1", "content": "有人一起打末影龙吗"},
                {"id": 19, "pool": "global", "source": "console", "sender": "控制台", "sender_id": "console", "content": "服务器 5 分钟后重启"},
            ],
        }

    plugin._api_get = second_get
    plugin.context.messages.clear()
    changed = asyncio.run(plugin._poll_pool("global", ["123456789", "987654321"]))
    check("增量拉取推进游标", changed and plugin._state["cursors"]["global"] == 19, str(plugin._state["cursors"]))
    check("增量拉取带 since 与排除来源", calls[0][1].get("since") == 17 and calls[0][1].get("exclude_source") == "qq", str(calls))
    check("两条消息发到两个群", len(plugin.context.messages) == 4, str(plugin.context.messages))
    check(
        "转发格式正确",
        plugin.context.messages[0] == ("aiocqhttp:GroupMessage:123456789", "[MC] Steve: 有人一起打末影龙吗"),
        str(plugin.context.messages[0]),
    )

    # 17. has_more 时连拉多轮
    rounds = {"n": 0}

    async def paged_get(path, params=None):
        rounds["n"] += 1
        since = params.get("since")
        if since == 0:
            return {"ok": True, "cursor": 1, "has_more": True,
                    "messages": [{"id": 1, "sender": "A", "content": "第一条", "source": "mc"}]}
        return {"ok": True, "cursor": 2, "has_more": False,
                "messages": [{"id": 2, "sender": "B", "content": "第二条", "source": "mc"}]}

    plugin._api_get = paged_get
    plugin._state["cursors"] = {"global": 0}
    plugin.context.messages.clear()
    asyncio.run(plugin._poll_pool("global", ["123456789"]))
    check("has_more 时自动连拉", rounds["n"] == 2 and len(plugin.context.messages) == 2, f"rounds={rounds['n']}")

    # 18. 接口不可用时安全退出
    async def dead_get(path, params=None):
        return None

    plugin._api_get = dead_get
    changed = asyncio.run(plugin._poll_pool("global", ["123456789"]))
    check("接口异常不抛异常", changed is False)

    # 19. 平台 ID 自动探测（AstrBot 里实例 ID 是用户起的名字，跟适配器类型无关）
    class FakeMeta:
        def __init__(self, pid: str, name: str):
            self.id = pid
            self.name = name

    class FakePlatform:
        def __init__(self, pid: str, name: str):
            self._meta = FakeMeta(pid, name)

        def meta(self):
            return self._meta

    class FakePlatformManager:
        def __init__(self, insts):
            self._insts = insts

        def get_insts(self):
            return self._insts

    plugin.config["platform_id"] = "auto"
    plugin._platform_id_cache = None
    plugin.context.platform_manager = FakePlatformManager([FakePlatform("default", "aiocqhttp")])
    check("平台 ID 自动探测用实例 id（而不是适配器名）", plugin._platform_id() == "default", plugin._platform_id())

    plugin._platform_id_cache = None
    plugin.context.platform_manager = FakePlatformManager([])
    check(
        "平台还没注册时不缓存探测结果",
        plugin._platform_id() == "aiocqhttp" and plugin._platform_id_cache is None,
        f"cache={plugin._platform_id_cache}",
    )

    plugin.config["platform_id"] = "my-qq"
    check("手写 platform_id 时不做探测", plugin._platform_id() == "my-qq")

    # 20. 投递失败不推进游标，重试到上限后放弃
    plugin.config["platform_id"] = "auto"
    plugin._platform_id_cache = None
    plugin.context.platform_manager = FakePlatformManager([FakePlatform("default", "aiocqhttp")])
    plugin.config["bridges"] = "global = 123456789"
    plugin._config_bridges = plugin._parse_bridges()
    plugin._failed_ids = {}

    async def one_message(path, params=None):
        return {"ok": True, "cursor": 11, "has_more": False,
                "messages": [{"id": 11, "sender": "Steve", "content": "发不出去的消息", "source": "mc"}]}

    async def failing_send(umo, chain):
        return False

    plugin._api_get = one_message
    plugin.context = FakeContext()
    plugin.context.send_message = failing_send
    plugin._state["cursors"] = {"global": 10}

    asyncio.run(plugin._poll_pool("global", ["123456789"]))
    check("投递失败时游标停住", plugin._state["cursors"]["global"] == 10, str(plugin._state["cursors"]))
    check("记录失败次数", plugin._failed_ids.get(11) == 1, str(plugin._failed_ids))

    for _ in range(module.MAX_DELIVERY_RETRIES - 1):
        asyncio.run(plugin._poll_pool("global", ["123456789"]))
    check("重试到上限后放弃并推进游标", plugin._state["cursors"]["global"] == 11, str(plugin._state["cursors"]))
    check("放弃后清掉失败计数", 11 not in plugin._failed_ids, str(plugin._failed_ids))

    # ------------------------------------------------------- 合并转发 / 全量读取

    class FakePlatformBot:
        def __init__(self, pid: str, name: str, bot=None):
            self._meta = FakeMeta(pid, name)
            self.bot = bot

        def meta(self):
            return self._meta

    def reset_runtime(bot=None, platform_id: str = "default") -> None:
        """把插件恢复到「平台已注册、游标为空」的干净状态，便于各用例独立观察。"""
        plugin._platform_id_cache = None
        plugin._failed_ids = {}
        plugin._deferred_warned = {}
        plugin._state["cursors"] = {}
        plugin.config["skip_backlog_on_first_poll"] = False  # 让首次轮询直接从 since=0 开始
        plugin.context = FakeContext()
        plugin.context.platform_manager = FakePlatformManager([FakePlatformBot(platform_id, "aiocqhttp", bot)])
        plugin._config_bridges = plugin._parse_bridges()

    def make_messages(count: int) -> list[dict]:
        return [
            {"id": i, "source": "mc", "sender": f"S{i}", "sender_id": f"uuid-{i}", "content": f"消息{i}"}
            for i in range(1, count + 1)
        ]

    def paged_stub(messages: list[dict]):
        """一次给完的增量接口替身。"""

        async def stub(path, params=None):
            newest = messages[-1]["id"]
            if params.get("since") == 0:
                return {"ok": True, "cursor": newest, "pool_cursor": newest, "has_more": False, "messages": messages}
            return {"ok": True, "cursor": newest, "pool_cursor": newest, "has_more": False, "messages": []}

        return stub

    plugin.config["bridges"] = "global = 123456789"
    plugin.config.pop("merge_forward_threshold", None)
    plugin.config.pop("read_all_history", None)

    # 21. 不足阈值：维持逐条发送（默认行为不变）
    reset_runtime()
    plugin._api_get = paged_stub(make_messages(5))
    asyncio.run(plugin._poll_pool("global", ["123456789"]))
    check(
        "5 条（< 阈值 6）仍然逐条发送",
        len(plugin.context.messages) == 5
        and not merged_cards(plugin.context)
        and plugin._state["cursors"]["global"] == 5,
        str(plugin.context.messages),
    )

    # 22. 达到阈值：一次合并转发，游标一次推进到最新
    reset_runtime()
    plugin._api_get = paged_stub(make_messages(6))
    asyncio.run(plugin._poll_pool("global", ["123456789"]))
    cards = merged_cards(plugin.context)
    check(
        "6 条（>= 阈值 6）改用一次合并转发",
        len(cards) == 1 and len(cards[0].nodes) == 6 and plugin._state["cursors"]["global"] == 6,
        f"cards={[len(c.nodes) for c in cards]}",
    )

    # 23. node 的显示名与内容（与逐条发送的渲染结果一致）
    node = plugin._to_node("global", {"id": 7, "sender": "Steve", "sender_id": "uuid-x", "content": "你好"})
    check(
        "node 显示名用 MC 发送者名、内容用 mc_to_qq_format 渲染结果",
        node.name == "Steve" and node.text() == "[MC] Steve: 你好",
        node.text(),
    )
    check("node 的 user_id 对 UUID 退化为 0", node.uin == "0", node.uin)
    node = plugin._to_node("global", {"id": 8, "sender": "群友", "sender_id": "10001", "content": "hi"})
    check("node 的 user_id 对纯数字（QQ）保留", node.uin == "10001", node.uin)

    # 24. 卡片标题与拆分
    plugin.config["merge_forward_max_nodes"] = 2
    bot = FakeBot()
    nodes = [plugin._to_node("global", message) for message in make_messages(5)]
    ok = asyncio.run(plugin._send_forward("default", "aiocqhttp:GroupMessage:123", "123", True, nodes, bot, None))
    check(
        "超过 merge_forward_max_nodes 拆成多张卡片",
        ok is True and [len(call[1]["messages"]) for call in bot.calls] == [2, 2, 1],
        str([len(call[1]["messages"]) for call in bot.calls]),
    )
    check(
        "群聊走 send_group_forward_msg 且带 group_id",
        all(call[0] == "send_group_forward_msg" and call[1].get("group_id") == "123" for call in bot.calls),
        str(bot.calls[0][0]),
    )
    check(
        "卡片带上配置的标题（summary）",
        all(call[1].get("summary") == "MC 聊天记录" for call in bot.calls),
        str(bot.calls[0][1].get("summary")),
    )
    check(
        "payload 里的昵称与内容来自 node",
        bot.calls[0][1]["messages"][0]["data"]["nickname"] == "S1"
        and bot.calls[0][1]["messages"][0]["data"]["content"][0]["data"]["text"] == "[MC] S1: 消息1",
        str(bot.calls[0][1]["messages"][0]),
    )

    bot = FakeBot()
    asyncio.run(plugin._send_forward("default", "aiocqhttp:FriendMessage:9", "9", False, nodes[:1], bot, None))
    check(
        "私聊走 send_private_forward_msg 且带 user_id",
        bot.calls[0][0] == "send_private_forward_msg" and bot.calls[0][1].get("user_id") == "9",
        str(bot.calls[0]),
    )

    plugin.config["merge_forward_title"] = ""
    bot = FakeBot()
    asyncio.run(plugin._send_forward("default", "aiocqhttp:GroupMessage:123", "123", True, nodes[:1], bot, None))
    check("标题留空时不传 summary", "summary" not in bot.calls[0][1], str(list(bot.calls[0][1].keys())))
    plugin.config["merge_forward_title"] = "MC 聊天记录"
    plugin.config["merge_forward_max_nodes"] = 100

    # 25. 合并转发失败 -> 逐条兜底（不能丢消息）
    reset_runtime(bot=FakeBot(fail=True))
    plugin._api_get = paged_stub(make_messages(6))
    asyncio.run(plugin._poll_pool("global", ["123456789"]))
    check(
        "合并转发失败后退回逐条发送",
        len(plugin.context.messages) == 6 and not merged_cards(plugin.context),
        str(len(plugin.context.messages)),
    )
    check("兜底发送成功后游标照样推进", plugin._state["cursors"]["global"] == 6, str(plugin._state["cursors"]))

    # 26. 全量分页累积
    limits: list = []

    async def history_stub(path, params=None):
        limits.append(params.get("limit"))
        since = params.get("since")
        if since == 0:
            return {"ok": True, "cursor": 3, "pool_cursor": 7, "has_more": True, "messages": make_messages(3)}
        if since == 3:
            return {
                "ok": True,
                "cursor": 6,
                "pool_cursor": 7,
                "has_more": True,
                "messages": [{"id": i, "sender": "A", "content": f"c{i}"} for i in (4, 5, 6)],
            }
        return {
            "ok": True,
            "cursor": 7,
            "pool_cursor": 7,
            "has_more": False,
            "messages": [{"id": 7, "sender": "A", "content": "c7"}],
        }

    plugin._api_get = history_stub
    plugin.config["history_page_size"] = 3
    collected, pool_cursor = asyncio.run(plugin._fetch_history("global", 0, plugin._history_page_size()))
    check(
        "分页累积读完整段历史",
        [m["id"] for m in collected] == [1, 2, 3, 4, 5, 6, 7] and pool_cursor == 7 and len(limits) == 3,
        f"ids={[m['id'] for m in collected]} pages={len(limits)}",
    )
    check("分页每页用 history_page_size", all(limit == 3 for limit in limits), str(limits))

    limits.clear()
    collected, _ = asyncio.run(plugin._fetch_history("global", 4, 3))
    check(
        "history_max_messages 限制累积条数",
        [m["id"] for m in collected] == [1, 2, 3, 4],
        str([m["id"] for m in collected]),
    )
    plugin.config["history_page_size"] = 200

    # 27. read_all_history：首次同步回放全部历史
    reset_runtime()
    plugin.config["read_all_history"] = True
    plugin.config["merge_forward_max_nodes"] = 2
    plugin.config["merge_forward_threshold"] = 6
    since_seen: list = []

    async def full_stub(path, params=None):
        since_seen.append(params.get("since"))
        newest = 6
        if params.get("since") == 0:
            return {"ok": True, "cursor": newest, "pool_cursor": newest, "has_more": False, "messages": make_messages(6)}
        return {"ok": True, "cursor": newest, "pool_cursor": newest, "has_more": False, "messages": []}

    plugin._api_get = full_stub
    changed = asyncio.run(plugin._poll_pool("global", ["123456789"]))
    check("read_all_history 首次同步从 since=0 开始", changed and since_seen and since_seen[0] == 0, str(since_seen))
    check("全量同步结束后游标推进到最新", plugin._state["cursors"]["global"] == 6, str(plugin._state["cursors"]))
    cards = merged_cards(plugin.context)
    check(
        "全量同步按 merge_forward_max_nodes 拆成合并转发卡片",
        [len(card.nodes) for card in cards] == [2, 2, 2],
        str([len(card.nodes) for card in cards]),
    )
    plugin.config["read_all_history"] = False

    # 28. #mc all 参数解析与导出
    plugin.config["merge_forward_max_nodes"] = 100
    check("all 缺省用本群绑定的池、条数为全部", plugin._parse_all_args("123456789", "") == ("global", 0))
    check("all 只给池名", plugin._parse_all_args("123456789", "tech") == ("tech", 0))
    check("all 只给条数", plugin._parse_all_args("123456789", "30") == ("global", 30))
    check("all 池名 + 条数", plugin._parse_all_args("123456789", "tech 30") == ("tech", 30))
    check("all 条数非法时按全部处理", plugin._parse_all_args("123456789", "tech abc") == ("tech", 0))

    export_calls: list = []

    async def export_stub(path, params=None):
        export_calls.append(dict(params or {}))
        all_messages = make_messages(6)
        if "since" not in params:  # tail 模式：服务端直接给最新的 N 条
            return {
                "ok": True,
                "cursor": 6,
                "pool_cursor": 6,
                "has_more": False,
                "messages": all_messages[-int(params.get("limit", 5)):],
            }
        return {
            "ok": True,
            "cursor": 6,
            "pool_cursor": 6,
            "has_more": False,
            "messages": all_messages if params["since"] == 0 else [],
        }

    reset_runtime()
    plugin._api_get = export_stub
    event = FakeEvent("#mc all 3")
    asyncio.run(plugin._handle_group_message(event))
    cards = sent_cards(event)
    check(
        "#mc all 3 用 tail 模式取最新 3 条并合并转发",
        len(cards) == 1 and len(cards[0].nodes) == 3 and "since" not in export_calls[0],
        f"cards={[len(card.nodes) for card in cards]} calls={export_calls}",
    )
    check(
        "#mc all 导出的是最新那几条",
        [n.text() for n in cards[0].nodes][-1] == "[MC] S6: 消息6",
        str([n.text() for n in cards[0].nodes]),
    )
    check("all 会回复导出结果", any("已导出" in message for message in event.sent), str(event.sent))

    reset_runtime()
    export_calls.clear()
    plugin._api_get = export_stub
    event = FakeEvent("#mc all")
    asyncio.run(plugin._handle_group_message(event))
    cards = sent_cards(event)
    check(
        "#mc all 不带条数时导出全部（分页 + 合并转发）",
        len(cards) == 1 and len(cards[0].nodes) == 6 and export_calls[0].get("since") == 0,
        f"cards={[len(card.nodes) for card in cards]} calls={export_calls}",
    )
    plugin.config["skip_backlog_on_first_poll"] = True

    # ============================================================ 推送接收端（push 模式）
    #
    # 接收端刻意把「校验 + 攒批」抽成 _handle_push_request（不碰 HTTP、不 await），
    # 所以这里不需要起真的 HTTP 服务就能把每条分支测到；HTTP 层是薄封装，
    # 它的真实行为另外在容器里用真 aiohttp 跑过（见交付说明）。

    plugin.config.update({
        "enable_receive": True,
        "receive_bind": "127.0.0.1",
        "receive_port": 6198,
        "receive_path": "/mc-chatpool/push",
        "receive_token": "push-token",
        "receive_merge_window_ms": 150,
        "receive_max_batch": 200,
        "receive_skip_sources": "qq",
    })
    plugin._config_bridges = {"global": ["123456789"], "tech": ["555555"]}

    # 配置读取
    check("推送：默认 bind/port", plugin._receive_bind() == "127.0.0.1" and plugin._receive_port() == 6198)
    check("推送：路径原样保留", plugin._receive_path() == "/mc-chatpool/push")
    plugin.config["receive_path"] = "no-slash/push/"
    check("推送：路径自动补前导斜杠并去掉结尾斜杠", plugin._receive_path() == "/no-slash/push")
    plugin.config["receive_path"] = "/mc-chatpool/push"
    plugin.config["receive_skip_sources"] = "qq, mc、console"
    check("推送：skip_sources 多值解析", plugin._receive_skip_sources() == {"qq", "mc", "console"})
    plugin.config["receive_skip_sources"] = "qq"

    def push(raw, header):
        return plugin._handle_push_request(raw, header)

    def push_json(token="push-token", **payload):
        header = f"Bearer {token}" if token else None
        return push(json.dumps(payload, ensure_ascii=False).encode("utf-8"), header)

    good = json.dumps({"pool": "global", "id": 1, "source": "mc", "sender": "Steve",
                       "sender_id": "u-1", "content": "hi"}, ensure_ascii=False).encode("utf-8")

    # 校验顺序：token → JSON → 必填字段 → 绑定 → 来源
    code, body = push(good, "Bearer push-token")
    check("推送：正常 -> 200 queued=1", code == 200 and body == {"ok": True, "queued": 1}, f"{code} {body}")
    check("推送：缺 token -> 401", push(good, None)[0] == 401)
    check("推送：错 token -> 401", push(good, "Bearer wrong")[0] == 401)
    check("推送：不带 Bearer 前缀 -> 401", push(good, "push-token")[0] == 401)
    check("推送：坏 JSON -> 400", push(b"{not json", "Bearer push-token")[0] == 400)
    check("推送：JSON 不是对象 -> 400", push(b"[1,2,3]", "Bearer push-token")[0] == 400)
    check("推送：缺 pool -> 400", push_json(content="x")[0] == 400)
    check("推送：缺 content -> 400", push_json(pool="global")[0] == 400)
    check("推送：content 全空白 -> 400", push_json(pool="global", content="   ")[0] == 400)
    big = b'{"pool":"global","content":"' + b"a" * (65536 + 10) + b'"}'
    check("推送：超过 64KB -> 413", push(big, "Bearer push-token")[0] == 413)
    code, body = push_json(pool="nosuchpool", content="x")
    check("推送：未绑定池 -> queued=0 no binding", code == 200 and body.get("reason") == "no binding", str(body))
    code, body = push_json(pool="global", content="x", source="qq")
    check("推送：qq 来源被跳过", code == 200 and body.get("reason") == "skipped source", str(body))
    code, body = push_json(pool="GLOBAL", content="大小写不敏感")
    check("推送：池名大小写不敏感", code == 200 and body.get("queued") == 1, str(body))
    check("推送：正常消息进了攒批缓冲", len(plugin._receive_buffers.get("global", [])) == 2,
          str(plugin._receive_buffers))

    # 攒批窗口：窗口内不投递，到期后整批交给已有的 _deliver_messages
    delivered: list = []

    async def record_deliver(pool, groups, messages):
        delivered.append((pool, list(groups), [(m["id"], m["content"]) for m in messages]))
        return True

    plugin._deliver_messages = record_deliver

    def reset_push_state():
        for timer in list(plugin._receive_timers.values()):
            timer.cancel()
        plugin._receive_timers.clear()
        plugin._receive_buffers.clear()
        delivered.clear()

    async def window_flow():
        for i in (1, 2, 3):
            plugin._handle_push_request(
                json.dumps({"pool": "global", "id": i, "sender": "S", "content": f"m{i}"}).encode("utf-8"),
                "Bearer push-token",
            )
        inside = len(delivered)
        await asyncio.sleep(0.45)   # 等窗口（150ms）到期
        return inside

    reset_push_state()
    inside = asyncio.run(window_flow())
    check("推送：窗口内不投递", inside == 0, str(delivered))
    check(
        "推送：窗口到期后整批交给 _deliver_messages",
        len(delivered) == 1
        and delivered[0][0] == "global"
        and delivered[0][1] == ["123456789"]
        and [content for _, content in delivered[0][2]] == ["m1", "m2", "m3"],
        str(delivered),
    )

    # 达到 receive_max_batch 立即投递
    async def batch_flow():
        plugin.config["receive_max_batch"] = 2
        plugin._handle_push_request(
            json.dumps({"pool": "global", "id": 10, "content": "b1"}).encode("utf-8"), "Bearer push-token")
        plugin._handle_push_request(
            json.dumps({"pool": "global", "id": 11, "content": "b2"}).encode("utf-8"), "Bearer push-token")
        await asyncio.sleep(0.1)

    reset_push_state()
    asyncio.run(batch_flow())
    check(
        "推送：达到 receive_max_batch 立即投递",
        len(delivered) == 1 and [content for _, content in delivered[0][2]] == ["b1", "b2"],
        str(delivered),
    )

    # 投递抛异常：记 warning、不影响下一批
    async def error_flow():
        async def boom(pool, groups, messages):
            raise RuntimeError("模拟投递炸了")

        plugin.config["receive_max_batch"] = 1
        plugin._deliver_messages = boom
        plugin._handle_push_request(
            json.dumps({"pool": "global", "id": 20, "content": "bad"}).encode("utf-8"), "Bearer push-token")
        await asyncio.sleep(0.1)
        plugin._deliver_messages = record_deliver
        plugin._handle_push_request(
            json.dumps({"pool": "global", "id": 21, "content": "good"}).encode("utf-8"), "Bearer push-token")
        await asyncio.sleep(0.1)

    reset_push_state()
    asyncio.run(error_flow())
    check(
        "推送：单批投递抛异常不影响下一批",
        len(delivered) == 1 and delivered[0][2] == [(21, "good")],
        str(delivered),
    )

    # 投递被延迟（平台没连上）也只是记日志
    async def deferred_flow():
        async def deferred(pool, groups, messages):
            return None

        plugin._deliver_messages = deferred
        plugin._handle_push_request(
            json.dumps({"pool": "global", "id": 30, "content": "later"}).encode("utf-8"), "Bearer push-token")
        await asyncio.sleep(0.1)

    reset_push_state()
    asyncio.run(deferred_flow())
    check("推送：平台未就绪时按失败统计且不抛异常", plugin._receive_stats["failed"] >= 1)

    # 没有 aiohttp.web 的离线环境：不启动、不抛错
    plugin._receive_runner = None
    asyncio.run(plugin._start_receive_server())
    check("推送：缺少 aiohttp.web 时不启动也不抛错", plugin._receive_runner is None)

    # 用假的 aiohttp.web 覆盖「启动成功」与「端口被占用」两条路径
    created_runners: list = []

    class FakeRouter:
        def __init__(self):
            self.posts: list = []
            self.gets: list = []

        def add_post(self, path, handler):
            self.posts.append(path)

        def add_get(self, path, handler):
            self.gets.append(path)

    class FakeApplication:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.router = FakeRouter()

    class FakeRunner:
        def __init__(self, app, **kwargs):
            self.app = app
            self.cleaned = 0
            created_runners.append(self)

        async def setup(self):
            return None

        async def cleanup(self):
            self.cleaned += 1

    class FakeSite:
        fail = False

        def __init__(self, runner, bind, port):
            self.runner = runner
            self.bind = bind
            self.port = port

        async def start(self):
            if FakeSite.fail:
                raise OSError("address already in use")

    fake_web = types.ModuleType("fake_web")
    fake_web.Application = FakeApplication
    fake_web.AppRunner = FakeRunner
    fake_web.TCPSite = FakeSite

    module.aiohttp_web = fake_web
    FakeSite.fail = False
    created_runners.clear()
    plugin._receive_runner = None
    asyncio.run(plugin._start_receive_server())
    check("推送：启动成功后记住 runner 与注册路由",
          plugin._receive_runner is created_runners[-1]
          and created_runners[-1].app.router.posts == ["/mc-chatpool/push"]
          and created_runners[-1].app.router.gets == ["/mc-chatpool/push/health"],
          str(created_runners[-1].app.router.posts))

    FakeSite.fail = True
    created_runners.clear()
    plugin._receive_runner = None
    asyncio.run(plugin._start_receive_server())     # 端口被占用：不能抛
    check("推送：端口被占用时不抛异常且不留下 runner", plugin._receive_runner is None)
    check("推送：启动失败时清理已建立的 runner",
          bool(created_runners) and created_runners[-1].cleaned == 1,
          str([r.cleaned for r in created_runners]))

    # 没配 token 时不启动（未鉴权的写接口不能开）
    FakeSite.fail = False
    created_runners.clear()
    plugin.config["receive_token"] = ""
    asyncio.run(plugin._start_receive_server())
    check("推送：receive_token 为空时不启动接收端", plugin._receive_runner is None and not created_runners)
    plugin.config["receive_token"] = "push-token"
    module.aiohttp_web = None

    # 关停：runner 被 cleanup，定时器被取消，攒批被丢弃
    class FakeRunner:
        def __init__(self):
            self.cleaned = 0

        async def cleanup(self):
            self.cleaned += 1

    class FakeTimer:
        def __init__(self):
            self.cancelled = 0

        def cancel(self):
            self.cancelled += 1

    fake_runner = FakeRunner()
    fake_timer = FakeTimer()
    reset_push_state()
    plugin._receive_runner = fake_runner
    plugin._receive_buffers["global"] = [{"id": 99, "content": "pending"}]
    plugin._receive_timers["global"] = fake_timer
    asyncio.run(plugin._stop_receive_server())
    check("推送：关停时 runner.cleanup() 被调用", fake_runner.cleaned == 1, str(fake_runner.cleaned))
    check("推送：关停时窗口定时器被取消", fake_timer.cancelled >= 1, str(fake_timer.cancelled))
    check("推送：关停时 runner 引用被清空", plugin._receive_runner is None)
    check("推送：关停时丢弃未投递的缓冲", not plugin._receive_buffers.get("global"))

    print()
    print(f"全部通过：{len(PASSED)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
