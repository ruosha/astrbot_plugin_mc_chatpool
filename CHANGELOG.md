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

### 变更

- `mc_to_qq_format` 的渲染收敛到单一入口，逐条发送 / 合并转发 node / `#mc history` / `#mc all` 共用，
  保证同一套占位符与观感。
- 默认行为与 1.0.0 完全一致：新配置项的默认值下，小批量消息仍然是逐条发送、首次同步仍然跳过历史。

### 测试

- 离线自测从 38 项扩到 66 项，新增覆盖：合并转发阈值判断、node 构造与 `max_nodes` 拆分、
  卡片标题（`summary`）字段、`#mc all` 参数解析与导出、全量分页累积与上限截断、
  `read_all_history` 首次回放、合并转发失败的逐条兜底路径。

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
