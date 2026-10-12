# Changelog

本文件记录每个版本的变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
