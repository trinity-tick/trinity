# Trinity MCP 状态与 MCP v2 差距（2026-08-15）

## 一、当前 MCP 服务

- **Transport**：`stdio` / `SSE`(:8000) / **`streamable-http`(:8003, MCP v2, 2026-08-15 已实现)**（DSH 会话内，dsh-mcp-client 拉起）与 `SSE`（:8000，独立进程，
  `python -m trinity.mcp.server --mode sse --port 8000`）。
- **实现**：FastMCP（mcp.server.fastmcp）——原生 MCP 协议。

## 二、工具覆盖（8 个记忆工具 + 资源/提示）

| 工具 | 说明 |
|---|---|
| `memory_search` | 三模检索（semantic/graph/exact/hybrid-RRF） |
| `memory_write` | 写入（CRDT 版本化 + SHA-256 审计） |
| `memory_update` / `memory_delete` | 更新/软删 |
| `audit_query` | 版本链/审计查询 |
| `memory_chronicle` | 事件序列记录 |
| `memory_tag_search` | 按标签检索 |
| `trinity_diagnostics` | 引擎诊断 |
| 资源 / 提示 | memory_resources / memory_prompts 已注册 |

另有原生（非 MCP）trinity_* 工具集（dsh-trinity 插件直连 engine_worker，含结构层
trajectory/sessions/goals/schedules）与 MCP 并存（F5 计划移除 MCP 冗余）。

## 三、MCP v2（streamable HTTP）差距

| 项 | 现状 | MCP v2 目标 |
|---|---|---|
| HTTP transport | SSE（传统）+ **streamable-http（:8003 /mcp，已实现 2026-08-15）** | Streamable HTTP（单端点 /mcp，会话复用） |
| 认证 | 无（依赖上层） | 支持 Authorization/OAuth |
| 客户端兼容 | MCP v1 客户端 | v2 客户端 |
| 会话 | 每次连接独立 | HTTP 会话 + 复用 |

**现状**：已实现（FastMCP 原生支持 streamable-http）；官方 mcp 客户端已验证连接 + 工具调用。
适配路径：FastMCP 升级支持 streamable_http transport（若底层库支持）或
自建 /mcp 单端点桥接（转发到现有 SSE/stdio 逻辑）。

## 四、建议

1. **短期**：~~升级 FastMCP~~ ✅ 已实现（round45）；可扩展：
   `transport="streamable_http"` 分支——工作量小、可验证（v2 客户端连通）。
2. **中期**：MCP 工具集补 goal/schedule 操作（与结构层对齐）；F5 移除 MCP 冗余后
   保留原生通道为主。
