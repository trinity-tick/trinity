#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""maintenance_chain_audit.py — 维护链「声明 vs 实际执行」差集审计（体检 669 / 调研建议）。

为什么需要它：维护链是 30+ 任务的**串行**长链，一旦超时被 kill（实测 03:00 链多次
以 600s 被杀），**尾段任务会被静默跳过**——而链条整体仍"跑过了"，日志里只留一行
timed out，很容易被忽略。2026-09-11 更极端：新增的 4 个自愈任务因循环跑旧代码而
**一个都没进调度**，是靠人肉翻日志才发现的。

核心洞察（调研）：**静默漏跑不能靠 exit code 检测，必须靠「声明集合 vs 实际执行集合」
的差集**。本工具不需要维护静态清单——维护日志本身就记录了每次链的声明任务
（maintenance start ... tasks=...）与逐个执行记录（===== task: X =====），
故直接比对即可，且随代码自动更新。

用法：
    python scripts/maintenance_chain_audit.py             # 审计最近 N 次链
    python scripts/maintenance_chain_audit.py --last 20
    python scripts/maintenance_chain_audit.py --quiet     # 只在有缺口时输出（供日链）
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:
    def swallow(*_a, **_k):
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(*_a, **_k)
        except Exception:
            return None

LOG = os.path.expanduser(r"~/.trinity/logs/dsh-maintenance.log")
AUTOSTART_LOG = os.path.expanduser(r"~/.trinity/logs/dsh-autostart.log")
RE_TASK = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[INFO\] ===== task: (.+?) =====")
# 2026-09-18：**任务完成行**（`<task> : OK` / `<task> : FAILED (exit N)`）。
# 为什么需要它：并发写 dsh-maintenance.log 时 `Add-Content` 会抛**非终止错误**
# （GetContentWriterArgumentError，"流不可读"），原 try/catch 抓不到 ⇒ 行被静默丢弃。
# 实测 09-18 03:05 链丢了 `dcpm-consolidate` 的 **头行**（任务确实跑完并打了 OK/end），
# 审计据此误报漏跑 ⇒ chain-reconcile 环红、血流 L4 RED。
# 完成行是**任务名唯一**的证据（一个名字只会完成一次），比头行更强，故作为独立证据源。
RE_TASK_DONE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[(?:INFO|WARN)\] ([A-Za-z0-9_.\-]+) : (?:OK|FAILED)(?:\s|$)")
RE_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
RE_WRAP_KILL = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[WARN\].*timed out \((\d+)\).*killing")
RE_START = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[INFO\] maintenance start \(mode=(\w+), tasks=([^,]+(?:,[^=]*?)?), dryrun=(\w+)\)")


def _wrapper_kill_ts(timeout: int = 30):
    """`dsh-autostart.log` 里 `timed out (N) - killing` 的时刻列表。

    2026-09-15（R41-P23）：查"唯一 unexplained 缺口"时发现**第三种独立截断机制**——
    `trinity-autostart.ps1::Invoke-Script` 到点会 `Stop-Process -Id $child.Id -Force`
    **直接杀掉整条链**（包装器超时），链上尚未执行的任务全部丢失。
    实测 15 条历史记录（09-11 三次、09-13、09-14 四次、09-15 三次…），
    其中 09-15 09:44 那条（`perception-capture,ingest,recall`，超时 1800s）
    就是上一版归因判成 `unexplained` 的那个——它的死因其实**明文写在日志里**。

    为什么要读它：这比"从开机事件反推"精确得多，且能纠正误判——
    例如 09-15 04:18 那条，旧规则据 08:57 开机误判为 `host-restart`，
    真实死因是 `04:38:18 tail-light-1 timed out (1200) - killing`。
    失败一律返回 []（fail-open）。
    """
    out = []
    try:
        import datetime as _dt
        with open(AUTOSTART_LOG, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = RE_WRAP_KILL.match(line)
                if not m:
                    continue
                try:
                    out.append((_dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp(),
                                int(m.group(2))))
                except Exception:
                    continue
    except Exception:
        return []
    return out


def _host_lifecycle_ts(timeout: int = 60):
    """系统生命周期事件时间戳（启动 / 意外关机 / 内核启动），用于给漏跑**归因**。

    2026-09-15（R41-P23）：查 chain-reconcile 红项时手工查到根因是**主机意外关机**
    （Event 6008 + LastBootUpTime，实测 2026-09-15 04:47 宕机至 08:57，把日链拦腰
    截断，尾部 8 任务整夜未执行）。该结论当时靠人工翻事件日志 + 逐行读维护日志
    才得出（约半小时）。这里把它自动化，使报告**一眼可判**"这次缺口是主机没了，
    还是链自己漏跑"。

    **只归因、不改判定**：调用方仍按 `gaps == 0` 判红绿（不得用本函数让红项变绿
    ——那等于把真实后果藏起来）。

    只在**确实存在 gaps** 时调用（见 main），避免每 5 分钟白跑一次 Get-WinEvent。
    失败一律返回 []（fail-open：归因失败 ⇒ 记为"未归因"，绝不影响判定）。
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-WinEvent -FilterHashtable @{LogName='System'; Id=6005,6008,41,12} "
             "-MaxEvents 60 -ErrorAction SilentlyContinue | "
             "ForEach-Object { $_.TimeCreated.ToString('o') }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout)
        out = []
        import datetime as _dt
        for ln in (r.stdout or "").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(_dt.datetime.fromisoformat(ln).timestamp())
            except Exception:
                continue
        return out
    except Exception:
        return []


def _attribute(chain_ts: str, last_ts, host_ts, wrapper_ts,
               kill_window_min: float = 30.0, boot_window_h: float = 12.0):
    """给一次漏跑归因。**只归因、不改判定**（判定仍是 `gaps == 0`）。

    优先级（强 → 弱），每条都用实测校准过：
      1) `wrapper-timeout` —— **直接证据**：`dsh-autostart.log` 有 `timed out … - killing`
         且其时刻落在该次链**最后活动时刻**之后 kill_window_min 分钟内。包装器就是在
         那一刻 `Stop-Process -Force` 掉了子进程。
         实测：09:44 链最后输出 09:54:47 / WARN 10:14:18（+19.5min）；
               04:18 链最后输出 04:21:29 / WARN 04:38:18（+16.8min）。
      2) `host-restart` —— 主机生命周期事件落在 [最后活动, 最后活动+boot_window_h] 内。
         实测：03:05 **日链**最后输出 04:51:04、08:57 开机。日链由 `Start-Process`
         **直接拉起、不经 Invoke-Script**，故天然不会有 (1)——这正是能把两者分开的关键。
      3) `unexplained`。
    """
    def _p(s):
        try:
            import datetime as _dt
            return _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            return None

    _last = _p(last_ts) if last_ts else _p(chain_ts)
    for _ts, _to in (wrapper_ts or []):
        if _last is not None and _last <= _ts <= _last + kill_window_min * 60:
            return "wrapper-timeout(%ds)" % _to
    if host_ts and _last is not None:
        for _h in host_ts:
            if _last <= _h <= _last + boot_window_h * 3600:
                return "host-restart"
    return "unexplained"



#: ⭐ 2026-10-08（t111/G8 · D-14 案(b)）**可注入接缝**：产物目录与"已登记孤儿"登记源
#: 之所以提成模块级常量/函数（而不是写死在 ARTIFACT-CHECK 里）：判据要能**确定性地**压
#: 「未登记的陈旧 ⇒ 仍计入 gaps」这条**牙齿**（把 STATE_DIR 指到临时目录即可），
#: 不必依赖本机 `~/.trinity/state` 的真实新鲜度。
STATE_DIR = os.path.expanduser("~/.trinity/state")
REGISTRY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "docs", "SILENT_FAILURE_BUDGETS.json")


def _registered_orphan_names():
    """读 `docs/SILENT_FAILURE_BUDGETS.json::registered_orphans::items` 的键集合。

    判定口径（与 G5/D-13 共享，逐字登记在该文件的 `_consumer_predicate`）：
      ORGAN_REGISTRY 的 `consumers` 与 `code_readers_sample` **均空** 且 全仓检索该文件名
      **只命中三类**：生产者 / 审计清单 / 登记本身 ⇒ **无消费者**。

    ⚠️ **fail-closed**：读不到/解析失败 ⇒ 返回空集 ⇒ **一切陈旧仍按 `STALE` 计入 gaps**
    （宁可真红，不假绿）。
    """
    try:
        import json as _json3
        with open(REGISTRY_PATH, encoding="utf-8") as _fh:
            _reg = _json3.load(_fh).get("registered_orphans") or {}
        return set((_reg.get("items") or {}).keys())
    except Exception:
        return set()


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception as _e:
        swallow(__name__, _e)
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=10)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--supersede-window", type=int, default=120,
                    help="全链被后续链接管时，在该分钟窗口内按任务名回收执行记录")
    ap.add_argument("--inprogress-grace", type=int, default=45,
                    help="最后一次链在多少分钟内未收尾仍视为在跑（默认 45 分钟）")
    # 2026-09-15（R41-P23）：链可续跑所需的"欠账计划"（只输出、不执行任何任务）。
    # 2026-09-20（§918）：可指定日志路径 —— 让"并发链在跑豁免"能在**合成日志**上验证
    # （否则只能等生产事故复现，本仓对"不可复现的判据"有前科）。
    ap.add_argument("--log", default=LOG,
                    help="维护日志路径（默认 ~/.trinity/logs/dsh-maintenance.log）")
    ap.add_argument("--missing-json", action="store_true",
                    help="额外输出 MISSING-JSON 行：按时间顺序列出所有已判定漏跑的运行及其缺失任务")
    a = ap.parse_args(argv)

    if not os.path.exists(a.log):
        print("MAINT-AUDIT: 日志不存在 " + a.log)
        return 0

    # 2026-09-14（689）：**头部缺失的误报**修正。实测 00:24 那次链：declared=consolidate-recent，
    # 日志里**没有** "===== task: consolidate-recent =====" 头（子进程输出被整体捕获/交错吞掉了头行），
    # 但任务确实跑了（sleep_consolidation 的 Phase1/Phase2 都打了、还落了事实）。
    # 原判定只看头行 ⇒ 报"缺失"，即 P1 的"差集=0"被**误报**。现补一层**任务证据正则**：
    # 头行缺失时，只要本次链窗口内出现该任务的专有输出，就算执行过。
    TASK_EVIDENCE = {
        "consolidate-recent": "sleep_consolidation:",
        "perception-continuous": "perception_state",
        "brain-regions": "regions_total",
        # 2026-09-14（708）：backup 的**任务头行**自 09-10 起不再出现（链改造后备份改由另行
        # 调用执行），但产物一直在：实测 04:12:34 打出 backup ok → trinity_store_*.db(968MB)、
        # 04:13:42 pg backup -> *.dump(355MB)、offsite backup done: 2/2。
        # 缺这条件时审计把“备份没跑”报成缺口（实测 GAP 缺失: backup）——是**误报**。
        "backup": "backup ok",
    }

    def _evidence_hit(line: str) -> str:
        for _t, _needle in TASK_EVIDENCE.items():
            if _needle in line:
                return _t
        return ""

    runs = []          # [{ts, declared:[...], executed:[...], finished:None|'OK'|'FAILED'}]
    task_events = []   # [(line_no, task)] —— 705/708：用于**并发链交错**下的窗口化归属
    evidence_events = []  # [(line_no, task)] —— 708：任务证据行（头行缺失时按专有输出归属）
    done_events = []   # [(line_no, task)] —— 2026-09-18：任务**完成行**（头行被并发写吞掉时的强证据）
    starts = []        # [(line_no, ts, declared)]
    cur = None
    _ln = 0
    with open(a.log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            _ln += 1
            m = RE_START.match(line)
            if m:
                if cur:
                    runs.append(cur)
                declared = [t.strip() for t in m.group(3).split(",") if t.strip()]
                # 719：**dry-run 链不算执行**（10:37:55 那次 12 任务全为 [DRY-RUN] 却报 finished OK，
                # 被审计记成"declared=12 executed=2" ⇒ 假缺口）。头部已带 dryrun= 字段，直接识别。
                cur = {"ts": m.group(1), "declared": declared, "executed": [], "finished": None,
                       "dryrun": str(m.group(4)).lower() in ("true", "1", "yes"),
                       "start_line": _ln}
                starts.append((_ln, m.group(1), declared))
                continue
            if cur is None:
                continue
            # 2026-09-15（R41-P23）：记录本链的**最后活动时刻**——归因要用它把
            # "包装器超时"（WARN 紧跟其后 ≤30min）与"主机宕机"区分开。
            _mts = RE_TS.match(line)
            if _mts:
                cur["last_ts"] = _mts.group(1)
            m2 = RE_TASK.match(line)
            if m2:
                cur["executed"].append(m2.group(2).strip())
                task_events.append((_ln, m2.group(2).strip()))
                continue
            m3 = RE_TASK_DONE.match(line)
            if m3:
                _dt_name = m3.group(2).strip()
                done_events.append((_ln, _dt_name))
                if _dt_name not in cur["executed"]:
                    cur["executed"].append(_dt_name)
                continue
            _ev = _evidence_hit(line)
            if _ev:
                evidence_events.append((_ln, _ev))
                if _ev not in cur["executed"]:
                    cur["executed"].append(_ev)
                continue
            if "maintenance finished OK" in line:
                cur["finished"] = "OK"
            elif "maintenance finished with FAILED tasks" in line:
                cur["finished"] = "FAILED"
    if cur:
        runs.append(cur)

    # 2026-09-14（708）**并发链交错**修正：本系统的调度里，"每日全链（29 任务）"与
    # supervisor/autostart 的**小任务子链**（mode=Direct, tasks=2~7 个）会**同时在跑**。
    # 原实现按"下一个 maintenance start 之前"归属任务 ⇒ 全链一旦被 11 分钟后的小子链
    # 打断，剩余 28 个任务的执行记录就全被算到**子链**头上，于是全链被报成
    # "declared=29 executed=1" 的**假缺口**（实测 03:03:50 那次：任务其实一路跑到 04:28）。
    # 修正：对**全链**（declared 较多）在"下一个全链开始（或 EOF）"的更宽窗口内按任务名回收，
    # 并标记 interleaved=True（口径透明，不隐藏交错事实）。
    # 708 二次修正：改按**时间窗**回收（不是"下一个全链切段"）。实测 03:03:50 起了 29 任务全链、
    # 只跑 1 个任务，03:11:20 又有一次 **33 任务全链**接上并把同名任务全部跑完 —— 任务确实执行了，
    # 只是记在另一次链调用名下。窗宽 --supersede-window（默认 120 分钟）。
    import datetime as _dt2

    def _parse_ts(_s):
        try:
            return _dt2.datetime.strptime(_s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    _i_by_line = {s[0]: i for i, s in enumerate(starts)}
    for r in runs:
        _i = _i_by_line.get(r.get("start_line"))
        if _i is None or r.get("dryrun"):
            continue
        # 719：**取消 declared<10 的豁免**——小子链同样会被交错影响（实测 10:38:07 的
        # health/evolution/session-auto 三任务链缺 session-auto，而 session-auto 在更晚的链里跑了）。
        # 719：50% 阈值只对**大链**生效——小链（如 3 任务链跑到 2 个）同样会被交错切走，
        # 原写法让它跳过回收 ⇒ 实测 10:38:07 链缺 session-auto，而 session-auto 10:39:41 就在同一链里跑了。
        # 回收窗口上界：本链起点 + --supersede-window 分钟内的后续链。
        # （2026-09-18：原实现把这段放在 50% 闸门之后，完成行回收需要它，故上移。）
        _t0 = _parse_ts(r["ts"])
        _end = None
        if _t0 is not None:
            _limit = _t0 + _dt2.timedelta(minutes=getattr(a, "supersede_window", 120))
            for (_ln2, _ts2, _d2) in starts:
                if _ln2 <= r["start_line"]:
                    continue
                _t2 = _parse_ts(_ts2)
                if _t2 and _t2 > _limit:
                    _end = _ln2
                    break
        # 2026-09-18（修复其一）：闸门判定改用**链内执行数快照**（回收前）。
        # 血泪教训（我自己踩的）：先做完成行回收会抬高计数 ⇒ 满足 50% ⇒ **反而跳过**
        # 下面那条"头行/证据回收"，实测把 backup（只有 "backup ok" 证据、无头行）和
        # perception-bridge 一起漏掉，等于修一个报两个。故闸门只看链内原始计数。
        _inrun = len(r["executed"])
        if not (len(r["declared"]) >= 10 and _inrun >= len(r["declared"]) * 0.5):
            _win = [t for (ln, t) in (task_events + evidence_events)
                    if ln >= r["start_line"] and (_end is None or ln < _end)]
            _recovered = [t for t in r["declared"] if t in _win and t not in r["executed"]]
            if _recovered:
                r["executed"].extend(_recovered)
                r["superseded"] = True
        # 2026-09-18（修复其二）：**完成行回收**，不受 50% 闸门限制。
        # 理由：完成行是任务名唯一的强证据（一个名字只会完成一次），与头行证据不同质；
        # 而实测事故正是"executed=14/36 链内、头行被并发写吞掉 ⇒ 真跑过的任务被误报缺失"。
        _win_done = [t for (ln, t) in done_events
                     if ln >= r["start_line"] and (_end is None or ln < _end)]
        _rec_done = [t for t in r["declared"] if t in _win_done and t not in r["executed"]]
        if _rec_done:
            r["executed"].extend(_rec_done)
            r["superseded"] = True

    runs = runs[-a.last:]
    gaps = []
    for idx, r in enumerate(runs):
        missing = [t for t in r["declared"] if t not in r["executed"]]
        extra = [t for t in r["executed"] if t not in r["declared"]]
        r["missing"], r["extra"] = missing, extra
        # 2026-09-11（体检 669）：加「在跑豁免」避免误报。
        # 只出现 declared 而缺 executed、且当前无新日志跟进，可能是**正在跑**的链
        # （维护是串行长链，尾段任务尚未开始属正常）。仅当该链是"最后一次链"且
        # 距离现在超过 --inprogress-grace 分钟仍未收尾时，才判定为静默漏跑。
        # 2026-09-20（§918）：**并发链下的在跑豁免**。
        # 原实现只豁免 runs[-1]（"最后一次链"），于是本机常态的并发（多会话 + autostart 子链）
        # 会把仍在跑的长链挤下"最后一次"：实测 09:36:40 的 perception 长链在 09:44:31 被并发
        # 短链（consolidate-recent）顶位后，于 09:5x 被判 GAP（缺失 perception-recall），
        # 而它 09:54:52 正常 OK 收尾 ⇒ chain-reconcile 假红 + streak 被重置（G10 类）。
        # 新判据：**任何尚未打印收尾行、且启动时间在 grace 内**的运行都算在跑；
        # 已打印收尾行者立刻参与判定（不再被 grace 掩盖）；超 grace 未收尾者照旧判缺口。
        stale = True
        try:
            import datetime as _dt
            last_ts = _dt.datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
            stale = (_dt.datetime.now() - last_ts).total_seconds() >= a.inprogress_grace * 60
        except Exception as _e:
            swallow(__name__, _e)
        r["in_progress"] = (not r.get("finished")) and (not stale)
        # 719：dry-run 链按定义不执行任务 ⇒ 不构成缺口（但仍在列表里可见，便于核查）
        if r.get("dryrun"):
            r["missing"] = []
        if missing and not r["in_progress"] and not r.get("dryrun"):
            gaps.append(r)

    # ── 2026-09-15（R41-P23）：**给缺口加归因**（只加信息，**不改判定**）────────
    # 判定仍严格是 `gaps == 0`（见文件末尾）——绝不用归因让红项变绿（那是把真实
    # 后果藏起来）。这里只是把"这次缺口是主机没了、还是链自己漏跑"标注出来：
    # 实测 2026-09-15 的缺口根因是**主机 04:47 意外关机**（Event 6008，宕机至 08:57）
    # 把日链拦腰截断；而该结论当时是人工翻事件日志 + 逐行读维护日志约半小时才得出。
    # 只在**确有 gaps** 时才查事件日志（否则每 5 分钟白跑一次 Get-WinEvent）。
    if gaps:
        _host_ts = _host_lifecycle_ts()
        _wrap_ts = _wrapper_kill_ts()
        for _r in gaps:
            _r["cause"] = _attribute(_r["ts"], _r.get("last_ts"), _host_ts, _wrap_ts)

    if not a.quiet or gaps:
        print("=== 维护链审计（最近 %d 次）===" % len(runs))
        for r in runs:
            flag = "GAP " if (r["missing"] and not r.get("in_progress")) else (
                "RUN " if r.get("in_progress") else ("OK  " if r["finished"] == "OK" else "PART"))
            print("  %s %s  declared=%d executed=%d finished=%s%s%s" % (
                flag, r["ts"], len(r["declared"]), len(r["executed"]), r["finished"],
                ("  缺失: " + ",".join(r["missing"])) if r["missing"] else "",
                ("  [归因: %s]" % r["cause"]) if r.get("cause") else ""))
            if r["extra"]:
                print("        （额外执行: %s）" % ",".join(r["extra"]))
        if not gaps:
            print("  无静默漏跑")

    # ── ARTIFACT-CHECK（2026-09-11 审计 R2-3）：周报产物**被消费** ──────────
    # 背景：审计③ 发现维护链的 Invoke-Task **只用子进程 exit code 判成败，从不读
    # --out 产物** —— 于是「任务跑了、exit 0、但报告缺失/陈旧/损坏」这条静默失败
    # 路径无人发现。agent_flag_monitor 的阈值校准结果因此没有任何自动回灌。
    # 现按「产物必须存在且新鲜」做判定：缺失或超出该报告周期 2 倍即计为 STALE。
    _arts = [
        ("agent_flags_report.json", 8),         # flag-monitor 周任务
        ("ipi_report.json", 8),                 # ipi-check 周任务
        # 2026-09-14（682）：任务实写 **.jsonl**（ps1 的 --out 就是 .jsonl），原清单写 .json
        # ⇒ 每次都误报 MISSING（"尺子"错，不是任务错）。两种扩展名都接受。
        ("contradiction_resolutions.jsonl", 8),  # contradiction-resolve 周任务
        ("market_drill_report.json", 8),        # market-drill 周任务
        # 2026-09-14（682）：**删除**该项——全仓检索显示 agent_flags_stats.json
        # **没有任何生产者**（仅本清单引用它）；agent_flag_monitor.py 早已改为
        # "flags_per_run 比率门禁 + --strict-baseline 常量"，不再消费 stats 推导阈值
        # （见该文件 L216 注释）。继续期望一个不存在的产物 = 尺子错，不是任务错。
    ]
    import os as _os2, time as _t2
    _adir = STATE_DIR          # ⭐ 可注入（判据把它指向临时目录来压"未登记 ⇒ 仍红"的牙齿）

    # ── 2026-10-08（t111/G8 · D-14 案 (b)）：**已登记孤儿** vs **故障型陈旧** 分桶 ──────
    # 判定口径（与 G5/D-13 共享，逐字登记在
    # `docs/SILENT_FAILURE_BUDGETS.json::registered_orphans::_consumer_predicate`）：
    #   ORGAN_REGISTRY 的 `consumers` 与 `code_readers_sample` **均空**
    #   且 全仓检索该文件名**只命中三类**：生产者 / 审计清单 / 登记本身 ⇒ **无消费者**。
    # 语义：**无消费者的产物陈旧 ≠ 故障** ⇒ 报 `ORPHAN-STALE`（或 `ORPHAN-MISSING`）、
    #       **不计入 gaps**；**未登记的陈旧仍报 `STALE` 并计入 gaps** ⇒ **牙齿不变**
    #       （`test_maintenance_chain_evidence` / `test_chain_audit_concurrency` 照旧会红）。
    _orphan_names = _registered_orphan_names()
    stale, orphan_stale = [], []
    for _name, _days in _arts:
        _p = _os2.path.join(_adir, _name)
        if not _os2.path.exists(_p):
            _why = "MISSING"
        else:
            _age = (_t2.time() - _os2.path.getmtime(_p)) / 86400.0
            if _age <= _days * 2:
                continue
            _why = "STALE %.1f 天 > %d 天" % (_age, _days * 2)
        (orphan_stale if _name in _orphan_names else stale).append((_name, _why))
    if (not a.quiet) or stale or orphan_stale:
        print("  --- 产物新鲜度（周报被消费判定）---")
        if not stale and not orphan_stale:
            print("    全部产物存在且新鲜")
        for _n, _why in orphan_stale:
            print("    %-14s %-29s %s（已登记：无消费者 ⇒ 陈旧≠故障，**不计 gaps**）"
                  % ("ORPHAN-MISSING" if _why == "MISSING" else "ORPHAN-STALE", _n, _why))
        for _n, _why in stale:
            print("    STALE %-34s %s" % (_n, _why))
    gaps.extend([{"artifact": n, "why": w} for n, w in stale])

    # ── 2026-09-15（R41-P23）：**补跑计划输出**（供启动期实现"链可续跑"）。
    # 只输出**计划**、不执行任何任务。口径：按时间顺序列出所有「已判定漏跑且非在跑、
    # 非 dry-run」的运行及其缺失任务，交由调用方做 FIFO 排空（先补最早的欠账）。
    # 为什么必须是 FIFO：若固定取前 N 个，后面的永远轮不到——那只是把"饿死"
    # 从链尾搬到补跑清单里（同一类 bug 换个层级重演）。
    if getattr(a, "missing_json", False):
        import json as _json
        _owed = [{"ts": _r["ts"], "declared": len(_r["declared"]),
                  "missing": list(_r["missing"]), "cause": _r.get("cause")}
                 for _r in runs
                 if _r.get("missing") and not _r.get("in_progress") and not _r.get("dryrun")]
        print("MISSING-JSON: " + _json.dumps(
            {"owed": _owed, "runs": len(runs), "gaps": len(gaps)}, ensure_ascii=False))

    # 归因摘要并入末行：**新增 k=v 字段是向后兼容的**——loop_health 用
    # `dict(p.split("=") ...)` 按名取值，只读 runs/gaps，多出的字段不影响它；
    # 于是看护环的 note 能直接显示"缺口是不是主机没了"，无需再人工翻日志。
    _host_n = sum(1 for _r in gaps if str(_r.get("cause", "")).startswith("host-restart"))
    _wrap_n = sum(1 for _r in gaps if str(_r.get("cause", "")).startswith("wrapper-timeout"))
    _unexp_n = sum(1 for _r in gaps if _r.get("cause") == "unexplained")
    print("MAINT-AUDIT: runs=%d gaps=%d host_restart=%d wrapper_timeout=%d unexplained=%d" % (
        len(runs), len(gaps), _host_n, _wrap_n, _unexp_n))
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
