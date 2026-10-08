#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档级切分复现 + 异质性归因（B2 的增益是否只在部分文档上成立）。

## 为什么需要它（2026-10-05 的关键发现）

n=548 的**汇总**配对检验给出 ΔR@10=+0.0985、p=2e-6，看起来很强。
但按 **target 文档** md5 奇偶切两半后：
  · 半 A（42 文档 / 307 题）：Δ=**+0.1596**，SIGNIFICANT
  · 半 B（41 文档 / 241 题）：Δ=+0.0207，**NOT_SIGNIFICANT（p=0.583）**
⇒ **汇总指标掩盖了效应异质性**；盲目全局替换会让近一半文档几乎无益却承担改动风险。

本工具把该切分**固化成可复跑判据**，并进一步**归因**：
比较两半文档的可测特征（chunk 数 / 平均正文长度 / 章节数），看增益是否与它们相关。

## 判据（可失败）
- 两半的 ΔR@10 **差异显著与否**都要报；**只有两半都显著**才支持"普适增益"。
- 任一半不显著 ⇒ 结论为 **HETEROGENEOUS**，**禁止**据此改默认。

## 纪律：只读，不写库。
用法：
    python scripts/doc_split_replication.py --artifact output/doc_retrieval_eval_n548_*.json
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from collections import defaultdict
import logging

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/doc_split_replication.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_pac():
    path = os.path.join(ROOT, "scripts", "paired_arm_compare.py")
    spec = importlib.util.spec_from_file_location("paired_arm_compare", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["paired_arm_compare"] = mod
    spec.loader.exec_module(mod)
    return mod


def split_by_target(results: list) -> dict:
    """按 target 的 md5 奇偶切两半；**同一文档只落一半**。纯函数、可单测。"""
    halves = {"even": [], "odd": []}
    for r in results or []:
        h = int(hashlib.md5(str(r.get("target")).encode("utf-8")).hexdigest(), 16) % 2
        halves["even" if h == 0 else "odd"].append(r)
    return halves


def docs_of(results: list) -> set:
    return {r.get("target") for r in (results or [])}


def doc_features(store: str, persona: str) -> dict:
    """每篇文档的可测特征：chunk 数 / 平均正文长度（只读）。"""
    con = sqlite3.connect("file:%s?mode=ro" % store.replace("\\", "/"), uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=55000")
    feats = {}
    try:
        rows = con.execute(
            "SELECT json_extract(metadata,'$.source_file') AS rel, COUNT(*) n, "
            "AVG(LENGTH(COALESCE(content,''))) L "
            "FROM memories WHERE persona_id=? AND status='active' "
            "AND json_extract(metadata,'$.source_file') IS NOT NULL GROUP BY rel",
            (persona,)).fetchall()
        for rel, n, L in rows:
            feats[os.path.basename(str(rel).replace("\\", "/"))] = {
                "chunks": int(n or 0), "avg_len": round(float(L or 0), 1)}
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/doc_split_replication.py::doc_features")
    con.close()
    return feats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", default="")
    ap.add_argument("--persona", default="trinity-docs")
    ap.add_argument("--arm-a", default="A2")
    ap.add_argument("--arm-b", default="B2")
    ap.add_argument("--k", type=int, default=10)
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
    pac = _load_pac()
    store = os.environ.get("TRINITY_STORE") or os.path.join(
        os.path.expanduser("~"), ".trinity", "store-restored", "trinity_store.db")
    if not os.path.isfile(store):
        store = os.path.join(store, "trinity_store.db")
    feats = doc_features(store, a.persona)

    halves = split_by_target(results)
    out = {"artifact": os.path.basename(art), "arm_a": a.arm_a, "arm_b": a.arm_b,
           "k": a.k, "halves": {}}
    for name in ("even", "odd"):
        cmp_res = pac.compare(halves[name], a.arm_a, a.arm_b, a.k)
        j = pac.judge(cmp_res)
        ds = docs_of(halves[name])
        agg = {"docs": len(ds), "queries": cmp_res["n_paired"],
               "delta": cmp_res["delta_b_minus_a"], "verdict": j["verdict"],
               "p": j.get("mcnemar_p"), "a_only": cmp_res["a_only"], "b_only": cmp_res["b_only"]}
        ch = [feats[d]["chunks"] for d in ds if d in feats]
        ln = [feats[d]["avg_len"] for d in ds if d in feats]
        agg["mean_chunks"] = round(sum(ch) / len(ch), 1) if ch else None
        agg["mean_avg_len"] = round(sum(ln) / len(ln), 1) if ln else None
        agg["docs_with_features"] = len(ch)
        out["halves"][name] = agg
    # 判据：两半都显著才算"普适"
    both = all(out["halves"][h]["verdict"] == "SIGNIFICANT" for h in ("even", "odd"))
    out["overlap_docs"] = len(docs_of(halves["even"]) & docs_of(halves["odd"]))
    out["verdict"] = "UNIFORM" if both else "HETEROGENEOUS"
    out["conclusion"] = ("两半都显著 ⇒ 支持普适增益" if both else
                         "**至少一半不显著 ⇒ 效应异质，禁止据此改默认**")

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print("== 文档级切分复现 + 异质性归因 ==")
        print("产物 %s ｜ %s vs %s ｜ k=%d" % (out["artifact"], a.arm_a, a.arm_b, a.k))
        print("两半文档交集 %d（必须 0）" % out["overlap_docs"])
        print()
        print("%-7s %-7s %-8s %-10s %-9s %-12s %-12s" %
              ("半", "文档", "题数", "ΔR@k", "判定", "平均chunk数", "平均正文长"))
        for h in ("even", "odd"):
            v = out["halves"][h]
            print("%-7s %-7d %-8d %-10s %-9s %-12s %-12s" %
                  (h, v["docs"], v["queries"], "%+.4f" % v["delta"], v["verdict"],
                   v["mean_chunks"], v["mean_avg_len"]))
        print()
        print("总体判定：%s —— %s" % (out["verdict"], out["conclusion"]))
        print("（p 值：even=%s  odd=%s）"
              % (out["halves"]["even"]["p"], out["halves"]["odd"]["p"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
