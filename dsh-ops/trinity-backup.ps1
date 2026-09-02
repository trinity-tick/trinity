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

# ── PG 主存储备份（2026-09 P0-1：PG 是主存储，必须有独立备份）──────────
# pg_dump 自定义格式（-Fc，压缩 + 可选择性恢复），与 SQLite 备份同保留策略。
$pgDump = "C:\Users\Administrator\Desktop\pgsql\bin\pg_dump.exe"
if (Test-Path $pgDump) {
    try {
        $pgDst = Join-Path $dir ("trinity_pg_" + $stamp + ".dump")
        $env:PGPASSWORD = "trinity"
        & $pgDump -h 127.0.0.1 -p 5432 -U trinity -d trinity -Fc -f $pgDst 2>&1 | Out-String | Write-Output
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
        if (Test-Path $pgDst) {
            $pgMb = [math]::Round((Get-Item $pgDst).Length / 1MB, 1)
            Get-ChildItem $dir -Filter "trinity_pg_*.dump" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$RetentionDays) } | Remove-Item -ErrorAction SilentlyContinue
            $pgN = (Get-ChildItem $dir -Filter "trinity_pg_*.dump" | Measure-Object).Count
            Write-Output "pg backup -> $pgDst ($pgMb MB); retained pg backups: $pgN"
        } else {
            Write-Output "PG BACKUP FAILED (no output file)"
        }
    } catch {
        Write-Output "PG BACKUP ERROR: $($_.Exception.Message)"
    }
} else {
    Write-Output "pg_dump not found at $pgDump - PG backup skipped"
}

# ── 异卷备份（2026-09-01，异地/异盘物理级保护）：C: 备份产物复制到 D:（独立物理卷）──
$offsiteDir = "D:\trinity-backups"
if (-not (Test-Path $offsiteDir)) { New-Item -ItemType Directory -Path $offsiteDir -Force | Out-Null }
$copied = 0
foreach ($pattern in @("trinity_store_*.db", "trinity_pg_*.dump")) {
    $latest = Get-ChildItem $dir -Filter $pattern -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latest) {
        try {
            Copy-Item $latest.FullName (Join-Path $offsiteDir $latest.Name) -Force
            $dst = Join-Path $offsiteDir $latest.Name
            $srcSz = (Get-Item $latest.FullName).Length
            $dstSz = (Get-Item $dst).Length
            if ($srcSz -eq $dstSz) {
                Write-Output "offsite backup -> $dst ($([math]::Round($dstSz/1MB,1)) MB, size verified)"
                $copied++
            } else {
                Write-Output "OFFSITE BACKUP MISMATCH: $($latest.Name) $srcSz vs $dstSz"
            }
        } catch {
            Write-Output "OFFSITE BACKUP ERROR ($($latest.Name)): $($_.Exception.Message)"
        }
    }
}
# 异卷保留 14 天
Get-ChildItem $offsiteDir -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$RetentionDays) } | Remove-Item -ErrorAction SilentlyContinue
Write-Output "offsite backup done: $copied/2 artifacts copied to D:\trinity-backups"