@echo off
chcp 65001 >nul
setlocal
REM ============================================================
REM  Trinity Service Installer (api/mcp/collector watchdog)
REM  必须以管理员身份运行 / MUST be run as Administrator
REM ============================================================

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 必须以管理员身份运行此脚本 (right-click → Run as administrator)
    exit /b 1
)

set "PYTHON=C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
set "SCRIPT=C:\Users\Administrator\trinity\scripts\trinity_service.py"
set "LOG=C:\Users\Administrator\.trinity\logs"

if not exist "%PYTHON%" (
    echo [ERROR] System Python not found: %PYTHON%
    exit /b 1
)
if not exist "%SCRIPT%" (
    echo [ERROR] Service script not found: %SCRIPT%
    exit /b 1
)
if not exist "%LOG%" mkdir "%LOG%"

REM ---- check pywin32: native service vs scheduled-task fallback ----
"%PYTHON%" -c "import win32serviceutil" >nul 2>&1
if errorlevel 1 goto :no_pywin32

echo [1/3] Registering native Windows service TrinityService ...
sc create TrinityService binPath= "\"%PYTHON%\" \"%SCRIPT%\"" start= auto DisplayName= "Trinity Watchdog Service (api/mcp/collector)"
if errorlevel 1 (
    echo [ERROR] sc create failed — service may already exist. Run uninstall_trinity_service.bat first.
    exit /b 1
)
sc description TrinityService "Probes trinity-api (:8001/health), trinity-mcp SSE (:8000) and the collector every 30s and restarts missing processes."
sc failure TrinityService reset= 86400 actions= restart/5000/restart/10000/restart/30000
echo [2/3] Starting TrinityService ...
sc start TrinityService
echo [3/3] Done. Service status:
sc query TrinityService
exit /b 0

:no_pywin32
echo [WARN] pywin32 not found — registering a scheduled task instead (ONSTART, SYSTEM).
schtasks /Create /TN "TrinityService" /TR "\"%PYTHON%\" \"%SCRIPT%\" --foreground" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 (
    echo [ERROR] schtasks /Create failed.
    exit /b 1
)
echo [OK] Scheduled task TrinityService registered (runs at logon/startup as SYSTEM).
schtasks /Run /TN "TrinityService"
exit /b 0
