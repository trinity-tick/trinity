#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hard_holdout_eval.py — 生产难查询 holdout 三臂评测（二轮优化 Task 2）。

在 hard_holdout.json（近义改写难查询）上对比检索臂：
  - keyword（FTS，默认路径）
  - page_tree（页优先）
  - hybrid rrf（5 通道）与 hybrid+页通道（TRINITY_PAGETREE_HYBRID=on）
  - reason（LLM 判题）

指标：R@5 / R@10（期望事实是否出现在结果集中，归一化子串匹配），逐类目 + 逐臂
胜负归因（页树/reason 独中、漏检示例）。reason 臂有 LLM 成本（~$0.05/120 题）。

用法:
    python benchmark/hard_holdout_eval.py [--holdout output/hard_holdout.json] [--arms keyword,pagetree,hybrid,reason]
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

DEFAULT_HOLDOUT = os.path.join(ROOT, "output", "hard_holdout.json")


def norm(t):
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (t or "").lower())


def fact_hit(texts, fact):
    fn = norm(fact)
    if not fn or len(fn) < 8:
        return False
    cn = norm("\n".join(texts))
    return fn in cn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", default=DEFAULT_HOLDOUT)
    ap.add_argument("--arms", default="keyword,pagetree,hybrid,hybrid_pt,reason",
                    help="逗号分隔：keyword,pagetree,hybrid,hybrid_pt,reason")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--out", default=os.path.join(ROOT, "output", "hard_holdout_eval.md"))
    args = ap.parse_args()

    if not os.path.exists(args.holdout):
        print(f"ERROR: holdout not found: {args.holdout} (run scripts/build_hard_holdout.py first)")
        return 1
    data = json.load(open(args.holdout, encoding="utf-8"))
    items = data["items"]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    print(f"holdout: {len(items)} hard queries, arms={arms}, top_k={args.top_k}")

    from trinity import Trinity
    mem = Trinity(adapter="sqlite")

    # hybrid 页通道开关（本进程内两臂需要分开跑，语义缓存关闭防污染）
    os.environ["TRINITY_CACHE_BACKEND"] = "off"
    # reason 臂的 LLM judge 需要 API key（resolve_api_key 只读环境变量）
    if "reason" in arms:
        import re as _re
        _creds = {}
        try:
            _raw = open(os.path.expanduser("~/.dsh/.credentials.yaml"), encoding="utf-8-sig").read()
            for _line in _raw.splitlines():
                _m = _re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", _line)
                if _m and not _line.strip().startswith("#"):
                    _creds[_m.group(1)] = _m.group(2).strip().strip("'\"")
        except Exception:
            pass
        os.environ.setdefault("TRINITY_LLM_API_KEY",
                              _creds.get("DEEPSEEK_API_KEY", ""))

    stats = {a: {"n": 0, "r5": 0, "r10": 0} for a in arms}
    examples = {a: {"win": [], "lose": []} for a in arms}

    def _search_arm(arm, q):
        if arm == "keyword":
            r = mem.search(query=q, mode="keyword", top_k=args.top_k).get("results", [])
        elif arm == "pagetree":
            r = mem.search(query=q, mode="keyword", top_k=args.top_k, page_tree=True).get("results", [])
        elif arm == "hybrid":
            r = mem.search_hybrid(query=q, top_k=args.top_k, strategy="rrf").get("results", [])
        elif arm == "hybrid_pt":
            os.environ["TRINITY_PAGETREE_HYBRID"] = "on"
            try:
                r = mem.search_hybrid(query=q, top_k=args.top_k, strategy="rrf").get("results", [])
            finally:
                os.environ.pop("TRINITY_PAGETREE_HYBRID", None)
        elif arm == "reason":
            r = mem.search(query=q, mode="reason", top_k=args.top_k).get("results", [])
        else:
            r = []
        # 回补内容（hybrid 返回 lean dict）
        texts = []
        for x in r:
            c = x.get("content") or ""
            if not c:
                mid = x.get("memory_id")
                try:
                    c = (mem._adapter.get_memory(mid) or {}).get("content", "") if mid else ""
                except Exception:
                    c = ""
            texts.append(c)
        return texts

    for i, it in enumerate(items):
        q = it["query"]
        fact = it["fact"]
        hits = {}
        for arm in arms:
            texts = _search_arm(arm, q)
            hit10 = fact_hit(texts, fact)
            hit5 = fact_hit(texts[:5], fact)
            hits[arm] = hit10
            s = stats[arm]
            s["n"] += 1
            s["r10"] += 1 if hit10 else 0
            s["r5"] += 1 if hit5 else 0
        # 归因示例
        kw = hits.get("keyword", False)
        for arm in arms:
            if arm == "keyword":
                continue
            if hits[arm] and not kw and len(examples[arm]["win"]) < 8:
                examples[arm]["win"].append(q[:80])
            if not hits[arm] and kw and len(examples[arm]["lose"]) < 8:
                examples[arm]["lose"].append(q[:80])
        if (i + 1) % 30 == 0:
            print(f"  [{i + 1}/{len(items)}]")

    lines = ["# 生产难查询 holdout 评测（二轮优化 Task 2）", ""]
    lines.append(f"holdout: {len(items)} 题（近义改写，overlap<=40%），top_k={args.top_k}")
    lines.append("")
    lines.append("| 臂 | R@5 | R@10 |")
    lines.append("|---|---|---|")
    for a in arms:
        s = stats[a]
        lines.append(f"| {a} | {s['r5'] / max(1, s['n']):.3f} | {s['r10'] / max(1, s['n']):.3f} |")
    lines.append("")
    lines.append("### 页树/reason 独中（keyword 漏检）示例")
    lines.append("")
    for a in arms:
        if a == "keyword":
            continue
        wins = examples[a]["win"]
        if wins:
            lines.append(f"**{a} 独中 {len(wins)} 例**：")
            for w in wins:
                lines.append(f"  - {w}")
    lines.append("")
    lines.append("### 页树/reason 漏检（keyword 命中）示例")
    lines.append("")
    for a in arms:
        if a == "keyword":
            continue
        loses = examples[a]["lose"]
        if loses:
            lines.append(f"**{a} 漏检 {len(loses)} 例**：")
            for w in loses:
                lines.append(f"  - {w}")
    lines.append("")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))
    print(f"saved -> {args.out}")

    # 2026-08-26（DSH 借鉴 建议执行）：JSON 输出——目标引擎 default_metrics
    # 需要读取 reason R@10 作为指标（holdout_reason_r10）。
    json_out = {
        "test": "hard_holdout_eval",
        "n": len(items),
        "top_k": args.top_k,
        "arms": {a: {"r5": round(stats[a]["r5"] / max(1, stats[a]["n"]), 4),
                     "r10": round(stats[a]["r10"] / max(1, stats[a]["n"]), 4)}
                 for a in arms},
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    }
    import json as _json
    _json_path = os.path.splitext(args.out)[0] + ".json"
    with open(_json_path, "w", encoding="utf-8") as f:
        _json.dump(json_out, f, ensure_ascii=False, indent=1)
    print(f"saved -> {_json_path}")
    # 2026-08-26（Claude Science 借鉴 Phase 1）：实验工件 manifest
    try:
        from trinity.benchmark.manifest import build_manifest
        build_manifest(_json_path, params={"top_k": args.top_k, "arms": arms},
                       dataset_paths=[args.holdout])
    except Exception as _m_exc:
        print(f"WARN manifest: {_m_exc}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
