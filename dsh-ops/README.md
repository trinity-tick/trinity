# dsh-ops — DSH × Trinity 运维工具集

本目录是 DSH（DeepSeek Harness）优化 trinity 的执行产物。完整说明见
[EXECUTION.md](EXECUTION.md)（含回滚）。联合架构整体能力盘点见
[`../docs/JOINT_CAPABILITY_MAP_20260814.md`](../docs/JOINT_CAPABILITY_MAP_20260814.md)。

| 文件 | 说明 |
|---|---|
| `trinity-dsh-maintenance.ps1` | 维护驱动器（health / evolution / decay / tiers / sync / selftest），支持 `-Direct` / `-ViaDsh` |
| `trinity-supervisor.ps1` | 进程监督（api :8001 / mcp :8000 / collector） |
| `trinity-autostart.ps1` | 免提权常驻循环（每 5min 监督 + 每 4h 维护 + 每日 03:00 衰减分层同步） |
| `install-autostart.bat` / `uninstall-autostart.bat` | 安装/卸载 Startup 自启 VBS（无需管理员） |
| `install-dsh-schedules.bat` | 注册 5 个计划任务（**需管理员运行**；被环境拒绝时用 autostart 方案） |
| `uninstall-dsh-schedules.bat` | 删除计划任务 |
| `run-benchmarks.ps1` | 基准套件并行运行器 |
| `trinity-benchmark.workflow.js` | DSH workflow 编排示例（parallel 扇出 + 结构化汇总） |
| `align-pg-schema.sql` + `apply-pg-alignment.py` | 对齐 PG memories/memory_versions 到代码期望列集（带备份，幂等） |
| `evolution-as-goal.md` | 把进化周期迁到 DSH goal 的指南 |
| `EXECUTION.md` | 改动清单 / 验证结果 / 已知问题 / 回滚 |

相关改动：
- DSH profile MCP 接入：`C:\Users\Administrator\.dsh\profiles\web\cordis.patch.yml`
- trinity 源码：`trinity/telemetry/tracer.py`、`trinity/collector/__main__.py`、
  `trinity/collector/daemon.py`、`trinity/evolution/__init__.py`、
  `trinity/adapters/postgresql.py`、`trinity/daemon/memory_compressor.py`、
  `trinity/api/server.py`、`trinity/mcp/tools/memory_tools.py`、
  `scripts/run_decay_compress.py`、`scripts/run_memory_tiers.py`
