# Trinity Leaderboard（2026-08-15）

> 生成时间：2026-08-15T05:17:02.274240+00:00；版本 v8.2.0；环境 Windows / Python 3.14 / API :8001

## 一、BEAM 规模延迟（本地模拟 50 查询）

| 规模 | 记忆数 | QPS | P50 | P95 | P99 | Mean | Recall@5 |
|---|---|---|---|---|---|---|---|
| 1K | 1029 | 100.01 | 8.65ms | 13.7ms | 34.27ms | 9.97ms | 1.0 |
| 10K | 10000 | 4.12 | 240.0ms | 273.85ms | 291.56ms | 242.47ms | 1.0 |
| 100K | 110000 | 0.99 | 984.6ms | 1224.79ms | 1337.35ms | 1005.77ms | 1.0 |

## 二、LoCoMo 长程召回（50 题真实评测，B.session-aggregate 代表配置）

| 指标 | 值 |
|---|---|
| Recall@5 | 0.88 |
| MRR | 0.5633 |
| Precision@5 | 0.184 |

### 按类别


## 三、MemBench v1.0 核心指标（2026-08-14 实测，来源 benchmark/MEMBENCH_REPORT.md）

| 维度 | 指标 | 结果 |
|---|---|---|
| 延迟 | E2E P50 / P99 | 41ms / 49ms |
| 吞吐 | 200 并发 QPS | 2,431（内存稳定 ~27MB） |
| 检索质量 | SQuAD R@5（80 题） | 98.3% |
| 长程记忆 | LoCoMo Recall@5（会话聚合） | 0.88 |
| 抗幻觉 | MemSyco Composite（LLM judge） | 0.88（幻觉率 10%） |
| 压缩经济 | 记忆压缩 token 节省 | ~21% |
| 规模 | 大库 / 图 | 11.7k 记忆 / 11.1k 实体 / 28.3k 关系 |

## 四、口径说明

- BEAM 为本地 1K/10K/100K 模拟规模（beam_gin_index），非官方 BEAM 10M token 口径；
  Hindsight/Exabase 在官方 BEAM 上的 SOTA（64.1%）为不同基准，不可直接比较。
- LoCoMo 为中文 50 题本地集（locomo_test_set.json），与公开 LoCoMo 英文集口径不同。
- 如需对外宣称，建议后续跑官方 LongMemEval / BEAM 同口径（P2 待办）。
