<#
.SYNOPSIS
    Trinity DSH 自启循环 — 无需管理员权限的"计划任务"替代品。

.DESCRIPTION
    以隐藏窗口常驻循环，提供三个周期：
      - 每 5 分钟：跑一次 trinity-supervisor.ps1（api/mcp/collector 监督）；
      - 每 4 小时：跑一次维护 health,evolution（进化完整周期）；
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
$OpsDir = Split-Path -Parent $PSScriptRoot
$Supervisor = Join-Path $OpsDir "trinity-supervisor.ps1"
$Maintenance = Join-Path $OpsDir "trinity-dsh-maintenance.ps1"
$LogFile = Join-Path $LogDir "dsh-autostart.log"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch { }
}

function Invoke-Script {
    param([string]$Path, [string[]]$ArgsList, [string]$Label)
    try {
        $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $Path @ArgsList 2>&1
        Write-Log "$Label done (exit $LASTEXITCODE): $($out | Select-Object -Last 1)"
    } catch {
        Write-Log "$Label error: $_" "WARN"
    }
}

$lastMaint = (Get-Date).AddHours(-25)
$lastDaily = ""

Write-Log "autostart loop started (supervisor=${SupervisorIntervalSec}s, maint=${MaintIntervalSec}s)"

while ($true) {
    $now = Get-Date

    # ── 每 5 分钟：监督 ────────────────────────────────────────
    if (Test-Path $Supervisor) { Invoke-Script -Path $Supervisor -Label "supervisor" }

    # ── 每 4 小时：health + evolution（进化完整周期）────────────
    if ((Test-Path $Maintenance) -and (($now - $lastMaint).TotalSeconds -ge $MaintIntervalSec)) {
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "health,evolution") -Label "maintenance(health,evolution)"
        $lastMaint = $now
    }

    # ── 每日 03:00-03:10：decay + tiers + sync（需 PG）──────────
    $today = $now.ToString("yyyyMMdd")
    if ((Test-Path $Maintenance) -and $now.Hour -eq 3 -and $now.Minute -lt 10 -and $lastDaily -ne $today) {
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "decay,tiers,sync") -Label "maintenance(decay,tiers,sync)"
        $lastDaily = $today
    }

    Start-Sleep -Seconds $SupervisorIntervalSec
}
