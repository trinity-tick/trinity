# Trinity MCP 生态接入指南（2026-08-24）

> 让任意 MCP 客户端（Claude Code / Cursor / Codex / Dify / 任意支持
> MCP 的 agent）5 分钟接入 Trinity 记忆层。三形态传输 + 鉴权说明 +
> 生态市场上架清单。

---

## 一、快速接入（三形态 mcpServers 配置）

### 1. 本地 stdio（零鉴权，单机最快）

```json
{
  "mcpServers": {
    "trinity-memory": {
      "command": "trinity-mcp",
      "args": ["--mode", "stdio"]
    }
  }
}
```

适用：Claude Desktop / Claude Code / Cursor 本地配置。
工具：`memory_search` / `memory_write` / `memory_update` / `memory_delete` /
`audit_query` / `memory_tag_search` / `trinity_diagnostics` / `memory_chronicle`。

### 2. SSE（局域网/远程，:8000）

```json
{
  "mcpServers": {
    "trinity-memory": {
      "url": "http://127.0.0.1:8000/sse"
    }
  }
}
```

### 3. Streamable HTTP（推荐远程，:8003，Bearer 鉴权）

```json
{
  "mcpServers": {
    "trinity-memory": {
      "url": "http://127.0.0.1:8003/mcp",
      "headers": { "Authorization": "Bearer <TRINITY_MCP_API_KEY 或 GATEWAY_API_KEY>" }
    }
  }
}
```

- 鉴权默认开启（`TRINITY_MCP_HTTP_AUTH` 默认 on）；key 优先级
  `TRINITY_MCP_API_KEY` → `TRINITY_API_KEY` → `GATEWAY_API_KEY`；
- 无 key 时服务自动降级无鉴权并打 WARN（仅限本机场景）；
- OAuth 感知客户端可读 `/.well-known/oauth-protected-resource` 发现
  `authorization_servers` / `scopes_supported`（memory.read/write）。

---

## 二、生态市场上架清单（Smithery / mcp.so）

### 前置验证（上架前必跑）

```powershell
# 1. 服务健康
powershell -File dsh-ops/trinity-supervisor.ps1   # api/mcp/mcp-http 全部拉起

# 2. MCP 端到端验证（initialize + 工具列表）
python scripts/verify_mcp_server.py --transport streamable-http --port 8003 --key <KEY>
python scripts/verify_mcp_server.py --transport stdio
python scripts/verify_mcp_server.py --transport sse --port 8000
```

### Smithery 上架信息

| 项 | 值 |
|---|---|
| 名称 | trinity-memory |
| 描述 | Cross-session long-term memory OS for AI agents (47-channel framework, CRDT+audit, hybrid retrieval) |
| 形态 | stdio（Docker 包） |
| 标签 | memory, rag, agent, knowledge-graph, sqlite |
| 仓库 | https://github.com/trinity-tick/trinity |
| 许可 | MIT |

### mcp.so 上架信息

| 项 | 值 |
|---|---|
| 名称 | trinity-memory |
| 类型 | Memory & Knowledge |
| 入口 | `trinity-mcp --mode stdio` 或远程 `https://<host>:8003/mcp` |
| 环境变量 | TRINITY_MCP_API_KEY / TRINITY_STORE / TRINITY_API_KEY |

---

## 三、推荐的使用模式（写给接入的 agent）

1. **检索优先**：回答"是否记得/之前做过/用户偏好"类问题先
   `memory_search`（hybrid 模式），再回答；
2. **写入有纪律**：值得记住的才写（偏好/事实/决策/坑），自包含结构化
   文本（含路径/工具名/数字），importance 0.4-0.6 常规、0.7+ 决策；
3. **更新而非重复**：已有记忆用 `memory_update`（CRDT 版本链保留历史）；
4. **身份隔离**：每个会话自动注册独立 agent 身份（agent_id=dsh-<sid>），
   未显式指定时检索按会话隔离、空结果自动回退全局；
5. **审计溯源**：关键事实用 `audit_query` 核对版本链与来源。

---

## 四、验证脚本说明

`scripts/verify_mcp_server.py`（本目录配套）：
- `--transport stdio|sse|streamable-http` 选择传输；
- stdio 模式：spawn `trinity-mcp --mode stdio`，走 MCP 协议 initialize +
  tools/list + 一次 memory_search 冒烟；
- sse/streamable-http：HTTP 探测 `.well-known`（streamable-http）+
  initialize 握手 + 鉴权 401/200 检查；
- 退出码 0 = 可上架。
