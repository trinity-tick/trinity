# Trinity SLO（服务等级目标）— 2026-08-18

> 按 SRE 稳定性工程标准定义可度量目标。当前为自监控（本地），告警推送可选 webhook。

## 服务级 SLO

| 服务 | 可用性目标 | 延迟目标 | 说明 |
|---|---|---|---|
| trinity-api :8001 | 99.9%（月） | 检索 P95 < 100ms | 147 端点；supervisor 5 分钟自愈 |
| trinity-mcp :8000 | 99.9%（月） | 工具调用 P95 < 1s | SSE 常驻 |
| gateway :8002 | 99.5%（月） | /v1/models P95 < 500ms | 依赖上游 DeepSeek |
| 记忆检索 | — | P95 < 100ms | 47 通道 RRF（实测 19-30ms P50） |
| 记忆写入 | — | P95 < 200ms | 含审计/版本/FTS（实测 60ms P50） |

## 数据 SLO

| 项 | 目标 | 现状 |
|---|---|---|
| RPO（恢复点） | ≤ 24h（每日备份） | ✅ 每日 WAL 备份 14 天保留 |
| RTO（恢复时间） | ≤ 1h（备份恢复） | ✅ backup 库可恢复 |
| 数据一致性 | integrity ok + FTS 一致 | ✅ 每日链校验 |
| 写锁可用性 | 无持续锁 | ✅ 0.00s AVAILABLE 实测 |

## 告警通道

- 当前：supervisor 日志 WARN（collector 零事件 / pg down / api down）
- 可选：TRINITY_ALERT_WEBHOOK 环境变量设置后，关键告警 POST 推送（见 supervisor Send-Alert）

## 预算（error budget）

- 月度 99.9% → 允许停机 43 分钟/月；supervisor 自愈目标：单次故障 < 5 分钟
- 关键指标记录：/health degradation tier（full=无降级）
