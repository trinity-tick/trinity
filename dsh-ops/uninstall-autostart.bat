@echo off
REM 卸载 Trinity DSH 自启（删除 Startup VBS）
setlocal
set VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\trinity-dsh-autostart.vbs
if exist "%VBS%" (
    del "%VBS%"
    echo 已删除: %VBS%
) else (
    echo 未找到自启脚本（可能未安装）。
)
endlocal
