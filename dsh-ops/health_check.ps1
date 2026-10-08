# Trinity × DSH 集成链路健康检查脚本
# 用法: powershell -ExecutionPolicy Bypass -File dsh-ops\health_check.ps1
# 检查: Trinity API(8001) / MCP(8000) / Collector / DSH Web(3080) / engine_worker

$ErrorActionPreference = "SilentlyContinue"
$TRINITY_HOME = "C:\Users\Administrator\trinity"
$results = @()

function Test-Http($name, $url) {
    try {
        $r = Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing
        return [PSCustomObject]@{ Name = $name; Status = "UP"; Detail = "HTTP $($r.StatusCode)" }
    } catch {
        return [PSCustomObject]@{ Name = $name; Status = "DOWN"; Detail = $_.Exception.Message }
    }
}

function Test-DshWeb($name, $port) {
    # 2026-10-06 修正：DSH 0.1.7-rc.2 的 web 没有 /health 路由（实测 /health、/healthz、
    # /readyz、/status 全 404；/ 与 /api/health 返回 401 = 活着但要 token）。
    # 原探针用 /health 判活 ⇒ 恒报 DOWN。判据改为与其它看门狗一致的「TCP 在监听即存活」，
    # 并把 HTTP 应答码作为附加信息（不做成败判据）。
    $listen = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $listen) {
        return [PSCustomObject]@{ Name = $name; Status = "DOWN"; Detail = "port $port not listening" }
    }
    $owner = $listen[0].OwningProcess
    $http = "no-http-reply"
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -TimeoutSec 5 -UseBasicParsing
        $http = "HTTP $($r.StatusCode)"
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        if ($code -gt 0) { $http = "HTTP $code" }
    }
    return [PSCustomObject]@{ Name = $name; Status = "UP"; Detail = "pid=$owner, $http" }
}

# 1. Trinity API
$results += Test-Http "trinity-api(8001)" "http://127.0.0.1:8001/health"

# 2. Trinity MCP (SSE, TCP 8000)
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $tcp.Connect("127.0.0.1", 8000)
    $results += [PSCustomObject]@{ Name = "trinity-mcp(8000)"; Status = "UP"; Detail = "TCP connected" }
    $tcp.Close()
} catch {
    $results += [PSCustomObject]@{ Name = "trinity-mcp(8000)"; Status = "DOWN"; Detail = $_.Exception.Message }
}

# 3. Collector 守护进程
$pidFile = Join-Path $TRINITY_HOME "data\collector.pid"
if (Test-Path $pidFile) {
    $cpid = (Get-Content $pidFile).Trim()
    $proc = Get-Process -Id $cpid -ErrorAction SilentlyContinue
    if ($proc) {
        $results += [PSCustomObject]@{ Name = "collector($cpid)"; Status = "UP"; Detail = "PID alive, CPU=$([math]::Round($proc.CPU,1))s" }
    } else {
        $results += [PSCustomObject]@{ Name = "collector($cpid)"; Status = "DOWN"; Detail = "PID file exists but process dead" }
    }
} else {
    $results += [PSCustomObject]@{ Name = "collector"; Status = "DOWN"; Detail = "collector.pid not found" }
}

# 4. DSH Web
$results += Test-DshWeb "dsh-web(3080)" 3080

# 5. engine_worker (DSH 插件 spawn 的 Python 进程)
$workers = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "engine_worker" }
if ($workers) {
    $results += [PSCustomObject]@{ Name = "engine_worker"; Status = "UP"; Detail = "$($workers.Count) process(es)" }
} else {
    $results += [PSCustomObject]@{ Name = "engine_worker"; Status = "DOWN"; Detail = "no engine_worker process (DSH 插件未加载时正常)" }
}

# 6. 同步状态文件新鲜度
$syncFile = Join-Path $TRINITY_HOME "data\sync_state.json"
if (Test-Path $syncFile) {
    $ageMin = [math]::Round(((Get-Date) - (Get-Item $syncFile).LastWriteTime).TotalMinutes, 1)
    $fresh = if ($ageMin -lt 10) { "FRESH" } else { "STALE" }
    $results += [PSCustomObject]@{ Name = "sync_state.json"; Status = $fresh; Detail = "last write $ageMin min ago" }
} else {
    $results += [PSCustomObject]@{ Name = "sync_state.json"; Status = "DOWN"; Detail = "not found" }
}

Write-Output "=== Trinity x DSH Health Check ($(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) ==="
$results | Format-Table -AutoSize
$down = @($results | Where-Object { $_.Status -ne "UP" -and $_.Status -ne "FRESH" })
if ($down.Count -eq 0) {
    Write-Output "ALL_HEALTHY"
} else {
    Write-Output "DEGRADED: $($down.Count) item(s) not healthy"
    $down | ForEach-Object { Write-Output "  - $($_.Name): $($_.Detail)" }
}
