@echo off
REM ============================================================
REM Trinity — 一键发布脚本
REM 在您本地电脑上运行，无需手动操作 GitHub
REM ============================================================
TITLE Trinity 一键发布

setlocal enabledelayedexpansion

echo ============================================================
echo  Trinity 一键发布脚本
echo ============================================================
echo.

:: ---- 检查 git ----
where git >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo [错误] 未找到 git，请先安装: https://git-scm.com
    pause
    exit /b 1
)

:: ---- 配置 Git ----
git config --global user.name "Trinity Team" >nul 2>&1
git config --global user.email "trinity-dev@trinity-tick.dev" >nul 2>&1

:: ---- 第1步：创建 GitHub 仓库 ----
echo [1/3] 请在浏览器中创建 GitHub 仓库...
echo.
echo   1. 打开 https://github.com/new
echo   2. Owner: trinity-tick
echo   3. Repository name: trinity
echo   4. Public，不要勾选任何初始化选项
echo   5. 点击 Create repository
echo.
echo 创建好后按任意键继续...
pause >nul

:: ---- 第2步：推送代码 ----
echo.
echo [2/3] 推送到 GitHub...
cd /d %~dp0
git remote add origin https://github.com/trinity-tick/trinity.git 2>nul
git push -u origin master

if !ERRORLEVEL! neq 0 (
    echo.
    echo [推送失败] 可能需要 GitHub Token
    echo 请按以下步骤操作:
    echo.
    echo   1. 打开 https://github.com/settings/tokens
    echo   2. Generate classic token，勾选 repo
    echo   3. 复制生成的 token
    echo   4. 在本窗口执行:
    echo.
    echo      git remote set-url origin https://YOUR_TOKEN@github.com/trinity-tick/trinity.git
    echo      git push -u origin master
    echo.
    pause
    exit /b 1
)

echo.
echo [OK] 推送成功!

:: ---- 第3步：PyPI 发布 ----
echo.
echo [3/3] 发布到 PyPI...

where pip >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo [跳过] 未找到 pip，跳过 PyPI 发布
    echo 稍后手动运行:
    echo   pip install build twine
    echo   python -m build
    echo   twine upload dist/*
    goto end
)

pip install build twine -q >nul 2>&1
python -m build >nul 2>&1
if !ERRORLEVEL! equ 0 (
    twine upload dist/* 2>&1
    if !ERRORLEVEL! equ 0 (
        echo [OK] PyPI 发布成功!
    ) else (
        echo [跳过] PyPI 需要输入账号密码，稍后手动运行 twine upload dist/*
    )
) else (
    echo [跳过] build 失败，稍后手动运行 pip install build ^&^& python -m build
)

:end
echo.
echo ============================================================
echo  Trinity 发布完成!
echo.
echo  GitHub: https://github.com/trinity-tick/trinity
echo  PyPI:   pip install trinity-memory
echo  MCP:    trinity-mcp --mode stdio
echo  API:    trinity-api --port 8100
echo ============================================================
pause
