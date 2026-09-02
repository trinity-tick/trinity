# Trinity 特性总览：能做什么（2026-08-15 全景版）

> 基于源码盘点（526 Python 文件 / 147 API 路由）+ 全量实测（732 passed / 50 skipped）
> + 2026 Q3 网络对比。Trinity = **记忆操作系统（Memory OS）**：任何存储后端之上叠加
> 检索、治理、身份、进化、经济协议的共享记忆基础设施。

---

## 一、核心特性（按层）

### 1. 存储层（Storage）
| 特性 | 说明 |
|---|---|
| SQLite (FTS5) | 默认生产后端，WAL 模式，jieba 中文分词，零配置 |
| PostgreSQL (pg_trgm) | 生产后端，GIN 索引 |
| ChromaDB / Vectile | 向量原生 / 磁盘向量索引（Beta） |
| **CRDT 版本化写入** | 冲突保留（conflict-preserving），每条记忆带版本链 |
| **SHA-256 审计哈希** | 每次写入算哈希，去重/一致性链/身份保留 |
| **AES-256-GCM 存储加密（B5）** | `TRINITY_STORAGE_ENCRYPTION=on` 可选，内容列密文落盘，FTS/哈希链兼容 |
| 记忆分层 | working / episodic / semantic / procedural |

### 2. 检索层（Retrieval，47 通道 + 6 大算法族）
| 特性 | 说明 |
|---|---|
| **混合检索 Hybrid** | 5 路 RRF 融合（keyword + vector + graph + ...） |
| BM25 + jieba FTS5 | 中文分词稀疏检索（CJK 自动检测，已修多字查询） |
| FAISS HNSW 向量 | 稠密检索，**ANN 落盘持久化 + 增量维护**（跨进程免重建） |
| Exabase 3-Stage | 三信号打分（语义+词汇+时间） |
| BEAM-LIGHT / Hindsight 4-Network / Hopfield | ICLR/BEAM 对齐的分层情节记忆与能量模型 |
| 因果图谱 GoS BFS | 图谱多跳遍历 |
| **跨模态（A4）** | 图搜文 / 文搜图（CrossModalRetriever + ImageEncoder） |
| 重排 | SPLADE / CrossEncoderReranker / ColBERT |
| **语义缓存** | Redis 305x 命中（retriever 层，scope 隔离已修） |
| **自适应路由** | query 分层 light/full，短查询走轻通道 |
| **时点查询（R2）** | `query_relations_at(时间)` 返回该时点有效的边 |

### 3. 记忆生命周期（治理）
| 特性 | 说明 |
|---|---|
| **衰减 decay** | 多因子遗忘（时间+访问+重要性），真实 LLM 摘要 |
| **睡眠整合 sleep_consolidation** | 提取→冲突消解→压缩→归档→图更新→衰减报告 |
| 压缩 | 真实 LLM 压缩（实测 78.2% token 节省） |
| 分层 tiers / 归档 | memory_layer 回填（semantic/episodic） |
| 去重 dedup | 归一化 + embedding 相似（11,174→11,151） |
| 冲突仲裁 | conflict_group 链 + resolve |
| 间隔重复 | Anki 导出 |
| **每日维护链** | mirror→decay→tiers→consolidate→dedup→sync→compact（03:00 自动） |

### 4. 多智能体与身份
| 特性 | 说明 |
|---|---|
| **A2A v0.3** | AgentCard / 能力注册 / RSA 签名 / 任务 ACL / MarvisAdapter |
| **共享记忆聚合池** | MemoryAggregator（15+ agent 可注册共享） |
| **身份层** | 5 类锚点、四维加权漂移检测（0.3/0.3/0.25/0.15）、身份重建、包导入导出、LLM 路由 |
| **治理层（B3）** | YAML 策略（isolated/shared/delegated）+ 热切换 + 审计（最具体规则优先） |
| DSH 结构融合 | 身份/事件/todo/header/goal/schedule 6/6 自动同步 |

### 5. 治理与安全（50 层守护）
| 特性 | 说明 |
|---|---|
| **50 层 Guardian Chain** | Injection→Sandbox→Audit→Sanitize→Self-heal，L1-L50 |
| **RBAC** | 6 角色，default-deny，写入作用域强制 |
| **DCSA-EJP 双循环审计** | 全部写操作审计 + 签名（Ed25519/x509）+ SHA-256 完整性 |
| GDPR | 一键导出 / 删除权（软删+审计）/ 合规手册 |
| 对抗防御 | 投毒记忆审计、后门检测 |

### 6. 自我进化
| 特性 | 说明 |
|---|---|
| **MetaEvolution 五阶段** | detect→plan→execute→validate→consolidate |
| 热度图 / 热点 / 模式挖掘 | 进化 API |
| 课程生成 / 记忆重放 | 睡眠整合式 |
| 质量告警 / 建议自动应用 | 自愈循环 |

### 7. 记忆市场（经济层）
| 特性 | 说明 |
|---|---|
| **TrustExchange** | 资产创建/挂单/交易/订单簿/估价/声誉/背书/举报（11 端点） |
| 链上哈希审计 / KYC 设计 | 合规 |
| 协议文档 | MEMORY_MARKET_PROTOCOL.md |

### 8. 集成与协议
| 特性 | 说明 |
|---|---|
| **REST API** | 147 端点（记忆/检索/图谱/身份/A2A/市场/进化/审计/压缩） |
| **MCP** | 8 工具 + stdio/SSE(:8000)/streamable-http(:8003, MCP v2) |
| **DSH 原生插件** | dsh-trinity（engine_worker stdio NDJSON + 15 个 trinity_* 工具 + 结构层） |
| **Gateway** | OpenAI/Mem0 兼容层 :8002（鉴权/限流/模型映射/指标） |
| **GraphQL** | Strawberry（查询/变更/订阅） |
| **联邦 federation** | 多实例 export/import/diff 同步 |
| SDK | Python / TypeScript / Go |
| Docker | 4 容器栈（mcp/api/db/dash） |
| OpenTelemetry | 可观测 |
| Raft 共识 | 3-5 节点集群写入 |

### 9. 基准与评测（MemBench）
| 特性 | 说明 |
|---|---|
| LoCoMo | 0.88（会话聚合写入） |
| LongMemEval | 500q top_k=10 R@5=0.992（MS 0.525→0.95） |
| SQuAD | 98.3% |
| MemSyco | 0.88（幻觉率 10%） |
| 压缩经济学 | 真实 78.2% vs mock 97% |
| leaderboard | HTML 榜单已生成 |

---

## 二、能做什么（应用场景 × 成熟度）

| 场景 | 怎么用 | 成熟度 |
|---|---|---|
| **Agent 长期记忆** | `ingest` 写入 + hybrid 检索 + 衰减治理，任意 LLM agent 接入（DSH/MCP/OpenAI 兼容） | ★★★ 生产可用 |
| **RAG 增强** | 任意 RAG 应用叠记忆层：检索/去重/冲突/压缩全生命周期 | ★★★ 数据已验证 |
| **多智能体协作** | A2A + 共享聚合池 + 治理策略（谁可读谁）+ 身份漂移检测 | ★★★ 端到端可跑 |
| **垂直知识库** | 教育（间隔重复）/ 客服 / 合规知识库 / 个人第二大脑 | ★★★ |
| **记忆即服务** | Gateway OpenAI 兼容 + 联邦 + 市场协议 → SaaS 化 | ★★☆ 可演示 |
| **审计合规底座** | 50 层守护 + RBAC + DCSA 审计链 + GDPR 工具 + 存储加密 → 企业/出海准入 | ★★★ |
| **跨模态记忆** | 图搜文/文搜图（图片描述记忆 + 特征检索） | ★★☆ 闭环已验证 |
| **研究平台** | 47 通道消融 / 长程一致性 / 压缩经济学 / MemBench 基准 | ★★★ |
| **知识市场** | TrustExchange 挂单/估价/声誉 → 数据资产交易 | ★★☆ 协议已通 |
| **前沿** | 神经形态对齐（Loihi/TrueNorth）、Raft 集群、边缘 WASM | ★☆☆ 探索 |

## 三、特性数（2026-08-15 实测）

| 项 | 值 |
|---|---|
| 代码 | 526 Python 文件 / 147 API 路由 / 317 modules 子模块 |
| 检索 | 47 通道 / 6 大算法族 |
| 守护 | 50 层 Guardian Chain |
| 测试 | 732 passed / 50 skipped / 0 failed |
| 数据 | 记忆 12,164 · 实体 11,799 · 关系 29,500 · 审计 7,329 |
| 服务 | api :8001 · mcp :8000/:8003 · gateway :8002 · dashboard :3005 · PG :5430 |
| 最近新增 | V2 三动作（记忆可迁移 / 企业治理 / 联邦网络）· B3 治理 · B5 加密 · A4 跨模态 · 蒸馏 11x · goal 防回归 |

## 四、一句话总结

Trinity 能做的：**一个带治理、可进化、可交易、可联邦、跨模态、加密安全的完整记忆操作系统**——
从"给单个 agent 记东西"（最成熟）到"多智能体共享记忆 + 审计合规 + 记忆市场"（差异化护城河）
都能覆盖；当前最短板是官方基准数字（HF 网络阻塞）与对外产品化包装（SaaS/Console/SDK 生态）。
