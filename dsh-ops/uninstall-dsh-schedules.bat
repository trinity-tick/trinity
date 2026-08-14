@echo off
REM ============================================================
REM 卸载 Trinity DSH 计划任务 (全部 5 个)
REM ============================================================
setlocal
for %%T in (TrinityDSHHealth TrinityDSHMaintenance TrinityDSHEvolution TrinityDSHSelfTests TrinityDSHSupervisor) do (
    echo 删除任务: %%T
    schtasks /Delete /TN "%%T" /F >nul 2>&1
)
echo.
echo 全部删除完成。验证:
schtasks /Query /TN "TrinityDSH*" 2>nul || echo (无剩余 TrinityDSH 任务)
endlocal
