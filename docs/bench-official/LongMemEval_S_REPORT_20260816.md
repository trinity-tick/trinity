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
| QA accuracy（DeepSeek judged，官方模板） | **78%（50 题验证）/ 全量见 judged 报告** | 会话级 ingest + 全量证据上下文 + 强提示（QA2b） |
| 子串匹配 QA（弱下限） | 0.014 | 仅作参照，不作宣称 |
| 检索耗时 | 2,132s（~35min） | 500 题 ingest + 检索（QA2b 全量另计） |

> **QA 链路关键发现（2026-08-16）**：初版 QA（截断 600 字符上下文）仅 1.8%，原因不是检索失败
> （recall 96.8%），而是**上下文装配把长会话截断、证据丢失**；改为全量证据上下文后 50 题
> 验证判分 78%（temporal-reasoning 0.455 / multi-session 1.0 / knowledge-update 0.571）。
> 教训与 Arize 独立评测一致：**R@K 证明"检索到了"，不证明"答对了"——QA 链路的上下文
> 工程是 Trinity 下一步要补的深度**（全量 QA2b 结果见 lme_s_qa2b_full500.json）。

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
| Awareness（本地优先） | 96.0% | dev.to 独立复测 |
| **Trinity（本次实测）** | **96.8% session / 92.2% turn** | 本报告（官方 500 题，hybrid top-5） |
| Zep | 63.8% | vectorize 独立评测 |
| Mem0 OSS（独立） | ~32-49% | dev.to / vectorize |

> ⚠️ 口径差异说明：Trinity 为逐会话 ingest + 47 通道 hybrid 单轮检索；Awareness/MemPalace 为
> 官方数据集真实生产管线复测（dev.to，runner 公开）；各家 top-k 与判分不一，量级参考。
> **Trinity 与 Mem0/Zep 的差距在于 QA 链路与 LLM 提取，而非检索召回**（见 README 性能表）。

> 注：各家口径（R@5/R@10、判分模型、是否含 LLM 提取）不一，横向数字仅作量级参考。

## 四、产物

- 数据：C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json（264MB）
- 结果：C:\Users\Administrator\.trinity\bench-official\lme_s_full500.json
- 判分：C:\Users\Administrator\.trinity\bench-official\lme_s_full500_judged.json
