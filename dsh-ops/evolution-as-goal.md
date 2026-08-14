# 把 Trinity 进化周期迁到 DSH Goal（P0-3 落地指南）

## 背景

trinity 的自进化引擎 `MetaEvolution`（`trinity/evolution/core.py`）是
Observe → Analyze → Plan → Execute → Certify 五相位循环：

- 每次 `tick()` 只推进 **1 个相位**（5 tick = 1 完整周期）；
- 中途状态（`current_cycle` / `_phase_queue`）只在内存（`core.py:114-115`），
  进程重启即丢失；
- 状态 JSON 仅在周期完成时落盘，且写盘非原子（`core.py:150-151`）；
- `EvolutionScheduler.schedule_cycle()` 只存 `interval_hours`，从不真正调度
  （`evolution/evolution_scheduler.py:122-131`）。

## 方案：DSH goal = 每个相位一轮（有检查点、可暂停/恢复、GUI 可见）

在 DSH 会话中执行：

1. 用 `/goal` 命令或 goal 工具创建目标，例如：

   ```
   目标：推进 Trinity 自进化一个完整周期。
   每轮只执行一个相位（observe / analyze / plan / execute / certify 之一），
   用 .venv\Scripts\python.exe 运行：
     from trinity.evolution import MetaEvolution
     evo = MetaEvolution()
     r = evo.tick({'action':'goal'})
     evo.save_state()
     print(r)
   完成后报告当前相位与 diagnostics()，然后等待下一轮指令。
   ```

2. DSH goal 会按轮次推进，**每一轮结束即有一个持久化检查点**——即使会话/
   进程重启，也能从上一轮继续，不再丢中途状态（这正是 `core.py:114-115`
   内存态丢失问题的替代）。

3. 自动化触发：`install-dsh-schedules.bat` 已注册 `TrinityDSHEvolution`
   （每 4 小时）与 `TrinityDSHHealth`（每日，含 evolution tick），
   通过 `dsh-ops\trinity-dsh-maintenance.ps1 -Tasks evolution` 直接驱动
   tick + save_state（Direct 模式）或经 `dsh --profile headless`（-ViaDsh）。

4. 观察：DSH web GUI 的 goal 面板（GoalBar）会显示目标状态、轮次、暂停/
   恢复/清除按钮；trajectory 面板可回放每一轮的工具调用。

## 回滚

不修改任何 trinity 代码即可回退到旧行为：删除计划任务
（`uninstall-dsh-schedules.bat`），evolution 回到手动 `trinity_init.py init`。
