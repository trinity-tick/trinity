# -*- coding: utf-8 -*-
"""ms_diagnose.py — MS 类目失败归因诊断（2026-08-29 P0 GEN-MS 第一步）。

抽样 MS 题目 → 检索（keyword/hybrid）→ 生成 → 归因：
  漏检（答案不在检索结果）/ 漏拼（检索到但上下文组装缺失）/ 幻觉（生成内容与检索无关）
输出诊断报告（docs/MS_DIAGNOSIS_*.md）。
"""
import os
import sys
import json
import argparse

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10, help="抽样 MS 题数")
    ap.add_argument("--out", default=os.path.join(_TRINITY_ROOT, "docs"))
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from trinity import Trinity
    mem = Trinity(adapter="sqlite")

    # MS 类目题目来源：ai_500 mock 数据集（benchmark 下）或历史评测结果
    ds = None
    for cand in ["benchmark/data/ai_500_questions.json",
                 "output/ae_500_reason_v5.json",
                 "output/ae_500_reason_v3.json"]:
        p = os.path.join(_TRINITY_ROOT, cand)
        if os.path.exists(p):
            ds = p
            break
    if not ds:
        print("no MS dataset found")
        return 1

    data = json.load(open(ds, encoding="utf-8"))
    # 数据结构兼容：ae_500 是评测结果（by_category/answers），非题目列表——
    # MS 题目直接从 LongMemEval 数据集抽（bench-official）
    import glob as _gl
    lme = sorted(_gl.glob(os.path.expanduser("~/.trinity/bench-official/lme_s_*.json")))
    qs = []
    for lf in lme:
        if "manifest" in lf:
            continue
        try:
            ld = json.load(open(lf, encoding="utf-8"))
            for q in ld.get("questions_detail", []) or ld.get("questions", []):
                if isinstance(q, dict) and (q.get("type") or "").startswith("multi"):
                    qs.append(q)
        except Exception:
            continue
    # 兜底：用 lme 数据集原始 json
    if not qs:
        orig = os.path.expanduser("~/.trinity/bench-official/longmemeval_s_cleaned.json")
        if os.path.exists(orig):
            od = json.load(open(orig, encoding="utf-8"))
            qs = [x for x in (od if isinstance(od, list) else od.get("questions", []))
                  if (x.get("question_type") or x.get("type") or "").startswith("multi_session")][:50]
    ms_items = qs
    ms_items = []
    for it in qs:
        cat = (it.get("category") or it.get("type") or "").lower()
        if "ms" in cat or it.get("multi_session"):
            ms_items.append(it)
    ms_items = ms_items[: args.limit]
    print(f"MS sample: {len(ms_items)} (from {os.path.basename(ds)})")

    report = ["# MS 类目失败归因诊断", "", f"样本: {len(ms_items)} 条", ""]

    for i, it in enumerate(ms_items):
        q = it.get("query") or it.get("question") or it.get("content") or ""
        gold = it.get("answer") or it.get("golden_answer") or ""
        # 1) 检索
        kw = mem.search(query=q, mode="keyword", top_k=10)
        hy = mem.search(query=q, mode="hybrid", top_k=10)
        kw_ids = {x.get("memory_id") for x in kw.get("results", [])}
        hy_ids = {x.get("memory_id") for x in hy.get("results", [])}
        union = kw_ids | hy_ids
        # 2) 生成（占位——GEN-MS 下一步接 LLM 生成）
        report.append(f"## 样本 {i+1}: {q[:60]}")
        report.append(f"- keyword hits: {len(kw_ids)} | hybrid hits: {len(hy_ids)} | union: {len(union)}")
        report.append(f"- 归因: {'待生成分析（GEN-MS 下一步）' if union else '漏检（检索未命中）'}")
        report.append("")
        # 归因统计位
    # 汇总
    report.append("## 归因统计（首轮检索面）")
    report.append("- 检索面（union 非空）与漏检比例见上方样本")
    report.append("- 下一步：接入 LLM 生成 → 区分漏检/漏拼/幻觉三类")

    out = os.path.join(args.out, "MS_DIAGNOSIS_20260829.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(chr(10).join(report))
    print("report:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
