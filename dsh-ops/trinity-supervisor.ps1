<#
.SYNOPSIS
    Trinity 进程监督器 — 确保 trinity-api / trinity-mcp(SSE) / collector 常驻。

.DESCRIPTION
    每次运行做一次"检查 → 拉起"：
      - trinity-api   (FastAPI, 端口 8001)：HTTP /health 探测，失败则重启；
      - trinity-mcp   (SSE, 端口 8000)：TCP 端口探测，失败则重启；
      - collector     (python -m trinity.collector status)：STOPPED/STALE 则 start。
    重启带最小间隔保护（同一进程 60s 内最多重启 1 次），日志写入
    .trinity\logs\dsh-supervisor.log。
    建议由计划任务每 5 分钟调用一次（install-dsh-schedules.bat 已注册）。

.EXAMPLE
    .\trinity-supervisor.ps1
    .\trinity-supervisor.ps1 -LogDir C:\temp
#>
[CmdletBinding()]
param(
    [string]$LogDir = "C:\Users\Administrator\.trinity\logs",
    [int]$MinRestartIntervalSec = 60
)

$ErrorActionPreference = "Continue"
$TrinityRoot = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $TrinityRoot ".venv\Scripts\python.exe"
$ApiExe = Join-Path $TrinityRoot ".venv\Scripts\trinity-api.exe"
$SysPy = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
$ApiPy = $SysPy
$McpPy = $SysPy
$StateFile = Join-Path $LogDir "dsh-supervisor-state.json"

# ── 凭证注入：从 ~/.dsh/.credentials.yaml 注入敏感环境变量（未设置时），
#    供 Start-Process 拉起的 api/mcp 子进程继承（继承当前进程环境）。
. (Join-Path $PSScriptRoot "dsh-credentials.ps1")
foreach ($cred in @("TRINITY_PG_HOST", "TRINITY_PG_PORT", "TRINITY_PG_DB", "TRINITY_PG_USER", "TRINITY_PG_PASSWORD", "TRINITY_API_KEY")) {
    if (-not [Environment]::GetEnvironmentVariable($cred, "Process")) {
        $v = Get-DshCredential $cred
        if ($v) { [Environment]::SetEnvironmentVariable($cred, $v, "Process") }
    }
}
# 语义缓存（OPT6 生产开启）：子进程（api/mcp）继承；可用 TRINITY_CACHE_BACKEND=off 关闭
foreach ($cache in @("TRINITY_CACHE_BACKEND", "TRINITY_REDIS_URL", "TRINITY_CACHE_TTL")) {
    if (-not [Environment]::GetEnvironmentVariable($cache, "Process")) {
        if ($cache -eq "TRINITY_CACHE_BACKEND") { [Environment]::SetEnvironmentVariable($cache, "redis", "Process") }
        elseif ($cache -eq "TRINITY_REDIS_URL") { [Environment]::SetEnvironmentVariable($cache, "redis://127.0.0.1:6379/0", "Process") }
        else { [Environment]::SetEnvironmentVariable($cache, "300", "Process") }
    }
}

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Add-Content -Path (Join-Path $LogDir "dsh-supervisor.log") -Value $line -Encoding UTF8
}

function Read-State {
    if (Test-Path $StateFile) {
        try { return Get-Content $StateFile -Raw | ConvertFrom-Json } catch { }
    }
    return @{ restartedAt = @{} }
}

function Save-State($state) {
    try { $state | ConvertTo-Json -Depth 5 | Set-Content $StateFile -Encoding UTF8 } catch { }
}

function Test-Tcp {
    param([int]$Port)
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect("127.0.0.1", $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(2000)
        if ($ok -and $c.Connected) { $c.Close(); return $true }
        $c.Close(); return $false
    } catch { return $false }
}

function Test-ApiHealth {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8001/health" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Start-WithLogs {
    param([string]$Name, [string]$Exe, [string[]]$ArgList)
    $outLog = Join-Path $LogDir "$Name.out.log"
    $errLog = Join-Path $LogDir "$Name.err.log"
    $p = Start-Process -FilePath $Exe -ArgumentList $ArgList -WorkingDirectory $TrinityRoot `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
        -WindowStyle Hidden -PassThru
    Write-Log "$Name started (PID $($p.Id)) -> $outLog"
    return $p
}

$state = Read-State
if (-not $state.restartedAt) { $state.restartedAt = @{} }
$now = Get-Date

function Should-Restart {
    param([string]$Name)
    $last = $state.restartedAt.$Name
    if (-not $last) { return $true }
    $lastTime = [datetime]::Parse($last)
    return (($now - $lastTime).TotalSeconds -ge $MinRestartIntervalSec)
}

# ── 1. API ────────────────────────────────────────────────────────────────
if (-not (Test-ApiHealth)) {
    if (Should-Restart "api") {
        Write-Log "api DOWN (health probe failed) — restarting" "WARN"
        Start-WithLogs -Name "api" -Exe $ApiPy -ArgList @("-m", "trinity.api.server", "--port", "8001")
        $state.restartedAt.api = $now.ToString("o")
    } else {
        Write-Log "api DOWN but within restart interval — skipped" "WARN"
    }
} else {
    Write-Log "api OK"
}

# ── 2. MCP (SSE) ──────────────────────────────────────────────────────────
if (-not (Test-Tcp -Port 8000)) {
    if (Should-Restart "mcp") {
        Write-Log "mcp DOWN (port 8000 closed) — restarting" "WARN"
        Start-WithLogs -Name "mcp" -Exe $McpPy -ArgList @("-m", "trinity.mcp.server", "--mode", "sse", "--port", "8000", "--host", "127.0.0.1")
        $state.restartedAt.mcp = $now.ToString("o")
    } else {
        Write-Log "mcp DOWN but within restart interval — skipped" "WARN"
    }
} else {
    Write-Log "mcp OK (port 8000 open)"
}

# ── 3. Collector ──────────────────────────────────────────────────────────
# 注意：collector 依赖 PyYAML 等包，须用系统 Python（venv 缺少这些依赖）。
if (Test-Path $SysPy) {
    $out = & $SysPy -m trinity.collector status 2>&1 | Out-String
    if ($out -match "RUNNING") {
        Write-Log "collector OK"
    } else {
        if (Should-Restart "collector") {
            Write-Log "collector not RUNNING — starting: $($out.Trim())" "WARN"
            & $SysPy -m trinity.collector start 2>&1 | Out-String | Write-Log
            $state.restartedAt.collector = $now.ToString("o")
        } else {
            Write-Log "collector DOWN but within restart interval — skipped" "WARN"
        }
    }
} else {
    Write-Log "collector check skipped (system python not found at $SysPy)" "WARN"
}

Save-State $state
Write-Log "supervisor pass complete"
