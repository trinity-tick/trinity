# Trinity 一键发布脚本
# 用法: powershell -ExecutionPolicy Bypass -File scripts\publish.ps1
# 要求: 已创建 GitHub 仓库并设置好 GITHUB_USERNAME 环境变量

$ErrorActionPreference = "Stop"
$ROOT = "C:\Users\Administrator\trinity"
$USER = [Environment]::GetEnvironmentVariable("GITHUB_USERNAME", "User")

if (-not $USER) {
    Write-Host "请输入你的 GitHub 用户名:" -ForegroundColor Yellow
    $USER = Read-Host
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "   Trinity 发布脚本" -ForegroundColor Cyan
Write-Host "   目标: github.com/$USER/trinity" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# 步骤 1: 确认
Write-Host "`n[1/4] 确认远程仓库..." -ForegroundColor Green
Write-Host "请在浏览器中打开 https://github.com/new" -ForegroundColor Yellow
Write-Host "创建仓库: $USER/trinity (Public, 不初始化)" -ForegroundColor Yellow
$confirm = Read-Host "`n是否已创建? (y/n)"
if ($confirm -ne "y") { Write-Host "请先创建仓库后再运行"; exit 1 }

# 步骤 2: 推送
Write-Host "`n[2/4] 推送到 GitHub..." -ForegroundColor Green
cd $ROOT

# 确保 git 配置
git config user.email "trinity-dev@example.com"
git config user.name "Trinity Team"

# 设置 remote 并推送
git remote add origin "https://github.com/$USER/trinity.git" 2>$null
git branch -M main
git push -u origin main

if ($LASTEXITCODE -eq 0) {
    Write-Host "✅ 推送成功!" -ForegroundColor Green
} else {
    Write-Host "❌ 推送失败。请检查:" -ForegroundColor Red
    Write-Host "  1. GitHub 仓库是否存在" -ForegroundColor Red
    Write-Host "  2. 是否有推送权限" -ForegroundColor Red
    Write-Host "  3. 可尝试: git push -u origin main --force" -ForegroundColor Red
    exit 1
}

# 步骤 3: 创建 Tag
Write-Host "`n[3/4] 创建 v6.37.0 tag..." -ForegroundColor Green
git tag -a v6.37.0 -m "Trinity v6.37.0 — Initial Release"
git push origin v6.37.0

Write-Host "✅ Tag 推送成功!" -ForegroundColor Green

# 步骤 4: 发布到 PyPI (可选)
Write-Host "`n[4/4] 发布到 PyPI..." -ForegroundColor Green
$pypi = Read-Host "是否发布到 PyPI? (y/n)"
if ($pypi -eq "y") {
    pip install build twine
    python -m build
    python -m twine upload dist/*
    
    Write-Host "`n构建 auto-daemon..." -ForegroundColor Green
    cd auto-daemon
    python -m build
    python -m twine upload dist/*
    cd $ROOT
    
    Write-Host "✅ PyPI 发布成功!" -ForegroundColor Green
}

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  发布完成!" -ForegroundColor Cyan
Write-Host "  GitHub: https://github.com/$USER/trinity" -ForegroundColor Cyan
Write-Host "  PyPI:   https://pypi.org/project/trinity-memory/" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

Write-Host "`n后续步骤:" -ForegroundColor Yellow
Write-Host "  1. 创建 GitHub Release: https://github.com/$USER/trinity/releases/new" -ForegroundColor Yellow
Write-Host "  2. 启用 GitHub Pages: https://github.com/$USER/trinity/settings/pages" -ForegroundColor Yellow
Write-Host "  3. 设置 PyPI 可信发布: https://github.com/$USER/trinity/settings/secrets/actions" -ForegroundColor Yellow
