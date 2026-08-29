# auto-evolve 长期观察基线（2026-08-28）

## 当前基线（阶段2 实战）

| 项 | 值 |
|---|---|
| 首次无人值守 | 2026-08-28（tune_report.py help 文本） |
| 补丁生成 | LLM 文本替换模式（短块 1-3 行 + 3 次重试） |
| 校验 | 唯一匹配（count==1）+ py_compile |
| 门禁 | fulltest（pytest 全量 1261+ + eval 12） |
| 结果 | APPLIED → auto commit → GATE PASSED ✓ |
| 失败处理 | 门禁失败自动 git revert |

## 观察点（每次 auto-evolve 后检查）

1. **门禁失败率**：连续 2 次失败 → 暂停 auto 模式（排查 LLM 质量/查询集）；
2. **补丁质量**：revert 后检查 revert 原因（门禁具体失败项）；
3. **白名单遵守**：确认从未修改 scripts/ 外文件（git diff 核对）；
4. **commit 增长**：auto-evolve commit 数量（预期低频——按需触发非自动循环）；
5. **git 健康**：revert 后 git log 干净（无残留半合并状态）。

## 告警信号（出现即人工介入）

- 门禁失败率 >50%（连续）；
- LLM 补丁格式错误率上升（3 次重试仍失败）；
- 任何 scripts/ 外文件被修改（越界——立即 git revert + 排查白名单）；
- 门禁执行超时（>30min——fulltest 本身故障）。

## 检查命令

```bash
git log --oneline --grep "auto-evolve"       # 自改历史
python scripts/evolve_patch.py --target ... --goal ... --apply --auto  # 触发
python scripts/fulltest_gate.py              # 手动门禁
```

## 演进方向

- 阶段 2.5：auto 模式白名单扩展评估（tests/ 辅助文件）；
- 阶段 3：核心代码受控自改（见 SELF_EDIT_PIPELINE 阶段 3 设计）；
- 长期：auto-evolve 纳入维护链（按需触发——非定时循环）。

*生成 2026-08-28*

## 回放验证（2026-08-29，首次安全网实测）

- 对 auto-evolve commit 091e2bb 执行 git revert → 文件恢复原状 ✓
→ revert the revert → 补丁恢复（help 行恢复）✓ 编译 OK ✓；
- **结论：安全网真实可用**（门禁失败时的 revert 路径已实证）；
- git log 新增 Revert/Reapply 对（历史清晰可溯）。
