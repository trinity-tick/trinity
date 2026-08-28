# -*- coding: utf-8 -*-
"""tune_judge.py — 自进化自动调参第一步（2026-08-27）。

judge 蒸馏阈值自动 A/B：候选阈值（0.5/0.55/0.6/0.7）→ 同一查询集跑 reason
→ 按"结果非空率不降 + LLM 调用最少"选优 → 持久化推荐到
~/.trinity/tuned_config.json（应用方读取）。
用法: python scripts/tune_judge.py [--queries 12]
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
    "上架作业 扫码 库位", "波次拣货 分配", "运费模板 配置 区域",
    "盘点差异 处理", "计费 按件 按重量", "退货 审批 权限",
    "扫码确认 库位 上架", "拣货 波次 分配 规则", "物流 运费 模板",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=12, help="要测试的查询数量（默认12）")
    ap.add_argument("--param", default="threshold", choices=["threshold", "top_k"], help="要调优的参数")
    args = ap.parse_args()
    import trinity.core.client._pagetree as PT
    from trinity import Trinity
    mem = Trinity(adapter="sqlite")
    queries = _QUERIES[: args.queries]
    results = {}
    if args.param == "threshold":
        cands_p = ("0.5", "0.55", "0.6", "0.7")
    else:
        cands_p = (3, 5, 10)
    for cv in cands_p:
        PT._JUDGE_CACHE.clear()
        PT._JUDGE_CACHE_TS.clear()
        PT._JUDGE_LLM_CALLS = 0
        if args.param == "threshold":
            os.environ["TRINITY_JUDGE_THRESHOLD"] = str(cv)
        else:
            os.environ.pop("TRINITY_JUDGE_THRESHOLD", None)
        hits = 0
        _tk = int(cv) if args.param == "top_k" else 5
        for q in queries:
            try:
                r = mem.search(query=q, mode="reason", top_k=_tk)
                if r.get("results"):
                    hits += 1
            except Exception:
                pass
        results[str(cv)] = {"hits": hits, "llm_calls": PT._JUDGE_LLM_CALLS,
                            "hit_rate": round(hits / max(1, len(queries)), 3)}
        print(f"param={args.param} value={cv} hits={hits}/{len(queries)} llm={PT._JUDGE_LLM_CALLS}")
    # 选优：hit_rate 最高的组里 LLM 最少
    best_rate = max(v["hit_rate"] for v in results.values())
    cands = {k: v for k, v in results.items() if v["hit_rate"] >= best_rate - 0.05}
    best = min(cands, key=lambda k: cands[k]["llm_calls"])
    rec = {"tuned_param": args.param, "recommended_" + args.param: best,
           "results": results,
           "updated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S")}
    out = os.path.expanduser("~/.trinity/tuned_config.json")
    # 2026-08-27: merge old recommendations
    try:
        import json as _j
        _old = _j.load(open(out, encoding="utf-8-sig")) if os.path.exists(out) else {}
        if isinstance(_old, dict):
            for _k in ("recommended_threshold", "recommended_top_k"):
                if _k not in rec and _k in _old:
                    rec[_k] = _old[_k]
    except Exception:
        pass
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    print("recommended:", best, "->", out)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
