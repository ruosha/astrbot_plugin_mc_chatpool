# astrbot_plugin_mc_chatpool — MC 聊天池桥接

把 **Minecraft(Paper) 服务器的聊天池**与 **QQ 群**接起来：

* **MC → QQ**：按固定间隔轮询服务端插件的 HTTP 接口，把游戏内新消息转发到绑定的群；
* **QQ → MC**：群内 `#mc <文本>` 命令把消息送回服务器，由服务器广播给该池在线的玩家。

服务端插件（Paper 26.2）是另一个仓库：**McQqChatPool**，它提供聊天池与 HTTP 接口。
两者通过本机 HTTP 通信，接口协议见该仓库的 README。

```
   游戏内玩家 ──/chatpool send <池> <文本>──┐
                                          ▼
                                 ┌──────────────────┐
                                 │   聊天池 ChatPool   │
                                 └────────┬─────────┘
            本插件每 N 秒 GET             │        ▲   POST /api/v1/messages
            /api/v1/messages?since=…      │        │   （群内 #mc 命令）
                                          ▼        │
                                   NapCat ──► QQ 群 ──► 群成员
```

---

## 1. 安装

需要 AstrBot **4.16+**（在 4.28.2 上实测）。依赖只有 `aiohttp`，AstrBot 自带。

**方式一：git clone（推荐）**

```bash
cd <AstrBot>/data/plugins
git clone https://github.com/<你的用户名>/astrbot_plugin_mc_chatpool.git
```

然后在 WebUI 的「插件管理」里重载，或重启 AstrBot。

**方式二：手动复制**

把本仓库的所有文件复制到 `<AstrBot>/data/plugins/astrbot_plugin_mc_chatpool/`。

> Docker 部署（官方 compose）时，`<AstrBot>/data` 就是宿主机上挂载进去的那个 `data` 目录，
> 例如 `./data/plugins/astrbot_plugin_mc_chatpool/`。

---

## 2. 配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enable_poll` | `true` | 「MC → QQ」定时拉取总开关 |
| `mc_base_url` | `http://127.0.0.1:8123` | 服务端插件的 HTTP 接口地址 |
| `mc_token` | 空 | **必须**与服务端 `plugins/McQqChatPool/config.yml` 的 `http.token` 一致 |
| `poll_interval_seconds` | `5` | 拉取间隔（秒） |
| `poll_batch_limit` | `50` | 单次最多拉多少条；积压时自动连拉多轮 |
| `poll_exclude_sources` | `qq` | 不转发到群的来源，**别删**，否则群消息会被拉回来刷屏 |
| `skip_backlog_on_first_poll` | `true` | 首次运行只记游标，不回放服务器里的历史消息 |
| `send_interval_ms` | `300` | 消息之间的发送间隔，防 QQ 风控 |
| `platform_id` | `auto` | 平台适配器 ID。**建议保持 auto 自动探测**：AstrBot 里平台实例的 ID 是用户自己起的名字，写死成 `aiocqhttp` 会报 `cannot find platform for session` 而发不出消息 |
| `bridges` | 示例值 | **必填**：`聊天池 = 群号1, 群号2`，每行一条 |
| `enable_group_to_mc` | `true` | 允许群内命令往服务器发消息 |
| `command_prefix` | `#mc` | 命令前缀（`/mc` 也认） |
| `group_whitelist` | `[]` | 留空 = 所有群可用 |
| `admin_qq_ids` | `[]` | 能用 `#mc bind/unbind` 的 QQ 号（群管理员身份也自动认可） |
| `default_pool` | `global` | 群没绑定任何池时，`#mc <文本>` 发到这里 |
| `max_message_length` | `300` | 单条消息长度上限，超出截断 |
| `mc_to_qq_format` | `[MC] {sender}: {content}` | 转发到群里的格式，可用 `{pool} {sender} {sender_id} {content} {id} {source} {time}` |
| `qq_sender_format` | `{nickname}` | QQ 成员名在游戏内的显示方式，可用 `{nickname} {qq} {group_id} {pool}` |
| `reply_to_group` | `true` | 是否回复命令结果 |
| `http_timeout_seconds` | `8` | HTTP 超时 |

`bridges` 的写法：

```yaml
# 每行一条：聊天池名 = 群号（多个群用逗号分隔）
global = 123456789, 987654321
tech = 555555
```

也可以直接粘 JSON：`[{"pool":"global","groups":["123456789"]}]`。
池名必须与服务端 `config.yml` 里 `pools` 段的键一致（小写字母/数字/下划线/短横线）。

---

## 3. 群内命令

| 命令 | 作用 |
|---|---|
| `#mc <文本>` | 发到本群绑定的聊天池 |
| `#mc send <池> <文本>` | 发到指定聊天池；只给池名不给文本会提示用法 |
| `#mc status` | 服务器在线人数、聊天池概况、接口地址 |
| `#mc pools` | 列出所有聊天池与绑定情况 |
| `#mc history [池] [条数]` | 查看最近的游戏内消息（默认 5 条，最多 20 条） |
| `#mc bind <池>` | 把本群绑定到某个池（管理员） |
| `#mc unbind [池]` | 解除绑定（管理员） |
| `#mc help` | 帮助（单独发 `#mc`、`#mc 帮助`、`#mc ?` 也一样） |

前缀后面必须跟空格或直接结束才算命令，所以 `#mc你好` 不会触发（避免误伤普通聊天）。

命令处理完会调用 `event.stop_event()`，**不会**再触发 LLM 回复，所以不会出现「群里发一句、机器人也接一句」的重复。

---

## 4. 对接步骤

1. **服务端**：安装 McQqChatPool 插件并设置 `http.token`（见该仓库 README）。
2. **本插件**：填 `mc_base_url`、`mc_token`（与服务端一致）、`bridges`，重载插件。
3. **验证**：群里发 `#mc status`
   * 看到「🟢 Minecraft 服务器在线」说明链路通了；
   * 看到 `❌ 连不上…` 就按第 5 节排查。

### AstrBot 跑在 Docker 里

容器里的 `127.0.0.1` 是容器自己，所以 `mc_base_url` 要指向宿主机：

* **Docker Desktop for Windows**：填 `http://host.docker.internal:8123` 即可。
  实测（2026-10）**不需要**把服务端插件改成监听 `0.0.0.0`——Docker Desktop 的网关
  能直接访问宿主机回环端口上监听的 8123。
* **Linux 上的 Docker / 自定义 bridge 网络**：通常访问不到宿主机回环端口，
  这时把服务端插件的 `http.host` 改成宿主机内网 IP（或 `0.0.0.0` + 强 token）。

---

## 5. 排查表

| 现象 | 检查 |
|---|---|
| `#mc status` 报连不上 | 服务端起了吗；`http.enabled`；端口/地址；Docker 场景见第 4 节 |
| 报 401 | `mc_token` 与服务端 `http.token` 不一致（改完要重载插件/重启服务器） |
| 日志出现 `cannot find platform for session`、`ApiNotAvailable` | `platform_id` 没写对：保持 `auto` 让插件自动探测（AstrBot 的实例 ID 常是 `default`） |
| 重启 AstrBot 后前几十秒发不出消息 | 正常现象：NapCat 的反向 WS 要等约 30 秒重连。插件会把消息挂起并在连上后自动补发，不会丢 |
| 群里收不到游戏内消息 | `bridges` 里池名写对了吗；服务端 `/chatpool list` 看池里有没有消息；日志里有没有 `401` |
| 游戏内看不到群消息 | 回执里的「游戏内在线可见人数」是不是 0——玩家需要 `/chatpool join <池>` 或让该池 `default-join: true` |
| 群里消息被重复转发 | `poll_exclude_sources` 被改掉了，恢复成 `qq` |
| 插件一启动就刷屏历史消息 | `skip_backlog_on_first_poll` 设为 `true`，或删除 `data/plugin_data/astrbot_plugin_mc_chatpool/state.json` 后重启 |
| 极短时间内反复重启后偶发重复消息 | Docker Desktop 的绑定挂载可能让新进程读到略旧的 `state.json`（游标回退一点），于是重发少量消息。正常使用不受影响；设计上宁可重复也不丢 |

数据文件：`data/plugin_data/astrbot_plugin_mc_chatpool/state.json`（拉取游标 + `#mc bind` 建立的绑定）。

---

## 6. 工作原理

* **轮询与游标**：服务端给每条消息分配全局自增 ID。本插件每轮用 `since=<上次游标>` 增量拉取，
  游标由插件自己按「已确认送达的最大消息 ID」推进并持久化到 `state.json`；
  一轮最多连拉 5 次以追平积压。服务端返回的 `cursor` 只用于首次同步定位最新位置。
* **防回环**：拉取时带 `exclude_source=qq`，因此从群里注入到服务器的消息不会再被拉回群里。
* **失败重试**：某条消息没送达时**不推进游标**，下一轮重试（最多 `MAX_DELIVERY_RETRIES = 10` 次），
  避免丢消息；连续失败到上限就放弃这一条并继续推进游标（宁可丢一条也不卡死）。
  平台尚未连上（`ApiNotAvailable` 等）属于暂时不可用，挂起等待、**不消耗**重试次数，连上后自动补发。
* **首次同步**：默认只记录游标、不回放历史，避免插件一启动就把服务器里攒了半天的消息全刷进群。
* **一键安装友好**：命令处理走 `event_message_type(GROUP_MESSAGE)` 过滤器而不是 `@filter.command`，
  不依赖 AstrBot 的唤醒前缀配置。

---

## 7. 离线自测

不需要 AstrBot 运行时，用标准 Python 就能跑（脚本会把 `astrbot.*` 与 `aiohttp` 桩掉）：

```bash
python tests/test_offline.py
```

覆盖 38 项断言：桥接配置解析（行格式 / JSON）、占位符渲染与防二次替换、群命令分发与权限、
消息截断、轮询游标与 `has_more` 连拉、平台 ID 自动探测、投递失败不推进游标与重试上限等。

---

## 8. 发布到 GitHub 之后

`metadata.yaml` 里的 `repo` 字段可以填上仓库地址，AstrBot 的插件管理界面会用它检查更新：

```yaml
repo: "https://github.com/<你的用户名>/astrbot_plugin_mc_chatpool"
```

---

## License

[MIT](LICENSE)
