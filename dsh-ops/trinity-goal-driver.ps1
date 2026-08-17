<#
.SYNOPSIS
    Trinity DSH Goal 轮次驱动器 — 把 trinity 的长期工作（进化/维护/基准/同步）
    跑成 DSH goal 的一轮，产出结构化轮次结果供 goal 轮次追踪。

.DESCRIPTION
    每个 DSH goal 轮次调用本脚本一次（-Phase 指定该轮要做的相位）：
      - evolution   → 健康检查 + 进化完整周期（5 tick），检查点取 evolution_state.json
      - maintenance → 衰减压缩 + 记忆分层 + 双向同步（-DecayLimit 控制归档量）
      - benchmark   → 基准套件并行运行（默认 latency,concurrency，无需 API key）
      - sync        → Hermes + Marvis 双向同步
    轮次状态写入 .trinity\logs\dsh-goal-state.json：
      { round, lastPhase, lastRunAt, totalEvolutionCycles, checkpoint }
    输出一行 JSON（结构化轮次结果），供 DSH goal 记录/展示。

.EXAMPLE
    .\trinity-goal-driver.ps1 -Phase evolution
    .\trinity-goal-driver.ps1 -Phase maintenance -DecayLimit 50
    .\trinity-goal-driver.ps1 -Phase benchmark
#>
[CmdletBinding()]
param(
    [ValidateSet("evolution", "maintenance", "benchmark", "sync")]
    [string]$Phase = "evolution",
    [int]$DecayLimit = 100,
    [string]$StateFile = "C:\Users\Administrator\.trinity\logs\dsh-goal-state.json",
    [string]$LogDir = "C:\Users\Administrator\.trinity\logs"
)

$ErrorActionPreference = "Continue"
# 本脚本与 maintenance/benchmarks 同处 dsh-ops，必须用 $PSScriptRoot 定位；
# 此前误用父目录（trinity 根），解析出 trinity\trinity-dsh-maintenance.ps1 导致
# 子进程 -File 路径不存在、退出码恒为 -196608。
$OpsDir = $PSScriptRoot
$Maintenance = Join-Path $OpsDir "trinity-dsh-maintenance.ps1"
$Benchmarks = Join-Path $OpsDir "run-benchmarks.ps1"
$SysPy = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

# ── 轮次状态读写 ──────────────────────────────────────────────────────────
function Read-GoalState {
    if (Test-Path $StateFile) {
        try { return Get-Content $StateFile -Raw | ConvertFrom-Json } catch { }
    }
    return @{ round = 0; lastPhase = ""; lastRunAt = ""; totalEvolutionCycles = 0; checkpoint = @{} }
}

function Save-GoalState($s) {
    try { $s | ConvertTo-Json -Depth 6 | Set-Content $StateFile -Encoding UTF8 } catch { }
}

function Get-EvolutionCycles {
    $ev = "C:\Users\Administrator\.trinity\evolution_state.json"
    if (Test-Path $ev) {
        try {
            $j = Get-Content $ev -Raw | ConvertFrom-Json
            return [int]$j.total_cycles
        } catch { }
    }
    return -1
}

# ── 执行一轮 ──────────────────────────────────────────────────────────────
$state = Read-GoalState
$state.round = [int]$state.round + 1
$state.lastPhase = $Phase
$state.lastRunAt = Get-Date -Format "o"
$exit = 0

switch ($Phase) {
    "evolution" {
        $null = & powershell -NoProfile -ExecutionPolicy Bypass -File $Maintenance -Tasks "health,evolution" 2>&1
        $exit = $LASTEXITCODE
        $state.totalEvolutionCycles = Get-EvolutionCycles
        $state.checkpoint = @{
            evolutionStateFile = "C:\Users\Administrator\.trinity\evolution_state.json"
            totalEvolutionCycles = $state.totalEvolutionCycles
        }
    }
    "maintenance" {
        $null = & powershell -NoProfile -ExecutionPolicy Bypass -File $Maintenance -Tasks "decay,tiers,sync" -DecayLimit $DecayLimit 2>&1
        $exit = $LASTEXITCODE
        $state.checkpoint = @{ decayLimit = $DecayLimit; lastFullMaintenance = $state.lastRunAt }
    }
    "benchmark" {
        $null = & powershell -NoProfile -ExecutionPolicy Bypass -File $Benchmarks -Suites "latency,concurrency" 2>&1
        $exit = $LASTEXITCODE
        $state.checkpoint = @{ suites = "latency,concurrency"; benchResults = "C:\Users\Administrator\.trinity\bench-results" }
    }
    "sync" {
        $null = & powershell -NoProfile -ExecutionPolicy Bypass -File $Maintenance -Tasks "sync" 2>&1
        $exit = $LASTEXITCODE
        $state.checkpoint = @{ lastSync = $state.lastRunAt }
    }
}

Save-GoalState $state

# ── 结构化轮次结果（一行 JSON）────────────────────────────────────────────
$result = [ordered]@{
    goal_round = $state.round
    phase      = $Phase
    status     = if ($exit -eq 0) { "OK" } else { "FAIL" }
    exit_code  = $exit
    run_at     = $state.lastRunAt
    checkpoint = $state.checkpoint
    next_phase = switch ($Phase) {
        "evolution"   { "maintenance" }
        "maintenance"  { "benchmark" }
        "benchmark"    { "sync" }
        "sync"         { "evolution" }
    }
}
$result | ConvertTo-Json -Compress
