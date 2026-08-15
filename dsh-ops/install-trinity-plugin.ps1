# install-trinity-plugin.ps1 — 同步 dsh-trinity 插件源码到 web profile
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File dsh-ops\install-trinity-plugin.ps1
# 说明: 源码在 trinity\dsh-plugin\dsh-trinity（git 管理），node_modules 副本是安装产物。

$ErrorActionPreference = "Stop"

$src = Join-Path $PSScriptRoot "..\dsh-plugin\dsh-trinity"
$dst = Join-Path $env:USERPROFILE ".dsh\profiles\web\node_modules\@deepseek-ai\dsh-trinity"

if (-not (Test-Path (Join-Path $src "lib\index.js"))) {
    throw "插件源码缺失: $src\lib\index.js"
}

New-Item -Path $dst -ItemType Directory -Force | Out-Null
New-Item -Path (Join-Path $dst "lib") -ItemType Directory -Force | Out-Null
Copy-Item (Join-Path $src "package.json") (Join-Path $dst "package.json") -Force
Copy-Item (Join-Path $src "lib\index.js") (Join-Path $dst "lib\index.js") -Force

Write-Output "dsh-trinity 插件已同步到:"
Write-Output "  $dst"
Write-Output "（新 DSH 会话即可用 trinity_* 工具；node_modules 内 JS 变更需重启 web profile 生效）"
