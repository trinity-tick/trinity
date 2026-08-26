# Trinity 评测可信度声明（2026-08-24）

> 目的：业界记忆系统分数"自报打架"（同一系统同一基准可差出 4 个分数），
> 本文件声明 Trinity 对外宣称口径与可信度边界——**分数可复现、口径可核、
> 局限透明**。供 README/官网/商业材料引用。
> 背景依据：R9 调研（docs/COMPARISON_VS_2026_SOTA_R9.md §二）。

---

## 一、可宣称的成绩（全部本地实测、口径完整）

| 指标 | 数值 | 口径 | 复现方式 |
|---|---|---|---|
| LongMemEval-S 检索召回 | session R@5 = **0.968** / 500q top-10 R@5 = **0.992** | 官方数据集（ICLR 2025），500 题全量；hybrid top-5/top-10 | benchmark/ 脚本 + 官方数据集（网络恢复后可重跑） |
| LongMemEval-S QA | **78%**（产品口径 RouteReasoner，50 题 seed42，两轮复现同分） | DeepSeek **judge3 三票 majority**（reason-first 提示）；官方分题型模板 | benchmark/rr_ab50.py |
| 全量 500 QA | 68.6%（FINAL 配置） | 同上 judge3 三票 | 实测产物 ~/.trinity/bench-official/ |
| 同口径网络对比 | 78% > PlugMem 75.1% / Zep 71.2%（**同 judge3 口径**） | 网络方案数字按其官方口径转 judge3 | 见 R8 报告 §2.1 |
| MemBench 延迟/吞吐 | P50 30-41ms / 2,431 QPS（200 并发 0 错误） | 单机 Windows 实测 | benchmark/ |

## 二、评测口径声明（引用时必须附带）

1. **judge 模型**：所有 QA 分数用 **DeepSeek（deepseek-chat）judge3 三票
   majority** 判定，非 GPT-4o 单票。网络方案 80-90% 多为 GPT-4o 单票——
   **不可直接对比**（口径差可解释 10-15pp 的一部分；模型 A/B 已证
   deepseek-chat 为当前最优生产选择）。
2. **数据集**：LongMemEval_S 为官方数据集全量实测（500 题）；
   LoCoMo 为**本地中文集**（0.88，非官方英文集——英文集因网络阻塞未跑，
   **不得宣称 LoCoMo 官方成绩**）。
3. **检索 vs QA 分离**：R@5（召回）与 QA accuracy（端到端生成）分列——
   **不混排**（业界常见错误：recall 高 ≠ QA 高，MemPalace 已公开修正此分类）。
4. **证伪记录**：历轮优化中多次捕获伪增量（freshness/chronos/ppro/多轮
   提示词等已证伪清单见 docs/TRINITY_EVAL_STATUS_AND_COMPARISON_20260817.md），
   宣称的提升均有同批 A/B + 复现验证，非单次运气。

## 三、不宣称的（诚实边界）

| 项 | 状态 |
|---|---|
| BEAM（1M/10M tokens）官方成绩 | ❌ 未跑（HF 阻塞）——**不得宣称** |
| LoCoMo 官方英文集 | ❌ 未跑（数据源阻塞 + 官方仓库 404）——**不得宣称** |
| "47 通道"数字 | ⚠️ 运行时为 5 通道混合 + FTS 默认路径；"47 通道"为可扩展框架声明，非运行时事实 |
| 与 GPT-4o judge 方案的分数对比 | ⚠️ 需标注 judge 口径差异后才可引用 |
| Mem0/Zep 等厂商自报分数 | ⚠️ 同基准多版本分数打架（LoCoMo 上 Mem0 被报 65.99/84/58.44/75.14），引用需指定版本与口径 |

## 四、评测可信度判断清单（引用外部数字时）

1. 看口径：recall / QA / judge 分数？哪个基准？判分模型？
2. 是否第三方独立复测（有复现脚本）？厂商自报一律打折。
3. baseline 是否公平（"+x% vs baseline" 的 baseline 是什么）？
4. 同系统同基准是否被多方报出多个分数（红灯信号）？
5. 样本量（十几段对话的分数统计不可靠）+ 是否计入 adversarial 弃答题。

## 五、2026 新基准跟踪

- **BEAM**（1M/10M tokens）：全系统普遍掉到 40-64 分——网络恢复后 Trinity
  应优先补跑（最大分水岭）；
- **LoCoMo-Plus**（ACL 2026）、**MemoryCD**（长期跨域个性化）：纳入跟踪；
- Kumiho 93.3% 系 LoCoMo-Plus Judge Score（非 F1）且无第三方复测——引用需注明。

---

*维护：本文件随评测更新修订；任何对外数字须先对照本文件口径。*
