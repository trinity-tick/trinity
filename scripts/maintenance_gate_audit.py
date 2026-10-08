#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""maintenance_gate_audit.py —— **一次即闸**（once-due gate）失效检测（t99/D1）

## 这个脚本要防的 bug 类（2026-10-07 实测真实发生过）

维护链的"一次性"任务用 `once-<gate>-<key>.mark` 做**周期闸**（`Test-OnceDue`）：

```powershell
function Get-WeekKey { return (Get-Date).ToString("yyyy-'W'ww") }   # ← 这里
Test-OnceDue 'weekly' $wkKey     ⇒  return -not (Test-Path "once-weekly-$wkKey.mark")
```

⚠️ 实测（Windows PowerShell **5.1** / .NET Framework）：`ToString("ww")` **不展开**，原样返回字面量 `ww`
⇒ `Get-WeekKey` 对**任何**日期都返回 **`2026-Www`** ⇒ `once-weekly-2026-Www.mark` **一年只可能被写一次**
⇒ **闸门在首次运行后对当年永久关闭**，此后每周一都**静默跳过**。

现场后果：`once-weekly-2026-Www.mark`（2026-09-21 04:30:27）、`once-weekly-acc-…`（09-20）、
`once-weekly-check-…`（09-21）⇒ **周一质量门禁链 / 周日 answer-eval / 周一 weekly-check 三条周级链全部停摆**，
其中 4 份周报产物（`agent_flags_report.json` / `ipi_report.json` / `contradiction_resolutions.jsonl` /
`market_drill_report.json`）**冻结在 2026-09-21 03:53–03:54**（本次实测 16.77 天）。

## 本脚本的两条判据（各自可失败）

  · **DEGENERATE-KEY**：mark 的 key 是**纯字母**（如 `Www`）⇒ 它**不可能随周期变化** ⇒ 闸门必然一次即关。
    这是**该 bug 类的充分信号**（健康的 key 一定含数字：`20260915` / `2026-W39`）。
  · **GATE-STALL**：某个闸的最新 mark 龄 > **2×该闸周期**（闸名含 `weekly` ⇒ 7 天，否则 1 天）。

退出码：0 = 无异常；**1 = 有 DEGE NERATE-KEY 或 GATE-STALL（响亮）**；2 = 取不到日志目录（fail-closed）。

只读；不写任何状态。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

LOG_DIR = os.path.expanduser(r"~\.trinity\logs")
RE_MARK = re.compile(r"^once-(?P<stem>.+)\.mark$")


def parse_marks(log_dir: str) -> list:
    out = []
    for name in sorted(os.listdir(log_dir)):
        m = RE_MARK.match(name)
        if not m:
            continue
        stem = m.group("stem")
        gate, _, key = stem.rpartition("-")
        if not gate:
            gate, key = stem, ""
        path = os.path.join(log_dir, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        out.append({"file": name, "stem": stem, "gate": gate, "key": key, "mtime": mtime,
                    "age_days": (time.time() - mtime) / 86400.0})
    return out


def period_days(gate: str) -> int:
    return 7 if "weekly" in gate else 1


def gate_family(stem: str) -> str:
    """把 `once-<stem>` 的 stem 归到**闸族**：去掉尾部 key，再去掉尾部的 4 位年份。

    `screen-20260915` → `screen`；`weekly-2026-Www` → `weekly`；`weekly-check-2026-Www` → `weekly-check`。

    ⚠️ **第一版我漏了这一步**：把**每一个历史 mark** 都当独立闸判龄 ⇒ 日闸的历史 key 全部被判停摆
    （实测 27 条告警，其中 14 条是"昨天/前天的日闸"）⇒ **过宽 ⇒ 恒红**，正是本项目一路在抓的形态。
    正确的口径是：**同一闸族只看最新那条 mark**（历史 key 是"跑过的证据"，不是停摆）。
    """
    rest, _, _key = stem.rpartition("-")
    if not rest:
        return stem
    if re.fullmatch(r"\d{4}", rest.rsplit("-", 1)[-1]):
        rest = rest.rsplit("-", 1)[0]
    return rest or stem


def findings(marks: list) -> list:
    bad = []
    fams = {}
    for mk in marks:
        fam = gate_family(mk["stem"])
        cur = fams.get(fam)
        if cur is None or mk["mtime"] > cur["mtime"]:
            fams[fam] = dict(mk, family=fam)

    for fam, mk in sorted(fams.items()):
        # ① 退化 key：纯字母 ⇒ 不可能随周期变化（该 bug 类的充分信号）
        if mk["key"] and re.fullmatch(r"[A-Za-z]+", mk["key"]):
            bad.append({"kind": "DEGENERATE-KEY", "gate": fam, "key": mk["key"],
                        "file": mk["file"], "age_days": round(mk["age_days"], 2),
                        "why": "最新 mark 的 key 是纯字母（如 Www）⇒ 它不可能随周期变化 ⇒ "
                               "闸门首次运行后对本期永久关闭（静默跳过）"})
        # ② 停摆：闸族最新 mark 的龄 > 2×周期
        p = period_days(fam)
        if mk["age_days"] > 2 * p:
            bad.append({"kind": "GATE-STALL", "gate": fam, "key": mk["key"],
                        "file": mk["file"], "age_days": round(mk["age_days"], 2),
                        "why": "该闸族最新 mark 龄 %.1f 天 > 2×周期 %d 天 ⇒ 已停摆"
                               % (mk["age_days"], p)})
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", default=LOG_DIR)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.log_dir):
        print("GATE-AUDIT UNTESTABLE: 日志目录不存在 %s —— fail-closed" % a.log_dir)
        return 2
    marks = parse_marks(a.log_dir)
    bad = findings(marks)
    if a.json:
        import json
        print(json.dumps({"log_dir": a.log_dir, "marks": len(marks),
                          "findings": bad}, ensure_ascii=False, indent=1))
    else:
        print("GATE-AUDIT: once-due marks=%d findings=%d" % (len(marks), len(bad)))
        for f in bad:
            print("  %-14s %-34s key=%-10s age=%.2f 天  %s"
                  % (f["kind"], f["gate"], f["key"], f["age_days"], f["why"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
