<#
.SYNOPSIS
    Trinity 实时监测仪表盘（系统 + 数据 + 日志一体化）

.DESCRIPTION
    循环刷新展示：
      [系统] API/MCP/Gateway/docker 栈端口、API 健康与降级等级、关键进程
      [数据] 引擎库 memories/dsh_events/聚合池 增量、租约运行中状态
      [日志] 最近维护/监督日志尾部（错误提示）
      [资源] CPU 负载、内存、库文件大小
    用法: powershell -File dsh-ops/trinity-monitor.ps1 -Interval 5 [-Rounds 0(无限)] [-Simple]
    依赖: 系统 Python（库规模/租约查询）+ Test-NetConnection（端口）
#>
param(
    [int]$Interval = 5,
    [int]$Rounds = 0,          # 0 = 无限
    [switch]$Simple            # 简单模式（无颜色/无分区线）
)
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$Py = 'C:/Users/Administrator/AppData/Local/Programs/Python/Python314/python.exe'
if (-not (Test-Path $Py)) { throw "system python not found: $Py" }

$Ports = @(
    @{ N = 'api 8001';        P = 8001 },
    @{ N = 'mcp 8000';        P = 8000 },
    @{ N = 'gateway 8002';    P = 8002 },
    @{ N = 'pg 5430';         P = 5430 },
    @{ N = 'docker-api 8005'; P = 8005 },
    @{ N = 'docker-mcp 8006'; P = 8006 }
)
$Procs = @('trinity.api.server', 'trinity.mcp.server', 'collector', 'gateway', 'engine_worker')
$LogDir = 'C:\Users\Administrator\.trinity\logs'

function Test-Port2($port) {
    $c = Test-NetConnection -ComputerName 127.0.0.1 -Port $port -WarningAction SilentlyContinue -InformationLevel Quiet
    return $c
}
function Write-Status($ok) { if ($Simple) { return '' } return $(if ($ok) { 'OK ' } else { 'DOWN' }) }

$pythonProbe = @'
import sqlite3, os, json, time
db = os.path.expanduser('~/.trinity/store/trinity_store.db')
out = {}
try:
    c = sqlite3.connect('file:' + db + '?mode=ro', uri=True)
    out['memories'] = c.execute('select count(*) from memories').fetchone()[0]
    out['dsh_events'] = c.execute('select count(*) from dsh_events').fetchone()[0]
    try:
        out['running_leases'] = [r[0] for r in c.execute(
            "select job_kind from governance_jobs where status='running' and lease_expires_at > ?", (time.time(),))]
    except Exception:
        out['running_leases'] = []
    out['db_bytes'] = os.path.getsize(db)
    c.close()
except Exception as e:
    out['error'] = str(e)
pool = os.path.expanduser('~/trinity/data/aggregator_pool.json')
try:
    out['pool'] = len(json.load(open(pool, encoding='utf-8')).get('memories', []))
except Exception:
    out['pool'] = None
print(json.dumps(out))
'@

$health = $null
$degradation = '-'
try {
    $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8001/health' -TimeoutSec 5
    $health = $h.status
    $degradation = $h.degradation.tier
} catch { $health = 'DOWN' }

$round = 0
while ($true) {
    $round++
    if ($Rounds -gt 0 -and $round -gt $Rounds) { break }
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

    # ── 数据探测 ──
    $probe = $pythonProbe | & $Py - | ConvertFrom-Json
    $curMem = $probe.memories; $curEv = $probe.dsh_events; $curPool = $probe.pool
    $leases = $probe.running_leases; $dbBytes = $probe.db_bytes

    # ── 日志错误扫描 ──
    $logErr = ''
    $mlog = Join-Path $LogDir 'dsh-maintenance.log'
    if (Test-Path $mlog) {
        $tail = Get-Content $mlog -Tail 5 -ErrorAction SilentlyContinue
        $bad = $tail | Where-Object { $_ -match 'FAILED|WARN|ERROR' }
        if ($bad) { $logErr = ($bad | Select-Object -Last 1) }
    }

    # ── 输出 ──
    if (-not $Simple) { Write-Host ('=' * 78) -ForegroundColor DarkGray }
    Write-Host "[$ts] round $round  API=$health (tier=$degradation)" -ForegroundColor $(if ($health -eq 'ok') { 'Green' } else { 'Red' })
    if (-not $Simple) { Write-Host '-- 服务端口 --' -ForegroundColor Cyan }
    foreach ($s in $Ports) {
        $ok = Test-Port2 $s.P
        $fg = $(if ($ok) { 'Green' } else { 'Red' })
        Write-Host ("  {0,-16} {1}" -f $s.N, $(if ($ok) { 'UP' } else { 'DOWN' })) -ForegroundColor $fg
    }
    if (-not $Simple) { Write-Host '-- 进程 --' -ForegroundColor Cyan }
    $found = @()
    foreach ($pn in $Procs) {
        $hit = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match [regex]::Escape($pn) }
        if ($hit) { $found += "$pn($($hit.ProcessId))" }
    }
    Write-Host ("  存活: {0}" -f $(if ($found) { $found -join ', ' } else { '(无)' })) -ForegroundColor $(if ($found) { 'Green' } else { 'Yellow' })
    if (-not $Simple) { Write-Host '-- 数据 --' -ForegroundColor Cyan }
    Write-Host ("  memories={0} (+{1})  dsh_events={2} (+{3})  pool={4}" -f `
        $curMem, $(if ($script:pm) { $curMem - $script:pm } else { 0 }), `
        $curEv, $(if ($script:pe) { $curEv - $script:pe } else { 0 }), $curPool)
    if ($leases.Count -gt 0) {
        Write-Host ("  运行中租约: {0}" -f ($leases -join ', ')) -ForegroundColor Yellow
    } else {
        Write-Host '  运行中租约: 无' -ForegroundColor DarkGray
    }
    Write-Host ("  库文件: {0:N1} MB" -f ($dbBytes / 1MB)) -ForegroundColor DarkGray
    if (-not $Simple) { Write-Host '-- 日志 --' -ForegroundColor Cyan }
    if ($logErr) { Write-Host ("  ⚠ {0}" -f $logErr.Substring(0, [Math]::Min(110, $logErr.Length))) -ForegroundColor Yellow }
    else { Write-Host '  最近维护日志: 无 FAILED/WARN' -ForegroundColor DarkGray }
    $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    $memPct = (Get-CimInstance Win32_OperatingSystem | ForEach-Object { [math]::Round(100 * (1 - $_.FreePhysicalMemory / $_.TotalVisibleMemorySize), 0) })
    if (-not $Simple) { Write-Host '-- 资源 --' -ForegroundColor Cyan }
    Write-Host ("  CPU {0}%  MEM {1}%" -f [math]::Round($cpu, 0), $memPct)

    $script:pm = $curMem; $script:pe = $curEv
    if ($Rounds -gt 0) { Write-Host ("[剩余 $($Rounds - $round) 轮]") -ForegroundColor DarkGray }
    if ($round -lt $Rounds -or $Rounds -eq 0) { Start-Sleep -Seconds $Interval }
}
