# Changelog

本文件记录每个版本的变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增

- **全量读取历史**（`read_all_history`，默认 `false`）：打开后某个池**首次**同步不再跳过历史，
  而是从服务器保留的最早一条开始按 `history_page_size`（默认 200）分页拉取并转发到群，
  总量受 `history_max_messages`（默认 2000，`0` = 不限制）约束；完成后游标推进到该池最新位置，
  之后自动恢复增量。中途失败不会跳到最新，剩余部分按增量补发，不会丢消息。
- **合并转发**：一轮要发的消息达到 `merge_forward_threshold`（默认 6）时，不再逐条刷屏，
  改为打包成 QQ 的「合并转发」卡片——群聊 `send_group_forward_msg`、
  私聊 `send_private_forward_msg`。卡片内每条 node 的显示名是 MC 发送者名，内容仍是
  `mc_to_qq_format` 的渲染结果，观感与逐条发送一致；超过 `merge_forward_max_nodes`
  （默认 100）自动拆成多张卡片依次发；卡片外显标题由 `merge_forward_title`
  （默认「MC 聊天记录」，即协议里的 `summary`）控制。
- **`#mc all [池] [条数]`**：随时把某个池的历史以合并转发导出到当前会话；
  缺省 = 本群绑定的池 + 全部条数（受 `history_max_messages` 限制）。
- **失败兜底**：合并转发失败（协议端报错、平台未就绪等）自动退化成逐条发送并记 warning，
  不会丢消息。
- **推送接收端（push 模式）**：`enable_receive`（默认 `false`）打开后，插件自带一个小 HTTP 服务
  （`receive_bind` / `receive_port` / `receive_path`，默认 `0.0.0.0:6198/mc-chatpool/push`），
  接收 paper 插件主动 `POST` 过来的单条消息，用来替代「AstrBot 去拉 MC 服务器」——
  适合 MC 服务器搬到别的机器、AstrBot 跨网络拉不到它的场景。
  - 鉴权：`receive_token` **必填**，请求需带 `Authorization: Bearer <token>`（否则 `401`，
    常量时间比较）；token 为空时出于安全考虑**不启动**接收端。
  - 校验：请求体 ≤ 64 KB（超出 `413`）、合法 JSON 对象（否则 `400`）、`pool`/`content` 必填
    （否则 `400`）、池没有绑定群 → `200 {"queued":0,"reason":"no binding"}`、
    来源命中 `receive_skip_sources`（默认 `qq`）→ `200 {"queued":0,"reason":"skipped source"}`。
  - 攒批：同一个池在 `receive_merge_window_ms`（默认 800ms）内收到的消息合并成一批，
    达到 `receive_max_batch`（默认 200）先投递再继续攒；整批交给**现有**的投递链路，
    因此合并转发、失败兜底、渲染完全复用。
  - 投递在后台任务里执行，HTTP 响应不等结果；单批失败只记 warning，不影响后续批次；
    端口被占用等启动失败只记 warning，插件继续按轮询模式工作。
  - 关停时优雅清理（取消定时器与投递任务 → `runner.cleanup()`），不阻塞 AstrBot 退出。

### 变更

- `mc_to_qq_format` 的渲染收敛到单一入口，逐条发送 / 合并转发 node / `#mc history` / `#mc all` 共用，
  保证同一套占位符与观感。
- 默认行为与 1.0.0 完全一致：新配置项的默认值下，小批量消息仍然是逐条发送、首次同步仍然跳过历史。
- 推送模式与轮询模式并存时可能重复投递（同一条消息被推一次、又被拉一次）：README 已写明
  建议二选一，或让 paper 端不要推送 `qq` 来源（默认就跳过）。

### 测试

- 离线自测从 38 项扩到 **98 项**。其中 66 项覆盖合并转发阈值判断、node 构造与 `max_nodes` 拆分、
  卡片标题（`summary`）字段、`#mc all` 参数解析与导出、全量分页累积与上限截断、
  `read_all_history` 首次回放、合并转发失败的逐条兜底路径；
  新增 **32 项**覆盖推送接收端：token 缺失 / 错误 / 缺 `Bearer` 前缀 → `401`，
  坏 JSON、非对象 JSON、缺 `pool`/`content`、`content` 全空白 → `400`，超 64 KB → `413`，
  未绑定池与 `qq` 来源 → `queued=0`，正常入队攒批、池名大小写不敏感、窗口内不投递、
  窗口到期把整批交给 `_deliver_messages`（用桩断言调用参数）、达到 `receive_max_batch` 立即投递、
  单批投递抛异常不影响下一批、平台未就绪时不抛异常、缺少 `aiohttp.web` 时不启动也不抛错、
  启动成功后注册路由并记住 runner、端口被占用时不抛异常且清理已建立的 runner、
  `receive_token` 为空时不启动、关停时 `runner.cleanup()` 被调用且窗口定时器被取消、
  未投递缓冲被丢弃。
- HTTP 层本身（真实 aiohttp 服务、状态码、攒批与时序、关停后端口不可达）另外在容器里
  用真 aiohttp + 真 astrbot 跑了一遍，未依赖离线桩件。

## [1.0.0] - 2026-10-12

首个版本，在 AstrBot 4.28.2 + NapCat（Docker 部署）+ Paper 26.2 上实测通过。

### 新增

- **MC → QQ**：按 `poll_interval_seconds` 定时轮询服务端聊天池，把新消息按下发的格式
  转发到该池绑定的所有 QQ 群；积压时按 `has_more` 连续追平。
- **QQ → MC**：群内命令 `#mc <文本>`、`#mc send <池> <文本>`、`#mc status`、`#mc pools`、
  `#mc history [池] [条数]`、`#mc bind/unbind <池>`（管理员）、`#mc help`。
  处理完调用 `event.stop_event()`，不会触发 LLM 重复回复。
- **游标与持久化**：`state.json` 保存每个池的拉取游标与命令建立的群↔池绑定；
  首次运行只记游标、不回放历史（`skip_backlog_on_first_poll`）。
- **可靠投递**：某条消息没送达时**不推进游标**，下一轮重试（上限 10 次）；
  平台尚未连接（`ApiNotAvailable` 等）视为暂时不可用，挂起等待、不消耗重试次数，连上后自动补发。
- **平台 ID 自动探测**（`platform_id: auto`）：AstrBot 里平台实例的 ID 由用户自定义
  （常是 `default` 而不是 `aiocqhttp`），自动探测避免 `cannot find platform for session`。
- **防回环**：拉取时默认带 `exclude_source=qq`，注入到服务器的消息不会被再次拉回群里。
- **离线自测** `tests/test_offline.py`：桩掉 `astrbot.*` 与 `aiohttp`，38 项断言，
  不需要 AstrBot 运行时即可跑。
- **CI**：GitHub Actions 在 Python 3.10 / 3.11 / 3.12 上跑语法检查与离线测试。

[1.0.0]: https://github.com/ruosha/astrbot_plugin_mc_chatpool/releases/tag/v1.0.0
