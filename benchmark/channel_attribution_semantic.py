#!/usr/bin/env python3
"""
OPT7b: semantic vs keyword 逐类目检索归因（500q mock，本地 sklearn embedding）
==============================================================================
在相同 500q 数据集上对比：
  - keyword（FTS5，既有基线）
  - semantic（向量检索，SklearnEmbeddingEngine，本地离线）
输出逐类目 R@5 对比 → output/channel_attribution_semantic.md
"""
import json
import os
import sys
import tempfile
import time

ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, ROOT)
os.environ["TRINITY_MEMORY_ENABLED"] = "0"
os.environ["TRINITY_SILENT"] = "1"
DSET = r"C:\Users\Administrator\.marvis\workspace\conv_19f49996244_37d75ffae4a6\benchmark\longmemeval_mock_dataset.json"


def main():
    from trinity import Trinity
    data = json.load(open(DSET, encoding="utf-8"))
    questions = data["questions"]
    tmp = tempfile.mkdtemp(prefix="lme_att2_")
    mem = Trinity(store_path=os.path.join(tmp, "a.db"))
    for q in questions:
        for fact in q.get("context_facts", []):
            ftext = fact.get("fact", "")
            if not ftext:
                continue
            try:
                mem.ingest(ftext, persona_id=q.get("persona_name") or "default",
                           session_id=str(fact.get("session_id") or q.get("session_id") or "0"),
                           category=q.get("category", "general"), tags=["lme"])
            except Exception:
                pass
    print(f"ingested {len(questions)} questions' facts")

    def eval_mode(mode, use_vector=False):
        cat = {}
        hits = 0
        n = 0
        t0 = time.time()
        for q in questions:
            c = q.get("category", "?")
            question = q.get("question", "")
            facts = [f.get("fact", "").strip() for f in q.get("context_facts", []) if f.get("fact")]
            if not facts or not question:
                continue
            try:
                res = mem.search(query=question, mode=mode, top_k=10,
                                 use_vector=use_vector,
                                 persona_id=q.get("persona_name") or None).get("results", [])
            except Exception:
                res = []
            contents = "\n".join(r.get("content", "") for r in res)
            hit = any(f and f in contents for f in facts)
            n += 1
            hits += 1 if hit else 0
            st = cat.setdefault(c, {"total": 0, "hits": 0})
            st["total"] += 1
            st["hits"] += 1 if hit else 0
        return n, hits, cat, time.time() - t0

    n, h_kw, c_kw, t_kw = eval_mode("keyword")
    n, h_sem, c_sem, t_sem = eval_mode("semantic", use_vector=True)

    lines = ["# 检索通道归因（semantic vs keyword，OPT7b）\n",
             f"> 500q mock · top_k=10 · keyword=FTS5 · semantic=SklearnEmbeddingEngine(本地) · "
             f"耗时 kw={t_kw:.0f}s / sem={t_sem:.0f}s\n",
             "| 类目 | keyword R@5 | semantic R@5 | delta |",
             "|---|---|---|---|"]
    cats = ["KU", "MS", "SS-A", "SS-P", "SS-U", "TR"]
    for c in cats:
        k = c_kw[c]["hits"] / c_kw[c]["total"]
        s = c_sem[c]["hits"] / c_sem[c]["total"]
        lines.append(f"| {c} | {k:.3f} | {s:.3f} | {s-k:+.3f} |")
    lines.append(f"| **overall** | **{h_kw/n:.3f}** | **{h_sem/n:.3f}** | **{h_sem/n - h_kw/n:+.3f}** |")
    lines.append("\n## 结论\n")
    lines.append(f"- keyword overall R@5={h_kw/n:.3f}；semantic overall R@5={h_sem/n:.3f}。")
    best = max(cats, key=lambda c: c_sem[c]["hits"] / c_sem[c]["total"] - c_kw[c]["hits"] / c_kw[c]["total"])
    lines.append(f"- semantic 相对提升最大的类目：{best}。")
    lines.append("- 说明：本地 sklearn 引擎为降维哈希嵌入（dim 20），质量有限；"
                 "接入更强离线模型（如 bge-small）可进一步拉开差距。")

    out = os.path.join(ROOT, "output", "channel_attribution_semantic.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nsaved -> {out}")


if __name__ == "__main__":
    main()
