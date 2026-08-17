# Trinity 优化方向分析（2026-08-15）

> 依据：MEMBENCH_REPORT v1.0（2026-08-14）+ 2026-08-15 三轮运维梳理（EXECUTION 34.x）
> + 业界 2026 记忆系统公开方案对照。文中"网络方案"引用均附来源链接。

## 一、现状基线（真实实测）

| 维度 | 指标 | 结果 |
|---|---|---|
| 延迟 | E2E 查询 P50 / P99 | 41ms / 49ms |
| 模块瓶颈 | CB36_kv_cache | **307ms（全模块最慢，重点排查）** |
| 吞吐 | 200 并发 QPS | 2,431（内存稳定 ~27MB） |
| 检索质量 | SQuAD R@5 | 98.3%（BM25/FTS5，题目偏易） |
| 长程记忆 | LoCoMo Recall@5 | 0.88（会话聚合写入） |
| 抗幻觉 | MemSyco（LLM judge） | 0.88 |
| 压缩经济 | 记忆压缩 | ~21% token 节省 |
| 规模 | 大库 11.7k / 图 11.2k 实体 28.3k 关系 / 122 模块 50 守护 47 通道 | v8.2.0 |

## 二、与业界 2026 方案的差距（按优先级）

### P0-1 检索链路延迟与缓存未接线
- **业界**：Repeated KV cache / IntentKV 跨轮剪枝 / 4-bit KV 量化、语义缓存（
  [Baseten Repeated KV](https://www.baseten.co/research/repeated-kv-cache-for-long-running-agents/)、
  [IntentKV](https://arxiv-org.ezproxy.obspm.fr/html/2606.09916v1)、[UltraQuant](https://arxiv-org.ezproxy.obspm.fr/html/2606.20474v2)）。
- **Trinity**：`TRINITY_CACHE_BACKEND=redis` 已配置（supervisor 注入）但 **client.py search 主路径未见缓存接入**；
  `engine/rrf` 融合平均 233ms vs E2E 41ms——融合是热点；CB36_kv_cache 模块 profiling 307ms。
- **动作**：①给 hybrid search 加 Redis 语义缓存（query 嵌入命中直接返回）；②RRF 融合改用并行/分批；
  ③定位 CB36 307ms 来源（首次 miss 构建 or 每查询重建）。
- 收益：p95 延迟降 50%+；成本：小；风险：低。

### P0-2 记忆衰减/整合仍是"玩具级"（mock LLM + 单阈值）
- **业界**：睡眠式整合（[SCM: Sleep-Consolidated Memory](https://ar5iv.labs.arxiv.org/html/2604.20943)）、
  定时遗忘 pass（[SleepGate](https://github.com/bug-ops/zeph/issues/2397)）、多因子算法遗忘
  （[Novel Memory Forgetting](https://ar5iv.labs.arxiv.org/html/2604.02280)、
  [cognitive-memory](https://github.com/NP-compete/cognitive-memory)）。
- **Trinity**：decay 用 mock LLM（离线抽取式摘要）+ 单阈值 0.15 + 每次仅 100 条；
  已接入 daily 链（mirror,decay,tiers,sync，Option A 直治大库），但治理强度弱。
- **动作**：①接真实 LLM 摘要（`create_llm_compress_callable` 已存在，配 TRINITY_LLM_API_KEY）；
  ②多因子遗忘分数（时间+访问+重要性+干扰）替换单阈值；③每晚"睡眠整合"多阶段
  （提取→冲突消解→压缩→归档→图更新→衰减报告）。
- 收益：长程记忆质量（LoCoMo）与存储健康；成本：中；风险：中（需灰度）。

### P0-3 实体解析仍为启发式（11k 实体未去重）
- **业界**：embedding-based entity resolution + 增量合并（
  [Neo4j agent-memory](https://github.com/neo4j-labs/agent-memory)、
  [Entity Resolution & Dedup](https://deepwiki.com/neo4j-labs/agent-memory/3.3.2-entity-resolution-and-deduplication)）；
  Graphiti 的实体去重是检索质量关键（[Zep/Graphiti](https://www.ycombinator.com/launches/Lmc-graphiti-by-zep-ai-a-library-for-building-dynamic-knowledge-graphs)）。
- **Trinity**：upsert_entity 按 name 精确匹配，无 embedding 相似合并——同一实体多别名会分裂。
- **动作**：实体写入时做 embedding 相似度去重（>阈值则合并/加别名），
  增量 batch 去重任务；补 entity merge 端点。
- 收益：图检索/时序查询质量；成本：中；风险：低（可只对新增实体生效）。

### P1-1 时间知识图谱（bi-temporal）补全
- **业界**：Graphiti 的 edge 级 valid_from/valid_to + 增量更新是 2026 标配。
- **Trinity**：CB46 TemporalValidity 已有雏形（entity 级 bi-temporal），
  但 edge 级时序、时点查询、实体合并路径不完整。
- **动作**：补 edge 级时间戳 + `query_at_time` 覆盖边；entity merge 时合并时间线。
- 收益：时序一致性（"当时的事实"）；成本：中。

### P1-2 上下文工程层（KV 感知检索）
- **业界**：[Dynamic Long Context Reasoning over Compressed Memory (ACL2026)](https://aclanthology.org/2026.acl-long.365/)
  证明"记忆压缩后 RL 长程推理"是可行方向；KV 量化/剪枝直接降成本。
- **Trinity**：有 CB36 kv_cache 模块（bounded_exact）但未做剪枝/量化；压缩 21% 保守。
- **动作**：对接 IntentKV 式跨轮剪枝思路（只对长会话启用）；压缩策略参数化（token 预算 vs 质量曲线，A5 已规划）。
- 收益：长会话成本；成本：中。

### P1-3 可验证记忆与合规（出海/企业）
- **业界**：[verifiable-memory](https://github.com/Mars-proj/verifiable-memory)、
  [SAIHM 协议](https://www.ietf.org/archive/id/draft-saihm-memory-protocol-01.html)、
  [GDPR for AI Agents](https://atlan.com/know/ai-agent/gdpr-compliance-for-ai-agents/)、
  [出海三层记忆隔离](https://www.freebuf.com/articles/database/491970.html)。
- **Trinity**：已有 Ed25519/x509 签名 + DCSA 审计链 + export/forget 端点——基础在，
  缺合规手册、一键 GDPR 数据出境、跨租户隔离审计（B5 已在 roadmap）。
- **动作**：补合规手册 + 对账脚本（audit ↔ memories）；把签名做"可证明记忆"对外能力。
- 收益：企业/出海场景准入；成本：中。

### P2 基准与生态（对齐 roadmap A1/C3）
- **业界**：LoCoMo 2026 新口径（[AriadneMem](https://ar5iv.labs.arxiv.org/html/2603.03290)）、
  [AgentMemBench](https://arxiv-org.ezproxy.obspm.fr/html/2608.00009v1)、
  [LoCoMo 三方对比](https://huggingface.co/datasets/rovemark/locomo-benchmark-results/commit/37fc33a31faa81e57b9547182518df0c4e73c2cd)。
- **Trinity**：自建 MemBench（SQuAD 98.3% 偏易题、LoCoMo 0.88 尚可），未接外部公开基准可比口径。
- **动作**：跑 AgentMemBench/新版 LoCoMo 以对齐可公开宣称数字；leaderboard 上线（A1.6）。
- 收益：可信度/传播；成本：中。

## 三、运维/工程短板（前三轮梳理新增）

1. **三库拓扑**：SQLite 大库（运行时权威）/ docker PG 5430（维护库）/ 原生 PG 5432（遗留）。
   Option A 已让 decay/tiers 直治大库；PG 层仅剩 mirror 供分析——建议明确"PG 只读分析层"定位，停用或归档遗留实例。
2. **双通道语义不一致**：原生 trinity_search 默认按会话身份隔离（空结果），MCP search 全局——
   需文档化并可选统一（`scope` 参数）。
3. **collector 零事件**：扫描 agent 缓存目录，15h+ 零采集——事件源未配置或链路闲置，建议确认是否启用。
4. **仓库卫生**：大量未提交历史改动 + 未跟踪目录（backup/、federation/、gateway/ 等）；
   `engine_core.py.som_bak` 已删；建议分批审阅提交。
5. **测试基线**：135 passed/33 skipped；engine_core 重构后 diagnostics 曾坏（已修）——
   建议给 run_diagnostics 加 CI 冒烟，防重构回归。

## 四、执行进度（2026-08-15，round35）
| 项 | 状态 | 说明 |
|---|---|---|
| P0-1 检索链路 | ✅ | 语义缓存已在 retriever 层（env-gated，Redis 实测 305x）；修复缓存 key 隔离 scope + **get_memory_owners 隔离漏洞**（agent/persona/tenant 过滤此前完全失效）；CB36 307ms 确认系 profiler stub 模拟值 |
| P0-2a 真实 LLM | ✅ | maintenance 注入 DEEPSEEK_API_KEY → TRINITY_LLM_*；真实摘要实测优于 mock |
| P0-2b 多因子遗忘 | ✅ | access boost + recency 保护（旧记忆最近访问过 → 不归档） |
| P0-2c 睡眠整合 | ✅ | sleep_consolidation.py：decay→LLM 事实提取→图更新；接入每日链 |
| P0-3 实体去重 | ✅ | entity_dedup.py：归一化 33 合并 + embedding 相似；11,174→11,141 |
| P1-2 压缩参数化 | ✅ | TRINITY_LLM_MAX_TOKENS 可调 |
| P1-1 edge bi-temporal | ⏳ 下一轮 | |
| P1-3 合规 | 🟡 | gdpr_export.py 已建；合规手册文档待补 |
| P2 公开基准 | ⏳ 下一轮 | LongMemEval/BEAM/LoCoMo 同口径对齐 |

## 五、建议的下一步（P0 试点）

| # | 任务 | 依据 | 预估 |
|---|---|---|---|
| 1 | hybrid search 接 Redis 语义缓存 + RRF 并行 | P0-1 | 0.5-1d |
| 2 | CB36_kv_cache 307ms 定位修复 | P0-1 | 0.5d |
| 3 | decay 接真实 LLM + 多因子遗忘分（灰度 DecayLLM=real） | P0-2 | 1-2d |
| 4 | 实体 embedding 去重（新增实体生效） | P0-3 | 1-2d |
| 5 | 双通道 search scope 参数统一 | 运维 2 | 0.5d |

优先级：1-2 降延迟（量化可见）→ 3 提治理 → 4 提质量 → 5 一致性。
