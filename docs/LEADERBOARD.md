# Trinity 官方基准榜（EXECUTION 466，2026-09-03 刷新）

> 数据集：LongMemEval oracle（xiaowu0162/longmemeval-cleaned 官方变体，500 题 6 类）
> 工具：benchmark/official_lm_eval.py（--strategy routed；judge=normalize 子串 + LLM 语义判分）
> 纪律：全量锁定才上榜；实验（含失败）见 dsh-ops/EXECUTION.md 460-465。

| 版本 | AnswerAcc | 类目要点 | 产物 |
|---|---|---|---|
| 官方基准（09-02 上午） | 0.560 | SS-U .986/KU .731/SS-A .679/TR .399/MS .391/SS-P .367 | official_lmeval_S_answer500.json |
| v1 提示路由（460） | 0.578 | KU +7.7pp、SS-P +6.7pp | lme_oracle_500_routed_*.json |
| **v2（锁定）** | **0.642** | **MS .391→.617（深度上下文 cap14 +22.6pp）** | lme_oracle_500_routed_v2_20260902.json |
| v3 cap14 外推（463，证伪） | 0.626 | 小类目噪声翻转 → 回滚 | lme_oracle_500_routed_v3_20260902.json（存档） |

检索（同数据）：R@1/3/5/10 = 1.000 ×6 类（两次独立复现 EXIT=0）。
回归门禁（466 起）：分层抽样 100 题 recall rate5=1.0（基线已存，maintenance eval-gate 周检）。
诊断（466）：六类"答案会话首条即 rank1"——会话级召回无瓶颈；QA 短板在消息内容级与生成。
