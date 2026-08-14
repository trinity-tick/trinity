@echo off
REM ===========================================================================
REM  start_trinity_bridge.bat — Marvis-Trinity 实时同步桥启动脚本
REM ===========================================================================
REM 用途：确保 Trinity API 运行中，注册 6 个 Marvis Agent，输出就绪状态
REM ===========================================================================

setlocal enabledelayedexpansion

set TRINITY_HOME=%~dp0..
set API_URL=http://localhost:8005

echo.
echo   Marvis-Trinity Bridge — Startup
echo   ======================================

REM ── Step 1: 检查 Trinity API 是否运行 ────────────────────────────

echo   [1/2] Checking Trinity API at %API_URL%/health ...
powershell -NoProfile -Command ^
  "try { $r = Invoke-RestMethod '%API_URL%/health' -TimeoutSec 3; Write-Output 'OK: v' + $r.version } catch { Write-Output 'NOT_RUNNING' }"

if %ERRORLEVEL% neq 0 (
    echo   ERROR: Trinity API not reachable. Start Trinity first.
    echo   Use: cd %TRINITY_HOME% ^&^& python -m trinity.api.server
    exit /b 1
)

REM ── Step 2: 注册 Agent ───────────────────────────────────────────

echo.
echo   [2/2] Registering 6 Marvis agents ...

cd /d "%TRINITY_HOME%"
python -c "import sys; sys.path.insert(0, '.'); from trinity.bridges import MarvisTrinityBridge; bridge = MarvisTrinityBridge(); results = bridge.register_all_agents(); print(); ok = sum(1 for r in results if r['ok']); print(f'  Registered: {ok}/{len(results)} agents')"

if %ERRORLEVEL% neq 0 (
    echo   ERROR: Agent registration failed.
    exit /b 1
)

REM ── Ready ────────────────────────────────────────────────────────

echo.
echo   ======================================
echo     Bridge is READY.
echo     Trinity API : %API_URL%
echo     6 Marvis agents registered.
echo     Use bridge.push_*() to sync conversations.
echo   ======================================
echo.

endlocal
exit /b 0
