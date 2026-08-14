---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 1ed0c9b5065b1819decccf0a8be25f33_be573025918611f1a102525400826444
    ReservedCode1: dMNjPx3K7adXHtlm+36K4143UQaWcZEotYRnNKINgGtitR7sdJT9eUzTGndK72RmEOXHoT3oPnxcLpVBbnQuCiiyScJBIG9SjdsQZvbH2Ds04bYxI9vpnXA3sWFDCWSBORG9fnHIGIOYaYwgM/w8h5HGuNRl3mPY+aQZFt4345UYDUNOb6i23/njEkE=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 1ed0c9b5065b1819decccf0a8be25f33_be573025918611f1a102525400826444
    ReservedCode2: dMNjPx3K7adXHtlm+36K4143UQaWcZEotYRnNKINgGtitR7sdJT9eUzTGndK72RmEOXHoT3oPnxcLpVBbnQuCiiyScJBIG9SjdsQZvbH2Ds04bYxI9vpnXA3sWFDCWSBORG9fnHIGIOYaYwgM/w8h5HGuNRl3mPY+aQZFt4345UYDUNOb6i23/njEkE=
---

# LoCoMo Benchmark 评测报告

**生成时间**: 2026-08-06 17:52:14
**评测集**: `C:\Users\Administrator\trinity\benchmark\locomo_test_set.json`
**问题总数**: 50
**Top-K**: 5
**总耗时**: 0.01s

## 总体指标

| 指标 | 值 |
|------|-----|
| Recall@5 | **0.0600** |
| Precision@5 | **0.0160** |
| MRR | **0.0333** |

## 按类别明细

| 类别 | 题目数 | Recall@K | Precision@K | MRR |
|------|--------|----------|-------------|-----|
| 单会话用户事实 | 14 | 0.0000 | 0.0000 | 0.0000 |
| 单会话助手回复 | 19 | 0.0000 | 0.0000 | 0.0000 |
| 跨会话推理 | 7 | 0.2857 | 0.0857 | 0.1905 |
| 时间线推理 | 4 | 0.0000 | 0.0000 | 0.0000 |
| 知识更新检测 | 3 | 0.3333 | 0.0667 | 0.1111 |
| 偏好追踪 | 3 | 0.0000 | 0.0000 | 0.0000 |

## 题目详情

### 最佳 5 题

| ID | 问题 | 答案 | 类别 | Recall | MRR |
|----|------|------|------|--------|-----|
| q27 | P1 阶段总共完成了哪四个模块？ | 主动预取、自主分页、增量KG更新、联邦记忆查询 | 跨会话推理 | 1.0000 | 1.0000 |
| q15 | 联邦查询模块的 RRF 算法参数 k 当前取值是多少，Recall@5 是多少？ | k=60, Recall@5=0.874 | 跨会话推理 | 1.0000 | 0.3333 |
| q48 | David 负责的衰减算法 bug 是否已修复？ | 已于 8 月 3 日修复 | 知识更新检测 | 1.0000 | 0.3333 |
| q0 | 在季度复盘与规划中，助手回复了什么内容？ | P1 总投入 22 人天，产出：Recall@5 从 0.7 | 单会话助手回复 | 0.0000 | 0.0000 |
| q1 | 在产品路线图讨论中，助手回复了什么内容？ | v6.8 已完成的模块：Federated Memory Q | 单会话助手回复 | 0.0000 | 0.0000 |

### 最差 5 题

| ID | 问题 | 答案 | 类别 | Recall | MRR |
|----|------|------|------|--------|-----|
| q49 | 在客户需求变更处理的对话中，用户问了什么问题？ | 星辰科技的陈总刚打电话来，说他们希望合同增加一个数据导出的定 | 单会话用户事实 | 0.0000 | 0.0000 |
| q47 | 在产品路线图讨论的对话中，用户问了什么问题？ | 需要加资源吗？David 上周不是说内存管理模块的衰减算法有 | 单会话用户事实 | 0.0000 | 0.0000 |
| q46 | 衰减算法的 bug 是什么时候修复的，修复方案是什么？ | 8月3日修复，float32改float64消除累积舍入误差 | 跨会话推理 | 0.0000 | 0.0000 |
| q45 | 在季度复盘与规划中，助手回复了什么内容？ | P1 完成项：P1-1 主动预取模块、P1-2 自主分页模块 | 单会话助手回复 | 0.0000 | 0.0000 |
| q44 | 在产品路线图讨论的对话中，用户问了什么问题？ | P1-2 的自主分页是谁在负责？预计什么时候提测？ | 单会话用户事实 | 0.0000 | 0.0000 |

## 分析

- **最弱类别**: 单会话用户事实 (Recall@5=0.0000) — 建议针对性优化对应记忆检索策略
- **建议**: 整体 Recall 偏低，建议：(1) 增大 RRF k 参数；(2) 增加检索 top_k；(3) 启用 query expansion 多路召回。

---
*报告由 LoCoMoEvaluator (P2-1) 自动生成*
*（内容由AI生成，仅供参考）*
