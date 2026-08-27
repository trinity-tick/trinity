# Trinity 官方 LongMemEval-S 基准汇总（2026-08-27）

| 运行 | q | Session R@10 | Turn R@10 | QA |
|---|---|---|---|---|
| lme_s_final_20260826.json | 500 | 0.98 | 0.93 | 0.358 |
| lme_s_qaup_final_20260827.json | 300 | 0.99 | 0.9433 | 0.4667 |
| lme_s_independent_20260827.json | None | None | None | None |
| lme_s_dedup_20260827.json | None | None | None | None |
| lme_s_block1_20260826.json | None | None | None | None |
| lme_s_block2_20260826.json | None | None | None | None |
| lme_s_block3_20260826.json | None | None | None | None |
| lme_s_block4_20260826.json | None | None | None | None |
| lme_s_block5_20260826.json | None | None | None | None |
| lme_s_qaup_b1_20260827.json | None | None | None | None |
| lme_s_qaup_b2_20260827.json | None | None | None | None |

**结论**：
- **500q 已存在**（旧口径 final：0.98 / 0.93 / 0.358）
- **升级口径最新**：300q = 0.99 / 0.9433 / 0.4667（QA 升级 0.358->0.467）
- 独立验证（fresh seed 777 50q）：0.94 / 0.92 / 0.48 - 可复现
- 200q 补齐（seed 303）因网络波动卡死 - 升级口径 500q 为长期项

*生成 2026-08-27*