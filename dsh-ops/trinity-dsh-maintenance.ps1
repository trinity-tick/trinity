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
    [int]$DecayLimit = 2000,  # 2026-08-18 闭环优化：全量覆盖 active 1,422
    [string]$DecayLLM = "auto",
    [int]$ConsistencyThreshold = 500,  # 2026-08-22 收尾：consistency 任务 drift 阈值（实测基线 drift=897，500 以下只告警不 FAILED）
    [string]$LogDir = "C:\Users\Administrator\.trinity\logs"
)

# 兼容 powershell -File 传参：命令行里的 "a,b,c" 会以单个字符串到达，
# 这里统一按逗号拆分 + 校验。
$allowed = @("health", "evolution", "mirror", "decay", "compress", "tiers", "consolidate", "dedup", "sync", "agent-sync", "pool-sync", "compact", "backup", "selftest", "session-summarize", "session-auto", "agent-ttl", "db-health", "active-health", "slo", "consistency", "evolve-auto", "evolve-env", "consolidate-temporal", "memory-ops", "pagetree", "eval", "review", "usage", "rollout-audit", "audit-ps1", "forgetting", "produce", "federation-sync", "tune", "fulltest", "pg-sync", "evolve", "all")  # 2026-08-18 SRE: slo 报告任务; 2026-08-21: agent-sync 多机同步 + pool-sync 聚合池水位同步; 2026-08-21: consistency 聚合池vs引擎库一致性校验（治理层只读）
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

# 真实 LLM 压缩（生产默认 auto）：无 TRINITY_LLM_API_KEY 时用 DEEPSEEK_API_KEY 兜底（OpenAI 兼容）。
# -DecayLLM auto（默认）= 有 key 走 real、无 key 回退 mock（脚本内解析），显式 mock/real 可覆盖。
if (-not $env:TRINITY_LLM_API_KEY) {
    $dk = Get-DshCredential "DEEPSEEK_API_KEY"
    if ($dk) {
        $env:TRINITY_LLM_API_KEY = $dk
        if (-not $env:TRINITY_LLM_BASE_URL) { $env:TRINITY_LLM_BASE_URL = "https://api.deepseek.com/v1" }
        if (-not $env:TRINITY_LLM_MODEL) { $env:TRINITY_LLM_MODEL = "deepseek-chat" }
    }
}
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
        [string]$WorkDir = $TrinityRoot,
        [string]$LeaseJob = ""   # 2026-08-21 P0-1: 非空则经 scripts/with_lease.py 认领租约后再执行（并发重复任务 SKIP）
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
        if ($LeaseJob) {
            # P0-1 租约守卫：并发重复任务直接 SKIP，不在 SQLite 写锁上排队
            $out = & $Py "$TrinityRoot\scripts\with_lease.py" --job $LeaseJob -- $Py $tmpPy 2>&1
        } else {
            $out = & $Py $tmpPy 2>&1
        }
        $code = $LASTEXITCODE
        $out | ForEach-Object { Write-Log "  $_" }
        Remove-Item $tmpPy -Force -ErrorAction SilentlyContinue
        if ($LeaseJob -and ($out -match 'with_lease: SKIP')) {
            Write-Log "$Name : SKIP (lease held by another maintenance run)" "WARN"
        } elseif ($code -ne 0) { $Global:FAILED += $Name; Write-Log "$Name : FAILED (exit $code)" "WARN" }
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
# 注意：脚本按"最冷优先"取 N 条（access_count ASC, created_at ASC，N=--limit），compressor 默认用 mock_llm_compress；DecayLimit 默认 500（P1-1，覆盖 active 约 27%，全量可 -DecayLimit 5000）
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
sys.argv = ["run_memory_tiers", "--store", "sqlite", "--limit", "10000",
            "--output", r"$LogDir\memory_tiers_$Timestamp.json"]
runpy.run_path(r"$TrinityRoot\scripts\run_memory_tiers.py", run_name="__main__")
"@
$tiersPrompt = "在 C:\Users\Administrator\trinity 运行 python scripts/run_memory_tiers.py --store sqlite（对 SQLite 运行时大库执行三层记忆分层 Core/Recall/Archival），汇报分层统计；库不可用则报告失败。"

# 睡眠式整合（Option P0-2c，2026-08-15）：decay/压缩 + LLM 事实提取 + 图更新
$consolidateCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["sleep_consolidation", "--store", "sqlite", "--llm", "$DecayLLM",
            "--output", r"$LogDir\sleep_consolidation_$Timestamp.json"]
runpy.run_path(r"$TrinityRoot\scripts\sleep_consolidation.py", run_name="__main__")
"@
$consolidatePrompt = "在 C:\Users\Administrator\trinity 运行 python scripts/sleep_consolidation.py --store sqlite --llm mock（睡眠式记忆整合：衰减扫描压缩 + 从高重要性记忆聚合提取可固化事实 + 实体图更新，结果写入 .trinity\logs），汇报各阶段统计；失败阶段明确报告。"

# 实体去重（P0-3，2026-08-15）：归一化 + embedding 相似合并
$dedupCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["entity_dedup", "--threshold", "0.90", "--no-embed",
            "--output", r"$LogDir\entity_dedup_$Timestamp.json"]
runpy.run_path(r"$TrinityRoot\scripts\entity_dedup.py", run_name="__main__")
"@
$dedupPrompt = "在 C:\Users\Administrator\trinity 运行 python scripts/entity_dedup.py --threshold 0.90（实体归一化去重，结果写入 .trinity\logs），汇报合并数与关系迁移；先备份再执行。"

# SLO 报告（2026-08-18, SRE 制度化）：采集可用性/性能/数据 SLO 指标
$sloCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["slo_report", "--out", r"$LogDir"]
runpy.run_path(r"$TrinityRoot\scripts\slo_report.py", run_name="__main__")
"@
$sloPrompt = "在 C:\Users\Administrator\trinity 运行 python scripts/slo_report.py 生成 SLO 报告（服务可用性/检索写入延迟/备份 RPO/数据一致性），汇报关键指标。"

# 结构层 compaction（2026-08-15；2026-08-21 P0-3 改 token 预算模式：每会话保留
# 最近 32768 token 明细原文，更早部分按 turn 聚合为 compacted_turn 摘要；
# 尾部超预算时优先裁 tool/result → tool/call，用户/助手段落永不裁）
$compactCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["compact_structure", "--budget-tokens", "32768"]
runpy.run_path(r"$TrinityRoot\scripts\compact_structure.py", run_name="__main__")
"@
$compactPrompt = "在 C:\Users\Administrator\trinity 运行 python scripts/compact_structure.py --budget-tokens 32768（结构层 compaction token 预算模式：非 active 会话保留最近 32768 token 明细 + 更早部分聚合为 compacted_turn，控制表增长），汇报压缩会话数与移除明细数。"

# 记忆页树（2026-08-26，PageIndex 借鉴）：纯元数据建树 + LLM 节点摘要（增量）
$pagetreeCmd = @"
# 2026-08-27（增量入链）：每日增量（1.2s）+ 周日全量重建+摘要
import sys, datetime
sys.path.insert(0, r"$TrinityRoot")
import runpy
if datetime.datetime.now().weekday() == 6:
    sys.argv = ["build_memory_pagetree"]
    runpy.run_path(r"$TrinityRoot\scripts\build_memory_pagetree.py", run_name="__main__")
    sys.argv = ["run_pagetree_summaries", "--limit", "20"]
    runpy.run_path(r"$TrinityRoot\scripts\run_pagetree_summaries.py", run_name="__main__")
else:
    sys.argv = ["pagetree_incremental"]
    runpy.run_path(r"$TrinityRoot\scripts\pagetree_incremental.py", run_name="__main__")
"@
$pagetreePrompt = "运行 scripts/build_memory_pagetree.py 与 scripts/run_pagetree_summaries.py（页树重建+增量摘要），汇报统计。"

# 断言式评测回归（2026-08-26 DSH 借鉴）：功能正确性断言
$evalCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["run_evals", "--all"]
runpy.run_path(r"$TrinityRoot\scripts
un_evals.py", run_name="__main__")
"@
$evalPrompt = "运行 scripts/run_evals.py --all（断言评测回归），汇报通过/失败断言数。"

# 评测审阅（2026-08-26 Claude Science 借鉴）：自动对比最近两次 500q reason 结果
$reviewCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["experiment_review", "--latest"]
runpy.run_path(r"$TrinityRoot\scripts\experiment_review.py", run_name="__main__")
"@
$reviewPrompt = "运行 scripts/experiment_review.py --latest（对比最近两次 ae_500_reason 结果），汇报异常类目与代码一致性。"
# rollout 异常审计（2026-08-27）：扫描 automation 轨迹失败模式，异常 emit 告警
$rolloutAuditCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["rollout_audit", "--days", "7"]
runpy.run_path(r"$TrinityRoot\scripts\rollout_audit.py", run_name="__main__")
"@
$rolloutAuditPrompt = "运行 scripts/rollout_audit.py（扫描近 7 天 automation 轨迹失败模式，异常 emit 告警），汇报统计。"

# 使用反馈（2026-08-27 使用伙伴闭环）：聚合审计生成使用报告（供 evolution ANALYZE）
$usageCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["usage_feedback", "--days", "7"]
runpy.run_path(r"$TrinityRoot\scripts\usage_feedback.py", run_name="__main__")
"@
$usagePrompt = "运行 scripts/usage_feedback.py（聚合近 7 天使用：热门查询/高频记忆/闲置记忆，报告入 evolution 输入），汇报使用概况。"


# 大库 → 聚合池 watermark 增量同步（2026-08-21 P0-2；维护窗口任务，不进 all 链）
$poolSyncCmd = @"
import sys, urllib.request
# 安全守卫：API 在线时聚合池由 API 进程持有（内存池+脏写持久化），直接写盘会被覆盖
try:
    urllib.request.urlopen("http://127.0.0.1:8001/health", timeout=3)
    print("POOL-SYNC SKIP: trinity-api 在线(:8001)，聚合池由 API 进程持有——请在维护窗口（服务停止）运行")
    sys.exit(0)
except Exception:
    pass
import runpy
sys.argv = ["sync_pool_from_db_v2"]
runpy.run_path(r"$TrinityRoot\benchmark\sync_pool_from_db_v2.py", run_name="__main__")
"@
$poolSyncPrompt = "运行 benchmark/sync_pool_from_db_v2.py（大库→聚合池 watermark 增量同步，rowid 水位；API 在线时 SKIP 守卫），汇报水位/跳过/新增统计。"

# 聚合池 vs 引擎库一致性校验（2026-08-21 治理层，只读）：不改任何库/池文件。
# drift = missing_in_pool + extra_in_pool + hash_mismatch；--fail-threshold 取 $ConsistencyThreshold
# （默认 500；2026-08-22 收尾：实测基线 drift=897 为两套长期分叉的治理告警，接入计划任务前用
# 默认阈值避免每次 FAILED，0=从不失败）。只读任务，不加入 all 链，均由用户显式调用。
$consistencyCmd = @"
import sys, subprocess
r = subprocess.run([sys.executable, r"$TrinityRoot\scripts\consistency_check.py", "--json",
                    "--fail-threshold", "$ConsistencyThreshold"], cwd=r"$TrinityRoot", capture_output=True, text=True)
print((r.stdout or "").strip()[:4000])
if r.stderr:
    print("STDERR:", r.stderr.strip()[-1000:])
sys.exit(r.returncode)
"@
$consistencyPrompt = "运行 scripts/consistency_check.py（聚合池 trinity/data/aggregator_pool.json vs 引擎库 ~/.trinity/store/trinity_store.db 的只读一致性校验，输出 missing/extra/hash_mismatch/drift/source_breakdown），汇报各项漂移计数；退出码按 --fail-threshold 判定。"

# 双向同步：Hermes ↔ Trinity + Marvis 一次性同步
# 2026-08-21 外部依赖容错：HERMES（本地）失败 → 任务 FAILED；MARVIS（推 docker
# 栈 :8005）失败 → 降级 WARN 不 FAILED（docker 停机时属预期，hermes 同步不受影响）。
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
if r2.returncode != 0:
    print(r2.stdout[-2000:] if r2.stdout else "")
    print(r2.stderr[-1000:] if r2.stderr else "")
    print("MARVIS SYNC DEGRADED: exit %d (docker 栈 :8005 不可达时属预期，hermes 双向同步已完成)" % r2.returncode)
else:
    print(r2.stdout[-2000:] if r2.stdout else "")
    print(r2.stderr[-1000:] if r2.stderr else "")
sys.exit(0 if all(c == 0 for c in codes) else 1)
"@
$syncPrompt = "执行 Trinity 双向同步：1) 运行 python C:\Users\Administrator\.trinity\sync_hermes_trinity.py 同步 Hermes 记忆；2) 在 C:\Users\Administrator\trinity 运行 python -m trinity.collector sync 做 Marvis 一次性同步；汇报两边统计与错误。"

# 多机实时同步（2026-08-21 落地）：本地引擎库 → 远端服务器聚合池（--one 单轮）。
# 关键安全边界：仅当 ~/.trinity/sync-agent.yaml 存在 且 server.url 不是本机/内网环回时运行；
# 否则 SKIP（幂等无害），绝不默认把本地大库推回本机聚合池。
$agentSyncCmd = @"
import os, sys, json
from pathlib import Path
import importlib.util
cfg_file = Path.home() / ".trinity" / "sync-agent.yaml"
if not cfg_file.exists():
    print("AGENT-SYNC SKIP: no ~/.trinity/sync-agent.yaml (同步未配置) — 请见 dsh-ops/SYNC_AGENT_DEPLOY.md")
    sys.exit(0)
# 载入 sync-agent 配置做安全守卫
spec = importlib.util.spec_from_file_location("tsa", r"$TrinityRoot\dsh-ops\trinity-sync-agent.py")
tsa = importlib.util.module_from_spec(spec); spec.loader.exec_module(tsa)
cfg = tsa.load_config(str(cfg_file))
url = (cfg.get("server") or {}).get("url", "").lower()
blocked = [u for u in ("127.0.0.1", "localhost", "::1", "[::1]") if u in url]
if blocked and url.startswith("http"):
    print("AGENT-SYNC SKIP: 目标为本地环回 %s（%s）—— 请改为远端服务器 URL, 避免把本地大库推回本机聚合池污染检索面" % (blocked[0], url))
    sys.exit(0)
# 允许：指向远端服务器时执行一轮
import subprocess
r = subprocess.run([sys.executable, r"$TrinityRoot\dsh-ops\trinity-sync-agent.py", "--one", "--config", str(cfg_file)],
                   capture_output=True, text=True, timeout=300)
print("AGENT-SYNC exit", r.returncode)
print((r.stdout or "")[-2000:])
print((r.stderr or "")[-1000:])
sys.exit(r.returncode)
"@
$agentSyncPrompt = "执行 Trinity 多机同步 agent 一轮（python dsh-ops/trinity-sync-agent.py --one --config ~/.trinity/sync-agent.yaml）。若配置文件不存在或目标为本机环回则 SKIP；否则把本地引擎库 active 记忆增量推送到远端服务器聚合池，汇报推送条数与状态。"

# SQLite 大库 → PG 幂等镜像（2026-08-15 接入：保证 decay/tiers 扫描覆盖运行时全量 active）
# 2026-08-21 外部依赖容错：PG :5430（docker trinity-db）不可达时 SKIP 而非 FAILED——
# 镜像缺席不误报每日链，docker 恢复后幂等补数（已验证 added/skipped/errors 语义）。
$mirrorCmd = @"
import sys, socket
try:
    s = socket.create_connection(("127.0.0.1", int($PgPort)), timeout=3)
    s.close()
except Exception as e:
    print("MIRROR SKIP: PG 127.0.0.1:$PgPort 不可达（%s）——维护镜像降级，docker 恢复后自动补数（幂等）" % e)
    sys.exit(0)
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
# 冒烟：引擎诊断全通过（防重构回归，2026-08-15）
from trinity.core.client import TrinityClient
_d = TrinityClient().diagnostics()
_e = _d.get("engine", {})
assert _e.get("ALL_PASS"), "engine diagnostics not ALL_PASS: %s" % _e.get("status", "?")
print("SMOKE diagnostics ALL_PASS OK (modules=%s)" % _e.get("total_modules"))
# 模块审计：孤儿/实验标注一致性（2026-08-15, P3 CI 集成）
import subprocess
_aud = subprocess.run([sys.executable, r"$TrinityRoot\scripts\audit_modules.py", "--json-only"],
                      capture_output=True, text=True, timeout=120)
assert _aud.returncode == 0, "audit_modules failed: %s" % _aud.stderr[-300:]
print("SMOKE module audit OK")
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
$sessionAutoCmd = @"
import sys
sys.path.insert(0, r"C:\Users\Administrator\trinity\scripts")
from auto_session_summary import main
main()
"@
$sessionAutoPrompt = "在 C:\Users\Administrator\trinity 运行 scripts/auto_session_summary.py（会话结束自动沉淀：从结构层 dsh_events 提取已结束/超时无活动会话的事件流，DeepSeek LLM 或抽取式生成 session-auto-summary 记忆，幂等），汇报候选/生成/跳过数。"
$agentTtlCmd = @"
import sys
sys.path.insert(0, r"C:\Users\Administrator\trinity\scripts")
from cleanup_expired_agents import main
main()
"@
$agentTtlPrompt = "运行 scripts/cleanup_expired_agents.py(TTL 过期 agent 卡片清理,幂等),汇报过期卡片数。"
$dbHealthCmd = @"
import sys
sys.path.insert(0, r"C:\Users\Administrator\trinity\scripts")
from db_health import main
sys.exit(main())
"@
$dbHealthPrompt = "运行 scripts/db_health.py(SQLite integrity + WAL checkpoint),汇报健康状态。"

# Active 集健康（2026-08-18）：active 占比 + 归档高价值记忆告警
$activeHealthCmd = @"
import sys
sys.path.insert(0, r"C:\Users\Administrator\trinity\scripts")
from active_set_health import main
sys.exit(main())
"@
$activeHealthPrompt = "运行 scripts/active_set_health.py(active 集健康: total/active/archived 占比, 归档高价值记忆告警, 有告警提示 restore_high_value_memories.py),汇报指标。"

# 备份（2026-08-27 巡检补全）：WAL 安全备份（14 天保留）
$backupCmd = @"
powershell -NoProfile -ExecutionPolicy Bypass -File '\$PSScriptRoot\trinity-backup.ps1'
"@
$backupPrompt = "运行 trinity-backup.ps1（WAL 安全备份），汇报备份文件。"

# 记忆操作（2026-08-27 巡检补全）
$memoryOpsCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["memory_ops"]
runpy.run_path(r"$TrinityRoot\scripts\memory_ops.py", run_name="__main__")
"@
$memoryOpsPrompt = "运行 scripts/memory_ops.py（记忆操作），汇报结果。"

# 时序巩固（2026-08-27 巡检补全）
$consolidateTemporalCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["consolidate_temporal"]
runpy.run_path(r"$TrinityRoot\scripts\consolidate_temporal.py", run_name="__main__")
"@
$consolidateTemporalPrompt = "运行 scripts/consolidate_temporal.py（时序巩固），汇报结果。"

# 压缩（2026-08-27 巡检补全；与 decay 同管线）
$compressCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["run_decay_compress", "--store", "sqlite", "--limit", "$DecayLimit", "--llm", "auto"]
runpy.run_path(r"$TrinityRoot\scripts\run_decay_compress.py", run_name="__main__")
"@
$compressPrompt = "运行 run_decay_compress.py（记忆压缩），汇报统计。"

# 进化 env 应用（2026-08-27 巡检补全）
$evolveAutoCmd = @"
powershell -NoProfile -ExecutionPolicy Bypass -File '\$PSScriptRoot\apply_evolve_env.ps1'
"@
$evolveAutoPrompt = "运行 apply_evolve_env.ps1（应用进化 env），汇报。"
$evolveEnvCmd = @"
powershell -NoProfile -ExecutionPolicy Bypass -File '\$PSScriptRoot\apply_evolve_env.ps1'
"@
$evolveEnvPrompt = "运行 apply_evolve_env.ps1（应用进化 env），汇报。"

# ps1 三件套巡检（2026-08-27）：allowed/定义/dispatch 齐全性每日自检
$auditPs1Cmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["audit_maintenance_ps1"]
runpy.run_path(r"$TrinityRoot\scripts\audit_maintenance_ps1.py", run_name="__main__")
"@
$auditPs1Prompt = "运行 scripts/audit_maintenance_ps1.py（维护链三件套巡检），汇报 ALL OK 或缺失项。"

# 遗忘决策（2026-08-27 方向A）：低价值记忆每日检查+保守归档
$forgettingCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["forgetting_score", "--limit", "10", "--apply"]
runpy.run_path(r"$TrinityRoot\scripts\forgetting_score.py", run_name="__main__")
"@
$forgettingPrompt = "运行 scripts/forgetting_score.py（遗忘分 TOP + 保守归档 score>0.9 & importance<0.3），汇报候选与归档数。"

# 知识生产+合规（2026-08-27 第二阶段）：每日周报+合规报告
$produceCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["knowledge_produce", "--days", "1"]
runpy.run_path(r"$TrinityRoot\scripts\knowledge_produce.py", run_name="__main__")
sys.argv = ["compliance_report"]
runpy.run_path(r"$TrinityRoot\scripts\compliance_report.py", run_name="__main__")
"@
$producePrompt = "运行 knowledge_produce.py（每日周报）+ compliance_report.py（合规报告），汇报产出文件。"

# 联邦定时同步（2026-08-27 第三阶段）：导出->推送目标实例（TRINITY_FED_TARGET）
$federationSyncCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
target = os.environ.get("TRINITY_FED_TARGET", "")
if not target:
    print("federation-sync: no TRINITY_FED_TARGET - skip")
else:
    sys.argv = ["federation_sync", target]
    runpy.run_path(r"$TrinityRoot\scripts\federation_push.py", run_name="__main__")
"@
$federationSyncPrompt = "运行 federation_sync.py（联邦同步：导出 decision/knowledge 推送 TRINITY_FED_TARGET），汇报推送数。"

# 自动调参（2026-08-27 自进化）：judge 阈值每日 A/B 推荐
$tuneCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["tune_judge", "--queries", "10"]
runpy.run_path(r"$TrinityRoot\scripts\tune_judge.py", run_name="__main__")
"@
$tunePrompt = "运行 scripts/tune_judge.py（judge 阈值自动 A/B 选优，写 tuned_config.json），汇报推荐阈值。"

# 全量测试门禁（2026-08-28 阶段1）：pytest 全量 + eval 12（补丁验证用）
$fulltestCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
import pytest
# 2026-08-28: fulltest via fulltest_gate.py (file-redirect subprocess,
# cwd=trinity root - matches manual run environment)
import subprocess as _sp, sys as _sys
rc = _sp.run([_sys.executable, "-X", "utf8",
              r"$TrinityRoot\scripts\fulltest_gate.py"],
              cwd=r"$TrinityRoot", timeout=1800).returncode
print("pytest rc:", rc)
if rc == 0:
    sys.argv = ["run_evals", "--all"]
    runpy.run_path(r"$TrinityRoot\scripts\run_evals.py", run_name="__main__")
else:
    print("EVALS SKIPPED (pytest failed)")
"@
$fulltestPrompt = "运行 pytest 全量 + eval 12（全量测试门禁），汇报通过数。"

# PG 镜像同步（2026-08-29 双写过渡）：SQLite → PG 每日增量 upsert
$pgSyncCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["sync_sqlite_to_pg"]
runpy.run_path(r"$TrinityRoot\scripts\sync_sqlite_to_pg.py", run_name="__main__")
"@
$pgSyncPrompt = "运行 SQLite → PG 镜像同步（增量 upsert），汇报新增/更新数与 PG 总量。"

# 每日 auto-evolve（2026-08-29 递归闭环真实使用）：无人值守补丁（门禁+回滚）
$evolveCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["evolve_patch", "--target", "scripts/tune_report.py", "--goal", "improve robustness (add defensive guard if missing)", "--apply", "--auto"]
runpy.run_path(r"$TrinityRoot\scripts\evolve_patch.py", run_name="__main__")
"@
$evolvePrompt = "运行 auto-evolve（每日真实小目标无人值守——门禁通过自动合入），汇报补丁结果。"

# ── 选择任务 ──────────────────────────────────────────────────────────────
if ($Tasks -contains "all") { $Tasks = @("health", "evolution", "mirror", "decay", "tiers", "consolidate", "dedup", "sync", "compact", "pagetree", "backup", "selftest") }
if ($Tasks -contains "compress") { $Tasks = @($Tasks | Where-Object { $_ -ne "compress" }) + "decay" }

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
Write-Log "maintenance start (mode=$(if ($ViaDsh) {'ViaDsh'} else {'Direct'}), tasks=$($Tasks -join ','), dryrun=$DryRun)"

foreach ($t in $Tasks) {
    switch ($t) {
        "health"    { Invoke-Task -Name "health"    -DirectCommand $healthCmd -DshPrompt $healthPrompt }
        "evolution" {
            # 2026-08-16: 先喂 API analyzer(审计回放)再触发 API 进化周期,最后跑 MetaEvolution
            & "$Py" "$LogDir\feed_evolution.py" 2>&1 | Out-Null
            try { Invoke-RestMethod -Uri "http://127.0.0.1:8001/evolution/cycle/run" -Method Post -TimeoutSec 120 | Out-Null } catch { Write-Log "API evolution cycle failed: $_" "WARN" }
            Invoke-Task -Name "evolution" -DirectCommand $evoCmd -DshPrompt $evoPrompt
        }
        "decay"     { Invoke-Task -Name "decay"     -LeaseJob "decay"     -DirectCommand $decayCmd  -DshPrompt $decayPrompt }
        "tiers"     { Invoke-Task -Name "tiers"     -LeaseJob "tiers"     -DirectCommand $tiersCmd  -DshPrompt $tiersPrompt }
        "mirror"    { Invoke-Task -Name "mirror"    -LeaseJob "mirror"    -DirectCommand $mirrorCmd -DshPrompt $mirrorPrompt }
        "consolidate" { Invoke-Task -Name "consolidate" -LeaseJob "consolidate" -DirectCommand $consolidateCmd -DshPrompt $consolidatePrompt }
        "dedup"      { Invoke-Task -Name "dedup"      -LeaseJob "dedup"      -DirectCommand $dedupCmd      -DshPrompt $dedupPrompt }
        "sync"      { Invoke-Task -Name "sync"      -LeaseJob "sync"      -DirectCommand $syncCmd   -DshPrompt $syncPrompt }
        "agent-sync" { Invoke-Task -Name "agent-sync" -LeaseJob "agent-sync" -DirectCommand $agentSyncCmd -DshPrompt $agentSyncPrompt }  # 2026-08-21 多机同步
        "pool-sync" { Invoke-Task -Name "pool-sync" -LeaseJob "pool-sync" -DirectCommand $poolSyncCmd -DshPrompt $poolSyncPrompt }  # 2026-08-21 P0-2 聚合池水位同步（维护窗口任务）
        "consistency" { Invoke-Task -Name "consistency" -DirectCommand $consistencyCmd -DshPrompt $consistencyPrompt }  # 2026-08-21 治理层只读一致性校验（显式调用，不进 all 链）
        "compact"   { Invoke-Task -Name "compact"   -LeaseJob "compact"   -DirectCommand $compactCmd  -DshPrompt $compactPrompt }
        "pagetree"  { Invoke-Task -Name "pagetree"  -LeaseJob "pagetree"  -DirectCommand $pagetreeCmd -DshPrompt $pagetreePrompt }  # 2026-08-26 PageIndex 借鉴
        "eval"      { Invoke-Task -Name "eval"      -DirectCommand $evalCmd      -DshPrompt $evalPrompt }  # 2026-08-26 DSH 借鉴
        "review"    { Invoke-Task -Name "review"    -DirectCommand $reviewCmd   -DshPrompt $reviewPrompt }  # 2026-08-26 Claude Science 借鉴
        "usage"     { Invoke-Task -Name "usage"     -DirectCommand $usageCmd     -DshPrompt $usagePrompt }  # 2026-08-27 使用伙伴闭环
        "rollout-audit" { Invoke-Task -Name "rollout-audit" -DirectCommand $rolloutAuditCmd -DshPrompt $rolloutAuditPrompt }  # 2026-08-27 rollout 审计
        "selftest"  { Invoke-Task -Name "selftest"  -DirectCommand $selftestCmd -DshPrompt $selftestPrompt }
        "session-summarize" { Invoke-Task -Name "session-summarize" -LeaseJob "session-summarize" -DirectCommand $sessionSummaryCmd -DshPrompt $sessionSummaryPrompt }
        "session-auto" { Invoke-Task -Name "session-auto" -LeaseJob "session-auto" -DirectCommand $sessionAutoCmd -DshPrompt $sessionAutoPrompt }
        "agent-ttl" { Invoke-Task -Name "agent-ttl" -DirectCommand $agentTtlCmd -DshPrompt $agentTtlPrompt }
        "slo"      { Invoke-Task -Name "slo"      -DirectCommand $sloCmd      -DshPrompt $sloPrompt }  # 2026-08-18 SRE
        "db-health" { Invoke-Task -Name "db-health" -DirectCommand $dbHealthCmd -DshPrompt $dbHealthPrompt }
        "active-health" { Invoke-Task -Name "active-health" -DirectCommand $activeHealthCmd -DshPrompt $activeHealthPrompt }
        "backup"    { Invoke-Task -Name "backup"    -LeaseJob "backup"    -DirectCommand $backupCmd    -DshPrompt $backupPrompt }  # 2026-08-27 巡检补全
        "memory-ops" { Invoke-Task -Name "memory-ops" -LeaseJob "memory-ops" -DirectCommand $memoryOpsCmd -DshPrompt $memoryOpsPrompt }  # 2026-08-27 巡检补全
        "consolidate-temporal" { Invoke-Task -Name "consolidate-temporal" -LeaseJob "consolidate-temporal" -DirectCommand $consolidateTemporalCmd -DshPrompt $consolidateTemporalPrompt }  # 2026-08-27 巡检补全
        "compress"  { Invoke-Task -Name "compress"  -LeaseJob "compress"  -DirectCommand $compressCmd  -DshPrompt $compressPrompt }  # 2026-08-27 巡检补全
        "evolve-auto" { Invoke-Task -Name "evolve-auto" -DirectCommand $evolveAutoCmd -DshPrompt $evolveAutoPrompt }  # 2026-08-27 巡检补全
        "evolve-env" { Invoke-Task -Name "evolve-env" -DirectCommand $evolveEnvCmd -DshPrompt $evolveEnvPrompt }  # 2026-08-27 巡检补全
        "audit-ps1" { Invoke-Task -Name "audit-ps1" -DirectCommand $auditPs1Cmd -DshPrompt $auditPs1Prompt }  # 2026-08-27 ps1 自检
        "forgetting" { Invoke-Task -Name "forgetting" -DirectCommand $forgettingCmd -DshPrompt $forgettingPrompt }  # 2026-08-27 遗忘决策
        "produce"   { Invoke-Task -Name "produce"   -DirectCommand $produceCmd   -DshPrompt $producePrompt }  # 2026-08-27 知识生产+合规
        "federation-sync" { Invoke-Task -Name "federation-sync" -DirectCommand $federationSyncCmd -DshPrompt $federationSyncPrompt }  # 2026-08-27 联邦同步
        "tune"      { Invoke-Task -Name "tune"      -DirectCommand $tuneCmd      -DshPrompt $tunePrompt }  # 2026-08-27 自动调参
        "fulltest"  { Invoke-Task -Name "fulltest"  -LeaseJob "fulltest"  -DirectCommand $fulltestCmd  -DshPrompt $fulltestPrompt }  # 2026-08-28 全量门禁
        "pg-sync"  { Invoke-Task -Name "pg-sync"  -DirectCommand $pgSyncCmd  -DshPrompt $pgSyncPrompt }  # 2026-08-29 PG 镜像
        "evolve"  { Invoke-Task -Name "evolve"  -LeaseJob "evolve"  -DirectCommand $evolveCmd  -DshPrompt $evolvePrompt }  # 2026-08-29 每日自改
        "backup"    { Write-Log "backup: WAL 安全备份到 ~/.trinity/backups (保留 14 天)"; & "$PSScriptRoot\trinity-backup.ps1" 2>&1 | ForEach-Object { Write-Log $_ } }
        "evolve-env" { Write-Log "evolve-env: 应用自进化采纳 env（evolve_env.json → 进程环境，白名单校验）"; & "$PSScriptRootpply_evolve_env.ps1" -Show 2>&1 | ForEach-Object { Write-Log $_ } }  # 2026-08-25 缺口A
        "consolidate-temporal" { $consArgs = @("--days", "1"); if ((Get-Date).DayOfWeek -eq "Sunday") { $consArgs = @("--days", "7", "--weekly") }; Write-Log "consolidate-temporal: 时间层级巩固（TiMem 式；daily 每日，Sunday 加 weekly）"; & "$Py" "$TrinityRoot\scripts\consolidate_temporal.py" @consArgs 2>&1 | ForEach-Object { Write-Log $_ } }  # 2026-08-25 TiMem 式
        "memory-ops" { Write-Log "memory-ops: Mem0 式记忆操作（LLM 决策 ADD/UPDATE/NOOP，控制写放大）"; $env:TRINITY_MEM_OPS = "on"; if ($DryRun) { & "$Py" "$TrinityRoot\scripts\memory_ops.py" --hours 24 --limit 20 --dry-run 2>&1 | ForEach-Object { Write-Log $_ } } else { & "$Py" "$TrinityRoot\scripts\memory_ops.py" --hours 24 --limit 20 2>&1 | ForEach-Object { Write-Log $_ } } }  # 2026-08-25 Mem0 式
    }
}

if ($Global:FAILED.Count -gt 0) {
    Write-Log "maintenance finished with FAILED tasks: $($Global:FAILED -join ',')" "WARN"
    exit 1
}
Write-Log "maintenance finished OK"
exit 0
