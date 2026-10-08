#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mechanism_adjudication.py — 跑而不产出的机制逐项裁定（CLOSED_LOOP_STATUS §4 P0，2026-09-14）

背景（实测）：82 个机制状态文件里 27 个陈旧且无内容消费者。§4 P0 的要求是
**逐项给出输入从哪来**，无输入就退役 —— 而不是笼统地一起埋掉。

本脚本为每个候选项抽取四条可复核证据：
  1. writer：哪个模块写这个状态文件，并给出该模块的公开 API；
  2. scheduled：该模块是否在维护链/日链/脑区驱动里被调度；
  3. input_signals：写侧是否真的读了外部输入（PG/检索/记忆/其它状态），还是纯自重写；
  4. verdict：wireable（有输入、缺消费者）/ retire（无输入，纯摆设）。

只读：不改调度、不删代码、不删数据。产出
  ~/.trinity/state/mechanism_adjudication.json 与 docs/MECHANISM_ADJUDICATION_20260914.md
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.expanduser("~/.trinity/state")
OUT_JSON = os.path.join(STATE, "mechanism_adjudication.json")
OUT_MD = os.path.join(ROOT, "docs", "MECHANISM_ADJUDICATION_20260914.md")

SCAN_DIRS = ("trinity", "scripts")
INPUT_HINTS = ("pg_connect", "SELECT ", "search_memories", "search_hybrid", "ingest(",
               ".ingest", "load_memories", "self._adapter", "adapter.", "client.",
               "json.load", "read_text", "get_state", "_load_state")
SELF_ONLY = ("json.load", "get_state", "_load_state", "read_text")

#: **写调用**的函数/属性名。判据是 AST 调用节点，不是文本（见 `_write_calls`）。
_WRITE_ATTRS = frozenset({"dump", "write_text", "write_bytes", "save_state", "_save_state"})
_OPEN_WRITE_MODES = frozenset("wax+")     # open(..., "w"/"a"/"x"/"r+") 之类


def _dotted(node):
    """把 `a.b.c` 还原成点号串；不是纯属性链就返回 None。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _write_calls(txt):
    """该模块**真的调用了**哪些写函数 —— **AST 口径**（注释与文档字符串不参与）。

    2026-10-06（T1 测试归因轮 · 队长裁定 **R-1**）：

    ## 原实现为什么是假信号
    原代码是 `WRITE_RX = re.compile(r"json[.]dump|write_text|_save_state|save_state")`
    然后 `WRITE_RX.search(整个文件文本)` —— 于是**注释/文档字符串里写出这几个词**
    的模块会被判成"有写入能力"。实测形态（同一天第三处同型）：
    `docs/GATE_WIRING.json::_method.write_path_detection` 记的也是"源码正则"口径，
    而它决定"哪个门禁脚本能进 CI" ⇒ **注释能改变一个门禁的接线判定**。

    ## 现在
    只看 `ast.Call` 节点的函数名/属性名（`json.dump` / `*.dump` / `write_text` /
    `write_bytes` / `save_state` / `_save_state` / `open(..., "w"|"a"|"x"|"+")`）。
    注释与 docstring 在 AST 里**不产生 Call 节点** ⇒ 天然骗不到它。
    编译不过的文件返回 `[]`（语法错由 `scripts/script_compile_gate.py` 管，本函数不猜）。
    """
    try:
        tree = ast.parse(txt)
    except SyntaxError:
        return []
    hits = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name in _WRITE_ATTRS:
            hits.add(_dotted(func) or name)
        elif name == "open":
            for arg in list(node.args[1:]) + [k.value for k in node.keywords]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and any(c in arg.value for c in _OPEN_WRITE_MODES):
                    hits.add("open(mode=%r)" % arg.value)
                    break
    return sorted(hits)



def _py_files():
    for d in SCAN_DIRS:
        for root, dirs, files in os.walk(os.path.join(ROOT, d)):
            if "__pycache__" in root or ".venv" in root:
                continue
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(root, f)


def _owns(txt, name):
    """该模块是否拥有这个状态文件（而不是仅仅列出/读取它）。

    2026-09-14（681）精度修正：上一版把「文件里出现过该文件名 + 任意 json.dump」
    当作写侧 ⇒ 把 trinity/brain/brain_metrics.py（它只是在一行元组里列出 9 个
    脑区状态文件名做新鲜度体检）误判成这 9 个状态文件的写者（抽查 1 项即暴露）。
    现要求文件名出现在一个赋值/路径构造行上（X = ...expanduser(...name)、
    os.path.join(..., name) 等），即"这个模块声明了自己写哪个文件"。
    """
    for line in txt.splitlines():
        if name not in line:
            continue
        s = line.lstrip()
        if s.startswith("#"):
            continue
        if ("=" in line or "expanduser" in line or "os.path.join" in line
                or "Path(" in line):
            return True
    return False


def _writers(state_file):
    hits = []
    stem = state_file.replace(".json", "")
    for p in _py_files():
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        if state_file not in txt and stem not in txt:
            continue
        writes = _write_calls(txt)          # AST 口径（R-1）；空表示真的没有写调用
        if not writes:
            continue
        # 拥有该文件，或模块名与状态文件名同名（本仓主流约定）
        if not (_owns(txt, state_file) or os.path.basename(p)[:-3] == stem):
            continue
        rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
        api = re.findall(r"(?m)^(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", txt)[:8]
        sig = sorted({h.strip() for h in INPUT_HINTS if h in txt})
        hits.append({"file": rel, "api": api, "input_signals": sig,
                     "write_calls": writes})     # R-1：把 AST 证据一起落进裁定产物
    return hits


def _sched_strict() -> bool:
    """R40：严格判据（**默认 on**，R40 翻转）。

    翻转依据（实测，见 EXECUTION 740）：
      · 旧判据（宽松）在 contagion/sel/txn 上产出的"调度证据"是
        `scripts/mechanism_consumer_audit.py` —— **审计脚本自己的别名表**；
        而这 4 个机制的行为证据是 14 天未驱动、31 天日志 0 条、真调度器 0 次提及。
      · 函数级 A/B：7 个对照组按**行为真值**正确翻转（4 个未驱动 → 无证据；
        3 个在驱动的静态证据从"监测清单"改指向真驱动脚本）。
      · 可复现 band 内裁定改变 **0 项**（`retire` 由"无外部输入"决定，与 driven 无关）
        ⇒ **无回归面**。

    回滚：`TRINITY_SCHED_STRICT=off`（回到旧判据，逐字保留在 `_scheduled_evidence`
    的宽松分支里，并产出旧读数以便对照）。
    """
    return str(os.environ.get("TRINITY_SCHED_STRICT", "on")).strip().lower() in (
        "on", "1", "true", "yes")


# ── R40（2026-09-14）：调度判据修正 ────────────────────────────────────────
#
# 缺陷（实测，见 EXECUTION 740）：上一版 `_scheduled()` 在 `dsh-ops/` 与 `scripts/`
# 下做**纯子串匹配** —— 于是"任何文件里提到过这个状态文件名"都算被调度。实测后果：
#
#   contagion_state / sel_state / txn_state 的"命中"全部来自
#     scripts/mechanism_consumer_audit.py 的**别名表**、
#     scripts/brain_state_digest.py 的**监测清单**、
#     scripts/brain_mechanisms_status.py 的**诊断调用清单**
#   —— 全是"读它 / 列它 / 提及它"，**没有一个是调度**。
#
# 而行为证据完全相反：
#   · 真调度器 trinity-dsh-maintenance.ps1 里这 4 个名字出现 **0 次**；
#   · 31.1 天维护日志里 **0 条**记录（同族任务 forgetting 跑了 49 次、decay 1296 次）；
#   · 4 个状态文件 **14 天**未更新。
#   ⇒ 3 个机制被从 retire 里挑出来标为 wireable，实际与那 23 个一样从未被调度。
#
# 同族旧账：`_owns()` 在 681 轮修过**完全相同**的 bug 类（"文件名式提及"被当作实质证据，
# 当时把 brain_metrics.py 误判成 9 个状态文件的写者）——**只修了写侧，调度侧留在原地**。
#
# 修正后的判据（两跳）：
#   ① 调度器**真正 invoke** 的脚本（`runpy.run_path(...)` 的 basename 集合）；
#   ② 该脚本内容里出现 hint。
# 跳①是关键的过滤器：别名表 / 监测清单 / 诊断清单**都不被调度器 invoke**，
# 因此自动出局——不需要维护一份易腐的"自指文件黑名单"。

SCHEDULER_FILES = ("dsh-ops/trinity-dsh-maintenance.ps1",)
_RUNPY_RX = re.compile(r'runpy\.run_path\(\s*r?["\']([^"\']+\.py)["\']')
_invoked_cache = None


def _invoked_script_names():
    """调度器 invoke 的脚本 basename 集合（缓存）。"""
    global _invoked_cache
    if _invoked_cache is not None:
        return _invoked_cache
    names = set()
    for rel in SCHEDULER_FILES:
        p = os.path.join(ROOT, rel)
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for m in _RUNPY_RX.finditer(txt):
            raw = m.group(1).replace("\\\\", "\\")
            base = raw.replace("/", os.sep).split(os.sep)[-1]
            if base.endswith(".py"):
                names.add(base)
    _invoked_cache = names
    return names


def _fresh_days() -> float:
    """「被驱动」的新鲜度阈值（天）。

    **默认 8 天，取自实测分布的最大间隙**（EXECUTION 743 敏感性分析，82 项全量）：

        0.00 – 0.90 天   49 项   ← 活跃驱动
                ↓ 间隙 2.1
        3.00 – 6.10 天    5 项   ← 低频驱动（几天一次）
                ↓ **间隙 4.9 ← 最大**
        11.00 – 14.20 天 28 项   ← 休眠

    743 之前的默认是 **2 天**，它切在 0.9→3.0 那个 2.1 天的小间隙里，
    把 `calibration_map`(3.0d) / `video_state`(3.9d) / `social_trust`(4.8d) 三项
    **低频但确实在驱动**的机制误判为「未驱动」（742.6 边界②"未做敏感性分析"的具体暴露）。
    阈值落在最大间隙后，翻转集恰好收敛为那 4 个 14 天休眠项。
    """
    try:
        return float(os.environ.get("TRINITY_SCHED_FRESH_DAYS", "8") or 8)
    except Exception:
        return 8.0


def _driven_fresh(age_days):
    """行为证据：状态文件近期被真正写过 ⇒ 有东西在驱动这个机制。

    R40 为什么最终用**行为**而不是静态判据（两版静态都实测失败）：

      · 宽松版（纯子串）：contagion/sel/txn 的"命中"来自别名表/监测清单 ⇒ **假阳性**；
      · 严格版（调度器 invoke 的脚本 + 拥有该文件）：把 global_workspace /
        calibration_state / metamemory_state 误判为**未驱动** —— 它们的文件由
        `trinity/brain/<模块>.py` 自写，而调度器 invoke 的是 scripts/brain_heartbeat.py，
        后者只是**调用**该模块 ⇒ **假阴性**。

    实测（2026-09-14）行为证据呈**双峰、无灰区**：

        未驱动：contagion 14.14d · sel 14.09d · txn 14.08d · memory_traces 14.00d
        在驱动：global_workspace / calibration_state / metamemory_state /
                predictive_state 均 **0.04d**；self_markers 0.44d；consolidate 0.13d

    ⇒ 静态证据仍然产出并上报（`scheduled_in` / `scheduled_evidence`，供人判读），
      但**不再单独决定裁定**。
    """
    if age_days is None:
        return False
    try:
        return float(age_days) <= _fresh_days()
    except Exception:
        return False


def _scheduled_evidence(state_file):
    """返回 (命中文件相对路径列表, 证据行列表)。

    严格模式的两道门（缺一不可）：
      ① **被调度器 invoke** 的脚本（跳①滤掉别名表/监测清单/诊断清单——它们从不被 invoke）；
      ② 该脚本**拥有**这个状态文件（复用 681 轮为写侧修好的 `_owns()` 判据）。
    跳② 滤掉「只读体检」型命中：`brain_mechanisms_status.py` 确实被 brain-status 任务
    invoke，也确实**调用**了这 59 个机制的 `*_report()`，但它只是在一行映射里列出它们
    （`"trinity.brain.memory_traces": "trace_report",`）——`_owns()` 对 `=`/`Path(`/
    `os.path.join`/`expanduser` 的要求正好把它排除。**被调用 ≠ 被驱动。**
    """
    files, ev = [], []
    stem = state_file.replace(".json", "")
    if not stem:
        return files, ev

    if _sched_strict():
        for name in sorted(_invoked_script_names()):
            if name == stem + ".py":
                continue
            p = os.path.join(ROOT, "scripts", name)
            if not os.path.exists(p):
                continue
            try:
                txt = open(p, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            if stem not in txt and state_file not in txt:
                continue
            # 门②：必须"拥有"该状态文件（声明写哪个文件），而非仅列出/提及
            if not (_owns(txt, state_file) or name[:-3] == stem):
                continue
            rel = "scripts/" + name
            files.append(rel)
            for i, line in enumerate(txt.splitlines(), 1):
                if (stem in line or state_file in line) and not line.lstrip().startswith("#"):
                    ev.append("%s:%d: %s" % (rel, i, line.strip()[:100]))
                    break
        return files[:4], ev[:4]

    # 旧行为（默认，逐字保留以便回滚对照）
    for d in ("dsh-ops", "scripts"):
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for root, _dirs, fs in os.walk(base):
            for f in fs:
                if not f.endswith((".ps1", ".py", ".js")):
                    continue
                if f == stem + ".py":
                    continue
                p = os.path.join(root, f)
                try:
                    txt = open(p, encoding="utf-8", errors="replace").read()
                except Exception:
                    continue
                if stem in txt:
                    rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
                    files.append(rel)
                    for i, line in enumerate(txt.splitlines(), 1):
                        if stem in line:
                            ev.append("%s:%d: %s" % (rel, i, line.strip()[:100]))
                            break
    return files[:4], ev[:4]


def _scheduled(hint):
    """兼容入口：只返回命中文件列表（JSON schema 保持稳定）。"""
    return _scheduled_evidence(hint if hint.endswith(".json") else hint + ".json")[0]


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-json", default=OUT_JSON)
    ap.add_argument("--out-md", default=OUT_MD)
    ap.add_argument("--band", default="retire",
                    help="裁定范围：retire（默认，陈旧且无消费者）/ existence_only（有产物但只被 exists() 提及）"
                         " / dormant_observed（状态被写、只被 digest 观测，无决策消费者）"
                         " / ok / idle_by_design / all")
    a = ap.parse_args(argv)

    mc = os.path.join(STATE, "mechanism_consumers.json")
    data = json.load(open(mc, encoding="utf-8"))
    # R40 补全（EXECUTION 743）：上一版只有 retire / existence_only，
    # 而 `dormant_observed`（28 项 —— 含 742 里被误标为 wireable 的那 4 项）
    # **无法被任何 band 选中** ⇒ 742 的修复拿不到端到端 A/B 证据。
    # 现补齐全部五个 verdict，`all` 亦含之。
    _bands = {"retire": ("retire",),
              "existence_only": ("existence_only",),
              "dormant_observed": ("dormant_observed",),
              "ok": ("ok",),
              "idle_by_design": ("idle_by_design",),
              "all": ("retire", "existence_only", "dormant_observed",
                      "ok", "idle_by_design")}
    _want = _bands.get(a.band, ("retire",))
    cands = [r for r in (data.get("rows") or [])
             if str(r.get("verdict", "")) in _want]

    rows = []
    for c in sorted(cands, key=lambda x: str(x.get("state_file"))):
        sf = str(c.get("state_file"))
        writers = _writers(sf)
        # R40：调度证据带上**命中行**，让假阳性可见（旧版只给文件名，无法审计）
        sched, sched_ev = _scheduled_evidence(sf)
        sig = sorted({s for w in writers for s in w.get("input_signals", [])})
        ext = [s for s in sig if s not in SELF_ONLY]
        # R40：严格模式下「被驱动」以**行为证据**为准（见 _driven_fresh 的实测依据）
        fresh = _driven_fresh(c.get("age_days"))
        if _sched_strict():
            driven = fresh
            driven_ev = ("state_file_fresh:%.2fd" % float(c["age_days"])) if (
                fresh and c.get("age_days") is not None) else ""
        else:
            driven = bool(sched)
            driven_ev = ""
        if ext and driven:
            verdict = "wireable"
        elif ext:
            verdict = "wireable_unscheduled"
        else:
            verdict = "retire"
        rows.append({"state_file": sf, "age_days": c.get("age_days"),
                     "writers": writers, "scheduled_in": sched,
                     "scheduled_evidence": sched_ev,
                     "driven": driven, "driven_evidence": driven_ev,
                     "input_signals": ext,
                     "self_only_signals": [s for s in sig if s in SELF_ONLY],
                     "verdict": verdict})

    res = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "band": a.band,
           "sched_strict": _sched_strict(),
           "criterion": ("wireable = 写侧读得到外部输入且被调度（缺的只是消费者）；"
                         "retire = 既无外部输入也无消费者"),
           "sched_criterion": ("R40 严格=**行为证据**（状态文件在 %.1f 天内被写过 ⇒ 有东西在驱动它），"
                               "静态调度证据仅上报不决定裁定；"
                               "宽松=文件名在 dsh-ops/ 或 scripts/ 里出现过（含自指，已证假阳性）"
                               % _fresh_days()),
           "total": len(rows),
           "by_verdict": {v: sum(1 for r in rows if r["verdict"] == v)
                          for v in ("wireable", "wireable_unscheduled", "retire")},
           "rows": rows,
           "note": "只读裁定；不删代码、不改调度。退役=登记；接线=另立轮次（先造消费者）"}
    os.makedirs(os.path.dirname(a.out_json), exist_ok=True)
    with open(a.out_json, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

    md = ["# 跑而不产出的机制逐项裁定（2026-09-14）", "",
          "> 承接 docs/CLOSED_LOOP_STATUS_20260913.md §4 P0：逐项给出输入从哪来，无输入就退役。",
          "> 判据：wireable = 写侧读得到外部输入且被调度（缺的只是消费者）；retire = 既无外部输入也无消费者。",
          "> 本文件由 scripts/mechanism_adjudication.py 生成（只读，不改调度/代码/数据）。", "",
          "| 状态文件 | 年龄(d) | 写侧模块 | 有调度 | 外部输入信号 | 裁定 |",
          "|---|---|---|---|---|---|"]
    for r in rows:
        w = ", ".join(x["file"] for x in r["writers"][:2]) or "-"
        md.append("| %s | %s | %s | %s | %s | **%s** |"
                  % (r["state_file"], r["age_days"], w,
                     "是" if r["scheduled_in"] else "否",
                     ", ".join(r["input_signals"]) or "（无）", r["verdict"]))
    # R40：把调度证据的行号级明细单列一节（表格只放"是/否"，读者无法判断真假）
    ev_rows = [r for r in rows if r.get("scheduled_evidence")]
    md += ["", "## 调度证据明细（R40）", "",
           "> 判据：%s" % ("严格 —— 只在**被调度器 invoke 的脚本**里出现才算数"
                          if _sched_strict() else
                          "宽松（旧版）—— 文件名在 dsh-ops/ 或 scripts/ 里出现过即算，**含自指**"),
           ""]
    if ev_rows:
        md += ["| 状态文件 | 命中文件:行 | 命中内容 |", "|---|---|---|"]
        for r in ev_rows:
            for e in r["scheduled_evidence"]:
                parts = e.split(": ", 1)
                loc = parts[0]
                txt = (parts[1] if len(parts) > 1 else "")[:90].replace("|", "\\|")
                md.append("| %s | %s | `%s` |" % (r["state_file"], loc, txt))
    else:
        md.append("_无任何行命中调度证据。_")
    md += ["", "## 计数", "", "```", json.dumps(res["by_verdict"], ensure_ascii=False), "```", ""]
    with open(a.out_md, "w", encoding="utf-8") as f:
        f.write(chr(10).join(md))
    print("adjudication: total=%d %s" % (res["total"], json.dumps(res["by_verdict"], ensure_ascii=False)))
    for r in rows[:30]:
        print("  %-30s %-6s %-20s %s" % (r["state_file"], r["age_days"], r["verdict"],
                                         ",".join(x["file"] for x in r["writers"][:1]) or "-"))
    print("out -> " + a.out_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())