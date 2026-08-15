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
$env:TRINITY_STORE = Join-Path $env:USERPROFILE ".trinity\store"

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch { }
}

function Invoke-Script {
    param([string]$Path, [string[]]$ArgsList, [string]$Label)
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
        try { Wait-Process -Id $child.Id -Timeout 600 -ErrorAction Stop } catch {
            Write-Log "$Label timed out (600s) — killing" "WARN"
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
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "mirror,decay,tiers,sync") -Label "maintenance(decay,tiers,sync)"
        $lastDaily = $today
    }

    Start-Sleep -Seconds $SupervisorIntervalSec
}
