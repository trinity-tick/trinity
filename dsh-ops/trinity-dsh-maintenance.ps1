<#
.SYNOPSIS
    Trinity DSH 维护驱动器 — 由 Windows 计划任务或手动调用。

.DESCRIPTION
    把 trinity 的日常维护任务（健康检查 / 进化 tick / 记忆衰减压缩 /
    记忆分层 / 双向同步 / 自检）统一封装，每个任务可选：
      - Direct 模式（默认）：直接用项目 venv Python 确定性执行（可靠、快）；
      - ViaDsh 模式（-ViaDsh）：把任务包装成 `dsh --profile headless` 的
        agent 任务执行，运行记录进入 DSH 持久会话，可回溯。
    所有输出与退出码写日志到 .trinity\logs\。

.EXAMPLE
    .\trinity-dsh-maintenance.ps1 -Tasks health,evolution
    .\trinity-dsh-maintenance.ps1 -Tasks all
    .\trinity-dsh-maintenance.ps1 -Tasks evolution -ViaDsh
    .\trinity-dsh-maintenance.ps1 -Tasks all -DryRun
#>
[CmdletBinding()]
param(
    [string[]]$Tasks = @("health", "evolution"),
    [switch]$ViaDsh,
    [switch]$DryRun,
    [int]$DecayLimit = 100,
    [string]$DecayLLM = "mock",
    [string]$LogDir = "C:\Users\Administrator\.trinity\logs"
)

# 兼容 powershell -File 传参：命令行里的 "a,b,c" 会以单个字符串到达，
# 这里统一按逗号拆分 + 校验。
$allowed = @("health", "evolution", "mirror", "decay", "compress", "tiers", "sync", "selftest", "session-summarize", "all")
$normalized = @()
foreach ($t in $Tasks) { $normalized += $t.Split(',') }
$normalized = $normalized | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
$bad = $normalized | Where-Object { $_ -notin $allowed }
if ($bad) {
    Write-Error "Unknown task(s): $($bad -join ', '). Allowed: $($allowed -join ', ')"
    exit 2
}
$Tasks = $normalized

$ErrorActionPreference = "Continue"
$TrinityRoot = Split-Path -Parent $PSScriptRoot
# 维护任务统一使用系统 Python（trinity 完整安装：含 fastapi/mcp/yaml/psycopg2 等；
# 项目 .venv 仅含基础依赖 numpy/jieba，跑不动 decay/tiers/sync）。
$Py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
$HermesSync = "C:\Users\Administrator\.trinity\sync_hermes_trinity.py"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Global:FAILED = @()

# PG 连接参数：优先级 环境变量 → DSH 凭证文件（~/.dsh/.credentials.yaml）→ 默认。
# 密码不再硬编码在仓库脚本/trinity.yaml（trinity.yaml 已脱敏并从 git 移除跟踪）。
. (Join-Path $PSScriptRoot "dsh-credentials.ps1")
$PgHost = if ($env:TRINITY_PG_HOST) { $env:TRINITY_PG_HOST } else { (Get-DshCredential "TRINITY_PG_HOST") }
if (-not $PgHost) { $PgHost = "127.0.0.1" }
$PgPort = if ($env:TRINITY_PG_PORT) { $env:TRINITY_PG_PORT } else { (Get-DshCredential "TRINITY_PG_PORT") }
if (-not $PgPort) { $PgPort = "5432" }
$PgUser = if ($env:TRINITY_PG_USER) { $env:TRINITY_PG_USER } else { (Get-DshCredential "TRINITY_PG_USER") }
if (-not $PgUser) { $PgUser = "postgres" }
$PgPass = if ($env:TRINITY_PG_PASSWORD) { $env:TRINITY_PG_PASSWORD } else { (Get-DshCredential "TRINITY_PG_PASSWORD") }
if (-not $PgPass) { $PgPass = "postgres" }

# ── dsh CLI 解析 ──────────────────────────────────────────────────────────
function Get-DshCli {
    $cmd = Get-Command dsh -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $fallback = "C:\Users\Administrator\AppData\Local\npm-cache\_npx\1e7f6d9597241db0\node_modules\.bin\dsh.ps1"
    if (Test-Path $fallback) { return $fallback }
    throw "dsh CLI not found on PATH"
}

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Write-Host $line
    try { Add-Content -Path (Join-Path $LogDir "dsh-maintenance.log") -Value $line -Encoding UTF8 } catch { }
}

function Invoke-Task {
    param(
        [string]$Name,
        [string]$DirectCommand,
        [string]$DshPrompt,
        [string]$WorkDir = $TrinityRoot
    )
    if ($DryRun) {
        Write-Log "[DRY-RUN] $Name : $DirectCommand"
        return
    }
    Write-Log "===== task: $Name ====="
    if ($ViaDsh) {
        $cli = Get-DshCli
        $job = Start-Job -ScriptBlock {
            param($c, $t)
            & $c --profile headless $t 2>&1
        } -ArgumentList $cli, $DshPrompt
        if (-not (Wait-Job $job -Timeout 900)) {
            Write-Log "$Name : TIMEOUT (900s), stopping job" "WARN"
            Stop-Job $job -ErrorAction SilentlyContinue
            $Global:FAILED += $Name
        } else {
            $out = Receive-Job $job
            $code = 0
            if ($job.State -ne "Completed") { $code = 1 }
            Remove-Job $job -Force -ErrorAction SilentlyContinue
            $out | ForEach-Object { Write-Log "dsh> $_" }
            if ($code -ne 0) { $Global:FAILED += $Name; Write-Log "$Name : FAILED (dsh exit $code)" "WARN" }
            else { Write-Log "$Name : OK (via dsh headless)" }
        }
    } else {
        if (-not (Test-Path $Py)) {
            Write-Log "$Name : venv python not found at $Py" "WARN"
            $Global:FAILED += $Name
            return
        }
        $tmpPy = Join-Path $LogDir "dsh-task-$Name-$Timestamp.py"
        try {
            [System.IO.File]::WriteAllText($tmpPy, $DirectCommand, (New-Object System.Text.UTF8Encoding($false)))
        } catch {
            Write-Log "$Name : failed to write temp script: $_" "WARN"
            $Global:FAILED += $Name
            return
        }
        $out = & $Py $tmpPy 2>&1
        $code = $LASTEXITCODE
        $out | ForEach-Object { Write-Log "  $_" }
        Remove-Item $tmpPy -Force -ErrorAction SilentlyContinue
        if ($code -ne 0) { $Global:FAILED += $Name; Write-Log "$Name : FAILED (exit $code)" "WARN" }
        else { Write-Log "$Name : OK" }
    }
    Write-Log "===== end: $Name ====="
}

# ── 任务定义 ──────────────────────────────────────────────────────────────

# 健康检查（.github_token 缺失时自动降级为本地检查）
$healthCmd = @"
import subprocess, sys
r = subprocess.run([sys.executable, r"$TrinityRoot\health_check.py"], cwd=r"$TrinityRoot",
                   capture_output=True, text=True)
print(r.stdout[-3000:] if r.stdout else "")
print(r.stderr[-1000:] if r.stderr else "")
sys.exit(r.returncode)
"@
$healthPrompt = "在 C:\Users\Administrator\trinity 运行 python health_check.py（若 .github_token 缺失则报告本地检查结果），并汇报关键 OK/FAIL 项。"

# 进化周期：每次运行完整执行一个周期（5 tick = Observe→Analyze→Plan→Execute→Certify）。
# 注意：中途相位只在内存（core.py 的 current_cycle/_phase_queue），跨进程不保留，
# 因此必须在同一进程内跑满 5 tick 才能完成一个周期。
$evoCmd = @"
import sys, json
sys.path.insert(0, r"$TrinityRoot")
from trinity.evolution import MetaEvolution
evo = MetaEvolution()
phases = []
last = None
for i in range(5):
    last = evo.tick({"action": "scheduled", "source": "dsh-maintenance"})
    phases.append(last.get("phase"))
    if last.get("cycle_complete"):
        break
evo.save_state()
d = evo.diagnostics()
print(json.dumps({"phases": phases, "cycle_complete": last.get("cycle_complete"),
                  "total_cycles": d.get("total_cycles"),
                  "preferences": len(evo.state.active_preferences),
                  "patterns": len(evo.state.active_patterns),
                  "corrections": len(evo.state.corrections_log),
                  "state_file": evo.state_path}, ensure_ascii=False))
"@
$evoPrompt = "在 C:\Users\Administrator\trinity 用 Python 执行一次完整的 Trinity 进化周期：from trinity.evolution import MetaEvolution; evo=MetaEvolution(); 在同一进程内连续 tick 直至 cycle_complete（最多 5 次）; evo.save_state()。然后读取 evo.diagnostics() 汇报执行的相位序列、是否完成周期、总周期数、偏好与模式数量。"

# 记忆衰减 + 压缩（Option A，2026-08-15：--store sqlite 直接作用于 SQLite 运行时大库）
# 注意：脚本按创建时间取最旧的 N 条（N=--limit），compressor 默认用 mock_llm_compress
# （非真实 LLM 摘要）。为控制每次运行的影响面，默认限制 DecayLimit=100 条，
# 并建议接入真实 LLM（MemoryCompressor(llm_callable=...)）后再放开。
$decayCmd = @"
import sys, json
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["run_decay_compress", "--store", "sqlite",
            "--limit", "$DecayLimit", "--llm", "$DecayLLM",
            "--output", r"$LogDir\decay_compress_$Timestamp.json"]
runpy.run_path(r"$TrinityRoot\scripts\run_decay_compress.py", run_name="__main__")
"@
$decayPrompt = "在 C:\Users\Administrator\trinity 运行 python scripts/run_decay_compress.py --store sqlite（直接对 SQLite 运行时大库 ~/.trinity/store/trinity_store.db 执行记忆衰减扫描与 LLM 压缩，结果写入 .trinity\logs），汇报扫描与压缩统计；库不可用请明确报告失败原因。"

# 记忆分层（Core/Recall/Archival，Option A：--store sqlite 扫描 SQLite 运行时大库）
$tiersCmd = @"
import sys, json
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["run_memory_tiers", "--store", "sqlite",
            "--output", r"$LogDir\memory_tiers_$Timestamp.json"]
runpy.run_path(r"$TrinityRoot\scripts\run_memory_tiers.py", run_name="__main__")
"@
$tiersPrompt = "在 C:\Users\Administrator\trinity 运行 python scripts/run_memory_tiers.py --store sqlite（对 SQLite 运行时大库执行三层记忆分层 Core/Recall/Archival），汇报分层统计；库不可用则报告失败。"

# 双向同步：Hermes ↔ Trinity + Marvis 一次性同步
$syncCmd = @"
import sys, subprocess
codes = []
r1 = subprocess.run([sys.executable, r"$HermesSync"], capture_output=True, text=True)
print("HERMES SYNC exit", r1.returncode)
print(r1.stdout[-2000:] if r1.stdout else "")
print(r1.stderr[-1000:] if r1.stderr else "")
codes.append(r1.returncode)
r2 = subprocess.run([sys.executable, "-m", "trinity.collector", "sync"], cwd=r"$TrinityRoot",
                    capture_output=True, text=True)
print("MARVIS SYNC exit", r2.returncode)
print(r2.stdout[-2000:] if r2.stdout else "")
print(r2.stderr[-1000:] if r2.stderr else "")
codes.append(r2.returncode)
sys.exit(0 if all(c == 0 for c in codes) else 1)
"@
$syncPrompt = "执行 Trinity 双向同步：1) 运行 python C:\Users\Administrator\.trinity\sync_hermes_trinity.py 同步 Hermes 记忆；2) 在 C:\Users\Administrator\trinity 运行 python -m trinity.collector sync 做 Marvis 一次性同步；汇报两边统计与错误。"

# SQLite 大库 → PG 幂等镜像（2026-08-15 接入：保证 decay/tiers 扫描覆盖运行时全量 active）
$mirrorCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["sqlite_pg_mirror", "--pg-port", "$PgPort", "--pg-user", "$PgUser", "--pg-password", "$PgPass"]
runpy.run_path(r"$TrinityRoot\scripts\sqlite_pg_mirror.py", run_name="__main__")
"@
$mirrorPrompt = "在 C:\Users\Administrator\trinity 运行 python scripts/sqlite_pg_mirror.py --pg-port 5432（SQLite 大库 active 记忆幂等镜像到本地 PostgreSQL，供 decay/tiers 全量扫描），汇报 added/skipped/errors 统计。"

# 自检（逐模块，可能较慢；仅在显式指定时运行）
$selftestCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["run_all_self_tests"]
runpy.run_path(r"$TrinityRoot\scripts\run_all_self_tests.py", run_name="__main__")
"@
$selftestPrompt = "在 C:\Users\Administrator\trinity 运行 python scripts/run_all_self_tests.py，汇总 PASS/FAIL/TIMEOUT 数量并报告失败的模块。"

# 会话状态化（OPT9/SESS-1）：为 SQLite store 中尚无摘要的会话生成 LLM 摘要（幂等）。
# 真实 LLM 需 TRINITY_LLM_API_KEY；无 key 时降级为抽取式摘要。
$sessionSummaryCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
from trinity.adapters.sqlite import SQLiteAdapter
from trinity.daemon.session_state import summarize_all_sessions
key = os.environ.get("TRINITY_LLM_API_KEY")
llm = None
if key:
    from trinity.daemon.memory_compressor import create_llm_compress_callable
    llm = create_llm_compress_callable(
        base_url=os.environ.get("TRINITY_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=key, model=os.environ.get("TRINITY_LLM_MODEL", "deepseek-chat"), timeout=60)
store = os.path.expanduser("~/.trinity/store/trinity_store.db")
adapter = SQLiteAdapter(db_path=store)
adapter.connect()
try:
    res = summarize_all_sessions(adapter, llm)
    print("SESSION-SUMMARIZE:", res)
finally:
    adapter.disconnect()
"@
$sessionSummaryPrompt = "在 C:\Users\Administrator\trinity 为 ~/.trinity/store/trinity_store.db 中尚无摘要的会话生成会话摘要（trinity.daemon.session_state.summarize_all_sessions，幂等，LLM 或抽取式降级），汇报会话数与摘要数。"

# ── 选择任务 ──────────────────────────────────────────────────────────────
if ($Tasks -contains "all") { $Tasks = @("health", "evolution", "mirror", "decay", "tiers", "sync", "selftest") }
if ($Tasks -contains "compress") { $Tasks = @($Tasks | Where-Object { $_ -ne "compress" }) + "decay" }

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
Write-Log "maintenance start (mode=$(if ($ViaDsh) {'ViaDsh'} else {'Direct'}), tasks=$($Tasks -join ','), dryrun=$DryRun)"

foreach ($t in $Tasks) {
    switch ($t) {
        "health"    { Invoke-Task -Name "health"    -DirectCommand $healthCmd -DshPrompt $healthPrompt }
        "evolution" { Invoke-Task -Name "evolution" -DirectCommand $evoCmd    -DshPrompt $evoPrompt }
        "decay"     { Invoke-Task -Name "decay"     -DirectCommand $decayCmd  -DshPrompt $decayPrompt }
        "tiers"     { Invoke-Task -Name "tiers"     -DirectCommand $tiersCmd  -DshPrompt $tiersPrompt }
        "mirror"    { Invoke-Task -Name "mirror"    -DirectCommand $mirrorCmd -DshPrompt $mirrorPrompt }
        "sync"      { Invoke-Task -Name "sync"      -DirectCommand $syncCmd   -DshPrompt $syncPrompt }
        "selftest"  { Invoke-Task -Name "selftest"  -DirectCommand $selftestCmd -DshPrompt $selftestPrompt }
        "session-summarize" { Invoke-Task -Name "session-summarize" -DirectCommand $sessionSummaryCmd -DshPrompt $sessionSummaryPrompt }
    }
}

if ($Global:FAILED.Count -gt 0) {
    Write-Log "maintenance finished with FAILED tasks: $($Global:FAILED -join ',')" "WARN"
    exit 1
}
Write-Log "maintenance finished OK"
exit 0
