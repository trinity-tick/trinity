# -*- coding: utf-8 -*-
"""audit_maintenance_ps1.py — 维护链 ps1 完整性巡检（2026-08-27）。

检查 trinity-dsh-maintenance.ps1 的任务三件套齐全性：
  allowed 列表 / Cmd 定义（$xxxCmd = @） / dispatch 行（Invoke-Task -Name "x"）
找出缺失项（定义丢失/无 dispatch），输出修复清单。

用法:
    python scripts/audit_maintenance_ps1.py [--fix]
"""
import io
import re
import sys
import os

PS1 = r"C:\Users\Administrator\trinity\dsh-ops\trinity-dsh-maintenance.ps1"


def main() -> int:
    raw = io.open(PS1, "r", encoding="utf-8-sig").read()
    # allowed 列表
    m = re.search(r'\$allowed = @\((.*?)\)', raw, re.S)
    allowed = [t.strip().strip('"') for t in m.group(1).split(",") if t.strip()] if m else []
    # dispatch 任务名
    dispatched = set(re.findall(r'Invoke-Task -Name "([a-z0-9-]+)"', raw))
    # 定义（$xxxCmd = @"；变量名可含数字如 auditPs1）
    defined = set(re.findall(r'\$([a-zA-Z0-9]+)Cmd = @"', raw))

    problems = []
    for t in allowed:
        if t == "all":
            continue
        if t not in dispatched:
            problems.append(f"[{t}] allowed 有但 dispatch 缺失")
    for t in dispatched:
        # 任务名 -> 变量名（如 rollout-audit -> rolloutAuditCmd；首词小写）
        parts = t.split("-")
        var = parts[0].lower() + "".join(p.capitalize() for p in parts[1:]) + "Cmd"
        _ALIAS = {"evolution": "evo", "session-summarize": "sessionSummary"}  # 特殊变量名
        base = var[:-3]
        if _ALIAS.get(t):
            base = _ALIAS[t]
        if base not in defined:  # defined 存的是去 Cmd 的名字
            problems.append(f"[{t}] dispatch 引用 $ {var} 但定义缺失")
    # allowed 之外但有 dispatch 的
    for t in dispatched:
        if t not in allowed and t != "all":
            problems.append(f"[{t}] dispatch 存在但不在 allowed")

    print(f"allowed: {len(allowed)} | dispatched: {len(dispatched)} | defined cmds: {len(defined)}")
    if problems:
        for p in problems:
            print("  !!", p)
        print(f"PROBLEMS: {len(problems)}")
        return 1
    print("ALL OK — 三件套齐全")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
