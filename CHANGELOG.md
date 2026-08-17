# Changelog

## v8.5.0 (2026-08-14 源码核验)
**流式摄取（Streaming Ingestion）**

### New
- 流式摄取管线（v8.5 里程碑）

## v8.3.0 (2026-08-14 源码核验)
**主动记忆收集（Active Collection）**

### New
- Collector 守护进程（scanner + heartbeat + 事件捕获）

## v8.2.0 (2026-08-14)
**MemoryCompressor, 签名与可观测 + 当日实测维护轮**

### New
- MemoryCompressor（LLM 压缩：mock/real 双模式，`--llm real` 实测 DeepSeek 可跑）
- Ed25519 / x509 签名链（A2A 安全）
- OpenTelemetry 集成（/metrics + tracing）

### 实测与修复（2026-08-14 维护轮）
- MemBench 基线：端到端 P50≈30-41ms；200 并发 2,431 QPS / 0 错误；SQuAD R@5=98.3%；locomo 0.12→0.88（会话聚合）；memsyco LLM judge Composite=0.88
- Memory Gateway（OpenAI/Mem0 兼容层 + SDK + Docker）实测闭环
- 图谱关系层：实体 11,009 / 关系 28,043
- API 修复：`GET /memories/{id}` embedding BLOB 500、hybrid content_preview、`/agents/memory/export` 500、cross-modal 离线保护降级、market asset_id 生成

## v8.0.0 (2026-08-14 源码核验)
**多锚点身份 + DCSA-EJP 双循环审计 + A2A + Marvis**

### New
- Identity 层：5 类锚点、四维加权漂移检测、身份重建、包导入导出
- DCSA-EJP 双循环宪法审计（audit 全链路）
- Google A2A v0.3（AgentCard / RSA 签名 / 能力授权 / 任务 ACL）
- Marvis 生态适配器（联邦注册 / 调度 / 快照 / 信任）

## v6.96 (2026-08-14 源码核验)
- AutoDiscovery：agent 自动注册 + 共享池接入

## v6.95 (2026-08-14 源码核验)
- MemoryAggregator：共享聚合池 + RRF 融合 + 向量索引持久化

## v6.94 (2026-08-14 源码核验)
- AgentBridge：调度前注入 / 调度后提取 + A2A 协议层

## v6.93 (2026-08-14 源码核验)
- AgentBrain / DecisionEngine

## v6.40 (2026-08-14 源码核验)
**Multi-Agent Collaboration（此前未记录，实际已实现）**
- A2A memory sharing protocol（19 端点）
- Distributed memory sync（federation 实测导出 10,632 条）
- Conflict resolution（/memories/conflicts + CRDT 版本链）
- Cross-agent context handoff（Marvis dispatch）

## v6.39 (2026-08-14 源码核验)
**Production Hardening（此前未记录，实际已实现）**
- PostgreSQL 连接池、Redis 语义缓存、API 限流中间件、DCSA 审计、Prometheus /metrics

## v6.38 (2026-08-14 源码核验)
**Cross-Platform Enhancement（此前未记录）**
- Windows 原生支持（service 脚本 + autostart 循环实测）

## v6.37.0 (2026-07-18)

**BM25 Sparse Retrieval, CrossEncoder Reranker, Channel Config**

### New
- BM25SparseRetriever: keyword-based sparse retrieval with dense-sparse fusion (443 lines)
- CrossEncoderReranker: cross-encoder re-ranking for precision refinement (271 lines)
- ChannelConfig: 47-channel retrieval configuration management (156 lines)

### Enhancements
- PostgreSQL adapter: major refactor (+686 lines) — improved connection pooling, query optimization
- Core cache: significant rewrite (+464 lines) — better eviction, TTL support, statistics
- Retrieval module: cascade logic enhancement (+445 lines)
- Mixed index: improved hybrid search scoring (+264 lines)
- Adapter factory: streamlined configuration (+137 lines)
- VectorIndex: enhanced HNSW config options (+43 lines)
- Client: extended embedding engine access (+39 lines)

### Infrastructure
- Automated release workflow (tag → build → release)
- Issue/PR auto-labeling
- Stale issue management (60d → close)
- PyPI download badge, GitHub release badge

## v6.36.0 (2026-07-14)

**First structured release — package reorganization + benchmark readiness**

### New
- Standardized package structure with pip install support
- Unified CLI (search/ingest/diagnostics/mcp/bench)
- Trinity class — 5-line quickstart API
- Benchmark runner (bench --name longmemeval)
- MCP Server with stdio/SSE support
- LangChain adapter for agent integration

### Modules (122 total)
- CB54 ExabaseRetrieval — Tri-Signal scoring (S_sem + S_lex + T_temporal)
- CB55 HindsightFourNetwork — Four-network separation (P127 aligned)
- CB56 ZikkaronHopfield — Hopfield energy + spreading activation (P128 aligned)
- CB57 SelfOptimizingMemory — Agent-controlled memory strategy (P129 aligned)

### Guardian Chain (50-tier)
- L46 BEAMLIGHTGuard (P125)
- L47 ExabaseRetrievalGuard (P126)
- L48 HindsightFourNetworkValidation (P127)
- L49 ZikkaronHopfieldEnergyGate (P128)
- L50 SelfOptimizingMemoryGuard (P129)

### Infrastructure
- pyproject.toml with setuptools build system
- MIT License, .gitignore
- Example scripts (quickstart, MCP server, LangChain agent)
- Deprecation: old version files consolidated into clean structure

## v6.34 (2026-07-13)
- CB55 HindsightFourNetwork (P127)
- CB56 ZikkaronHopfield (P128)
- +L48, +L49 guards
- +ch45, +ch46 retrieval channels

## v6.32 (2026-07-13)
- CB54 ExabaseRetrieval (P126)
- +L46 BEAMLIGHTGuard, +L47 ExabaseRetrievalGuard
- +ch43, +ch44 retrieval channels
- P0: BEAM-LIGHT ICLR2026 alignment

## v6.28 (2026-07-12)
- CB49 RelationalVersioning (P121 — Supermemory aligned)
- CB50 ContextualChunkIngestion (P122)
- +L42, +L43 guards
- LongMemEval self-test: ~97.8%

## v6.26 (2026-07-12)
- CB47 TokenEfficientMemory (P119 — Mem0)
- CB48 AgentNativeCuration (P120 — ByteRover)
- +L40, +L41 guards

## v6.24 (2026-07-12)
- CB45 ProgressiveCascade (P117 — ByteRover)
- CB46 TemporalValidity (P118 — Zep/Graphiti)
- CB42-CB44 ChromaDB edge layer merge
- +L38, +L39 guards
