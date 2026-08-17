<#
.SYNOPSIS
    Trinity 基准并行运行器 — 并行跑 LongMemEval / LoCoMo / SQuAD / memsyco /
    延迟 / 并发 等基准套件并汇总结果。

.DESCRIPTION
    每个套件一个 PowerShell 后台作业并行执行（替代原先的子进程串行链），
    结果统一落到 <OutputDir>，结束时写 summary.json / summary.md。
    API key 从 -ApiKey 或环境变量 TRINITY_API_KEY / OPENAI_API_KEY 读取
    （不要把 key 写死在脚本或命令行历史里；可存入 DSH credentials 后注入 env）。

.EXAMPLE
    .\run-benchmarks.ps1 -Suites longmemeval,locomo -ApiKey $env:OPENAI_API_KEY
    .\run-benchmarks.ps1 -Suites latency,concurrency -SkipQa
    .\run-benchmarks.ps1 -Suites all
#>
[CmdletBinding()]
param(
    [string[]]$Suites = @("latency", "concurrency"),
    [string]$ApiKey = "",
    [switch]$SkipQa,
    [int]$MaxSamples = 50,
    [string]$OutputDir = ""
)

# 兼容 powershell -File 传参：命令行里的 "a,b,c" 会以单个字符串到达。
$allowedSuites = @("longmemeval", "locomo", "squad", "memsyco", "latency", "concurrency", "all")
$normalizedSuites = @()
foreach ($s in $Suites) { $normalizedSuites += $s.Split(',') }
$normalizedSuites = $normalizedSuites | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
$badSuites = $normalizedSuites | Where-Object { $_ -notin $allowedSuites }
if ($badSuites) {
    Write-Error "Unknown suite(s): $($badSuites -join ', '). Allowed: $($allowedSuites -join ', ')"
    exit 2
}
$Suites = $normalizedSuites

$ErrorActionPreference = "Continue"
$TrinityRoot = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $TrinityRoot ".venv\Scripts\python.exe"
$BenchDir = Join-Path $TrinityRoot "benchmark"

if (-not $ApiKey) { $ApiKey = $env:TRINITY_API_KEY; if (-not $ApiKey) { $ApiKey = $env:OPENAI_API_KEY } }
if (-not $OutputDir) { $OutputDir = Join-Path "C:\Users\Administrator\.trinity\bench-results" ((Get-Date -Format "yyyyMMdd_HHmmss")) }
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

if ($Suites -contains "all") { $Suites = @("longmemeval", "locomo", "squad", "memsyco", "latency", "concurrency") }

# 每个套件的执行定义: 脚本 + 参数模板（{out} 会被替换为输出目录）
$defs = @{
    longmemeval = @{ script = "run_benchmark.py";        args = @("--output-dir", "{out}") }
    locomo      = @{ script = "locomo_real_eval_v2.py";  args = @("--output-dir", "{out}") }
    squad       = @{ script = "squad_hybrid_runner.py";  args = @("--output-dir", "{out}") }
    memsyco     = @{ script = "memsyco_evaluator.py";    args = @("--output-dir", "{out}") }
    latency     = @{ script = "run_latency_bench.py";    args = @("--output-dir", "{out}") }
    concurrency = @{ script = "concurrency_bench.py";    args = @("--output-dir", "{out}") }
}

if (-not (Test-Path $Py)) { Write-Error "venv python not found: $Py"; exit 1 }

Write-Host "== Trinity benchmark parallel runner =="
Write-Host "   suites: $($Suites -join ', ')"
Write-Host "   output: $OutputDir"
if ($SkipQa) { Write-Host "   QA: skipped" }
if (-not $ApiKey) { Write-Warning "No API key (TRINITY_API_KEY/OPENAI_API_KEY/-ApiKey) — LLM-dependent suites may fail" }

$jobs = @()
foreach ($s in $Suites) {
    $d = $defs[$s]
    $scriptPath = Join-Path $BenchDir $d.script
    if (-not (Test-Path $scriptPath)) { Write-Warning "script missing: $scriptPath — skipping $s"; continue }
    $argsList = @($d.args | ForEach-Object { $_ -replace "{out}", $OutputDir })
    if ($ApiKey) { $argsList += @("--api-key", $ApiKey) }
    if ($SkipQa) { $argsList += "--skip-qa" }
    $argsList += @("--max-samples", [string]$MaxSamples)

    Write-Host "starting job: $s ($scriptPath)"
    $jobs += @{
        name  = $s
        job   = Start-Job -ScriptBlock {
            param($py, $sp, $al, $od)
            & $py $sp @al *> (Join-Path $od "job-$($sp | Split-Path -Leaf).log")
            exit $LASTEXITCODE
        } -ArgumentList $Py, $scriptPath, $argsList, $OutputDir
    }
}

$summary = [ordered]@{ run_at = (Get-Date -Format "o"); suites = @{} }
foreach ($j in $jobs) {
    $ok = Wait-Job $j.job -Timeout 3600
    if (-not $ok) {
        Stop-Job $j.job -ErrorAction SilentlyContinue
        $summary.suites[$j.name] = "TIMEOUT"
        Write-Warning "$($j.name): TIMEOUT (3600s)"
    } else {
        $code = 1
        if ($j.job.State -eq "Completed") { $code = 0 }
        $summary.suites[$j.name] = if ($code -eq 0) { "PASS" } else { "FAIL" }
        Write-Host "$($j.name): $($summary.suites[$j.name])"
    }
    Remove-Job $j.job -Force -ErrorAction SilentlyContinue
}

$summary | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $OutputDir "summary.json") -Encoding UTF8
$md = "# Benchmark Summary`n`n- run_at: $($summary.run_at)`n"
foreach ($k in $summary.suites.Keys) { $md += "- **$k**: $($summary.suites[$k])`n" }
$md += "`n详见 $OutputDir 下各 job-*.log 与套件产物。`n"
Set-Content (Join-Path $OutputDir "summary.md") -Value $md -Encoding UTF8
Write-Host ""
Write-Host "Done. Summary: $OutputDir\summary.md"
