<#
.SYNOPSIS
    Trinity DSH 自启循环 — 无需管理员权限的"计划任务"替代品。

.DESCRIPTION
    以隐藏窗口常驻循环，提供三个周期：
      - 每 5 分钟：跑一次 trinity-supervisor.ps1（api/mcp/collector 监督）；
      - 每 4 小时：跑一次维护 health,evolution,session-auto（进化完整周期 + 会话自动沉淀）；
      - 每日 03:00-03:10：跑一次维护 decay,tiers,sync（需 PostgreSQL）。
    由 install-autostart.bat 生成的 Startup VBS 在用户登录时自动启动；
    退出登录即停止（与旧 StartUp VBS 方案相同）。若已用管理员注册了
    install-dsh-schedules.bat 的计划任务，则本循环可不用（避免重复执行）。
    日志：.trinity\logs\dsh-autostart.log
#>
[CmdletBinding()]
param(
    [int]$SupervisorIntervalSec = 300,
    [int]$MaintIntervalSec = 14400,
    [string]$LogDir = "C:\Users\Administrator\.trinity\logs"
)

$ErrorActionPreference = "Continue"
# 路径修复（2026-08-15）：本脚本与 supervisor/maintenance 同处 dsh-ops，
# 必须用 $PSScriptRoot 定位；此前用父目录 $OpsDir=trinity 解析成
# trinity\trinity-supervisor.ps1（实际在 trinity\dsh-ops\），Test-Path 恒 False，
# 循环自 2026-08-14 20:23 起静默空转、从不执行监督/维护。
$Supervisor = Join-Path $PSScriptRoot "trinity-supervisor.ps1"
$Maintenance = Join-Path $PSScriptRoot "trinity-dsh-maintenance.ps1"
$LogFile = Join-Path $LogDir "dsh-autostart.log"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

# 存储统一（EXECUTION 31，双库修复双保险）：显式锚定权威大库路径，
# 由 Invoke-Script 拉起的维护脚本子进程继承，杜绝 cwd 兜底产生小库。
$env:TRINITY_STORE = "D:	rinity-data\store"  # 2026-09 迁移 D 盘后权威库

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch { }
}

function Invoke-Script {
    param([string]$Path, [string[]]$ArgsList, [string]$Label, [int]$TimeoutSec = 600)
    try {
        # 2026-08-15 修复：改用文件重定向 + Wait-Process 超时，
        # 避免 `& ... 2>&1` 管道被孙进程句柄持有导致父循环永久卡死
        # （实测：循环自 2026-08-14 20:23 起卡在首轮监督，15h 未迭代）。
        $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $outFile = Join-Path $LogDir "invoke-$Label-$stamp.out.log"
        $errFile = Join-Path $LogDir "invoke-$Label-$stamp.err.log"
        # 注意：$ArgsList 可能为 null（如 supervisor 无参数），直接拼进数组会产生
        # null 元素，Start-Process -ArgumentList 校验会抛错——先过滤再拼接。
        $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$Path`"")
        if ($ArgsList -and $ArgsList.Count -gt 0) { $argList += $ArgsList }
        $child = Start-Process -FilePath "powershell.exe" -ArgumentList $argList -WindowStyle Hidden -RedirectStandardOutput $outFile -RedirectStandardError $errFile -PassThru
        try { Wait-Process -Id $child.Id -Timeout $TimeoutSec -ErrorAction Stop } catch {
            Write-Log "$Label timed out ($TimeoutSec) — killing" "WARN"
            Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
        }
        $tail = if (Test-Path $outFile) { Get-Content $outFile -Tail 1 -ErrorAction SilentlyContinue } else { $null }
        Write-Log "$Label done: $tail"
    } catch {
        Write-Log "$Label error: $_" "WARN"
    }
}

$lastMaint = (Get-Date).AddHours(-25)
$lastDaily = ""
$lastWeekly = ""  # 2026-09-01: 每周质量门禁
$lastWeeklyAcc = ""  # 2026-09-01: 每周 AnswerAcc 评测

Write-Log "autostart loop started (supervisor=${SupervisorIntervalSec}s, maint=${MaintIntervalSec}s)"

# 2026-08-29 (PG main storage): ensure portable PG on 5432
if (-not (Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue)) {
    $pgbin = "C:\Users\Administrator\Desktop\pgsql\bin"
    $pgdata = "C:\Users\Administrator\.trinity\pgdata"
    if (Test-Path "$pgbin\pg_ctl.exe" -and (Test-Path "$pgdata\PG_VERSION")) {
        Start-Process -FilePath "$pgbin\pg_ctl.exe" -ArgumentList @("start","-D",$pgdata,"-l","$pgdata\pg.log") -WindowStyle Hidden
        Start-Sleep 3
    }
}

while ($true) {
    $now = Get-Date

    # ── 每 5 分钟：监督 ────────────────────────────────────────
    if (Test-Path $Supervisor) { Invoke-Script -Path $Supervisor -Label "supervisor" }

    # ── SQLite 锁看门狗(2026-08-16):持续锁占用时自动清理 ──
    if (Test-Path (Join-Path $PSScriptRoot "trinity-lock-watchdog.ps1")) { Invoke-Script -Path (Join-Path $PSScriptRoot "trinity-lock-watchdog.ps1") -Label "lock-watchdog" }

    # ── 每 4 小时：health + evolution（进化完整周期）────────────
    if ((Test-Path $Maintenance) -and (($now - $lastMaint).TotalSeconds -ge $MaintIntervalSec)) {
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "health,evolution,session-auto") -Label "maintenance(health,evolution)"
        $lastMaint = $now
    }

    # ── 每日 03:00-03:10：decay + tiers + sync（需 PG）──────────
    $today = $now.ToString("yyyyMMdd")
    if ((Test-Path $Maintenance) -and $now.Hour -eq 3 -and $now.Minute -lt 10 -and $lastDaily -ne $today) {
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "pg-backfill,mirror,decay,tiers,consolidate,dedup,pg-sync,sync,compact,agent-ttl,active-health,backup,observe,value-recalib,perception-bridge,dcpm-consolidate,integrity-monitor,self-reflect,reconcile,snapshot,market-list,replay,curiosity,proactive,cognition-agent,situation,opsbot-cycle,perception-continuous,expiry-review") -Label "maintenance(decay,tiers,sync)"  # 2026-09-01: +market-list -TimeoutSec 2400  # 2026-09-01: pg-backfill 前置；decay/tiers 走 PG；snapshot=AGENTS.md 快照刷新
        $lastDaily = $today
    }

    # ── 每周一 03:10-03:30：质量门禁 + 插件冒烟（2026-09-01）────────
    if ((Test-Path $Maintenance) -and $now.DayOfWeek -eq 'Monday' -and $now.Hour -eq 3 -and $now.Minute -ge 10 -and $now.Minute -lt 30 -and $lastWeekly -ne $today) {
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "quality-gate,plugin-smoke,brain-report") -Label "maintenance(quality-gate)" -TimeoutSec 1500  # 2026-09-01: +brain-report 周报
        $lastWeekly = $today
    }

    # ── 每周日 03:10-03:40：AnswerAcc 生成侧评测（500q LLM，20-30 分钟，2026-09-01）────────
    if ((Test-Path $Maintenance) -and $now.DayOfWeek -eq 'Sunday' -and $now.Hour -eq 3 -and $now.Minute -ge 10 -and $now.Minute -lt 40 -and $lastWeeklyAcc -ne $today) {
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "answer-eval") -Label "maintenance(answer-eval)" -TimeoutSec 2400
        $lastWeeklyAcc = $today
    }

    # ── 每 30 分钟：持续感知流（EXECUTION 458 P1-2；marker 文件防抖）──
    $percMark = Join-Path $LogDir "perception-loop.mark"
    $percDue = $false
    if (-not (Test-Path $percMark)) { $percDue = $true }
    elseif (((Get-Date) - (Get-Item $percMark).LastWriteTime).TotalMinutes -ge 30) { $percDue = $true }
    if ($percDue -and (Test-Path $Maintenance)) {
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "perception-continuous") -Label "maintenance(perception)"
        Set-Content -Path $percMark -Value (Get-Date -Format o)
    }

    Start-Sleep -Seconds $SupervisorIntervalSec
}
