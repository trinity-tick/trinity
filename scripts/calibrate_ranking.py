#!/usr/bin/env python3
"""Trinity 检索排序标定脚本（2026-08-17, 建议3 落地）

对标 MEMTIER（学习权重 w⊤ϕ）的数据驱动标定：在官方 LongMemEval_S 数据集
子集上，对 检索策略（fusion/rrf/cascade）与评分校准特性（confidence /
importance）做 A/B，比较 session R@5 / turn R@5 / 命中位次。

隔离：全部写入临时 SQLite 库，不污染线上大库。

Usage:
  python scripts/calibrate_ranking.py --limit 60 --top-k 5
  输出: ~/.trinity/bench-results/calib_ranking_<ts>.json + 终端对比表
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

# (name, env_overrides, strategy)
# 2026-08-17 首轮标定结论: fusion 静态权重 R@5=0.008 / rrf=0.950 → 默认已切 rrf。
# 本轮聚焦 rrf 基线 vs rrf+评分特性（confidence/importance），并保留 fusion 参考。
CONFIGS = [
    ("fusion_baseline", {}, "fusion"),
    ("rrf_baseline", {}, "rrf"),
    ("rrf_conf", {"TRINITY_CONFIDENCE_SCORER": "on"}, "rrf"),
    ("rrf_imp", {"TRINITY_IMPORTANCE_BOOST": "on"}, "rrf"),
    ("rrf_conf_imp", {"TRINITY_CONFIDENCE_SCORER": "on", "TRINITY_IMPORTANCE_BOOST": "on"}, "rrf"),
]

TOP_K = 5


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def main() -> None:
    parser = argparse.ArgumentParser(description="Trinity 检索排序标定（LongMemEval_S 子集 A/B）")
    parser.add_argument("--limit", type=int, default=60, help="题目数（0=全部 500）")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--out", default=None, help="报告 JSON 输出路径")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    global TOP_K
    TOP_K = args.top_k

    print(f"[calibrate] 加载官方数据集: {DATA}")
    with open(DATA, "r", encoding="utf-8") as f:
        data = json.load(f)
    if args.limit and args.limit > 0:
        data = data[: args.limit]
    print(f"[calibrate] 题目数: {len(data)}")

    tmpdir = tempfile.mkdtemp(prefix="calib_")
    from trinity import Trinity
    mem = Trinity(adapter="sqlite", store_path=tmpdir)

    # results[name][q_index] = {"session_hit": bool, "turn_hit": bool, "pos": int|None}
    results: dict = {name: [] for name, _, _ in CONFIGS}
    t0 = time.time()

    for qi, q in enumerate(data):
        try:
            qtype = str(q.get("question_type", ""))
            question = str(q.get("question", ""))
            sessions = q.get("haystack_sessions", []) or []
            sess_ids_list = q.get("haystack_session_ids", []) or []
            ans_sess = set(q.get("answer_session_ids", []) or [])
            agent = f"calib_{qi}"

            # 1) 摄入：每会话一条记忆
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
                    continue  # UNIQUE 冲突等
                mid = r.get("memory_id")
                if mid:
                    sid = sess_ids_list[si] if si < len(sess_ids_list) else f"idx_{si}"
                    sess_ids.append((mid, sid, has_answer))

            mid2sess = {mid: (sid, ha) for mid, sid, ha in sess_ids}

            # 2) 每个配置检索
            for name, env_over, strategy in CONFIGS:
                saved = {k: os.environ.get(k) for k in env_over}
                for k, v in env_over.items():
                    os.environ[k] = v
                try:
                    hits = mem.search_hybrid(
                        question, top_k=TOP_K, strategy=strategy, agent_id=agent,
                    )
                finally:
                    for k, v in saved.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                hit_list = hits.get("results", []) if isinstance(hits, dict) else (hits or [])
                retrieved = [h.get("memory_id") for h in hit_list]
                retrieved_sess = [mid2sess[m][0] for m in retrieved if m in mid2sess]
                retrieved_ha = [mid2sess[m][1] for m in retrieved if m in mid2sess]
                session_hit = any(s in ans_sess for s in retrieved_sess)
                turn_hit = any(retrieved_ha)
                pos = None
                for i, m in enumerate(retrieved):
                    if m in mid2sess:
                        sid, ha = mid2sess[m]
                        if sid in ans_sess or ha:
                            pos = i + 1
                            break
                results[name].append({
                    "session_hit": session_hit, "turn_hit": turn_hit, "pos": pos,
                    "qtype": qtype,
                })
        except Exception as exc:
            print(f"  [q{qi} error] {type(exc).__name__}: {exc}", flush=True)
            for name, _, _ in CONFIGS:
                results[name].append({"session_hit": False, "turn_hit": False, "pos": None, "qtype": "error"})

        if (qi + 1) % 10 == 0 or qi + 1 == len(data):
            print(f"[{qi + 1}/{len(data)}] {time.time() - t0:.0f}s", flush=True)

    # 3) 汇总
    summary = {"dataset": "longmemeval_s_cleaned (official subset)",
               "questions": len(data), "top_k": TOP_K, "configs": {}}
    print()
    print(f"{'config':<18}{'sess R@5':>10}{'turn R@5':>10}{'mean pos':>10}{'n':>6}")
    print("-" * 54)
    for name, _, strategy in CONFIGS:
        rs = results[name]
        n = len(rs)
        sess_r = sum(1 for x in rs if x["session_hit"]) / max(1, n)
        turn_r = sum(1 for x in rs if x["turn_hit"]) / max(1, n)
        pos_vals = [x["pos"] or TOP_K for x in rs]
        mean_pos = sum(pos_vals) / max(1, n)
        print(f"{name:<18}{sess_r:>10.3f}{turn_r:>10.3f}{mean_pos:>10.2f}{n:>6}")
        by_type = {}
        for t in sorted(set(x["qtype"] for x in rs)):
            sub = [x for x in rs if x["qtype"] == t]
            by_type[t] = {
                "n": len(sub),
                "session_r5": round(sum(1 for x in sub if x["session_hit"]) / len(sub), 4),
            }
        summary["configs"][name] = {
            "strategy": strategy,
            "session_r5": round(sess_r, 4),
            "turn_r5": round(turn_r, 4),
            "mean_hit_position": round(mean_pos, 2),
            "by_type": by_type,
        }

    out_path = args.out or os.path.join(
        os.path.expanduser("~/.trinity/bench-results"),
        f"calib_ranking_{time.strftime('%Y%m%d_%H%M%S')}.json",
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print()
    print("报告已保存:", out_path)

    # 4) 结论
    best = max(CONFIGS, key=lambda c: summary["configs"][c[0]]["session_r5"])
    base = summary["configs"]["fusion_baseline"]["session_r5"]
    best_v = summary["configs"][best[0]]["session_r5"]
    print()
    print(f"基线 fusion_baseline session R@{TOP_K} = {base:.3f}")
    print(f"最优 {best[0]} = {best_v:.3f} (Δ {best_v - base:+.3f})")
    if best[0] != "fusion_baseline" and best_v > base:
        print("建议：将默认检索配置切换为", best[0])
    else:
        print("建议：维持 fusion_baseline 默认配置（标定未发现更优配置）")


if __name__ == "__main__":
    main()
