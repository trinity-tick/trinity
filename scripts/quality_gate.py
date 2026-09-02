#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""质量门禁（2026-09-01，自查短板 #1 制度化）——检索质量 + 延迟 + 一致性 单命令门禁。

在 500q LongMemEval-style mock 集上评测（无 LLM，纯检索，~2-4 分钟）：
  1. R@5（keyword 与 hybrid 各跑一遍，按类目 KU/MS/SS-A/SS-P/SS-U/TR 分解）
  2. 检索延迟 p50/p95
  3. PG/SQLite 对账摘要（drift 面）
输出：~/.trinity/bench-results/quality-gate-<ts>.json + .md；退出码按阈值。

用法: python scripts/quality_gate.py [--dataset PATH] [--fail-r5 0.55] [--fail-hybrid-gap 0.02]
"""
import argparse
import datetime
import glob
import json
import os
import sys
import tempfile
import time


def _find_dataset():
    candidates = [
        r"C:\Users\Administrator\.marvis\workspace\conv_19f49996244_37d75ffae4a6\benchmark\longmemeval_mock_dataset.json",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    hits = glob.glob(r"C:\Users\Administrator\.marvis\workspace\**\longmemeval_mock_dataset.json", recursive=True)
    return hits[0] if hits else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="")
    ap.add_argument("--fail-r5", type=float, default=0.55)
    ap.add_argument("--fail-hybrid-gap", type=float, default=0.02)
    ap.add_argument("--ablate", action="store_true", help="100q 子集通道归因对比")
    args = ap.parse_args()

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, ROOT)
    os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

    dset = args.dataset or _find_dataset()
    if not dset:
        print("QUALITY-GATE: dataset not found (longmemeval_mock_dataset.json) — SKIP (exit 0)")
        return 0
    data = json.load(open(dset, encoding="utf-8"))
    questions = data.get("questions") or data
    print("QUALITY-GATE: dataset=%s questions=%d" % (os.path.basename(dset), len(questions)))

    from trinity import Trinity
    store = tempfile.mkdtemp(prefix="qgate_")
    mem = Trinity(adapter="sqlite", store_path=store)
    for q in questions:
        for fact in q.get("context_facts", []):
            ftext = (fact or {}).get("fact", "")
            if not ftext:
                continue
            try:
                mem.ingest(ftext, persona_id=q.get("persona_name") or "default",
                           session_id=str((fact or {}).get("session_id") or q.get("session_id") or "0"),
                           category=q.get("category", "general"), tags=["qgate", q.get("category", "")])
            except Exception:
                pass

    def eval_r5(mode="hybrid", k=5, use_all_channels=True, via_hybrid=False, limit=None):
        hits = n = 0
        cat = {}
        lat = []
        qs = questions[:limit] if limit else questions
        for q in qs:
            c = q.get("category", "?")
            question = q.get("question", "")
            facts = [f.get("fact", "").strip() for f in q.get("context_facts", []) if f and f.get("fact")]
            if not facts or not question:
                continue
            t0 = time.time()
            if via_hybrid:
                res = mem.search_hybrid(query=question, top_k=k, strategy="rrf",
                                        persona_id=q.get("persona_name") or None).get("results", [])
            else:
                res = mem.search(query=question, mode=mode, top_k=k,
                                 use_all_channels=use_all_channels,
                                 persona_id=q.get("persona_name") or None).get("results", [])
            lat.append((time.time() - t0) * 1000)
            contents = "\n".join(r.get("content", "") for r in res)
            hit = any(f and f in contents for f in facts)
            n += 1
            hits += 1 if hit else 0
            st = cat.setdefault(c, {"total": 0, "hits": 0})
            st["total"] += 1
            st["hits"] += 1 if hit else 0
        lat.sort()
        return {"mode": mode, "n": n, "r5": hits / n if n else 0.0, "cat": cat,
                "p50_ms": lat[len(lat) // 2] if lat else 0.0,
                "p95_ms": lat[int(len(lat) * 0.95)] if lat else 0.0}

    kw = eval_r5("keyword")
    hy = eval_r5("hybrid")
    # 2026-09-01（口径统一）：R@10（事实在前 10 条命中）——answer_eval 旧"R@5"实为
    # 全上下文口径(0.992)；本门禁 R@5=0.916 是严格前 5。双口径并存可互比。
    kw10 = eval_r5("keyword", k=10)

    # 对账面（只读，尽力而为）
    drift = None
    try:
        import contextlib, io, runpy
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runpy.run_path(os.path.join(ROOT, "scripts", "reconcile_pg_sqlite.py"), run_name="__main__")
        lines = [l for l in buf.getvalue().splitlines() if l.startswith("RECONCILE")]
        drift = lines[-1] if lines else None
    except Exception:
        pass

    # ── 通道归因 ablation（2026-09-01 CH-1 落地）：100 题子集上对比各配置 ──
    ablate = None
    if args.ablate:
        ablate = {}
        for cfg, cfg_kw in [
            ("keyword", dict(mode="keyword")),
            ("hybrid_all_channels", dict(mode="hybrid", use_all_channels=True)),
            ("hybrid_single_channel", dict(mode="hybrid", use_all_channels=False)),
            ("search_hybrid_rrf", dict(via_hybrid=True)),
        ]:
            _r = eval_r5(k=5, limit=100, **cfg_kw)
            ablate[cfg] = {"r5": round(_r["r5"], 4), "p50_ms": round(_r["p50_ms"], 1)}
        print("ABLATE(100q): " + json.dumps(ablate, ensure_ascii=False))

    cats = ["KU", "MS", "SS-A", "SS-P", "SS-U", "TR"]
    report = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "dataset": os.path.basename(dset),
        "keyword_r10": {"r5": round(kw10["r5"], 4), "p50_ms": round(kw10["p50_ms"], 1)},
        "keyword": {"r5": round(kw["r5"], 4), "p50_ms": round(kw["p50_ms"], 1), "p95_ms": round(kw["p95_ms"], 1),
                    "per_category": {c: round(kw["cat"][c]["hits"] / kw["cat"][c]["total"], 4) if kw["cat"].get(c) else None for c in cats}},
        "hybrid": {"r5": round(hy["r5"], 4), "p50_ms": round(hy["p50_ms"], 1), "p95_ms": round(hy["p95_ms"], 1),
                   "per_category": {c: round(hy["cat"][c]["hits"] / hy["cat"][c]["total"], 4) if hy["cat"].get(c) else None for c in cats}},
        "reconcile": drift,
        "ablate": ablate,
    }
    ok = kw["r5"] >= args.fail_r5 and hy["r5"] >= kw["r5"] - args.fail_hybrid_gap
    report["gate_ok"] = ok

    out_dir = os.path.join(os.path.expanduser("~"), ".trinity", "bench-results")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(out_dir, "quality-gate-%s.json" % ts), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print("QUALITY-GATE: %s (keyword R@5=%.3f hybrid R@5=%.3f threshold=%.2f)" %
          ("PASS" if ok else "FAIL", kw["r5"], hy["r5"], args.fail_r5))
    # 2026-09-01（元认知行动化）：FAIL→写 .quality-strict 标记(decay 保守化)；PASS→清除
    _marker = os.path.join(os.path.expanduser("~/.trinity"), ".quality-strict")
    if ok:
        if os.path.exists(_marker):
            os.remove(_marker)
            print("QUALITY-GATE: strict marker cleared (recovered)")
    else:
        with open(_marker, "w", encoding="utf-8") as _f:
            _f.write("quality gate FAIL at %s\n" % datetime.datetime.now().isoformat(timespec="seconds"))
        print("QUALITY-GATE: strict marker written (conservative decay ON)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
