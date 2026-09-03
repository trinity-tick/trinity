#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eval_regression_gate.py — 评测回归门禁（EXECUTION 466）

在维护链上快速守住官方基准：分层随机抽 100 题（每类按占比）跑 recall（无 LLM），
可 --qa 追加生成侧抽查（约 60 题，$0.08）；与基准（baseline.json）比较：
  recall ev<=5 比例/类目与总体偏差超阈值 → exit 1（FAIL），否则 PASS。
用法:
  python benchmark/eval_regression_gate.py --qa --out gate_result.json
"""
import argparse, json, os, random, sys, tempfile, time

ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

DSET = os.path.join(ROOT, "benchmark", "data", "longmemeval_oracle.json")
BASE = os.path.expanduser("~/.trinity/bench-official/gate_baseline.json")
THRESH_OVERALL = 0.03   # 总体 ev<=5 比例可容忍下降
THRESH_CAT = 0.08       # 单类目可容忍下降


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qa", action="store_true")
    ap.add_argument("--out", default=os.path.expanduser("~/.trinity/bench-official/gate_result.json"))
    ap.add_argument("--update-baseline", action="store_true", help="把本次结果存为基线")
    args = ap.parse_args()

    from trinity.adapters.sqlite import SQLiteAdapter
    data = json.load(open(DSET, encoding="utf-8"))
    by_cat = {}
    for q in data:
        by_cat.setdefault(q.get("question_type", "?"), []).append(q)
    cats = sorted(by_cat)
    # 分层：每类至少 8 题，总量 ~100（按占比放大到每类≥8）
    per = {}
    target_total = 100
    base_per = {c: max(8, int(target_total * len(v) / len(data))) for c, v in by_cat.items()}
    # 收敛到 ~100
    while sum(base_per.values()) > target_total and any(base_per[c] > 8 for c in cats):
        c = max(cats, key=lambda x: base_per[x])
        base_per[c] -= 1
    rng = random.Random(20260902)
    sample = []
    for c in cats:
        sample += rng.sample(by_cat[c], min(base_per[c], len(by_cat[c])))
    print("gate sample:", {c: min(base_per[c], len(by_cat[c])) for c in cats}, "total", len(sample))

    def ev_stats(qq, ad):
        question = str(qq.get("question") or "")
        ans_sess = set(qq.get("answer_session_ids") or [])
        sid_list = qq.get("haystack_session_ids") or []
        records = []
        for idx, msgs in enumerate(qq.get("haystack_sessions") or []):
            real_sid = str(sid_list[idx]) if idx < len(sid_list) else "sess_%d" % idx
            for mm in msgs:
                content = str(mm.get("content") or "") if isinstance(mm, dict) else str(mm)
                if content.strip():
                    records.append({"content": content[:2000], "persona_id": "u1",
                                    "session_id": real_sid, "agent_id": "u1", "importance": 0.5})
        ad.ingest_batch(records)
        res = ad.search_memories(query=question, top_k=20)
        for i, h in enumerate(res):
            if str(h.get("session_id") or "") in ans_sess:
                return {"ev_rank": i + 1, "ok5": i < 5, "ok14": i < 14}
        return {"ev_rank": None, "ok5": False, "ok14": False}

    overall = {"ok5": 0, "ok14": 0, "total": 0}
    by = {}
    t0 = time.time()
    for q in sample:
        cat = q.get("question_type", "?")
        tmp = tempfile.mkdtemp(prefix="gate_")
        ad = SQLiteAdapter(db_path=os.path.join(tmp, "s.db"))
        ad.connect()
        try:
            s = ev_stats(q, ad)
        finally:
            ad.disconnect()
        st = by.setdefault(cat, {"ok5": 0, "ok14": 0, "total": 0})
        st["ok5"] += 1 if s["ok5"] else 0
        st["ok14"] += 1 if s["ok14"] else 0
        st["total"] += 1
        overall["ok5"] += 1 if s["ok5"] else 0
        overall["ok14"] += 1 if s["ok14"] else 0
        overall["total"] += 1
    overall["rate5"] = overall["ok5"] / max(1, overall["total"])
    for c, st in by.items():
        st["rate5"] = st["ok5"] / max(1, st["total"])

    base = {}
    if os.path.exists(BASE):
        try:
            base = json.load(open(BASE, encoding="utf-8"))
        except Exception:
            base = {}
    ok = True
    notes = []
    if base.get("rate5"):
        if overall["rate5"] < base["rate5"] - THRESH_OVERALL:
            ok = False
            notes.append("overall rate5 %.3f < base %.3f - %.2f" % (overall["rate5"], base["rate5"], THRESH_OVERALL))
        for c, st in by.items():
            b5 = (base.get("by", {}).get(c) or {}).get("rate5")
            if b5 and st["rate5"] < b5 - THRESH_CAT:
                ok = False
                notes.append("%s rate5 %.3f < base %.3f" % (c, st["rate5"], b5))
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "sample": len(sample),
           "overall": overall, "by": by, "pass": ok, "notes": notes,
           "elapsed_s": round(time.time() - t0, 1)}
    if args.update_baseline:
        json.dump({"rate5": overall["rate5"], "by": by}, open(BASE, "w", encoding="utf-8"), indent=1)
    json.dump(out, open(args.out, "w", encoding="utf-8"), indent=1)
    print("GATE", "PASS" if ok else "FAIL", json.dumps({"rate5": overall["rate5"], "notes": notes}))
    print("saved ->", args.out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
