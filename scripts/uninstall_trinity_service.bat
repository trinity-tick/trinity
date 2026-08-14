@echo off
chcp 65001 >nul
setlocal
REM ============================================================
REM  Trinity Service Uninstaller
REM  必须以管理员身份运行 / MUST be run as Administrator
REM ============================================================

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 必须以管理员身份运行此脚本 (right-click → Run as administrator)
    exit /b 1
)

echo [1/3] Stopping TrinityService (if running) ...
sc stop TrinityService >nul 2>&1
timeout /t 3 /nobreak >nul

echo [2/3] Deleting Windows service ...
sc delete TrinityService >nul 2>&1

echo [3/3] Removing scheduled-task fallback (if any) ...
schtasks /Delete /TN "TrinityService" /F >nul 2>&1

echo [OK] TrinityService removed.
exit /b 0
