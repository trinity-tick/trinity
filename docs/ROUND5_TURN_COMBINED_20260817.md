# 评测优化整合快照（2026-08-17, turn x route2 组合验证）

> 注: Trinity 记忆服务本轮不可用（ping/write 均超时），本文件为补充持久化。
> 完整上下文见 docs/OPTIMIZATION_FROM_LMEME_20260816.md 3.9-3.13 节。

## 本轮实测（judge3 3票, 50题同批 seed42, 脚本 benchmark/lme_route3.py）

| 配置 | 整体 | multi(17) | temporal(11) | KU(7) | user(10) | pref(2) |
|---|---|---|---|---|---|---|
| baseline（dated plain） | 0.64 | 0.353 | 0.545 | 1.0 | 1.0 | 0.0 |
| route（multi=turn 粒度） | 0.74 | 0.588 | 0.636 | 0.857 | 1.0 | 0.5 |
| route_tt（multi+temporal 都 turn） | 0.72 | 0.647 | 0.545 | 0.857 | 1.0 | 0.0 |

## 结论（与 3.12 修订整合后）

1. multi=turn 粒度检索: 确定性增益（3.10 单独 +24pp / 3.11 组合 +23.5pp 双重一致）
2. temporal 保持 session 粒度 + REL + inner2（turn 粒度拖累 63.6→54.5%）
3. preference 用 LLM 两段式 pref3（36-60%），不用 ppro 正则画像（3.12 30题证伪）
4. KU/其余 dated plain（freshness/chronos 证伪）
5. 当前最优组合预期: route2(72%) 基础上 +multi turn ≈ 78%

## 产物
- 脚本: benchmark/lme_route3.py, lme_multi_turn.py, lme_multi_con.py, judge3.py
- 数据: .trinity/bench-official/r3_*.json, turn_*.json, mc_*.json, judge3_*.json
