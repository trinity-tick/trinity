# -*- coding: utf-8 -*-
"""A2 检索通道自适应路由实验 — 对比各检索策略的延迟与结果差异。

对比维度（同一组查询）:
  - pool/hybrid : 聚合池 hybrid (keyword+vector+secondbrain)
  - pool/keyword: 聚合池 keyword
  - engine/fusion|rrf|cascade: 引擎 47 通道融合策略

产出: 每查询各策略延迟 + 结果 id 集合差异（Jaccard），输出路由建议表。

用法:
    python benchmark/adaptive_routing.py [--queries a,b,c] [--top-k 5]
"""
import argparse
import json
import sys
import time
import requests

API = "http://127.0.0.1:8001"
HEADERS = {"X-Agent-ID": "adaptive-routing", "X-Agent-Role": "admin"}

DEFAULT_QUERIES = [
    "彩棠派样仓最近发了多少订单",
    "GraphRAG 是什么",
    "WMS 上架策略",
    "供应链管理专业前景",
    "订单详情接口返回结构",
    "拼多多仓用哪家物流",
    "记忆压缩怎么做",
    "仓库布局怎么规划",
    "用户偏好深色模式",
    "SKU 编码规则",
]


def time_call(fn):
    t0 = time.perf_counter()
    try:
        data = fn()
        ok = True
    except Exception as exc:
        data, ok = {"error": str(exc)}, False
    return data, ok, (time.perf_counter() - t0) * 1000


def pool_search(q, mode, top_k):
    r = requests.get(f"{API}/agents/memory/search",
                     params={"q": q, "top_k": top_k, "mode": mode},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def engine_hybrid(q, strategy, top_k):
    r = requests.post(f"{API}/memory/search/hybrid",
                      json={"query": q, "top_k": top_k, "strategy": strategy},
                      headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def ids_of(data):
    res = data.get("results", data if isinstance(data, list) else [])
    return {m.get("memory_id") for m in res if m.get("memory_id")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", default=",".join(DEFAULT_QUERIES))
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()
    queries = [q.strip() for q in args.queries.split(",") if q.strip()]

    strategies = {
        "pool/hybrid": lambda q: pool_search(q, "hybrid", args.top_k),
        "pool/keyword": lambda q: pool_search(q, "keyword", args.top_k),
        "engine/fusion": lambda q: engine_hybrid(q, "fusion", args.top_k),
        "engine/rrf": lambda q: engine_hybrid(q, "rrf", args.top_k),
        "engine/cascade": lambda q: engine_hybrid(q, "cascade", args.top_k),
    }

    lat = {k: [] for k in strategies}
    sets = {k: set() for k in strategies}
    per_query = []

    for q in queries:
        row = {"query": q}
        for name, fn in strategies.items():
            data, ok, ms = time_call(lambda: fn(q))
            row[name] = {"ok": ok, "ms": round(ms, 1), "hits": len(ids_of(data)) if ok else 0}
            if ok:
                lat[name].append(ms)
                sets[name] |= ids_of(data)
        per_query.append(row)

    print(f"queries: {len(queries)} | top_k: {args.top_k}\n")
    print("== 延迟 (ms, 均值) & 命中率 ==")
    print(f"{'strategy':<16}{'mean_ms':>9}{'hit_ratio':>11}{'unique_ids':>12}")
    summary = {"queries": queries, "top_k": args.top_k, "strategies": {}}
    for name in strategies:
        mean = sum(lat[name]) / len(lat[name]) if lat[name] else -1
        hit = sum(1 for r in per_query if r[name]["ok"] and r[name]["hits"] > 0) / len(queries)
        print(f"{name:<16}{mean:>9.1f}{hit:>10.0%}{len(sets[name]):>12}")
        summary["strategies"][name] = {"mean_ms": round(mean, 1), "hit_ratio": hit, "unique_ids": len(sets[name])}

    print("\n== 策略两两 Jaccard 相似度（结果 id 集合）==")
    names = list(strategies)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = sets[names[i]], sets[names[j]]
            jac = len(a & b) / len(a | b) if (a | b) else 0
            print(f"  {names[i]} vs {names[j]}: {jac:.2f}")

    with open(r"C:\Users\Administrator\.trinity\bench-results\adaptive_routing.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "per_query": per_query}, f, ensure_ascii=False, indent=2)
    print("\nsaved -> .trinity/bench-results/adaptive_routing.json")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
