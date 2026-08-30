# -*- coding: utf-8 -*-
"""模块分级扫描（EXECUTION 108）：按 core 引用数分类 trinity 包下模块。
用法: python scripts/module_classify.py [--json]
输出: 分级表（core/reserve/frozen），与 docs/ARCHITECTURE.md 第 2 节对齐。
"""
import os, re, json, sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trinity")
SKIP_DIRS = {"__pycache__", ".git", "sdk", "data", "output", "docs"}
CORE_PKGS = {"core", "api", "mcp", "adapters", "retrieval", "vector_index", "embeddings",
             "security", "telemetry", "audit", "memory", "agents", "evolution", "identity",
             "governance", "collector", "market", "structure_store", "session_recorder",
             "engine_worker", "brain", "cognition", "kgraph", "automation", "llm"}

def walk_py(d):
    out = []
    for dp, dn, fn in os.walk(d):
        dn[:] = [x for x in dn if x not in SKIP_DIRS]
        for f in fn:
            if f.endswith(".py") and not f.startswith("__"):
                out.append(os.path.join(dp, f))
    return out

def modkey(p):
    rel = os.path.relpath(p, ROOT).replace("\\", "/")
    parts = rel.split("/")
    return parts[0] if len(parts) > 1 else parts[0].replace(".py", "")

def main():
    all_files = walk_py(ROOT)
    imports = {}
    for p in all_files:
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        src_mod = rel.split("/")[0]
        try:
            text = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for m in re.finditer(r"(?:from|import)\s+trinity\.([a-zA-Z_][a-zA-Z0-9_]*)", text):
            tgt = m.group(1)
            is_core = src_mod in CORE_PKGS or src_mod in {x.replace(".py", "") for x in CORE_PKGS}
            key = (tgt, "core" if is_core else "other")
            imports[key] = imports.get(key, 0) + 1
    agg = {}
    for (tgt, kind), n in imports.items():
        d = agg.setdefault(tgt, {"core_refs": 0, "other_refs": 0, "total": 0})
        d["core_refs" if kind == "core" else "other_refs"] += n
        d["total"] += n
    mod_files = {}
    for p in all_files:
        k = modkey(p)
        mod_files[k] = mod_files.get(k, 0) + 1
    rows = []
    for mod, cnt in mod_files.items():
        ref = agg.get(mod, {"core_refs": 0, "other_refs": 0, "total": 0})
        cl = "core" if ref["core_refs"] >= 3 else ("reserve" if ref["total"] > 0 else "frozen")
        rows.append({"module": mod, "files": cnt, "core_refs": ref["core_refs"], "total_refs": ref["total"], "class": cl})
    rows.sort(key=lambda r: (-r["core_refs"], -r["files"]))
    if "--json" in sys.argv:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        print(f"{'module':28s} {'files':>5s} {'core_refs':>9s} {'total_refs':>10s}  class")
        for r in rows:
            print(f"{r['module']:28s} {r['files']:5d} {r['core_refs']:9d} {r['total_refs']:10d}  {r['class']}")

if __name__ == "__main__":
    main()
