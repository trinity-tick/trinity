# Trinity MCP 对接指南(2026-08-16)

## 一、现状(已实测验证)

- MCP Server v1.1.0,支持 3 种传输:stdio / sse / streamable-http
- 标准协议:protocolVersion 2025-03-26(initialize 验证通过)
- **8 个工具**(tools/list 实测):
  memory_search / memory_write / memory_update / memory_delete /
  audit_query / trinity_diagnostics / memory_chronicle / memory_tag_search

## 二、传输方式

| 方式 | 命令/地址 | 适用 |
|---|---|---|
| stdio | python -m trinity.mcp.server --mode stdio | 本地客户端(推荐) |
| SSE | http://127.0.0.1:8000/sse(常驻) | HTTP 客户端 |
| streamable-http | 按需:python -m trinity.mcp.server --mode streamable-http --port 8003(挂载 /mcp) | 现代 MCP 客户端(2025-03+) |

## 三、WorkBuddy 接入(已完成配置)

配置文件:C:\Users\Administrator\.workbuddy\mcp.json(已写,备份 .bak)
- trinity(stdio):本地 spawn python,自动拉起
- trinity-sse(SSE):连常驻 :8000

生效:重启 WorkBuddy 后自动加载;对话中应可使用 memory_search 等 MCP 工具。

## 四、其他客户端配置示例

### Claude Desktop
claude_desktop_config.json:
{ "mcpServers": {
  "trinity": { "command": "C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python314\\python.exe",
               "args": ["-m", "trinity.mcp.server", "--mode", "stdio"],
               "cwd": "C:\\Users\\Administrator\\trinity",
               "env": { "PYTHONUTF8": "1", "PYTHONPATH": "C:\\Users\\Administrator\\trinity" } } } }

### Cursor / 其他 OpenAI 兼容 MCP 客户端
- stdio:同上 command
- SSE:url = http://127.0.0.1:8000/sse

### streamable-http(如客户端支持)
1. 启动:python -m trinity.mcp.server --mode streamable-http --port 8003
2. 客户端 url = http://127.0.0.1:8003/mcp

## 五、关键环境变量(客户端 spawn 时)

- PYTHONUTF8=1(Windows 中文编码,必需)
- PYTHONPATH=C:\Users\Administrator\trinity(定位 trinity 包)
- TRINITY_QUIET_IMPORT=1(静默导入)

## 六、故障排查

1. 工具不出现:检查 MCP 服务器是否 spawn 成功(手动跑 command 看 stderr)
2. 中文乱码:确认 PYTHONUTF8=1
3. SSE 连不上:确认 :8000 监听(Get-NetTCPConnection -LocalPort 8000)
4. 锁冲突:stdio 实例过多会竞争写锁,必要时看门狗自动清理

## 七、注意

- DSH 会话内已用 trinity-native(直连,不走 MCP),MCP 主要服务外部客户端(WorkBuddy/Claude/Cursor)
- 每个 MCP 客户端拉起一个 stdio 进程,多个同时写会竞争锁(看门狗兜底)
