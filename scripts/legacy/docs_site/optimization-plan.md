# Trinity 功能汇总 · 业界最优对比 · 优化建议与常用规划（2026-08-14）

> 基于本机实测（pytest 161/6/1、基准产物、EXECUTION.md 第 1-8 轮）与 2026 年公开
> 信息（LongMemEval-S 排行榜、agentmemory / Exabase / Engram / Mem0 / Zep 等）。

---

## 一、Trinity 功能汇总（实测现状）

### 1. 存储层
| 层 | 实现 | 实测 |
|---|---|---|
| 运行时存储（记录源） | SQLite（FTS5 + jieba 中文分词 + WAL） | 11313 条 / active 1449 / 40MB |
| 批处理镜像层 | PostgreSQL（docker trinity-db :5430，VARCHAR id + FK 重建） | 1449 条镜像，幂等同步 |
| 缓存层 | SemanticCache（memory / **redis** 后端，env 开关默认 off） | 8 测试通过；Redis 3.0.504 RESP2 回退 |

### 2. 检索层（核心卖点）
- **47 通道级联检索**（keyword / vector / graph / second_brain / exabase / beamlight 等），
  hybrid 融合 + HNSW ANN；`Trinity.search()` 支持 persona/tenant/agent/session/category 过滤。
- **Second Brain 引擎**：303 个模块（含与 BEAM-ICLR2026、Exabase M-1、Mastra OM、
  Zep/Graphiti、Supermemory、Mem0、RAGAS、ByteRover 对齐的能力块）。
- **50 级 Guardian 链**（安全/合规门禁）+ DCSA 审计（六项指标）+ 遥测（Jaeger OTLP）。

### 3. 生命周期
- **进化系统**：5 相位周期（observe→analyze→plan→execute→certify），已完成 3+ 个完整周期。
- **记忆衰减/分层/压缩**：decay（阈值 0.15）→ tiers（core/recall/archival）→ 压缩
  （**真实 LLM 摘要已就绪**：`--llm real` + DeepSeek，实测调用成功）。
- **同步**：Trinity↔Hermes 双向（sha256 去重 + 近实时 watch）、collector 守护、Marvis。

### 4. 接口与交付
- REST API（:8001，v8.2.0）：限流（令牌桶）+ Prometheus `/metrics` + GraphQL + RBAC + 批量/会话聚合端点。
- MCP Server（stdio + SSE，8 工具全量遥测）；DSH 原生 `mcp__trinity__*` 集成。
- A2A 协议（双智能体端到端演示 19/19）、插件注册表、Windows 服务包装、`trinity-config` CLI 向导。
- 运维：dsh-ops 维护/supervisor/autostart 三件套 + 凭证保险库 + `trinity-maintenance` skill。

### 5. 基准（本机实测，2026-08-14）
| 项 | 分数 | 说明 |
|---|---|---|
| LongMemEval-style 500q | R@5=0.916 / MRR=0.8618 | 社区 mock（对齐官方六类目）；KU/SS-P/TR=1.0，**MS 多会话=0.525 短板** |
| LongMemEval-sim 55q | R@5=0.9818 | 模板模拟集 |
| LoCoMo（38q 子集） | R@5=0.88 / MRR=0.5353 | 官方 1982 题网络不可达 |
| SQuAD v1.1 | R@5=98.3%（177/180） | 双口径统一 |
| BEAM Scale | 1K/10K/100K Recall@5=1.0 | PG FTS 无 GIN 索引，100K P50≈985ms |
| GraphQL 负载 | p50=2.06ms / p99=29.25ms | 100QPS 0 错误 |
| pytest | 161 passed / 6 skipped / 1 failed | 1 失败为外部 Marvis 依赖 |

---

## 二、与 2026 年业界最优方案对比

参考：[LongMemEval-S Leaderboard (JamJet)](https://jamjet.dev/benchmarks/engram-longmemeval/)、
[agentmemory #1 96.2%](https://github.com/JordanMcCann/agentmemory)、
[Exabase 最高分（小模型）](https://www.tmcnet.com/usubmit/2026/05/26/10388514.htm)、
[Mem0 2026 报告](https://mem0.ai/blog/state-of-ai-agent-memory-2026?trk=public_post_comment-text)、
[Agent Memory 生态对比](https://www.innobu.com/en/articles/agent-memory-2026-mem0-letta-zep-hermes-openclaude-comparison.html)、
[Vectorize 8 框架对比](https://vectorize.io/articles/best-ai-agent-memory-systems)、
[MAGMA 多图记忆](https://ar5iv.labs.arxiv.org/html/2601.03236)、
[All-Mem 动态拓扑记忆](https://ar5iv.labs.arxiv.org/html/2603.19595)。

| 方案 | 定位/方法 | LongMemEval-S（官方 500 题） | 对 Trinity 的启示 |
|---|---|---|---|
| **agentmemory** | 轻量检索 + 知识图谱 + 记忆整合，单作者 16 天 | **96.2%（481/500）#1** | 高分不靠堆通道，靠"检索+KG+整合"正确组合；开源可借鉴 |
| **Exabase** | 小模型 + 高召回索引 | 最高分之一（~93%+） | Trinity 已有 CB54 Exabase 对齐块，应验证其在 SS/MS 类目收益 |
| **Engram (JamJet)** | MCP-native、时间事实、冲突检测、混合检索 | 排行榜 T2/T3（见 [JamJet 博客](https://jamjet.dev/blog/engram-longmemeval-tier-2-3/)） | 时间事实 + 冲突检测 = Trinity CB46/CB49 同思路，需落到检索主路径 |
| **Chronos / Mastra OMEGA** | 时序记忆 / 多智能体编排 | 低于 agentmemory | Trinity CB51 ObserverReflector 对齐 Mastra OM，未形成闭环 |
| **Mem0** | 抽取式记忆，生态成熟 | 中等 | Trinity 记忆抽取/ER 已有，差距在**答案生成评测**与产品化 |
| **Zep / Graphiti** | 时序知识图谱（FalkorDB） | 中等 | Trinity kgraph + CB46 对齐，但无时序图可视化/查询语言 |
| **Letta** | 状态化 agent OS（MemGPT 后裔） | — | 记忆即 agent 状态；Trinity 有 agent 层但未做状态化会话 |
| **All-Mem / MAGMA（论文）** | 动态拓扑演化 / 多图记忆 | 研究前沿 | 中长期方向：Trinity 的 graph 检索可向"动态拓扑"演进 |

### 关键差距（诚实评估）
1. **没有官方 LongMemEval-S 答案精度**：Trinity 只测了检索 R@5（0.916），SOTA 榜单比的是
   **答案生成准确率**（agentmemory 96.2%）。检索强 ≠ 答案准；mock 集可补测精度（DeepSeek 可用）。
2. **MS（多会话）类目 0.525 是明确短板**：SOTA 方案靠会话级摘要/情节记忆解决。
3. **PG 检索无索引**：BEAM 100K P50 985ms（全表扫描），加 GIN 索引是低垂果实。
4. **真实 LLM 压缩已就绪未投产**：`--llm real` 默认仍是 mock。
5. **工程健壮性**：聚合池 JSON 曾出现 0 字节截断（需原子写+自愈）。
6. **多智能体记忆共享仍是 demo 级**：A2A 未接真实跨进程 transport。
7. **产品化缺一环**：无租户计费/云原生编排（官方 ROADMAP 的 SaaS/K8s 项未动）。

---

## 三、优化建议（按收益/成本排序）

| # | 建议 | 收益 | 成本 | 状态 |
|---|---|---|---|---|
| 1 | **答案生成评测 harness**：mock 500q 走 DeepSeek 出答案，算 accuracy/latency/cost，对齐 LongMemEval-S 六类目 | 可入排行榜对比，量化一切 | 中 | ✅ 已落地（OPT1，500q 全量见 output/answer_eval_results.json） |
| 2 | **PG FTS 加 GIN 索引**（to_tsvector 表达式索引）并复测 BEAM 100K | 检索延迟预计 10-50× 提升 | 低 | ✅ 10K 实测 P50 6.2×（286→45.9ms）（OPT2） |
| 3 | **MS 多会话短板修复**：会话级摘要记忆 + 检索加权（/memories/session 已有聚合接口） | 0.525 → 目标 0.8+ | 中 | ✅ 根因=排名问题+数据集缺陷；top_k=10 时 MS R@5=0.950；会话扩展检索已实现（OPT3） |
| 4 | **真实 LLM decay 灰度**：维护任务 `--llm real` + DeepSeek key，小批量限流 | 压缩质量真实化 | 低 | ✅ 灰度 20 条→4 摘要+19 归档（OPT4） |
| 5 | **聚合池原子写 + 自愈**（os.replace + 校验和 + 启动校验） | 消除截断/损坏隐患 | 低 | ✅ pid 独立 tmp + fsync + 损坏备份自愈（OPT5） |
| 6 | **Redis 缓存生产开启**并量化命中率收益（benchmark 前后对比） | 检索延迟/TCO | 低 | ✅ supervisor 注入默认 redis；API 级 miss 18.4ms vs hit 10.2ms（OPT6） |
| 7 | **KG 检索贡献分析**：逐类目开关 kgraph/47 通道，找出 SS/MS 提升组合（对齐 agentmemory 方法） | 检索质量提升可解释 | 中 | ✅ 部分：top_k 敏感性+mode 参数装饰性发现（OPT7）；kgraph 逐通道归因待 embedding 引擎可用后补 |
| 8 | **A2A 跨进程落地**：SSE transport + registry 持久化 | 多智能体记忆共享真实可用 | 中 | ✅ HTTP 跨进程 6/6 PASS + sqlite 线程安全修复（OPT8） |
| 9 | **会话状态化**：agent 级会话摘要/续接（Letta 思路） | 长会话体验 | 中高 | ✅ `trinity/daemon/session_state.py`：会话摘要（LLM 落库可检索）+ 续接包，demo 6/6 PASS（第十二轮） |
| 10 | **官方基准补测**：网络恢复后跑 LongMemEval-S/LoCoMo 真集（harness 先备好） | 权威背书 | 外部依赖 | harness 就绪（answer_eval.py，支持 --categories），待网络 |

---

## 四、常用规划（三个里程碑）

### P0 — 可量化冲刺（1-2 周）
1. 答案生成评测 harness（500q × DeepSeek，六类目 accuracy 报告）→ 拿到"可上排行榜"的数字基线。
2. PG FTS GIN 索引 + BEAM 复测（写进 benchmarks.md）。
3. MS 类目修复（session-summary 记忆 + 检索加权），目标 R@5 ≥ 0.8。
4. 聚合池原子写自愈 + 真实 LLM decay 灰度（--llm real + limit 20）。

### P1 — 质量与产品化（1 个月）
5. 复刻 agentmemory 式"检索 + KG + 整合"组合调优（逐类目归因），冲击 mock 500q R@5≥0.95、
   答案 accuracy ≥90%。
6. Redis 缓存生产开启 + 命中率/延迟量化；限流阈值按 profile 调参。
7. A2A SSE 跨进程 + agent registry 持久化；Hermes 双向同步纳入 autostart。
8. Jaeger 面板固化 + 关键 span 告警（错误率、p99）。

### P2 — 长期演进（季度）
9. 网络恢复后跑官方 LongMemEval-S/LoCoMo 真集，更新 README 对比表。
10. 多智能体联邦记忆（A2A 规模化）+ 记忆市场（已有 market 模块雏形）。
11. SaaS 化：租户计费（/metrics + RBAC 已有基础）+ K8s Helm（官方 ROADMAP v6.39/v6.40 对齐）。
12. 研究前沿跟进：动态拓扑记忆（All-Mem）、多图记忆（MAGMA）、时序图查询语言。

---
*本文件为活文档；每完成一项在 EXECUTION.md 追加验证记录并更新此处状态列。*
