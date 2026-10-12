# 开发笔记

这个文件面向改这个插件的开发者，普通安装使用只看 [README.md](README.md) 就够了。

## 目录约定

* 本仓库就是**插件本体**：把它 clone（或复制）到 `<AstrBot>/data/plugins/astrbot_plugin_mc_chatpool/`，
  AstrBot 就会加载它，不需要额外的"部署"步骤。
* 作者本机上源码放在工作区、AstrBot 跑在 Docker 里，所以另有一个 `deploy.bat`
  用 robocopy 把源码镜像到 `data\plugins\`。它写死了本机路径，**已在 `.gitignore` 里，不会提交**。
  你如果也这么用，照它的样子自己写一个即可：

  ```bat
  set "DST=<你的 AstrBot 根目录>\data\plugins\astrbot_plugin_mc_chatpool"
  robocopy "%~dp0." "%DST%" /MIR /XD __pycache__ /XF deploy.bat
  ```

  改完源码后在 AstrBot 的 WebUI 里重载插件（或重启容器）即可生效。

## 本地自测

不需要 AstrBot 运行时，用标准 Python 就能跑：

```bash
python -m py_compile main.py tests/test_offline.py
python tests/test_offline.py
```

`tests/test_offline.py` 会在导入插件前把 `astrbot.*` 与 `aiohttp` 桩掉，覆盖桥接配置解析、
占位符渲染、群命令分发与权限、消息截断、轮询游标、`has_more` 连拉、平台 ID 自动探测、
投递失败不推进游标与重试上限等 38 项断言。

## 真机联调

改完投递逻辑后建议至少验证这三条路径（都需要一台跑着服务端插件的 Paper 服务器）：

1. **MC → QQ**：在游戏里 `/chatpool say 测试`，或在服务器接口 `POST /api/v1/messages`
   造一条 `source=console` 的消息，看群里是否收到、插件日志里是否有 `已转发到 …`。
2. **QQ → MC**：在群里发 `#mc 你好`，看游戏内在线玩家是否收到、服务端日志是否出现
   `[global/qq] …`，以及群里是否有"已发送到聊天池"的回执。
3. **重启空窗**：重启 AstrBot 后立刻触发一条 MC → QQ，日志里应先出现
   `平台还没准备好（ApiNotAvailable），稍后自动补发`，等 NapCat 反向 WS 重连（约 30 秒）后
   出现 `已转发到 …`，游标在此期间不得前进。

## 提交前检查

* `main.py`、`tests/`、`metadata.yaml`、`_conf_schema.json` 的改动都要同步（配置项说明写在 schema 的 hint 里）。
* **不要提交** `state.json`、`data/`、`__pycache__/`（`.gitignore` 已覆盖）。
* `.bat` 文件保持**纯 ASCII**：cmd.exe 用 OEM 代码页解析批处理，UTF-8 中文会把脚本解析坏。
* 改了用户可见行为就顺手更新 `README.md` 与 `CHANGELOG.md`。
