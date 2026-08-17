# Trinity 功能汇总与未来规划

> 生成日期：2026-08-14 | 基于本地源码 v8.2.0（Docker 镜像运行 v7.0.0，本地 API 运行 v8.0.0）
>
> **⚠️ 2026-08-15 更新注记**（本文为历史快照，最新状态见 TRINITY_STATUS_20260815_V2.md）：
> 测试 583 → **732 passed / 0 failed**；本地数据 31 条 → **12,164 条（active 1,920）**；
> 端口 api:8005 → **:8001**、dash:3000 → **:3005**、新增 mcp v2 :8003 + gateway :8002；
> 模块 122 宣称 → **41 active + 261 储备**（audit 管理）；V2 动作 A/B/C 已落地
> （记忆可迁移 / 企业治理 / 联邦网络）。

---

## 一、Trinity 是什么

**Trinity = Memory Operating System（记忆操作系统）**，不是普通的"记忆库"：

> 任何记忆存储（向量库、图库、SQLite/PostgreSQL）都可以插进来，之上叠加**身份、RBAC、审计、经济协议**——面向多智能体的共享记忆基础设施。

- 规模（实测）：22+ 子包、**517 个 Python 文件、195,940 行**、**138 处 API 路由**、317 个 modules 子模块（CB 编号制）
- 定位层级：Agent Layer → Governance Layer → Memory Layer → Storage Layer → Economic Layer
- 对标/对齐：Mem0、Zep/Graphiti、LangMem、Supermemory、BEAM、Hindsight、ExaBase、HyperMem、AnchorMem 等 129 篇论文
- 版本演进（源码核查）：v6.93 AgentBrain → v6.94 Bridge/A2A → v6.95 MemoryAggregator → v6.96 AutoRegistry → **v8.0** 多锚点身份 + DCSA-EJP 双循环审计 + A2A + Marvis → **v8.2** MemoryCompressor + Ed25519/x509 签名 + OpenTelemetry → v8.3 主动记忆收集 → v8.5 流式摄取
- 注：README 宣称的"GraphRAG""语音收件箱"等部分名词在源码中无对应模块，实际实现为 CausalGraph/GraphRetriever/GoS BFS（图谱）与运行时脚本（voice_inbox）

---

## 二、核心功能全景

### 1. 记忆存储层（Storage）
| 后端 | 状态 | 说明 |
|---|---|---|
| SQLite (FTS5) | 生产 | 默认、零配置 |
| PostgreSQL (pg_trgm) | 生产 | UUID、GIN 索引、schema 迁移 |
| ChromaDB | Beta | 向量原生存储 |
| Vectile | Beta | 磁盘向量索引 |

- 记忆模型：workflow 分层（working / episodic / semantic / procedural）
- CRDT 版本化写入 + SHA-256 审计哈希
- 本地实际数据：SQLite 31 条记忆（583 摄入 / 552 合并 / 425 历史查询）

### 2. 检索能力（Retrieval，47 通道 + 6 大算法）
| 通道 | 说明 | 对标 |
|---|---|---|
| BM25 + jieba FTS5 | 中文分词稀疏检索，CJK 自动检测 | — |
| FAISS HNSW 向量 | 稠密检索（hnswlib→FAISS→Numpy 降级） | — |
| Exabase 3-Stage | 三信号打分（语义+词汇+时间） | LongMemEval 96.4% |
| BEAM-LIGHT | 分层情节记忆 | ICLR 2026 |
| Hindsight 4-Network | 四网络记忆融合 | BEAM 10M 64.1% |
| Zikkaron Hopfield | Hopfield 能量 + 扩散激活 | Non-LLM SOTA 40.4% |
| CausalGraph/GraphRetriever | 因果图 + GoS BFS 图谱遍历 | — |
| 跨模态 | 图搜文 / 文搜图（CrossModalRetriever） | — |
| RRF 融合 | MemoryAggregator 5 通道 RRF 融合 | — |
- 47 通道体系：ch1-8 核心（dense/sparse/语义稀疏/时序/重要性门控/新鲜度/类别/重排）+ ch33-38 增强（多跳/对比负样本/查询分解/HyDE/跨模态/时序模式）+ 备份通道
- 稀疏/重排：SPLADE（naver/splade-cocondenser）、CrossEncoderReranker、ColBERT

### 3. 多智能体（A2A v0.3 + 共享记忆）
- A2A 协议（Google A2A v0.3）：JSON-RPC 2.0、单播/广播、能力协商、gRPC/SSE 传输；AgentCard、CapabilityRegistry、TaskManager、RSA 签名/能力授权/任务 ACL、Ed25519+x509 链、MarvisAdapter
- Agent 层：AgentBrain/DecisionEngine、AgentBridge（调度前注入/调度后提取）、MemoryAggregator 共享池、DimensionEngine、import 时自动注册（AutoDiscovery）
- 身份层（Identity，arXiv 2604.09588 对齐）：5 类锚点、四维加权漂移检测（0.3/0.3/0.25/0.15）、身份重建、身份包导入导出、RLM 动态路由

### 4. MCP 集成（8 个工具，stdio + SSE，MCP 1.0 兼容 v1.1.0）
`memory_search`（semantic/graph/exact/hybrid 四模式） / `memory_write`（CRDT 版本化 + SHA-256 审计） / `memory_update` / `memory_delete`（软删） / `audit_query` / `trinity_diagnostics` / `memory_chronicle` / `memory_tag_search`
- 资源：`trinity://stats|snapshot|health`、`sessions://list|{id}`
- 写入时双写共享聚合器；检索空结果自动回退会话全文搜索；另有 LangChain 适配器

### 5. REST API（135+ 端点，v8.0.0）
按功能域分组：
- **Health/Metrics/Diagnostics**：健康检查、Prometheus 指标、全组件自检
- **Memories**：CRUD、老化、冲突、去重、多模态、关联链接、touch
- **Search**：hybrid / cross-modal（图搜文、文搜图）
- **Graph**：实体、关系、遍历
- **Embeddings/Vector**：单条/批量嵌入、向量搜索、索引
- **Agents**：注册、写/批量写、搜索、池统计、洞察、导出、桥接
- **Audit**：记忆审计、agent 回放、完整性、时间线、DCSA 双循环审计、宪法不变式、六项指标
- **Identity**：锚点、画像、漂移、重建、路由
- **A2A**：目录、任务、消息、安全签名/ACL、Marvis
- **Memory Market**：挂单/摘单/搜索/订单簿/买入/声誉/背书/举报/估价
- **Self Evolution**：访问追踪、热力图、热点、模式、反馈、质量告警、建议、进化周期
- **Memory Compression**：压缩、统计、恢复
- 另有 GraphQL API（Strawberry，查询/变更/订阅）

### 6. 治理与安全（Governance）
- **50 层 Guardian Chain**：Injection → Sandbox → Audit → Sanitize → Self-heal，L1-L50
- **RBAC**：6 角色，写入时作用域强制
- **审计**：DCSA-EJP 双循环审计、宪法不变式、SHA-256 完整性校验、GDPR 第 17 条删除权
- 对抗记忆防御、投毒记忆审计、后门检测（论文对齐）

### 7. 自我进化（Self-Evolving）
- MetaEvolution 五阶段循环：detect → plan → execute → validate → consolidate
- 自动课程生成、engram 记忆重放（睡眠整合）、RL 保留策略、漂移检测自愈
- 进化 API：热力图、热点、模式挖掘、质量告警、建议自动应用

### 8. 记忆市场（Economic Layer）
- TrustExchange：资产创建/挂单/交易、订单簿、估价、声誉引擎、背书/举报
- 链上哈希审计、KYC/AML 合规设计

### 9. 其他亮点
- **间隔重复**（Anki 导出、语音复习）、**语音收件箱**、**harvester 采集器**（网页/视频/Hercules）
- **Raft 共识集群**（3 节点，100/100 写入）
- **神经形态适配**：Loihi 2（SNN）、TrueNorth（<100mW）
- **SDK**：Python / TypeScript / Go 三语言
- **Docker 4 容器栈**：mcp:8000 / api:8005 / db:5430 / dash:3000
- **Benchmark**：LongMemEval（模拟 R@5=0.9818）、SQuAD 改编、BEAM、LoCoMo、MemoryAgentBench、自定义数据集协议

### 10. 实际落地场景（本机）
- **SmartCOS-WMS v4.0**：五维架构（AI/流程/功能/工具/数据层），12 容器、PostgreSQL 132 表、Gateway 112 端点、IoT 4 协议（MQTT/Modbus/PLC/OPC UA）、数字孪生、3 个前端看板
- 记忆来源分布实测：file-agent(9)、main(5)、browser(4)、app-agent(4) 等 14 个来源

---

## 三、现状诊断

| 维度 | 状态 | 风险 |
|---|---|---|
| 代码规模 | 极庞大（517 文件 / 195,940 行 / 138 路由） | 维护负担重，build/lib 副本与调试杂文件多 |
| 版本一致性 | 源码 8.2.0 vs Docker 镜像 7.0.0 vs server 报告 8.0.0 | 镜像滞后、版本信息多处不一致 |
| 文档一致性 | README(8.2.0)/CHANGELOG(停在 v6.37)/ROADMAP(停在 v6.40) | 规划与变更记录严重过时 |
| 依赖管理 | pyproject 依赖清单缺失 strawberry 等 | 安装即失败（本次已实测修复） |
| 测试 | 208/208 自测通过 | 官方基准（LongMemEval-S 真实集）未跑 |
| 数据规模 | 31 条记忆（本地） | 未规模化验证 |
| 认证 | git remote 内嵌明文 token | 安全风险 |
| 文档真实性 | README 部分名词（GraphRAG/语音收件箱）源码无对应 | 夸大宣传风险 |
| GitHub 仓库 | trinity-tick/trinity（public，0 stars/0 forks，2026-07-16 创建，最近推送 07-31） | 无社区曝光 |

---

## 四、未来规划

### 战略定位：不是"又一个记忆库"，而是"治理优先的记忆操作系统"

Trinity 相对 Mem0/Zep 等竞品的差异化：**50 层 Guardian + RBAC + DCSA 审计 + 记忆市场**。未来规划围绕"让治理能力和多智能体共享成为可交付的产品"，而不是继续堆模块。

### 阶段一：工程化修复（1-2 个月）——把地基打牢
| 项 | 动作 |
|---|---|
| 依赖治理 | pyproject.toml 补全依赖（strawberry、sdk、mcp extras 实测化），修 `pip install -e .` 即可用 |
| 版本对齐 | 统一 README / CHANGELOG / ROADMAP / server 版本号，恢复 CHANGELOG 从 v6.37→v8.5 的记录 |
| 安全 | git remote 移除明文 token → 换 GitHub CLI/credential manager 或只读 token |
| 镜像重建 | 重建 Docker 镜像至 v8.2.0，消除 7.0.0 vs 8.2.0 漂移 |
| 仓库卫生 | 清理 build/lib、__pycache__、调试杂文件（agg.txt、_jieba*、proc_test* 等） |
| 自动化 | CI 加依赖完整性检查（pip check + import smoke test） |

### 阶段二：可信化验证（2-4 个月）——把分数做实
- 跑**官方 LongMemEval-S（500 题）**、LoCoMo、MemoryAgentBench、BEAM 真实基准，替换"模拟数据集"分数
- 用 benchmarks.arena.MemorySystem 协议参加 MemArena 社区评测
- 大数据量压测：10 万级记忆导入（当前仅 31 条本地数据）、PostgreSQL 生产验证、Raft 3 节点真实跨机部署

#### 实测基线（2026-08-14，真实检索器 MemoryAggregator hybrid + LoCoMo 50 题中文集）
| 指标 | MockRetriever（旧报告） | 真实检索器（首次实测） | 会话聚合增强（v2） |
|---|---|---|---|
| Recall@5 | 0.0600 | 0.1000 | **0.8800** |
| Precision@5 | 0.0160 | 0.0200 | 0.1840 |
| MRR | 0.0333 | 0.0237 | **0.5353** |

分类明细：single-session-user Recall@5=0.2857、multi-session-reasoning=0.1429，其余类别（assistant 回复/temporal/knowledge-update/preference）均为 0。
**诊断（第一轮）**：①评测协议为"片段召回 vs 答案合成"，ground truth 是总结性长文本，关键词分散在多个 turn，Trinity 无生成式答案合成层，天然难命中；②中文问题 BM25+jieba 召回有限；③query 是问题而非答案，缺少 query expansion。**结论：分数不代表检索器完全无效，但证明"模拟分 0.98"严重虚高，真实基线需如实披露，并需补检索增强。** 评测脚本已保留：`benchmark/locomo_real_eval.py`（可复用，内存模式不污染数据）。

**检索增强实验（v2，2026-08-14）**：对比 4 配置（脚本 `benchmark/locomo_real_eval_v2.py`，结果 `locomo_enhanced_report.json`）：
| 配置 | Recall@5 | MRR | 结论 |
|---|---|---|---|
| A. 单 turn 基线 | 0.14 | 0.061 | 逐条写入 → 检索碎片化 |
| **B. 会话聚合** | **0.88** | **0.535** | **记忆粒度 = 决定性因素（6.3× 提升）** |
| C. 单 turn + jieba 查询扩展 | 0.14 | 0.067 | 查询扩展无增益（噪声抵消） |
| D. 会话聚合 + 查询扩展 | 0.88 | 0.508 | ≈B |

**核心结论**：Trinity 检索栈（keyword+vector+RRF，1024 维真实嵌入）本身能力正常；**真实瓶颈是默认逐 turn 写入导致记忆碎片化**。产品级改进点：提供"会话/事件级聚合写入"API 或自动聚合策略（对 WMS 事件沉淀尤其重要：按工单/事件聚合而非逐条日志）。

**已产品化（2026-08-14）**：新增 `POST /memories/session` 端点——接收 `{session_id, turns:[{speaker,text}], ...}`，把整段对话聚合为一条记忆并**双写**（引擎 + 共享聚合池）。实测：写入 0.5s，检索"B12 货位缺货 补货 方案"可 top-1 召回该聚合记忆（BM25 命中）。调用示例：
```bash
curl -X POST http://localhost:8001/memories/session -H "Content-Type: application/json" -d '{
  "session_id": "SO-001", "source_agent": "wms-outbound",
  "turns": [{"speaker": "用户", "text": "B12 缺货怎么处理？"},
            {"speaker": "助手", "text": "改用 C05 补货，延误 25 分钟"}]
}'
```

### 阶段三：场景深化（3-6 个月）——把 WMS 案例做成样板
- SmartCOS-WMS 已落地（五维架构、132 表、112 端点、IoT 4 协议），下一步：把 Trinity 记忆接入 8 个 smartcos AI 微服务（prediction/routing/voice/vision/orchestrator），实现**跨 agent 共享运营记忆**
- 文档库/法规记忆：用 harvester + 记忆池沉淀 SOP、法规、税率知识（现有数据已有 invoice/tax/pdf 主题）
- 数字孪生 + 记忆：设备影子、异常事件进记忆图谱，AI 层预测纠错闭环

### 阶段四：产品化与商业化（6-12 个月）
| 方向 | 说明 |
|---|---|
| 企业控制台 | 租户管理、RBAC 可视化、审计回放 UI（复用 /audit + /dashboard） |
| SaaS API | 托管记忆服务（ROADMAP 已有），按量计费 + 记忆市场 TrustExchange 落地 |
| 插件系统 | 第三方 adapter 插件（ROADMAP Future） |
| 云原生 | Helm chart / K8s（Docker 4 容器 → K8s operator） |
| 边缘/移动 | WASM 客户端、iOS/Android SDK（ROADMAP Future） |
| MCP v2 / A2A v1 | 跟踪 MCP 规范演进 + Google A2A 正式版对齐 |

### 关键风险与对策
1. **模块过载**：317 个 modules 子模块维护成本高 → 砍掉未用模块、建立模块生命周期（deprecate/archive）
2. **基准可信度**：模拟分数被质疑 → 官方数据集补测，透明披露
3. **竞品挤压**：Mem0 商业化快 → 走"治理+审计+市场"差异化，不与通用记忆库正面竞争
4. **安全合规**：GDPR 删除权、记忆投毒 → Guardian + 审计已有基础，需补隐私计算落地（联邦已有 PrivacyBudget）

---

## 五、行业生态对标（2025-2026 调研）

### 5.1 竞争格局
| 框架 | 资本/动态 | 路线 |
|---|---|---|
| **Mem0** | $24M A 轮（YC/Peak XV/Basis Set） | "AI 记忆层"基础设施，分层提取 + 三级记忆 + 混合检索 |
| **Zep/Graphiti** | YC W24，时序知识图谱（arXiv:2501.13956） | 事件+实体+时间衰减图谱 |
| **Letta**（原 MemGPT） | Memory Blocks；与 DeepLearning.AI 合作课程 | agentic 上下文管理，"LLM 即操作系统" |
| **Cognee** | €7.5M seed | 知识图谱记忆 |
| **Supermemory** | $3M seed | 统一记忆引擎 |
- Anthropic 官方推动 **Context Engineering**（记忆/压缩/工具清理）；OpenAI Agents SDK 新增 **Sessions 记忆**；持久记忆赛道竞争白热化
- MCP 已移交 **Agentic AI Foundation** 治理；官方 memory server 检索质量被诟病 → **记忆类 MCP server 是机会窗口**
- **A2A 已捐赠 Linux Foundation**（2026-04 一周年）：A2A 管 agent↔agent、MCP 管 agent↔工具，定位互补
- 治理方向独立成型：权限化知识图谱、"Governed Memory OS"、开源受治理共享记忆 Caura、CrewAI Enterprise 租户隔离/审计模式

### 5.2 对 Trinity 的启示（子代理调研结论）
1. **叙事对齐**：Trinity 的 "Memory OS" 话语与 Mem0 融资叙事、市场认知一致，顺势而为
2. **多后端分工**：向量=语义、图=关系/时序（对齐 Graphiti）、SQLite=结构化审计，做成 Mem0 式分层混合检索
3. **治理是最大差异化**：企业痛点是 RBAC/审计/租户隔离/合规 → 把 50 层 Guardian 表述为"治理即架构"，补可审计 API
4. **MCP 是生态入口**：官方 memory server 检索质量差 → Trinity 主打"更聪明的记忆 MCP server"切入 Claude/IDE 生态
5. **结合 A2A**：A2A 定义 agent 间通信 + Trinity 定义共享记忆 = "记忆服务化"护城河（Trinity 已原生支持 A2A v0.3 ✅）
6. **商业化**：托管版 memory-as-a-service 已被资本验证；可探索记忆市场/交易（Trinity 已有 TrustExchange ✅）；用 Hindsight 类基准建立可量化记忆质量指标
- 市场空间：中国智能体记忆市场预测 **14.4 亿→642.5 亿元**；全球 Agentic Memory Systems 市场报告至 2033
- 数据佐证：Trinity 论文对齐的 Hindsight/SelfMem/Mem0/Graphiti 均在 HF/arXiv 可查；BEAM 论文来源未能核实（README 相关表述需谨慎）

---

*本文档基于本地源码核查（2026-08-14），功能部分经代码级验证；生态部分基于 2026 年网络调研，来源见正文链接。*
