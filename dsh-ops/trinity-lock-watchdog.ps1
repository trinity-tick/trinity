# Trinity SQLite 锁看门狗(2026-08-16)
# 检测 ~/.trinity/store/trinity_store.db 写锁持续占用,自动清理:
#   行动1: kill engine_worker(插件会自动 reconnect,安全)
#   行动2: 仍锁则 kill 全部 trinity python(api/mcp/collector 由 supervisor 5 分钟内重启)
param(
    [string]$LogDir = "C:\Users\Administrator\.trinity\logs"
)

$ErrorActionPreference = "Continue"
$SysPy = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
$LockDiag = Join-Path $LogDir "lock_diag.py"
$Log = Join-Path $LogDir "dsh-lock-watchdog.log"

function Write-Log { param([string]$Msg) "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Msg" | Out-File $Log -Append -Encoding UTF8 }

if (-not (Test-Path $LockDiag)) { exit 0 }

# 1) 持续锁检测(3 次, 每次间隔 2s)
$locked = 0
for ($i = 0; $i -lt 3; $i++) {
    $res = & $SysPy $LockDiag 2>&1 | Out-String
    if ($res -match "LOCKED") { $locked++ }
    Start-Sleep -Seconds 2
}
if ($locked -gt 0) { Write-Log "lock detected: $locked/3 rounds (below cleanup threshold or cleared)" }
if ($locked -lt 3) { exit 0 }

Write-Log "LOCKED x$locked - starting cleanup"

# 2) 行动1: kill engine_worker + trinity-mcp(客户端会重连;stdio 是反复锁源之一)
$wk = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { ($_.CommandLine -match "engine_worker" -or $_.CommandLine -match "trinity-mcp") -and $_.ProcessId -ne $PID }
foreach ($p in $wk) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; Write-Log "killed engine_worker PID $($p.ProcessId)" }
Start-Sleep -Seconds 3
$res2 = & $SysPy $LockDiag 2>&1 | Out-String
if ($res2 -match "AVAILABLE") { Write-Log "unlocked after worker kill"; exit 0 }

# 3) 行动2: kill 全部 trinity python(由 supervisor 重启)
$all = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -match "trinity" -and $_.ProcessId -ne $PID }
foreach ($p in $all) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; Write-Log "killed trinity python PID $($p.ProcessId)" }
Start-Sleep -Seconds 3
$res3 = & $SysPy $LockDiag 2>&1 | Out-String
if ($res3 -match "AVAILABLE") { Write-Log "unlocked after full kill (supervisor will restart services)" } else { Write-Log "STILL LOCKED after full kill - manual intervention needed" }