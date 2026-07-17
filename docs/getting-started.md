# Getting Started

## Installation

```bash
pip install trinity-memory
```

Or with dev dependencies:

```bash
pip install "trinity-memory[dev]"
```

## Quick Start

### Python API

```python
from trinity import Trinity

# 初始化
mem = Trinity()

# 写入记忆
mem.ingest("用户偏好暗色模式", tags=["preference", "ui"])
mem.ingest("用户使用 Python 3.12", tags=["environment", "python"])

# 搜索记忆
results = mem.search("用户偏好")
for r in results:
    print(f"[{r['score']:.2f}] {r['content']}")

# 诊断
print(mem.diagnostics())
```

### CLI

```bash
# 搜索
python -m trinity search --query "用户偏好" --top-k 5

# 写入
python -m trinity ingest --content "自定义记忆内容" --tags tag1,tag2

# 诊断
python -m trinity diagnostics

# 性能基准
python -m trinity bench --name mock
```

### MCP Server

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

### REST API

```bash
# 启动 API 服务
trinity-api --port 8100

# 写入
curl -X POST http://localhost:8100/memories \
  -H "Content-Type: application/json" \
  -d '{"content":"用户信息","importance":0.8}'

# 搜索
curl "http://localhost:8100/search?q=用户&top_k=5"
```

## Docker

```bash
docker compose up -d
```
