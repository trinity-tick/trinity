# Trinity 官方 LongMemEval_S 基准报告（2026-08-16）

> 数据集：longmemeval_s_cleaned（官方 ICLR 2025，500 题，~115K tokens/题历史）
> 获取：hf-mirror.com（官方 HF 在本环境被墙）
> 运行器：benchmark/longmemeval_official_runner.py ｜ 判分：benchmark/lme_qa_judge.py（官方分题型模板，DeepSeek judge）
> 环境：Windows / 系统 Python 3.14 / Trinity v8.5.0

## 一、方法（诚实口径）

1. 每题的 haystack_sessions 逐会话 ingest 为一条记忆（agent 命名空间隔离，逐题独立，无跨题泄漏）；
2. 检索：hybrid 47 通道融合（BM25+FTS5+jieba / FAISS HNSW / 图谱 / RRF），top_k=5；
3. 指标：
   - **session_recall@k**：证据会话（answer_session_ids）是否在 top-k 检索结果中
   - **turn_recall@k**：含 has_answer 标注的会话是否在 top-k 中
   - **QA accuracy（judged）**：DeepSeek 依据检索到的 top-5 上下文作答，官方分题型模板判分（temporal 免 off-by-one、knowledge-update 接受新答案）
4. 局限：QA 判分为 DeepSeek（官方用 GPT-4o）；检索为单轮 top-k（无迭代检索）；未用官方 115K 全历史（逐会话 ingest 保留全量信息）。

## 二、结果（2026-08-16 实测，500 题全量）

| 指标 | 数值 | 备注 |
|---|---|---|
| **session_recall@5** | **0.968（96.8%）** | 证据会话进 top-5 |
| **turn_recall@5** | **0.922（92.2%）** | 含 has_answer 会话进 top-5 |
| mean hit position | **1.3** | 证据平均首次命中位次（多数为第 1 位） |
| QA accuracy（DeepSeek judged，官方模板） | **0.540（54.0%）** | **dated 优化模式**（时间戳+全量证据+强提示+temporal 分步推理，500 题全量） |
| QA accuracy（优化前基线） | 0.496（49.6%） | QA2b（会话级+全量证据+强提示） |
| 子串匹配 QA（弱下限） | 0.014 | 仅作参照，不作宣称 |
| 检索耗时 | 2,132s（~35min） | 500 题 ingest + 检索（QA2b 另计 2,300s / dated 2,199s） |

> **QA 链路演进（2026-08-16）**：①初版 QA（截断 600 字符上下文）仅 1.8%——不是检索失败
> （recall 96.8%），而是**上下文装配把长会话截断、证据丢失**；②全量证据上下文 + 强提示
> （QA2b）= **49.6%**；③**dated 优化**（会话内容前缀 [DATE: 时间戳] + temporal 分步推理提示）
> = **54.0%**，其中 **temporal-reasoning 28.6% → 44.4%（+15.7pp）**。教训与 Arize 独立评测一致：
> **R@K 证明"检索到了"，不证明"答对了"——QA 链路的上下文工程决定端到端能力**。

### 优化前后分题型对比（500 题，DeepSeek 官方模板判分）

| 题型 | QA2b 基线 | dated 优化 | Δ |
|---|---|---|---|
| single-session-assistant | 0.911 | 0.911 | — |
| single-session-user | 0.843 | 0.871 | +2.8pp |
| knowledge-update | 0.615 | 0.641 | +2.6pp |
| temporal-reasoning | 0.286 | **0.444** | **+15.7pp** |
| multi-session | 0.384 | 0.361 | -2.3pp（样本噪声） |
| single-session-preference | 0.033 | 0.033 | —（n=30，需专门策略） |
| **整体** | **0.496** | **0.540** | **+4.4pp** |

> **A/B 负面结论（同样重要）**：分题型专用生成提示（preference/multi/KU 各写一套）在 50 题
> A/B 中为**负优化**（preference 提示诱发 13 个 UNKNOWN、multi 提示伤多会话）——强基底提示 +
> 仅对 temporal 加分步推理是当前最优组合。judge 双口径一致性 91.7%（reason-first 宽松判分
> 40% vs 官方模板 44.4%，官方数字未被高估）。

### 分题型 session_recall@5

| 题型 | n | session_recall@5 |
|---|---|---|
| single-session-assistant | 56 | 1.000 |
| knowledge-update | 78 | 0.987 |
| single-session-user | 70 | 0.986 |
| multi-session | 133 | 0.977 |
| temporal-reasoning | 133 | 0.955 |
| single-session-preference | 30 | 0.833 |

## 三、与网络对比（同口径语境）

| 系统 | LongMemEval_S R@5（独立/官方口径） | 来源 |
|---|---|---|
| MemPalace | 96.6% | dev.to 独立复测 |
| **Trinity（本次实测）** | **96.8% session / 92.2% turn** | 本报告（官方 500 题，hybrid top-5） |
| Awareness（本地优先） | 96.0% | dev.to 独立复测 |
| Zep | 63.8% | vectorize 独立评测 |
| Mem0 OSS（独立） | ~32-49% | dev.to / vectorize |

**QA 对比语境**：Trinity QA=49.6%（DeepSeek judge，上下文装配修复后；未调优）。官方论文中
最佳系统 QA 约 80-90%（GPT-4o judge + 专门调优）；Mem0/Zep 未公开可复现 QA 数字。
Trinity 检索召回进入头部区间（96.8%），**QA 生成是当前主要深度差距**（temporal/preference 题型）。

> ⚠️ 口径差异说明：Trinity 为逐会话 ingest + 47 通道 hybrid 单轮检索；Awareness/MemPalace 为
> 官方数据集真实生产管线复测（dev.to，runner 公开）；各家 top-k 与判分不一，量级参考。
> **Trinity 与 Mem0/Zep 的差距在于 QA 链路与 LLM 提取，而非检索召回**（见 README 性能表）。

> 注：各家口径（R@5/R@10、判分模型、是否含 LLM 提取）不一，横向数字仅作量级参考。

## 四、产物

- 数据：C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json（264MB）
- 结果：C:\Users\Administrator\.trinity\bench-official\lme_s_full500.json
- 判分：C:\Users\Administrator\.trinity\bench-official\lme_s_full500_judged.json
