# -*- coding: utf-8 -*-
"""tune_report.py — 参数应用效果评估（2026-08-27）。

tuned 配置（tuned_config.json）vs 默认配置对比：命中率 + LLM 调用。
用法: python scripts/tune_report.py [--queries 10]
"""
import os
import sys
import json
import argparse

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

_QUERIES = [
    "仓库上架扫码确认库位 波次分配", "退货流程审批节点 权限", "物流运费模板 配置",
    "库存盘点差异处理 规则", "旺店通计费策略 按件", "跨境电商退货质检 标准",
    "上架作业 扫码 库位", "波次拣货 分配", "运费模板 配置 区域", "盘点差异 处理",
]


def main() -> int:
    if not _QUERIES:
        print("No queries available for evaluation.")
        return 1
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=10, help="要评估的查询数量（默认: 10，最大: %d）" % len(_QUERIES))
    args = ap.parse_args()
    if args.queries < 1:
        args.queries = 1
    import trinity.core.client._pagetree as PT
    from trinity import Trinity
    mem = Trinity(adapter="sqlite")
    queries = _QUERIES[: args.queries]

    def run(thr):
        PT._JUDGE_CACHE.clear()
        PT._JUDGE_CACHE_TS.clear()
        PT._JUDGE_LLM_CALLS = 0
        if thr:
            os.environ["TRINITY_JUDGE_THRESHOLD"] = thr
        else:
            os.environ.pop("TRINITY_JUDGE_THRESHOLD", None)
        hits = 0
        for q in queries:
            try:
                if mem.search(query=q, mode="reason", top_k=5).get("results"):
                    hits += 1
            except Exception:
                pass
        return hits, PT._JUDGE_LLM_CALLS

    # tuned 配置（推荐值或默认）
    tuned = None
    cfg = os.path.expanduser("~/.trinity/tuned_config.json")
    if os.path.exists(cfg):
        try:
            tuned = json.load(open(cfg, encoding="utf-8"))
        except Exception:
            tuned = None
    tuned_thr = str((tuned or {}).get("recommended_threshold", "0.55"))
    # A: tuned 生效（清 env 走 tuned_config）
    os.environ.pop("TRINITY_JUDGE_THRESHOLD", None)
    h1, l1 = run(None)
    # B: 默认（强制 0.55）
    h2, l2 = run("0.55")
    print(f"tuned ({tuned_thr}): hits={h1}/{len(queries)} llm={l1}")
    print(f"default (0.55): hits={h2}/{len(queries)} llm={l2}")
    gain = l2 - l1
    print(f"LLM calls saved: {gain} | hit delta: {h1 - h2}")
    print("TUNE_REPORT_OK")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
