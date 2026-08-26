#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pagetree_ab_compare.py — PageIndex 借鉴 Phase 1 A/B 汇总与逐问归因。

1) 汇总两臂 LLM 评测结果 JSON（answer_eval --out）：R@5 / AnswerAcc / gap 逐类目对比；
2) 检索侧逐问归因（纯本地，无 LLM 成本）：同 500q 集 + 同临时库，
   keyword（baseline）vs page_tree 两臂 R@5 逐问对比：
     - pt_only_hits: 页树命中而关键词漏检（页树增益）
     - base_only_hits: 关键词命中而页树漏检（页树损失）
     - both_hit / both_miss
   按类目输出 → output/pagetree_attribution.md

用法:
    python benchmark/pagetree_ab_compare.py --base output/ae_500_base.json --pt output/ae_500_pt.json
"""
import argparse
import json
import os
import sys
import tempfile

ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, ROOT)
os.environ["TRINITY_MEMORY_ENABLED"] = "0"

DSET = r"C:\Users\Administrator\.marvis\workspace\conv_19f49996244_37d75ffae4a6\benchmark\longmemeval_mock_dataset.json"


def norm(text: str) -> str:
    import re
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())


def fact_hit(context_text: str, facts) -> bool:
    cn = norm(context_text)
    for f in facts:
        fn = norm(f)
        if fn and len(fn) >= 4 and fn in cn:
            return True
    return False


def load_llm_results(path):
    if not os.path.exists(path):
        return None
    return json.load(open(path, encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.path.join(ROOT, "output", "ae_500_base.json"))
    ap.add_argument("--pt", default=os.path.join(ROOT, "output", "ae_500_pt.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "output", "pagetree_attribution.md"))
    args = ap.parse_args()

    base_r = load_llm_results(args.base)
    pt_r = load_llm_results(args.pt)
    lines = ["# PageTree A/B 归因报告（2026-08-26，Phase 1）", ""]

    if base_r and pt_r:
        lines.append("## 一、LLM 评测汇总（answer_eval，deepseek-chat）")
        lines.append("")
        lines.append("| 指标 | 基线 keyword | 页树 page_tree | Δ |")
        lines.append("|---|---|---|---|")
        for k in ("R@5", "AnswerAcc", "generation_gap", "retrieval_gap", "avg_latency_s", "est_cost_usd"):
            b = base_r.get(k, "-")
            p = pt_r.get(k, "-")
            d = ""
            if isinstance(b, (int, float)) and isinstance(p, (int, float)):
                d = f"{p - b:+.4f}"
            lines.append(f"| {k} | {b} | {p} | {d} |")
        lines.append("")
        lines.append("### 逐类目")
        lines.append("")
        lines.append("| 类目 | n | 基线 R@5 | 页树 R@5 | 基线 Acc | 页树 Acc |")
        lines.append("|---|---|---|---|---|---|")
        cats = sorted(set(base_r.get("by_category", {})) | set(pt_r.get("by_category", {})))
        for c in cats:
            b = base_r.get("by_category", {}).get(c, {})
            p = pt_r.get("by_category", {}).get(c, {})
            lines.append(f"| {c} | {b.get('total', p.get('total', 0))} | "
                         f"{b.get('R@5', '-')} | {p.get('R@5', '-')} | "
                         f"{b.get('AnswerAcc', '-')} | {p.get('AnswerAcc', '-')} |")
        lines.append("")

    # ── 检索侧逐问归因（无 LLM 成本）──────────────────────────────
    lines.append("## 二、检索侧逐问归因（keyword vs page_tree，R@5）")
    lines.append("")
    data = json.load(open(DSET, encoding="utf-8"))
    questions = data["questions"]
    tmpdir = tempfile.mkdtemp(prefix="lme_ab_")
    from trinity import Trinity
    mem = Trinity(adapter="sqlite", store_path=tmpdir)
    for q in questions:
        for fact in q.get("context_facts", []):
            ftext = fact.get("fact", "")
            if not ftext:
                continue
            try:
                mem.ingest(ftext, persona_id=q.get("persona_name") or "default",
                           session_id=str(q.get("session_id") or "0"),
                           category=q.get("category", "general"),
                           tags=["lme", q.get("category", "")])
            except Exception:
                pass
    mem.build_pagetree(exclude_categories=set(), exclude_tags={"lme"})

    stats = {}
    examples = {}
    for q in questions:
        cat = q.get("category", "?")
        question = q.get("question", "")
        facts = [f.get("fact", "").strip() for f in q.get("context_facts", []) if f.get("fact")]
        if not facts or not question:
            continue
        persona = q.get("persona_name") or None
        r_base = mem.search(query=question, mode="keyword", top_k=10,
                            persona_id=persona).get("results", [])
        r_pt = mem.search(query=question, mode="keyword", top_k=10,
                          persona_id=persona, page_tree=True).get("results", [])
        hit_base = fact_hit("\n".join(r.get("content", "") for r in r_base), facts)
        hit_pt = fact_hit("\n".join(r.get("content", "") for r in r_pt), facts)
        st = stats.setdefault(cat, {"n": 0, "both": 0, "pt_only": 0, "base_only": 0, "miss": 0})
        st["n"] += 1
        key = "both" if (hit_base and hit_pt) else "pt_only" if (hit_pt and not hit_base) \
            else "base_only" if (hit_base and not hit_pt) else "miss"
        st[key] += 1
        if key in ("pt_only", "base_only"):
            ex = examples.setdefault(cat, {})
            ex.setdefault(key, []).append(question[:90])

    lines.append("| 类目 | n | both | 页树增益(pt_only) | 关键词独中(base_only) | 双失 |")
    lines.append("|---|---|---|---|---|---|")
    tot = {"n": 0, "both": 0, "pt_only": 0, "base_only": 0, "miss": 0}
    for c in sorted(stats):
        s = stats[c]
        for k in tot:
            tot[k] += s[k]
        lines.append(f"| {c} | {s['n']} | {s['both']} | **{s['pt_only']}** | {s['base_only']} | {s['miss']} |")
    lines.append(f"| **合计** | {tot['n']} | {tot['both']} | **{tot['pt_only']}** | {tot['base_only']} | {tot['miss']} |")
    lines.append("")
    # ── hybrid 通道臂：rrf 基线 vs rrf+页树通道（TRINITY_PAGETREE_HYBRID=on）──
    lines.append("")
    lines.append("## 三、hybrid 通道臂（rrf vs rrf+pagetree 通道，R@5）")
    lines.append("")
    import os as _os
    # 语义缓存会污染两臂对比（cache key 不含 pagetree 开关）→ 关闭
    _os.environ["TRINITY_CACHE_BACKEND"] = "off"

    def _hybrid_hit(q, facts, persona):
        # search_hybrid 返回 lean dict（无 content）→ 按 memory_id 回补内容
        res = mem.search_hybrid(query=q, top_k=10, strategy="rrf",
                                persona_id=persona).get("results", [])
        texts = []
        for r in res:
            c = r.get("content") or ""
            if not c:
                mid = r.get("memory_id")
                try:
                    full = mem._adapter.get_memory(mid) if (mid and mem._adapter) else None
                    c = (full or {}).get("content") or ""
                except Exception:
                    c = ""
            texts.append(c)
        return fact_hit("\n".join(texts), facts)

    hy_stats = {}
    _os.environ["TRINITY_PAGETREE_HYBRID"] = "off"
    for q in questions:
        cat = q.get("category", "?")
        question = q.get("question", "")
        facts = [f.get("fact", "").strip() for f in q.get("context_facts", []) if f.get("fact")]
        if not facts or not question:
            continue
        persona = q.get("persona_name") or None
        hit1 = _hybrid_hit(question, facts, persona)
        st = hy_stats.setdefault(cat, {"n": 0, "hits": 0})
        st["n"] += 1
        st["hits"] += 1 if hit1 else 0
    _os.environ["TRINITY_PAGETREE_HYBRID"] = "on"
    hy_stats2 = {}
    for q in questions:
        cat = q.get("category", "?")
        question = q.get("question", "")
        facts = [f.get("fact", "").strip() for f in q.get("context_facts", []) if f.get("fact")]
        if not facts or not question:
            continue
        persona = q.get("persona_name") or None
        hit2 = _hybrid_hit(question, facts, persona)
        st = hy_stats2.setdefault(cat, {"n": 0, "hits": 0})
        st["n"] += 1
        st["hits"] += 1 if hit2 else 0
    _os.environ.pop("TRINITY_PAGETREE_HYBRID", None)
    lines.append("| 类目 | n | rrf R@5 | rrf+pagetree R@5 | Δ |")
    lines.append("|---|---|---|---|---|")
    h1t = h2t = 0
    for c in sorted(set(hy_stats) | set(hy_stats2)):
        s1 = hy_stats.get(c, {})
        s2 = hy_stats2.get(c, {})
        r1v = s1.get("hits", 0) / max(1, s1.get("n", 1))
        r2v = s2.get("hits", 0) / max(1, s2.get("n", 1))
        h1t += s1.get("hits", 0)
        h2t += s2.get("hits", 0)
        lines.append(f"| {c} | {s1.get('n', 0)} | {r1v:.3f} | {r2v:.3f} | {r2v - r1v:+.3f} |")
    lines.append(f"| 合计 | 500 | {h1t/500:.3f} | {h2t/500:.3f} | {h2t/500 - h1t/500:+.3f} |")
    lines.append("")

    if examples:
        lines.append("### 页树增益/损失示例问题")
        lines.append("")
        for c in sorted(examples):
            for k in ("pt_only", "base_only"):
                qs = examples[c].get(k, [])[:5]
                if qs:
                    lines.append(f"- **{c} / {k}**（{len(examples[c].get(k, []))} 题）：")
                    for qq in qs:
                        lines.append(f"  - {qq}")
    lines.append("")
    lines.append(f"（检索侧归因：同一临时库、同 ingest、top_k=10，仅检索无 LLM）")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))
    print(f"saved -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
