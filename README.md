# Trinity — Open Memory Layer with Governance

> **v8.2.0** — 治理优先的记忆操作系统：模型会换、框架会换，但记忆不换。
> Trinity 是让记忆**可迁移、可治理、可交易**的基础设施。

Trinity is not a "memory library." It is a **Memory Operating System** — an
infrastructure layer that any memory store (vector DB, graph DB, SQLite) can
plug into, with retrieval, governance, identity, evolution, and economic
protocols on top.

**定位（2026-08-15, V2）**：记忆是 AI 最后的切换成本。Trinity = 开放记忆层 + 治理底座——
记忆可进可出（[可迁移标准](scripts/memory_portability.py)）、企业敢存（治理/合规/审计）、
记忆值钱（TrustExchange 市场）。

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│ Agent Layer      A2A v0.3 · DSH 原生 · 共享聚合池 · 身份   │
├──────────────────────────────────────────────────────────┤
│ Governance Layer RBAC(6) · 50-Guardian · 审计签名 · 加密  │
│                  B3 策略层（isolated/shared/delegated）    │
├──────────────────────────────────────────────────────────┤
│ Memory Layer     47 通道 · PPR · 意图压缩 · 蒸馏 11x      │
│                  个性化(PAHF) · 跨模态 · 联邦              │
├──────────────────────────────────────────────────────────┤
│ Storage Layer    SQLite(FTS5) · PostgreSQL · AES-GCM 加密 │
├──────────────────────────────────────────────────────────┤
│ Economic Layer   TrustExchange 记忆市场 · 资产定价         │
└──────────────────────────────────────────────────────────┘
```

**526 Python files · 243K+ lines · 147 API endpoints · 705 tests passing**

---

## Quick Start

```bash
# 安装（系统 Python 3.11+）
cd trinity
pip install -e .

# 验证
python -c "import trinity; print(trinity.__version__)"   # → 8.2.0

# 全量测试（705 passed / 50 skipped / 0 failed）
python -m pytest tests/ -q
```

### 服务（全在线，supervisor 自愈）

| 服务 | 端口 | 说明 |
|---|---|---|
| trinity-api | :8001 | REST（147 端点） |
| trinity-mcp | :8000 / :8003 | MCP SSE / MCP v2 streamable-http |
| gateway | :8002 | OpenAI/Mem0 兼容层（DeepSeek 上游，鉴权/限流/模型映射） |
| dashboard | :3005 | 可视化 |
| PostgreSQL | :5430 | 维护镜像（docker） |

---

## Key Features（名实一致，2026-08-15 实测）

| 能力 | 状态 | 说明 |
|---|---|---|
| **41 个 active 模块** | ✅ | 运行路径可达（另有 261 个论文对齐储备，`status: orphan` 标注，audit_modules.py 审计） |
| **47 通道检索** | ✅ | BM25+jieba / FAISS HNSW / Exabase / BEAM-LIGHT / Hindsight / PPR 图扩散 / RRF 融合 |
| **语义缓存** | ✅ | Redis 305x，scope 隔离 |
| **存储加密** | ✅ | AES-256-GCM 可选（TRINITY_STORAGE_ENCRYPTION），FTS/哈希链兼容 |
| **治理策略层** | ✅ | B3：YAML 策略（isolated/shared/delegated）+ 热切换 + 审计 |
| **多智能体** | ✅ | A2A v0.3 + 共享聚合池 + 身份漂移检测 |
| **意图压缩** | ✅ | SimpleMem 对齐（TRINITY_INTENT_CLUSTER=on） |
| **结构化蒸馏** | ✅ | ICML 2026 对齐，11x 压缩（TRINITY_DISTILL_COMPRESS=on） |
| **个性化** | ✅ | PAHF 双反馈（Meta ICLR 2026 对齐） |
| **跨模态** | ✅ | 图搜文/文搜图闭环 |
| **DSH 结构融合** | ✅ | 6 表自动同步（会话/事件/goal/todo/header/schedule），goal objective 100% |
| **记忆可迁移** | ✅ | memory_portability.py：标准 JSON/NDJSON + Mem0/Zep 导入 |
| **记忆市场** | ✅ | TrustExchange：挂单/订单簿/定价/声誉（11 端点） |
| **联邦** | ✅ | 多实例 export/import/diff 同步 |

---

## 记忆可迁移（V2 核心）

```bash
# 导出标准格式（记忆护城河入场券：可进可出）
python scripts/memory_portability.py export --out memories.json
python scripts/memory_portability.py export --out memories.ndjson --format ndjson

# 导入（幂等：content_hash 去重）
python scripts/memory_portability.py import --file memories.json

# 从 Mem0 / Zep 迁移
python scripts/memory_portability.py import-mem0 --file mem0_export.json --persona p1
python scripts/memory_portability.py import-zep --file zep_export.json --persona p1
```

---

## Benchmark（本地实测口径，官方集网络阻塞中）

| Benchmark | Score | 口径 |
|---|---|---|
| LongMemEval-style (500q) | R@5 = **0.992** | 社区 mock 集（官方集 HF 不可达） |
| SQuAD v1.1 (adapted) | R@5 = **98.3%** | 180 题 passage selection |
| LoCoMo (subset) | R@5 = **0.88** | 38 题会话聚合 |
| pytest | **705 passed / 50 skipped / 0 failed** | 全量 |

> 本地口径 ≠ 官方口径。HF 网络就绪后替换为 LongMemEval-S / LoCoMo 官方集，
> 获得可对外宣称的公开数字（见 docs/FUTURE_PLAN_V2_20260815.md）。

---

## Research Foundation

Trinity 的 `second_brain` 与 2026 前沿对齐：PPR/HippoRAG 2、SimpleMem (ICML 2026)、
Structured Distillation (11x)、PAHF (Meta ICLR 2026)、Hindsight/BEAM、Mem0/Zep/Graphiti 思路。

---

## Requirements

- Python 3.11+（推荐 3.14）
- Docker Desktop（容器化部署）
- jieba（中文分词）、fastapi、strawberry-graphql

---

## License

MIT — see [pyproject.toml](pyproject.toml).

---

*Trinity: model changes, framework changes, memory doesn't.*
