@echo off
REM ============================================================
REM 安装 Trinity DSH 自启（无需管理员）— 用户登录时自动运行
REM 监督循环（每5分钟）+ 维护循环（每4小时 + 每日03:00）
REM 卸载: uninstall-autostart.bat
REM ============================================================
setlocal
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set VBS=%STARTUP%\trinity-dsh-autostart.vbs

echo 生成自启脚本: %VBS%
(
echo Set ws = CreateObject("Wscript.Shell"^)
echo ws.Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\Users\Administrator\trinity\dsh-ops\trinity-autostart.ps1""", 0, False
) > "%VBS%"

echo.
echo 完成。下次登录 Windows 时自动生效；也可立即手动启动:
echo   powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\Administrator\trinity\dsh-ops\trinity-autostart.ps1
echo 日志: C:\Users\Administrator\.trinity\logs\dsh-autostart.log
endlocal
