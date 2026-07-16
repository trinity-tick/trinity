# Trinity Memory — 三位一体长程记忆系统

[![PyPI version](https://img.shields.io/pypi/v/trinity-memory)](https://pypi.org/project/trinity-memory/)
[![CI](https://github.com/trinity-tick/trinity/actions/workflows/ci.yml/badge.svg)](https://github.com/trinity-tick/trinity/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-1.0-orange)](https://modelcontextprotocol.io)

---

## 快速开始

```bash
# 安装
pip install trinity-memory

# Python API
from trinity import Trinity
mem = Trinity()
mem.ingest("用户偏好暗色模式", tags=["preference", "ui"])
results = mem.search("用户偏好")
print(results)

# 启动 API 服务
trinity-api --port 8100

# 启动 MCP Server
trinity-mcp --mode stdio
```

## 架构

Trinity 是三位一体记忆架构，整合了 12+ 业界最优方案：

| 层级 | 组件 | 对齐方案 |
|:-----|:-----|:---------|
| **检索层** | CB53 BEAM-LIGHT | ICLR 2026 BEAM 基准 |
| | CB54 Exabase 三阶段检索 | LongMemEval 96.4% SOTA |
| | CB55 Hindsight 四网络 | BEAM 10M SOTA 64.1% |
| | CB56 Zikkaron Hopfield | 非LLM SOTA 40.4% |
| **记忆层** | CB45-CB48 级联提取 | ByteRover / Mem0 / Graphiti |
| | CB49-CB52 关系管理 | Supermemory / Mastra / MemMachine |
| | CB57 自优化 | SelfMem July 2026 |
| **保护层** | 50 级 Guardian 链 | 遗忘防护 / 压缩审计 |
| **检索通道** | 47 路融合检索 | 语义/图谱/精确/混合 |

## 快速集成

### 作为 MCP Server

Trinity 提供标准 MCP 接口，可被任何 MCP 客户端调用：

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

8 个工具可用：`memory_search`, `memory_write`, `memory_update`, `memory_delete`, `audit_query`, `trinity_diagnostics`, `memory_chronicle`, `memory_tag_search`

### 作为 REST API

```bash
# 写入记忆
curl -X POST http://localhost:8100/memories \
  -H "Content-Type: application/json" \
  -d '{"content":"用户信息","importance":0.8}'

# 搜索记忆
curl "http://localhost:8100/search?q=用户&top_k=5"
```

## 部署

### Docker

```bash
docker build -t trinity-memory .
docker run -d -p 8100:8100 -p 8000:8000 -v /data:/data trinity-memory
```

### 一键启动

```bash
# Windows
start_trinity.bat

# Linux/Mac
chmod +x docker-entrypoint.sh
./docker-entrypoint.sh
```

## 商业化

| 产品 | 定价 | 适用场景 |
|:-----|:----:|:---------|
| **MCP Server** | 免费开源 | AI Agent 集成 |
| **SaaS API** | 按量付费 | 应用开发 |
| **企业私有部署** | 许可证 | 合规需求 |

## 许可证

MIT License — 可自由使用于商业和非商业项目。
