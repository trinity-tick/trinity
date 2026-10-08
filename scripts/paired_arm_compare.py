#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两臂**配对**比较（McNemar 精确检验 + Wilson 区间）—— 不许只比点值。

## 为什么需要它

`doc_retrieval_eval.py` 的 `summary` 只给每臂的 R@k 点值。但 A2 与 B2 跑的是
**同一批题**（配对设计），逐题成败**相关** ⇒ 拿两个独立比例的区间去比会低估功效、
也答不出"差异是否在噪声内"。本工具从评测产物的 `results` 里取逐题命中，
做**配对**分析：

- **McNemar 精确检验**：只看**不一致对**（A2 中 B2 不中 = b；A2 不中 B2 中 = c），
  在 H0（两臂等价）下 b ~ Binomial(b+c, 0.5)。p 值即双侧精确值。
- **Wilson 95% 区间**：给出每臂自身的区间（小样本/接近 0/1 时比正态近似稳）。
  （与 `scripts/cold_candidate_ab.py::wilson_ci` 同式；此处独立实现以避免
  跨脚本 import 的路径耦合，公式一致。）

## 判据（可失败）
- `b+c == 0` ⇒ **INCONCLUSIVE**（两臂逐题完全一致，无法判定，不得当作"无差异"）；
- p >= 0.05 ⇒ **不显著**，不得据此改默认；
- p < 0.05 且方向为 B2 更优 ⇒ 支持改默认（仍需看效应量）。

用法：
    python scripts/paired_arm_compare.py --artifact output/doc_retrieval_eval_XXX.json \
        --arm-a A2 --arm-b B2 --k 10
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
import logging

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/paired_arm_compare.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def mcnemar_exact(b: int, c: int) -> float:
    """双侧精确 McNemar：不一致对 (b, c)，H0 下 b ~ Binomial(b+c, 0.5)。纯函数、可单测。"""
    n = b + c
    if n <= 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2.0 ** n)
    return min(1.0, 2.0 * tail)


def wilson_ci(hits: int, n: int, z: float = 1.96) -> "tuple[float, float]":
    """Wilson 区间（与 cold_candidate_ab.wilson_ci 同式）。纯函数、可单测。"""
    if n <= 0:
        return (0.0, 0.0)
    p = hits / float(n)
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (round(max(0.0, (c - h) / d), 4), round(min(1.0, (c + h) / d), 4))


def hit(page_list, target: str, k: int) -> int:
    """逐题是否命中（与评测脚本同口径：文档级去重后的前 k）。纯函数、可单测。"""
    seen, ded = set(), []
    for p in page_list or []:
        if p and p not in seen:
            seen.add(p)
            ded.append(p)
    return 1 if target in ded[:k] else 0


def compare(results: list, arm_a: str, arm_b: str, k: int = 10) -> dict:
    b = c = both = neither = 0
    a_hits = b_hits = 0
    for r in results or []:
        if arm_a not in r or arm_b not in r:
            continue
        ta, tb = r["target"], None
        ha = hit((r[arm_a] or {}).get("pages"), r["target"], k)
        hb = hit((r[arm_b] or {}).get("pages"), r["target"], k)
        a_hits += ha
        b_hits += hb
        if ha and hb:
            both += 1
        elif ha and not hb:
            b += 1
        elif hb and not ha:
            c += 1
        else:
            neither += 1
    n = both + b + c + neither
    out = {"arm_a": arm_a, "arm_b": arm_b, "k": k, "n_paired": n,
           "both_hit": both, "a_only": b, "b_only": c, "neither": neither,
           "a_hits": a_hits, "b_hits": b_hits,
           "a_R@k": round(a_hits / n, 4) if n else 0.0,
           "b_R@k": round(b_hits / n, 4) if n else 0.0}
    if n:
        out["a_wilson95"] = list(wilson_ci(a_hits, n))
        out["b_wilson95"] = list(wilson_ci(b_hits, n))
    out["delta_b_minus_a"] = round(out["b_R@k"] - out["a_R@k"], 4)
    return out


def judge(cmp_res: dict) -> dict:
    """判定。**可失败**：不一致对为 0 ⇒ INCONCLUSIVE；p>=0.05 ⇒ 不显著。纯函数、可单测。"""
    b, c = cmp_res.get("a_only", 0), cmp_res.get("b_only", 0)
    if b + c == 0:
        return {"verdict": "INCONCLUSIVE",
                "why": "两臂逐题完全一致（不一致对 0）⇒ 无法判定，**不得当作无差异**"}
    p = mcnemar_exact(b, c)
    res = dict(cmp_res, mcnemar_p=round(p, 6), discordant=b + c)
    if p >= 0.05:
        return dict(res, verdict="NOT_SIGNIFICANT",
                    why="McNemar p=%.4f >= 0.05（不一致对 %d）⇒ 差异在噪声内，**不得据此改默认**"
                        % (p, b + c))
    better = cmp_res["arm_b"] if c > b else cmp_res["arm_a"]
    return dict(res, verdict="SIGNIFICANT",
                why="McNemar p=%.6f < 0.05（不一致对 %d：%s 独中 %d / %s 独中 %d）⇒ %s 显著更优；"
                    "ΔR@%d=%+.4f" % (p, b + c, cmp_res["arm_a"], b, cmp_res["arm_b"], c,
                                     better, cmp_res["k"], cmp_res["delta_b_minus_a"]))


def load_results(artifact: str) -> list:
    d = json.load(open(artifact, encoding="utf-8-sig"))
    return d.get("results") if isinstance(d, dict) else d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", default="", help="评测产物 JSON；空=取 output/ 下最新一个")
    ap.add_argument("--arm-a", default="A2")
    ap.add_argument("--arm-b", default="B2")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    art = a.artifact
    if not art:
        cands = sorted(glob.glob(os.path.join(ROOT, "output", "doc_retrieval_eval*.json")),
                       key=os.path.getmtime)
        if not cands:
            print("找不到评测产物；先跑 scripts/doc_retrieval_eval.py")
            return 2
        art = cands[-1]
    res = load_results(art)
    if not res:
        print("产物里没有 results 字段 —— 无法做配对分析：%s" % art)
        return 2
    cmp_res = compare(res, a.arm_a, a.arm_b, a.k)
    j = judge(cmp_res)
    if a.json:
        print(json.dumps(j, ensure_ascii=False, indent=1))
    else:
        print("== 两臂配对比较（McNemar 精确检验）==")
        print("产物：%s" % os.path.basename(art))
        print("配对题数 %d；k=%d" % (cmp_res["n_paired"], cmp_res["k"]))
        print()
        print("%-8s %-10s %-22s %s" % ("臂", "R@k", "Wilson 95%", "命中"))
        for arm, key in ((a.arm_a, "a"), (a.arm_b, "b")):
            print("%-8s %-10s %-22s %s" % (arm, cmp_res[key + "_R@k"],
                                           str(cmp_res.get(key + "_wilson95")),
                                           cmp_res[key + "_hits"]))
        print()
        print("2x2：双中 %d ｜ %s 独中 %d ｜ %s 独中 %d ｜ 双不中 %d"
              % (cmp_res["both_hit"], a.arm_a, cmp_res["a_only"],
                 a.arm_b, cmp_res["b_only"], cmp_res["neither"]))
        print("ΔR@%d（%s − %s）= %+.4f" % (a.k, a.arm_b, a.arm_a, cmp_res["delta_b_minus_a"]))
        print()
        print("判定：%s —— %s" % (j["verdict"], j["why"]))
    return 0 if j["verdict"] != "INCONCLUSIVE" else 2


if __name__ == "__main__":
    sys.exit(main())
