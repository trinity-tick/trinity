# Trinity 执行器地图（EXECUTION 458C 梳理，2026-09-02）

> 目标：195+ scripts / 双 runner / 多 daily-loop 的"谁在跑、谁退役、入口在哪"一目了然。

## 1. 基准 runner（收敛）
| 文件 | 用途 | 状态 |
|---|---|---|
| benchmark/official_lm_eval.py | **官方 LongMemEval oracle 锁定数字入口**（R@k 500=1.0；--answer QA） | ✅ 正式入口 |
| benchmark/answer_eval_strategies.py | 生成侧策略 A/B（tr/ms/ss-p 对照） | ✅ 专项入口 |
| benchmark/longmemeval_official_runner.py | 265MB cleaned-S ingest 实验（458 已修 sqlite 隔离+提速） | ⚠️ 实验入口（数字以 official_lm_eval 为准） |

## 2. 自治/好奇/主动类（4 个，分工）
| 脚本 | 角色 | 建议 |
|---|---|---|
| scripts/curiosity_daily.py | 通用好奇→web_search（老入口） | 保留（历史任务 curiosity） |
| scripts/cognition_agent.py | 通用主动主体（缺口/错误→think→沉淀） | 保留（任务 cognition-agent） |
| scripts/proactive_daily.py | 内部状态→倡议收集 | 保留（任务 proactive） |
| scripts/opsbot_daily.py | 第二 agent（ops-bot）自治实例 | 保留（任务 opsbot-cycle） |

## 3. 感知类（4 个，分工）
| 脚本 | 角色 | 建议 |
|---|---|---|
| scripts/perception_scan.py | 日志告警→感知（历史任务 perception-bridge/perception-scan） | 保留 |
| scripts/perception_bridge.py | 感知桥（文件侧） | ⚠️ 与 scan/loop 重叠，观察期 |
| scripts/perception_loop.py | **持续感知流**（inbox 图→语义视觉→记忆，458 新增） | ✅ 新入口 |
| scripts/web_perception.py | 网络感知 | 保留 |

> 收敛原则：**不删可运行脚本**（可能被任务/会话引用），只做分工标注 + 退役建议；真正的删除需审计引用后另行执行。
