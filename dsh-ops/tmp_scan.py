# -*- coding: utf-8 -*-
"""Trinity → SmartCos 自优化扫描（EXECUTION 418）"""
import os, re, glob, json
from collections import Counter

ROOT = r"D:\smartcos-wms\backend\internal"
out = {}

# 1) 包结构 + 测试覆盖
pkgs = []
for d in sorted(os.listdir(ROOT)):
    dp = os.path.join(ROOT, d)
    if not os.path.isdir(dp):
        continue
    go = glob.glob(os.path.join(dp, "**", "*.go"), recursive=True)
    tests = [f for f in go if f.endswith("_test.go")]
    lines = 0
    for f in go:
        try:
            lines += sum(1 for _ in open(f, encoding="utf-8", errors="ignore"))
        except Exception:
            pass
    pkgs.append({"pkg": d, "go": len(go), "tests": len(tests), "lines": lines})
out["pkg_total"] = len(pkgs)
out["zero_test_pkgs"] = [p["pkg"] for p in pkgs if p["tests"] == 0]
out["tested_pkgs"] = len([p for p in pkgs if p["tests"] > 0])
out["biggest_files"] = []

# 2) 最大文件（复杂度热点）
all_go = glob.glob(os.path.join(ROOT, "**", "*.go"), recursive=True)
sizes = []
for f in all_go:
    try:
        n = sum(1 for _ in open(f, encoding="utf-8", errors="ignore"))
        sizes.append((n, f))
    except Exception:
        pass
sizes.sort(reverse=True)
out["biggest_files"] = [f"{os.path.relpath(f, ROOT)} ({n} 行)" for n, f in sizes[:5]]

# 3) 技术债扫描（全 backend）
be = r"D:\smartcos-wms\backend"
todo = ignored_err = panics = 0
for f in all_go:
    if f.endswith("_test.go"):
        continue
    try:
        c = open(f, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    todo += len(re.findall(r"TODO|FIXME|HACK", c))
    ignored_err += len(re.findall(r"_ = ", c))
    panics += len(re.findall(r"panic(", c))
out["tech_debt"] = {"todo_fixme": todo, "ignored_errors": ignored_err, "panics_non_test": panics}

# 4) 接线缺口（新模块被谁引用）
new_mods = ["metrics", "twinsim", "promotionplan", "reconciliation"]
wires = {}
for m in new_mods:
    refs = 0
    for f in all_go:
        if "ai-slotting" in f or "metrics" + "\\" in f or f.replace("\\", "/").endswith(m + "/" + m + "_test.go"):
            pass
        try:
            c = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        if re.search(r"smartcos/wms/internal/" + m + r"", f.replace("\\", "/")):
            refs += 1
    wires[m] = refs
out["wire_gaps"] = wires

print(json.dumps(out, ensure_ascii=False, indent=1))
