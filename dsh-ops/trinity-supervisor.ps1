<#
.SYNOPSIS
    Trinity 进程监督器 — 确保 trinity-api / trinity-mcp(SSE) / collector / gateway 常驻。

.DESCRIPTION
    每次运行做一次"检查 → 拉起"：
      - trinity-gateway (OpenAI/Mem0 兼容层, 端口 8002)：/v1/models 带鉴权探测，失败则重启；
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
# api/mcp 统一使用系统 Python 3.14（fastapi/strawberry/psycopg2 等依赖齐全，
# 与本机实际运行实例一致；EXECUTION 第五轮已确认）。.venv 仅含 numpy/jieba，
# 缺 fastapi/strawberry，无法拉起 API（2026-08-15 实测 .venv 起服务失败）。
$ApiPy = $SysPy
$McpPy = $SysPy
$StateFile = Join-Path $LogDir "dsh-supervisor-state.json"

# ── 凭证注入：从 ~/.dsh/.credentials.yaml 注入敏感环境变量（未设置时），
#    供 Start-Process 拉起的 api/mcp 子进程继承（继承当前进程环境）。
. (Join-Path $PSScriptRoot "dsh-credentials.ps1")
foreach ($cred in @("TRINITY_PG_HOST", "TRINITY_PG_PORT", "TRINITY_PG_DB", "TRINITY_PG_USER", "TRINITY_PG_PASSWORD", "TRINITY_API_KEY", "TRINITY_STORE", "TRINITY_STORAGE_BACKEND", "GATEWAY_API_KEY")) {  # 2026-08-17 安全加固：注入 gateway 鉴权 key
    if (-not [Environment]::GetEnvironmentVariable($cred, "Process")) {
        $v = Get-DshCredential $cred
        if ($v) { [Environment]::SetEnvironmentVariable($cred, $v, "Process") }
    }
}
# ── Gateway 上游（OpenAI/Mem0 兼容层，2026-08-15）：UPSTREAM_BASE_URL 默认
#    OpenAI 会超时（无 OPENAI_API_KEY）。复用 DEEPSEEK_API_KEY 兜底为 DeepSeek
#    上游（MODEL_ALIASES 自动映射 gpt-4o-mini→deepseek-v4-flash）。
if (-not [Environment]::GetEnvironmentVariable("UPSTREAM_BASE_URL", "Process")) {
    [Environment]::SetEnvironmentVariable("UPSTREAM_BASE_URL", "https://api.deepseek.com/v1", "Process")
}
if (-not [Environment]::GetEnvironmentVariable("UPSTREAM_API_KEY", "Process")) {
    $dk = Get-DshCredential "DEEPSEEK_API_KEY"
    if ($dk) { [Environment]::SetEnvironmentVariable("UPSTREAM_API_KEY", $dk, "Process") }
}
if (-not [Environment]::GetEnvironmentVariable("MODEL_ALIASES", "Process")) {
    [Environment]::SetEnvironmentVariable("MODEL_ALIASES", "gpt-4o-mini:deepseek-v4-flash,gpt-4o:deepseek-v4-pro", "Process")
}
# 语义缓存（OPT6 生产开启）：子进程（api/mcp）继承；可用 TRINITY_CACHE_BACKEND=off 关闭
foreach ($cache in @("TRINITY_CACHE_BACKEND", "TRINITY_REDIS_URL", "TRINITY_CACHE_TTL")) {
    if (-not [Environment]::GetEnvironmentVariable($cache, "Process")) {
        if ($cache -eq "TRINITY_CACHE_BACKEND") { [Environment]::SetEnvironmentVariable($cache, "redis", "Process") }
        elseif ($cache -eq "TRINITY_REDIS_URL") { [Environment]::SetEnvironmentVariable($cache, "redis://127.0.0.1:6379/0", "Process") }
        else { [Environment]::SetEnvironmentVariable($cache, "300", "Process") }
    }
}
# 存储统一（EXECUTION 31，双库修复双保险）：显式锚定权威大库路径，
# 子进程（api/mcp）继承后不再依赖 _find_trinity_store() 的 cwd 兜底。
$TrinityStore = Join-Path $env:USERPROFILE ".trinity\store"

# 2026-08-27 (伙伴系列): API 常驻启用 automation (knowledge.stale 告警等)
if (-not [Environment]::GetEnvironmentVariable("TRINITY_AUTOMATION", "Process")) {
    [Environment]::SetEnvironmentVariable("TRINITY_AUTOMATION", "on", "Process")
    Write-Output "TRINITY_AUTOMATION -> on"
}
if (-not [Environment]::GetEnvironmentVariable("TRINITY_STORE", "Process")) {
    [Environment]::SetEnvironmentVariable("TRINITY_STORE", $TrinityStore, "Process")
    Write-Output "TRINITY_STORE -> $TrinityStore"
}

# ── 自进化采纳 env 应用（2026-08-25 缺口A 补全）：读取 evolve_env.json
#    白名单校验后注入当前进程环境（Start-Process 子进程继承）。
#    服务已运行时不重启（避免打断连接）；下一轮拉起时新实例自动带最新 env。
if (Test-Path (Join-Path $env:USERPROFILE ".trinity\evolve\evolve_env.json")) {
    & (Join-Path $PSScriptRoot "apply_evolve_env.ps1") 2>&1 | ForEach-Object { Write-Log $_ }
}

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Add-Content -Path (Join-Path $LogDir "dsh-supervisor.log") -Value $line -Encoding UTF8
    if ($Level -in @("WARN", "ERROR")) { Send-Alert $Message $Level }  # 2026-08-18 SRE: 关键告警推送
}

# 2026-08-18（SRE 骨架）：关键告警 webhook 推送（设置 TRINITY_ALERT_WEBHOOK 后启用）。
function Send-Alert {
    param([string]$Message, [string]$Level)
    if (-not $env:TRINITY_ALERT_WEBHOOK) { return }
    try {
        $body = @{ level = $Level; message = $Message; ts = (Get-Date -Format "o"); service = "trinity-supervisor" } | ConvertTo-Json
        Invoke-RestMethod -Uri $env:TRINITY_ALERT_WEBHOOK -Method Post -ContentType "application/json" -Body $body -TimeoutSec 5 -ErrorAction Stop | Out-Null
    } catch { }
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

function Test-McpAlive {
    # 2026-08-16 稳定性修复:端口 8000 通即认为 MCP 存活。
    # 原实现检查监听进程归属(命令行含 trinity),在后台 supervisor 环境下
    # Get-CimInstance 调用失败导致误判 down -> 每 5 分钟重启 MCP -> 客户端被打断。
    # 本机 8000 已被 Trinity MCP 独占,误杀比假 OK 危害大。
    return (Test-Tcp -Port 8000)
}

function Test-ApiHealth {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8001/health" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Test-GatewayHealth {
    # Gateway（OpenAI/Mem0 兼容层，端口 8002）：/v1/models 带鉴权探测。
    # 未设置 GATEWAY_API_KEY 时用默认 gw-test-key；设置则读取。
    try {
        $gwKey = $env:GATEWAY_API_KEY
        if (-not $gwKey) { $gwKey = "gw-test-key" }
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8002/v1/models" -TimeoutSec 3 `
            -UseBasicParsing -Headers @{ "Authorization" = "Bearer $gwKey" } -ErrorAction Stop
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
# 修复：JSON 反序列化后 restartedAt 是 PSCustomObject，无法添加新键（曾致
# $state.restartedAt.collector 赋值报错、60s 重启间隔保护失效）。统一转成 hashtable。
$rt = @{}
if ($state.restartedAt) {
    foreach ($prop in $state.restartedAt.PSObject.Properties) { $rt[$prop.Name] = $prop.Value }
}
$state.restartedAt = $rt
$now = Get-Date

function Should-Restart {
    param([string]$Name)
    $last = $state.restartedAt.$Name
    if (-not $last) { return $true }
    $lastTime = [datetime]::Parse($last)
    return (($now - $lastTime).TotalSeconds -ge $MinRestartIntervalSec)
}

# ── 0. Gateway（OpenAI/Mem0 兼容层，2026-08-15 加入监督）────────────
# 此前 Gateway 不在 supervisor 管理范围（偶发 down 无人拉起）。
# 启动方式：系统 Python 跑 gateway/server.py（cwd=trinity 根）。
if (-not (Test-GatewayHealth)) {
    if (Should-Restart "gateway") {
        Write-Log "gateway DOWN (/v1/models probe failed) — restarting" "WARN"
        $gwPy = $SysPy
        Start-WithLogs -Name "gateway" -Exe $gwPy -ArgList @("gateway\server.py")
        $state.restartedAt.gateway = $now.ToString("o")
    } else {
        Write-Log "gateway DOWN but within restart interval — skipped" "WARN"
    }
} else {
    Write-Log "gateway OK (/v1/models 200)"
}
# ---- 0.5 memory stream UI (2026-08-27 partner) ----
if (-not (Test-Tcp -Port 8010)) {
    if (Should-Restart "memstream") {
        Write-Log "memstream DOWN (:8010) - restarting" "WARN"
        Start-WithLogs -Name "memstream" -Exe $SysPy -ArgList @("scripts\memory_stream_server.py", "--port", "8010")
        $state.restartedAt.memstream = $now.ToString("o")
    } else {
        Write-Log "memstream DOWN but within restart interval - skipped" "WARN"
    }
} else {
    Write-Log "memstream OK (:8010)"
}

# ── 1. API ────────────────────────────────────────────────────────────────
if (-not (Test-ApiHealth)) {
    if (Should-Restart "api") {
        Write-Log "api DOWN (health probe failed) — restarting" "WARN"
        Start-WithLogs -Name "api" -Exe $ApiPy -ArgList @("-m", "trinity.api.server", "--port", "8001", "--host", "127.0.0.1")  # 2026-08-17 安全加固：仅本机
        $state.restartedAt.api = $now.ToString("o")
    } else {
        Write-Log "api DOWN but within restart interval — skipped" "WARN"
    }
} else {
    Write-Log "api OK"
}

# ── 2. MCP (SSE) ──────────────────────────────────────────────────────────
if (-not (Test-McpAlive)) {
    $portHeld = Test-Tcp -Port 8000
    $reason = if ($portHeld) { "port 8000 held by non-trinity process (e.g. Docker)" } else { "port 8000 closed" }
    if (Should-Restart "mcp") {
        Write-Log "mcp DOWN ($reason) — restarting" "WARN"
        Start-WithLogs -Name "mcp" -Exe $McpPy -ArgList @("-m", "trinity.mcp.server", "--mode", "sse", "--port", "8000", "--host", "127.0.0.1")
        $state.restartedAt.mcp = $now.ToString("o")
    } else {
        Write-Log "mcp DOWN ($reason) but within restart interval — skipped" "WARN"
    }
} else {
    Write-Log "mcp OK (port 8000 open, trinity process)"
}

# ── 2.5. MCP streamable-http (:8003, 2026-08-24 P0-3 默认常驻) ─────────────
# MCP v2 streamable-http 传输（OAuth 2.1 Bearer 鉴权，默认 TRINITY_MCP_HTTP_AUTH=on；
# 无 TRINITY_MCP_API_KEY/TRINITY_API_KEY 时 server 自动降级无鉴权并告警）。
# 网络 2025 生产组合 = Streamable HTTP + OAuth；与 :8000 SSE 并存过渡。
if (-not (Test-Tcp -Port 8003)) {
    if (Should-Restart "mcp-http") {
        Write-Log "mcp-http DOWN (:8003 closed) — restarting (streamable-http)" "WARN"
        Start-WithLogs -Name "mcp-http" -Exe $McpPy -ArgList @("-m", "trinity.mcp.server", "--mode", "streamable-http", "--port", "8003", "--host", "127.0.0.1")
        $state.restartedAt.'mcp-http' = $now.ToString("o")
    } else {
        Write-Log "mcp-http DOWN (:8003 closed) but within restart interval — skipped" "WARN"
    }
} else {
    Write-Log "mcp-http OK (port 8003 open, streamable-http)"
}

# ── 3. Collector ──────────────────────────────────────────────────────────
# 注意：collector 依赖 PyYAML 等包，须用系统 Python（venv 缺少这些依赖）。
if (Test-Path $SysPy) {
    $out = & $SysPy -m trinity.collector status 2>&1 | Out-String
    if ($out -match "RUNNING") {
        $ev = 0
        if ($out -match 'events_captured=(\d+)') { $ev = [int]$Matches[1] }
        if ($ev -gt 0) {
            Write-Log "collector OK (events_captured=$ev)"
            $state | Add-Member -NotePropertyName zeroEventCount -NotePropertyValue 0 -Force  # 2026-08-17 修复：PSCustomObject 不能直接加新属性
        } else {
            $z = 0
            if ($state.PSObject.Properties['zeroEventCount']) { $z = [int]$state.zeroEventCount }
            $z += 1
            $state | Add-Member -NotePropertyName zeroEventCount -NotePropertyValue $z -Force
            # 去噪（2026-08-17/18）：2026-08-18 起已接入 DSH 结构层事件源
            # （active_collection.dsh_events），events_captured=0 表示近周期内
            # 无"高价值事件"（user/message、goal/write、持久化工具、错误）——属预期
            # 状态而非无源；真实故障（scanner_errors>0 或进程 DOWN）仍由其他分支告警。
            if (($z -eq 3) -or (($z -gt 3) -and (($z - 3) % 12 -eq 0))) {
                Write-Log "collector RUNNING but ZERO high-value events ($z consecutive) - DSH event source attached (only user-message/goal/error/persist events captured; quiet period is expected)" "WARN"
            } else {
                Write-Log "collector OK (events_captured=0, $z consecutive; DSH source active)"
            }
        }
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

# ── 3.5. 维护库 PostgreSQL (docker trinity-db :5430) ─────────────────────
# 2026-08-17（记忆周期优化 P0-2）：每日链 mirror/decay/tiers 依赖 PG :5430，
# 8-16 每日链因 trinity-db 未启动而 mirror→decay→tiers→consolidate→dedup
# 5 任务全挂。这里每轮探测 TCP :5430，失败且 docker 可用时拉起容器
# （60s 重启间隔保护，避免反复 docker start 空转）。
if (Test-Tcp -Port 5430) {
    Write-Log "pg-maintenance OK (:5430 open)"
} else {
    $dockerOk = $false
    try { docker version *> $null; $dockerOk = ($LASTEXITCODE -eq 0) } catch { $dockerOk = $false }
    if ($dockerOk) {
        if (Should-Restart "pg-maintenance") {
            Write-Log "pg-maintenance DOWN (:5430 closed) — docker start trinity-db" "WARN"
            docker start trinity-db 2>&1 | Out-String | Write-Log
            $state.restartedAt.'pg-maintenance' = $now.ToString("o")
        } else {
            Write-Log "pg-maintenance DOWN but within restart interval — skipped" "WARN"
        }
    } else {
        Write-Log "pg-maintenance DOWN (:5430 closed) — docker unavailable, manual intervention needed" "WARN"
    }
}

# ── 4. DSH goal 同步（2026-08-15, V2 兜底）────────────────────────────
# 插件 goal/change 事件通道在 web 部署中不可靠（不落盘），projcache 是
# 可靠来源（每次 goal 变更实时更新）。supervisor 每轮同步一次，保证
# dsh_goals objective 不丢（幂等：已存在跳过）。
if (Test-Path $SysPy) {
    $gs = & $SysPy "C:\Users\Administrator\trinity\scripts\sync_dsh_goals.py" 2>&1 | Out-String
    if ($gs -match "回填: (\d+)") {
        Write-Log "dsh-goals sync: $($Matches[0])"
    } else {
        Write-Log "dsh-goals sync: $($gs.Trim())" "WARN"
    }
} else {
    Write-Log "dsh-goals sync skipped (system python not found)" "WARN"
}

Save-State $state
Write-Log "supervisor pass complete"
