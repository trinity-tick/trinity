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
    [string[]]$Tasks = @("health", "leg-record", "evolution"),
    [switch]$ViaDsh,
    [switch]$DryRun,
    [int]$DecayLimit = 2000,  # 2026-08-18 闭环优化：全量覆盖 active 1,422
    [string]$DecayLLM = "auto",
    [int]$ConsistencyThreshold = 500,  # 2026-08-22 收尾：consistency 任务 drift 阈值（实测基线 drift=897，500 以下只告警不 FAILED）
    [string]$LogDir = "C:\Users\Administrator\.trinity\logs"
)

# 兼容 powershell -File 传参：命令行里的 "a,b,c" 会以单个字符串到达，
# 这里统一按逗号拆分 + 校验。
$allowed = @("analytics", "health", "evolution", "mirror", "decay", "compress", "tiers", "consolidate", "sublimate", "dedup", "sync", "agent-sync", "fok-mark-test", "fok-counts-fill", "fok-counts-fill-light", "silent-skip", "doc-regen-guard", "criterion-hygiene", "xref-check", "resource-window", "leg-record", "pool-sync", "compact", "backup", "selftest", "session-summarize", "session-auto", "agent-ttl", "db-health", "canary", "active-health", "slo", "consistency", "evolve-auto", "evolve-loop", "evolve-env", "brain-event", "brain-consumers", "brain-regions", "brain-status", "valence-backfill", "confidence-bp", "procedure-extract", "consolidate-temporal", "memory-ops", "pagetree", "eval", "review", "usage", "rollout-audit", "audit-ps1", "forgetting", "produce", "federation-sync", "tune", "fulltest", "pg-sync", "evolve", "observe", "value-recalib", "replay", "extract-skills", "perception-bridge", "cognitive-eval", "event-extract", "reversible-compress", "memory-purify", "cognition-agent", "dcpm-consolidate", "replay-consolidate", "integrity-monitor", "perception-scan", "self-reflect", "cognition-check", "web-perception", "web-search", "drift-check", "brain-health", "identity-refresh", "loop-audit", "brainification-guard", "capability-check", "action-loop", "forgetting", "dream-replay", "curiosity", "self-assess", "predictive-loop", "sensory-integration", "emotional-consolidation", "narrative", "self-axioms", "memory-manager", "proactive", "reconcile", "pg-backfill", "quality-gate", "plugin-smoke", "answer-eval", "snapshot", "brain-report", "consolidate-recent", "market-list", "situation", "opsbot-cycle", "perception-continuous", "expiry-review", "module-classify", "eval-gate", "self-upgrade", "opsbot-deep-action", "retro-boost", "summary-layer", "conflict-worker", "opsbot-report", "session-distill", "trinity-hud", "reader-agent", "recurrence-consolidate", "observation-build", "brain-md-export", "brain-heartbeat", "value-gate", "neuromodulate", "copies-sweep", "priority-map", "metacog-monitor", "reader-agent-ops", "perception-capture", "perception-screen-ingest", "reflect-rewrite", "blocks-heartbeat", "perception-recall", "smoke", "rewards", "nightly", "audit-reconcile", "contradiction-resolve", "flag-monitor", "ipi-check", "market-drill", "blind-judge", "dream", "perception-archival", "prewarm", "reason-slow-alert", "pg-pool-smoke", "store-growth", "disk-growth", "archive-purge-audit", "decay-real-llm", "write-policy-eval", "cluster-stress", "meta-strategy", "meta-strategy-propose", "pg-embed", "loop-health", "auditverify", "coverage", "reverb", "rl-guard", "forget-bias", "memory-digest", "session-candidates", "all", "all-full", "memory-correlation")  # 2026-09-01 对账 + PG→SQLite 反向同步  # 2026-08-18 SRE: slo 报告任务; 2026-08-21: agent-sync 多机同步 + pool-sync 聚合池水位同步; 2026-08-21: consistency 聚合池vs引擎库一致性校验（治理层只读）
$normalized = @()
# 环境感知流（2026-09 EXECUTION 136）：日志告警自动感知入记忆
# 2026-09-21（§1104）：静默跳过自检（只报告，不进闸门集 —— 冻结集条数不动）。
$silentSkipCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\silent_skip_audit.py", run_name="__main__")
"@
# 2026-09-21（§1128）：文档再生成守卫（只报告：产物必须比模板新，否则说明生成器没跑成）
$docRegenGuardCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\doc_regen_guard.py", run_name="__main__")
"@
# 2026-09-21（§1149）：判据卫生自查（只报告：每条闸门是否有可失败证明/采样时刻/裸 except）
$criterionHygieneCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\criterion_hygiene.py", run_name="__main__")
"@
# 2026-09-21 (S1166): doc cross-reference check (report only: paths / scripts / section numbers)
$xrefCheckCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\xref_check.py", run_name="__main__")
"@
# 2026-09-22 (S1189): resource window pre-check (read-only); run BEFORE heavy work.
# rationale: EXECUTION S1184 - running heavy selftest while commit memory is exhausted
# took the API down with it (guard logged commit-free 0.1GB, no-eligible-victim).
$resourceWindowCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\resource_window_check.py", run_name="__main__")
"@

# 2026-09-22 S1208: hourly sample of the supervisor leg into PG supervisor_leg_samples
# (read-only probe + one INSERT; measurement data goes to PG, never to state/).
$legRecordCmd = @"
import runpy, sys
sys.argv = ["supervisor_leg_status.py", "--record"]
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\supervisor_leg_status.py", run_name="__main__")
"@
$perceptionScanCmd = @"
import sys, os
sys.path.insert(0, r"C:\Users\Administrator\trinity")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
import runpy
sys.argv = ["perception_scan", "--max=20"]
runpy.run_path(r"C:\Users\Administrator\trinity\\scripts\\perception_scan.py", run_name="__main__")
"@
$perceptionScanPrompt = "运行 scripts/perception_scan.py（环境感知：日志告警→感知入记忆），输出 perceived/skipped 数。"

# 认知能力自检（2026-09 EXECUTION 155）：情绪指标+反思三能力量化评测
$cognitionCheckCmd = @"
import sys, os
sys.path.insert(0, r"C:\Users\Administrator\trinity")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")
import runpy
sys.argv = ["brain_cognition_eval"]
runpy.run_path(r"C:\Users\Administrator\trinity\\scripts\\brain_cognition_eval.py", run_name="__main__")
"@
$cognitionCheckPrompt = "运行 scripts/brain_cognition_eval.py（认知自检：情绪 EMA/极性/偏置 + 反思 retain/recall/quality），输出 JSON。"

# 网络搜索（2026-09 EXECUTION 161）：Bing 真实搜索（兴趣词驱动）
$webSearchCmd = @"
import sys, os
sys.path.insert(0, r"C:\Users\Administrator\trinity")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
import runpy
sys.argv = ["web_search", "--auto", "--max=15"]
runpy.run_path(r"C:\Users\Administrator\trinity\\scripts\\web_search.py", run_name="__main__")
"@
$webSearchPrompt = "运行 scripts/web_search.py --auto（网络搜索：Bing 兴趣词搜索→感知入记忆），输出 perceived 数。"

# 网络感知（2026-09 EXECUTION 158）：RSS 订阅实时抓取入记忆
$webPerceptionCmd = @"
import sys, os
sys.path.insert(0, r"C:\Users\Administrator\trinity")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
import runpy
sys.argv = ["web_perception", "--max=15"]
runpy.run_path(r"C:\Users\Administrator\trinity\\scripts\\web_perception.py", run_name="__main__")
"@
$webPerceptionPrompt = "运行 scripts/web_perception.py（网络感知：RSS 抓取→感知入记忆），输出 perceived 数。"

# 每日自我反思（2026-09 EXECUTION 151）：会话自省沉淀
$selfReflectCmd = @"
import sys, os
sys.path.insert(0, r"C:\Users\Administrator\trinity")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
import runpy
sys.argv = ["self_reflect_daily"]
runpy.run_path(r"C:\Users\Administrator\trinity\\scripts\\self_reflect_daily.py", run_name="__main__")
"@
$selfReflectPrompt = "运行 scripts/self_reflect_daily.py（每日自我反思：会话自省写入 self-reflection 记忆），输出 sessions/reflected 数。"

# 数据完整性巡检（2026-09 EXECUTION 133）：embedding/tsv 覆盖率 + 审计链 + 自愈
$integrityMonitorCmd = @"
import sys, os
sys.path.insert(0, r"C:\Users\Administrator\trinity")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
import runpy
sys.argv = ["pg_integrity_monitor"]
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\pg_integrity_monitor.py", run_name="__main__")
"@
$integrityMonitorPrompt = "运行 scripts/pg_integrity_monitor.py（数据完整性巡检），输出 JSON 报告。"

# 2026-09-09 P0-1/P0-2（《异星心智》借鉴轮）：全链审计记账 + 监控信心仪表
$auditVerifyCmd = @"
import sys, os
sys.path.insert(0, r"C:\Users\Administrator\trinity\scripts")
import runpy
sys.argv = ["audit_fullchain_verify"]
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\audit_fullchain_verify.py", run_name="__main__")
"@
$auditVerifyPrompt = "运行 scripts/audit_fullchain_verify.py（审计链全量校验+audit_runs 记账），汇报 integrity_ok/总条目。"

$coverageCmd = @"
import sys, os
sys.path.insert(0, r"C:\Users\Administrator\trinity\scripts")
import runpy
sys.argv = ["monitor_coverage", "--days", "7"]
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\monitor_coverage.py", run_name="__main__")
"@
$coveragePrompt = "运行 scripts/monitor_coverage.py（监控信心仪表），汇报 OVERALL monitor_coverage 与 partial/untracked 子系统。"

# 2026-09-09 P1/P2（《异星心智》借鉴轮）：回音室/RL源/遗忘偏见/digest
$reverbCmd = @"
import sys, os
sys.path.insert(0, r"D:\trinity-code\scripts")
import runpy
sys.argv = ["reverberation_detect", "--days", "90"]
runpy.run_path(r"D:\trinity-code\scripts\reverberation_detect.py", run_name="__main__")
"@
$reverbPrompt = "运行 scripts/reverberation_detect.py（循环溯源/回音室检测），汇报 rings/flagged/echo。"

$rlGuardCmd = @"
import sys, os
sys.path.insert(0, r"D:\trinity-code\scripts")
import runpy
sys.argv = ["rl_source_guard", "--days", "30"]
runpy.run_path(r"D:\trinity-code\scripts\rl_source_guard.py", run_name="__main__")
"@
$rlGuardPrompt = "运行 scripts/rl_source_guard.py（RL 源分级盲抽检），汇报 journal 分级与漂移。"

$forgetBiasCmd = @"
import sys, os
sys.path.insert(0, r"D:\trinity-code\scripts")
import runpy
sys.argv = ["forgetting_bias_audit", "--days", "90"]
runpy.run_path(r"D:\trinity-code\scripts\forgetting_bias_audit.py", run_name="__main__")
"@
$forgetBiasPrompt = "运行 scripts/forgetting_bias_audit.py（自利性遗忘审计），汇报负样本占比与 verdict。"

$memoryDigestCmd = @"
import sys, os
sys.path.insert(0, r"D:\trinity-code\scripts")
import runpy
sys.argv = ["memory_health_digest"]
runpy.run_path(r"D:\trinity-code\scripts\memory_health_digest.py", run_name="__main__")
"@
$memoryDigestPrompt = "运行 scripts/memory_health_digest.py（记忆库健康叙事），汇报 digest 路径。"

# DCPM System2 夜间整合（2026-09 EXECUTION 117）：信念→schema→记忆落库
$dcpmConsolidateCmd = @"
import sys, os
sys.path.insert(0, r"C:\Users\Administrator\trinity")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")
import runpy
sys.argv = ["dcpm_consolidate", "--write"]
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\dcpm_consolidate.py", run_name="__main__")
"@
$dcpmConsolidatePrompt = "运行 scripts/dcpm_consolidate.py --write（DCPM System2 夜间整合：PG 信念→schema 归纳→记忆落库），汇报信念/schema/落库数。"

$subCmd = @"
import sys, os
sys.path.insert(0, r"C:\Users\Administrator\trinity")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\knowledge_sublimation.py", run_name="__main__")
"@
$subPrompt = "运行 scripts/knowledge_sublimation.py（知识升华：感知输入批量提炼语义知识）"


# 情节→语义泛化（2026-09 EXECUTION 120）：重放管线 → 语义泛化记忆落库
$replayConsolidateCmd = @"
import sys, os
sys.path.insert(0, r"C:\Users\Administrator\trinity")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")
import runpy
sys.argv = ["memory_replay_consolidate", "--write", "--max", "80"]
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\memory_replay_consolidate.py", run_name="__main__")
"@
$replayConsolidatePrompt = "运行 scripts/memory_replay_consolidate.py --write（情节→语义泛化：PG 情节记忆重放→对比三元组→语义泛化记忆落库），汇报记忆/查询对/关键词数。"



foreach ($t in $Tasks) { $normalized += $t.Split(',') }
$normalized = $normalized | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
$bad = $normalized | Where-Object { $_ -notin $allowed }
if ($bad) {
    Write-Error "Unknown task(s): $($bad -join ', '). Allowed: $($allowed -join ', ')"
    exit 2
}
$Tasks = $normalized

$ErrorActionPreference = "Continue"
# 2026-09-01: 统一 UTF-8——wrapper(py) 已把 stdout reconfigure 为 UTF-8，
# PS 侧必须同样按 UTF-8 解码子进程输出，否则 GBK 控制台把 em-dash 等读成乱码。
# 无控制台场景（计划任务/服务）下 OutputEncoding setter 可能抛错，忽略即可。
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [System.Text.UTF8Encoding]::new()
} catch { }
# 2026-09-01: python 子进程统一按 UTF-8 写 stdout/stderr（与 PS 侧 UTF-8 解码配套，
# 覆盖未 reconfigure 的任务如 observe/runpy 型 wrapper，根治双向 GBK 乱码）
$env:PYTHONIOENCODING = "utf-8"

# 2026-09-09（优化执行）：线程上限——与 supervisor 同款。直接拉起的 python 任务
#（session-auto/db-health/canary 等）默认 56 核 OpenMP/MKL arena 每进程提交 8GB+；
# 限 4 线程后 0.4GB 级。防"短任务也吃 8GB"的提交压力。
foreach ($_tc in @("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS")) {
    if (-not [Environment]::GetEnvironmentVariable($_tc, "Process")) { [Environment]::SetEnvironmentVariable($_tc, "4", "Process") }
}
foreach ($_tc in @("KMP_BLOCKTIME", "OMP_WAIT_POLICY", "OMP_NESTED")) {
    if (-not [Environment]::GetEnvironmentVariable($_tc, "Process")) {
        $_v = if ($_tc -eq "OMP_WAIT_POLICY") { "PASSIVE" } elseif ($_tc -eq "OMP_NESTED") { "FALSE" } else { "0" }
        [Environment]::SetEnvironmentVariable($_tc, $_v, "Process")
    }
}
# 2026-09-11（第三轮审计 I2）：**PG 连接预算下压**——与上面的线程上限同一处、同一范式。
# 背景（实测）：一个进程内会创建多个 PostgreSQLAdapter（engine/aggregator/second_brain
# 等各持一池，上限默认 10）。批处理进程实测可持有 ~190 条 PG 连接，直接打爆
# max_connections=200：2026-09-11 13:10 与 13:18 两次实测——单个 quality_gate.py 进程
# 持有 194/189 条连接，金丝雀当场 FAIL "sorry, too many clients"，生产 API/写入被饿死；
# 其中一次进程跑完仍滞留持连接。适配器已内置 TRINITY_PG_POOL_MAX/MIN 覆盖
# （trinity/adapters/postgresql.py:142-154），但此前**全仓只有一个消费点**
# （scripts/e2e_qa_eval.py:36）——本链 30+ 个任务的子进程全部按默认 10 建池。
# 处置：在本链统一把「池上限」压到 3（维护任务是串行执行，3 条足够；根因修复见
# _advanced.py 的 SAGE 单飞锁，此项为保险丝）。已显式设置的调用方不受影响。
if (-not [Environment]::GetEnvironmentVariable("TRINITY_PG_POOL_MAX", "Process")) {
    [Environment]::SetEnvironmentVariable("TRINITY_PG_POOL_MAX", "3", "Process")
}
if (-not [Environment]::GetEnvironmentVariable("TRINITY_PG_POOL_MIN", "Process")) {
    [Environment]::SetEnvironmentVariable("TRINITY_PG_POOL_MIN", "1", "Process")
}
# 2026-09 (EXECUTION 120): 存储统一——租约/维护与运行时一致。
# 2026-10-02 **灾难恢复(补做)**：兜底值原为 "D:\trinity-data\store"（那是 2026-09-28 的遗留半迁移副本，
# mtime 09-28 03:17，非权威）；而原权威库 `...\.trinity\store\trinity_store.db` 已被强杀 API 写坏
# （quick_check: "Tree 77 page 644019: btreeInitPage() returns error code 11"）。
# 与 trinity-autostart.ps1 L35 / trinity-supervisor.ps1 L166 统一指向恢复库。
if (-not $env:TRINITY_STORE) { $env:TRINITY_STORE = "C:\Users\Administrator\.trinity\store-restored" }  # 2026-10-02 灾难恢复后的权威库
$TrinityRoot = Split-Path -Parent $PSScriptRoot
# 维护任务统一使用系统 Python（trinity 完整安装：含 fastapi/mcp/yaml/psycopg2 等；
# 项目 .venv 仅含基础依赖 numpy/jieba，跑不动 decay/tiers/sync）。
$Py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
$HermesSync = "C:\Users\Administrator\.trinity\sync_hermes_trinity.py"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Global:FAILED = @()

# ── 2026-09-20 §1004：单任务预算护栏的**默认表**（S1 用例：tests/unit/test_task_budget_guardrail.py）──
# 缺陷（实测）：Invoke-Task 的单任务预算早已实现，但默认 0=关闭 ⇒ 护栏是死代码；
# 而实测挂死过的任务（produce/selftest/pg-embed …）会把 1800s 包装器吃光、饿死链尾。
# 取证（dsh-autostart.log）：09-15/09-16/09-17/09-19 的 maintenance(...) timed out（last task: session-auto/selftest/produce）。
#
# 取值规则（用例判据②）：1.1 × 实测合法最长 ≤ 预算 ≤ 1700s（上界必须 < 包装器 1800s）。
#   · pg-embed = 1400 —— **本次实测修正**：用例表里 pg-embed 的「实测最长」是 44s，
#     那是**空闲轮**读数（当时 embedding IS NULL 很少，跑完就退）。2026-09-20 05:26 那轮
#     有真实欠账 ⇒ 05:36:03 撞上 600s 预算被 kill，记 pg-embed : FAILED (exit 124)（当日回填=0）。
#     合法上界由脚本自负：backfill_pg_embeddings.py 的 --max-seconds 默认 1200 ⇒ 上界约 1250s，
#     故预算取 1400（≥1.1×1250 且 ≤1700）。
#   · session-auto = 1500 —— 其自身上界 = 脚本墙钟 1200s + 预去重扫描/init 约 1370s，故取 1500。
#
# 语义（与 Invoke-Task 消费者侧**同一个 key 规范化表达式**，用例判据⑤）：
#   只设「调用方尚未设置」的 key ⇒ 显式 export 的预算永远优先，不被本表覆盖。
# 返回：本次真正写入的 {任务名 = 秒数}（供用例与日志核对）。
function Initialize-TaskBudgets {
    param([hashtable]$Defaults = $null)
    if ($null -eq $Defaults) {
        $Defaults = @{
            'consolidate-recent'        = 120
            # 900 = 用例判据①加强版（真实入口必须打印 selftest=900s）锁定的取值；
            # 且满足判据②（1.1×512=563 ≤ 900 ≤ 1700）。
            'selftest'                  = 900
            'brain-event'               = 700
            'perception-screen-ingest'  = 700
            'perception-capture'        = 750
            'quality-gate'              = 750
            'pg-sync'                   = 800
            'answer-eval'               = 900
            'tiers'                     = 900
            # 1500（2026-09-27 §1362 实测修正，原 1000）：修好 `fuse_docs` 之后它**真的开始融合文档**（原先秒级 DEGRADED），
            # 于是 produce 的真实耗时从「被快速失败掩盖的 ~353s」涨到 **1256s**（`TRINITY_TASK_BUDGET_PRODUCE=1700` 的完整跑，五步全 OK），
            # 撞穿原 1000s ⇒ 段里给了 1200s 也没用（先被自己的任务预算杀）。按本表规则：1.1×1256=1382 ≤ 预算 ≤ 1700 ⇒ 取 1500。
            # 配套：autostart 的尾段包装器超时同步提到 1700（**段预算必须 > 段内最大任务预算**），判据 tests/unit/test_task_budget_guardrail.py。
            'produce'                   = 1500
            'pg-embed'                  = 1400
            # 1550：用例判据②要求 ≥ 1.1×(脚本墙钟 1200 + 扫描/init 170 = 1370) = 1507，
            # 且 ≤1700（包装器 1800 内必须生效）；同时满足判据③的 > 自身上界。
            'session-auto'              = 1550
            # 2026-10-05：**prewarm = 1700**。背景（本会话查清的 40× 之谜）：
            #   API 进程被 supervisor **刻意**限成 `TRINITY_ONNX_THREADS=1`
            #   （§1020：8 线程曾把 CPU 占满 ⇒ uvicorn 拿不到时间片 ⇒ 健康探测超时被杀），
            #   而 ONNX 默认是 **8**（`embeddings/engine.py:383`）⇒ 同一批 200 行：
            #   **隔离/本脚本 30.1s（0.150 s/行）**，而 **API 进程内 >20 分钟未完成（≈40×）**。
            #   ⇒ 语料向量索引预热**必须在独立进程做**（本仓既有纪律：批量活走维护链、
            #     不与在线服务抢，见 `trinity-supervisor.ps1:97`）。
            #   `run_prewarm.py` 新增 `_warm_corpus_index()` 承担此事，并带**墙钟预算**
            #   （`TRINITY_PREWARM_CORPUS_BUDGET_S` 默认 1500s）⇒ 到点保存、**下次续跑**。
            # 取值：1.1 × 1500 = 1650 ≤ 1700（= 上界），且与调用方 `timeout=1700` 对齐。
            # 判据：`prewarm` 在日链、`run_prewarm.py` 打印 `corpus index: ok=True`，
            #       且 `~/.trinity/data/corpus_vec.*` 随轮次增长。
            # 回滚：从 `$dcTasks` 删掉 `prewarm`（或设 `TRINITY_PREWARM_CORPUS=0`）。
            'prewarm'                   = 1700
        }
    }
    $applied = @{}
    foreach ($k in @($Defaults.Keys)) {
        $key = "TRINITY_TASK_BUDGET_" + ($k -replace '[^A-Za-z0-9]', '_').ToUpper()
        if ([Environment]::GetEnvironmentVariable($key, 'Process')) { continue }
        [Environment]::SetEnvironmentVariable($key, [string]$Defaults[$k], 'Process')
        $applied[$k] = $Defaults[$k]
    }
    return $applied
}

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
if (-not $env:TRINITY_EMBED_BACKEND) { $env:TRINITY_EMBED_BACKEND = Get-DshCredential "TRINITY_EMBED_BACKEND" }

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

# 2026-09（EXECUTION 105.20）：失败告警推送（TRINITY_ALERT_WEBHOOK 配置后，维护失败即时通知）
function Send-Alert {
    param([string]$Message, [string]$Level = "ERROR")
    if (-not $env:TRINITY_ALERT_WEBHOOK) { return }
    try {
        $body = @{ level = $Level; message = $Message; ts = (Get-Date -Format "o"); source = "trinity-maintenance" } | ConvertTo-Json
        Invoke-RestMethod -Uri $env:TRINITY_ALERT_WEBHOOK -Method Post -ContentType "application/json" -Body $body -TimeoutSec 5 -ErrorAction Stop | Out-Null
    } catch { }
}

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Write-Host $line
    # 2026-09-18（事故修复）：维护链是**多进程并发**写同一个日志（全链 + supervisor/autostart
    # 的小子链同时在跑）。并发下 Add-Content 抛的是**非终止错误**
    # GetContentWriterArgumentError（"流不可读"），而 PowerShell 的 try/catch **抓不到非终止错误**
    # ⇒ 原写法（无 -ErrorAction Stop）等于静默丢行。
    # 实测后果：2026-09-18 03:25 并发窗口丢了 dcpm-consolidate 的 "===== task: ... =====" 头行，
    # 维护链审计据此误报"任务漏跑"（该次运行 err 日志 350 次该错误）⇒ chain-reconcile 环红、血流 L4 RED。
    # **判据实测（2026-09-18）**：6 路并发各写 60 行 ⇒ 只落 76/360 行（丢 79%）。
    # 异常两种：ArgumentException"流不可读"（非终止错误，原 try/catch 抓不到）
    # 与 IOException"文件正由另一进程使用"。**重试治不了**（多进程同步退避 ⇒ 反复对撞：
    # 实测 4 次重试 + 3 次兜底后，120 行只落 1 行，比不重试更差）。
    # 正解：用**命名互斥体**把"打开-追加-关闭"串行化，把并发写变成排队写。
    # 判据（可复跑）：tests/unit/test_write_log_concurrency.py 断言并发下行数零丢失。
    # 回滚：删掉互斥体分支、恢复单行 Add-Content 即可（本函数无其他副作用）。
    $logPath = Join-Path $LogDir "dsh-maintenance.log"
    if (-not $script:TrinityLogMutex) {
        try { $script:TrinityLogMutex = New-Object System.Threading.Mutex($false, "LocalTrinityDshMaintenanceLog") } catch { }
    }
    $acquired = $false
    if ($script:TrinityLogMutex) {
        try {
            $acquired = $script:TrinityLogMutex.WaitOne(15000)
        } catch [System.Threading.AbandonedMutexException] {
            $acquired = $true   # 上一持有者异常退出：锁归本进程，继续写
        } catch {
            $acquired = $false
        }
    }
    try {
        if ($acquired) {
            # 无 BOM：日志由 Python 审计按 utf-8 读，BOM 会污染首行时间戳匹配
            [IO.File]::AppendAllText($logPath, $line + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
        } else {
            for ($i = 0; $i -lt 3; $i++) {
                try {
                    Add-Content -Path $logPath -Value $line -Encoding UTF8 -ErrorAction Stop
                    break
                } catch {
                    Start-Sleep -Milliseconds (50 * ($i + 1))
                }
            }
        }
    } catch { }
    finally {
        if ($acquired) { try { $script:TrinityLogMutex.ReleaseMutex() } catch { } }
    }
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
        # 2026-09-15（R41-P23）：**每任务预算（DirectCommand 路径）**。
        # 背景：`$ViaDsh` 路径**已有** 900s 单任务超时，但 DirectCommand 路径**没有任何超时**
        # （原先就是一句 `$out = & $Py $tmpPy`）⇒ 一个失控任务可吃掉**整条链**的包装器超时
        # 并**饿死链尾**——实测 2026-09-15 09:44 的 perception 链即如此
        # （采集 10.5min + 吃帧 ~19.5min > 1800s ⇒ 尾部 `perception-recall` 被截，
        #  这正是 `chain-reconcile` 唯一红项的机理）。
        #
        # **默认关闭（0 = 不设预算，行为与改动前完全一致）**：任务耗时差异极大
        # （`pg-embed`/`fulltest`/`cluster-stress` 本来就慢），**统一默认值会误杀正常任务**。
        # 开启方式（按任务名覆盖优先）：
        #   · 全局   ：`$env:TRINITY_TASK_BUDGET_SEC = 900`
        #   · 单任务 ：`$env:TRINITY_TASK_BUDGET_<任务名大写、非字母数字转下划线> = 600`
        # 超时行为：**记 FAILED 并继续下一个任务**（不杀整链），与 $ViaDsh 路径口径一致。
        $budget = 0
        if ($env:TRINITY_TASK_BUDGET_SEC) { try { $budget = [int]$env:TRINITY_TASK_BUDGET_SEC } catch { $budget = 0 } }
        $perTaskKey = "TRINITY_TASK_BUDGET_" + ($Name -replace '[^A-Za-z0-9]', '_').ToUpper()
        $perTaskVal = [Environment]::GetEnvironmentVariable($perTaskKey)
        if ($perTaskVal) { try { $budget = [int]$perTaskVal } catch { } }
        if ($budget -gt 0) {
            $job = Start-Job -ScriptBlock {
                param($py, $script, $root, $lease, $leaseDb)
                Set-Location $root
                if ($lease) {
                    & $py "$root\scripts\with_lease.py" --job $lease --db $leaseDb -- $py $script 2>&1
                } else {
                    & $py $script 2>&1
                }
            } -ArgumentList $Py, $tmpPy, $TrinityRoot, $LeaseJob, "D:\trinity-data\store\trinity_store.db"
            if (-not (Wait-Job $job -Timeout $budget)) {
                Write-Log "$Name : TIMEOUT (${budget}s task budget), stopping job" "WARN"
                Stop-Job $job -ErrorAction SilentlyContinue
                $Global:FAILED += $Name
                $out = @("TASK BUDGET EXCEEDED (${budget}s)")
                $code = 124
            } else {
                $out = Receive-Job $job
                $code = 0
                if ($job.State -ne "Completed") { $code = 1 }
            }
            Remove-Job $job -Force -ErrorAction SilentlyContinue
        } elseif ($LeaseJob) {
            # P0-1 租约守卫：并发重复任务直接 SKIP，不在 SQLite 写锁上排队
            $out = & $Py "$TrinityRoot\scripts\with_lease.py" --job $LeaseJob --db "D:\trinity-data\store\trinity_store.db" -- $Py $tmpPy 2>&1
            $code = $LASTEXITCODE
        } else {
            $out = & $Py $tmpPy 2>&1
            $code = $LASTEXITCODE
        }
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
# 2026-09 Ollama 解耦观察期检查（逻辑在 dsh-ops/trinity-embed-observe.py）
$observeCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\dsh-ops\trinity-embed-observe.py", run_name="__main__")
"@
$observePrompt = "运行 Trinity 嵌入观察检查（/health + 3 查询抽样 + Ollama 连接计数），输出 JSON 报告。"

# 2026-09（EXECUTION 105）：价值驱动编码批量补标（LLM 多因素评估 importance）
$valueRecalibCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\value_recalibration.py", run_name="__main__")
"@
$valueRecalibPrompt = "运行 scripts/value_recalibration.py（LLM 五因素价值评估，写回 importance/importance_score/metadata.value_model），汇报 value 分布。"

# 2026-09（EXECUTION 105 第 2 轮）：海马体重放巩固——高价值记忆重新激活+片段整合
$replayCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\replay_consolidation.py", run_name="__main__")
"@
$replayPrompt = "运行 scripts/replay_consolidation.py（高价值记忆重放：replay_count+1、重新激活、相关片段 LLM 整合摘要），汇报重放统计。"
# 2026-09（EXECUTION 105 第 2 轮）：程序性记忆提取——工具轨迹频繁模式固化为技能库
$skillsCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\extract_skills.py", run_name="__main__")
"@
$skillsPrompt = "运行 scripts/extract_skills.py（从 dsh_events 工具轨迹提取频繁模式并固化到 PG skills 表），汇报技能列表。"

# 2026-09（EXECUTION 105.8）：感知桥——DSH 结构事件流（工具错误/目标完成）自动 feed 感知通道
$perceptionCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\perception_bridge.py", run_name="__main__")
"@
$perceptionPrompt = "运行 scripts/perception_bridge.py（扫描 dsh_events 高显著事件 feed /memory/perceive，习惯化门控），汇报编码统计。"

# 2026-09（EXECUTION 105.12）：认知能力评估套件（recall/gap/wm/value 四维）
$cognitiveEvalCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\cognitive_eval.py", run_name="__main__")
"@
$cognitiveEvalPrompt = "运行 scripts/cognitive_eval.py（认知能力四维评测：重建回忆一致性/元认知缺口精度/工作记忆命中/价值评估对齐），汇报指标与 PASS/FAIL。"

# 2026-09（EXECUTION 105.13）：事件中心时态图谱提取（Graphiti 式）
$eventExtractCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\event_extractor.py", run_name="__main__")
"@
$eventExtractPrompt = "运行 scripts/event_extractor.py（工具错误/目标完成/感知/决策事故 → 事件图谱事件节点，LLM 批量+规则兜底），汇报插入统计。"

# 2026-09（EXECUTION 105.14）：可逆压缩-重构（R3Mem 式：摘要+重构提示存 metadata，原内容不动）
$reversibleCompressCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\reversible_compress.py", run_name="__main__")
"@
$reversibleCompressPrompt = "运行 scripts/reversible_compress.py（长记忆可逆压缩：摘要+重构提示存 metadata，幂等），汇报压缩统计。"

# 2026-09（EXECUTION 105.15）：主动遗忘净化闭环（重复归档/冲突消解/过期失效/污染复查）
$purifyCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\memory_purification.py", run_name="__main__")
"@
$purifyPrompt = "运行 scripts/memory_purification.py（主动遗忘净化：重复记忆归档/冲突消解/过期失效，审计留痕），汇报净化统计。"

# 感知日采样（EXECUTION 553）：前台窗口 10 分钟（授权范围内，09:40 计划任务触发）
$perceptionCaptureCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["perception_capture", "--minutes", "10"]
runpy.run_path(r"$TrinityRoot/scripts/perception_capture.py", run_name="__main__")
"@
$perceptionCapturePrompt = "感知采样 10 分钟（前台窗口，45s 节奏，幂等去重）。"

$perceptionScreenIngestCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["perception_screen_ingest"]
runpy.run_path(r"$TrinityRoot/scripts/perception_screen_ingest.py", run_name="__main__")
"@
$perceptionScreenIngestPrompt = "运行感知流数据面(EXECUTION 589)：events.jsonl 新帧(sha幂等+watermark)→本地视觉描述→/memory/perceive channel=screen→P5边界episodes；输出 picked/encoded/episodes。"

$reflectRewriteCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["reflection_rewrite", "--recent", "5", "--apply"]
runpy.run_path(r"$TrinityRoot/scripts/reflection_rewrite.py", run_name="__main__")
"@
$reflectRewritePrompt = "运行重写式反思记忆(EXECUTION 592 Hindsight-lite)：最近会话轨迹→整条可重写反思(门禁A才写, E/B保持, 版本链保留)；输出 decision/action。"

$blocksHeartbeatCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["memory_blocks_heartbeat", "--both", "--apply"]
runpy.run_path(r"$TrinityRoot/scripts/memory_blocks_heartbeat.py", run_name="__main__")
"@
$blocksHeartbeatPrompt = "运行记忆块心跳(EXECUTION 593 Letta-lite)：dsh-self core/working 块门禁重写；输出 decision/action。"


# 2026-09-20（§933）：元记忆计数的**离线补算**。动机：fok 的子串计数在 250ms 上限内从来算不完
# ⇒ 弃答判据线上静默失效（A/B：负例弃答 0/30）；而换语义误弃 11/30、抽样仍有 2/30。物化表
# （fok_counts）+ 本任务离线补算，查询侧只查表 ⇒ A/B 实测 误弃 0/30、负例弃答 29/30、平均 7.9ms。
# 2026-09-20（§946）：**合成探测 key 出列**。本会话压测造了 1031 个"我发明的词"的 key
# （占全表 43%），它们永远不可能被真实用户问到，却混在 ROI 的"需求侧"里污染读数。
# 本任务每轮把这类 key 标成 source='test'（仍留在表里、仍省过算力，但**不进 ROI 结论**），
# 并把"最近 N 分钟"登记为一个探测窗口，供 ROI 给出「窗口外命中(≈自然)」读数。
$fokMarkTestCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["fok_mark_test_keys", "--apply", "--auto-burst"]
runpy.run_path(r"$TrinityRoot/scripts/fok_mark_test_keys.py", run_name="__main__")
"@
$fokMarkTestPrompt = "运行合成探测 key 出列(§946)：把压测造的 key 标成 source=test 并登记探测窗口；输出 decision/action。"

$fokCountsFillCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["fok_counts_fill", "--max-keys", "2400", "--batch", "40", "--prewarm", "200", "--prewarm-corpus", "0", "--adaptive", "1", "--max-pause-s", "20"]
runpy.run_path(r"$TrinityRoot/scripts/fok_counts_fill.py", run_name="__main__")
"@
$fokCountsFillPrompt = "运行元记忆计数离线补算(§933)：把 fok_counts_pending 里的待算 key 批量算好写入物化表；输出 decision/action。"
# D9 选项 C（2026-09-23 拍板并施工）：**按需触发**的轻量轮 —— 挂进 autostart 的「每 4 小时」段。
# 与上面那条同名重活的区别：预算小（200）、**队列不够长就自己跳过**（--min-pending 500）。
# 为什么把闸放在 .py 里而不是这里（§11 的取向：逻辑写成 .py）：阈值可测、可回滚、可单测。
$fokCountsFillLightCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["fok_counts_fill", "--max-keys", "200", "--batch", "40", "--prewarm", "0", "--prewarm-corpus", "0", "--adaptive", "1", "--max-pause-s", "10", "--min-pending", "500"]
runpy.run_path(r"$TrinityRoot/scripts/fok_counts_fill.py", run_name="__main__")
"@
$fokCountsFillLightPrompt = "运行元记忆计数**按需**补算(D9 选项 C)：pending>=500 才干活、单轮最多 200 个 key；队列短时打一行 [SKIP] 留痕。"
$perceptionRecallCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["perception_recall_check", "--limit", "20"]
runpy.run_path(r"$TrinityRoot/scripts/perception_recall_check.py", run_name="__main__")
"@
$perceptionRecallPrompt = "运行感知回查自检(EXECUTION 594)：screen 行 exact 命中率+域内语义自召回+全局隔离确认；输出 summary 到 recall_report.json。"

# reader ops 观察者（EXECUTION 551）：第二视角
$readerOpsCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["reader_agent", "--role", "ops"]
runpy.run_path(r"$TrinityRoot/scripts/reader_agent.py", run_name="__main__")
"@
$readerOpsPrompt = "reader-ops 第二视角观察简报。"

# HUD 看板（A3）+ reader 观察者（A4），EXECUTION 549
$hudCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["trinity_hud"]
runpy.run_path(r"$TrinityRoot/scripts/trinity_hud.py", run_name="__main__")
"@
$hudPrompt = "刷新 docs/TRINITY_HUD.html 运营看板。"
$readerCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["reader_agent"]
runpy.run_path(r"$TrinityRoot/scripts/reader_agent.py", run_name="__main__")
"@
$readerPrompt = "reader 观察者：生成外部视角 observation 记忆。"

# 会话末提炼（EXECUTION 547）：raw 会话 → insight 高价值记忆（对治写入质量缺口）
$sessionDistillCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["session_distill", "--limit", "5", "--apply"]
runpy.run_path(r"$TrinityRoot/scripts/session_distill.py", run_name="__main__")
"@
$sessionDistillPrompt = "会话末提炼：最近 raw 会话经 DS 教师收档为 insight（importance .72）。"

# opsbot 观察窗周报（EXECUTION 544）：自动汇总自治证据写 OPSBOT_WEEKLY_REPORT.md
$opsbotReportCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["opsbot_weekly_report"]
runpy.run_path(r"$TrinityRoot/scripts/opsbot_weekly_report.py", run_name="__main__")
"@
$opsbotReportPrompt = "生成 opsbot 观察窗周报（决策/内语/深度行动/市场/审计）。开始前先检索并引用 dsh-self 的 core/working 记忆块(category=memory_block)与近期 reflection(category=reflection)作为自我语境；无则略过。"

# F1 冲突巩固 worker（EXECUTION 517）：快写窗口后补跑冲突组分配（引擎同语义）
$conflictWorkerCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["conflict_consolidate_worker", "--cap", "200"]
runpy.run_path(r"$TrinityRoot/scripts/conflict_consolidate_worker.py", run_name="__main__")
"@
$conflictWorkerPrompt = "F1 冲突巩固：扫 active 无组行→引擎 _assign_conflicts 补组；输出 F1_DONE。"

# P9 回溯增强（EXECUTION 507/508）：重大事件→近24h 相关记忆 importance 微升（7d 幂等）
$retroBoostCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["brain_retro_boost", "--apply", "--cap", "50"]
runpy.run_path(r"$TrinityRoot/scripts/brain_retro_boost.py", run_name="__main__")
"@
$retroBoostPrompt = "P9 重大事件回溯增强（幂等 7d），输出 EVENTS/PLANNED/APPLIED。"

# P3 摘要层（EXECUTION 507/508）：抽取式摘要写 metadata.summary_extractive
# 2026-09-17（$797）：**cap 30 → 500**。原上限是"每周 30"的口径，但实测（本机）：
#   · 合格积压（active 且 len>700 且无 summary_extractive）= **3,588 条**；
#   · cap=200 实测 117s（0.59s/行）⇒ 按 cap 30/天 需 **119.6 天** 才能清完，等于永不清零；
#   · cap=500 约 5 分钟/次 ⇒ 约 7 天清完，且这条链本来就允许长任务。
# 同时写路径已默认生成摘要（trinity/memory/l0_summary.py），新增长记忆不再进入积压，
# 因此这个 cap 只作用于**历史欠账**，清完后每天实际处理量会自然趋近 0。
# 回滚：cap 改回 30。
$summaryLayerCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["summary_layer_builder", "--apply", "--cap", "500"]
runpy.run_path(r"$TrinityRoot/scripts/summary_layer_builder.py", run_name="__main__")
"@
$summaryLayerPrompt = "P3 摘要层：长记忆抽取式摘要写 metadata+审计 SUMMARY_WRITE，输出压缩比。"

# 2026-09（EXECUTION 105.22）：主动主体性循环（开放缺口/感知事件 → 主动思考 → 沉淀）
$cognitionAgentCmd = @"
import runpy
runpy.run_path(r"C:\Users\Administrator\trinity\scripts\cognition_agent.py", run_name="__main__")
"@
$cognitionAgentPrompt = "运行 scripts/cognition_agent.py（主动主体：扫描开放缺口与感知事件，主动思考并沉淀记忆），汇报触发与落库统计。"

$healthCmd = @"
import subprocess, sys, os
# 2026-09 (EXECUTION 111): 控制台 GBK 编码兼容——UTF-8 输出（health_check 含
# � 替换符字符时 print 到 GBK 终端报 UnicodeEncodeError，health 误报 FAILED）
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
r = subprocess.run([sys.executable, r"$TrinityRoot\health_check.py"], cwd=r"$TrinityRoot",
                   capture_output=True, text=True, encoding="utf-8", errors="replace")  # 2026-09: 显式 utf-8 解码
print(r.stdout[-3000:] if r.stdout else "")
print(r.stderr[-1000:] if r.stderr else "")
sys.exit(r.returncode)
"@
$healthPrompt = "在 C:\Users\Administrator\trinity 运行 python health_check.py（若 .github_token 缺失则报告本地检查结果），并汇报关键 OK/FAIL 项。"

# 进化周期：每次运行完整执行一个周期（5 tick = Observe→Analyze→Plan→Execute→Certify）。
# 注意：中途相位只在内存（core.py 的 current_cycle/_phase_queue），跨进程不保留，
# 因此必须在同一进程内跑满 5 tick 才能完成一个周期。
$evoCmd = @"
import sys, json, glob, os
sys.path.insert(0, r"$TrinityRoot")
from trinity.evolution import MetaEvolution
# 2026-09-01（D 修复）：把最近一次质量门禁指标喂进 tick 上下文——让进化环"看到"检索质量
# 数字（gate_ok/r5/延迟），为其后续决策提供评测锚点；无结果时为 None。
_gate = None
try:
    _gs = sorted(glob.glob(os.path.expanduser("~/.trinity/bench-results/quality-gate-*.json")))
    if _gs:
        _g = json.load(open(_gs[-1], encoding="utf-8"))
        _gate = {"gate_ok": _g.get("gate_ok"), "keyword_r5": (_g.get("keyword") or {}).get("r5"),
                 "hybrid_r5": (_g.get("hybrid") or {}).get("r5"),
                 "p50_ms": (_g.get("keyword") or {}).get("p50_ms"), "ts": _g.get("ts")}
except Exception:
    _gate = None
evo = MetaEvolution()
phases = []
last = None
for i in range(5):
    last = evo.tick({"action": "scheduled", "source": "dsh-maintenance", "quality_gate": _gate})
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

# 记忆衰减 + 压缩（2026-09-01 迁移：--store pg 作用于 PG 主存储；SQLite 由 pg-backfill 派生镜像）
# 注意：脚本按"最冷优先"取 N 条（access_count ASC, created_at ASC，N=--limit），compressor 默认用 mock_llm_compress；DecayLimit 默认 500（P1-1，覆盖 active 约 27%，全量可 -DecayLimit 5000）
# （非真实 LLM 摘要）。为控制每次运行的影响面，默认限制 DecayLimit=100 条，
# 并建议接入真实 LLM（MemoryCompressor(llm_callable=...)）后再放开。
$decayCmd = @"
import sys, json
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["run_decay_compress", "--store", "pg", "--host", "127.0.0.1", "--port", "$PgPort", "--user", "$PgUser", "--password", "$PgPass",  # 2026-09-01: PG 单写主（localhost→::1 会被 pg_hba 拒，须显式 127.0.0.1）
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
sys.argv = ["run_memory_tiers", "--store", "pg", "--host", "127.0.0.1", "--port", "$PgPort", "--user", "$PgUser", "--password", "$PgPass", "--limit", "10000",  # 2026-09-01: PG 单写主（localhost→::1 被拒）
            "--output", r"$LogDir\memory_tiers_$Timestamp.json"]
runpy.run_path(r"$TrinityRoot\scripts\run_memory_tiers.py", run_name="__main__")
"@
$tiersPrompt = "在 C:\Users\Administrator\trinity 运行 python scripts/run_memory_tiers.py --store sqlite（对 SQLite 运行时大库执行三层记忆分层 Core/Recall/Archival），汇报分层统计；库不可用则报告失败。"

# 睡眠式整合（Option P0-2c，2026-08-15）：decay/压缩 + LLM 事实提取 + 图更新
$consolidateCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["sleep_consolidation", "--store", "sqlite", "--llm", "$DecayLLM", "--facts", "20", "--min-importance", "0.2",  # 2026-09-01: 抽取规模化(5→20)
            "--output", r"$LogDir\sleep_consolidation_$Timestamp.json"]
runpy.run_path(r"$TrinityRoot\scripts\sleep_consolidation.py", run_name="__main__")
"@
$consolidatePrompt = "在 C:\Users\Administrator\trinity 运行 python scripts/sleep_consolidation.py --store sqlite --llm mock（睡眠式记忆整合：衰减扫描压缩 + 从高重要性记忆聚合提取可固化事实 + 实体图更新，结果写入 .trinity\logs），汇报各阶段统计；失败阶段明确报告。"

# 事件驱动巩固（2026-09-01 大脑化第三阶段）：仅聚合近 1 天记忆的小批高频巩固
$consolidateRecentCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["sleep_consolidation", "--store", "sqlite", "--llm", "$DecayLLM",
            "--facts", "10", "--min-importance", "0.3", "--recent-days", "1",
            "--output", r"$LogDir/sleep_consolidation_recent_$Timestamp.json"]  # 2026-09-01: 正斜杠路径
runpy.run_path(r"$TrinityRoot/scripts/sleep_consolidation.py", run_name="__main__")
"@
$consolidateRecentPrompt = "运行近期巩固（sleep_consolidation --recent-days 1 --facts 10）：仅聚合近 1 天记忆，小批高频，汇报提取/持久化/实体统计。"

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
# 2026-09-15（R41-P23）：**全量重建由"仅周日"改为"按龄"**（错过补跑范式）。
# 原判据 `weekday() == 6` ⇒ 全量重建**只在周日**发生；而主机实测每天约 4.6 次硬断电
# ⇒ 周日那次链若被截断/错过，**全量重建要再等一周**（而它正是页树摘要被冲掉的那一步，
# 直接决定 eval 断言 `pagetree-summary-coverage` 能否持续达标）。
# 现改为：**距上次全量重建 >= 6 天即到期**（仍是周频，但任何一次链运行都能补上），
# 用文件 mark 记录上次全量重建时间（跨重启有效）。
import sys, datetime, os, time
sys.path.insert(0, r"$TrinityRoot")
import runpy

def _guard(path, **kw):

    """逐步隔离（2026-09-14 R41-P5）：子脚本以 raise SystemExit(main()) 收尾，

    SystemExit 属 BaseException，except Exception 抓不到 ⇒ 会带走本块**后续步骤**。

    实测：本块原本只跑得到第一个脚本（第二个及其后从未执行）。"""

    try:

        runpy.run_path(path, run_name=kw.get("run_name", "__main__"))

    except SystemExit as e:

        if getattr(e, "code", 0) not in (0, None):

            print("step SystemExit:%s (%s)" % (e.code, path))

    except BaseException as e:

        print("step DEGRADED: %s: %s (%s)" % (type(e).__name__, str(e)[:120], path))
_pgmark = os.path.join(os.path.expanduser("~"), ".trinity", "state", "pagetree_full_rebuild.mark")
_full_due = True
try:

    if os.path.exists(_pgmark):

        _age_d = (time.time() - os.path.getmtime(_pgmark)) / 86400.0
        _full_due = _age_d >= 6.0
        print("pagetree full-rebuild age=%.1fd due=%s" % (_age_d, _full_due))

except Exception as _e:

    _full_due = True
    print("pagetree full-rebuild age check degraded:", str(_e)[:80])
if _full_due:
    sys.argv = ["build_memory_pagetree"]
    _guard(r"$TrinityRoot\scripts\build_memory_pagetree.py", run_name="__main__")
    # 2026-09-15（R41-P23）：**`--limit 20` → 260**——这是"断言结构性不可达"的最后一环。
    # 机理：周日这次**全量重建会冲掉页树摘要**（实测重建后 404 簇摘要全空），
    # 而紧接着只补 20 条 ⇒ 覆盖率 20/404 = **0.05 < 0.3 阈值** ⇒ eval 断言
    # `pagetree-summary-coverage` **每次都红**，且下个周日再冲一次 ⇒ **永远不可能连续通过**。
    # 实测单条摘要 ≈ 4.7s、成功率 170/170 ⇒ 260 条上限 ≈ 20 分钟（首次全补）；
    # 稳态下每个周日只有当轮被冲掉的簇需要补，量远小于此。
    # 取 260 而非 175，是给"新增长出来的簇"留余量。
    sys.argv = ["run_pagetree_summaries", "--limit", "260"]
    _guard(r"$TrinityRoot\scripts\run_pagetree_summaries.py", run_name="__main__")
    # 写"上次全量重建"标记（跨重启有效）⇒ 下次按龄判到期，不再依赖"恰好是周日"。
    try:

        os.makedirs(os.path.dirname(_pgmark), exist_ok=True)
        with open(_pgmark, "w", encoding="utf-8") as _f:

            _f.write(datetime.datetime.now().isoformat())

        print("pagetree full-rebuild mark written:", _pgmark)

    except Exception as _e:

        print("pagetree mark write degraded:", str(_e)[:80])
else:
    sys.argv = ["pagetree_incremental"]
    _guard(r"$TrinityRoot\scripts\pagetree_incremental.py", run_name="__main__")
    # 2026-09-17（$798 对标 ②「PageTree 默认打开」）：**非重建日也补增量摘要**。
    # 缺口：摘要步原本只在 `_full_due`（按龄 >=6 天）那一支里跑 ⇒ 新记忆长出的新簇
    # 最多要等 6 天才拿到摘要，而全量重建还会把已有摘要整片冲掉。
    # 实测（2026-09-17）：clusters=449 / 有摘要 184 / 空 265（空簇均为 <2 条记忆的
    # 单例簇，min-count=2 语义下**不该**有摘要，故 todo=0）⇒ 当前无欠账，
    # 这一步是"防漂移"：有活儿才花 LLM，没活儿 2 秒返回。
    # 成本：单条摘要 ~4.7s（既有实测）/ 上限 20 条 ≈ 95s，且只在真有新簇时才发生。
    # 回滚：删掉下面两行（注释可留）。
    sys.argv = ["run_pagetree_summaries", "--limit", "20"]
    _guard(r"$TrinityRoot\scripts\run_pagetree_summaries.py", run_name="__main__")
"@
$pagetreePrompt = "运行 scripts/build_memory_pagetree.py 与 scripts/run_pagetree_summaries.py（页树重建+增量摘要），汇报统计。"

# 断言式评测回归（2026-08-26 DSH 借鉴）：功能正确性断言
$evalCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["run_evals", "--all"]
runpy.run_path(r"$TrinityRoot\scripts\run_evals.py", run_name="__main__")
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
  # 2026-09-10（658.38）：API 现已自带池增量刷新（trinity/api/server/_pool_refresh.py，
  # 每 20 分钟一次、含自愈式追赶）——本离线脚本仅在 API 长期停机或池损坏时用于重建。
  # 安全守卫保留：API 在线时直接写盘仍会被内存池覆盖。
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
                    "--fail-threshold", "$ConsistencyThreshold", "--min-coverage", "0.5"], cwd=r"$TrinityRoot", capture_output=True, text=True, encoding="utf-8", errors="replace")  # 2026-09: 显式 utf-8 解码
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
import sys, subprocess, os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
codes = []
hermes = r"$HermesSync"
if os.path.exists(hermes):
    r1 = subprocess.run([sys.executable, hermes], capture_output=True, text=True, encoding="utf-8", errors="replace")  # 2026-09: 显式 utf-8 解码
    print("HERMES SYNC exit", r1.returncode)
    print(r1.stdout[-2000:] if r1.stdout else "")
    print(r1.stderr[-1000:] if r1.stderr else "")
    codes.append(r1.returncode)
else:
    print("HERMES SYNC SKIP: %s not found (本机未部署 Hermes 双向同步)" % hermes)
    codes.append(0)
# 658.40：先探测 :8005（docker 栈 Marvis API）——未监听则跳过推送，避免每日产生
# "bulk push partial: written=0 failed=1 (HTTPConnectionPool 8005)" 的假故障噪音。
import socket as _sock
_s = _sock.socket(); _s.settimeout(2)
try:
    _s.connect(("127.0.0.1", 8005)); _marvis_up = True
except Exception:
    _marvis_up = False
finally:
    _s.close()
if not _marvis_up:
    print("MARVIS SYNC SKIP: 127.0.0.1:8005 无监听（docker 栈未运行，属预期；启动该栈即自动恢复推送）")
    r2 = None
else:
    r2 = subprocess.run([sys.executable, "-m", "trinity.collector", "sync"], cwd=r"$TrinityRoot",
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("MARVIS SYNC exit", r2.returncode)
if r2 is not None:
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
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)  # 2026-09-01: 显式 utf-8 解码
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
# 冒烟：引擎诊断（防重构回归，2026-08-15）
from trinity.core.client import TrinityClient
_d = TrinityClient().diagnostics()
_e = _d.get("engine", {})
# 2026-09-28：这里原本断言的是**未披露的**合取 `ALL_PASS`，而那个合取里至少含一个
# 恒真项（engine_core.py:337 的 M101_dual_channel 曾是硬编码 True）。于是这条 CI 的
# 红只可能意味着"有人改了字面量"，而不是"引擎坏了" —— 那会训练读者忽略红色，
# 比假绿更糟。改法（按审计给的顺序：先让检查诚实，再把消费端指向**会说话**的信号）：
# 断言的仍是同一批门禁项，但每一项都来自**数据推导**，并且失败时**点出具体缺陷**。
_bad = []
if not _e.get("M101_dual_channel"):
    _bad.append(
        "M101 false-recall: 互补通道对一个无意义查询给出了回答 "
        "(2026-09-28 实测 'completely_unknown_query_string' -> knowledge_piece_14, "
        "match_score 0.86) —— 即返回了一条自信的错误记忆，而正确行为是 None。"
        "M101_exact_channel_ok=%s M101_false_recall=%s"
        % (_e.get("M101_exact_channel_ok"), _e.get("M101_false_recall")))
if not _e.get("CB50_resolution_ok"):
    _bad.append("CB50 解析出 0 个指代（fixture 里有 Alice/she，本应解析成功）")
assert not _bad, "engine capability RED: " + " | ".join(_bad)
print("SMOKE diagnostics OK (modules=%s, retrieval_channels_contributing=%s, "
      "guardian_levels_enforcing=%s)"
      % (_e.get("total_modules"), _e.get("retrieval_channels_contributing"),
         _e.get("guardian_levels_enforcing")))
# 模块审计：孤儿/实验标注一致性（2026-08-15, P3 CI 集成）
import subprocess
_aud = subprocess.run([sys.executable, r"$TrinityRoot\scripts\audit_modules.py", "--json-only"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)  # 2026-09-01: 显式 utf-8 解码
assert _aud.returncode == 0, "audit_modules failed: %s" % _aud.stderr[-300:]
print("SMOKE module audit OK")
import runpy
sys.argv = ["run_all_self_tests"]
runpy.run_path(r"$TrinityRoot\scripts\run_all_self_tests.py", run_name="__main__")
"@
$selftestPrompt = "在 C:\Users\Administrator\trinity 运行 python scripts/run_all_self_tests.py，汇总 PASS/FAIL/TIMEOUT 数量并报告失败的模块。"

# 大脑机制状态面（H1-1 接线，2026-09-14 EXECUTION 724）：把 59 个"有实现无调用方"的
# 大脑区机制真正调用起来（只读诊断，不改任何检索/写入行为），产出 output/brain_mechanisms_status.json。
$brainStatusCmd = @"
import sys, runpy
sys.argv = ["brain_mechanisms_status", "--quiet"]
runpy.run_path(r"$TrinityRoot\scripts\brain_mechanisms_status.py", run_name="__main__")
"@
$brainStatusPrompt = "在 C:\Users\Administrator\trinity 运行 python scripts/brain_mechanisms_status.py，报告 mechanisms/ok/failed 计数。"

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
import sys, os
sys.path.insert(0, r"C:\Users\Administrator\trinity\scripts")
# 2026-09-09（闭环执行 A2）：恢复真实写入 + 限量安全阀——SESSION_AUTO_DRYRUN 默认
# 1 使 session-auto 自 09-07 起空转（摘要停更 09-04/05）；现置 0 并限 20/轮
# （幂等续跑，防旧长链卡死复现），经验蒸馏默认开启。
os.environ.setdefault("SESSION_AUTO_DRYRUN", "0")
os.environ.setdefault("SESSION_AUTO_MAX", "20")
os.environ.setdefault("SESSION_AUTO_EXPERIENCE", "1")
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

# 金丝雀端到端链路自检（2026-09-09 优化执行 P1）：写入→检索闭环黄金信号（防健康假象）
$canaryCmd = @"
import sys
sys.path.insert(0, r"C:\Users\Administrator\trinity\scripts")
from canary_check import main
sys.exit(main())
"@
$canaryPrompt = "运行 scripts/canary_check.py(金丝雀: 写一条带日戳记忆→hybrid 检索断言命中), 汇报 CANARY: OK/FAIL。"

# Active 集健康（2026-08-18）：active 占比 + 归档高价值记忆告警
$activeHealthCmd = @"
import sys
sys.path.insert(0, r"C:\Users\Administrator\trinity\scripts")
from active_set_health import main
sys.exit(main())
"@
$activeHealthPrompt = "运行 scripts/active_set_health.py(active 集健康: total/active/archived 占比, 归档高价值记忆告警, 有告警提示 restore_high_value_memories.py),汇报指标。"

# 备份（2026-08-27 巡检补全；2026-09 加恢复演练）：WAL 安全备份（14 天保留）+ PG 恢复演练
$backupCmd = @"
import subprocess, sys
r = subprocess.run(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-File", r"$PSScriptRoot\trinity-backup.ps1"], capture_output=True, text=True, encoding="utf-8", errors="replace")
print(r.stdout[-2000:] if r.stdout else "")
print(r.stderr[-1000:] if r.stderr else "")
r2 = subprocess.run([sys.executable, r"$TrinityRoot\scripts\pg_restore_drill.py"], capture_output=True, text=True, encoding="utf-8", errors="replace")
print("PG RESTORE DRILL exit", r2.returncode)
print(r2.stdout[-1500:] if r2.stdout else "")
sys.exit(0 if r.returncode == 0 and r2.returncode == 0 else 1)
"@
$backupPrompt = "运行 trinity-backup.ps1（WAL 安全备份）+ scripts/pg_restore_drill.py（恢复演练），汇报备份文件与演练 PASS/FAIL。"

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

# 2026-09-09 闭环修复：自进化全闭环（SIGNAL→VARIANT→A/B→CERTIFY）此前无任何调度，
# 产物停在 08-25。接入周链（预算/降频由 evolve_loop 自身 pacing 门控制）。
$evolveLoopCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["evolve_loop", "--n-qa", "30", "--max-variants", "1"]
runpy.run_path(r"$TrinityRoot\scripts\evolve_loop.py", run_name="__main__")
"@
$evolveLoopPrompt = "运行 scripts/evolve_loop.py --n-qa 10 --max-variants 1（自进化闭环：信号→候选→A/B→采纳/证伪），汇报结果。"

# 2026-09-09 大脑化优化：事件驱动脑区 / 休眠脑区消费 / 情感价 / 置信传播 / 程序性记忆
$brainEventCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["brain_event", "--drain", "--max-stages", "3"]
runpy.run_path(r"$TrinityRoot\scripts\brain_event.py", run_name="__main__")
"@
$brainEventPrompt = "运行 scripts/brain_event.py --drain（事件驱动脑区调度，日预算 40），汇报执行的阶段。"

$brainConsumersCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["consumers"]
runpy.run_path(r"$TrinityRoot\trinity\brain\consumers.py", run_name="__main__")
"@
$brainConsumersPrompt = "运行 trinity/brain/consumers.py（休眠脑区消费：状态→可检索记忆+提议），汇报写入数。"

$valenceBackfillCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["valence", "--limit", "2000"]
runpy.run_path(r"$TrinityRoot\trinity\brain\valence.py", run_name="__main__")
"@
$valenceBackfillPrompt = "运行 trinity/brain/valence.py --limit 300（情感价词典标注），汇报 tagged 数。"

$confidenceBpCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["confidence_propagation", "--limit", "5000"]
runpy.run_path(r"$TrinityRoot\trinity\brain\confidence_propagation.py", run_name="__main__")
"@
$confidenceBpPrompt = "运行 trinity/brain/confidence_propagation.py --limit 2000（关系图置信传播），汇报 nodes/edges/updated。"

$procedureExtractCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["procedure", "--limit", "1000"]
runpy.run_path(r"$TrinityRoot\trinity\brain\procedure.py", run_name="__main__")
"@
$procedureExtractPrompt = "运行 trinity/brain/procedure.py --limit 200（程序性记忆抽取），汇报 written 数。"

# 2026-09-09 大脑化"保证运行"：13 个脑区驱动器（库函数 → 每小时 tick）
$brainRegionsCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
os.environ.setdefault("TRINITY_PROPOSAL_BRIDGE", "on")
os.environ.setdefault("TRINITY_PREDICTION_ERROR", "on")
os.environ.setdefault("TRINITY_PRECISION_TIERS", "on")
os.environ.setdefault("TRINITY_WORLD_MODEL", "on")
os.environ.setdefault("TRINITY_SKILL_LIBRARY", "on")
os.environ.setdefault("TRINITY_SOCIAL_COGNITION", "on")
sys.argv = ["brain_regions_tick"]
runpy.run_path(r"$TrinityRoot\scripts\brain_regions_tick.py", run_name="__main__")
"@
$brainRegionsPrompt = "运行 scripts/brain_regions_tick.py（13 脑区驱动器），汇报 failed 数与各脑区状态新鲜度。"

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

def _step(name, argv, path):
    """逐步隔离执行：**连 SystemExit 一起接**（2026-09-14 R41-P4）。

    实测缺陷：子脚本一律以 raise SystemExit(main()) 收尾，而 SystemExit 属 BaseException，
    except Exception 抓不到 ⇒ 第一步跑完就把整个内联块带走，后续步骤**静默跳过**
    （produce 的 fuse_docs / knowledge_produce / video_transcript_ingest / compliance_report
     因此从未在调度路径上运行；video_state.json 陈旧 98.6h 即其症状）。
    现在每步独立隔离：退出码 0/None 视为正常收尾，其余打印出来但不影响后续步骤。
    """
    _old = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as e:
        if getattr(e, "code", 0) not in (0, None):
            print("%s SystemExit:%s" % (name, e.code))
    except BaseException as e:  # noqa: BLE001
        print("%s DEGRADED: %s: %s" % (name, type(e).__name__, str(e)[:140]))
    finally:
        sys.argv = _old

# 658.42/658.45 的四个知识积累步骤 + 视频采集 + 合规报告，逐步隔离执行
_step("harvest_kb_structured", ["harvest_kb_structured", "--limit", "40"],
      r"$TrinityRoot\scripts\harvest_kb_structured.py")
_step("fuse_docs", ["fuse_docs", "--dir", "docs", "--persona", "trinity-docs"],
      r"$TrinityRoot\scripts\fuse_docs.py")
_step("knowledge_produce", ["knowledge_produce", "--days", "1"],
      r"$TrinityRoot\scripts\knowledge_produce.py")
_step("video_transcript_ingest", ["video_transcript_ingest", "--limit", "5"],
      r"$TrinityRoot\scripts\video_transcript_ingest.py")
_step("compliance_report", ["compliance_report"],
      r"$TrinityRoot\scripts\compliance_report.py")
"@
$producePrompt = "运行 knowledge_produce.py（每日周报）+ compliance_report.py（合规报告），汇报产出文件。"

# 复发式整合（658.44，RecMem 启发）：语义聚类识别复发主题 → 生成 consolidated 记忆
# + 提升簇内成员 importance（复发=重要）。幂等（rc:<指纹> 去重）、有界、可 dry-run。
$recurrenceCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["recurrence_consolidate", "--days", "21", "--limit", "1500", "--min-cluster", "3", "--threshold", "0.34", "--promote", "--pe-gate"]
runpy.run_path(r"$TrinityRoot\scripts\recurrence_consolidate.py", run_name="__main__")
"@
$recurrencePrompt = "运行 recurrence_consolidate.py（复发主题整合：簇内 ≥3 条 → consolidated 记忆 + 成员重要性提升），汇报 clusters/consolidated/boosted。"

# 脑模块心跳（658.51）：点亮事件驱动模块（attention/blink/inattention/signal_context）
# 并报告全部脑状态文件新鲜度；有 STALE 时以 exit 1 暴露（原实现无法区分"安静"与"死亡"）。
$brainHeartbeatCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["brain_heartbeat"]
runpy.run_path(r"$TrinityRoot\scripts\brain_heartbeat.py", run_name="__main__")
"@
$brainHeartbeatPrompt = "运行 brain_heartbeat.py（脑模块心跳 + 状态新鲜度判定），汇报 fresh/idle/stale。"

# 写入价值闸门（658.51，对齐《Useful Memories Become Faulty…》警示）：确定性打分
# （长度/具体性/新颖度/溯源/标签）→ 低价值派生记忆归档，防"持续更新稀释记忆"。
$valueGateCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["write_value_gate", "--days", "7", "--limit", "3000", "--threshold", "2.0", "--audit"]
runpy.run_path(r"$TrinityRoot\scripts\write_value_gate.py", run_name="__main__")
"@
$valueGatePrompt = "运行 write_value_gate.py --audit（写入价值闸门：低价值派生记忆归档），汇报 scanned/below_threshold/archived。"

# 四维优先图 + 杏仁核快通道（658.54，对标 ZenBrain PriorityMap）
$priorityCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["priority_map", "--limit", "20"]
runpy.run_path(r"$TrinityRoot\trinity\brain\priority_map.py", run_name="__main__")
"@
$priorityPrompt = "运行 trinity/brain/priority_map.py（四维优先图 + 杏仁核快通道），汇报 scored/amygdala_fast_path。"

# 元认知偏差监测（658.54，对标 ZenBrain MetacognitiveMonitor）
$metacogCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["metacog_monitor", "--rebalance"]
runpy.run_path(r"$TrinityRoot\trinity\brain\metacog_monitor.py", run_name="__main__")
"@
$metacogPrompt = "运行 trinity/brain/metacog_monitor.py（五种偏差监测），汇报 flagged 与建议。"

# 四通道神经调制（658.53，对标 ZenBrain NeuromodulatorEngine）：DA/NE/SHT/ACh 状态推进
$neuroCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["neuromodulate"]
runpy.run_path(r"$TrinityRoot\scripts\neuromodulate.py", run_name="__main__")
"@
$neuroPrompt = "运行 neuromodulate.py（四通道神经调制状态推进），汇报 DA/NE/SHT/ACh 与调制因子。"

# 三副本衰减 + 显式层化（658.53，对标 ZenBrain TripleCopyMemory）
$copiesCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["copies_sweep", "--limit", "4000", "--min-importance", "0.7"]
runpy.run_path(r"$TrinityRoot\scripts\copies_sweep.py", run_name="__main__")
"@
$copiesPrompt = "运行 copies_sweep.py（显式层化 + 三副本衰减维护），汇报 relabeled/layer_distribution/deep_protected。"

# 证据化观察层（658.48，借鉴 Hindsight）：support_count + freshness + 矛盾调和
$observationCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["observation_build", "--days", "30", "--limit", "2000", "--min-support", "3"]
runpy.run_path(r"$TrinityRoot\scripts\observation_build.py", run_name="__main__")
"@
$observationPrompt = "运行 observation_build.py（证据化观察层：主题分组 → support_count/freshness/矛盾状态），汇报 observations/conflicted。"

# markdown-first 大脑导出（658.48，借鉴 GBrain）：可 git 版本化、可人工编辑、可迁移
$brainMdCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
_out = os.path.join(os.path.expanduser("~"), ".trinity", "brain_md")
sys.argv = ["export_memories_markdown", "--out", _out, "--active-only", "--init-git"]
runpy.run_path(r"$TrinityRoot\scripts\export_memories_markdown.py", run_name="__main__")
"@
$brainMdPrompt = "运行 export_memories_markdown.py 导出 markdown 大脑（~/.trinity/brain_md，含 INDEX/AGENTS，git 版本化），汇报导出文件数。"

# 会话 → 候选记忆队列（2026-09-10 EXECUTION 662；OpenViking 会话抽取借鉴）
# 只产候选**不自动写库**：写库仍需显式 --promote（并做写后自检 + 语义判重）。
$sessionCandidatesCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["session_memory_candidates", "--days", "7", "--limit-events", "6000"]
runpy.run_path(r"$TrinityRoot\scripts\session_memory_candidates.py", run_name="__main__")
"@
$sessionCandidatesPrompt = "运行 session_memory_candidates.py 抽取会话候选记忆入队（只产候选不写库），汇报 SRC/CAND/QUEUE 三个数字。"

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
# 2026-09-28 (F1): timeout=1800 here was a STALE DUPLICATE of the gate's own
# budget. It was written 2026-08-28 when fulltest_gate.py used 1500s, and was
# never raised when the gate went to 9000s (fulltest_gate.py PYTEST_TIMEOUT_S).
# A real full run takes 62-73 min (measured 4038s on 09-27, 4390s on 09-28), so
# the OUTER 1800 always fired first: it killed the gate mid-pytest, and because
# the gate writes ALL of its evidence at the END (fulltest_pytest_*.log,
# fulltest_<ts>.json, fulltest_last.json) the run produced NOTHING at all, while
# the orphaned pytest still ran to completion (09-28 12:20:12: 5 failed,
# 3762 passed in 4390s) and its verdict was silently discarded.
# Two occurrences logged the same day, both exactly 30m02s:
#   09:41:56 + 1800 -> 10:11:57   and   11:06:56 + 1800 -> 11:36:57
# Fix: keep this OUTER guard strictly ABOVE the gate's own budget, so the gate is
# always the one that decides and always gets to persist its evidence.
# 9000 (gate) + 900 (evals + evidence writing) = 9900.
try:
    rc = _sp.run([_sys.executable, "-X", "utf8",
                  r"$TrinityRoot\scripts\fulltest_gate.py"],
                  cwd=r"$TrinityRoot", timeout=9900).returncode
except _sp.TimeoutExpired:
    # Outer budget exceeded anyway: fail LOUDLY with an explicit reason instead of
    # dying with a bare traceback, and use a non-zero rc so the task cannot look
    # green. Do NOT "fix" this by lowering the gate's own budget.
    print("FULLTEST OUTER TIMEOUT after 9900s - fulltest_gate.py did not return; "
          "evidence may be partial. Raise this outer budget, never lower the gate's.")
    rc = 124
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

# PG → SQLite 反向同步（2026-09-01 短板 #2 修复）：PG 成为主存储后直写 PG 的记忆
# 从未回流 SQLite（实测分叉 5158 条 active）。本任务按原 memory_id 回填缺失的 active
# 记忆到 SQLite 镜像（加密/FTS/版本链/审计全走 adapter 路径），幂等可日常化。
$pgBackfillCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["backfill_sqlite_from_pg"]
runpy.run_path(r"$TrinityRoot/scripts/backfill_sqlite_from_pg.py", run_name="__main__")
"@
$pgBackfillPrompt = "运行 PG → SQLite active 反向回填（把 PG 主存储中 SQLite 缺失的 active 记忆按原 id 镜像回来，走加密/FTS/审计，幂等），汇报回填条数。"

# 双库对账（2026-09-01）：只读 diff（total/集合差/hash 不一致），供收敛决策
$reconcileCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["reconcile_pg_sqlite"]
runpy.run_path(r"$TrinityRoot/scripts/reconcile_pg_sqlite.py", run_name="__main__")
"@
$reconcilePrompt = "运行 PG vs SQLite 只读对账，汇报 total / pg_only / sq_only / active 集合差 / 哈希不一致数。"

# AGENTS.md 快照刷新（2026-09-01 文档漂移修复）：重建头部快照块（会话/事件/目标/活跃目标/最近会话）
$snapshotCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy

def _guard(path, **kw):

    """逐步隔离（2026-09-14 R41-P5）：子脚本以 raise SystemExit(main()) 收尾，

    SystemExit 属 BaseException，except Exception 抓不到 ⇒ 会带走本块**后续步骤**。

    实测：本块原本只跑得到第一个脚本（第二个及其后从未执行）。"""

    try:

        runpy.run_path(path, run_name=kw.get("run_name", "__main__"))

    except SystemExit as e:

        if getattr(e, "code", 0) not in (0, None):

            print("step SystemExit:%s (%s)" % (e.code, path))

    except BaseException as e:

        print("step DEGRADED: %s: %s (%s)" % (type(e).__name__, str(e)[:120], path))
sys.argv = ["update_agents_snapshot"]
_guard(r"$TrinityRoot/scripts/update_agents_snapshot.py", run_name="__main__")
# 2026-09-01（自传注入）：同步刷新用户全局 AGENTS.md 快照段（dsh-agent-instructions 每会话加载）
_guard(r"$TrinityRoot/scripts/update_dsh_agents_md.py", run_name="__main__")
"@
$snapshotPrompt = "刷新 AGENTS.md 头部快照（会话/事件/目标计数 + 活跃目标 + 最近会话），汇报刷新结果。"

# 大脑体检周报（2026-09-01 大脑化层3）：聚合检索/生成/一致性/巩固/健康/水位 → 周报
$brainReportCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["brain_report"]
runpy.run_path(r"$TrinityRoot/scripts/brain_report.py", run_name="__main__")
"@
$brainReportPrompt = "生成记忆系统大脑体检周报（检索R@5/生成AnswerAcc/一致性/压缩忠实度/服务健康/事件水位），输出 .trinity/bench-results/brain-report-*.md。"

# 市场供给（2026-09-01 生态启动）：高价值记忆自动上架（去重，幂等）
$marketListCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["market_list_high_value", "--top", "20"]
runpy.run_path(r"$TrinityRoot/scripts/market_list_high_value.py", run_name="__main__")
"@
$marketListPrompt = "运行市场供给：挑选高价值记忆（decision/insight/milestone/summary × importance>=0.8）自动上架记忆市场，去重幂等，汇报上架/跳过/失败。"

# 情境流刷新（2026-09-02 大脑化 EXECUTION 457）：情境=持续上下文流（非按查询现算）
$situationCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["run_situation_stream"]
runpy.run_path(r"$TrinityRoot/scripts/run_situation_stream.py", run_name="__main__")
"@
$situationPrompt = "刷新情境持续上下文流（当下信号→situation_stream.json + PG ctx:brain），输出摘要。"

# ops-bot 自治日循环（2026-09-02 EXECUTION 458）：第二 agent 自己思考/决策/上架
$opsbotCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["opsbot_daily"]
runpy.run_path(r"$TrinityRoot/scripts/opsbot_daily.py", run_name="__main__")
"@
$opsbotPrompt = "运行 ops-bot 自治日循环（自身主题→检索→决策记忆→市场上架），输出主题/决策/挂单。开始前先检索并引用 dsh-self 的 core/working 记忆块(category=memory_block)与近期 reflection(category=reflection)作为自我语境；无则略过。"

# 持续感知流（2026-09-02 EXECUTION 458）：inbox 截图→语义视觉→感知记忆→情境流
$perceptionContinuousCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["perception_loop", "--once"]
runpy.run_path(r"$TrinityRoot/scripts/perception_loop.py", run_name="__main__")
"@
$perceptionContinuousPrompt = "持续感知流一轮：处理 perception_inbox 新图（本地语义视觉→感知记忆→刷新情境流），输出 perceived/processed 数。"

# 模块分级刷新（EXECUTION 458D）：重扫 trinity 包 core 引用 → core/reserve/frozen 表
$moduleClassifyCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["module_classify", "--json"]
runpy.run_path(r"$TrinityRoot/scripts/module_classify.py", run_name="__main__")
"@
$moduleClassifyPrompt = "模块分级扫描（core 引用>=3=core / 有引用=reserve / 无=freeze），输出 core/reserve/frozen 计数与 20 大模块。"

# 评测回归门禁（EXECUTION 466）：分层抽样 ~100 题 recall（无 LLM），与基线比 ev<=5 比例
$evalGateCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy

def _guard(path, **kw):

    """逐步隔离（2026-09-14 R41-P5）：子脚本以 raise SystemExit(main()) 收尾，

    SystemExit 属 BaseException，except Exception 抓不到 ⇒ 会带走本块**后续步骤**。

    实测：本块原本只跑得到第一个脚本（第二个及其后从未执行）。"""

    try:

        runpy.run_path(path, run_name=kw.get("run_name", "__main__"))

    except SystemExit as e:

        if getattr(e, "code", 0) not in (0, None):

            print("step SystemExit:%s (%s)" % (e.code, path))

    except BaseException as e:

        print("step DEGRADED: %s: %s (%s)" % (type(e).__name__, str(e)[:120], path))
sys.argv = ["eval_regression_gate", "--update-baseline"]
_guard(r"$TrinityRoot/benchmark/eval_regression_gate.py", run_name="__main__")
sys.argv = ["content_ev_metric"]
_guard(r"$TrinityRoot/benchmark/content_ev_metric.py", run_name="__main__")  # EXECUTION 472: 周检并入内容级证据诊断
"@
$evalGatePrompt = "评测回归门禁：分层抽样 100 题 recall（ev<=5/14 比例），与基线比较，超阈值 FAIL（exit 1）。"

# 自我/意识账本刷新（EXECUTION 473）：自主/社会/自省证据 → 蓝图可消费
$selfUpgradeCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy

def _guard(path, **kw):

    """逐步隔离（2026-09-14 R41-P5）：子脚本以 raise SystemExit(main()) 收尾，

    SystemExit 属 BaseException，except Exception 抓不到 ⇒ 会带走本块**后续步骤**。

    实测：本块原本只跑得到第一个脚本（第二个及其后从未执行）。"""

    try:

        runpy.run_path(path, run_name=kw.get("run_name", "__main__"))

    except SystemExit as e:

        if getattr(e, "code", 0) not in (0, None):

            print("step SystemExit:%s (%s)" % (e.code, path))

    except BaseException as e:

        print("step DEGRADED: %s: %s (%s)" % (type(e).__name__, str(e)[:120], path))
sys.argv = ["brain_self_upgrade"]
_guard(r"$TrinityRoot/scripts/brain_self_upgrade.py", run_name="__main__")
sys.argv = ["brain_self_markers"]
_guard(r"$TrinityRoot/scripts/brain_self_markers.py", run_name="__main__")  # EXECUTION 475: 自-他标记电池
"@
$selfUpgradePrompt = "刷新自主性/社会/自省证据账本（autonomy_ledger.json），供意识蓝图判据消费；输出账本摘要。"

# ops-bot 深度行动（EXECUTION 479）：真实维护任务执行 + 记忆/账本闭环
$deepActionCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["opsbot_deep_action_pilot"]
runpy.run_path(r"$TrinityRoot/scripts/opsbot_deep_action_pilot.py", run_name="__main__")
"@
$deepActionPrompt = "ops-bot 深度行动：备份完整性/心跳/PG 校验→结果入自身决策记忆+deep_actions 计数，输出证据。开始前先检索并引用 dsh-self 的 core/working 记忆块(category=memory_block)与近期 reflection(category=reflection)作为自我语境；无则略过。"

# 质量门禁（2026-09-01 短板 #1）：500q 检索 R@5（keyword/hybrid 逐类目）+ 延迟 + 对账，
# 输出 ~/.trinity/bench-results/quality-gate-*.json；阈值不过 exit 1。显式调用，不进日链。
$qualityGateCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["quality_gate"]
runpy.run_path(r"$TrinityRoot/scripts/quality_gate.py", run_name="__main__")
"@
$qualityGatePrompt = "运行 500q 检索质量门禁（R@5 keyword/hybrid 逐类目 + p50/p95 延迟 + 对账摘要），按阈值判 PASS/FAIL。"

# DSH 插件冒烟（2026-09-01 rc.7 契约回归）：bundle 层 + trinity_ping + 水位推进
$pluginSmokeCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["dsh_plugin_smoke"]
runpy.run_path(r"$TrinityRoot/scripts/dsh_plugin_smoke.py", run_name="__main__")
"@
$pluginSmokePrompt = "运行 DSH dsh-trinity 插件冒烟：合成配置含 trinity 层 + headless 会话 trinity_ping + dsh_events 水位推进。"

# AnswerAcc 评测（2026-09-01 中期方向：生成侧周度例行，500q LLM，20-30 分钟）
$answerEvalCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["answer_eval_guard", "--limit", "500", "--ms-top-k", "20", "--ms-ctx-len", "900"]  # 2026-09-06 EXECUTION 595: 经守卫运行(心跳+悬挂自愈)
runpy.run_path(r"$TrinityRoot/scripts/answer_eval_guard.py", run_name="__main__")
"@
$answerEvalPrompt = "运行 500q AnswerAcc 生成侧评测（检索 top-5 + DeepSeek 生成 + 事实包含率判分），输出 output/answer_eval_results.json。"

# 每日 auto-evolve（2026-08-29 递归闭环真实使用）：无人值守补丁（门禁+回滚）
$evolveCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["evolve_patch", "--target", "scripts/tune_report.py", "--goal", "improve robustness (add defensive guard if missing)", "--apply", "--auto"]
runpy.run_path(r"$TrinityRoot\scripts\evolve_patch.py", run_name="__main__")
"@
$evolvePrompt = "运行 auto-evolve（每日真实小目标无人值守——门禁通过自动合入），汇报补丁结果。"

# 过期复核队列（2026-09-02 Fable 对照 459.4）：扫 metadata expires_at 到期/临期记忆
# → ~/.trinity/state/expiry_review_*.json 复核队列（dry-run 默认只出队列不动库）
$expiryReviewCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
os.environ.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")
import runpy
sys.argv = ["expiry_review", "--horizon-days", "7", "--dry-run"]
runpy.run_path(r"$TrinityRoot\scripts\run_expiry_review.py", run_name="__main__")
"@
$expiryReviewPrompt = "运行 scripts/run_expiry_review.py（expires_at 临期/到期复核队列，dry-run 只出清单）"

$smokeCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["smoke"]
runpy.run_path(r"$TrinityRoot\scripts\canary_smoke.py", run_name="__main__")
"@
$smokePrompt = "秒级检索冒烟 canary（EXECUTION 555/556）：后端隔离守卫+检索命中+延迟，输出 CANARY 摘要"

$rewardsCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["rewards"]
runpy.run_path(r"$TrinityRoot\scripts\apply_bandit_rewards.py", run_name="__main__")
"@
$rewardsPrompt = "纠错日志→路由 bandit 奖励（EXECUTION 555/556）：rl_feedback_journal 按 channel 归因喂 UCB1 学习器"

$nightlyCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["run_gate_tiers", "--tier", "nightly"]
runpy.run_path(r"$TrinityRoot\scripts\run_gate_tiers.py", run_name="__main__")
"@
$nightlyPrompt = "分层评测 nightly（EXECUTION 555/556）：canary+quality_gate 500q+eval_regression_gate 100q 门禁"

$actionLoopCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy

def _guard(path, **kw):

    """逐步隔离（2026-09-14 R41-P5）：子脚本以 raise SystemExit(main()) 收尾，

    SystemExit 属 BaseException，except Exception 抓不到 ⇒ 会带走本块**后续步骤**。

    实测：本块原本只跑得到第一个脚本（第二个及其后从未执行）。"""

    try:

        runpy.run_path(path, run_name=kw.get("run_name", "__main__"))

    except SystemExit as e:

        if getattr(e, "code", 0) not in (0, None):

            print("step SystemExit:%s (%s)" % (e.code, path))

    except BaseException as e:

        print("step DEGRADED: %s: %s (%s)" % (type(e).__name__, str(e)[:120], path))
sys.argv = ["action_loop_tick"]
_guard(r"$TrinityRoot\scripts\action_loop_tick.py", run_name="__main__")
sys.argv = ["closed_loop_check"]
_guard(r"$TrinityRoot\scripts\closed_loop_check.py", run_name="__main__")
"@
$actionLoopPrompt = "action-loop 日任务（EXECUTION 558 补注册, 2026-09-04）"

$brainHealthCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["brain_health_check"]
runpy.run_path(r"$TrinityRoot\scripts\brain_health_check.py", run_name="__main__")
"@
$brainHealthPrompt = "brain-health 日任务（EXECUTION 558 补注册, 2026-09-04）"

$brainificationGuardCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["brainification_guard"]
runpy.run_path(r"$TrinityRoot\scripts\brainification_guard.py", run_name="__main__")
"@
$brainificationGuardPrompt = "brainification-guard 日任务（EXECUTION 558 补注册, 2026-09-04）"

$capabilityCheckCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["capability_self_check"]
runpy.run_path(r"$TrinityRoot\scripts\capability_self_check.py", run_name="__main__")
"@
$capabilityCheckPrompt = "capability-check 日任务（EXECUTION 558 补注册, 2026-09-04）"

$curiosityCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["curiosity_daily"]
runpy.run_path(r"$TrinityRoot\scripts\curiosity_daily.py", run_name="__main__")
"@
$curiosityPrompt = "curiosity 日任务（EXECUTION 558 补注册, 2026-09-04）"

$dreamReplayCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["dream_replay"]
runpy.run_path(r"$TrinityRoot\scripts\dream_replay.py", run_name="__main__")
"@
$dreamReplayPrompt = "dream-replay 日任务（EXECUTION 558 补注册, 2026-09-04）"

$driftCheckCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["config_drift_check"]
runpy.run_path(r"$TrinityRoot\scripts\config_drift_check.py", run_name="__main__")
"@
$driftCheckPrompt = "drift-check 日任务（EXECUTION 558 补注册, 2026-09-04）"
$auditReconcileCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["audit_reconcile", "--db", os.path.expanduser("~/.trinity/store/trinity_store.db"), "--state-dir", os.path.expanduser("~/.trinity/state"), "--since-hours", "168"]
runpy.run_path(r"$TrinityRoot\scripts\audit_reconcile.py", run_name="__main__")
"@
$auditReconcilePrompt = "audit-reconcile 周任务（EXECUTION 567：审计↔OS 痕迹对账，幽灵行/漏记检测）"

$contradictionResolveCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["contradiction_resolve", "--db", os.path.expanduser("~/.trinity/store/trinity_store.db"), "--out", os.path.expanduser("~/.trinity/state/contradiction_resolutions.jsonl")]
runpy.run_path(r"$TrinityRoot\scripts\contradiction_resolve.py", run_name="__main__")
"@
$contradictionResolvePrompt = "contradiction-resolve 周任务（EXECUTION 567：contradicts 时间加权迁移链）"

$flagMonitorCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["agent_flag_monitor", "--db", os.path.expanduser("~/.trinity/store/trinity_store.db"), "--days", "7", "--strict", "--strict-baseline", "120", "--out", os.path.expanduser("~/.trinity/state/agent_flags_report.json")]
runpy.run_path(r"$TrinityRoot\scripts\agent_flag_monitor.py", run_name="__main__")
"@
$flagMonitorPrompt = "flag-monitor 周任务（EXECUTION 567：headless agent 运行旗标率）"

$ipiCheckCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["ipi_eval", "--offline", "--out", os.path.expanduser("~/.trinity/state/ipi_report.json")]
runpy.run_path(r"$TrinityRoot\scripts\ipi_eval.py", run_name="__main__")
"@
$ipiCheckPrompt = "ipi-check 周任务（EXECUTION 567：IPI 语料有效性+读回标注率）"

$marketDrillCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["market_supply_chain_drill", "--offline", "--out", os.path.expanduser("~/.trinity/state/market_drill_report.json")]
runpy.run_path(r"$TrinityRoot\scripts\market_supply_chain_drill.py", run_name="__main__")
"@
$marketDrillPrompt = "market-drill 周任务（EXECUTION 567：市场供应链三防线演练）"
$blindJudgeCmd = @"
import sys, os
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["blind_judge_weekly"]
runpy.run_path(r"$TrinityRoot\scripts\blind_judge_weekly.py", run_name="__main__")
"@
$blindJudgePrompt = "blind-judge 周任务（EXECUTION 574：oracle 双臂盲评——真实检索×记忆供给 honesty 留档；无 key 自动跳过）"

$emotionalConsolidationCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["emotional_consolidation_daily"]
runpy.run_path(r"$TrinityRoot\scripts\emotional_consolidation_daily.py", run_name="__main__")
"@
$emotionalConsolidationPrompt = "emotional-consolidation 日任务（EXECUTION 558 补注册, 2026-09-04）"

$identityRefreshCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["identity_refresh_daily"]
runpy.run_path(r"$TrinityRoot\scripts\identity_refresh_daily.py", run_name="__main__")
"@
$identityRefreshPrompt = "identity-refresh 日任务（EXECUTION 558 补注册, 2026-09-04）"

$loopAuditCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["anti_loop_self_check"]
runpy.run_path(r"$TrinityRoot\scripts\anti_loop_self_check.py", run_name="__main__")
"@
$loopAuditPrompt = "loop-audit 日任务（EXECUTION 558 补注册, 2026-09-04）"

$memoryManagerCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["memory_manager_daily"]
runpy.run_path(r"$TrinityRoot\scripts\memory_manager_daily.py", run_name="__main__")
"@
$memoryManagerPrompt = "memory-manager 日任务（EXECUTION 558 补注册, 2026-09-04）"

$narrativeCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["narrative_daily"]
runpy.run_path(r"$TrinityRoot\scripts\narrative_daily.py", run_name="__main__")
"@
$narrativePrompt = "narrative 日任务（EXECUTION 558 补注册, 2026-09-04）"

$predictiveLoopCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["predictive_loop_daily"]
runpy.run_path(r"$TrinityRoot\scripts\predictive_loop_daily.py", run_name="__main__")
"@
$predictiveLoopPrompt = "predictive-loop 日任务（EXECUTION 558 补注册, 2026-09-04）"

$proactiveCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["proactive_daily"]
runpy.run_path(r"$TrinityRoot\scripts\proactive_daily.py", run_name="__main__")
"@
$proactivePrompt = "proactive 日任务（EXECUTION 558 补注册, 2026-09-04）"

$selfAssessCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["self_assess_daily"]
runpy.run_path(r"$TrinityRoot\scripts\self_assess_daily.py", run_name="__main__")
"@
$selfAssessPrompt = "self-assess 日任务（EXECUTION 558 补注册, 2026-09-04）"

$selfAxiomsCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["self_axioms_daily"]
runpy.run_path(r"$TrinityRoot\scripts\self_axioms_daily.py", run_name="__main__")
"@
$selfAxiomsPrompt = "self-axioms 日任务（EXECUTION 558 补注册, 2026-09-04）"

$sensoryIntegrationCmd = @"
import sys
sys.path.insert(0, r"$TrinityRoot")
import runpy
sys.argv = ["sensory_integration_daily"]
runpy.run_path(r"$TrinityRoot\scripts\sensory_integration_daily.py", run_name="__main__")
"@
$sensoryIntegrationPrompt = "sensory-integration 日任务（EXECUTION 558 补注册, 2026-09-04）"

# dream 周任务（EXECUTION 621）: dream_round v0 dry（journal 回放→promote/suppress，不触库）
$dreamCmd = @"
import subprocess, sys, os
root = r"C:\Users\Administrator\trinity"
r = subprocess.run([sys.executable, os.path.join(root, 'scripts', 'dream_round.py'), '--days', '7'],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
sys.stdout.write((r.stdout or '')[-800:])
sys.stderr.write((r.stderr or '')[-400:])
sys.exit(r.returncode)
"@
$dreamPrompt = "dream 任务(dry): 运行 scripts/dream_round.py --days 7，回报报告路径与统计"

# perception-archival 任务（EXECUTION 624）: 感知/收割低值归档 dry（默认不 apply）
$perceptionArchivalCmd = @"
import subprocess, sys, os
root = r"C:\Users\Administrator\trinity"
r = subprocess.run([sys.executable, os.path.join(root, 'scripts', 'run_perception_archival.py'),
                     '--apply', '--max-importance', '0.75', '--limit', '300'],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
sys.stdout.write((r.stdout or '')[-800:])
sys.stderr.write((r.stderr or '')[-400:])
sys.exit(r.returncode)
"@
$perceptionArchivalPrompt = "perception-archival 任务(有界 apply 300 条/日, max-importance 0.75, CSV 备份可回滚): run_perception_archival.py 汇报归档数"
# EXECUTION 625: prewarm (B3 cold-start warmup; TTL 6h auto-skip, --force to override)
# 2026-10-05：**改掉嵌套子进程 + 管道**（原实现会死锁）。
#   原写法：`subprocess.run([...run_prewarm.py], capture_output=True, ..., timeout=1700)`
#   ⇒ 而 `Invoke-Task` 把本段写进临时 `.py` 再交给 `$Py` 跑 ⇒ **两层子进程 + 管道嵌套**：
#     内层子进程的输出写进管道，若超过管道缓冲，而外层又在等它退出 ⇒ **双方互等**。
#   实证：`-Tasks prewarm` 连续两次都在 **1700s 被 TASK BUDGET 掐死**（exit 124）、
#   `~/.trinity/data/corpus_vec*` 为空；而**直接**跑 `scripts/run_prewarm.py` 只要 ~136s 并正常落盘。
#   原先只跑 jieba/FTS（输出极少）所以从未暴露；加上语料预热（会打印进度）后就触发了。
#   修法：**同进程 `runpy` 执行**（无嵌套子进程、无管道）—— 输出直接走本进程 stdout，
#   由 PowerShell 单层管道接收，不再存在"子进程写满管道而我们不读"的形态。
# 回滚：恢复原来的 subprocess.run 写法（注释保留上述风险说明）。
$prewarmCmd = @"
import runpy, sys, os
sys.path.insert(0, r"$TrinityRoot")
try:
    runpy.run_path(os.path.join(r"$TrinityRoot", "scripts", "run_prewarm.py"), run_name="__main__")
    sys.exit(0)
except SystemExit as e:
    sys.exit(int(e.code or 0))
except Exception as e:
    sys.stderr.write("prewarm failed: %r\n" % (e,))
    sys.exit(1)
"@
$prewarmPrompt = "prewarm task (EXECUTION 625 B3): run_prewarm.py cold-start warmup (jieba/FTS/vector/ANN probe, read-only, TTL 6h)"

# EXECUTION 625: reason slow-latency alert (threshold default 30s, watchdog 35s)
$reasonSlowAlertCmd = @"
import sys, os, subprocess
sys.path.insert(0, r"$TrinityRoot")
r = subprocess.run([sys.executable, os.path.join(r"$TrinityRoot", "scripts", "check_reason_latency.py")], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
sys.stdout.write((r.stdout or "")[-1200:])
sys.stderr.write((r.stderr or "")[-400:])
sys.exit(r.returncode)
"@
$reasonSlowAlertPrompt = "reason-slow-alert task (EXECUTION 625): check_reason_latency.py isolated probe, alert file on >30s"

# EXECUTION 625: PG pool read-only smoke
$pgPoolSmokeCmd = @"
import sys, os, subprocess
sys.path.insert(0, r"$TrinityRoot")
r = subprocess.run([sys.executable, os.path.join(r"$TrinityRoot", "scripts", "pg_pool_smoke.py")], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)
sys.stdout.write((r.stdout or "")[-1200:])
sys.stderr.write((r.stderr or "")[-400:])
sys.exit(r.returncode)
"@
$pgPoolSmokePrompt = "pg-pool-smoke task (EXECUTION 625): read-only pooled connection smoke against 127.0.0.1:5432"

# EXECUTION 770 (2026-09-16): DuckDB 分析层——只读 ATTACH PG，零写入、零常驻服务
$analyticsCmd = @"
import sys, os, subprocess
sys.path.insert(0, r"$TrinityRoot")
r = subprocess.run([sys.executable, os.path.join(r"$TrinityRoot", "scripts", "analytics_duckdb.py"), "--snapshot"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
sys.stdout.write(r.stdout or "")
sys.stderr.write(r.stderr or "")
sys.exit(r.returncode)
"@
$analyticsPrompt = "运行 scripts/analytics_duckdb.py --snapshot（DuckDB 只读分析 PG 主存储：概览/每日写入/类目/标签/数据质量 + 每日快照趋势），汇报总数、active、加密占比与质量三项。"

# EXECUTION 625: store growth threshold alert (read-only; total>1000/d or archived>2000/d -> exit1)
$storeGrowthCmd = @"
import sys, os, subprocess
sys.path.insert(0, r"$TrinityRoot")
r = subprocess.run([sys.executable, os.path.join(r"$TrinityRoot", "scripts", "check_store_growth.py"), "--check-regression"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
sys.stdout.write((r.stdout or "")[-1200:])
sys.stderr.write((r.stderr or "")[-400:])
sys.exit(r.returncode)
"@
$storeGrowthPrompt = "store-growth task (EXECUTION 625 R1): check_store_growth.py read-only water level + threshold alert"
$diskGrowthCmd = @"
import sys, os, subprocess
sys.path.insert(0, r"$TrinityRoot")
r = subprocess.run([sys.executable, os.path.join(r"$TrinityRoot", "scripts", "check_disk_growth.py")], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
sys.stdout.write((r.stdout or "")[-1200:])
sys.stderr.write((r.stderr or "")[-400:])
sys.exit(r.returncode)
"@
$diskGrowthPrompt = "disk-growth task (2026-09-14): check_disk_growth.py disk water-level trend (C:/D:, 14-point regression -> days_to_full; exit 1 only when <7d)"

# EXECUTION 625: archive-side purge candidate audit (dry only; --apply refuses)
$archivePurgeAuditCmd = @"
import sys, os, subprocess
sys.path.insert(0, r"$TrinityRoot")
r = subprocess.run([sys.executable, os.path.join(r"$TrinityRoot", "scripts", "audit_archive_purge.py"), "--days", "90"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
sys.stdout.write((r.stdout or "")[-1200:])
sys.stderr.write((r.stderr or "")[-400:])
sys.exit(r.returncode)
"@
$archivePurgeAuditPrompt = "archive-purge-audit task (EXECUTION 625): audit_archive_purge.py dry candidate CSV (weekly explicit; apply never automated)"

# EXECUTION 625: decay real-LLM summary (dry default; apply needs --yes + CSV backup; audit annotation only)
$decayRealLlmCmd = @"
import sys, os, subprocess
sys.path.insert(0, r"$TrinityRoot")
r = subprocess.run([sys.executable, os.path.join(r"$TrinityRoot", "scripts", "decay_real_llm.py"), "--limit", "10"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
sys.stdout.write((r.stdout or "")[-1200:])
sys.stderr.write((r.stderr or "")[-400:])
sys.exit(r.returncode)
"@
$decayRealLlmPrompt = "decay-real-llm task (EXECUTION 625): decay_real_llm.py dry candidate list (explicit; never in all chain)"

# EXECUTION 625: P2 write-policy pilot 2-week evaluation aggregator
$writePolicyEvalCmd = @"
import sys, os, subprocess
sys.path.insert(0, r"$TrinityRoot")
r = subprocess.run([sys.executable, os.path.join(r"$TrinityRoot", "scripts", "write_policy_eval.py"), "--days", "14"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
sys.stdout.write((r.stdout or "")[-1200:])
sys.stderr.write((r.stderr or "")[-400:])
sys.exit(r.returncode)
"@
$writePolicyEvalPrompt = "write-policy-eval task (EXECUTION 625): aggregate pilot_events.jsonl 2-week summary (read-only)"

# EXECUTION 625: cluster stress raft re-check (isolated 3-node; 5/5 checks, ~1s)
$clusterStressCmd = @"
import sys, os, subprocess
sys.path.insert(0, r"$TrinityRoot")
r = subprocess.run([sys.executable, os.path.join(r"$TrinityRoot", "benchmark", "cluster_stress.py"), "--num-nodes", "3", "--num-writes", "60", "--workers", "3"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
sys.stdout.write((r.stdout or "")[-1600:])
sys.stderr.write((r.stderr or "")[-400:])
sys.exit(r.returncode)
"@
$clusterStressPrompt = "cluster-stress task (EXECUTION 625): isolated 3-node raft stress re-check (5/5 checks)"

# EXECUTION 626: 元层策略外环 meta-strategy（借鉴 EvoX arXiv:2602.23413；status/check/score/task-series/descriptor 只读；写操作需 --apply；默认不进 all 链与 03:00 链）
$metaStrategyCmd = @"
import sys, os, subprocess
sys.path.insert(0, r"$TrinityRoot")
r = subprocess.run([sys.executable, os.path.join(r"$TrinityRoot", "scripts", "meta_strategy.py"), "check"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
sys.stdout.write((r.stdout or "")[-1600:])
sys.stderr.write((r.stderr or "")[-400:])
sys.exit(r.returncode)
"@
$metaStrategyPrompt = "meta-strategy task (EXECUTION 626): scripts/meta_strategy.py check — 停滞检测+J打分+状态描述子（只读；建议每周显式跑一次；写注册表/调 LLM 需人工 --apply）"

# EXECUTION 629: meta-strategy propose 显式任务（TRINITY_META_STRATEGY_AUTO=on 时 --apply 无人值守；否则 dry 只读）
$metaStrategyProposeCmd = @"
import sys, os, subprocess
sys.path.insert(0, r"$TrinityRoot")
args = [sys.executable, os.path.join(r"$TrinityRoot", "scripts", "meta_strategy.py"), "propose", "--force"]
if os.environ.get("TRINITY_META_STRATEGY_AUTO", "").lower() in ("on", "1", "true"):
    args.append("--apply")
r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
sys.stdout.write((r.stdout or "")[-1600:])
sys.stderr.write((r.stderr or "")[-400:])
sys.exit(r.returncode)
"@
$metaStrategyProposePrompt = "meta-strategy-propose task (EXECUTION 629): propose --force；AUTO=on 才 --apply（预算门 $2/次）"

# EXECUTION 634: PG 向量增量回填（embedding IS NULL 幂等续传；ollama bge-m3）
$pgEmbedCmd = @"
import sys, os, subprocess
sys.path.insert(0, r"$TrinityRoot")
r = subprocess.run([sys.executable, os.path.join(r"$TrinityRoot", "scripts", "backfill_pg_embeddings.py")], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2400)
sys.stdout.write((r.stdout or "")[-1500:])
sys.stderr.write((r.stderr or "")[-400:])
sys.exit(r.returncode)
"@
$pgEmbedPrompt = "pg-embed task (EXECUTION 634): backfill_pg_embeddings.py 增量回填(幂等/断点续传) — 已挂 03:00 日链"

# EXECUTION 646: 闭环心跳/停滞看护（只读）
$loopHealthCmd = @"
import sys, os, subprocess
sys.path.insert(0, r"$TrinityRoot")
r = subprocess.run([sys.executable, os.path.join(r"$TrinityRoot", "scripts", "loop_health.py")], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
sys.stdout.write((r.stdout or "")[-1500:])
sys.stderr.write((r.stderr or "")[-300:])
sys.exit(r.returncode)
"@
$loopHealthPrompt = "loop-health task (EXECUTION 646): 闭环心跳看护(cycle/journal/obs/cog/teach stale 阈值) — 建议每日"


# ── 选择任务 ──────────────────────────────────────────────────────────────
if ($Tasks -contains "all") { $Tasks = @("health", "evolution", "mirror", "decay", "tiers", "consolidate", "dedup", "sync", "compact", "pagetree", "backup", "brain-status", "selftest"); Write-Log ("[all] WARNING: 'all' expands to a HISTORICAL 12-task subset, NOT all {0} registered tasks -- use an explicit -Tasks list for full coverage" -f $allowed.Count) "WARN" }
if ($Tasks -contains "all-full") { $Tasks = @($allowed | Where-Object { $_ -ne "all" -and $_ -ne "all-full" }); Write-Log ("[all-full] expanding to ALL {0} registered tasks -- HEAVY run (decay/consolidate/fulltest/cluster-stress included); use a narrow -Tasks list for smoke tests" -f $Tasks.Count) "WARN" }
if ($Tasks -contains "compress") { $Tasks = @($Tasks | Where-Object { $_ -ne "compress" }) + "decay" }

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
Write-Log "maintenance start (mode=$(if ($ViaDsh) {'ViaDsh'} else {'Direct'}), tasks=$($Tasks -join ','), dryrun=$DryRun)"

# ── 2026-09-28：长任务运行锁（仅 fulltest）─────────────────────────────────
# 背景（实测，不是推测）：2026-09-28 13:16:56 loop-guard 发起的 fulltest 跑了
# **4914s（82 分钟）**，而 supervisor 每 5 分钟发起的 consolidate-recent 在**同期并发**
# 执行（14:38:50 一条日志里同时出现 fulltest 收尾与 consolidate-recent 收尾）。
# 在同一份 2.4 GB SQLite 上叠两条重活，是全量门禁里两个"隔离跑必过、套内必挂"用例
# （test_pool_persist_retry / test_task_budget_guardrail，均实测 isolated=全绿）
# 最可能的解释；本仓事故表也记过多次 maintenance 因重活叠加而 timeout。
#
# 为什么只锁 fulltest：它是唯一被实测到 80 分钟级的任务。给**所有**调用加运行锁会把
# "可见的并发"换成"静默的饿死"——那只是把一种 green-but-empty 换成另一种，所以不做。
#
# 为什么用命名互斥体而不是像日链那样的锁文件：
#   ① 进程异常退出时由 OS 自动释放，不会留下陈旧锁（日链锁需要 150 分钟 stale 接管）；
#   ② 它**不依赖进程枚举**——本机实测枚举面被裁剪（0 实例 ≠ 不存在），任何基于
#      Get-Process 的"是否已在跑"判断在这里都是死代码。
$isLongTask = ($Tasks -contains 'fulltest')
if ($isLongTask -and -not $DryRun) {
    $runMutex = $null
    try { $runMutex = New-Object System.Threading.Mutex($false, "LocalTrinityDshMaintenanceLongRun") } catch { }
    $runOk = $true
    if ($runMutex) {
        try { $runOk = $runMutex.WaitOne(1000) }
        catch [System.Threading.AbandonedMutexException] { $runOk = $true }  # 上一持有者异常退出：锁归本进程
        catch { $runOk = $true }                                            # 互斥体不可用时 fail-open，只此一种
    }
    if (-not $runOk) {
        Write-Log "another LONG maintenance run (fulltest) is already in progress - SKIP this invocation; rc=3 so this CANNOT be read as success" "WARN"
        exit 3
    }
}

# EXECUTION 626 收口: python stdio 统一 utf-8（消除 cluster-stress 等经本包装运行时的 GBK 解码噪音；仅影响 python 子进程编码）
$env:PYTHONIOENCODING = 'utf-8'

# ── 2026-09-11（体检 660 P0）：日链互斥锁 ───────────────────────────────────
# 背景：autostart 循环在 2026-09-09 21:21 启动后内存里是旧版循环脚本（PowerShell
# 启动时只解析一次），因此对循环脚本的修复对已运行实例无效；经计划任务重启后新旧
# 两个循环并存（旧进程提权，Stop-Process 报 Access denied 无法终止），二者都会在
# 03:00 触发同一条 33 任务日链。并发重跑会打满连接池（2026-09-11 03:12 实测 905 条
# FATAL: sorry, too many clients already）。
# 本锁刻意放在**维护脚本自身**而非循环脚本里：Invoke-Script 每次都以
# powershell -File 重新加载本文件，故新旧两个循环都会经过此处，才能真正互斥。
# 仅当日链（任务表含 pg-backfill）加锁，短任务链互不影响。
$isDailyChain = $false
foreach ($t in $Tasks) { if ($t -eq 'pg-backfill') { $isDailyChain = $true } }
if ($isDailyChain -and -not $DryRun) {
    $dailyLock = Join-Path $env:USERPROFILE '.trinity\state\daily_chain.lock'
    $lockOk = $false
    try {
        $null = New-Item -ItemType File -Path $dailyLock -ErrorAction Stop
        $lockOk = $true
        Set-Content -Path $dailyLock -Value ("{0} pid={1} tasks={2}" -f (Get-Date -Format o), $PID, $Tasks.Count) -Encoding UTF8
    } catch {
        $lockAge = 9999
        try { $lockAge = ((Get-Date) - (Get-Item $dailyLock).LastWriteTime).TotalMinutes } catch { }
        if ($lockAge -gt 150) {
            Write-Log ("daily-chain lock stale ({0:N0} min) - taking over" -f $lockAge) "WARN"
            Set-Content -Path $dailyLock -Value ("{0} pid={1} (stale takeover)" -f (Get-Date -Format o), $PID) -Encoding UTF8
            $lockOk = $true
        } else {
            Write-Log ("daily chain already running (lock age {0:N1} min) - SKIP this invocation" -f $lockAge) "WARN"
        }
    }
    if (-not $lockOk) {
        # 2026-09-28：原来这里是 `exit 0` —— 于是"因为别人在跑所以什么都没做"
        # 在调用方看来与"跑完且成功"完全一样（loop-guard 会记 maintenance OK）。
        # 这正是本仓反复出现的 green-but-empty。SKIP 必须与成功可区分：改用 rc=3。
        Write-Log "daily chain SKIPPED (another daily chain is running) - rc=3, NOT success" "WARN"
        exit 3
    }
    Register-EngineEvent -SourceIdentifier PowerShell.Exiting -SupportEvent -Action {
        Remove-Item (Join-Path $env:USERPROFILE '.trinity\state\daily_chain.lock') -Force -ErrorAction SilentlyContinue
    } | Out-Null
}

# 2026-09-20 §1004：应用单任务预算默认表（函数定义见文件上部 Initialize-TaskBudgets）。
# 位置：**任务循环之前、Write-Log 定义之后** —— 输出会进 dsh-maintenance.log，
# 让「这一轮到底用了什么预算」可事后核对（用例判据①加强版断言这一行存在）。
$appliedBudgets = Initialize-TaskBudgets
if (@($appliedBudgets.Keys).Count -gt 0) {
    $budgetPairs = @($appliedBudgets.GetEnumerator() | Sort-Object Name | ForEach-Object { $_.Key + '=' + $_.Value + 's' })
    Write-Log ('task-budget defaults applied: ' + ($budgetPairs -join ', '))
}

$windowBlocked = $false   # D6 选项 B（2026-09-22 执行）：resource-window 判 BLOCKED 时置真 ⇒ 跳过重活
# 2026-09-28（§1394.23）：把 `fulltest` 也登记进**窗口跳过量表** —— 它是最重的任务（实测 67 分钟 / 3703 条用例），
# 原先只有 selftest/produce 会被跳过 ⇒ 「窗口 BLOCKED 时照样跑全量门禁」是个真缺口（新增任务请求通道后更必须堵上）。
# 判据：`tests/unit/test_window_skip_ratchet.py`（新增重活漏登记即红）。
$windowSkipTasks = @('selftest', 'produce', 'fulltest')
foreach ($t in $Tasks) {
    if ($windowBlocked -and ($windowSkipTasks -contains $t)) {
        Write-Log ("SKIPPED(窗口阻塞): 跳过重活 '" + $t + "' -- D6 选项 B 的既定行为（commit 内存不足时保护服务）；本轮验证记录缺失，下周须复核。") "WARN"
        continue
    }
    # A清单受控启用（EXECUTION 624）: 本地臂 flag 存在→仅批次上下文启用（交互/检索路径不受影响；DS 复核终稿保障）
    if (Test-Path (Join-Path $env:USERPROFILE '.trinity\local_reasoner_enabled')) {
        $env:TRINITY_LOCAL_REASONER_USE = 'on'
        $env:TRINITY_SUMMARY_LOCAL_DRAFT = 'on'
        $env:TRINITY_BLIND_LOCAL = 'on'
        Write-Log 'local-reasoner batch enable: on (flag present; interactive paths unaffected)'
    }

    switch ($t) {
        "perception-archival" { Invoke-Task -Name "perception-archival" -DirectCommand $perceptionArchivalCmd -DshPrompt $perceptionArchivalPrompt }  # EXECUTION 624
        "dream"     { Invoke-Task -Name "dream" -DirectCommand $dreamCmd -DshPrompt $dreamPrompt }  # EXECUTION 621
        "health"    { Invoke-Task -Name "health"    -DirectCommand $healthCmd -DshPrompt $healthPrompt }
        "evolution" {
            # 2026-08-16: 先喂 API analyzer(审计回放)再触发 API 进化周期,最后跑 MetaEvolution
            # 2026-09-07: 加重试——历史失败全为 API 重启/预热窗口的瞬时连接拒绝（原无重试直接 WARN）
            & "$Py" "$LogDir\feed_evolution.py" 2>&1 | Out-Null
            $evoOk = $false
            for ($ei = 0; $ei -lt 3 -and -not $evoOk; $ei++) {
                try { Invoke-RestMethod -Uri "http://127.0.0.1:8001/evolution/cycle/run" -Method Post -TimeoutSec 120 | Out-Null; $evoOk = $true }
                catch { if ($ei -lt 2) { Start-Sleep -Seconds 20 } }
            }
            if (-not $evoOk) { Write-Log "API evolution cycle failed after 3 tries" "WARN" }
            Invoke-Task -Name "evolution" -DirectCommand $evoCmd -DshPrompt $evoPrompt
        }
        "decay"     { Invoke-Task -Name "decay"         -DirectCommand $decayCmd  -DshPrompt $decayPrompt }
        "tiers"     { Invoke-Task -Name "tiers"         -DirectCommand $tiersCmd  -DshPrompt $tiersPrompt }
        "mirror"    { Invoke-Task -Name "mirror"       -DirectCommand $mirrorCmd -DshPrompt $mirrorPrompt }
        "consolidate" { Invoke-Task -Name "consolidate" -DirectCommand $consolidateCmd -DshPrompt $consolidatePrompt }
        "consolidate-recent" { Invoke-Task -Name "consolidate-recent" -DirectCommand $consolidateRecentCmd -DshPrompt $consolidateRecentPrompt }  # 2026-09-01 事件驱动巩固
        "dedup"      { Invoke-Task -Name "dedup"           -DirectCommand $dedupCmd      -DshPrompt $dedupPrompt }
        "sync"      { Invoke-Task -Name "sync"           -DirectCommand $syncCmd   -DshPrompt $syncPrompt }
        "agent-sync" { Invoke-Task -Name "agent-sync" -DirectCommand $agentSyncCmd -DshPrompt $agentSyncPrompt }  # 2026-08-21 多机同步
        "pool-sync" { Invoke-Task -Name "pool-sync" -DirectCommand $poolSyncCmd -DshPrompt $poolSyncPrompt }  # 2026-08-21 P0-2 聚合池水位同步（维护窗口任务）
        "consistency" { Invoke-Task -Name "consistency" -DirectCommand $consistencyCmd -DshPrompt $consistencyPrompt }  # 2026-08-21 治理层只读一致性校验（显式调用，不进 all 链）
        "compact"   { Invoke-Task -Name "compact"     -DirectCommand $compactCmd  -DshPrompt $compactPrompt }
        "pagetree"  { Invoke-Task -Name "pagetree"   -DirectCommand $pagetreeCmd -DshPrompt $pagetreePrompt }  # 2026-08-26 PageIndex 借鉴
        "eval"      { Invoke-Task -Name "eval"      -DirectCommand $evalCmd      -DshPrompt $evalPrompt }  # 2026-08-26 DSH 借鉴
        "review"    { Invoke-Task -Name "review"    -DirectCommand $reviewCmd   -DshPrompt $reviewPrompt }  # 2026-08-26 Claude Science 借鉴
        "usage"     { Invoke-Task -Name "usage"     -DirectCommand $usageCmd     -DshPrompt $usagePrompt }  # 2026-08-27 使用伙伴闭环
        "rollout-audit" { Invoke-Task -Name "rollout-audit" -DirectCommand $rolloutAuditCmd -DshPrompt $rolloutAuditPrompt }  # 2026-08-27 rollout 审计
        "selftest"  { Invoke-Task -Name "selftest"  -DirectCommand $selftestCmd -DshPrompt $selftestPrompt }
        "brain-status" { Invoke-Task -Name "brain-status" -DirectCommand $brainStatusCmd -DshPrompt $brainStatusPrompt }  # 2026-09-14 H1-1
        "session-summarize" { Invoke-Task -Name "session-summarize" -DirectCommand $sessionSummaryCmd -DshPrompt $sessionSummaryPrompt }
        "session-auto" { Invoke-Task -Name "session-auto" -DirectCommand $sessionAutoCmd -DshPrompt $sessionAutoPrompt }
        "agent-ttl" { Invoke-Task -Name "agent-ttl" -DirectCommand $agentTtlCmd -DshPrompt $agentTtlPrompt }
        "slo"      { Invoke-Task -Name "slo"      -DirectCommand $sloCmd      -DshPrompt $sloPrompt }  # 2026-08-18 SRE
        "db-health" { Invoke-Task -Name "db-health" -DirectCommand $dbHealthCmd -DshPrompt $dbHealthPrompt }
        "canary" { Invoke-Task -Name "canary" -DirectCommand $canaryCmd -DshPrompt $canaryPrompt }  # 2026-09-09 优化执行 P1（金丝雀端到端检索闭环）
        "active-health" { Invoke-Task -Name "active-health" -DirectCommand $activeHealthCmd -DshPrompt $activeHealthPrompt }
        # 658.73 去重(保留 2026-09-11 DryRun 感知分支): 原 backup 分支
        # 658.73 去重(保留 2026-09-11 DryRun 感知分支): 原 memory-ops 分支
        # 658.73 去重(保留 2026-09-11 DryRun 感知分支): 原 consolidate-temporal 分支
        "compress"  { Invoke-Task -Name "compress"   -DirectCommand $compressCmd  -DshPrompt $compressPrompt }  # 2026-08-27 巡检补全
        "evolve-auto" { Invoke-Task -Name "evolve-auto" -DirectCommand $evolveAutoCmd -DshPrompt $evolveAutoPrompt }
  "evolve-loop" { Invoke-Task -Name "evolve-loop" -DirectCommand $evolveLoopCmd -DshPrompt $evolveLoopPrompt }  # 2026-09-09 闭环修复
  "brain-event" { Invoke-Task -Name "brain-event" -DirectCommand $brainEventCmd -DshPrompt $brainEventPrompt }  # 2026-09-09 大脑化
  "brain-consumers" { Invoke-Task -Name "brain-consumers" -DirectCommand $brainConsumersCmd -DshPrompt $brainConsumersPrompt }
  "valence-backfill" { Invoke-Task -Name "valence-backfill" -DirectCommand $valenceBackfillCmd -DshPrompt $valenceBackfillPrompt }
  "confidence-bp" { Invoke-Task -Name "confidence-bp" -DirectCommand $confidenceBpCmd -DshPrompt $confidenceBpPrompt }
  "procedure-extract" { Invoke-Task -Name "procedure-extract" -DirectCommand $procedureExtractCmd -DshPrompt $procedureExtractPrompt }
  "brain-regions" { Invoke-Task -Name "brain-regions" -DirectCommand $brainRegionsCmd -DshPrompt $brainRegionsPrompt }  # 2026-09-09 大脑化  # 2026-08-27 巡检补全
        # 658.73 去重(保留 2026-09-11 DryRun 感知分支): 原 evolve-env 分支
        "audit-ps1" { Invoke-Task -Name "audit-ps1" -DirectCommand $auditPs1Cmd -DshPrompt $auditPs1Prompt }  # 2026-08-27 ps1 自检
        "forgetting" { Invoke-Task -Name "forgetting" -DirectCommand $forgettingCmd -DshPrompt $forgettingPrompt }  # 2026-08-27 遗忘决策
    "dream-replay" { Invoke-Task -Name "dream-replay" -DirectCommand $dreamReplayCmd -DshPrompt $dreamReplayPrompt }
    "curiosity" { Invoke-Task -Name "curiosity" -DirectCommand $curiosityCmd -DshPrompt $curiosityPrompt }
    "self-assess" { Invoke-Task -Name "self-assess" -DirectCommand $selfAssessCmd -DshPrompt $selfAssessPrompt }
    "predictive-loop" { Invoke-Task -Name "predictive-loop" -DirectCommand $predictiveLoopCmd -DshPrompt $predictiveLoopPrompt }
    "sensory-integration" { Invoke-Task -Name "sensory-integration" -DirectCommand $sensoryIntegrationCmd -DshPrompt $sensoryIntegrationPrompt }
    "emotional-consolidation" { Invoke-Task -Name "emotional-consolidation" -DirectCommand $emotionalConsolidationCmd -DshPrompt $emotionalConsolidationPrompt }
    "narrative" { Invoke-Task -Name "narrative" -DirectCommand $narrativeCmd -DshPrompt $narrativePrompt }
    "self-axioms" { Invoke-Task -Name "self-axioms" -DirectCommand $selfAxiomsCmd -DshPrompt $selfAxiomsPrompt }
    "memory-manager" { Invoke-Task -Name "memory-manager" -DirectCommand $memoryManagerCmd -DshPrompt $memoryManagerPrompt }
    "proactive" { Invoke-Task -Name "proactive" -DirectCommand $proactiveCmd -DshPrompt $proactivePrompt }
        "produce"   { Invoke-Task -Name "produce"   -DirectCommand $produceCmd   -DshPrompt $producePrompt }  # 2026-08-27 知识生产+合规
        "federation-sync" { Invoke-Task -Name "federation-sync" -DirectCommand $federationSyncCmd -DshPrompt $federationSyncPrompt }  # 2026-08-27 联邦同步
        "tune"      { Invoke-Task -Name "tune"      -DirectCommand $tuneCmd      -DshPrompt $tunePrompt }  # 2026-08-27 自动调参
        "fulltest"  { Invoke-Task -Name "fulltest"   -DirectCommand $fulltestCmd  -DshPrompt $fulltestPrompt }  # 2026-08-28 全量门禁
        "pg-sync"  { Invoke-Task -Name "pg-sync"  -DirectCommand $pgSyncCmd  -DshPrompt $pgSyncPrompt }  # 2026-08-29 PG 镜像
        "pg-backfill" { Invoke-Task -Name "pg-backfill" -DirectCommand $pgBackfillCmd -DshPrompt $pgBackfillPrompt }  # 2026-09-01 PG→SQLite 反向同步
        "reconcile" { Invoke-Task -Name "reconcile" -DirectCommand $reconcileCmd -DshPrompt $reconcilePrompt }  # 2026-09-01 双库对账（只读）
        "quality-gate" { Invoke-Task -Name "quality-gate" -DirectCommand $qualityGateCmd -DshPrompt $qualityGatePrompt }  # 2026-09-01 检索质量门禁
        "snapshot" { Invoke-Task -Name "snapshot" -DirectCommand $snapshotCmd -DshPrompt $snapshotPrompt }  # 2026-09-01 AGENTS.md 快照刷新
        "brain-report" { Invoke-Task -Name "brain-report" -DirectCommand $brainReportCmd -DshPrompt $brainReportPrompt }  # 2026-09-01 大脑体检周报
        "market-list" { Invoke-Task -Name "market-list" -DirectCommand $marketListCmd -DshPrompt $marketListPrompt }  # 2026-09-01 市场供给自动化
        "plugin-smoke" { Invoke-Task -Name "plugin-smoke" -DirectCommand $pluginSmokeCmd -DshPrompt $pluginSmokePrompt }  # 2026-09-01 DSH 插件冒烟
        "answer-eval" { Invoke-Task -Name "answer-eval" -DirectCommand $answerEvalCmd -DshPrompt $answerEvalPrompt }  # 2026-09-01 AnswerAcc 生成侧评测
        "evolve"  { Invoke-Task -Name "evolve"   -DirectCommand $evolveCmd  -DshPrompt $evolvePrompt }  # 2026-08-29 每日自改
        "observe" { Invoke-Task -Name "observe" -DirectCommand $observeCmd -DshPrompt $observePrompt }  # 2026-09 Ollama 解耦观察期检查
        "value-recalib" { Invoke-Task -Name "value-recalib" -DirectCommand $valueRecalibCmd -DshPrompt $valueRecalibPrompt }  # 2026-09 价值驱动编码补标
        "replay" { Invoke-Task -Name "replay" -DirectCommand $replayCmd -DshPrompt $replayPrompt }  # 2026-09 海马体重放巩固
        "extract-skills" { Invoke-Task -Name "extract-skills" -DirectCommand $skillsCmd -DshPrompt $skillsPrompt }  # 2026-09 程序性记忆技能库
        "perception-bridge" { Invoke-Task -Name "perception-bridge" -DirectCommand $perceptionCmd -DshPrompt $perceptionPrompt }  # 2026-09 感知桥
        "cognitive-eval" { Invoke-Task -Name "cognitive-eval" -DirectCommand $cognitiveEvalCmd -DshPrompt $cognitiveEvalPrompt }  # 2026-09 认知能力评测
        "event-extract" { Invoke-Task -Name "event-extract" -DirectCommand $eventExtractCmd -DshPrompt $eventExtractPrompt }  # 2026-09 事件图谱提取
        "reversible-compress" { Invoke-Task -Name "reversible-compress" -DirectCommand $reversibleCompressCmd -DshPrompt $reversibleCompressPrompt }  # 2026-09 可逆压缩
        "memory-purify" { Invoke-Task -Name "memory-purify" -DirectCommand $purifyCmd -DshPrompt $purifyPrompt }  # 2026-09 主动遗忘净化

    "conflict-worker" { Invoke-Task -Name "conflict-worker" -DirectCommand $conflictWorkerCmd -DshPrompt $conflictWorkerPrompt }  # 2026-09-03 F1 冲突巩固

    "opsbot-report" { Invoke-Task -Name "opsbot-report" -DirectCommand $opsbotReportCmd -DshPrompt $opsbotReportPrompt }  # 2026-09-04 周报

    "session-distill" { Invoke-Task -Name "session-distill" -DirectCommand $sessionDistillCmd -DshPrompt $sessionDistillPrompt }  # 2026-09-04 会话提炼

    "trinity-hud" { Invoke-Task -Name "trinity-hud" -DirectCommand $hudCmd -DshPrompt $hudPrompt }  # 2026-09-04 HUD
    "reader-agent" { Invoke-Task -Name "reader-agent" -DirectCommand $readerCmd -DshPrompt $readerPrompt }  # 2026-09-04 观察者

    "reader-agent-ops" { Invoke-Task -Name "reader-agent-ops" -DirectCommand $readerOpsCmd -DshPrompt $readerOpsPrompt }  # 2026-09-04 第二视角

    "perception-capture" { Invoke-Task -Name "perception-capture" -DirectCommand $perceptionCaptureCmd -DshPrompt $perceptionCapturePrompt }  # 2026-09-04 日采样
    "perception-screen-ingest" { Invoke-Task -Name "perception-screen-ingest" -DirectCommand $perceptionScreenIngestCmd -DshPrompt $perceptionScreenIngestPrompt }  # 2026-09-06 数据面(EXECUTION 589)
    "reflect-rewrite" { Invoke-Task -Name "reflect-rewrite" -DirectCommand $reflectRewriteCmd -DshPrompt $reflectRewritePrompt }  # 2026-09-06 重写式反思(EXECUTION 592)
    "fok-mark-test" { Invoke-Task -Name "fok-mark-test" -DirectCommand $fokMarkTestCmd -DshPrompt $fokMarkTestPrompt }  # §946
    "silent-skip" { Invoke-Task -Name "silent-skip" -DirectCommand $silentSkipCmd }  # §1104（只报告）
    "doc-regen-guard" { Invoke-Task -Name "doc-regen-guard" -DirectCommand $docRegenGuardCmd }  # §1128（只报告）
    "criterion-hygiene" { Invoke-Task -Name "criterion-hygiene" -DirectCommand $criterionHygieneCmd }  # §1149（只报告）
    "xref-check" { Invoke-Task -Name "xref-check" -DirectCommand $xrefCheckCmd }
    "resource-window" {
        # D6 选项 B（2026-09-22 执行）：直接跑以拿到 rc；BLOCKED(3) ⇒ 置位 + 大声留痕（不许静默略过）
        & $Py -c "import runpy; runpy.run_path(r'$TrinityRoot\scripts\resource_window_check.py', run_name='__main__')"
        if ($LASTEXITCODE -eq 3) { $windowBlocked = $true; Write-Log ("resource-window BLOCKED (rc=3) => D6-B: 本轮跳过 " + ($windowSkipTasks -join '/') + "（随后逐条留痕）") "WARN" }
        else { Write-Log ("resource-window rc=" + $LASTEXITCODE + " => 允许重活") }
    }
    "leg-record" { Invoke-Task -Name "leg-record" -DirectCommand $legRecordCmd }
    "fok-counts-fill" { Invoke-Task -Name "fok-counts-fill" -DirectCommand $fokCountsFillCmd -DshPrompt $fokCountsFillPrompt }  # §933
    "fok-counts-fill-light" { Invoke-Task -Name "fok-counts-fill-light" -DirectCommand $fokCountsFillLightCmd -DshPrompt $fokCountsFillLightPrompt }  # D9-C（2026-09-23）
"memory-correlation" { Invoke-Task -Name "memory-correlation" -DirectCommand "import runpy,sys; sys.argv=['x','--hours','6','--record']; runpy.run_path(r'$TrinityRoot/scripts/memory_stall_correlation.py', run_name='__main__')" }  # 996
    "blocks-heartbeat" { Invoke-Task -Name "blocks-heartbeat" -DirectCommand $blocksHeartbeatCmd -DshPrompt $blocksHeartbeatPrompt }  # 2026-09-06 记忆块心跳(EXECUTION 593)
    "perception-recall" { Invoke-Task -Name "perception-recall" -DirectCommand $perceptionRecallCmd -DshPrompt $perceptionRecallPrompt }  # 2026-09-06 感知回查(EXECUTION 594)

    "retro-boost" { Invoke-Task -Name "retro-boost" -DirectCommand $retroBoostCmd -DshPrompt $retroBoostPrompt }  # 2026-09-03 P9 回溯增强
    "summary-layer" { Invoke-Task -Name "summary-layer" -DirectCommand $summaryLayerCmd -DshPrompt $summaryLayerPrompt }  # 2026-09-03 P3 摘要层

    "dcpm-consolidate" { Invoke-Task -Name "dcpm-consolidate" -DirectCommand $dcpmConsolidateCmd -DshPrompt $dcpmConsolidatePrompt }
"sublimate" { Invoke-Task -Name "sublimate" -DirectCommand $subCmd -PromptText $subPrompt }
    "replay-consolidate" { Invoke-Task -Name "replay-consolidate" -DirectCommand $replayConsolidateCmd -DshPrompt $replayConsolidatePrompt }
    "recurrence-consolidate" { Invoke-Task -Name "recurrence-consolidate" -DirectCommand $recurrenceCmd -DshPrompt $recurrencePrompt }  # 658.44 RecMem 式复发整合
    "observation-build" { Invoke-Task -Name "observation-build" -DirectCommand $observationCmd -DshPrompt $observationPrompt }  # 658.48 证据化观察层
    "brain-md-export" { Invoke-Task -Name "brain-md-export" -DirectCommand $brainMdCmd -DshPrompt $brainMdPrompt }  # 658.48 markdown 大脑导出
    "brain-heartbeat" { Invoke-Task -Name "brain-heartbeat" -DirectCommand $brainHeartbeatCmd -DshPrompt $brainHeartbeatPrompt }  # 658.51 脑模块心跳
    "value-gate" { Invoke-Task -Name "value-gate" -DirectCommand $valueGateCmd -DshPrompt $valueGatePrompt }  # 658.51 写入价值闸门
    "neuromodulate" { Invoke-Task -Name "neuromodulate" -DirectCommand $neuroCmd -DshPrompt $neuroPrompt }  # 658.53 四通道神经调制
    "copies-sweep" { Invoke-Task -Name "copies-sweep" -DirectCommand $copiesCmd -DshPrompt $copiesPrompt }  # 658.53 三副本+层化
    "priority-map" { Invoke-Task -Name "priority-map" -DirectCommand $priorityCmd -DshPrompt $priorityPrompt }  # 658.54 四维优先图
    "metacog-monitor" { Invoke-Task -Name "metacog-monitor" -DirectCommand $metacogCmd -DshPrompt $metacogPrompt }  # 658.54 偏差监测
    "session-candidates" { Invoke-Task -Name "session-candidates" -DirectCommand $sessionCandidatesCmd -DshPrompt $sessionCandidatesPrompt }  # 662 会话→候选记忆队列（只产候选）
    "drift-check" { Invoke-Task -Name "drift-check" -DirectCommand $driftCheckCmd -DshPrompt $driftCheckPrompt }
    "audit-reconcile" { Invoke-Task -Name "audit-reconcile" -DirectCommand $auditReconcileCmd -DshPrompt $auditReconcilePrompt }  # 2026-09-04 EXECUTION 567
    "contradiction-resolve" { Invoke-Task -Name "contradiction-resolve" -DirectCommand $contradictionResolveCmd -DshPrompt $contradictionResolvePrompt }  # 2026-09-04 EXECUTION 567
    "flag-monitor" { Invoke-Task -Name "flag-monitor" -DirectCommand $flagMonitorCmd -DshPrompt $flagMonitorPrompt }  # 2026-09-04 EXECUTION 567
    "ipi-check" { Invoke-Task -Name "ipi-check" -DirectCommand $ipiCheckCmd -DshPrompt $ipiCheckPrompt }  # 2026-09-04 EXECUTION 567
    "market-drill" { Invoke-Task -Name "market-drill" -DirectCommand $marketDrillCmd -DshPrompt $marketDrillPrompt }  # 2026-09-04 EXECUTION 567
    "blind-judge" { Invoke-Task -Name "blind-judge" -DirectCommand $blindJudgeCmd -DshPrompt $blindJudgePrompt }  # 2026-09-04 EXECUTION 574
    "brain-health" { Invoke-Task -Name "brain-health" -DirectCommand $brainHealthCmd -DshPrompt $brainHealthPrompt }
    "identity-refresh" { Invoke-Task -Name "identity-refresh" -DirectCommand $identityRefreshCmd -DshPrompt $identityRefreshPrompt }
    "loop-audit" { Invoke-Task -Name "loop-audit" -DirectCommand $loopAuditCmd -DshPrompt $loopAuditPrompt }
    "brainification-guard" { Invoke-Task -Name "brainification-guard" -DirectCommand $brainificationGuardCmd -DshPrompt $brainificationGuardPrompt }
    "capability-check" { Invoke-Task -Name "capability-check" -DirectCommand $capabilityCheckCmd -DshPrompt $capabilityCheckPrompt }
    "action-loop" { Invoke-Task -Name "action-loop" -DirectCommand $actionLoopCmd -DshPrompt $actionLoopPrompt }
    # 658.71 去重: 重复分支会被 switch 全部执行 -> forgetting 双跑。原分支已由首个分支承担。
        "web-search" { Invoke-Task -Name "web-search" -DirectCommand $webSearchCmd -DshPrompt $webSearchPrompt }
    "web-perception" { Invoke-Task -Name "web-perception" -DirectCommand $webPerceptionCmd -DshPrompt $webPerceptionPrompt }
    "cognition-check" { Invoke-Task -Name "cognition-check" -DirectCommand $cognitionCheckCmd -DshPrompt $cognitionCheckPrompt }
    "self-reflect" { Invoke-Task -Name "self-reflect" -DirectCommand $selfReflectCmd -DshPrompt $selfReflectPrompt }
    "situation" { Invoke-Task -Name "situation" -DirectCommand $situationCmd -DshPrompt $situationPrompt }  # 2026-09-02 情境流
    "opsbot-cycle" { Invoke-Task -Name "opsbot-cycle" -DirectCommand $opsbotCmd -DshPrompt $opsbotPrompt }  # 2026-09-02 第二 agent 自治
    "perception-continuous" { Invoke-Task -Name "perception-continuous" -DirectCommand $perceptionContinuousCmd -DshPrompt $perceptionContinuousPrompt }  # 2026-09-02 持续感知
    "module-classify" { Invoke-Task -Name "module-classify" -DirectCommand $moduleClassifyCmd -DshPrompt $moduleClassifyPrompt }  # 2026-09-02 模块分级刷新
    "eval-gate" { Invoke-Task -Name "eval-gate" -DirectCommand $evalGateCmd -DshPrompt $evalGatePrompt }  # 2026-09-02 评测回归门禁
    "self-upgrade" { Invoke-Task -Name "self-upgrade" -DirectCommand $selfUpgradeCmd -DshPrompt $selfUpgradePrompt }  # 2026-09-03 自我/意识证据账本+蓝图
    "opsbot-deep-action" { Invoke-Task -Name "opsbot-deep-action" -DirectCommand $deepActionCmd -DshPrompt $deepActionPrompt }  # 2026-09-03 深度行动试点
    "expiry-review" { Invoke-Task -Name "expiry-review" -DirectCommand $expiryReviewCmd -DshPrompt $expiryReviewPrompt }  # 2026-09-02 459.4 过期复核队列
    "smoke" { Invoke-Task -Name "smoke" -DirectCommand $smokeCmd -DshPrompt $smokePrompt }  # 2026-09-04 EXECUTION 556 秒级 canary
    "rewards" { Invoke-Task -Name "rewards" -DirectCommand $rewardsCmd -DshPrompt $rewardsPrompt }  # 2026-09-04 EXECUTION 556 journal→bandit
    "nightly" { Invoke-Task -Name "nightly" -DirectCommand $nightlyCmd -DshPrompt $nightlyPrompt }  # 2026-09-04 EXECUTION 556 分层评测 nightly
    "perception-scan" { Invoke-Task -Name "perception-scan" -DirectCommand $perceptionScanCmd -DshPrompt $perceptionScanPrompt }
    "integrity-monitor" { Invoke-Task -Name "integrity-monitor" -LeaseJob "integrity-monitor" -DirectCommand $integrityMonitorCmd -DshPrompt $integrityMonitorPrompt }
    "auditverify" { Invoke-Task -Name "auditverify" -DirectCommand $auditVerifyCmd -DshPrompt $auditVerifyPrompt }  # 2026-09-09 P0-1 全链审计常态化
    "coverage" { Invoke-Task -Name "coverage" -DirectCommand $coverageCmd -DshPrompt $coveragePrompt }  # 2026-09-09 P0-2 监控信心仪表
    "reverb" { Invoke-Task -Name "reverb" -DirectCommand $reverbCmd -DshPrompt $reverbPrompt }  # 2026-09-09 P1-1 回音室检测
    "rl-guard" { Invoke-Task -Name "rl-guard" -DirectCommand $rlGuardCmd -DshPrompt $rlGuardPrompt }  # 2026-09-09 P1-2 RL 源盲抽检
    "forget-bias" { Invoke-Task -Name "forget-bias" -DirectCommand $forgetBiasCmd -DshPrompt $forgetBiasPrompt }  # 2026-09-09 P1-3 遗忘偏见审计
    "memory-digest" { Invoke-Task -Name "memory-digest" -DirectCommand $memoryDigestCmd -DshPrompt $memoryDigestPrompt }  # 2026-09-09 P2-3 健康叙事
    "cognition-agent" { Invoke-Task -Name "cognition-agent" -DirectCommand $cognitionAgentCmd -DshPrompt $cognitionAgentPrompt }  # 2026-09 主动主体
        # 2026-09-11（审计修正）：本分支**绕过 Invoke-Task** 直接调 trinity-backup.ps1，
        # 而 Invoke-Task 的 DryRun 保护（约 :303-306）只作用于经它的任务 —— 导致实测
        # -Tasks all -DryRun 时 backup 仍**真实写盘**（~/.trinity/backups 与
        # C:\trinity-offsite-backups 各 1 份，821MB + 296MB）。DryRun 必须真的 dry。
        "backup"    { if ($DryRun) { Write-Log "[DRY-RUN] backup : 将执行 trinity-backup.ps1（DryRun 下已跳过，不写盘）" } else { Write-Log "backup: WAL 安全备份到 ~/.trinity/backups (保留 14 天)"; & "$PSScriptRoot\trinity-backup.ps1" 2>&1 | ForEach-Object { Write-Log $_ } } }
        "evolve-env" { Write-Log "evolve-env: 应用自进化采纳 env（evolve_env.json → 进程环境，白名单校验）"; & "$PSScriptRoot\apply_evolve_env.ps1" -Show 2>&1 | ForEach-Object { Write-Log $_ } }  # 2026-08-25 缺口A
        "consolidate-temporal" {
            # 2026-09-15（R41-P23）：**周级巩固由"仅周日"改为"按龄 ≥6 天"**（错过补跑范式）。
            # 原判据 `(Get-Date).DayOfWeek -eq "Sunday"` ⇒ 只有周日那次链才带 `--weekly`；
            # 而主机实测每天约 4.6 次硬断电 ⇒ **周日链若被截断/错过，该周就缺"周级巩固"**
            # （只剩每日 `--days 1`），且要再等一周。现用文件 mark 记录上次周级巩固时间，
            # **距上次 >= 6 天即到期** ⇒ 任何一次链运行都能补上，周频语义不变。
            # $805（2026-09-17 巡检）：**补审计所需的 task 标记**。
            # 本分支是本文件里少数**不走 Invoke-Task** 的分支之一，因而从不写
            # `===== task: X =====`；而 maintenance_chain_audit 判定"实际执行"
            # **只看这个标记** ⇒ 一旦它进了日链，每天都会被记成"声明了却没执行"的缺口，
            # chain-reconcile 环恒红、`连续 7 天 gaps=0` 结构性不可达。
            # 实测证据：本轮两次手工 `-Tasks consolidate-temporal` 直接产出了 2 个缺口
            # （audit: declared=1 executed=0 缺失: consolidate-temporal），而分支本身跑得好好的。
            Write-Log "===== task: consolidate-temporal ====="
            $consArgs = @("--days", "1")
            $wkMark = Join-Path $LogDir "consolidate-temporal-weekly.mark"
            $weeklyDue = $true
            try {
                if (Test-Path $wkMark) {
                    $wkAgeD = ((Get-Date) - (Get-Item $wkMark).LastWriteTime).TotalDays
                    $weeklyDue = $wkAgeD -ge 6
                    Write-Log ("consolidate-temporal: weekly mark age={0:N1}d due={1}" -f $wkAgeD, $weeklyDue)
                }
            } catch { $weeklyDue = $true }
            if ($weeklyDue) {
                $consArgs = @("--days", "7", "--weekly")
            }
            Write-Log "consolidate-temporal: 时间层级巩固（TiMem 式；daily 每日，周级按龄 >=6 天补跑）"
            & "$Py" "$TrinityRoot\scripts\consolidate_temporal.py" @consArgs 2>&1 | ForEach-Object { Write-Log $_ }
            # 2026-09-26（事故）：本分支不走 Invoke-Task ⇒ 原实现**从不检查退出码**，
            # 实测脚本 KeyError 崩了 3 天（09-24..09-26），链日志里既没有 FAILED
            # 也没有告警，唯一症状是 blood_flow L1 的"consolidate_state 陈旧且未解释"。
            # 非零一律进 $Global:FAILED ⇒ 尾段汇总 + Send-Alert。
            if ($LASTEXITCODE -ne 0) {
                $Global:FAILED += "consolidate-temporal"
                Write-Log "consolidate-temporal : FAILED (exit $LASTEXITCODE)" "WARN"
            } else {
                Write-Log "consolidate-temporal : OK"
            }
            if ($weeklyDue) {
                try { Set-Content -Path $wkMark -Value (Get-Date -Format o) -ErrorAction Stop } catch { }
            }
            Write-Log "===== end: consolidate-temporal ====="
        }  # 2026-08-25 TiMem 式
        "memory-ops" { Write-Log "memory-ops: Mem0 式记忆操作（LLM 决策 ADD/UPDATE/NOOP，控制写放大）"; $env:TRINITY_MEM_OPS = "on"; if ($DryRun) { & "$Py" "$TrinityRoot\scripts\memory_ops.py" --hours 24 --limit 20 --dry-run 2>&1 | ForEach-Object { Write-Log $_ } } else { & "$Py" "$TrinityRoot\scripts\memory_ops.py" --hours 24 --limit 20 2>&1 | ForEach-Object { Write-Log $_ } } }  # 2026-08-25 Mem0 式
        "prewarm"             { Invoke-Task -Name "prewarm" -DirectCommand $prewarmCmd -DshPrompt $prewarmPrompt }  # EXECUTION 625 (case 原错位入 heredoc, EXECUTION 626 修复归位)
        "reason-slow-alert"   { Invoke-Task -Name "reason-slow-alert" -DirectCommand $reasonSlowAlertCmd -DshPrompt $reasonSlowAlertPrompt }  # EXECUTION 625 (626 修复归位)
        "pg-pool-smoke"       { Invoke-Task -Name "pg-pool-smoke" -DirectCommand $pgPoolSmokeCmd -DshPrompt $pgPoolSmokePrompt }  # EXECUTION 625 (626 修复归位)
        "store-growth"        { Invoke-Task -Name "store-growth" -DirectCommand $storeGrowthCmd -DshPrompt $storeGrowthPrompt }
        "analytics"           { Invoke-Task -Name "analytics" -DirectCommand $analyticsCmd -DshPrompt $analyticsPrompt }  # EXECUTION 770 (2026-09-16) DuckDB 分析层
"disk-growth"        { Invoke-Task -Name "disk-growth" -DirectCommand $diskGrowthCmd -DshPrompt $diskGrowthPrompt }  # 2026-09-14 capacity  # EXECUTION 625 (626 修复归位)
        "archive-purge-audit" { Invoke-Task -Name "archive-purge-audit" -DirectCommand $archivePurgeAuditCmd -DshPrompt $archivePurgeAuditPrompt }  # EXECUTION 625 (626 修复归位)
        "decay-real-llm"      { Invoke-Task -Name "decay-real-llm" -DirectCommand $decayRealLlmCmd -DshPrompt $decayRealLlmPrompt }  # EXECUTION 625 (626 修复归位)
        "write-policy-eval"   { Invoke-Task -Name "write-policy-eval" -DirectCommand $writePolicyEvalCmd -DshPrompt $writePolicyEvalPrompt }  # EXECUTION 625 (626 修复归位)
        "cluster-stress"      { Invoke-Task -Name "cluster-stress" -DirectCommand $clusterStressCmd -DshPrompt $clusterStressPrompt }  # EXECUTION 625 (626 修复归位)
        "meta-strategy" { Invoke-Task -Name "meta-strategy" -DirectCommand $metaStrategyCmd -DshPrompt $metaStrategyPrompt }  # EXECUTION 626
        "meta-strategy-propose" { Invoke-Task -Name "meta-strategy-propose" -DirectCommand $metaStrategyProposeCmd -DshPrompt $metaStrategyProposePrompt }  # EXECUTION 629
        "pg-embed" { Invoke-Task -Name "pg-embed" -DirectCommand $pgEmbedCmd -DshPrompt $pgEmbedPrompt }  # EXECUTION 634
        "loop-health" { Invoke-Task -Name "loop-health" -DirectCommand $loopHealthCmd -DshPrompt $loopHealthPrompt }  # EXECUTION 646
    }
}

if ($Global:FAILED.Count -gt 0) {
    Write-Log "maintenance finished with FAILED tasks: $($Global:FAILED -join ',')" "WARN"
    Send-Alert "Trinity 维护失败: $($Global:FAILED -join ',')" "ERROR"
    exit 1
}
Write-Log "maintenance finished OK"
exit 0

