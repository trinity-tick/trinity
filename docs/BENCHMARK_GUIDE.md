# Trinity 基准复现指南（Benchmark Guide）

> 价值兑现路径 2（2026-08-26）：所有基准结果带 **Experiment Manifest**（代码/环境/数据集
> 哈希），任何人可复现。本指南说明口径与复现步骤。

## 1. 评测集

| 评测集 | 位置 | 说明 |
|---|---|---|
| mock 500q | `~/.marvis/workspace/conv_*/benchmark/longmemeval_mock_dataset.json` | LongMemEval 风格 6 类目（KU/MS/SS-A/SS-P/SS-U/TR）×80 |
| hard holdout | `trinity/benchmark/private_holdout*.json`（**不入库**）+ `output/hard_holdout.json` | 95 条生产难查询（近义改写 overlap<=40%） |
| 官方 LongMemEval | 需自行下载（`longmemeval_s_cleaned.json` 277MB 在 data/） | 官方集未跑（待办） |

## 2. 复现命令

```bash
# 全量 500q reason（默认 30 候选池）
python benchmark/answer_eval.py --limit 500 --reason --out output/ae_500_reason_NEW.json

# 深度模式（候选 50 + 事件规则）
python benchmark/answer_eval.py --limit 500 --reason --reason-deep --out output/ae_500_reason_deep_NEW.json

# holdout（keyword/pagetree/reason/hybrid 臂）
python benchmark/hard_holdout_eval.py --arms keyword,reason --out output/hard_holdout_eval_NEW.md

# 页树构建（含节点向量）
python scripts/build_memory_pagetree.py

# 实验审阅（对比最近两次）
python scripts/experiment_review.py --latest
```

## 3. 结果与 manifest

- 每次评测保存结果 JSON 时**自动生成** `<name>.json.manifest.json`：
  code_hash（trinity 关键模块聚合哈希）/ env（python/trinity 版本/依赖）/ dataset 哈希 / params；
- `validate_manifest()` 校验：数据集漂移（dataset_changed）→ 拦截（口径漂移不可比）；
  code_changed → 信息性（审阅工具输出"对比跨代码版本，谨慎解读"）；
- 目标引擎 `default_metrics()` 读取前自动校验（漂移结果不会被喂进目标评估）。

## 4. 历史基线（全部可复现，带 manifest）

| 结果 | AnswerAcc | R@5 | 备注 |
|---|---|---|---|
| ae_500_base.json | 0.726 | 0.992 | keyword 基线 |
| ae_500_reason_v3.json | **0.752** | 0.994 | 默认 reason（30 池） |
| ae_500_reason_v6.json | 0.684 | 0.990 | deep 模式 + MS 提示词（**已回滚的负优化样例**） |
| hard_holdout_eval.json | — | — | reason R@10 **0.663**（v6 深度池，目标达标） |
| hard_holdout_eval_v7.json | — | — | 页树向量 R@10 0.200 / hybrid_pt 0.368 |

## 5. 评测纪律（历史教训固化）

1. 生成侧提示词改动**必须全量 A/B**（MS-only 小池无区分度——v6 负优化教训）；
2. 评测集文件被改 → manifest 校验拦截（pagetree.json 损坏教训）；
3. 对比跨代码版本时审阅工具警告（每次评测记录自己的 code_hash）。

---
*价值兑现路径 2 · 2026-08-26*
