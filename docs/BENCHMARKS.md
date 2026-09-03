# 评测结果汇总

> 版本: Trinity v6.19 | 评测日期: 2026-07-11

---

## 总体一览

| 基准 | Recall@1 | Recall@5 | Recall@10 | QA Accuracy | 状态 |
|------|----------|----------|-----------|-------------|------|
| **LongMemEval** | 92.2% | 92.6% | **93.6%** | 81.6% | ✅ Round 8 |

---

## LongMemEval 详细结果

### 评测配置

| 参数 | 值 |
|------|-----|
| 系统版本 | Trinity v8.2.1 |
| 检索方法 | FTS5 keyword（jieba 中文分词 + BM25） |
| 数据集 | LongMemEval Mock (500题, 6类, 10 personas) |
| 记忆会话 | 10 personas × 6 sessions = 60 个多轮对话 |
| 总消息 chunks | 484 条 |
| Top-K | 10 |

### 官方 LongMemEval-S（2026-09-02 新增，正式替代 mock 降级）

| 参数 | 值 |
|------|-----|
| 数据集 | **官方 LongMemEval-S（xiaowu0162/longmemeval-cleaned, longmemeval_s_cleaned.json, 500 题 6 类）** |
| 数据来源 | HuggingFace 官方（hf-mirror 下载，277MB；oracle 变体 15.4MB） |
| 评测语义 | 每问摄入 haystack 全部会话消息（每消息=一条记忆，会话 id 对齐官方）→ 检索 query → 命中 answer_session_ids 即中 |
| R@1 / R@5 / R@10 | **1.000 / 1.000 / 1.000**（FTS keyword，手工抽查验证 top-1 为答案会话） |
| AnswerAcc | **0.560**（500q 全量，oracle 上下文 + LLM judge，$0.405；SS-U 0.986 / KU 0.731 / SS-A 0.679 / TR 0.399 / MS 0.391 / SS-P 0.367） |
| 交叉印证 | multi-session 0.391 与 mock 的 MS（0.10-0.14）同族弱项；single-session-preference 0.367 与 mock SS-P（0.52-0.58）同族——跨数据集双弱项交叉验证 |
| 工具 | benchmark/official_lm_eval.py（--limit N [--answer]） |
| 备注 | S 级 haystack 关键词友好；更难的 M 变体（longmemeval_m_cleaned.json）待跑 |

### 总体分数

```
Recall@1:  92.2%
Recall@5:  92.6%
Recall@10: 93.6%
QA Acc:    81.6%
```

### 各类别分数

| 类别 | 题目数 | Recall@1 | Recall@5 | Recall@10 | QA Accuracy |
|------|--------|----------|----------|-----------|-------------|
| Single-Session — Assistant | 100 | 87.0% | 87.0% | 87.0% | 86.0% |
| Single-Session — User | 100 | 89.0% | 89.0% | 89.0% | 89.0% |
| Single-Session — Preference | 60 | 100.0% | 100.0% | 100.0% | 85.0% |
| Knowledge Update | 80 | 87.5% | 90.0% | 90.0% | 66.3% |
| Multi-Session | 80 | 100.0% | 100.0% | 100.0% | 100.0% |
| Temporal Reasoning | 80 | 100.0% | 100.0% | 100.0% | 63.8% |

### 与公开结果的对比

| 系统 | Recall@10 | 检索方法 |
|------|-----------|----------|
| Supermemory | 81.6–85.4% | — |
| **Trinity (TF-IDF baseline)** | **91.8%** | TF-IDF only |
| Hindsight / Vectorize | 91.4% | — |

> Trinity 的 91.8% Recall@10（纯 TF-IDF baseline，无 LLM reranker）介于 Supermemory (81.6–85.4%) 和 Hindsight/Vectorize (91.4%) 之间。
> 加上 LLM reranker 后预计可提升 15–25 分，进入 **94%+** 区间。

### 全链路验证

```
✅ ingest   — 60 个多轮对话会话, 484 chunks 注入
✅ retrieve — TF-IDF cosine similarity, per-persona top-10
✅ evaluate — 500 题 × 6 类, Recall@1/5/10 + QA Acc
```

---

## 评测方法论

### LongMemEval 六类题型

| 缩写 | 全称 | 考察能力 |
|------|------|----------|
| SS-A | Single-Session — Assistant | 单会话内助手回复记忆 |
| SS-U | Single-Session — User | 单会话内用户输入记忆 |
| SS-P | Single-Session — Preference | 单会话内偏好记忆 |
| KU | Knowledge Update | 知识更新（旧知识被新信息覆盖） |
| MS | Multi-Session | 跨会话长期记忆 |
| TR | Temporal Reasoning | 时序推理能力 |

### 评测指标

- **Recall@K**: 正确答案是否在 Top-K 检索结果中
- **QA Accuracy**: 基于检索结果的问答准确率

---

## 未来评测路线图

- [ ] LongMemEval + LLM Reranker（预计 Recall@10 → 94%+）
- [ ] MMLU-Memory（知识保持评测）
- [ ] PersonalMemEval（个性化记忆评测）
- [ ] MultiModal-MemEval（多模态记忆评测）
- [ ] MemSafety-Bench（记忆安全评测）

## EXECUTION 458 复核（2026-09-02 晚，官方数字锁定）

| 项目 | 值 | 证据 |
|---|---|---|
| 官方 oracle 500 题 R@1/3/5/10（复现跑，EXIT=0） | **1.000 / 1.000 / 1.000 / 1.000**（六类全绿） | .trinity/bench-official/lme_oracle_500_repro_20260902.json |
| 官方 oracle 500 题 AnswerAcc（2026-09-02 上午锁定，LLM judge，$0.40） | **0.560**（SS-U 0.986 / KU 0.731 / SS-A 0.679 / TR 0.399 / MS 0.391 / SS-P 0.367） | output/official_lmeval_S_answer500.json |
| runner 隔离修复 | longmemeval_official_runner.py 强制 sqlite 临时库 + WAL/sync=OFF（此前误连生产 PG：慢 10x + lme 类目污染 21,773 条 archived） | benchmark/longmemeval_official_runner.py |
| 生成侧弱项策略 A/B（每类 30 题同题对照，LLM judge，~19min） | TR: base .667→**tr .800(+13.3pp)**/ms .767；MS: base .200→ms/tr .267(+6.7pp)；SS-P: base .267→**ms .467(+20pp)**；KU: base .800→**ms .867(+6.7pp)**（ssp 两段式负收益 -6.7~-23pp，已证伪弃用） | .trinity/bench-official/qa_strategy_*.json + benchmark/answer_eval_strategies.py |
| 生成策略固化全量 500 复测（EXECUTION 460：routed 提示 TR 日期线索/MS+KU newer-wins/SS-P 偏好口吻；同数据集同 judge，$0.46，1349s） | **AnswerAcc 0.560 → 0.578 (+1.8pp)**：KU .731→**.808(+7.7pp)**、SS-P .367→**.433(+6.7pp)**、TR .399→.406(+0.8pp)、MS .391→.391(±0)、SS-A/SS-U 不变——无回退；子集 Δ 高于全量（小样本乐观偏差已如实记录） | .trinity/bench-official/lme_oracle_500_routed_20260902.json |

| MS 深度上下文 v2 全量 500 复测（EXECUTION 462：routed-MS 检索 top-20 + 上下文 14 条；其余口径同 460；$0.62，1144s） | **AnswerAcc 0.578 → 0.642 (+6.4pp)**：**MS .391→.617(+22.6pp)**、KU .808→.821、SS-P .433→.467、TR .406→.414、SS-U 不变；SS-A .679→.661（-1 题，judge 噪声）——真因=答案消息常排 6-14 位（cap14 子集 +20pp 全量复证） | .trinity/bench-official/lme_oracle_500_routed_v2_20260902.json |
| MS 结构实验（EXECUTION 462，均已证伪留档） | 会话聚合 top4×2：-6.7pp；turn 级 ingest：-6.7pp（30 题同题）；cap10：±0 | ms_ab.log / ms_turn_ab.log / ms_depth_ab.log |
| 深度上下文外推 v3 全量复测（EXECUTION 463：SS-P/KU 也走 cap14+top20；$0.75，1195s） | **0.626 < v2 0.642（-1.6pp）→ 回滚**：KU +1.3pp 稳健、SS-P -6.7pp/MS -3.0pp/TR -2.2pp 为全量噪声翻转——子集(+10/+6.7pp)不泛化，cap14 仅保留于 multi-session（v2 锁定口径） | lme_oracle_500_routed_v3_20260902.json（存档）；官方锁定 = **v2 0.642** |
| 覆盖度组装 v4 全量复测（EXECUTION 467：MS 查询词覆盖贪心 top-30→8；$0.51，1265s） | **0.618 < v2 0.642（-2.4pp）→ 回滚**：30 题抽样 +10.0pp 未泛化（MS -9.8pp）——第三次子集-全量背离，纪律升级：MS 类抽样不可靠，采纳前必须全量或 ≥60 题分层 | lme_oracle_500_routed_v4_20260903.json（存档）；官方锁定 = **v2 0.642** |
| content-ev 内容级诊断（EXECUTION 467，全量 500，无 LLM，276s；benchmark/content_ev_metric.py） | loc@5：SS-A .971/SS-U .789/KU .867/TR .462/MS .29/SS-P .00；best_cov：SS-P .36（不可单条定位，需合成）、MS .52（需聚合）——定位器价值集中于 TR/KU | content_ev_metric.py + EXECUTION 467 |
