# Trinity 快速入门

## 安装

```bash
pip install trinity-memory
```

## 使用

```python
from trinity import Trinity

# 初始化
mem = Trinity()

# 写入记忆
mem.ingest("用户偏好深色主题")

# 搜索
results = mem.search("用户偏好")
for r in results:
    print(f"[{r['score']:.3f}] {r['content_preview']}")

# 系统诊断
print(mem.diagnostics())
```

## CLI

```bash
# 搜索
python -m trinity search --query "Alice" --top-k 5

# 诊断
python -m trinity diagnostics

# 基准测试
python -m trinity bench --name mock

# MCP 服务
python -m trinity mcp --mode sse --port 8000
```

## Docker

```bash
docker compose up -d
```

## 更多

- 文档: [docs/](docs/)
- 示例: [examples/](examples/)
- 贡献指南: [CONTRIBUTING.md](CONTRIBUTING.md)
