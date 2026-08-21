@echo off
REM install-sync-agent-schedule.bat — 注册 trinity-sync-agent 登录自启计划任务（免提权）
REM 注意：必须先配置好 ~/.trinity/sync-agent.yaml（server.url 指向远端服务器），否则不要开启。
setlocal
set PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe
set SCRIPT=C:\Users\Administrator\trinity\dsh-ops\trinity-sync-agent.py
if not exist "%PY%" (
  echo [ERR] Python not found at %PY%
  exit /b 1
)
schtasks /query /tn "trinity-sync-agent" >nul 2>&1
if %errorlevel%==0 (
  echo [INFO] task already exists — delete it first to reinstall:
  echo   schtasks /delete /tn "trinity-sync-agent" /f
  exit /b 2
)
schtasks /create /tn "trinity-sync-agent" /tr "\"%PY%\" \"%SCRIPT%\" --loop" /sc onlogon /rl limited /f
if %errorlevel%==0 (
  echo [OK] trinity-sync-agent scheduled at logon.
  echo     Make sure server.url in sync-agent.yaml points to your REMOTE server (not 127.0.0.1).
) else (
  echo [ERR] schtasks create failed.
)
endlocal
