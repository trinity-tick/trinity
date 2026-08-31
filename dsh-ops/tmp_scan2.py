# -*- coding: utf-8 -*-
import os, re, glob, json, io

ROOT = r"D:/smartcos-wms/backend/internal"
out = {}
pkgs = []
for d in sorted(os.listdir(ROOT)):
    dp = os.path.join(ROOT, d)
    if not os.path.isdir(dp):
        continue
    go = glob.glob(dp + "/**/*.go", recursive=True)
    tests = [f for f in go if f.endswith("_test.go")]
    lines = 0
    for f in go:
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                lines += sum(1 for _ in fh)
        except Exception:
            pass
    pkgs.append({"pkg": d, "go": len(go), "tests": len(tests), "lines": lines})
out["pkg_total"] = len(pkgs)
out["tested_pkgs"] = len([p for p in pkgs if p["tests"] > 0])
out["zero_test_pkgs"] = [p["pkg"] for p in pkgs if p["tests"] == 0]

all_go = glob.glob(r"D:/smartcos-wms/backend/**/*.go", recursive=True)
sizes = []
todo = ignored = panics = 0
for f in all_go:
    try:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            c = fh.read()
            n = c.count("\n") + 1
    except Exception:
        continue
    sizes.append((n, f))
    if not f.endswith("_test.go"):
        todo += len(re.findall(r"TODO|FIXME|HACK", c))
        ignored += len(re.findall(r"_ = ", c))
        panics += len(re.findall(r"panic\(", c))
sizes.sort(reverse=True)
out["biggest"] = [os.path.relpath(f, r"D:/smartcos-wms/backend").replace("\\", "/") + " (" + str(n) + ")" for n, f in sizes[:5]]
out["debt"] = {"todo": todo, "ignored_err": ignored, "panics": panics}

wires = {}
for m in ["metrics", "twinsim", "promotionplan", "reconciliation"]:
    refs = 0
    for f in all_go:
        rel = f.replace("\\", "/")
        if "/internal/" + m + "/" in rel:
            continue
        try:
            c = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        if "smartcos/wms/internal/" + m in c:
            refs += 1
    wires[m] = refs
out["wire_refs"] = wires

io.open(r"D:/trinity-code/dsh-ops/scan_out.json", "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
print("DONE")
