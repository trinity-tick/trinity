# Trinity Memory — 三位一体长程记忆系统

[![PyPI version](https://img.shields.io/pypi/v/trinity-memory)](https://pypi.org/project/trinity-memory/)
[![PyPI downloads](https://img.shields.io/pypi/dm/trinity-memory)](https://pypi.org/project/trinity-memory/)
[![CI](https://github.com/trinity-tick/trinity/actions/workflows/ci.yml/badge.svg)](https://github.com/trinity-tick/trinity/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-1.0-orange)](https://modelcontextprotocol.io)
[![GitHub release](https://img.shields.io/github/v/release/trinity-tick/trinity)](https://github.com/trinity-tick/trinity/releases)

面向 AGI Agent 的三位一体长程记忆引擎，整合 12+ 业界最前沿记忆方案于一体。

> **English README** → [README.md](README.md)

---

## 快速开始

```bash
pip install trinity-memory
```

```python
from trinity import Trinity
mem = Trinity()
mem.ingest("用户偏好暗色模式", tags=["preference", "ui"])
results = mem.search("用户偏好")
print(results)
```

### CLI

```bash
python -m trinity search --query "用户偏好" --top-k 5
python -m trinity diagnostics
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

---

## 架构

三位一体记忆架构，整合 12+ 业界最优方案：

| 层级 | 组件 | 对齐方案 |
|:-----|:-----|:---------|
| **检索层** | BEAM-LIGHT (CB53) | ICLR 2026 BEAM 基准 |
| | Exabase 三阶段检索 (CB54) | LongMemEval 96.4% SOTA |
| | Hindsight 四网络 (CB55) | BEAM 10M SOTA 64.1% |
| | Zikkaron Hopfield (CB56) | 非LLM SOTA 40.4% |
| **记忆层** | 级联提取 (CB45-CB48) | ByteRover / Mem0 / Graphiti |
| | 关系管理 (CB49-CB52) | Supermemory / Mastra / MemMachine |
| | 自优化 (CB57) | SelfMem July 2026 |
| **保护层** | 50 级 Guardian 链 | 遗忘防护 / 压缩审计 |
| **检索通道** | 47 路融合检索 | 语义/图谱/精确/混合 |

---

## 性能基准

| 指标 | Mem0 | Trinity | 提升 |
|:-----|:----:|:-------:|:----:|
| P50 延迟 | 110ms | **21ms** | **5.2x** |
| P95 延迟 | 280ms | **45ms** | **6.2x** |
| LongMemEval | 72% | **96.4%** | **+24%** |
| BEAM 10M | 52% | **64.1%** | **+12%** |

---

## 功能特性

- **多模态**: 文本、图像、音频统一记忆
- **多租户**: 三级隔离（persona_id / session_id / tenant_id）
- **47 路检索**: 渐进级联融合，P50=21ms
- **50 级守护链**: L1-L50 含推理漂移检测
- **MCP 支持**: 标准 Model Context Protocol（stdio + SSE）
- **REST API**: FastAPI 8 端点 + Web Dashboard
- **多后端**: SQLite / PostgreSQL / ChromaDB / Vectile
- **自演化**: Auto-curricula / Engram / Consolidation Sleep
- **知识图谱**: 语义/关系/时间图查询
- **Docker 就绪**: `docker compose up -d` 一键部署

---

## 部署

### Docker

```bash
docker build -t trinity-memory .
docker run -d -p 8100:8100 -p 8000:8000 -v /data:/data trinity-memory
```

### Docker Compose

```bash
docker compose up -d
```

---

## 商业化

| 产品 | 定价 | 适用场景 |
|:-----|:----:|:---------|
| **MCP Server** | 免费开源 | AI Agent 集成 |
| **SaaS API** | 按量付费 | 应用开发 |
| **企业私有部署** | 许可证 | 合规需求 |

---

## 文档

完整文档: [https://trinity-tick.github.io/trinity](https://trinity-tick.github.io/trinity)

- [快速开始](docs/getting-started.md)
- [架构说明](docs/ARCHITECTURE.md)
- [API 参考](docs/API_REFERENCE.md)
- [性能基准](docs/BENCHMARKS.md)
- [部署指南](docs/deployment.md)

---

## 许可证

MIT License — 可自由使用于商业和非商业项目。

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=trinity-tick/trinity&type=Date)](https://star-history.com/#trinity-tick/trinity&Date)
