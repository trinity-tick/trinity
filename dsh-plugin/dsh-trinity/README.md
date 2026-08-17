# dsh-trinity — DSH 原生 Trinity 融合插件（源码）

本目录是 `@deepseek-ai/dsh-trinity` 插件的**源码**（版本管理用）。
安装位置：`C:\Users\Administrator\.dsh\profiles\web\node_modules\@deepseek-ai\dsh-trinity\`
（由 `dsh-ops\install-trinity-plugin.ps1` 同步；勿直接编辑 node_modules 副本）。

## 插件做什么（融合改造 F2/F4）

取代 mcp-trinity（MCP 协议层）：
- apply 时 spawn `trinity/engine_worker.py`（stdio NDJSON 直连引擎，无 MCP 中间层）；
- 注册 10 个原生工具：`trinity_ping / trinity_search / trinity_write /
  trinity_update / trinity_delete / trinity_audit / trinity_diagnostics /
  trinity_chronicle / trinity_tag_search / trinity_identity_register`；
- worker 崩溃指数退避自动重启；stderr 转 pipe 防污染；
- F4：write/search 自动注入 `agent_id=dsh-<sessionId>` / `session_id`（DSH 会话
  自动成为 Trinity 身份，多会话隔离），首次调用自动 identity_register。

## 启用

1. `powershell -File dsh-ops\install-trinity-plugin.ps1`（同步源码到 web profile node_modules）
2. `web/cordis.patch.yml` 已有 `trinity-native` insert（与 mcp-trinity 并存；
   F5 阶段移除 mcp-trinity 使内部完全原生化）
3. 新开 DSH 会话即可使用 `trinity_*` 工具（HMR 或重启 web profile 生效）

## 回滚

- 移除 `web/cordis.patch.yml` 的 trinity-native insert
- 删除 `C:\Users\Administrator\.dsh\profiles\web\node_modules\@deepseek-ai\dsh-trinity`
