<#
.SYNOPSIS
    Trinity DSH 自启循环 — 无需管理员权限的"计划任务"替代品。

.DESCRIPTION
    以隐藏窗口常驻循环，提供三个周期：
      - 每 5 分钟：跑一次 trinity-supervisor.ps1（api/mcp/collector 监督）；
      - 每 4 小时：跑一次维护 health,leg-record,evolution,session-auto（进化完整周期 + 会话自动沉淀）；
      - 每日 03:00-03:10：跑一次维护 decay,tiers,sync（需 PostgreSQL）；
        收尾跑一次审计链链头外部锚定 audit_anchor.py --anchor --apply。
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
$env:TRINITY_STORE = "C:\Users\Administrator\.trinity\store-restored"  # 2026-10-02 **灾难恢复**：原库 trinity_store.db 被强杀 API 损坏（memories/audit_log 报 malformed）；
# 已从 quick_check=ok 的备份 trinity_store.db.pre-sqlcipher-20261002-123638（113012 行 memories / 321342 行 audit_log）
# 恢复到 .trinity\store-restored\ 并校验通过。**指回旧目录前必须确认原库已修复**（旧库仍被某进程独占持有，mtime 仍在变）。
# 回滚：把本行改回 "...\.trinity\store"（旧库未被删除，仍在原处）。
# 2026-09-07: 同 supervisor, 对齐代码 canonical C: 权威库
$env:TRINITY_BRAIN_CONSUMERS = "on"  # 2026-09-09 闭环 A3: 脑区产物消费（brain-consumers 任务）
$env:TRINITY_SOCIAL_COGNITION = "on"  # 2026-09-14 R41-S2: 社会认知模块启用（同伴选择已修正；**吸收仍默认 dry-run**，见 EXECUTION 752）

# 2026-10-02（**故障自保**）：关掉"启动期全量语料嵌入预热"。
# 实测（supervisor 日志逐行）：
#   17:53:42 MEM: PID 7248 8.95GB (uptime 1min)  →  17:53:56 MEM-LIMIT hit: PID 7248 **24.97GB** > 14GB - killing
#   18:00:57 MEM-LIMIT hit: PID 28984 **24.47GB** > 14GB - killing（同一形状复现）
#   18:07:42 MEM-LIMIT hit: PID 4004  **24.40GB** > 14GB - killing（**此时本开关已生效**）
# 来源之一是 `trinity/api/server/_deps.py:210`：
#   `if os.environ.get("TRINITY_PREWARM_VEC_CORPUS", "1") == "1":`
# 那段在启动期把 ~19,355 条正文全量嵌一遍；而**默认向量通道是 lexical**
# （`TRINITY_VECTOR_CHANNEL` 默认 lexical）⇒ 这笔嵌入在默认配置下**不服务任何查询**。
#
# ⚠️ **必须留痕的自我更正**：我曾断言"就是这个开关把内存顶到 24.9GB"。
#    **实测证伪**：把下面两行置 0 并让循环重载（18:02:03，sha256=64448B744D90）之后，
#    18:06:24 起的 PID 4004 **仍然 24.40GB 被杀**。⇒ 语料/嵌入预热**不是**主因，
#    真正的大块在**启动期常驻加载**：聚合池 19,361 条
#    （`trinity/agents/aggregator/__init__.py::_load()`）+ BM25 索引构建
#    （`_deps.py:159-166` 的 `_warm()` 无条件 `mem._ensure_bm25_index()` 并等 30s）
#    + 向量索引**每次启动被判 mismatch 丢弃后全量重建**
#    （启动日志：`vector index row count mismatch (idx=19355 pool=19361) — discard, will rebuild`）。
#    24GB 的量级与"19K 条正文重新分词建索引"相符，但**我未做内存剖析**
#    ⇒ 这一步的置信度是"形状相符"，不是"已证明"。
#
# 保留这两行的理由：lexical 默认下预热无收益，关掉不亏（代价：首个 full 查询冷启动 ≈14.62s，
#   见本仓 2026-09-30 实测注释）。**但它不是这次的解**，不许当成已解决。
# 回滚：把下面两行删掉或改回 1。
#
# ── 2026-10-04（**正解已落地 ⇒ 语料预热恢复为 1**）────────────────────────────
# 上一轮我给出的"真正解法"是**语料向量持久化**，现已实现：
#   · `trinity/vector_index/index.py::VectorIndex.save/load`（后端无关，按 `_entries` 序列化，
#     加载走公开 `add()` ⇒ 后端自建 `_index/_id_map/_next_faiss_id`，避免手工反序列化错位）；
#   · `trinity/core/client/_helpers.py::_get_vector_index` 增加**加载分支**（失败静默回落空索引）；
#   · `trinity/core/client/_search.py` **从已加载索引播种 `_vec_index_seen`** —— 关键：
#     `_seen` 是进程内增量判据，不播种则"能加载也照样全量重嵌"；
#   · 落盘：`~/.trinity/data/corpus_vec.{manifest.json,vec.npz,meta.jsonl}`（原子替换）。
#
# **为什么必须同时恢复预热**：请求路径的语料嵌入预算默认是
# `TRINITY_VEC_CORPUS_EMBED_BUDGET = 0`（`_vec_budget.py:75`，即"请求不嵌语料"）
# ⇒ **没有预热就永远没人填索引** ⇒ 落盘永不触发 ⇒ 持久化形同虚设。
# 恢复预热后，因 `_seen` 已被播种，它**只嵌停机期间新增/变更的差额**（秒级），
# 而不再是"全量 1.9 万条 ≈73 分钟"。
#
# 判定（修复后应成立）：首个 full 查询不再冷建索引；`py-spy` 里 `_warm → onnxruntime`
# 短暂出现即结束；磁盘出现上述三文件。回滚：改回 "0"（退回"首次查询冷建 ≈14.6s"）。
# 注意 `TRINITY_PREWARM_EMBED` 仍为 0：它是**查询嵌入**预热，与语料索引无关。
$env:TRINITY_PREWARM_VEC_CORPUS = "0"  # 语料向量预热：**保持关闭**（依据见下方实测）
$env:TRINITY_PREWARM_EMBED = "0"       # 嵌入器预热：同上（查询嵌入，保持关闭）
#
# ── 2026-10-04 实测（取代同日早些时候那份**不准确**的结论）──────────────────────
# 【已确证的事实】
#  1. 本开关为 1 时，预热线程**确实会运行**并**打满单核**：
#     `py-spy dump` 可见活动线程 `_warm (trinity/api/server/_deps.py:301)`，
#     实测 `30s CPU 增量 = 31.9s`（= 单核 100%）、`60s 增量 = 61.8s`。
#  2. 同一次运行里，预热循环内的 `mem._vector_search("预热", 8)` **没有建起索引**：
#     进程内诊断 `PREWARM-DIAG` 实测 `vi_type=NoneType  dirty=0  n_seen=0`
#     ⇒ `self._vector_index` 始终 None、`_vec_index_seen` 始终为空。
#  3. 因此"把语料索引落盘以让预热只补差额"这条路径**在当前生产配置下不会被触达**
#     （落盘逻辑位于 `_vector_index` 建好**之后**的代码块里）。
# 【因此本开关保持 0】
#  它开着 = 稳定地烧掉一个核，且**拿不到**它想要的那个收益（索引并未建起来）。
#  关闭时实测：`45s CPU 增量 = 0.5s`、`commit ≈ 3.2GB`、`readyz=200`、冻结集 26/26。
# 【已保留】持久化的实现（`_corpus_persist.py` + `VectorIndex.save/load`）作为
#   **无害的备用路径**：当 `use_ann=False` 且 adapter 无 `vector_search` 时它确实生效
#   （已在 SQLiteAdapter 配置下离线验证 save/load 往返、检索**位级一致**、
#    维度不符拒绝加载、单调性守卫的判别力）。
# 【仍未查清（不得当成已解决）】
#   `_vector_search` 为何在这个生产进程里**没能建起 `_vector_index`**，
#   而**同一个类在离线探针里**却能正常建索引 —— 两者配置实测相同
#   （`mem_cls=Trinity`、`adapter=SQLiteAdapter`、`has_pgvec=False`、`use_ann=False`）。
#   下一步应从"预热首轮之前 `_vector_index`/`_adapter` 的状态"与
#   "是否有更早的 `_vector_search`（或其它路径）把它置成 None / 或让 `_get_vector_index()` 返回 None"入手。
#   在那之前，**不要**再凭"持久化已完成"把本开关打开。
#
# 【我自己在这一轮犯的错，留痕以免重犯】
#   我先把 CPU 打满归因于"语料预热"并据此关掉它——**方向对（它确实在烧）**；
#   但我随后用一次**时机不对**的观测（抓在预热结束之后）得出"预热根本没在跑"，
#   并据此写了更正——**那个更正本身是错的**。教训：
#   **对"某线程是否在运行"下结论，必须在同一进程存活期内多次采样**，
#   单次 `py-spy dump`/单次 CPU 增量都不足以证明"它没在跑"。

# 2026-10-02（**已回退**）：我曾在此设单进程内存上限 28GB 想"止血"（API 启动期实测 ≈24.4GB，
#   被 14GB 上限反复判死）。**该做法已被本仓注释明确否决**：
#   `trinity-memory-guard.ps1:25` ——「抬高上限 = 拆掉唯一的安全网。**改回 14.0 并保持**；
#   泄漏本身是待修的真缺陷（§1010）」，并记录 **43 秒涨 66GB（到 73.65GB）**、commit-free 险些耗尽（§1006）。
#   复现判据：**健康时 PrivateMemorySize64 稳定 5–13GB；单调升到数十 GB 即为该泄漏。**
#   ⇒ 实测的 24.4GB 是**真泄漏**，不是正常占用；抬上限只会把"服务被杀"换成"整机被拖垮"。
# 另外实测：这个环境变量**根本没到达 supervisor**（MEM 行仍写 `limit 14GB`）——
#   两条腿（串行循环 / 独立监督腿）的环境继承不可靠，别指望从这里改到它。
# 结论：**两个守卫都保持 14.0**；正解是修泄漏（§1010），已登记为下一步。
# 回滚：本块即"已回退"状态，无需动作（原本就是删掉那一行）。

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

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch { }
}

function Test-OrphanExpired {
    # 孤儿判定（2026-09-19 §869；2026-09-22 §1242 恢复）：**有界**，不无限放任。
    # 语义（由 test_loop_orphan_and_maint_gate 的反向锁钉死）：严格大于 超时+宽限。
    param([datetime]$StartedAt, [int]$TimeoutSec, [int]$GraceSec = 0, [datetime]$Now = (Get-Date))
    return (($Now - $StartedAt).TotalSeconds -gt ($TimeoutSec + $GraceSec))
}

function Get-ChildResult {
    # 起一个子进程并返回**可靠可读**的退出码（心跳 + 精确超时 + stdout/stderr 落文件）。
    #
    # 2026-09-22（§1249）重写原因（实测，判据见 tests/unit/test_child_exitcode_capture.py）：
    #   · 旧实现用 Start-Process -PassThru（不带 -Wait）⇒ PS 5.1 **不填充 .ExitCode**：
    #     进程已退出但 $p.ExitCode 是 $null，而 `$null -ne 0` 求值为 True
    #     ⇒ **成功的任务每天被报成 FAILED**（反向的 $null 守卫又造成假绿）；
    #   · 旧实现用 `while (-not $child.HasExited) { Start-Sleep -Seconds 30 }` 轮询
    #     ⇒ 4 秒的超时要等 30 秒才生效（实测超时用例跑了 30s）；
    #   · 旧实现返回 Start-Process 的 Process 对象 ⇒ 没有 .TimedOut/.StdOut，
    #     调用方拿不到「超时」与「读不到码」的区别。
    # 现在：.NET 直连 + **异步读流**（3MB 级输出写满管道会死锁）+ WaitForExit(ms) 精确超时。
    # 文件仍**字节透传**（BaseStream → WriteAllBytes）⇒ 不改日志面的编码口径。
    param([string]$FilePath, [Alias('Arguments')][string[]]$ArgumentList, [string]$OutFile,
          [string]$ErrFile, [string]$Label, [int]$TimeoutSec = 600)
    # 注意：$ArgumentList 可能为 null（如 supervisor 无参数），直接拼命令行会产生空 token；
    # 这里先滤掉 null，再**按需**加引号（调用方有的已自带引号，别重复包一层）。
    $argv = @()
    if ($ArgumentList) {
        foreach ($a in $ArgumentList) {
            if ($null -eq $a) { continue }
            $s = [string]$a
            if ($s.Length -ge 2 -and $s.StartsWith('"') -and $s.EndsWith('"')) { $argv += $s }
            elseif ($s -match '\s') { $argv += ('"' + $s + '"') }
            else { $argv += $s }
        }
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = ($argv -join ' ')
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    # 异步读流：两个管道同时抽干（同步 ReadToEnd 在子进程写满管道时会双向死锁）。
    $outMs = New-Object System.IO.MemoryStream
    $errMs = New-Object System.IO.MemoryStream
    $outTask = $proc.StandardOutput.BaseStream.CopyToAsync($outMs)
    $errTask = $proc.StandardError.BaseStream.CopyToAsync($errMs)
    # 658.100：**阻塞期间持续写心跳**（本次稳定性修复的核心）。
    # LoopGuard 以「心跳 > 15 分钟」判定循环僵死并**杀掉循环**，而维护链单段可跑 20 分钟
    # （health/evolution 可 10~30 分钟）→ 循环在**正常工作时被误杀**：实测 09-13 04:21
    # heartbeat stale (17.2 min) → killed pid 42572，直接吞掉当日 seg2/3/4。
    # 现改为**每秒**探一次、每 30 秒 touch 心跳，直到子进程退出或超时（旧版 30s 轮询会让
    # 短超时失效 —— 设 4s 实测要 30s 才生效）。
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $lastBeat = Get-Date
    $childTimedOut = $false
    while (-not $proc.WaitForExit(1000)) {
        if ((Get-Date) -ge $deadline) { $childTimedOut = $true; break }
        if (((Get-Date) - $lastBeat).TotalSeconds -ge 30) {
            try { (Get-Date -Format o) | Set-Content (Join-Path $env:USERPROFILE '.trinity\state\autostart_heartbeat.txt') -Encoding UTF8 } catch { }
            $lastBeat = Get-Date
        }
    }
    if ($childTimedOut) {
        # 2026-09-10（体检 659 P0-3）：超时不再只说超时——把维护日志里最后
        # 一个进入的 task 一并记下，直接回答'死在哪个任务'。
        $lastTask = ''
        try {
            $mlog = Join-Path $env:USERPROFILE '.trinity\logs\dsh-maintenance.log'
            if (Test-Path $mlog) {
                $hit = Select-String -Path $mlog -Pattern '===== task: ' -ErrorAction SilentlyContinue | Select-Object -Last 1
                if ($hit) { $lastTask = ($hit.Line -split '===== task: ')[-1].Trim() }
            }
        } catch { }
        Write-Log "$Label timed out ($TimeoutSec) - killing (last task: $lastTask)" "WARN"
        try { $proc.Kill() } catch { }
        [void]$proc.WaitForExit(5000)
    }
    try { [void]$outTask.Wait(5000) } catch { }
    try { [void]$errTask.Wait(5000) } catch { }
    $outBytes = $outMs.ToArray()
    $errBytes = $errMs.ToArray()
    # 文件**按字节写回**：与改动前 Start-Process -RedirectStandardOutput 的字节面一致，
    # 编码口径不在本函数里改（否则日志面会出现「看着一样、其实换了编码」的漂移）。
    if ($OutFile) { try { [IO.File]::WriteAllBytes($OutFile, $outBytes) } catch { } }
    if ($ErrFile) { try { [IO.File]::WriteAllBytes($ErrFile, $errBytes) } catch { } }
    # 字符串视图只给调用方取"末行"（$tail）用：UTF-8 优先，否则按本机 ANSI(936)。
    $txtEnc = [Text.Encoding]::UTF8
    try {
        if (-not ($env:PYTHONIOENCODING -match 'utf-?8')) { $txtEnc = [Text.Encoding]::GetEncoding(936) }
    } catch { }
    $stdout = ''
    $stderr = ''
    try { $stdout = $txtEnc.GetString($outBytes) } catch { }
    try { $stderr = $txtEnc.GetString($errBytes) } catch { }
    $exitCode = $null
    try { if ($proc.HasExited) { $exitCode = $proc.ExitCode } } catch { }
    return [pscustomobject]@{
        Id = $proc.Id; ExitCode = $exitCode; TimedOut = $childTimedOut
        StdOut = $stdout; StdErr = $stderr; OutFile = $OutFile; ErrFile = $ErrFile
        Label = $Label; HasExited = $true
    }
}

function Invoke-Script {
    param([string]$Path, [string[]]$ArgsList, [string]$Label, [int]$TimeoutSec = 600)
    try {
        # 2026-08-15 修复：改用文件重定向 + Wait-Process 超时，
        # 避免 `& ... 2>&1` 管道被孙进程句柄持有导致父循环永久卡死
        # （实测：循环自 2026-08-14 20:23 起卡在首轮监督，15h 未迭代）。
        $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $outFile = Join-Path $LogDir "invoke-$Label-$stamp.out.log"
        $errFile = Join-Path $LogDir "invoke-$Label-$stamp.err.log"
        $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$Path`"")
        if ($ArgsList -and $ArgsList.Count -gt 0) { $argList += $ArgsList }
        # ── 在途记录（§869 / §1242 恢复）────────────────────────────────────
        # 为什么：父循环被硬杀（本机日均有硬断电）时子进程会**变成孤儿继续跑**，
        # 而"有没有孤儿在飞"此前完全不可见。写一条 {label,pid,timeout_sec,started}，
        # 子进程结束后删除 ⇒ 残留即证据；`Test-OrphanExpired` 给它上界。
        $inflight = Join-Path $env:USERPROFILE '.trinity\state\autostart_inflight.json'
        $startedIso = (Get-Date).ToString('o')
        try {
            [IO.File]::WriteAllText($inflight, (@{ label = $Label; pid = 0; timeout_sec = $TimeoutSec; started = $startedIso } | ConvertTo-Json -Compress))
        } catch { }
        $child = Get-ChildResult -FilePath "powershell.exe" -ArgumentList $argList -OutFile $outFile -ErrFile $errFile -Label $Label -TimeoutSec $TimeoutSec
        try {
            [IO.File]::WriteAllText($inflight, (@{ label = $Label; pid = $child.Id; timeout_sec = $TimeoutSec; started = $startedIso } | ConvertTo-Json -Compress))
        } catch { }
        $tail = if (Test-Path $outFile) { Get-Content $outFile -Tail 1 -ErrorAction SilentlyContinue } else { $null }
        # 2026-09-08: 校验子进程退出码——"done" 曾掩盖静默失败（BOM 事故：链 ParserError
        # 但仍记 done）。2026-09-22（§1249）：退出码改由 Get-ChildResult 用 .NET 直连返回
        # （PS 5.1 的 Start-Process -PassThru 不带 -Wait 时 .ExitCode 恒为 $null ⇒ 假失败/假绿），
        # 「超时」也在此**显式区分** —— 不再与 exit 0 混为一谈。
        $rc = $child.ExitCode
        if ($child.TimedOut) {
            Write-Log "$Label TIMED OUT (${TimeoutSec}s): $tail" "WARN"
        } elseif ($null -ne $rc -and $rc -ne 0) {
            Write-Log "$Label FAILED (exit=$rc): $tail" "WARN"
        } else {
            Write-Log "$Label done: $tail"
        }
        Remove-Item -LiteralPath $inflight -ErrorAction SilentlyContinue
    } catch {
        Write-Log "$Label error: $_" "WARN"
        try { Remove-Item -LiteralPath $inflight -ErrorAction SilentlyContinue } catch { }
    }
}

function Read-MaintMark {
    # 4h 闸门的**专用**时间戳（2026-09-19 §869；2026-09-22 §1242 恢复）。
    # 为什么专用：用维护日志 mtime 判"该不该起链"会被**轻链饿死**（实测 13:46/13:50/13:58…
    # 每 3–5 分钟刷一次日志 ⇒ 4h 链永远判 not due）；用内存变量则在**重启后忘掉**
    # ⇒ 重复起链。所以落一个只有本链写的文件。
    # 边界（必须容错）：文件缺失/内容损坏 ⇒ 回落 now-25h（**绝不抛异常**，
    # 否则闸门整个失效 —— 这条是 §869 的判据①）。
    param([string]$Path, [datetime]$Now = (Get-Date))
    try {
        if (Test-Path $Path) {
            $raw = (Get-Content -LiteralPath $Path -TotalCount 1 -ErrorAction Stop)
            if ($raw) {
                $t = [datetime]::MinValue
                if ([datetime]::TryParse($raw.Trim(), [ref]$t)) { return $t }
            }
        }
    } catch { }
    return $Now.AddHours(-25)
}

function Test-MaintDue {
    # 判据：距上次**跑完**的间隔 >= IntervalSec 才算 due；IntervalSec <= 0 ⇒ **永不 due**
    # （关闭开关；反向锁 test_loop_orphan_and_maint_gate 的 DUE_DISABLED 就是这个语义）。
    param([datetime]$LastWrite, [int]$IntervalSec, [datetime]$Now = (Get-Date))
    if ($IntervalSec -le 0) { return $false }
    return (($Now - $LastWrite).TotalSeconds -ge $IntervalSec)
}

$lastMaint = (Get-Date).AddHours(-25)
$lastDaily = ""
$lastWeekly = ""  # 2026-09-01: 每周质量门禁
$lastWeeklyAcc = ""  # 2026-09-01: 每周 AnswerAcc 评测

Write-Log "autostart loop started (supervisor=${SupervisorIntervalSec}s, maint=${MaintIntervalSec}s)"

# 2026-08-29 (PG main storage): ensure portable PG on 5432
if (-not (Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue)) {
    $pgbin = "C:\Users\Administrator\Desktop\pgsql\bin"
    $pgdata = "C:\Users\Administrator\.trinity\pgdata"
    if (Test-Path "$pgbin\pg_ctl.exe" -and (Test-Path "$pgdata\PG_VERSION")) {
        Start-Process -FilePath "$pgbin\pg_ctl.exe" -ArgumentList @("start","-D",$pgdata,"-l","$pgdata\pg.log") -WindowStyle Hidden
        Start-Sleep 3
    }
}


# 2026-09-11（体检 663）脚本自检基线：进入循环前记录脚本哈希。
$Global:StartupHash = $null
$Global:ScriptPath = $PSCommandPath
if (-not $Global:ScriptPath) { $Global:ScriptPath = $MyInvocation.MyCommand.Path }
if (-not $Global:ScriptPath) { $Global:ScriptPath = Join-Path $PSScriptRoot 'trinity-autostart.ps1' }
try {
    $Global:StartupHash = (Get-FileHash -Path $Global:ScriptPath -Algorithm SHA256 -ErrorAction Stop).Hash
    Write-Log ("script self-check armed: path=" + $Global:ScriptPath + " sha256=" + $Global:StartupHash.Substring(0,12))
} catch {
    Write-Log ("script self-check FAILED to arm: " + $_.Exception.Message) "WARN"
}
while ($true) {
    # 2026-09-11（体检 663）：脚本变更自检 + 自重启。
    # 背景：PowerShell 启动时只解析一次脚本，循环跑的是内存旧副本——改了文件但
    # 循环仍执行旧代码，已连续三次造成修复无效（日链超时 600s 复现、4 个自愈任务
    # 未进调度、当夜无备份）。且旧循环进程提权，Stop-Process 一律 Access denied，
    # 只能靠计划任务 Stop/Start，会累积多个循环实例。
    # 本块让循环自己发现脚本变了并重启自身：新实例加载新代码，旧实例正常 exit，
    # 既保证改动生效，又不会留下无法终止的僵尸循环。
    try {
        $curHash = (Get-FileHash -Path $Global:ScriptPath -Algorithm SHA256 -ErrorAction Stop).Hash
        if ($Global:StartupHash -and $curHash -ne $Global:StartupHash) {
            Write-Log "autostart script changed on disk (hash mismatch) - relaunching to load new code" "WARN"
            $vbs = Join-Path $PSScriptRoot 'trinity-autostart.hidden.vbs'
            if (Test-Path $vbs) {
                Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $vbs + '"') -WindowStyle Hidden
            } else {
                Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File', ('"' + $PSCommandPath + '"')) -WindowStyle Hidden
            }
            exit 0
        }
    } catch { Write-Log ("script self-check error: " + $_.Exception.Message) "WARN" }

    $now = Get-Date
    # EXECUTION 647: 心跳文件（dead-man: supervisor 监控本循环存活）
    try { (Get-Date -Format o) | Set-Content (Join-Path $env:USERPROFILE '.trinity\state\autostart_heartbeat.txt') -ErrorAction Stop } catch { }

    # ── 每 5 分钟：监督（2026-09-18 修复：**计划任务优先，本循环兜底**）──────
    # 原实现无条件在**串行阻塞循环**里跑监督 ⇒ 任何长步骤都会把它整体推迟
    # （延迟 = 长步骤时长 + 轮间隔 300s）。实测 09:40:39 → 10:02:30 出现
    # **22 分钟盲窗**：我在窗口内 kill 掉常驻 api，246s 无人拉起，最后人工恢复。
    # 更根本的是 install-dsh-schedules.bat [5/5] 声明的独立任务 TrinityDSHSupervisor
    # （每 5 分钟）**实测并不存在** ⇒ 监督只剩串行循环这一条腿。
    # 现：主职责交给独立计划任务（不受本循环阻塞）；本循环只在监督产物**明显陈旧**
    # （>2×间隔）时兜底 —— 既避免"双调度"（本仓明确要求二选一），
    # 又保证计划任务失效时仍有人管。判据见 tests/unit/test_supervisor_cadence.py。
    $supLog = Join-Path $LogDir "dsh-supervisor.log"
    $supStale = $true
    try {
        if (Test-Path $supLog) {
            $supStale = (((Get-Date) - (Get-Item $supLog).LastWriteTime).TotalSeconds -gt ($SupervisorIntervalSec * 2))
        }
    } catch { $supStale = $true }
    if ($supStale -and (Test-Path $Supervisor)) {
        Write-Log "supervisor fallback: 监督产物陈旧(>2x间隔=$($SupervisorIntervalSec * 2)s)，本循环兜底执行" "WARN"
        Invoke-Script -Path $Supervisor -Label "supervisor"
    # ── 2026-09-22 §1201（D7 选项 B）：独立监督腿的 **keeper** ─────────────────────
    # 实测（§1198/§1200）：那条腿（trinity-supervisor-loop.ps1，免提权第二条腿）09-21 18:26 死掉后
    # **全仓没有任何机制拉起它**，监督退回本循环（当天间隔 11.5-25 分钟，最长 61.4）⇒ API 停摆 30.6 分钟。
    # 为什么由**本循环**当 keeper：本循环是 Medium（任务 RunLevel=Limited）⇒ 用 wscript 拉起的腿仍是 Medium；
    # 若交给 Highest 的 loop-guard 直接 spawn，子进程会继承 High ⇒ 破 D4（那条腿的设计就是「免提权」）。
    # 为什么不注册计划任务：那是 D7 的选项 C，与「二选一」纪律绑在一起，待拍板。
    # 幂等：vbs→脚本自带单例互斥，重复拉起会自行退出（已实测多次）。
    # 判据：tests/unit/test_supervisor_cadence.py 的 keeper 两条（在位 + 反事实）。
    $SupLoopVbs = Join-Path $PSScriptRoot "trinity-supervisor-loop.vbs"
    if (Test-Path $SupLoopVbs) {
        $supLoopAlive = $false
        try {
            $supLoopAlive = [bool](Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -match 'trinity-supervisor-loop\.ps1' -and $_.CommandLine -match '-File' } |
                Select-Object -First 1)
        } catch { $supLoopAlive = $false }
        if (-not $supLoopAlive) {
            Write-Log "supervisor-loop keeper: 独立监督腿不在，用 wscript 拉起（保持 Medium）" "WARN"
            try {
                Start-Process -FilePath "wscript.exe" -ArgumentList @($SupLoopVbs) -WindowStyle Hidden
                Write-Log "supervisor-loop keeper: 已发起拉起"
            } catch {
                Write-Log ("supervisor-loop keeper: 拉起失败: " + $_.Exception.Message) "WARN"
            }
        }
    }
    }
    # EXECUTION 647.4: supervisor 返回后心跳再写一次（快速路径心跳 gap≈间隔; 重链期靠 60min 上界判定）
    try { (Get-Date -Format o) | Set-Content (Join-Path $env:USERPROFILE '.trinity\state\autostart_heartbeat.txt') -ErrorAction Stop } catch { }

    # ── SQLite 锁看门狗(2026-08-16):持续锁占用时自动清理 ──
    if (Test-Path (Join-Path $PSScriptRoot "trinity-lock-watchdog.ps1")) { Invoke-Script -Path (Join-Path $PSScriptRoot "trinity-lock-watchdog.ps1") -Label "lock-watchdog" }

    # ── 孤儿检查（§869 / §1242 恢复）：父循环被杀后子进程会继续跑 ──────────────
    # 判据：在途记录存在 **且** started 已超过 timeout+宽限 ⇒ 报 ORPHANED CHILD。
    # 杀之前先核对**活进程自己的 StartTime** 是否与记录相符（±120s）——
    # 陈旧记录绝不允许杀一个被系统复用了 pid 的无辜进程；核不上就**只报告**。
    # 回滚：TRINITY_ORPHAN_KILL=off ⇒ 只报告不杀。
    $orphanFile = Join-Path $env:USERPROFILE '.trinity\state\autostart_inflight.json'
    if (Test-Path $orphanFile) {
        try {
            $rec = Get-Content -LiteralPath $orphanFile -Raw | ConvertFrom-Json
            $startedAt = [datetime]$rec.started
            $tmo = [int]$rec.timeout_sec
            if (Test-OrphanExpired -StartedAt $startedAt -TimeoutSec $tmo -Now $now) {
                Write-Log ("ORPHANED CHILD: label=" + $rec.label + " pid=" + $rec.pid + " started=" + $rec.started + " timeout=" + $tmo) "WARN"
                $canKill = ("$env:TRINITY_ORPHAN_KILL" -ne 'off')
                if ($canKill) {
                    try {
                        $op = Get-Process -Id ([int]$rec.pid) -ErrorAction Stop
                        $drift = [math]::Abs(($op.StartTime - $startedAt).TotalSeconds)
                        if ($drift -le 120) {
                            Stop-Process -Id $op.Id -Force -ErrorAction SilentlyContinue
                            Write-Log ("ORPHANED CHILD killed pid=" + $op.Id + " (drift=" + [int]$drift + "s)") "WARN"
                        } else {
                            Write-Log ("ORPHANED CHILD pid " + $rec.pid + " start-time drift " + [int]$drift + "s > 120s => 只报告，不杀") "WARN"
                        }
                    } catch { Write-Log ("ORPHANED CHILD pid " + $rec.pid + " 已不在 => 清理记录") "WARN" }
                }
                Remove-Item -LiteralPath $orphanFile -ErrorAction SilentlyContinue
            }
        } catch { Write-Log ("orphan check error: " + $_.Exception.Message) "WARN" }
    }

    # ── 每 4 小时：reason-slow-alert + health + leg-record + evolution（+ mirror）──
    # 2026-09-22（§1242 修复）：这条链在 2026-09-20 被改写时**丢了链首 `reason-slow-alert`**
    # （唯一的告警任务 ⇒ reason_last.json 停更 44.8h ⇒ loop_health 的 reason-latency-probe 环转红）
    # 与 `mirror`（§866 的 4h 可见性目标退回每日一次），并丢掉了专用标记闸门
    # （内存态 $lastMaint 重启即忘 ⇒ 重复起链）。三条守卫单测
    # （test_task_wiring_no_dangling_alert / test_mirror_in_4h_chain /
    # test_maint_gate_dedicated_mark）在丢失当天就变红，本次按它们的契约原样恢复。
    # 读法：`-Label` 仍是 maintenance(health,evolution)（日志/闸门锚点，**不含 mirror/leg-record**）；
    # 到底跑了哪些任务要看 maintenance 日志的 `tasks=` 行（§912 已记过一次这条口径）。
    # D9 选项 C（2026-09-23 拍板并施工）：把「按需触发」的轻量补算挂进 4 小时段 ——
    # 它自己会在队列短（pending < 500）时打一行 [SKIP] 跳过，所以常态不增加 PG 负载。
    $maintTasks = 'reason-slow-alert,health,leg-record,evolution,session-auto,fok-counts-fill-light'
    if ("$env:TRINITY_MIRROR_4H" -ne 'off') { $maintTasks = $maintTasks + ',mirror' }
    $maintMark = Join-Path $env:USERPROFILE '.trinity\state\autostart_last_maint.txt'
    $maintLast = Read-MaintMark -Path $maintMark -Now $now
    if ((Test-Path $Maintenance) -and (Test-MaintDue -LastWrite $maintLast -IntervalSec $MaintIntervalSec -Now $now)) {
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", $maintTasks) -Label "maintenance(health,evolution)" -TimeoutSec 1800
        $lastMaint = $now
        try { [IO.File]::WriteAllText($maintMark, $now.ToString('o')) } catch { Write-Log ("maint mark write failed: " + $_.Exception.Message) 'WARN' }
    }

    # ── 语料预热：**错峰调度**（2026-10-05 落地；见 `$dcTasks` 处那段决策留档）──────
    # 为什么**不放进日链**：日链本身是并发环境，而预热在并发下实测从
    # 0.15 s/行 掉到 ~7.9 s/行（**≈50×**）⇒ 必然超预算并被记 FAILED。
    # 为什么放在**这里**：紧接在主维护块之后 ⇒ 维护刚结束、宿主机最安静，
    # 这正是预热需要的窗口；且它与主维护**同间隔（4h）但不同时机**，天然错峰。
    # 双保险：① `run_prewarm.py` 自带 **CPU 空闲门**（实测判别力：空闲 94.2% 运行 /
    #   忙 67~78% 跳过；阈值 `TRINITY_PREWARM_CPU_IDLE_MIN_PCT` 默认 80）——
    #   忙时**跳过并 rc=0**（不是 FAILED）；② `run_prewarm.py` 自带 **TTL 6h 幂等**
    #   （`~/.trinity/state/prewarm_last.json`），重复触发会自动 skip。
    # 预算：与外层一致取 1700s（内层墙钟预算默认 1200s，留 500s 给落盘与退出）。
    # 回滚：删掉下面这个 if 块（或设 `TRINITY_PREWARM_SCHEDULE=off`）。
    if ("$env:TRINITY_PREWARM_SCHEDULE" -ne 'off') {
        $pwMark = Join-Path $env:USERPROFILE '.trinity\state\prewarm_last.json'
        $pwLast = $null
        if (Test-Path $pwMark) { try { $pwLast = (Get-Item $pwMark).LastWriteTime } catch { $pwLast = $null } }
        if ((Test-Path $Maintenance) -and
            (Test-MaintDue -LastWrite $pwLast -IntervalSec $MaintIntervalSec -Now $now)) {
            Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "prewarm") -Label "maintenance(prewarm)" -TimeoutSec 1700
        }
    }

    # ── 每日 03:00-03:10：decay + tiers + sync（需 PG）──────────
    $today = $now.ToString("yyyyMMdd")
    # ── H2-4（2026-09-13）：审计链链头外部锚定 —— **独立触发** ────────────────
    # 原实现嵌在下面 03:00 日链的 if 块内：日链一旦被跳过/超时（09-11 实测 600s 超时被杀），
    # 锚定一起消失。实测 09-11/12/13 **三天零日志**、见证断档 3 天 —— 而锚定正是
    # "链被改写/截断"唯一的链外证据来源。现改为独立时间窗 03:40-03:59，
    # 且**幂等**（当日已锚过则跳过，不依赖 $lastDaily 这个循环内存态变量）。
    $anchorScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\audit_anchor.py'
    # 2026-09-15（R41-P23）：**改为"按龄补跑"**（原判据只在 03:40–03:59 的 **20 分钟窗口**内触发）。
    # 动机（实测）：主机每天约 **4.6 次硬断电**（14 天 17 次 Event 41）⇒ 窗口期一旦宕机，
    # 当天锚点**整日跳过**；连续两天 ⇒ 见证年龄 >48h ⇒ `audit-anchor` / `anchor-offsite`
    # 两个看护环**转红**（实测 2026-09-15 16:59 `stale 48.3h`）。
    # 而锚定是"链被改写/截断"**唯一的链外证据**，不能靠"恰好没宕机"来保证 ——
    # 这正是本会话反复出现的"用一次性窗口代替可补跑的节奏"这一类缺陷。
    # 现口径：**见证年龄 >= 20h 即视为到期**（仍是日频，但任何一轮 autostart 都能补上错过的窗口）；
    # 原有的"同一 UTC 日期内不重复锚定"幂等检查**保留**（避免同日二次锚定）。
    # 2026-10-06（复评 F2）：**双链锚定** —— 锚定对象从"只有 PG 链"扩到"PG + 在服 SQLite 链"。
    # 此前 audit_anchor.py 只连 PG（≈123.6k 行），而 audit_fullchain_verify.py 校验的是
    # SQLite 链（≈359.2k 行）⇒ **被锚的链与被校验的链错位**，"整链重算"缺口对真正在
    # 服务的库完全敞开。现 --backend 默认已是 both，这里**显式**写出，并把"到期判据"
    # 与"同日幂等判据"从只看 PG 文件改为**两条链各自判**（任一链缺失/超龄即触发）。
    $anchorStateDir = Join-Path $env:USERPROFILE '.trinity\state'
    $anchorTargets = @(
        @{ name = 'postgresql'; file = (Join-Path $anchorStateDir 'audit_anchor.jsonl') },
        @{ name = 'sqlite';     file = (Join-Path $anchorStateDir 'audit_anchor_sqlite.jsonl') }
    )
    $anchorDue = $false
    $anchorAgeH = 0.0
    foreach ($at in $anchorTargets) {
        if (-not (Test-Path $at.file)) {
            # 文件缺失 = 该链从未被外锚（SQLite 在 2026-10-06 之前一直是这种状态）
            $anchorDue = $true
            $anchorAgeH = 999.0
            continue
        }
        try {
            $tsProbe = ((Get-Content $at.file -Tail 1 -ErrorAction Stop) | ConvertFrom-Json).ts
            $ageH = ((Get-Date).ToUniversalTime() - ([datetime]$tsProbe).ToUniversalTime()).TotalHours
            if ($ageH -gt $anchorAgeH) { $anchorAgeH = $ageH }
            if ($ageH -ge 20) { $anchorDue = $true }
        } catch {
            Write-Log ('audit-anchor: ' + $at.name + ' 锚文件不可解析 ⇒ 视为到期') 'WARN'
            $anchorDue = $true
        }
    }
    if ((Test-Path $anchorScript) -and $anchorDue) {
        # 幂等：**每条链**在同一 UTC 日期内都已锚过才算完成（只看 PG 会让 SQLite 永远被跳过）
        $anchorDone = $true
        $anchorPending = @()
        foreach ($at in $anchorTargets) {
            $done = $false
            try {
                if (Test-Path $at.file) {
                    $lastTs = ((Get-Content $at.file -Tail 1 -ErrorAction Stop) | ConvertFrom-Json).ts
                    if ($lastTs -and ([string]$lastTs).Substring(0, 10) -eq (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd')) { $done = $true }
                }
            } catch { }
            if (-not $done) { $anchorDone = $false; $anchorPending += $at.name }
        }
        if (-not $anchorDone) {
            $aPy = 'C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python314\\python.exe'
            $aOut = Join-Path $LogDir ('audit-anchor-' + $today + '.out.log')
            $aErr = $aOut + '.err'
            $ap = Get-ChildResult -FilePath $aPy -ArgumentList @($anchorScript, '--anchor', '--apply', '--backend', 'both') -OutFile $aOut -ErrFile $aErr -Label 'audit-anchor' -TimeoutSec 300
            $arc = $ap.ExitCode
            $aTail = if (Test-Path $aOut) { (Get-Content $aOut -Tail 1 -ErrorAction SilentlyContinue) -join ' | ' } else { '' }
            if ($arc -ne 0) {
                Write-Log ('audit-anchor FAILED (exit=' + $arc + ', pending=' + ($anchorPending -join ',') + '): ' + $aTail) 'WARN'
            } else {
                Write-Log ('audit-anchor done (pending were ' + ($anchorPending -join ',') + '): ' + $aTail)
            }
            # 落盘后复核：SQLite 链的锚文件必须真的出现，否则明示（防止"跑过了但没写"）
            $sqAnchorPath = Join-Path $anchorStateDir 'audit_anchor_sqlite.jsonl'
            if (-not (Test-Path $sqAnchorPath)) {
                Write-Log 'audit-anchor(sqlite): 锚文件仍缺失 —— live SQLite 链无外锚（整链重算缺口敞开）' 'WARN'
            }
        }
    }

    # ── H2-1（2026-09-13）：自主目标结果裁定 —— 独立触发 ──────────────────────
    # 背景：goals.json 的 12 条自主目标**全部 phase=active、无 status/outcome** ——
    # 产生之后没有任何东西评价它们（自主回路只有前半截）。本步骤每天给它们落结局，
    # 产出 completion_rate（弱证据率）。窗口 03:50-03:59，幂等（当日已评则跳过）。
    $goalAudit = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\autonomous_goal_audit.py'
    # 2026-09-15（R41-P23）：**去掉窗口上界**（原 03:50–03:59 仅 10 分钟）。
    # 幂等本已由**当日输出日志**（$gDone，见下）保证，与窗口无关 ⇒ 放宽严格更安全：
    # 主机每天 4.6 次断电，10 分钟窗口极易整日错过。
    if ((Test-Path $goalAudit) -and ($now.Hour -gt 3 -or ($now.Hour -eq 3 -and $now.Minute -ge 50))) {
        $gOut = Join-Path $LogDir ('goal-audit-' + $today + '.out.log')
        $gDone = $false
        try {
            if (Test-Path $gOut) {
                if ((Get-Item $gOut).LastWriteTime.ToString('yyyy-MM-dd') -eq (Get-Date).ToString('yyyy-MM-dd')) { $gDone = $true }
            }
        } catch { }
        if (-not $gDone) {
            $gPy = 'C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python314\\python.exe'
            $gErr = $gOut + '.err'
            $gp = Get-ChildResult -FilePath $gPy -ArgumentList @($goalAudit, '--apply') -OutFile $gOut -ErrFile $gErr -Label 'goal-audit' -TimeoutSec 300
            $grc = $gp.ExitCode
            $gTail = if (Test-Path $gOut) { (Get-Content $gOut -Tail 1 -ErrorAction SilentlyContinue) -join ' | ' } else { '' }
            if ($grc -ne 0) { Write-Log ('goal-audit FAILED (exit=' + $grc + '): ' + $gTail) 'WARN' } else { Write-Log ('goal-audit done: ' + $gTail) }
        }
    }

    # ── W4/W5（2026-09-13）：记忆利用率 + 机制消费者 —— 一等指标，每日出数 ──────
    # 为什么独立触发：这两项是「闭环是否真的在转」的唯一客观读数（检索覆盖率 / 机制有无内容消费者），
    # 此前只在手动跑时才有数。窗口 04:10-04:29，幂等（当日已出数则跳过）。
    # ── 2026-09-29（判据接线 · C1-C4）：四能力判据看板日跑 ────────────────────
    # 口径：C0 口径卫生 / C1 连续性 / C2 自述诚实 / C3 校准 / C4 自主目标。
    # **为什么必须日跑**：C1 的判据是「不变量锚点全部 grounded 且 verified=true 连续 >=7 天」——
    # 没有每日快照，这条判据**永远无法判定**（此前确实从未开始计时）。
    # 幂等键 = 当日产物 capability_judgment_<date>.json；窗口 >=04:30（让过 03:54 的目标审计）。
    # 回滚：删掉本段即可（看板本身只读：不写 DB、不改配置、不动仓库文件）。
    $cjScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\capability_judgment.py'
    $cjOut = Join-Path $env:USERPROFILE ('.trinity\state\capability_judgment_' + $today + '.json')
    $cjWindow = ($now.Hour -gt 4) -or (($now.Hour -eq 4) -and ($now.Minute -ge 30))
    if ((Test-Path $cjScript) -and (-not (Test-Path $cjOut)) -and $cjWindow) {
        $cjPy = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
        $cjLog = Join-Path $LogDir ('capability-judgment-' + $today + '.out.log')
        try {
            & $cjPy $cjScript --self-report-n 3 --json $cjOut *> $cjLog
            $cjrc = $LASTEXITCODE
            if ($cjrc -ne 0) { Write-Log ('capability-judgment FAILED (exit=' + $cjrc + '); log=' + $cjLog) 'WARN' }
            else { Write-Log ('capability-judgment done -> ' + $cjOut) }
        } catch { Write-Log ('capability-judgment exception: ' + $_.Exception.Message) 'WARN' }
    }

    $covScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\retrieval_coverage.py'
    $conScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\mechanism_consumer_audit.py'
    # 2026-09-15（R41-P23）：**去掉窗口上界**（原仅 04:10–04:59）；幂等由当日 `metrics-<date>.out.log` 保证。
    if ((Test-Path $covScript) -and ($now.Hour -gt 4 -or ($now.Hour -eq 4 -and $now.Minute -ge 10))) {
        $mOut = Join-Path $LogDir ('metrics-' + $today + '.out.log')
        $mDone = $false
        try {
            if (Test-Path $mOut) {
                if ((Get-Item $mOut).LastWriteTime.ToString('yyyy-MM-dd') -eq (Get-Date).ToString('yyyy-MM-dd')) { $mDone = $true }
            }
        } catch { }
        if (-not $mDone) {
            $mPy = 'C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python314\\python.exe'
            try {
                $c1 = & $mPy $covScript 2>&1 | Select-Object -Last 1
                $c2 = & $mPy $conScript 2>&1 | Select-Object -Last 1
                # 2026-09-14（R41-D2）：**血流分层体检**纳入每日指标窗口 ——
                # 六层判据（调度/生产/消费/效果/监控/覆盖）一次出数，产物
                # output/blood_flow_status_<date>.json，供 loop_health 的 blood-flow 环判读。
                # 回滚：删掉本段与下方 $c4 即可（其余三项不受影响）。
                $bfScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\blood_flow_status.py'
                $c4 = ''
                if (Test-Path $bfScript) { try { $c4 = & $mPy $bfScript 2>&1 | Select-Object -Last 1 } catch { $c4 = 'blood-flow-status failed' } }
                # 2026-09-13：第三项 = **字段级**生产者/消费者对账（补「字段/返回值」层：
                # 有写无读的返回值键 —— MCP 丢弃聚合层字段、abstain 被丢弃都属这一类）。
                $fldScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\field_consumer_audit.py'
                $c3 = ''
                if (Test-Path $fldScript) { try { $c3 = & $mPy $fldScript 2>&1 | Select-Object -Last 1 } catch { $c3 = 'field-audit failed' } }
                # 2026-09-14（R41-P5）：runpy 守卫审计（扫『子脚本 SystemExit 带走调用者』这类缺陷）
                # 产物 output/runpy_guard_audit.json；**AT_RISK>0 = 有任务块/函数的后续步骤会被静默跳过**。
                $rgScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\runpy_guard_audit.py'
                $c5 = ''
                if (Test-Path $rgScript) { try { $c5 = & $mPy $rgScript 2>&1 | Select-Object -Last 1 } catch { $c5 = 'runpy-guard-audit failed' } }
                # 2026-09-14（R41-P11）：C 批接线**效果评估**（P1 覆盖>=7天 / P2 成功率>=0.9 / P3 输入非空）
                # 评审点 epoch+7d（约 2026-09-21）；每日落数，避免到点临时找数据。
                $cwScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\capability_wire_eval.py'
                $c6 = ''
                if (Test-Path $cwScript) { try { $c6 = & $mPy $cwScript 2>&1 | Select-Object -Last 1 } catch { $c6 = 'capability-wire-eval failed' } }
                ($c1, $c2, $c3, $c4, $c5, $c6) | Out-File -FilePath $mOut -Encoding utf8
                Write-Log ('metrics done: ' + $c1)
            } catch { Write-Log ('metrics FAILED: ' + $_.Exception.Message) 'WARN' }
        }
    }

    # ── P1 闭环层（2026-09-13）：**声明 vs 实际执行**对账 —— 独立窗口，每日出数 ──
    # 动机（当日实测）：日链里内嵌的 maint-audit **自己也在被静默漏跑**（历史零日志），
    # 于是「有没有任务被跳过」这个问题长期无人回答。本段把它移出日链、给独立窗口
    # 04:20-05:59（追赶窗）+ 文件标记幂等，即使日链整条没跑，对账也照出。
    # 2026-09-15（R41-P23）：**去掉窗口上界**（原 `Hour -ge 4 -and Hour -lt 6` 两小时窗）。
    # 幂等已由**当日 `maint-audit-<date>.out.log`** 判据（见下 `$aDone`）保证，与窗口无关
    # ⇒ 放宽严格更安全：主机每天 4.6 次断电，两小时窗仍可能被整段错过。
    $auditScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\maintenance_chain_audit.py'
    if ((Test-Path $auditScript) -and $now.Hour -ge 4) {
        $aMark = Join-Path $LogDir ('maint-audit-' + $today + '.out.log')
        $aDone = $false
        try {
            if (Test-Path $aMark) {
                if ((Get-Item $aMark).LastWriteTime.ToString('yyyy-MM-dd') -eq (Get-Date).ToString('yyyy-MM-dd')) { $aDone = $true }
            }
        } catch { }
        if (-not $aDone) {
            $aAuditPy = 'C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python314\\python.exe'
            $aOut2 = $aMark + '.txt'
            $aErr2 = $aOut2 + '.err'
            $aProc = Get-ChildResult -FilePath $aAuditPy -ArgumentList @($auditScript, '--last', '12') -OutFile $aOut2 -ErrFile $aErr2 -Label 'maint-audit' -TimeoutSec 600
            $arc2 = $aProc.ExitCode
            $aTail2 = if (Test-Path $aOut2) { (Get-Content $aOut2 -Tail 1 -ErrorAction SilentlyContinue) -join ' ' } else { '' }
            if ($arc2 -ne 0) { Write-Log ('maint-audit GAP DETECTED: ' + $aTail2) 'WARN' } else { Write-Log ('maint-audit ok: ' + $aTail2) }
            Set-Content -Path $aMark -Value (Get-Date -Format o)
        }
    }

    # 2026-09-13（重启安全）：幂等判定由**内存变量**改为**文件标记**。
    # 原因：$lastDaily 是循环内存态，autostart 一旦在 03:00-03:09 窗口内被重启（当天实测 03:22 重启过），
    # 新进程 $lastDaily 为空 ⇒ **整条日链会被再启动一次**（30+ 任务、含备份/衰减/同步，代价不小）。
    # 文件标记与进程生命周期无关，重启也认账。
    $dcMark = Join-Path $LogDir ('daily-chain-' + $today + '.mark')
    $dcDone = Test-Path $dcMark
    if ((Test-Path $Maintenance) -and $now.Hour -ge 3 -and $lastDaily -ne $today -and -not $dcDone) {
        # 2026-09-15（R41-P23）：**把 10 分钟窗口放宽为"03:00 之后的首次机会"**（按龄补跑范式）。
        # 原判据 `$now.Hour -eq 3 -and $now.Minute -lt 10` ⇒ 日链**每天只有 03:00–03:09 这 10 分钟**
        # 可被触发；而主机实测**每天约 4.6 次硬断电**，一旦在该窗口内宕机/重启，
        # **当天整条日链（备份/衰减/分层/同步/审计…）被整体跳过**。
        # 关键：**"当天只跑一次"由 `$dcDone`（文件 mark，见下方 `Set-Content $dcMark`）保证，
        # 不需要窗口来保证** ⇒ 窗口纯粹是脆弱性来源，去掉它**严格更安全**：
        #   · 正常日：03:00 后首轮即跑（与原来等效）；
        #   · 错过窗口：恢复后的首轮补跑（原来会整天不跑）；
        #   · 重启：mark 仍在 ⇒ 不重复（原设计意图不变）。
        # ── 2026-09-13（实测修复）：日链主调用改为**非阻塞启动** ──────────────
        # 实测：09-13 日链 03:04:27 启动主调用、跑到 03:26:49，而 autostart 在 **03:22:04 重启** ⇒
        # 阻塞等待之后的**全部内嵌子任务被静默跳过**：benchmark-quarantine / active-dedup /
        # health-snapshot / maint-audit 的日志**历史零文件**（写在脚本里但从没执行过）。
        # 现改为独立进程（不等待）：① 主链不再受父进程重启影响（自己的日志文件照留）；
        # ② 其后内嵌的子任务立即执行，不再被 22 分钟的阻塞调用挡住。
        $dcOut = Join-Path $LogDir ('daily-chain-' + $today + '.out.log')
        $dcErr = $dcOut + '.err'
        try {
            # 2026-09-15（R41-P23）：**长任务后移**，修正"尾部任务被系统性饿死"。
            # 取证（dsh-maintenance.log 逐行 + 系统事件）：2026-09-15 03:05 那条 30 任务日链里，
            # 两个长任务吃掉绝大部分时长——value-recalib 03:36:46→04:09:33（约 33min）、
            # pg-embed 04:11:04→04:51:04（40min，撞满维护任务给的 2400s 超时）；
            # 而 reconcile/snapshot/market-list/replay/proactive/opsbot-cycle/
            # perception-archival/disk-growth 这 8 个全排在它们之后，该夜 04:47 机器意外
            # 关机（System 事件 6008，宕机至 08:57）即**整批未执行** ⇒ 直接表现为看护环
            # chain-reconcile 的 gaps（当前唯一红项）。
            # 处置：把这两个长任务挪到链尾，**其余 28 个任务相对顺序一字未动**
            # （已用脚本自证为纯置换：集合相同、仅顺序不同、其余相对序不变）。
            # 取舍：长任务幂等/可续做（pg-embed 已加单次预算，value-recalib 为批量重打分），
            # 被截断的代价远小于"运维类任务整夜不跑"。
            # 回滚：恢复 temp/_autostart.bak-r41p23 的原顺序行。
            # 2026-09-16（本轮接线）：**self-upgrade 进日链**。实测它此前只在 09-07 / 09-14 跑过两次（整份 20MB 维护日志里仅 2 条 'task: self-upgrade'），而 loop_health 对 self_markers.json 的判据是 **48h**⇒ 声明与调度长期不一致：self_markers.json 实测陈旧 63h、环 self-markers 恒红，而它的 5 个消费者（consciousness_blueprint / brain_daily_snapshot / brain_regions_tick / trinity_hud / loop_health）长期读的是陈旧自评。生产者实测 **4 秒**、1KB 产物 ⇒ 放日链成本可忽略。
            # 位置刻意放在**两个长任务之前**（value-recalib / pg-embed 为链尾长任务），避免重蹈 2026-09-15 的尾部饿死；其余任务相对顺序一字未动。
            # 回滚：从 $dcTasks 里删掉 self-upgrade（注释可留）。
            # 2026-09-17（全面评价 P1-2）：**brain-status 进日链**。与上一行 self-upgrade
            # **同一类缺陷的第二例**：任务在 trinity-dsh-maintenance.ps1 里已实现
            # （L2222，跑 scripts/brain_mechanisms_status.py），也在 `all` 集合里（L2135），
            # 但日链走的是**下面这份显式清单** ⇒ 任务从来没有被调度过。
            # 实测：`output/brain_mechanisms_status.json` 最后写入 **09-15 15:03**，
            # 而 loop_health 的 brain-status 环判据是 **<36h** ⇒ 环恒红
            # （09-14/15/16/17 四份日链日志里 'task: brain-status' 出现 **0** 次）。
            # 生产者实测 **15.8 秒** / 12KB 产物（本机实测，2026-09-17）⇒ 成本可接受。
            # 位置放在 self-upgrade 之前、两个链尾长任务之前，避免尾部饿死。
            # 回滚：从 $dcTasks 里删掉 brain-status（注释可留）。
            # 2026-09-17（$795 写入经济学同轮）：**consolidate-temporal 进日链**。
            # 与 self-upgrade / brain-status **同一类缺陷的第三例**：任务已在
            # trinity-dsh-maintenance.ps1 里实现（L2356，跑 scripts/consolidate_temporal.py），
            # 也在 $allowed 白名单（L32）里，但**既不在 `all` 展开（L2135 的 13 项）
            # 也不在下面这份显式日链清单** ⇒ 从来没有被调度过。
            # 实测（2026-09-17）：~/.trinity/consolidate_state.json 停在 09-15 17:29、
            # processed_days 只到 2026-09-14；maintenance 日志里它最后三次运行
            # （08-27 / 09-11 / 09-14）**全是 dryrun=True**；
            # 后果可量：category='consolidation' 记忆仅 10 条、memory_layer='consolidated' 仅 3 条。
            # 而 blood_flow_status L1（>48h 且未登记原因）把它判成全仓**唯一红项** ⇒ 
            # overall=RED。生产者实测（本轮补跑）：daily 3 天 ≈ 40 秒。
            # 位置放在两个链尾长任务（value-recalib / pg-embed）之前，避免尾部饿死。
            # 回滚：从 $dcTasks 里删掉 consolidate-temporal（注释可留）。
            # 2026-09-17（$797 L0 摘要 sidecar 同轮）：**summary-layer 与 pagetree 进日链**。
            # 与 self-upgrade / brain-status / consolidate-temporal **同一类缺陷的第四、五例**：
            # 两个任务都在 $allowed 白名单、都有 maintenance 分支（L2297 / L2216），
            # 但都不在这份显式日链清单里 ⇒ 从未被调度。后果可量（2026-09-17 实测）：
            #   · summary-layer → metadata.summary_extractive 覆盖率 **223/24,942 = 0.9%**
            #     （它正是「每轮可见目录 / 分层摘要」这条对标短板的落地者）；
            #   · pagetree      → 页树摘要覆盖率是 eval 断言 pagetree-summary-coverage 的依据。
            # 成本实测：pagetree 每日增量 ~1.2s（全量重建按龄 >=6 天补跑，dry-run 全扫 59s）；
            #          summary-layer --cap 30 实测 25s。两者都放在链尾长任务之前，避免尾部饿死。
            # 回滚：从 $dcTasks 里删掉 summary-layer,pagetree（注释可留）。
            $dcTasks = 'pg-backfill,mirror,decay,tiers,consolidate,dedup,pg-sync,sync,compact,agent-ttl,active-health,canary,quality-gate,db-health,analytics,backup,observe,perception-bridge,dcpm-consolidate,integrity-monitor,self-reflect,reconcile,snapshot,market-list,replay,proactive,opsbot-cycle,perception-archival,disk-growth,brain-status,self-upgrade,consolidate-temporal,summary-layer,pagetree,value-recalib,pg-embed,auditverify'
            # 2026-10-06（复评 F2 收尾）：把 `auditverify`（= scripts/audit_fullchain_verify.py，
            # 全链完整性校验 + audit_runs 记账）接进日链。此前它**注册齐全但从未被调度**：
            #   · dsh-ops/trinity-dsh-maintenance.ps1:32 白名单里有它、:2671 有 dispatch 分支；
            #   · 但本文件全部 `-Tasks` 调用点都不含它 ⇒ 实测 fullchain_audit.jsonl 最后写入
            #     2026-09-10，PG audit_runs 只有 6 行且全在同一天 ⇒ **26 天零运行**。
            # 为什么现在接：F2 已把外锚从"只锚 PG"扩到"同时锚在服 SQLite 链"，
            # 而 audit_fullchain_verify.py 校验的正是 SQLite 链 —— 锚与校验必须成对，
            # 否则"链被改写"有锚无校验，"链没被改写"也无从证明。
            # 代价（实测，非估计）：该任务会调 `GET :8001/audit/integrity`（359k 行），
            # 同端点实测 13–96s；且它会**写 PG `audit_runs`**（不是纯只读）与
            # `~/.trinity/logs/fullchain_audit.jsonl`。日链有预算（见下方 tail 分段的
            # _dcTasks 预算），此任务量级可接受。
            # ⚠️ 这一改动**推翻**了 dsh-ops/EXECUTION-archive-202609.md:2042 记载的
            # 「决策 #1（43 项孤儿任务）：不删除、不盲目调度，登记待人工逐项裁定」中
            # 关于 auditverify 的"暂不行动"。本次是**用户明确指示"全部执行"**下的接线，
            # 非擅自行动。若要回退：把本行末尾的 `,auditverify` 删掉即可。
            # 2026-10-05：**`prewarm` 曾两次加入日链、两次撤出**，最终**不在日链**（现状）。
            # 三次判断全部由**实测读数**驱动，过程与依据留档如下 —— 尤其是最后那次的
            # **性能事实**，它是"为什么不能放日链"的决定性证据。
            #
            # 【为什么它以前根本不产出】语料向量索引预热原先在 **API 的 lifespan 里**跑。
            #   该进程被本仓库**刻意**限成 `TRINITY_ONNX_THREADS=1`
            #   （`trinity-supervisor.ps1:84-93` §1020：8 线程会占满 CPU ⇒ uvicorn 拿不到
            #   时间片 ⇒ 健康探测 >20s ⇒ 被 supervisor 判"不服务"而杀）。
            #   因此"预热跑不完却稳定烧一个核"。修法：把预热**移出 API 进程** ——
            #   `scripts/run_prewarm.py` 新增 `_warm_corpus_index()`（独立进程、带墙钟预算、
            #   **可续跑**：索引与嵌入缓存都落盘，实测 `idx_rows 1800→3600→4000`）。
            #
            # 【决定性性能事实（本会话逐一排除后得到）】
            #   · `TRINITY_ONNX_THREADS` 1 vs 8：**0.101 vs 0.105 s/行** ⇒ **不影响**吞吐；
            #   · 文本长度（首 200 行平均 370、最大 3000 字符）：隔离 **0.150 s/行** ⇒ **不影响**；
            #   · **与其它维护任务并发**：隔离 **0.107~0.150 s/行** vs 维护链运行中 **~7.9 s/行**
            #     ⇒ **≈50×，这才是真因**。
            #   ⇒ 日链**本身就是并发环境**（`~/.trinity/logs/` 里有
            #     `invoke-maintenance(brain-regions)`、`invoke-maintenance(perception)` 等
            #     并发调用的日志，实测与那次 1575s 的单轮时间窗重合）
            #     ⇒ 预热在日链里**必然超预算、被记 FAILED**（实测两次 `exit 124`）。
            #
            # 【因此的正确用法（不是"日链任务"）】
            #   在**安静窗口**手动/单独调度：
            #     `dsh-ops/trinity-dsh-maintenance.ps1 -Tasks prewarm`
            #   配合 `TRINITY_PREWARM_CORPUS_BUDGET_S`（默认 1200s）分块推进。
            #   实测该方式**可以成功**：`prewarm : OK`（rc=0）、`saved=True`、
            #   `corpus_vec.manifest.json.rows=200`、锁正常释放。
            #   ⚠️ 但即便成功，**经维护链单轮也实测到 1575s**（隔离态只要 ~30s）——
            #   说明"经维护链"这条路径本身就在争抢环境中，这也是不把它放进日链的原因。
            #
            # 【撤出的纪律依据】本仓既定："不能把会 FAILED 的任务留在调度里"
            #   （污染 `dsh-maintenance.log` 的失败计数与 MAINT-AUDIT、且白烧最多 1700s CPU）。
            #   第一次撤出（`6c815be`）依据"从未成功"；第二次（`03e3fb1`）依据升级为
            #   "**能在安静窗口成功，但在日链的并发环境下必然超预算**"。
            #
            # 【回滚 / 重新加入的前置条件】把 `prewarm` 加回 `$dcTasks`（放 `pg-embed` 前），
            #   并**先取得**如下的成功读数：`-Tasks prewarm` → `prewarm : OK` →
            #   `corpus_vec.manifest.json.rows > 0`。判据：`dsh-autostart.log` 出现 prewarm 段
            #   且该段不是 `TASK BUDGET EXCEEDED`。
            # 关闭语料那一段：`TRINITY_PREWARM_CORPUS=0`。
            # 2026-09-15（R41-P23）②：**链可续跑**——把上一（几）轮被截断而漏跑的任务，
            # 在启动期就拼到声明列表**最前面**，使其不必再排队到链尾去等下一次意外关机。
            # 与"后移长任务"互补：后者缩小被截断的窗口，本项保证**被截断的账下一轮先还**。
            # 计划由 scripts/chain_catchup_plan.py 给出（只输出计划、不执行任务）：
            # 它做 **FIFO 排空**（先补最早的欠账），因为固定取前 N 个会让后面的永远轮不到
            # ——那只是把"饿死"从链尾搬到补跑清单里。上限默认 10，避免把链重新撑长。
            # fail-open：计划脚本任何异常 → 取到空串 → 行为与接线前**逐字一致**。
            # 关闭：TRINITY_CHAIN_CATCHUP=off（脚本自身即输出空行）。
            try {
                $catchupPy = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
                $catchupPlan = Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts\chain_catchup_plan.py'
                if ((Test-Path $catchupPy) -and (Test-Path $catchupPlan)) {
                    $catchup = & $catchupPy $catchupPlan 2>$null | Select-Object -Last 1
                    if ($catchup) {
                        # **必须先剔除重复**：欠账任务本就在日链清单里（它们正是被截断的链尾），
                        # 直接前拼会让它们跑两遍、并把链撑长。正确语义是
                        # "**欠账挪到最前、其余顺延**"（既不重复，也不丢）。
                        $owed = @($catchup.Split(',') | Where-Object { $_ })
                        $rest = @($dcTasks.Split(',') | Where-Object { $owed -notcontains $_ })
                        $dcTasks = (@($owed) + $rest) -join ','
                        Write-Log ('chain-catchup: moved missed tasks to front -> ' + ($owed -join ','))
                    } else {
                        Write-Log 'chain-catchup: no owed tasks (or disabled)'
                    }
                } else {
                    Write-Log 'chain-catchup: plan script or python missing - skipping' 'WARN'
                }
            } catch { Write-Log ('chain-catchup plan FAILED (fail-open): ' + $_.Exception.Message) 'WARN' }
            $dcArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Maintenance, '-Tasks', $dcTasks)
            Start-Process -FilePath 'powershell' -ArgumentList $dcArgs -WindowStyle Hidden -RedirectStandardOutput $dcOut -RedirectStandardError $dcErr
            Write-Log ('daily-chain launched (non-blocking) -> ' + $dcOut)
        } catch { Write-Log ('daily-chain launch FAILED: ' + $_.Exception.Message) 'WARN' }
        Set-Content -Path $dcMark -Value (Get-Date -Format o)   # 文件标记：重启后不再重复启动
        $lastDaily = $today


        # ── 每日 03:00：评测/消融语料 active 面自愈隔离（2026-09-10 体检 659 P0-1）──
        # 评测流水线自身会显式关掉写入守卫（评测期间需检索自己写入的语料），
        # 故污染只能"运行后"清理。脚本内置在跑保护：目标 agent 近 30 分钟仍有
        # 写入则 SKIP，绝不打断在跑实验。
        $qScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\quarantine_benchmark_active.py'
        if (Test-Path $qScript) {
            $qPy = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
            $qOut = Join-Path $LogDir ('benchmark-quarantine-' + $today + '.out.log')
            $qErr = $qOut + '.err'
            $qp = Start-Process -FilePath $qPy -ArgumentList @($qScript, '--apply', '--min-age-days', '1') -WindowStyle Hidden -RedirectStandardOutput $qOut -RedirectStandardError $qErr -PassThru
            try { Wait-Process -Id $qp.Id -Timeout 900 -ErrorAction Stop } catch { Stop-Process -Id $qp.Id -Force -ErrorAction SilentlyContinue; Write-Log 'benchmark-quarantine timed out - killed' 'WARN' }
            $qTail = if (Test-Path $qOut) { (Get-Content $qOut -Tail 2 -ErrorAction SilentlyContinue) -join ' | ' } else { '' }
            Write-Log ('benchmark-quarantine done: ' + $qTail)
        }

        # ── 每日 03:00：PG active 面重复副本归档（2026-09-10 体检 659）────────
        # 一次性去重不持久：实测归档 4,071 条后，随着持续写入重复组由 7 回涨到 85。
        # 故纳入日链：① 先 --backfill-hash 回填 NULL content_hash（实测回填是一次性脚本，
        # 未入链后已从 0 回涨到 73 条，唯一索引对 NULL 完全无效）；
        # ② 再按 (persona,agent,content_hash) 每组保留 1 条（access_count>
        # importance>created_at），其余归档；CSV 留证、可恢复。
        $dScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\pg_content_hash_and_dedup.py'
        if (Test-Path $dScript) {
            $dPy = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
            $dOut = Join-Path $LogDir ('active-dedup-' + $today + '.out.log')
            $dp = Start-Process -FilePath $dPy -ArgumentList @($dScript, '--backfill-hash', '--dedup', '--apply') -WindowStyle Hidden -RedirectStandardOutput $dOut -RedirectStandardError ($dOut + '.err') -PassThru
            try { Wait-Process -Id $dp.Id -Timeout 900 -ErrorAction Stop } catch { Stop-Process -Id $dp.Id -Force -ErrorAction SilentlyContinue; Write-Log 'active-dedup timed out - killed' 'WARN' }
            $dTail = if (Test-Path $dOut) { (Get-Content $dOut -Tail 2 -ErrorAction SilentlyContinue) -join ' | ' } else { '' }
            Write-Log ('active-dedup done: ' + $dTail)
        }

        # ── 每日 03:00：健康快照 + 趋势 + 容量外推（2026-09-11 体检 669）──────
        # 动机：09-10 与 09-11 两次事故（autovacuum 阈值失效、PG 日志 161GB、连接打满）
        # 都有数周前置征兆，但没有任何地方在记录趋势，每次都是爆了才查。
        # 本任务按日落盘关键指标并做分档判定（健康/降级/故障）+ 14 天窗口外推。
        $hScript2 = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\trinity_health_snapshot.py'
        if (Test-Path $hScript2) {
            $hPy = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
            $hOut = Join-Path $LogDir ('health-snapshot-' + $today + '.out.log')
            $hp = Get-ChildResult -FilePath $hPy -ArgumentList @($hScript2) -OutFile $hOut -ErrFile ($hOut + '.err') -Label 'health-snapshot' -TimeoutSec 600
            $hTail = if (Test-Path $hOut) { (Get-Content $hOut -Tail 1 -ErrorAction SilentlyContinue) -join ' ' } else { '' }
            # 2026-09-11（审计 ③）：本工具此前只把分档结果 print 到 stdout，无人消费——
            # 属典型的「只写状态」。改为按退出码分级上报，让 FAULT 真正进入告警面。
            $hrc = $hp.ExitCode
            if ($hrc -ne 0) { Write-Log ('health-snapshot FAULT DETECTED: ' + $hTail) 'WARN' }
            else { Write-Log ('health-snapshot done: ' + $hTail) }
        }

        # ── 每日 03:00：维护链静默漏跑审计（2026-09-11 体检 670）────────────
        # 需求来源：维护链是 30+ 任务串行长链，超时被 kill 时**尾段任务会被静默跳过**，
        # 而链条整体仍算跑过；2026-09-11 更极端——4 个自愈任务因循环跑旧代码一个都没进
        # 调度，靠人肉翻日志才发现。调研结论：静默漏跑不能靠 exit code 检测，
        # 必须比对「声明任务集合 vs 实际执行集合」的差集。本工具直接读维护日志自动比对。
        $aScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\maintenance_chain_audit.py'
        if (Test-Path $aScript) {
            $aPy = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
            $aOut = Join-Path $LogDir ('maint-audit-' + $today + '.out.log')
            $ap = Get-ChildResult -FilePath $aPy -ArgumentList @($aScript, '--last', '12') -OutFile $aOut -ErrFile ($aOut + '.err') -Label 'maint-audit' -TimeoutSec 300
            $arc = $ap.ExitCode
            $aTail = if (Test-Path $aOut) { (Get-Content $aOut -Tail 1 -ErrorAction SilentlyContinue) -join ' ' } else { '' }
            if ($arc -ne 0) { Write-Log ('maint-audit GAP DETECTED: ' + $aTail) 'WARN' } else { Write-Log ('maint-audit ok: ' + $aTail) }
        }

        # ── 每日 03:00：PG 日志保留上限（2026-09-11 体检 660）──────────────
        # 修复前 log_filename 只有日期 + log_truncate_on_rotation=off，按大小轮转
        # 生成不出新文件名 → 单文件无限追加（实测 postgresql-20260902.log 达 112GB，
        # pgdata/log 合计 161.5GB）。配置已修（带时刻的文件名 + 100MB 轮转），
        # 此处再加一道保留兜底，防止任何原因导致的再次堆积。
        try {
            $pglog = Join-Path $env:USERPROFILE '.trinity\pgdata\log'
            if (Test-Path $pglog) {
                $cut = (Get-Date).AddDays(-7)
                $oldf = Get-ChildItem $pglog -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt $cut }
                if ($oldf -and $oldf.Count -gt 0) {
                    $mb = [math]::Round((($oldf | Measure-Object Length -Sum).Sum)/1MB, 1)
                    $oldf | Remove-Item -Force -ErrorAction SilentlyContinue
                    Write-Log ('pg-log-retention: removed ' + $oldf.Count + ' files / ' + $mb + ' MB (>7d)')
                } else {
                    Write-Log 'pg-log-retention: nothing older than 7d'
                }
            }
        } catch { Write-Log ('pg-log-retention failed: ' + $_.Exception.Message) 'WARN' }
        # -- 每日 03:00: HNSW 向量索引召回漂移守护 (2026-09-10) --
        # HNSW 是近似最近邻(ANN): 随索引膨胀/删除/重建会静默降级 -- 查询不报错,
        # 只返回看似合理但非真 top-k 的邻居。脚本用真实向量采样 + 精确顺序扫描
        # (enable_indexscan/bitmapscan=off) 对账召回率, 与基线比对, 漂移超阈值则 exit 1。
        # 只读: 不带 --apply 不写基线; 基线由首次手动 --apply 建立。
        $hScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\hnsw_recall_guard.py'
        if (Test-Path $hScript) {
            $hPy = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
            $hOut = Join-Path $LogDir ('hnsw-recall-guard-' + $today + '.out.log')
            $hErr = $hOut + '.err'
            # PS 5.1 坑: Start-Process -PassThru 在不带 -Wait 时不会填充 .ExitCode
            # (实测恒为空, Refresh/WaitForExit 都救不回来), 故由脚本自己写 rc 文件。
            $hRcFile = $hOut + '.rc'
            if (Test-Path -LiteralPath $hRcFile) { Remove-Item -LiteralPath $hRcFile -Force -ErrorAction SilentlyContinue }
            $hp = Start-Process -FilePath $hPy -ArgumentList @($hScript, '--sample', '50', '--k', '10', '--rc-file', $hRcFile) -WindowStyle Hidden -RedirectStandardOutput $hOut -RedirectStandardError $hErr -PassThru
            try { Wait-Process -Id $hp.Id -Timeout 900 -ErrorAction Stop } catch { Stop-Process -Id $hp.Id -Force -ErrorAction SilentlyContinue; Write-Log 'hnsw-recall-guard timed out - killed' 'WARN' }
            # rc 文件在 Wait-Process 返回后可能尚未 flush, 短重试直到可读
            $hrc = -1
            for ($i = 0; $i -lt 30; $i++) {
                if (Test-Path -LiteralPath $hRcFile) {
                    try { $hrc = [int](Get-Content -LiteralPath $hRcFile -Raw -Encoding UTF8).Trim(); if ($hrc -ge 0) { break } } catch { }
                }
                Start-Sleep -Milliseconds 300
            }
            $hTail = if (Test-Path $hOut) { (Get-Content $hOut -Tail 3 -Encoding UTF8 -ErrorAction SilentlyContinue) -join ' | ' } else { '' }
            if ($hrc -eq 1) { Write-Log ('hnsw-recall-guard DRIFT detected: ' + $hTail) 'WARN' }
            elseif ($hrc -eq 0) { Write-Log ('hnsw-recall-guard ok: ' + $hTail) }
            else { Write-Log ('hnsw-recall-guard broken (exit ' + $hrc + '): ' + $hTail) 'WARN' }
        }
    }

    # ── 每日 03:40-03:55：认知周期（大脑化 orchestrator：FSRS/Hebbian/情感/自评/提议）──
    if ($null -eq $lastBrain) { $lastBrain = '' }
    if ($now.Hour -eq 3 -and $now.Minute -ge 40 -and $now.Minute -lt 55 -and $lastBrain -ne $today) {
        $bcOut = Join-Path $LogDir ('brain-cycle-' + $today + '.out.log')
        $bcErr = Join-Path $LogDir ('brain-cycle-' + $today + '.err.log')
        $bcPy = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
        $bc = Start-Process -FilePath $bcPy -ArgumentList @('C:\Users\Administrator\trinity\scripts\brain_cycle.py') -WindowStyle Hidden -RedirectStandardOutput $bcOut -RedirectStandardError $bcErr -PassThru
        try { Wait-Process -Id $bc.Id -Timeout 1500 -ErrorAction Stop } catch { Stop-Process -Id $bc.Id -Force -ErrorAction SilentlyContinue; Write-Log 'brain-cycle timed out - killed' 'WARN' }
        Write-Log 'brain-cycle done (03:40 daily)'
        $lastBrain = $today
    }
    # EXECUTION 647: 错过补跑（hermes cron catchup 模式）——窗口错过且 cycle 陈旧 >36h 时随下一循环补跑
    elseif ($lastBrain -ne $today) {
        $bcState = Join-Path $env:USERPROFILE '.trinity\brain\cycle_state.json'
        if (Test-Path $bcState) {
            $bcAgeH = ((Get-Date) - (Get-Item $bcState).LastWriteTime).TotalHours
            if ($bcAgeH -gt 36) {
                $bcOut = Join-Path $LogDir ('brain-cycle-' + $today + '.catchup.out.log')
                $bcErr = Join-Path $LogDir ('brain-cycle-' + $today + '.catchup.err.log')
                $bcPy = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
                $bc = Start-Process -FilePath $bcPy -ArgumentList @('C:\Users\Administrator\trinity\scripts\brain_cycle.py') -WindowStyle Hidden -RedirectStandardOutput $bcOut -RedirectStandardError $bcErr -PassThru
                try { Wait-Process -Id $bc.Id -Timeout 1500 -ErrorAction Stop } catch { Stop-Process -Id $bc.Id -Force -ErrorAction SilentlyContinue; Write-Log 'brain-cycle catchup timed out - killed' 'WARN' }
                Write-Log 'brain-cycle done (missed catch-up)'
                $lastBrain = $today
            }
        }
    }

    # ── 每日 04:00-04:20：轻量尾段链（2026-09-08 从 03:00 大链拆出，防尾段饥饿）──
# 2026-09-09 大脑化修复：self-reflect/replay-consolidate/drift-check/web-search
# 从未调度或被 33 任务长链饿死 → 全部并入本轻链（cognition-check 亦加跑一次）。
    if ($null -eq $lastTail) { $lastTail = '' }
    # 658.99：**窗口放宽 + 断点续跑**。实测（09-12/09-13）autostart 循环会被 LoopGuard 等
    # 在 04:00-04:19 窗口内重启（09-12 于 03:57 与 04:17 重启 → 整链未跑；09-13 在 tail-light-1
    # 完成后 04:22 重启 → seg2/3/4 永久丢失）。原实现把"今天跑过"记在内存变量里，重启即丢。
    # 现改为：① 窗口放宽到 04:00-05:59；② 每段完成写"当日标记文件"，重启后**只补未完成的段**。
    if ($null -eq $lastTail) { $lastTail = '' }
    $tailStateDir = Join-Path $env:USERPROFILE '.trinity\state'
    if (-not (Test-Path $tailStateDir)) { New-Item -ItemType Directory -Path $tailStateDir -Force | Out-Null }
    $tailMark = Join-Path $tailStateDir ("tail_light_" + $today + ".done")
    $tailDone = @()
    if (Test-Path $tailMark) { $tailDone = @(Get-Content $tailMark -Encoding UTF8 -ErrorAction SilentlyContinue) }
    # 2026-09-15（R41-P23）：**去掉窗口上界**（原 `Hour -eq 4 -or Hour -eq 5` 两小时窗）。
    # 幂等由**逐段文件标记** `$tailMark`（见下 `Add-Content -Path $tailMark -Value $sp.n`）保证：
    # 已完成的分段不会被重跑，未完成的分段天然会补 ⇒ 放宽严格更安全。
    if ((Test-Path $Maintenance) -and $now.Hour -ge 4) {
        # 2026-09-11（第三轮审计 I3）：**尾段链再拆 3 段**——单段 38 任务共用 1800s
        # 预算时，尾段任务被系统性饿死。实测三天的 tail-light 日志：09-09 推进到第
        # 14 个任务(perception-scan)、09-10 第 25 个(cognition-check)、09-11 第 20 个
        # (procedure-extract) 就被 kill ——**38 个任务没有一次跑完**，且
        # `~/.trinity/logs` 下 *neuromodulate* / *priority-map* / *brain-heartbeat*
        # 命中 0（从未被调度）。后果不是"少跑几个任务"，而是**已接线的功能输入长期
        # 陈旧**：神经调制状态衰减后由 P1 排序的 ±0.05 调制项消费（实测 SHT 漂到
        # 0.252，基线 0.55、半衰期 900s），metacog_bias.json 陈旧则 T1 的偏差反馈
        # 读到旧值。处置：按 3 段调度，每段 ≤13 任务、各自 1200s 预算。
        # 回滚：TRINITY_TAIL_LIGHT_SEGMENTS=1 恢复成原来的单段 38 任务链。
        $tailSeg1 = 'curiosity,cognition-agent,situation,session-distill,trinity-hud,reader-agent,reader-agent-ops,expiry-review,smoke,rewards,blocks-heartbeat,identity-refresh,web-perception'
        $tailSeg2 = 'perception-scan,perception-recall,loop-health,brain-consumers,valence-backfill,confidence-bp,procedure-extract,self-reflect,replay-consolidate,drift-check,web-search,cognition-check'
        $tailSeg3 = 'fok-mark-test,fok-counts-fill,recurrence-consolidate,observation-build,brain-md-export,session-candidates,dream,emotional-consolidation,brain-heartbeat,value-gate,neuromodulate,copies-sweep,priority-map,metacog-monitor'
        # 658.71：**第 4 段——唤醒休眠的脑区任务**。审计发现这 9 个任务"有实现、有分发、
        # 但从未排入任何链"，自 08-30/08-31 起休眠 11 天（属"器官实现了但从不点火"）：
        # forgetting(遗忘评分) / predictive-loop(世界模型) / self-axioms(自我模型) /
        # self-assess(元认知自评) / narrative(自我叙事) / sensory-integration(感知整合) /
        # action-loop(行动) / memory-manager(工作记忆) / capability-check(能力自检)。
        # 实测总耗时约 300s，单段 1200s 预算充裕。
        # 658.74：审计发现另有 7 个任务 2~4 周未运行——挑两个有价值且轻量的并入本段：
        #   slo（服务级目标检查，最后运行 08-18）、pagetree（PageIndex 页树通道，08-27）。
        # 其余 5 个登记为设计性休眠：agent-sync / federation-sync（无联邦目标）、
        #   evolve（已被 evolve-loop/evolution 取代）、session-summarize（已被 session-distill/
        #   session-auto/session-candidates 取代）、fulltest（重型回归，按需手动跑）。
        $tailSeg4 = 'forgetting,predictive-loop,self-axioms,self-assess,narrative,sensory-integration,action-loop,memory-manager,capability-check,slo,pagetree'
        # 2026-09-27（§1361 实测）：`produce` 原在 seg2 段尾，**连续 6 天**（09-22..09-27）被段预算杀掉 ——
        # autostart 日志每天一条 `maintenance(tail-light-2) timed out (1200) - killing (last task: produce)`。
        # 后果不是「少跑一个任务」而是**已接线功能长期陈旧**：`~/.trinity/video_state.json` 停在 09-18（220h），
        # 而该文件的生产者设计上**每次运行都刷新**（即便 0 新增）⇒ 陈旧即「那一步从没跑完」。
        # 手工跑 `video_transcript_ingest.py --limit 5` 实测正常（files=1/skipped=1）且 state 当场刷新 ⇒ 不是生产者坏，是**段预算饿死**。
        # 处置：把 produce 单独成段（第 5 段，独占 1200s），与第 4 段同一写法（658.71 的先例）。
        # 判据（可失败）：次日 autostart 日志出现 `maintenance(tail-light-5) done` 且**没有** timed out；
        #           且 `~/.trinity/video_state.json` 的 mtime 逐日推进（>48h 陈旧 = 调度又断，见 blood_flow_status.py 的 STALE_REASONS）。
        # 回滚：删掉本段与 segPairs 里的第 5 项，并把 'produce' 放回 seg2 段尾。
        $tailSeg5 = 'produce'
        $tailAll = $tailSeg1 + ',' + $tailSeg2 + ',' + $tailSeg3 + ',' + $tailSeg4 + ',' + $tailSeg5
        # 658.99：每段完成即写当日标记；重启后只补未完成的段（断点续跑）
        if ($env:TRINITY_TAIL_LIGHT_SEGMENTS -eq '1') {
            if ($tailDone -notcontains 'all') {
                Invoke-Script -Path $Maintenance -ArgsList @('-Tasks', $tailAll) -Label 'maintenance(tail-light)' -TimeoutSec 1800
                Add-Content -Path $tailMark -Value 'all' -Encoding UTF8
            }
        } else {
            # 2026-09-27（§1362 实测）：**每段用自己的包装器预算**。原先是五段共用一个 1200s，
            # 而 seg5 的 `produce` 任务预算已按实测提到 1500s（见 maintenance 的 Initialize-TaskBudgets），
            # 段预算若仍是 1200 就**先把任务杀了**（任务预算形同虚设）⇒ 段预算必须 ≥ 段内最大任务预算。
            # 只给确实需要的那一段加预算，其余四段保持 1200s（不扩大未实测的爆炸半径）。
            $segPairs = @(
                @{ n = '1'; tasks = $tailSeg1; budget = 1200 }, @{ n = '2'; tasks = $tailSeg2; budget = 1200 },
                @{ n = '3'; tasks = $tailSeg3; budget = 1200 }, @{ n = '4'; tasks = $tailSeg4; budget = 1200 },
                @{ n = '5'; tasks = $tailSeg5; budget = 1700 })
            foreach ($sp in $segPairs) {
                if ($tailDone -contains $sp.n) { continue }
                $segBudget = 1200
                if ($sp.budget) { $segBudget = [int]$sp.budget }
                Invoke-Script -Path $Maintenance -ArgsList @('-Tasks', $sp.tasks) -Label ('maintenance(tail-light-' + $sp.n + ')') -TimeoutSec $segBudget
                Add-Content -Path $tailMark -Value $sp.n -Encoding UTF8
            }
        }
        $lastTail = $today
    }
    # ══════════════════════════════════════════════════════════════════════════
    # 2026-09-15（R41-P23）：**"按次数补跑"辅助**——把"一次性时间窗"换成"可补跑"
    # ══════════════════════════════════════════════════════════════════════════
    # 背景（实测）：本脚本多处用 `$now.Hour -eq N -and $now.Minute -lt M` 式判据，
    # **最窄窗口仅 10 分钟**（日链 03:00–03:09、感知 09:35–09:44、周一 03:10–03:29、
    # 周日 03:10–03:39 …）。而主机实测**每天约 4.6 次硬断电** ⇒ 错过窗口即
    # **整天（甚至整周）不执行**——锚点任务已因此连续两天未跑、两个看护环转红。
    #
    # 关键前提：这些块原本用**内存变量**（$lastScreen/$lastWeekly/$lastWeeklyAcc）
    # 做"已跑过"守卫，而**内存态在重启后会忘** ⇒ **不能直接去掉窗口**（会重复执行）。
    # 故先补**文件 mark**（跨重启有效），再去掉窗口上界 ⇒ 任何一轮 autostart 都能补上。
    function Test-OnceDue {
        param([string]$Name, [string]$Key)
        $mk = Join-Path $LogDir ("once-$Name-$Key.mark")
        return -not (Test-Path $mk)
    }
    function Set-OnceDone {
        param([string]$Name, [string]$Key)
        try {
            Set-Content -Path (Join-Path $LogDir ("once-$Name-$Key.mark")) `
                        -Value (Get-Date -Format o) -ErrorAction Stop
        } catch { }
    }
    function Get-WeekKey {
        # ⚠️ 为什么不用 `(Get-Date).ToString("yyyy-'W'ww")`（2026-10-07 t99/D1 实测修）：
        # **Windows PowerShell 5.1 上 `ww` 不展开** —— `(Get-Date).ToString('ww')` 原样返回字面量
        # `ww`，于是该格式串对**任何日期**都返回同一个常量 `2026-Www` ⇒ `once-weekly-2026-Www.mark`
        # 一年只可能被写一次 ⇒ `Test-OnceDue` 在当年**永远返回 False** ⇒ 周级闸首跑后**永久关闭**。
        # 现场后果：周一质量门禁链 / 周一 weekly-check / 周日 answer-eval 三条周级链自 2026-09-21
        # 起静默停摆 16.77 天（4 份周报产物冻结），而"产物新鲜度"审计只会在 16 天后才报 STALE。
        # ⛔ **不要改回字符串格式**：必须走 Calendar.GetWeekOfYear（5.1 与 7.x 都正确）。
        $d = Get-Date
        $c = [System.Globalization.CultureInfo]::InvariantCulture.Calendar
        $w = $c.GetWeekOfYear($d, [System.Globalization.CalendarWeekRule]::FirstFourDayWeek,
                              [System.DayOfWeek]::Monday)
        return "{0}-W{1:D2}" -f $d.Year, $w
    }

    # ── 每日 09:35 起：感知采样 10 分钟（EXECUTION 553；**窗口上界已去掉 ⇒ 可补跑**）────
    if ($null -eq $lastScreen) { $lastScreen = "" }
    if ((Test-Path $Maintenance) -and ($now.Hour -gt 9 -or ($now.Hour -eq 9 -and $now.Minute -ge 35)) -and (Test-OnceDue 'screen' $today)) {
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "perception-capture,perception-screen-ingest,perception-recall") -Label "maintenance(perception-capture,ingest,recall)" -TimeoutSec 1800
        Set-OnceDone 'screen' $today
        $lastScreen = $today
    }

    # ── 每周一 03:10-03:30：质量门禁 + 插件冒烟（2026-09-01）────────
    # 2026-09-15（R41-P23）：**周键 + 去掉窗口上界**（原为周一 03:10–03:29 的 20 分钟窗口）。
    # 错过即**等一周**，而主机每天 4.6 次断电 ⇒ 风险远高于日任务。守卫改为**周键文件 mark**。
    $wkKey = Get-WeekKey
    if ((Test-Path $Maintenance) -and $now.DayOfWeek -eq 'Monday' -and ($now.Hour -gt 3 -or ($now.Hour -eq 3 -and $now.Minute -ge 10)) -and (Test-OnceDue 'weekly' $wkKey)) {
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "quality-gate,plugin-smoke,brain-report,module-classify,eval-gate,self-upgrade,opsbot-deep-action,retro-boost,summary-layer,conflict-worker,dream-replay,opsbot-report,audit-reconcile,contradiction-resolve,flag-monitor,ipi-check,market-drill,blind-judge,reflect-rewrite,cognition-check,capability-check,brainification-guard,self-assess,predictive-loop,emotional-consolidation,narrative,loop-audit,memory-manager,memory-purify,archive-purge-audit,meta-strategy,cognitive-eval,evolve-loop") -Label "maintenance(quality-gate)" -TimeoutSec 4800  # EXECUTION 630: 认知评估入周链(差距矩阵唯一❌补调度)  # 2026-09-01: +brain-report 周报; 2026-09-04 EXECUTION 567: +Astra 安全/审计周任务
        Set-OnceDone 'weekly' $wkKey
        $lastWeekly = $today
    }


    # ── 每周一 03:31 起：周检产物（P3：supervisor/金丝雀/回归/镜像健康汇总；**窗口上界已去掉**）──
    $wcPath = Join-Path $PSScriptRoot "trinity-weekly-check.ps1"
    if ($null -eq $lastWeeklyCheck) { $lastWeeklyCheck = "" }
    if ((Test-Path $wcPath) -and $now.DayOfWeek -eq [DayOfWeek]::Monday -and ($now.Hour -gt 3 -or ($now.Hour -eq 3 -and $now.Minute -ge 31)) -and (Test-OnceDue 'weekly-check' $wkKey)) {
        Invoke-Script -Path $wcPath -Label weekly-check -TimeoutSec 900
        Set-OnceDone 'weekly-check' $wkKey
        $lastWeeklyCheck = $today
    }

    # ── 每周日 03:10 起：AnswerAcc 生成侧评测（500q LLM，20-30 分钟；**窗口上界已去掉**）────────
    # 2026-09-15（R41-P23）：原为 03:10–03:39 的 **30 分钟窗口**，错过即**等一周**（500q 评测数据断档）。
    # 守卫由内存变量改为**周键文件 mark**，窗口上界去掉 ⇒ 周日任何时刻恢复都能补跑。
    if ((Test-Path $Maintenance) -and $now.DayOfWeek -eq 'Sunday' -and ($now.Hour -gt 3 -or ($now.Hour -eq 3 -and $now.Minute -ge 10)) -and (Test-OnceDue 'weekly-acc' $wkKey)) {
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "answer-eval") -Label "maintenance(answer-eval)" -TimeoutSec 43200  # 2026-09-06 EXECUTION 595: 守卫接管(自愈可至2 attempts)
        Set-OnceDone 'weekly-acc' $wkKey
        $lastWeeklyAcc = $today
    }

    # ── 每 30 分钟：持续感知流（EXECUTION 458 P1-2；marker 文件防抖）──
    $percMark = Join-Path $LogDir "perception-loop.mark"
    $percDue = $false
    if (-not (Test-Path $percMark)) { $percDue = $true }
    elseif (((Get-Date) - (Get-Item $percMark).LastWriteTime).TotalMinutes -ge 30) { $percDue = $true }
    if ($percDue -and (Test-Path $Maintenance)) {
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "perception-continuous,brain-event") -Label "maintenance(perception)" -TimeoutSec 900
        Set-Content -Path $percMark -Value (Get-Date -Format o)
    }

    # ── 每小时：脑区驱动器（2026-09-09 大脑化保证运行）──
    $brainMark = Join-Path $LogDir "brain-regions.mark"
    $brainDue = $false
    if (-not (Test-Path $brainMark)) { $brainDue = $true }
    elseif (((Get-Date) - (Get-Item $brainMark).LastWriteTime).TotalMinutes -ge 60) { $brainDue = $true }
    if ($brainDue -and (Test-Path $Maintenance)) {
        # §991：同一小时档里带上 memory-correlation（内存/重启/停摆时间线落 PG，供长期对照）
        Invoke-Script -Path $Maintenance -ArgsList @("-Tasks", "brain-regions,memory-correlation") -Label "maintenance(brain-regions)" -TimeoutSec 900
        Set-Content -Path $brainMark -Value (Get-Date -Format o)
    }

    Start-Sleep -Seconds $SupervisorIntervalSec
}
# self-check e2e test
# self-check e2e verify
# loop-guard e2e verify
