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

| 层级 | 组件 | 对齐方案（模块为储备；运行路径见 README.md Key Features） |
|:-----|:-----|:---------|
| **检索层** | BEAM-LIGHT (CB53) | ICLR 2026 BEAM 基准（对齐） |
| | Exabase 三阶段检索 (CB54) | Exabase 论文（对齐；96.4% 系 Exabase M-1 官方宣称，非 Trinity 成绩） |
| | Hindsight 四网络 (CB55) | Hindsight 论文（对齐；64.1% 系 Hindsight 在 BEAM 10M 的成绩，非 Trinity 成绩） |
| | Zikkaron Hopfield (CB56) | Zikkaron 项目（对齐） |
| **记忆层** | 级联提取 (CB45-CB48) | ByteRover / Mem0 / Graphiti（对齐） |
| | 关系管理 (CB49-CB52) | Supermemory / Mastra / MemMachine（对齐） |
| | 自优化 (CB57) | SelfMem（arXiv:2607.03726，对齐） |
| **保护层** | 50 级 Guardian 链 | 遗忘防护 / 压缩审计 |
| **检索通道** | 47 路融合检索 | 语义/图谱/精确/混合 |

---

## 性能基准（本地实测口径）

| 指标 | Trinity（本地实测） | 口径 |
|:-----|:-------:|:-----|
| 端到端查询 P50 / P99 | **30-41ms / 33-49ms** | MemBench v1.0 单机 Windows（benchmark/MEMBENCH_REPORT.md） |
| 200 并发 QPS | **2,431**（0 错误，~27MB） | concurrency_bench |
| SQuAD v1.1 R@5（180 题） | **98.3%** | 题目偏易，本地 passage selection |
| LoCoMo Recall@5（50 题） | **0.88**（会话聚合写入） | 中文本地集，非官方英文集 |
| LongMemEval-style R@5（500q） | **0.992** | 社区 mock 集（官方集 HF 阻塞期间） |
| MemSyco（LLM judge，20 题） | **0.88**（谄媚率 10%） | 小样本 |
| 压缩经济 | **~21%** token 节省 | 15 条采样（真实 LLM 模式 78-97%） |
| pytest | **732 passed / 0 failed** | 全量 |

| **LongMemEval_S（官方 ICLR 2025，500 题）** | **session R@5=0.968 · turn R@5=0.922** | 官方数据集实测（2026-08-16，hybrid top-5，见 docs/bench-official） |

> 📊 官方 LongMemEval_S 详情与分题型：docs/bench-official/LongMemEval_S_REPORT_20260816.md
> ⚠️ **口径声明（2026-08-16）**：README 旧版引用的 "LongMemEval 96.4% / BEAM 10M 64.1%" 系 **Exabase M-1 / Hindsight 的成绩**，
> 并非 Trinity 实测，已移除。BEAM / LoCoMo 英文官方集仍未跑（网络限制），不构成对外宣称。

---

## 功能特性

- **多模态**: 文本、图像、音频统一记忆
- **多租户**: 三级隔离（persona_id / session_id / tenant_id）
- **47 路检索**: 渐进级联融合，P50=30-41ms（本地实测）
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
