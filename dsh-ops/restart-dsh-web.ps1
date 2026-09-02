# dsh web host 重启包装（2026-08-17, 使插件 JS 改动生效）
$ErrorActionPreference = "SilentlyContinue"
Start-Sleep -Seconds 5
# 2026-09-01: 按端口杀（原写死 PID 38784 已过期）
$webConn = Get-NetTCPConnection -LocalPort 3080 -State Listen -ErrorAction SilentlyContinue
if ($webConn) { $webConn | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }
Start-Sleep -Seconds 3
Set-Location "C:\Users\Administrator"
$log = "C:\Users\Administrator\.trinity\logs\dsh-web-restart.log"
try { Start-Process -FilePath "cmd.exe" -ArgumentList "/c","dsh web" -WindowStyle Hidden; "OK $(Get-Date -Format o) started new dsh web" | Out-File $log -Append } catch { "FAIL $(Get-Date -Format o) $($_.Exception.Message)" | Out-File $log -Append }
