#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确认区别特征：B2 的相对增益是否随**文档 chunk 数**上升（统计检验，不是比均值）。

## 要回答的问题

2026-10-05 的文档级切分发现：n=548 的汇总 Δ=+0.0985 掩盖了异质性——
42 个文档上 +16pp（显著），另 41 个文档上 +2pp（不显著）。
归因时看到「增益大的那半平均 chunk 数 23.3 vs 17.2」，但这只是**比均值**，
**没有任何显著性**，不能当结论。

本工具把它做成**可失败的判据**：
1. 把每道题映射到其 target 文档的 chunk 数（来自 DB，只读）；
2. 按 chunk 数**中位数/三分位**切组；
3. 每组内做 A2 vs B2 的**配对 McNemar**；
4. 用**文档级 bootstrap**（重采样文档、非题目）给出「高碎片组 Δ − 低碎片组 Δ」的 95% 区间。

## 判据（可失败）
- 两组 Δ 之差的 95% bootstrap 区间**不含 0** ⇒ 该区别特征成立；
- 含 0 ⇒ **不成立**，不得据此设计选择性启用；
- 若高碎片组本身不显著 ⇒ 假设同样不成立。

## 为什么 bootstrap 要按**文档**重采样
题目在同一文档内**不独立**（同一文档的 chunk 共享词汇与结构）。按题目重采样会低估方差、
把"文档间差异"误当"题目间差异"⇒ 假显著。按文档整簇重采样才对。

## 纪律：只读，不写库、不改任何既有脚本。
用法：
    python scripts/chunk_heterogeneity_test.py --artifact output/doc_retrieval_eval_n548_*.json
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import random
import sqlite3
import sys
from collections import defaultdict
import logging

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/chunk_heterogeneity_test.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOT = 2000
SEED = 20261005


def _load_pac():
    path = os.path.join(ROOT, "scripts", "paired_arm_compare.py")
    spec = importlib.util.spec_from_file_location("paired_arm_compare", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["paired_arm_compare"] = mod
    spec.loader.exec_module(mod)
    return mod


def chunk_counts(store: str, persona: str) -> dict:
    """doc 基名 -> chunk 数（只读）。"""
    con = sqlite3.connect("file:%s?mode=ro" % store.replace("\\", "/"), uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=55000")
    out = {}
    try:
        for rel, n in con.execute(
                "SELECT json_extract(metadata,'$.source_file') AS rel, COUNT(*) "
                "FROM memories WHERE persona_id=? AND status='active' "
                "AND json_extract(metadata,'$.source_file') IS NOT NULL GROUP BY rel",
                (persona,)):
            out[os.path.basename(str(rel).replace("\\", "/"))] = int(n or 0)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/chunk_heterogeneity_test.py::chunk_counts")
    con.close()
    return out


def median(xs: list) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def split_by_chunks(results: list, counts: dict, mode: str = "median") -> dict:
    """按 target 文档 chunk 数把**文档**切两组，再分配题目。纯函数、可单测。

    - `median`：低于中位数 = low，其余 = high；
    - `tertile`：最低 1/3 = low，最高 1/3 = high（中间 1/3 丢弃）。
    没有 chunk 信息的文档**整篇丢弃**（不猜），并计数。
    """
    by_doc = defaultdict(list)
    for r in results or []:
        by_doc[r.get("target")].append(r)
    known = {d: counts[d] for d in by_doc if d in counts}
    missing = len(by_doc) - len(known)
    if not known:
        return {"low": [], "high": [], "missing_docs": missing, "threshold": None}
    vals = sorted(known.values())
    if mode == "tertile":
        k = max(1, len(vals) // 3)
        lo_cut, hi_cut = vals[k - 1], vals[-k]
        low_docs = [d for d, c in known.items() if c <= lo_cut]
        high_docs = [d for d, c in known.items() if c >= hi_cut]
        thr = {"lo_cut": lo_cut, "hi_cut": hi_cut}
    else:
        m = median(vals)
        low_docs = [d for d, c in known.items() if c < m]
        high_docs = [d for d, c in known.items() if c >= m]
        thr = {"median": m}
    out = {"low": [r for d in low_docs for r in by_doc[d]],
           "high": [r for d in high_docs for r in by_doc[d]],
           "missing_docs": missing, "threshold": thr,
           "low_docs": len(low_docs), "high_docs": len(high_docs)}
    return out


def _delta(pac, results, a, b, k) -> "tuple[float, int]":
    c = pac.compare(results, a, b, k)
    return c["delta_b_minus_a"], c["n_paired"]


def bootstrap_delta_diff(pac, low, high, counts, a, b, k, n_boot: int = BOOT,
                         seed: int = SEED) -> dict:
    """**按文档**整簇重采样，给出 (Δ_high − Δ_low) 的 95% 区间。"""
    def by_doc(rs):
        d = defaultdict(list)
        for r in rs:
            d[r.get("target")].append(r)
        return d
    dl, dh = by_doc(low), by_doc(high)
    low_docs, high_docs = sorted(dl), sorted(dh)
    if not low_docs or not high_docs:
        return {"error": "empty group"}
    rnd = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        sl = [r for _ in low_docs for r in dl[rnd.choice(low_docs)]]
        sh = [r for _ in high_docs for r in dh[rnd.choice(high_docs)]]
        d_l, _ = _delta(pac, sl, a, b, k)
        d_h, _ = _delta(pac, sh, a, b, k)
        diffs.append(d_h - d_l)
    diffs.sort()
    lo = diffs[int(0.025 * n_boot)]
    hi = diffs[int(0.975 * n_boot) - 1]
    return {"diff_point": None, "ci95": [round(lo, 4), round(hi, 4)],
            "boot_mean": round(sum(diffs) / len(diffs), 4), "n_boot": n_boot,
            "excludes_zero": (lo > 0) or (hi < 0)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", default="")
    ap.add_argument("--persona", default="trinity-docs")
    ap.add_argument("--arm-a", default="A2")
    ap.add_argument("--arm-b", default="B2")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--mode", default="median", choices=["median", "tertile"])
    ap.add_argument("--boot", type=int, default=BOOT)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    art = a.artifact
    if not art:
        c = sorted(glob.glob(os.path.join(ROOT, "output", "doc_retrieval_eval_n548*.json")),
                   key=os.path.getmtime)
        if not c:
            print("找不到 n548 产物")
            return 2
        art = c[-1]
    res = json.load(open(art, encoding="utf-8-sig"))
    results = res.get("results") if isinstance(res, dict) else res
    store = os.environ.get("TRINITY_STORE") or os.path.join(
        os.path.expanduser("~"), ".trinity", "store-restored", "trinity_store.db")
    if not os.path.isfile(store):
        store = os.path.join(store, "trinity_store.db")
    pac = _load_pac()
    counts = chunk_counts(store, a.persona)

    g = split_by_chunks(results, counts, a.mode)
    out = {"artifact": os.path.basename(art), "mode": a.mode,
           "threshold": g["threshold"], "missing_docs": g["missing_docs"],
           "low_docs": g["low_docs"], "high_docs": g["high_docs"], "arms": [a.arm_a, a.arm_b]}
    for name in ("low", "high"):
        c = pac.compare(g[name], a.arm_a, a.arm_b, a.k)
        j = pac.judge(c)
        out[name] = {"queries": c["n_paired"], "a_R@k": c["a_R@k"], "b_R@k": c["b_R@k"],
                     "delta": c["delta_b_minus_a"], "verdict": j["verdict"],
                     "p": j.get("mcnemar_p"), "a_only": c["a_only"], "b_only": c["b_only"]}
    out["observed_diff"] = round(out["high"]["delta"] - out["low"]["delta"], 4)
    out["bootstrap"] = bootstrap_delta_diff(pac, g["low"], g["high"], counts,
                                            a.arm_a, a.arm_b, a.k, a.boot)
    excl = out["bootstrap"].get("excludes_zero")
    hi_sig = out["high"]["verdict"] == "SIGNIFICANT"
    if not hi_sig:
        out["verdict"] = "HYPOTHESIS_REJECTED"
        out["conclusion"] = "高碎片组本身不显著 ⇒ 区别特征不成立"
    elif excl:
        out["verdict"] = "HYPOTHESIS_SUPPORTED"
        out["conclusion"] = ("两组 Δ 之差 95%% 区间不含 0 ⇒ 「chunk 数越多、B2 相对增益越大」**成立**"
                             "⇒ 可设计按碎片化程度选择性启用")
    else:
        out["verdict"] = "INCONCLUSIVE"
        out["conclusion"] = "两组 Δ 之差 95%% 区间含 0 ⇒ 该区别特征**未获支持**，不得据此设计选择性启用"

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print("== chunk 数异质性检验（统计，不是比均值） ==")
        print("产物 %s ｜ %s vs %s ｜ k=%d ｜ 切分 %s" % (out["artifact"], a.arm_a, a.arm_b,
                                                       a.k, a.mode))
        print("阈值 %s；无 chunk 信息而丢弃的文档 %d" % (out["threshold"], out["missing_docs"]))
        print()
        print("%-8s %-7s %-8s %-10s %-10s %-10s %-10s" %
              ("组", "文档", "题数", "A2 R@k", "B2 R@k", "Δ", "McNemar p"))
        for name in ("low", "high"):
            v = out[name]
            print("%-8s %-7d %-8d %-10s %-10s %-10s %-10s" %
                  (name, g[name + "_docs"], v["queries"], v["a_R@k"], v["b_R@k"],
                   "%+.4f" % v["delta"], v["p"]))
        print()
        b = out["bootstrap"]
        print("Δ(high) − Δ(low) 观测 = %+.4f" % out["observed_diff"])
        print("文档级 bootstrap（%d 次，按**文档**整簇重采样）95%% 区间 = %s（boot 均值 %s）"
              % (b.get("n_boot"), b.get("ci95"), b.get("boot_mean")))
        print("区间是否不含 0：%s" % b.get("excludes_zero"))
        print()
        print("判定：%s —— %s" % (out["verdict"], out["conclusion"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
