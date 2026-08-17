# Trinity MemBench 评测报告 v1.0

> **发布时间**：2026-08-14 ｜ **Trinity 版本**：v8.2.0 ｜ **环境**：Windows / 系统 Python 3.14 / API :8001
> 数据规模：引擎库 11,368 记忆（活跃 1,467）｜ 聚合池 10,632 ｜ 图谱 11,009 实体 / 28,043 关系

## 摘要

| 维度 | 指标 | 结果 |
|---|---|---|
| 延迟 | 端到端查询 P50 / P99 | **30-41 ms / 33-49 ms** |
| 吞吐 | 200 并发 QPS | **2,431**（0 错误，内存稳定 ~27MB） |
| 检索质量 | SQuAD v1.1 R@5（180 题） | **98.3%** |
| 长程记忆 | LoCoMo Recall@5（会话聚合） | **0.88**（逐轮写入仅 0.12） |
| 抗谄媚 | MemSyco Composite（LLM judge） | **0.88**（谄媚率 10%） |
| 压缩经济 | 记忆压缩 token 节省 | **约 21%**（预算 2048） |

## 一、延迟与吞吐

### 端到端延迟（run_latency_bench.py，30 样本）

| 项 | P50 | P99 |
|---|---|---|
| E2E trinity_query | 40.99 ms | 48.69 ms |
| 模块瓶颈 CB36_kv_cache | 306.98 ms | 339.02 ms |

### 并发吞吐（concurrency_bench.py，300 req/轮）

| 并发 | QPS | P50 | P95 | P99 | 内存峰值 |
|---|---|---|---|---|---|
| 10 | 200.4 | 46.8ms | 63.1ms | 64.3ms | 27.2MB |
| 50 | 923.1 | 46.4ms | 61.9ms | 62.4ms | 27.3MB |
| 100 | 1,599.1 | 46.5ms | 63.3ms | 75.0ms | 27.3MB |
| 200 | 2,431.3 | 45.3ms | 60.2ms | 74.5ms | 27.3MB |

## 二、检索质量

### SQuAD v1.1（180 题 / 30 文章，本地数据）

| 引擎 | R@5 | 耗时 |
|---|---|---|
| BM25-only（adapter） | 98.3% | 0.89s |
| Trinity keyword（FTS5+jieba） | 98.3% | 1.01s |

### LoCoMo（50 题，真实评测）

| 写入策略 | Recall@5 | 结论 |
|---|---|---|
| A. 逐轮写入 | 0.12 | 记忆碎片化 |
| D. 会话聚合 + 查询扩展 | **0.88** | **推荐**：整段会话聚合为一条记忆 |

## 三、抗谄媚评测（MemSyco，20 题，DeepSeek deepseek-chat）

| 判分方式 | Composite | 谄媚率 | 客观准确率 |
|---|---|---|---|
| 子串启发式（旧） | 0.63 | 5% | 15% |
| **LLM judge（新，推荐）** | **0.88** | 10% | **85%** |

> 说明：真实 LLM 会同义改写措辞，子串匹配严重低估客观准确率；judge 判分更接近真实质量。

## 四、记忆压缩经济学（A5）

- 采样 15 条大记忆（>300 字），预算 2048 token：
  **original_tokens 1,729 → compressed_tokens 1,369（-21%）**，budget_usage 66.9%。

## 五、检索策略对比（A2，10 查询 × 5 策略）

| 策略 | 平均延迟 | 命中率 | 结论 |
|---|---|---|---|
| engine/rrf | 233ms | 100% | ✅ 推荐默认 |
| engine/fusion | 391ms | 100% | 与 rrf 结果相似（Jaccard 0.88） |
| engine/cascade | 275ms | 30% | ⚠️ 勿单独使用 |
| pool/keyword | 2,301ms | 0% | ❌ 不可用 |

## 六、复现命令

```powershell
$py = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
$out = 'C:\Users\Administrator\.trinity\bench-results\<ts>'
# 延迟与并发
& $py benchmark\run_latency_bench.py --output-dir $out
& $py benchmark\concurrency_bench.py --output-dir $out
# 检索质量（本地数据）
& $py benchmark\squad_hybrid_runner.py
& $py benchmark\locomo_real_eval_v2.py --quick
# 抗谄媚（需 TRINITY_LLM_API_KEY=DeepSeek key）
$env:TRINITY_LLM_BASE_URL='https://api.deepseek.com/v1'; $env:TRINITY_LLM_MODEL='deepseek-chat'
& $py benchmark\memsyco_evaluator.py --llm --judge --output-dir $out
# 压缩经济
& $py benchmark\compress_economics.py --samples 15
# 归一化汇总
& $py benchmark\membench_report.py --results-dir $out
```

## 七、已知限制与后续

- 长程一致性（LongMemEval 真实集）与多模态评测待网络/数据就绪后补充
- MemSyco 数据集为 10 场景 20 题的小样本；扩大样本后更新
- 并发与延迟为单机（Windows）结果，跨平台差异待测

---

*报告生成：benchmark/membench_report.py 归一化 + 各套件原始产物（.trinity/bench-results/20260814_v2baseline/）*
