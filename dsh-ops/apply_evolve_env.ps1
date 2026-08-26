# apply_evolve_env.ps1 — 自进化采纳 env 应用器（SELF_EVOLUTION_DESIGN 阶段3 缺口A 补全）
#
# 读取 ~/.trinity/evolve/evolve_env.json（evolve_loop.py CERTIFY 采纳后写入），
# 将其中 K=V 注入当前进程环境（与 supervisor 凭证注入同机制——Start-Process 子进程继承）。
# 若服务已运行，本脚本只注入环境并提示需重启服务生效（调用方决定是否重启）。
#
# 用法：
#   powershell -File dsh-ops/apply_evolve_env.ps1              # 注入 + 报告
#   powershell -File dsh-ops/apply_evolve_env.ps1 -Restart     # 注入 + 重启 api/mcp/mcp-http
#   powershell -File dsh-ops/apply_evolve_env.ps1 -Show        # 只显示已采纳 env（不注入）
#
# 安全：只允许白名单前缀的 env key（防 evolve_env.json 被恶意写入后注入任意环境变量）；
#       TRINITY_STORE 等关键路径不可被进化覆盖（防自进化改坏存储定位）。

param(
    [switch]$Restart,
    [switch]$Show
)

$ErrorActionPreference = "Continue"
$EvolveEnvFile = Join-Path $env:USERPROFILE ".trinity\evolve\evolve_env.json"
$LogDir = Join-Path $env:USERPROFILE ".trinity\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$Log = Join-Path $LogDir "dsh-evolve-env.log"

function Write-Log {
    param([string]$Message)
    $line = "{0} [INFO] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $Log -Value $line -Encoding UTF8
    Write-Output $line
}

# 白名单：自进化只允许调整这些参数域（对齐 SELF_EVOLUTION_DESIGN 受限变异域）
# 2026-08-25（审计）：移除 TRINITY_RERANK/TOP_K/TURN_TOP_K/DECAY/LLM_MODEL（不在 search_hybrid 路径）；
# 新增 IMPORTANCE_BOOST/STRENGTH_BOOST（hybrid 校准重排参数）。
$AllowedPrefixes = @(
    "TRINITY_RRF_", "TRINITY_GRAPH_", "TRINITY_CACHE_", "TRINITY_SEMANTIC_CACHE_",
    "TRINITY_ROUTE_", "TRINITY_ADAPTIVE_ROUTING", "TRINITY_CONFIDENCE_SCORER",
    "TRINITY_IMPORTANCE_BOOST", "TRINITY_STRENGTH_BOOST",
    "TRINITY_VECTOR_WEIGHT", "TRINITY_BM25_WEIGHT", "TRINITY_BM25_K1", "TRINITY_BM25_B"
)
$BlockedExact = @("TRINITY_STORE", "TRINITY_PG_HOST", "TRINITY_PG_PORT", "TRINITY_PG_DB",
                  "TRINITY_PG_USER", "TRINITY_PG_PASSWORD", "TRINITY_API_KEY",
                  "TRINITY_MCP_API_KEY", "GATEWAY_API_KEY", "TRINITY_MEMORY_ENABLED")

if (-not (Test-Path $EvolveEnvFile)) {
    Write-Log "no evolve_env.json — nothing to apply"
    exit 0
}

try {
    $evolveEnv = Get-Content $EvolveEnvFile -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Write-Log "evolve_env.json parse error: $($_.Exception.Message)"
    exit 1
}

$applied = @()
$skipped = @()
foreach ($prop in $evolveEnv.PSObject.Properties) {
    $key = $prop.Name
    $val = [string]$prop.Value
    # 白名单校验
    $allowed = $false
    foreach ($p in $AllowedPrefixes) {
        if ($key -like "$p*") { $allowed = $true; break }
    }
    if ($key -in $BlockedExact) { $allowed = $false }
    if (-not $allowed) {
        $skipped += "$key (not in whitelist)"
        continue
    }
    if ($Show) {
        $applied += "$key=$val"
    } else {
        [Environment]::SetEnvironmentVariable($key, $val, "Process")
        $applied += "$key=$val"
    }
}

if ($Show) {
    Write-Log "=== adopted env (evolve_env.json) ==="
    foreach ($a in $applied) { Write-Log "  $a" }
    foreach ($s in $skipped) { Write-Log "  [SKIP] $s" }
} else {
    Write-Log "applied $($applied.Count) evolve env: $($applied -join '; ') | skipped: $($skipped -join '; ')"
    if ($Restart -and $applied.Count -gt 0) {
        Write-Log "restarting services to apply env..."
        # 重启 api/mcp/mcp-http（supervisor 会在下一轮保持它们存活；这里直接拉最新 env 的新实例）
        foreach ($svc in @(
            @{ Name = "api";      Args = @("-m", "trinity.api.server", "--port", "8001", "--host", "127.0.0.1") },
            @{ Name = "mcp";      Args = @("-m", "trinity.mcp.server", "--mode", "sse", "--port", "8000", "--host", "127.0.0.1") },
            @{ Name = "mcp-http"; Args = @("-m", "trinity.mcp.server", "--mode", "streamable-http", "--port", "8003", "--host", "127.0.0.1") }
        )) {
            $proc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -match $svc.Args[2] -and $_.CommandLine -match $svc.Name -or
                               ($svc.Name -eq "api" -and $_.CommandLine -match "8001") } | Select-Object -First 1
            if ($proc) {
                Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
                Start-Sleep -Milliseconds 800
                Write-Log "  restarted $($svc.Name) (old PID $($proc.ProcessId))"
            }
        }
        Write-Log "note: supervisor/autostart 将在 5 分钟内拉起未启动的服务；若需立即完整重启请手动运行 trinity-supervisor.ps1"
    }
}
exit 0
