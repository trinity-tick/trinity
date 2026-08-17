# Trinity 生产级治理验收报告（2026-08-18）

> 范围：2026-08-17 生产级治理（第 37 轮）+ 验收遗留建议执行（第 38 轮，含 QA 深度优化）。所有数据均为实测。

## 一、治理目标与达成

| 目标 | 状态 | 实测证据 |
|---|---|---|
| 工程卫生（构建产物去重） | 完成 | build/545 文件删除、site-packages stale 6.37 清除、output/temp 归档 backup/artifacts-20260817/ |
| 四大单体拆分（行为不变） | 完成 | sqlite 11 mixins / client 12 / aggregator 13 / server 12 routers；151 路由=151 路由、145 端点字节级一致；全量 pytest 815 passed |
| decay 接真实 LLM | 完成 | --llm auto（有 key 走 real 实测返回摘要 / 无 key 回退 mock）；maintenance 默认 auto |
| 三库拓扑收敛 | 完成 | 原生 PG 5432 双服务（postgresql-16 + postgresql-x64-16）均 Manual |
| 全量 500 QA 基线锁定 | 完成 | judge3 三票 majority 63.2%（316/500），稳定性 97.3% |
| 一次性脚本归档 | 完成 | 43 个实验脚本 → benchmark/archive/ |
| 合并 main | 完成 | feat/hygiene-20260814 → main（merge d66c0e6，153 commits ahead of origin） |
| push origin/main | 外部阻塞 | github.com:443 网络已恢复（200），但 .github_token 失效（401）——需用户提供有效 token |

## 二、QA 优化成果（全量 500 实测，judge3 三票）

| 版本 | 全量 | multi | SS-P | temporal | KU |
|---|---|---|---|---|---|
| 基线（route2 脚本） | 63.2% | 43.6% | 20.0% | 62.4% | 64.1% |
| RouteReasoner 产品化策略路由 | 67.4% | 49.6% | 36.7% | 65.4% | 69.2% |
| FINAL（+ pref-inner2） | 68.6% | 49.6% | 56.7% | 65.4% | 69.2% |

- SS-P：20.0% → 56.7%（+36.7pp），达到 >=45% 目标 —— pref 分支 inner2 过滤（与 opt3 pref3 对齐）。
- multi：43.6% → 49.6%（+6.0pp）—— turn 粒度检索；>=55% 目标未达，已穷尽全部低成本路线（turn24=45.1%、dates+chrono=14.3%、命题化慢版 7h 不可行、命题化快版 0.75% 灾难性）——瓶颈在跨会话综合，需写入时命题化管线重构（大工程，OPTIMIZATION_PLAN 已规划）。
- temporal：62.4% → 65.4%（+3.0pp，修复摄入 [DATE:] 前缀后 REL/时间线生效）。

## 三、collector 零事件处理

- 无源非故障确认：4318 scanner cycles / 0 errors；6 个 BUILTIN_AGENTS 只是监听目录；agent_config.yaml 不存在；无 agent 运行时写缓存。
- supervisor 告警去噪（3 连 + 每 12 轮一次）；事件源接入 API 已文档化（AgentConnector hooks / agent_config.yaml）。

## 四、稳定性与验证

- 全量 pytest：815 passed / 50 skipped / 0 failed（多次复跑确认）。
- API：运行重构后代码（v8.2.0, tier=full），10 个 router 域全部 200，supervisor 自愈实测通过（kill→自动拉起）。
- MCP :8000 / gateway :8002 / PG :5430 全部在线。
- 两个 .ps1 的 UTF-8 BOM 已修复（PS 5.1 已知坑）。

## 五、交付物

- 拆分包：trinity/adapters/sqlite/、trinity/core/client/、trinity/agents/aggregator/、trinity/api/server/（各含 _monolith_backup.py 可回滚）
- 文档：dsh-ops/EXECUTION.md 第 37/38 轮（含回滚指引）、README/README.zh 更新
- QA 产物：~/.trinity/bench-official/（lme_route2_full500 / rr_route2_full500 / rr_temporal_fix_133 / rr_pref_inner2_30 / 对应 judge3 结果）
- 提交：main @ cb4433a（153 commits ahead of origin/main，待 push）

## 六、遗留事项（需用户）

1. push origin/main：提供有效 GitHub token（写入 trinity/.github_token）后即可推送 153 commits。
2. multi >=55%：需写入时命题化管线重构（独立大工程，建议开新目标）。
3. collector 事件源：如需真实采集，需 agent 运行时接入 AgentConnector。

---
报告基于 2026-08-17/18 实测数据生成。