# Trinity — Open Memory Layer with Governance

<!-- mcp-name: io.github.trinity-tick/trinity-memory -->

<p align="center">
<img alt="License" src="https://img.shields.io/badge/license-MIT-blue.svg">
<img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue.svg">
<img alt="Tests" src="https://img.shields.io/badge/tests-1261%20passed-brightgreen.svg">
<img alt="Commits" src="https://img.shields.io/badge/commits-335-green.svg">
<img alt="Storage" src="https://img.shields.io/badge/storage-PostgreSQL%2BSQLite-orange.svg">
<img alt="QA" src="https://img.shields.io/badge/MS%20QA-0.467-brightgreen.svg">
</p>


> **v8.2.1** — 治理优先的记忆操作系统：模型会换、框架会换，但记忆不换。
> Trinity 是让记忆**可迁移、可治理、可交易**的基础设施。

> 🚀 **开源就绪（2026-08-26）**：MIT 许可证 · 数据全本地（无遥测）· 存储加密 + 审计可证明 ·
> 全部基准带可复现 manifest。详见 [docs/BENCHMARK_GUIDE.md](docs/BENCHMARK_GUIDE.md)（复现指南）、
> [docs/PRIVACY.md](docs/PRIVACY.md)（隐私说明）、[docs/INDEX.md](docs/INDEX.md)（文档索引）。

> 🏆 **官方基准（2026-08-27）**：LongMemEval-S（ICLR 2025 官方集，500 问）
> **Session Recall@10 = 0.98-0.99** · Turn Recall@10 = 0.93-0.94 · QA accuracy 0.358（旧口径）/
> **0.467（升级口径：top-3 完整上下文 + 语义 judge，300 问）**
> ——检索对齐头部（TiMem/Mem0 0.9+）；结果带 manifest 完全可复现。


> 🧠 **自进化认知协作平台（2026-08-29）**：八大能力在线——记忆（加密+审计+版本链）、
> 知识层（198 源）、自进化引擎（记忆/系统/代码三类指标）、自动化编排（8 规则+全编排）、
> AgentMesh 协作（委托+订阅+配额）、记忆资产化、联邦同步、RAG 服务化（/v1/retrieval）。
> **代码自改三级达成**（参数/脚本/自动合入，fulltest 门禁保障）。
> 快速接入见 [docs/QUICKSTART_20260829.md](docs/QUICKSTART_20260829.md) · 
> 完整总览见 [docs/TRINITY_SUMMARY_20260827.md](docs/TRINITY_SUMMARY_20260827.md) · 
> 优化报告见 [docs/OPTIMIZATION_REPORT_20260827.md](docs/OPTIMIZATION_REPORT_20260827.md)。


## ✨ 亮点（为什么值得看）

1. **可证明的记忆**：每条记忆带 SHA-256 审计回执（可独立重算）+ CRDT 版本链
   ——全网唯一 inspectable memory 完整实践；
2. **自进化引擎**：参数自动调优 → LLM 代码补丁 → 门禁验证自动合入（无人值守）；
3. **自运行平台**：39 任务每日维护链（健康/调参/遗忘/联邦/镜像同步）+ 自愈 + 秒级回滚；
4. **PG 主存储**：PostgreSQL 正式主存储 + SQLite 镜像（一键切换/回滚）；
5. **MS 类目突破**：multi-session QA 0.237→0.467（答案生成时序列表策略）；
6. **零成本判题**：Ollama 本地 judge（云调用 0）+ 启发式蒸馏。

## Quick Start

```bash
# 安装（本地运行，无遥测）
pip install -e ".[dev,test]"

# 启动 API（:8001）+ MCP（:8000/:8003）
python -m trinity.api.server --port 8001
python -m trinity.mcp.server --mode sse --port 8000

# 写入与检索
python -m trinity ingest --content "用户偏好暗色模式"
python -m trinity search --query "用户偏好" --top-k 5

# 维护（每日链：health/evolution/decay/tiers/sync/backup...）
powershell -File dsh-ops/trinity-dsh-maintenance.ps1 -Tasks all

# 评测（12 项功能断言 + 官方基准复现）
python scripts/run_evals.py --all
python benchmark/longmemeval_official_runner.py --limit 100 --qa --out results.json
```

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

**528 Python files · 206K+ lines · 147 API endpoints · 815 tests passing**

---

## Quick Start

```bash
# 安装（系统 Python 3.11+）
cd trinity
pip install -e .

# 验证
python -c "import trinity; print(trinity.__version__)"   # → 8.2.1

# 全量测试（815 passed / 50 skipped / 0 failed，系统 Python 3.14）
python -m pytest tests/ -q
```

### 服务（全在线，supervisor 自愈）

| 服务 | 端口 | 说明 |
|---|---|---|
| trinity-api | :8001 | REST（147 端点） |
| trinity-mcp | :8000 / :8003 | MCP SSE / MCP v2 streamable-http |
| gateway | :8002 | OpenAI/Mem0 兼容层（DeepSeek 上游，鉴权/限流/模型映射） |
| dashboard | :3005 | 可视化 |
| PostgreSQL | :5432 | 主存储（原生 PG18，服务 trinity-pg 开机自启；2026-08-29 切换） |

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
| **记忆市场** | ✅ | TrustExchange：挂单/订单簿/定价/声誉（11 端点）+ 冷启动模拟（scripts/market_sim.py） |
| **联邦** | ✅ | 多实例 export/import/diff 同步 + sync-agent（单向增量+轮询） |
| **短期记忆符号卸载** | ✅ | Mermaid 画布 + node_id 溯源（/offload/*，原文落盘 refs） |
| **Persona 白盒画像** | ✅ | 命题聚合 → persona.md（/persona/*，TRINITY_PERSONA 默认 off） |
| **召回可解释** | ✅ | /memory/search/explain 分数分解（keyword/vector/rerank/final） |
| **一致性校验** | ✅ | scripts/consistency_check.py + maintenance `consistency` 任务（只读） |
| **环境体检** | ✅ | scripts/env_doctor.py（8 项只读检查，退出码 0/1/2） |

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

## Benchmark（实测；官方 LongMemEval_S 已跑，2026-08-16）

| Benchmark | Score | 口径 |
|---|---|---|
| **LongMemEval_S（官方 ICLR 2025，500 题）** | **session R@5 = 0.968 · turn R@5 = 0.922 · hit pos 1.3** | 官方数据集实测（hf-mirror 获取），hybrid top-5 |
| SQuAD v1.1 (adapted) | R@5 = **98.3%** | 180 题 passage selection（本地） |
| LoCoMo (subset) | R@5 = **0.88** | 38 题会话聚合（中文本地集） |
| pytest | **815 passed / 0 failed** | 全量 |
| **LongMemEval_S 500 题 QA（judge3 三票，RouteReasoner 产品化策略路由 + pref-inner2）** | **68.6%（343/500）** | 2026-08-17 全量；SS-A 96.4 / SS-U 92.9 / KU 69.2 / TR 65.4 / SS-P 56.7 / MS 49.6 |
| LongMemEval_S 500 题 QA（judge3 三票，route2 benchmark 脚本） | 63.2%（316/500） | 2026-08-17 基线；MS 43.6 / SS-P 20.0 |
| LongMemEval_S 500 题 QA（dated，旧 judge） | 54.0% | 2026-08-16 全量实测 |

> 📊 官方 LongMemEval_S 详情与分题型：docs/bench-official/LongMemEval_S_REPORT_20260816.md
> **QA accuracy（DeepSeek judged，官方模板，500 题）= 54.0%**（dated 优化：时间戳+全量证据+
> temporal 分步推理；优化前基线 49.6%，temporal-reasoning +15.7pp）。
> 分题型：assistant 91% / user 87% / knowledge-update 64% / multi 36% / temporal 44% / preference 3%。
>
> ⚠️ **口径声明（2026-08-16）**：README 旧版引用的 "LongMemEval 96.4% / BEAM 10M 64.1%"
> 系 **Exabase M-1 / Hindsight 的成绩**，非 Trinity 实测，已移除。BEAM/LoCoMo 英文官方集
> 仍未跑（网络限制），不构成对外宣称。

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
