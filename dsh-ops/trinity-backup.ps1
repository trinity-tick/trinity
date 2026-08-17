# Trinity WAL 安全备份 + 保留策略 (基建夯实 2026-08-16)
param([int]$RetentionDays = 14)
$ErrorActionPreference = "Stop"
$src = Join-Path $env:USERPROFILE ".trinity\store\trinity_store.db"
$dir = Join-Path $env:USERPROFILE ".trinity\backups"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$dst = Join-Path $dir ("trinity_store_" + $stamp + ".db")
$py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
$code = @"
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src)
d = sqlite3.connect(dst)
s.backup(d)
d.close(); s.close()
print("backup ok")
"@
$tmp = Join-Path $env:TEMP ("trinity_backup_" + $PID + ".py")
Set-Content -Path $tmp -Value $code -Encoding UTF8
& $py $tmp $src $dst
Remove-Item $tmp -ErrorAction SilentlyContinue
Get-ChildItem $dir -Filter "trinity_store_*.db" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$RetentionDays) } | Remove-Item -ErrorAction SilentlyContinue
$mb = [math]::Round((Get-Item $dst).Length / 1MB, 1)
$n = (Get-ChildItem $dir -Filter "trinity_store_*.db" | Measure-Object).Count
Write-Output "backup -> $dst ($mb MB); retained backups: $n"