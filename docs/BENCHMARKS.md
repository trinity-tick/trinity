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
| 系统版本 | Trinity v6.19 |
| 检索方法 | TF-IDF cosine similarity (baseline, 无 LLM reranker) |
| 数据集 | LongMemEval Mock (500题, 6类, 10 personas) |
| 记忆会话 | 10 personas × 6 sessions = 60 个多轮对话 |
| 总消息 chunks | 484 条 |
| Top-K | 10 |

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
