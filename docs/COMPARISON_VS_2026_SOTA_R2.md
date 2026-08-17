# Trinity vs 网络最优方案 — 2026 Q3 再对比与优化空间（2026-08-15 第二轮）

> 承接 COMPARISON_VS_2026_SOTA.md（第一轮，round34-43 执行后已消化大部分差距）。
> 本文件为**第二轮再对比**：以 2026 Q3 最新网络公开情报为基准，核查剩余差距与新增机会点。

## 一、Trinity 当前快照（实测量，2026-08-15）

| 维度 | 实测值 |
|---|---|
| 版本/规模 | v8.2.0 · SecondBrain 122 模块 / 50 守护层 / 47 检索通道 |
| 数据 | 记忆 11,782（active 1,538）· 实体 11,151 · 关系 28,340 + 12,116 · 审计 6,535 · 版本链 869 |
| 服务 | api :8001 · mcp :8000(SSE) · mcp :8003(MCP v2) · gateway :8002 · dashboard :3005 · PG :5430 · collector |
| 测试 | **616 passed / 43 skipped / 0 failed**（含 B3 治理 9 例 + B5 加密 20 例 + A4 跨模态 4 例） |
| 性能 | FTS 热查 ~3ms · hybrid 命中 ~5ms · ANN 热查 9ms · 语义缓存 305x |
| 基准 | LoCoMo 0.88（自测）· LongMemEval 500q top_k=10 R@5=0.992 · SQuAD 98.3% · MemSyco 0.88 · 压缩 78.2% 真实 LLM |
| 新增能力 | B3 治理层（隔离/共享/委托+审计）· B5 AES-256-GCM 存储加密 · A4 跨模态闭环 · FTS 迁移+CJK 分词修复 |

## 二、2026 Q3 网络最优方案盘点（第二轮，含新情报）

| 方案 | 2026 Q3 定位 | 新增/关键点 |
|---|---|---|
| [Mem0](https://mem0.ai/blog/ai-memory-layer-guide) | 通用记忆层，生态最广 | +26% accuracy 宣称（[Mem0 Review 2026](https://weavai.app/blog/en/2026/05/09/mem0-review-2026-ai-agent-memory-king-26-accuracy/)）；graph memory + token-efficient |
| [Zep / Graphiti](https://www.getzep.com/research/) | 企业级时序知识图谱 | edge 级 bi-temporal 仍是最强项 |
| [Letta (MemGPT)](https://agentmarketcap.ai/blog/2026/04/08/ai-agent-memory-shootout-2026-mem0-zep-letta-supermemory) | agent 运行时内置记忆 | core/archival/recall 块、自我编辑 |
| [Supermemory](https://agentmarketcap.ai/blog/2026/04/17/locomo-ama-bench-long-context-beats-structured-memory) | 网页记忆+知识库 | 2026 冲入 LoCoMo 共享记分板 |
| [Hindsight](https://hindsight.vectorize.io/blog/2026/04/02/beam-sota) | 自我反思记忆 | BEAM 10M token 64.1% #1 |
| [Synap](https://www.maximem.ai/blog/synap-benchmark-results) | **新进者** | **LongMemEval 92% / LoCoMo 93.2%**（独立评测口径，2026 最高公开数字） |
| [Cognee](https://www.cognee.ai/blog/guides/open-source-memory-frameworks-llm-agents) | 图谱记忆 | graphRAG 整合 |
| Anthropic [Managed Agents Memory](https://platform.claude.com/docs/en/managed-agents/memory) | 平台级记忆 | 会话记忆自动注入 + prompt cache TTL |
| 研究向 | — | [Storage Is Not Memory（检索中心架构）](https://scirate.com/arxiv/2605.04897) · SCM 睡眠整合 · IntentKV 剪枝 |

> 关键新事实：①**Synap 的 LongMemEval 92% / LoCoMo 93.2%** 成为新的"最高公开数字"
> （此前 Trinity 对照的是 Mem0/Zep 口径）；②**"Storage Is Not Memory"** 论文主张
> 检索中心架构（存储≠记忆，召回链路决定质量）——与 Trinity hybrid 5 路 RRF 思路一致；
> ③ Anthropic 把"prompt cache TTL + 会话记忆自动注入"做成平台标配。

## 三、逐维度对比（第二轮核查结果）

| 维度 | Trinity（现状） | 2026 Q3 最优 | 差距 | 本轮判定 |
|---|---|---|---|---|
| 检索质量 | LongMemEval 500q R@5=0.992（本地） | Synap 92%（官方口径） | 口径不同，本地数字更高但**不可对外宣称** | 🟡 待官方基准 |
| 记忆写入 | 原样入库 + 冲突组 + PII 脱敏 | Mem0/Zep LLM 结构化事实抽取 | 提取层仍弱（无 LLM 事实抽取入库） | ⚠️ 有空间 |
| 整合 | 真实 LLM 压缩 78.2% + 睡眠整合链 | Zep 异步 consolidation / Hindsight 反思 | 已对齐 | ✅ |
| 遗忘 | 多因子（时间+访问+重要性）+ 真实 LLM 摘要 | SCM/SleepGate 研究前沿 | 已对齐工程级 | ✅ |
| 时序图谱 | CB46 entity 级 bi-temporal | Graphiti edge 级 valid_from/to | **edge 级时序仍缺** | ⚠️ 有空间 |
| 实体解析 | 归一化 + embedding 去重（11,174→11,151） | Neo4j/Graphiti embedding ER | 已对齐 | ✅ |
| 治理/多代理 | B3 治理层（策略+审计，本轮新增） | Mem0/Zep 多代理隔离 | 领先（多数方案无策略层） | ✅ |
| 安全 | B5 存储加密 + 审计签名 + RBAC（本轮新增） | 企业级（Cognee/Graphiti） | 已对齐 | ✅ |
| 跨模态 | A4 闭环 + image_description 模态（本轮新增） | Mem0 以文本为主 | **领先（文本外模态少有人做）** | ✅ |
| 上下文工程 | 语义缓存 305x · ANN 落盘 | Anthropic prompt cache TTL / IntentKV 剪枝 | **prompt cache 层未接**（LLM 侧，超出记忆层） | 🟡 有空间 |
| 生态 | REST+MCP+DSH+Gateway+联邦 | Mem0 全 SDK（Py/JS/Go） | SDK 生态待扩（LangChain 依赖阻塞） | 🟡 待办 |
| 可观测/社区 | 审计链+签名+leaderboard HTML | dashboards + 开放榜单 | leaderboard 未上线 | 🟡 待办 |

## 四、剩余优化空间（按价值排序）

### A. 基准可信度（最高价值，仍被网络阻塞）
- **现状**：LongMemEval 500q 本地 R@5=0.992 高于 Synap 公开 92%，但口径不同、不可对外宣称。
- **动作**：HF 网络就绪后跑官方 LongMemEval + LoCoMo 2026 共享记分板（[rovemark 协议](https://huggingface.co/datasets/rovemark/locomo-benchmark-results)）拿到同口径数字。
- **收益**：对"最优"最有力的证明。**阻塞中**（HF 不可达，已标记）。

### B. LLM 事实抽取入库（记忆写入层升级）
- **现状**：写入原样入库，无 LLM 结构化事实抽取（对比 Mem0/Zep 写入即抽取实体/事实）。
- **动作**：写路径加可选"LLM 事实抽取"（TRINITY_LLM_* 已配好）：content → 抽取 (subject, predicate, object) → 实体/关系 upsert + 事实记忆（modality=fact）。
- **收益**：图检索/时序查询质量直接提升；对齐 Mem0 graph memory。**未做**。

### C. edge 级 bi-temporal 补全
- **现状**：CB46 entity 级 valid_from/valid_to；edge 级时间戳与"当时的事实"时点查询缺。
- **动作**：relations 表补 valid_from/valid_to + `query_relations_at(time)`。
- **收益**：对齐 Graphiti 最强项。**未做**。

### D. 上下文工程（prompt cache 感知）
- **现状**：语义缓存 305x 在记忆检索层；LLM prompt cache TTL 未接入。
- **动作**：检索结果按会话组织成稳定前缀（prompt-cache 友好）+ 文档化 TTL 策略。
- **收益**：长会话成本直降（Anthropic 平台标配思路）。**未做**，部分超出记忆层边界。

### E. SDK 生态与 leaderboard 上线
- **现状**：Gateway OpenAI 兼容就绪；Python SDK 有雏形；JS/Go SDK 缺；LangChain 依赖未装。
- **动作**：leaderboard 页挂到 dashboard（静态已生成，接路由即可）；SDK 扩 JS。
- **收益**：生态与传播。**部分待做**。

## 五、结论

- **定位升级**：第一轮（round34-43）消化的差距（语义缓存/ANN/真实 LLM 整合/多因子遗忘/实体去重/
  Gateway/融合 6/6）保持有效；第二轮新增的 B3 治理、B5 加密、A4 跨模态让 Trinity 在
  **多代理治理、安全、跨模态**三个维度**超过**网络多数方案。
- **剩余空间排序**：A 官方基准（阻塞）→ B LLM 事实抽取 → C edge 时序 → D prompt cache 感知
  → E SDK/榜单。
- **最有价值的下一轮可落地项**：**B（LLM 事实抽取入库）** —— 无需网络、收益直接体现在
  图检索与 LoCoMo 类基准上，是"深度追平 Synap/Mem0 写入层"的关键一步。

## 六、R2 执行结果（2026-08-15 落地）

| 项 | 状态 | 验证 |
|---|---|---|
| B LLM 事实抽取写路径 | ✅ | `TRINITY_LLM_EXTRACT=on` 时 `client.ingest` 走 `EntityRelationExtractor`
  （LLM 提取实体+关系谓词 → relations 表）；实测 3 实体 + 2 条 `works_on` 语义关系；
  未开启/失败静默回退规则提取；er_extractor 兼容双参/单参 LLM callable |
| C edge 级 bi-temporal | ✅ | `relations` 表补 `valid_from`/`valid_to` 列（幂等迁移+索引）；
  `create_relation` 支持时间窗；新增 `query_relations_at(时点)` 只返回该时点有效边
  （实测：过期边当前不可见、15 天前可见、无 TTL 边默认 now 生效） |
| 单元测试 | ✅ | `tests/unit/test_r2_extract_temporal.py` 10 例（LLM 兼容/写路径/时点/兼容性） |
| demo | ✅ | `scripts/r2_extract_temporal_demo.py` 规则/LLM 双模式 PASS |
| A1 官方基准 | ⏳ | HF 网络不可达，维持阻塞标记 |
