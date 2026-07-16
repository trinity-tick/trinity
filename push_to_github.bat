@echo off
REM ============================================================
REM Trinity — 一键推送到 GitHub
REM ============================================================
setlocal

echo [1/2] 配置远程仓库...
cd /d %~dp0
git remote add origin https://github.com/trinity-tick/trinity.git 2>nul
git remote set-url origin https://github.com/trinity-tick/trinity.git

echo [2/2] 推送到 GitHub...
git push -u origin master

if %ERRORLEVEL% equ 0 (
    echo.
    echo ======== 推送成功! ========
    echo.
    echo 发布到 PyPI:
    echo   pip install build twine
    echo   python -m build
    echo   twine upload dist/*
) else (
    echo.
    echo 推送失败。请运行:
    echo   git remote set-url origin https://YOUR_TOKEN@github.com/trinity-tick/trinity.git
    echo   git push -u origin master
    echo.
    echo 获取 Token: https://github.com/settings/tokens
)
pause
