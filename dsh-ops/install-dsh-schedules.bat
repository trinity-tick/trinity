@echo off
REM ============================================================
REM Trinity DSH 计划任务注册 (替换旧的 Startup VBS + 单条 schtasks)
REM 用法: 双击运行 或 命令行执行本文件
REM 卸载: 运行 uninstall-dsh-schedules.bat
REM ============================================================
setlocal
set OPS=C:\Users\Administrator\trinity\dsh-ops
set PS=powershell -NoProfile -ExecutionPolicy Bypass -File

echo.
echo [1/5] TrinityDSHHealth — 每日 08:30 健康检查 + 进化 tick
schtasks /Create /TN "TrinityDSHHealth" /TR "%PS% \"%OPS%\trinity-dsh-maintenance.ps1\" -Tasks health,evolution" /SC DAILY /ST 08:30 /F

echo.
echo [2/5] TrinityDSHMaintenance — 每日 03:00 衰减压缩 + 分层 + 双向同步 (需 PostgreSQL)
schtasks /Create /TN "TrinityDSHMaintenance" /TR "%PS% \"%OPS%\trinity-dsh-maintenance.ps1\" -Tasks decay,tiers,sync" /SC DAILY /ST 03:00 /F

echo.
echo [3/5] TrinityDSHEvolution — 每 4 小时进化 tick
schtasks /Create /TN "TrinityDSHEvolution" /TR "%PS% \"%OPS%\trinity-dsh-maintenance.ps1\" -Tasks evolution" /SC HOURLY /MO 4 /F

echo.
echo [4/5] TrinityDSHSelfTests — 每周日 04:00 全模块自检 (较慢)
schtasks /Create /TN "TrinityDSHSelfTests" /TR "%PS% \"%OPS%\trinity-dsh-maintenance.ps1\" -Tasks selftest" /SC WEEKLY /D SUN /ST 04:00 /F

echo.
echo [5/5] TrinityDSHSupervisor — 每 5 分钟进程监督 (api/mcp/collector)
schtasks /Create /TN "TrinityDSHSupervisor" /TR "%PS% \"%OPS%\trinity-supervisor.ps1\"" /SC MINUTE /MO 5 /F

echo.
echo 注册完成。验证:
schtasks /Query /TN "TrinityDSHHealth" /FO LIST | findstr /C:"TaskName" /C:"Status"
echo.
echo 手动立即运行一次:  schtasks /Run /TN TrinityDSHHealth
endlocal
