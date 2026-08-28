# tune 每日实况观察基线（2026-08-28）

## 基线（2026-08-28 实测）

| 项 | 值 |
|---|---|
| 命中率（10 问 reason） | 10/10（阈值 0.5/0.55/0.6/0.7 全部） |
| LLM judge 调用 | 0（启发式全覆盖） |
| 推荐阈值 | 0.5（LLM 最少） |
| 推荐 top_k | 3 |
| 双推荐保留 | ✓（合并逻辑修复后） |
| 维护链执行 | -Tasks tune 真实运行 OK（37 任务链） |

## 观察点（每日检查）

1. **命中率应保持 ~1.0**（低于 0.8 告警——检索/启发式退化）；
2. **LLM 调用数**：>0 且增长 → 启发式覆盖下降（阈值需重调或候选质量下降）；
3. **推荐值漂移**：推荐阈值变化 >0.1 → 查询分布变化（观察）；
4. **告警信号**：hits 恒 0（静默异常——曾因 int 转换 bug）——应立即排查。

## 检查命令

```bash
powershell -File dsh-ops\trinity-dsh-maintenance.ps1 -Tasks tune   # 手动跑
cat ~/.trinity/tuned_config.json                                   # 看推荐
python scripts/tune_report.py                                      # 效果评估
```

## 阈值策略

- 阶段 1（当前）：推荐 0.5，应用链 env > tuned_config > 0.55；
- 阶段 2：命中率 <0.8 或 LLM 调用持续增长时，手动 A/B 校验后下调/重调；
- 告警：维护链输出 hits 异常即触发 investigation（记录 EXECUTION）。

*生成 2026-08-28*
