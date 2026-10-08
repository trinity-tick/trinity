#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""遗留①② A/B：作用域**欠量补齐**（`TRINITY_SCOPED_TOPUP`）在 REST 同款方法上的效果。

## 为什么在这个层面的测

上一轮的 A/B 走的是引擎 worker（`search(mode="hybrid")`），而那条路**自带 FTS 兜底**
（`_search.py:221-230`）⇒ 把我要测的效应盖住了。本次直接调
**`Trinity.search_hybrid(...)`** —— 也就是 REST `/memory/search/hybrid` 调的那个方法
（`_routers_search.py:86`），**没有兜底**，因此能干净地隔离"补齐"这一个变量。

## 单变量

同一份代码、同一批查询、同一 top_k，唯一变量 = `TRINITY_SCOPED_TOPUP`（off/on）。
每个臂在**独立子进程**里跑（env 必须在 import 前生效）。

用法：
    python dsh-ops/_v3_topup_ab.py --arms off,on --top-k 10
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLD = os.path.join(ROOT, "eval", "doc_golden_set.json")
PERSONA = "trinity-docs"


def basename(p):
    try:
        return os.path.basename(str(p).replace("\\", "/")) if p else None
    except Exception:  # noqa: BLE001
        return None


def _arm_worker(arm: str, top_k: int, golden: str = GOLD) -> dict:
    """在子进程里跑一个臂（本函数被 `--arm` 复用时执行真正的测量）。"""
    sys.path.insert(0, ROOT)
    items = json.load(open(golden, encoding="utf-8"))
    items = items if isinstance(items, list) else items.get("items", [])
    from trinity import Trinity  # noqa: E402

    mem = Trinity(adapter="postgresql")
    pages_list, returned, lat, topup_seen, topup_added = [], [], [], 0, 0
    fusion_counts, topup_counts = [], []
    for it in items:
        t0 = time.perf_counter()
        try:
            res = mem.search_hybrid(it["query"], top_k=top_k, persona_id=PERSONA)
        except Exception as e:  # noqa: BLE001
            res = {"results": [], "error": str(e)[:80]}
        lat.append((time.perf_counter() - t0) * 1000.0)
        hits = res.get("results") or []
        pgs = [basename((h.get("metadata") or {}).get("source_file")
                        or (h.get("metadata") or {}).get("source_uri")
                        or h.get("source_uri")) for h in hits]
        # 2026-10-05（Step 2）：把「融合命中」与「补齐命中」分开记。
        # 这是让「R@10 涨了、精确率同时掉了」**能被看见**的唯一途径：
        # 旧版只记 R@k 与条数，而单目标题集下补位只会多给「命中机会」、
        # 从不暴露「每补一条就多一条错」的代价（实测 n=3：补 8 条，R@10 一点没动）。
        n_topup = sum(1 for h in hits if isinstance(h, dict) and h.get("_scoped_topup"))
        pages_list.append(pgs)
        fusion_counts.append(len(hits) - n_topup)
        topup_counts.append(n_topup)
        returned.append(len(pgs))
        if res.get("scoped_topup"):
            topup_seen += 1
            topup_added += int(res["scoped_topup"].get("added") or 0)

    def rec(k):
        return round(sum(1 for pgs, it in zip(pages_list, items)
                         if it["target"] in pgs[:k]) / len(items), 4)

    ls = sorted(lat)
    # 逐题明细：供**分布与分层**分析（§783.6 遗留③：只报均值不足以说明代表性）
    per_item = []
    for it, pgs, fc, tc in zip(items, pages_list, fusion_counts, topup_counts):
        rank = (pgs.index(it["target"]) + 1) if it["target"] in pgs else None
        per_item.append({"id": it["id"], "rank": rank, "returned": len(pgs),
                         "qlen": len(it["query"]), "target": it["target"],
                         "fusion_count": fc, "topup_count": tc})
    metrics = rank_metrics(per_item, top_k)
    return {
        "arm": arm, "n": len(items),
        "avg_returned": round(sum(returned) / len(items), 2),
        "empty_queries": sum(1 for x in returned if x == 0),
        "full_queries": sum(1 for x in returned if x >= top_k),
        "topup_seen": topup_seen, "topup_added": topup_added,
        "fusion_count_mean": round(sum(fusion_counts) / len(items), 2),
        "topup_count_mean": round(sum(topup_counts) / len(items), 2),
        "R@1": rec(1), "R@5": rec(5), "R@10": rec(10),
        **metrics,
        "found_any_rank": sum(1 for p in per_item if p["rank"]),
        "per_item": per_item,
        "latency_ms_p50": round(ls[len(ls) // 2], 1) if ls else None,
        "latency_ms_p95": round(ls[min(len(ls) - 1, int(len(ls) * 0.95))], 1) if ls else None,
    }


# ---- Step 2（2026-10-05）：排序与**稀释**指标 ---------------------------------
#
# 为什么必须补这一组：题集每题只有**一个** target（`eval/doc_golden_set*.json`
# 的键就是 id/type/query/target）。在这个口径下：
#   * 补齐**只会**增加「target 出现在 top-k 里」的机会 ⇒ R@k 天然只升不降；
#   * 每补进来一条**不是** target 的条目，都是实打实的错（稀释），而 R@k 看不见。
# 所以旧口径（只有 R@k + 条数）在数学上**不可能**量到补齐的代价。
# 这正是 SCORES.json 里 `docs_corpus_auto_topup_on_20260916` 只报
# 「ΔR@10=+0.2167、空结果 65/120→1/120」的原因 —— 收益被记下了，代价没有。

def ndcg_at_k(per_item: list, k: int) -> float:
    """单目标二值相关的 nDCG@k：= 1/log2(rank+1)（rank<=k），否则 0。

    IDCG = 1（每题只有一个相关项），故无需再除。纯函数、可单测。
    与 `benchmark/memarena/metrics.py::NDCG` 同义（此处按单目标简化，避免依赖注入）。
    """
    if not per_item:
        return 0.0
    import math
    tot = 0.0
    for p in per_item:
        r = p.get("rank")
        if r and 1 <= r <= k:
            tot += 1.0 / math.log2(r + 1)
    return round(tot / len(per_item), 4)


def precision_at_k(per_item: list, k: int) -> float:
    """precision@k = 命中数 / k（单目标：命中即 1/k）。

    **这条就是「稀释」的度量**：同一批查询，返回条数越多、命中不变 ⇒ 它单调下降。
    """
    if not per_item or k <= 0:
        return 0.0
    hit = sum(1 for p in per_item if p.get("rank") and p["rank"] <= k)
    return round(hit / (len(per_item) * k), 4)


def rank_metrics(per_item: list, top_k: int) -> dict:
    """汇总排序敏感 + 稀释指标。纯函数、可单测。"""
    return {
        "nDCG@5": ndcg_at_k(per_item, 5),
        "nDCG@10": ndcg_at_k(per_item, 10),
        "precision@5": precision_at_k(per_item, 5),
        "precision@10": precision_at_k(per_item, min(10, top_k) or 1),
    }


def topup_decomposition(per_item: list, k: int = 10) -> dict:
    """把补齐的**收益**与**代价**分开记 —— 本 Step 的核心判据。

    * `gained_queries`：target 只在**补齐段**里出现（rank > fusion_count）
      ⇒ 补齐**真的救回了**这一题（真收益）；
    * `diluted_queries`：target 本来就在融合段里，补齐又追加了 N 条无关项
      ⇒ **零召回收益、纯稀释**（代价）；`diluted_entries` 是追加的无关条数总量；
    * `missed_queries`：两段都没有 target。

    纯函数、可单测。
    """
    gained = diluted = missed = 0
    diluted_entries = 0
    for p in per_item:
        r = p.get("rank")
        fc = int(p.get("fusion_count") or 0)
        tc = int(p.get("topup_count") or 0)
        if not r or r > k:
            missed += 1
            continue
        if r > fc:
            gained += 1                    # 只在补齐段命中 ⇒ 真收益
        else:
            if tc > 0:
                diluted += 1               # 融合段已命中，补齐只追加了无关项
                diluted_entries += tc
    total_topup = sum(int(p.get("topup_count") or 0) for p in per_item)
    return {"gained_queries": gained, "diluted_queries": diluted,
            "diluted_entries": diluted_entries, "missed_queries": missed,
            "net_gain_queries": gained,
            # 自洽字段：`diluted_entries` 只统计「已命中且被稀释」的那部分，
            # 未命中题的补位不计入 ⇒ 必须另报总量，否则读者无法把
            # 引擎自报的 `scoped_topup.added` 与观测到的条数对上。
            # 实测（2026-10-05 冒烟 n=3）：引擎自报 added=8，而 diluted_entries=4
            # —— 差额 4 落在「未命中题」上，不是丢数据。
            "total_topup_entries_observed": total_topup,
            "topup_selfreport_vs_observed_delta": None}  # 由调用方填（需引擎自报值）


def distribution(off: dict, on: dict) -> dict:
    """逐题对比：改善/回退/持平 + 按 query 长度分层。

    回答 §783.6 遗留③ 的质疑："效应是不是被少数题驱动、auto 题集有没有代表性"。
    纯函数（可单测）。
    """
    o = {p["id"]: p for p in off.get("per_item") or []}
    n = {p["id"]: p for p in on.get("per_item") or []}

    def hit(p, k):
        return bool(p and p.get("rank") and p["rank"] <= k)

    imp = reg = same = 0
    strata = {"short(<=30)": [0, 0, 0], "mid(31-60)": [0, 0, 0], "long(>60)": [0, 0, 0]}
    for i in o:
        if i not in n:
            continue
        a, b = hit(o[i], 10), hit(n[i], 10)
        d = 1 if (b and not a) else (-1 if (a and not b) else 0)
        imp += 1 if d > 0 else 0
        reg += 1 if d < 0 else 0
        same += 1 if d == 0 else 0
        ql = n[i].get("qlen") or 0
        key = "short(<=30)" if ql <= 30 else ("mid(31-60)" if ql <= 60 else "long(>60)")
        strata[key][0] += 1 if d > 0 else 0
        strata[key][1] += 1 if d < 0 else 0
        strata[key][2] += 1
    return {"improved": imp, "regressed": reg, "unchanged": same,
            "by_query_len": {k: {"improved": v[0], "regressed": v[1], "n": v[2]}
                             for k, v in strata.items()}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="off,on")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--arm", default="", help="内部用：在子进程里执行单个臂")
    ap.add_argument("--golden", default=GOLD, help="题集路径（默认手工 20 题 golden set）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.arm:
        print(json.dumps(_arm_worker(a.arm, a.top_k, a.golden), ensure_ascii=False))
        return 0

    rows = []
    for arm in [x.strip() for x in a.arms.split(",") if x.strip()]:
        env = {**os.environ, "TRINITY_SCOPED_TOPUP": arm, "PYTHONIOENCODING": "utf-8"}
        env.setdefault("TRINITY_ENGINE_MUTEX_NAME",
                       "Global\\TrinityEngineWorker_TopupAB_%s" % arm)
        r = subprocess.run([sys.executable, os.path.abspath(__file__),
                            "--arm", arm, "--top-k", str(a.top_k), "--golden", a.golden],
                           cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        line = [l for l in (r.stdout or "").splitlines() if l.strip().startswith("{")]
        if not line:
            print("臂 %s 失败：%s" % (arm, (r.stderr or "")[-300:]))
            return 1
        rows.append(json.loads(line[-1]))

    if a.json:
        print(json.dumps({"persona": PERSONA, "top_k": a.top_k, "arms": rows}, ensure_ascii=False))
        return 0

    print("== 作用域欠量补齐 A/B（persona=%s, top-k=%d, n=%d）==" % (PERSONA, a.top_k, rows[0]["n"]))
    print("%-6s %-11s %-9s %-9s %-9s %-7s %-7s %-7s %-8s %-8s" %
          ("臂", "平均返回", "空结果题", "满额题", "补入条数", "R@1", "R@5", "R@10", "p50ms", "p95ms"))
    for r in rows:
        print("%-6s %-11s %-9d %-9d %-9d %-7s %-7s %-7s %-8s %-8s" %
              (r["arm"], r["avg_returned"], r["empty_queries"], r["full_queries"],
               r["topup_added"], r["R@1"], r["R@5"], r["R@10"],
               r["latency_ms_p50"], r["latency_ms_p95"]))
    print()
    print("%-6s %-9s %-10s %-11s %-11s %-11s" %
          ("臂", "nDCG@5", "nDCG@10", "precision@5", "precision@10", "补入条数均值"))
    for r in rows:
        print("%-6s %-9s %-10s %-11s %-11s %-11s" %
              (r["arm"], r["nDCG@5"], r["nDCG@10"], r["precision@5"],
               r["precision@10"], r["topup_count_mean"]))
    off = next((r for r in rows if r["arm"] == "off"), None)
    on = next((r for r in rows if r["arm"] == "on"), None)
    if off and on:
        print()
        print("ΔR@10 = %+.4f（n=%d ⇒ %+.0f 题）；ΔR@1 = %+.4f" %
              (on["R@10"] - off["R@10"], off["n"], (on["R@10"] - off["R@10"]) * off["n"],
               on["R@1"] - off["R@1"]))
        print("ΔnDCG@5 = %+.4f；Δprecision@5 = %+.4f；Δprecision@10 = %+.4f" %
              (on["nDCG@5"] - off["nDCG@5"], on["precision@5"] - off["precision@5"],
               on["precision@10"] - off["precision@10"]))
        print("平均返回 %s → %s；空结果题 %d → %d；满额题 %d → %d" %
              (off["avg_returned"], on["avg_returned"], off["empty_queries"],
               on["empty_queries"], off["full_queries"], on["full_queries"]))
        print("延迟 p50 %s → %s ms（Δ%+.1f）；p95 %s → %s ms（Δ%+.1f）" %
              (off["latency_ms_p50"], on["latency_ms_p50"],
               (on["latency_ms_p50"] or 0) - (off["latency_ms_p50"] or 0),
               off["latency_ms_p95"], on["latency_ms_p95"],
               (on["latency_ms_p95"] or 0) - (off["latency_ms_p95"] or 0)))
        # ---- Step 2 核心判据：补齐的收益与代价分开报 ----
        d = topup_decomposition(on.get("per_item") or [], a.top_k)
        d["topup_selfreport_vs_observed_delta"] = int(on.get("topup_added") or 0) - \
            d["total_topup_entries_observed"]
        print()
        print("== 补齐的收益 / 代价分解（只在 on 臂有意义，判据 k=%d）==" % a.top_k)
        print("真收益（target 只在补齐段出现，补齐救回）: %d 题" % d["gained_queries"])
        print("纯稀释（融合段已命中，补齐只追加无关项）  : %d 题 / %d 条"
              % (d["diluted_queries"], d["diluted_entries"]))
        print("未命中（两段都没有 target）              : %d 题" % d["missed_queries"])
        print("补齐条目总量（观测）%d 条 vs 引擎自报 added=%s 条（Δ%+d）"
              % (d["total_topup_entries_observed"], on.get("topup_added"),
                 d["topup_selfreport_vs_observed_delta"]))
        gain_r10 = on["R@10"] - off["R@10"]
        if gain_r10 > 0 and d["gained_queries"] == 0:
            print("⚠ 判据警告：R@10 上升但**没有任何一题**由补齐救回 "
                  "⇒ 该增益来自「多给命中机会」而非补齐价值（口径缺陷，见本文件注释）")
        if d["gained_queries"] == 0 and d["diluted_entries"] > 0:
            print("⇒ 补齐在本口径下**零召回收益、纯稀释**：%d 条无关条目进入返回集"
                  % d["diluted_entries"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
