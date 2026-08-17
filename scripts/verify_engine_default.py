#!/usr/bin/env python3
"""引擎默认路径验证（2026-08-17 二轮, 建议3 深化）

对标: 官方 LongMemEval_S 子集上 A/B 引擎默认检索路径——
  fts           mem.search(mode="keyword")        (旧默认, 官方 96.8% 口径)
  hybrid        mem.search(mode="hybrid")          (新默认: hybrid-rrf + FTS 兜底)
  hybrid_conf   新默认 + TRINITY_CONFIDENCE_SCORER=on
  hybrid_imp    新默认 + TRINITY_IMPORTANCE_BOOST=on
  hybrid_confimp 两者都开
判定: session R@5 / turn R@5 / 命中位次。用于决定新默认是否落地、评分特性是否默认启用。

Usage:
  python scripts/verify_engine_default.py --limit 120 --top-k 5
"""
import argparse
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["TRINITY_MEMORY_ENABLED"] = "0"

DATA = os.path.expanduser(r"~/.trinity/bench-official/longmemeval_s_cleaned.json")

CONFIGS = [
    ("fts", "keyword", {}),
    ("hybrid", "hybrid", {}),
    ("hybrid_conf", "hybrid", {"TRINITY_CONFIDENCE_SCORER": "on"}),
    ("hybrid_imp", "hybrid", {"TRINITY_IMPORTANCE_BOOST": "on"}),
    ("hybrid_confimp", "hybrid", {"TRINITY_CONFIDENCE_SCORER": "on", "TRINITY_IMPORTANCE_BOOST": "on"}),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    with open(DATA, "r", encoding="utf-8") as f:
        data = json.load(f)
    if args.limit and args.limit > 0:
        data = data[: args.limit]
    print(f"[verify] questions={len(data)} top_k={args.top_k}")

    tmpdir = tempfile.mkdtemp(prefix="engverify_")
    from trinity import Trinity
    mem = Trinity(adapter="sqlite", store_path=tmpdir)

    results = {name: [] for name, _, _ in CONFIGS}
    t0 = time.time()
    for qi, q in enumerate(data):
        try:
            qtype = str(q.get("question_type", ""))
            question = str(q.get("question", ""))
            sessions = q.get("haystack_sessions", []) or []
            sess_ids_list = q.get("haystack_session_ids", []) or []
            ans_sess = set(q.get("answer_session_ids", []) or [])
            agent = f"v_{qi}"
            sess_ids = []
            for si, sess in enumerate(sessions):
                turns = sess if isinstance(sess, list) else sess.get("turns", [])
                has_answer = False
                parts = []
                for t_ in turns:
                    role = t_.get("role", "user") if isinstance(t_, dict) else "user"
                    content = t_.get("content", "") if isinstance(t_, dict) else str(t_)
                    parts.append(f"[{role}] {content}")
                    if isinstance(t_, dict) and t_.get("has_answer"):
                        has_answer = True
                text = "\n".join(parts)
                if not text.strip():
                    continue
                try:
                    r = mem.ingest(text, agent_id=agent, category="lme",
                                   tags=["lme", qtype], postprocess=False)
                except Exception:
                    continue
                mid = r.get("memory_id")
                if mid:
                    sid = sess_ids_list[si] if si < len(sess_ids_list) else f"idx_{si}"
                    sess_ids.append((mid, sid, has_answer))
            mid2sess = {mid: (sid, ha) for mid, sid, ha in sess_ids}

            for name, mode, env_over in CONFIGS:
                saved = {k: os.environ.get(k) for k in env_over}
                for k, v in env_over.items():
                    os.environ[k] = v
                try:
                    hits = mem.search(question, top_k=args.top_k, mode=mode, agent_id=agent)
                finally:
                    for k, v in saved.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                hit_list = hits.get("results", []) if isinstance(hits, dict) else (hits or [])
                retrieved = [h.get("memory_id") for h in hit_list]
                r_sess = [mid2sess[m][0] for m in retrieved if m in mid2sess]
                r_ha = [mid2sess[m][1] for m in retrieved if m in mid2sess]
                pos = None
                for i, m in enumerate(retrieved):
                    if m in mid2sess:
                        sid, ha = mid2sess[m]
                        if sid in ans_sess or ha:
                            pos = i + 1
                            break
                results[name].append({
                    "session_hit": any(s in ans_sess for s in r_sess),
                    "turn_hit": any(r_ha), "pos": pos, "qtype": qtype,
                })
        except Exception as exc:
            print(f"  [q{qi} err] {type(exc).__name__}: {exc}", flush=True)
            for name, _, _ in CONFIGS:
                results[name].append({"session_hit": False, "turn_hit": False, "pos": None, "qtype": "error"})
        if (qi + 1) % 20 == 0 or qi + 1 == len(data):
            print(f"[{qi + 1}/{len(data)}] {time.time() - t0:.0f}s", flush=True)

    print()
    print(f"{'config':<14}{'sess R@5':>10}{'turn R@5':>10}{'mean pos':>10}{'n':>6}")
    print("-" * 50)
    summary = {"questions": len(data), "top_k": args.top_k, "configs": {}}
    for name, mode, _ in CONFIGS:
        rs = results[name]
        n = len(rs)
        sr = sum(1 for x in rs if x["session_hit"]) / max(1, n)
        tr = sum(1 for x in rs if x["turn_hit"]) / max(1, n)
        mp = sum(x["pos"] or args.top_k for x in rs) / max(1, n)
        print(f"{name:<14}{sr:>10.3f}{tr:>10.3f}{mp:>10.2f}{n:>6}")
        summary["configs"][name] = {
            "mode": mode,
            "session_r5": round(sr, 4), "turn_r5": round(tr, 4),
            "mean_hit_position": round(mp, 2),
        }
    out_path = args.out or os.path.join(
        os.path.expanduser("~/.trinity/bench-results"),
        f"engine_default_{time.strftime('%Y%m%d_%H%M%S')}.json",
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("报告已保存:", out_path)

    base = summary["configs"]["fts"]["session_r5"]
    hyb = summary["configs"]["hybrid"]["session_r5"]
    print()
    print(f"FTS(旧默认) = {base:.3f} | hybrid-rrf(新默认) = {hyb:.3f} (Δ {hyb - base:+.3f})")
    if hyb >= base:
        print("结论: hybrid-rrf 新默认 ≥ FTS，落地成立（含 FTS 兜底无退化风险）")
    else:
        print("结论: hybrid-rrf 未胜出，保持 FTS 默认（改回 _use_hybrid 需 hybrid retriever 已初始化）")


if __name__ == "__main__":
    main()
