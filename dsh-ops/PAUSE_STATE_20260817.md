# Trinity 优化执行 — 暂停状态快照（2026-08-17 ~10:00）

> 用户指示：聊天暂停。以下为可无缝恢复的执行状态。

## 已完成并提交（本地，未推送）
- d87a742: 官方 LongMemEval_S 500 题基准 + README 诚实化 + 产品化文档
- ce3aed2: QA2b 全量结果（49.6%）
- 9a273d2: dated 优化（49.6%→54.0%，temporal +15.7pp）+ 异步 LLM 提取默认
- bb245e9: 第二轮（pref2 两段式偏好 3.3%→16.7%、inner2 temporal 63.6%、all2 50 题 64%）

## Round-3 实验（已产出，**待判分**）— 使用并发工作流的 lme_qa_opt3.py（variant 版，09:34 写入）
| 文件 | 配置 | 说明 |
|---|---|---|
| r3_temp_base.json | baseline temporal 50 题 seed42 | 控制组 |
| r3_temp_timeline.json | timeline temporal 50 题 | [REL: N 天] 相对日期链，待判分 |
| r3_multi_base.json | baseline multi 50 题 | 控制组 |
| r3_multi_stitch.json | stitch multi 50 题 | 逐会话提取+聚合，待判分 |
| r3_pref_base.json | baseline pref 30 题 | 控制组 |
| r3_pref_pref3.json | pref3 pref 30 题 | 具体锚点两段式，待判分 |

## 待办（恢复后）
1. 判分 6 个 r3 文件（用 benchmark/lme_judge2.py 或 judge 脚本，官方模板）
2. 对比 timeline/stitch/pref3 vs baseline，记录正/负面结论
3. 有效项合并 → 50 题总 A/B（全量 500 用户已指示不跑）
4. 更新 docs/OPTIMIZATION_FROM_LMEME_20260816.md + 本地提交

## 重要发现（并发工作流）
- benchmark/lme_qa_opt3.py 与 benchmark/judge_ab.py 在 09:34 被另一进程改写/创建
  （variant: baseline/timeline/stitch/pref3）——勿覆盖，直接使用
- 我的 pref3 变体（加最近经验）负优化：3.3% vs pref2 16.7%
- multi3 两段式（简单版）负优化：23.3% vs 38.4%
- LoCoMo/BEAM 数据获取：网络阻塞

## 环境
- DeepSeek key 在 ~/.dsh/.credentials.yaml（DEEPSEEK_API_KEY）
- 官方数据: C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json
- 判分模板: benchmark/lme_judge2.py（官方分题型模板 + reason-first 双口径）
