#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**注入路径**的 nDCG@5 / 精确率闸门 + 基线（Step 2 缺失的另一半）。

## 为什么必须单独做：注入路径与 top-up **不是同一条路**

2026-10-05 实测（源码级）：
- **注入路径**：`engine_worker._opening()` → `_search()` → **`engine.search(mode="hybrid")`**
  —— 这条路**自带 FTS 兜底**（`trinity/core/client/_search.py:221-230`）。
- **top-up**：住在 `Trinity.search_hybrid()`（REST `/memory/search/hybrid` 调的那个），
  **没有兜底**，`_scoped_topup` 只在这里生效。

⇒ 我在 Step 2 为 top-up 加的 nDCG/精确率判据**覆盖不到注入路径**。
本工具补的就是这一半，并把口径对齐 Step 2（同样用**单目标**题集、同样报
`precision@k` 与 `nDCG@k`、同样把"补位"的代价显式化）。

## 两个臂（都走**真实注入路径的取数**，只差装配方式）

- `naive`  ：直接把引擎返回的前 `top_k` 条注进去（"不做装配"的朴素做法）
- `surface`：走**真实装配** `build_opening_surface(...)`（含来源白名单、不可信过滤、冷槽位）

判据：装配**不该**让 R@k 上升而 precision@k 下降（那是"多塞几条换命中率"的补位特征，
与 Step 2 在 top-up 上抓到的同一形态）。

## 基线锁定与"能失败"的闸门

`--save-baseline` 落 `output/opening_injection_precision_baseline.json`；
`--gate` 与基线比对，出现下面任一情形即 **FAIL（rc=1）**：
  ① `precision@5` 低于基线 − `--tol`；
  ② `R@5` 上升**且** `precision@5` 下降（补位换命中率的特征）；
  ③ 基线缺失 ⇒ **INCONCLUSIVE（rc=2）**，绝不当作通过。

## 纪律
- **只读**：不改库、不改任何表；`TRINITY_AUTO_RECALL` 不被设置
  （本工具直接调装配函数，不经过 `_opening` 的门，所以**不影响生产行为**）。
- **§13.2**：取不到就**分原因计数**（引擎异常/形状不符），不合并成 `skipped`。
- 产物落 `output/`（§12）。

用法：
    python scripts/opening_injection_precision_audit.py                  # 只测量
    python scripts/opening_injection_precision_audit.py --save-baseline  # 落基线
    python scripts/opening_injection_precision_audit.py --gate           # 与基线比对
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import logging

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/opening_injection_precision_audit.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLD = os.path.join(ROOT, "eval", "doc_golden_set.json")
BASELINE = os.path.join(ROOT, "output", "opening_injection_precision_baseline.json")
PERSONA = "trinity-docs"


def ndcg_at_rank(rank, k: int) -> float:
    """单目标二值相关的 nDCG@k 单项贡献 = 1/log2(rank+1)（IDCG=1）。纯函数、可单测。"""
    if not rank or rank < 1 or rank > k:
        return 0.0
    return 1.0 / math.log2(rank + 1)


def precision_at_k(injected: list, target: str, k: int) -> float:
    """precision@k = 命中的相关条数 / k（单目标：命中即 1/k）。

    **这就是"补位"的代价度量**：注入条数越多而命中不变 ⇒ 它单调下降。
    与 Step 2 的 top-up 判据同一语义（同一把尺子）。
    """
    if k <= 0:
        return 0.0
    hit = sum(1 for x in (injected or [])[:k] if x == target)
    return hit / float(k)


def rank_of(injected: list, target: str):
    """目标在注入序列中的 1-based 位置；不在则 None。纯函数。"""
    for i, x in enumerate(injected or [], 1):
        if x == target:
            return i
    return None


def summarize(per_query: list, k: int) -> dict:
    """把一个臂的逐题结果汇总成 R@k / precision@k / nDCG@k。纯函数、可单测。

    ⚠ 指标键名**按 k 参数化**（`R@5` / `precision@5` …）。
    2026-10-05 实测代价：初版 `summarize` 发 `R@k`/`precision@k`，
    而 `judge` 读 `R@5`/`precision@5` ⇒ 两边都用 `.get(..., 0.0)` 兜底，
    于是**闸门拿到 0.0 vs 0.0、恒判 PASS** —— 一个永远绿的假闸门
    （与本仓 GuardianChain `validate()` 的恒真式同族）。
    参数化后键名**由 k 唯一决定**，两边不可能再对不上。
    """
    n = len(per_query)
    rk, pk, nk = "R@%d" % k, "precision@%d" % k, "nDCG@%d" % k
    if not n:
        return {"n": 0, rk: 0.0, pk: 0.0, nk: 0.0, "avg_injected": 0.0}
    hits = sum(1 for p in per_query if p.get("rank"))
    return {
        "n": n,
        rk: round(hits / n, 4),
        pk: round(sum(p.get("precision", 0.0) for p in per_query) / n, 4),
        nk: round(sum(p.get("ndcg", 0.0) for p in per_query) / n, 4),
        "avg_injected": round(sum(p.get("injected", 0) for p in per_query) / n, 2),
    }


def judge(candidate: dict, baseline: dict, tol: float = 0.0, k: int = 5) -> dict:
    """闸门判定。**可失败**：三条规则各自对应一个真实形态。纯函数、可单测。

    键名与 `summarize` 同源（都由 k 决定）；**禁止**用 `.get(key, 0.0)` 兜底缺键 ——
    那正是假绿的来源。缺键一律 INCONCLUSIVE。
    """
    rk, pk = "R@%d" % k, "precision@%d" % k
    if not baseline or rk not in baseline or pk not in baseline:
        return {"verdict": "INCONCLUSIVE",
                "why": "基线缺失或缺指标键（%s/%s）—— 不得当作通过（先跑 --save-baseline）"
                       % (rk, pk)}
    if rk not in candidate or pk not in candidate:
        return {"verdict": "INCONCLUSIVE",
                "why": "候选取不到指标键（%s/%s）—— 契约对不上，不得当作通过" % (rk, pk)}
    cp, bp = candidate[pk], baseline[pk]
    cr, br = candidate[rk], baseline[rk]
    if cr > br and cp < bp:
        return {"verdict": "FAIL",
                "why": "%s 上升且 %s 下降（%+.4f/%+.4f）⇒ 补位换命中率"
                       % (rk, pk, cr - br, cp - bp)}
    if cp < bp - tol:
        return {"verdict": "FAIL",
                "why": "%s %.4f 低于基线 %.4f − 容差 %.4f" % (pk, cp, bp, tol)}
    return {"verdict": "PASS", "why": "%s %.4f >= 基线 %.4f" % (pk, cp, bp)}


def _arm(name: str, top_k: int, golden: str, mode: str) -> dict:
    """在子进程里跑一个臂（由 `--arm` 复用）。"""
    sys.path.insert(0, ROOT)
    items = json.load(open(golden, encoding="utf-8"))
    items = items if isinstance(items, list) else items.get("items", [])
    from trinity import Trinity  # noqa: E402
    from trinity.bridges.opening_surface import build_opening_surface  # noqa: E402

    mem = Trinity(adapter="postgresql")
    per_query, errors = [], {"engine_error": 0, "shape_mismatch": 0, "empty": 0}
    cold_used = skipped_untrusted = skipped_source = 0
    for it in items:
        try:
            res = mem.search(it["query"], top_k=max(top_k * 3, 12), mode="hybrid",
                             persona_id=PERSONA)
            hits = (res or {}).get("results", res if isinstance(res, list) else [])
        except Exception:  # noqa: BLE001  §13.2 分原因计数
            errors["engine_error"] += 1
            continue
        if not isinstance(hits, list):
            errors["shape_mismatch"] += 1
            continue
        if not hits:
            errors["empty"] += 1

        def _id(h):
            return (h or {}).get("memory_id")

        if mode == "surface":
            surf = build_opening_surface(hits, opening_text=it["query"], top_k=top_k,
                                        skip_untrusted=True)
            injected = list(surf.get("delivered_ids") or [])
            cold_used += int(surf.get("cold_slots_used") or 0)
            skipped_untrusted += int(surf.get("skipped_untrusted") or 0)
            skipped_source += int(surf.get("skipped_source") or 0)
        else:
            injected = [_id(h) for h in hits[:top_k]]

        target = it["target"]
        # 目标比对：题集的 target 是**文档基名**，而注入的是 memory_id ⇒
        # 用 hits 的 metadata.source_file 反查目标对应的 memory_id（同一口径）
        wanted = set()
        for h in hits:
            meta = (h or {}).get("metadata") or {}
            src = meta.get("source_file") or meta.get("source_uri") or (h or {}).get("source_uri")
            base = os.path.basename(str(src).replace("\\", "/")) if src else None
            if base == target:
                wanted.add(_id(h))
        rank = next((i for i, x in enumerate(injected, 1) if x in wanted), None)
        per_query.append({
            "id": it["id"], "target": target, "injected": len(injected),
            "rank": rank,
            "precision": (1.0 / top_k) if rank else 0.0,
            "ndcg": ndcg_at_rank(rank, top_k),
        })
    out = {"arm": name, "mode": mode, "top_k": top_k,
           "errors": errors, "cold_slots_used": cold_used,
           "skipped_untrusted": skipped_untrusted, "skipped_source": skipped_source,
           "per_query": per_query}
    out.update(summarize(per_query, top_k))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=5, help="真实注入条数（_opening 默认 5）")
    ap.add_argument("--golden", default=GOLD)
    ap.add_argument("--arm", default="", help="内部用")
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--tol", type=float, default=0.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.arm:
        want = "surface" if a.arm == "surface" else "naive"
        print(json.dumps(_arm(a.arm, a.top_k, a.golden, want), ensure_ascii=False))
        return 0

    import subprocess
    rows = []
    for arm in ("naive", "surface"):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        env.setdefault("TRINITY_ENGINE_MUTEX_NAME",
                       "Global\\TrinityOpeningPrecision_%s" % arm)
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--arm", arm,
                            "--top-k", str(a.top_k), "--golden", a.golden],
                           cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        # 2026-10-06（t74/I14）：原为 `[l for l in …]`（E741 含糊变量名）⇒ 改名 `ln`（真修）。
        line = [ln for ln in (r.stdout or "").splitlines() if ln.strip().startswith("{")]
        if not line:
            print("臂 %s 失败：%s" % (arm, (r.stderr or "")[-300:]))
            return 2
        rows.append(json.loads(line[-1]))

    by = {r["arm"]: r for r in rows}
    result = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "top_k": a.top_k,
              "persona": PERSONA, "golden": os.path.basename(a.golden),
              "arms": by}
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print("== 注入路径 nDCG/精确率审计（Step 2 的另一半）==")
        print("题集 %s  top_k=%d（真实注入条数）  persona=%s"
              % (os.path.basename(a.golden), a.top_k, PERSONA))
        print("[采样时刻] %s" % result["ts"])
        print()
        print("%-9s %-8s %-12s %-10s %-12s %-10s" %
              ("臂", "R@%d" % a.top_k, "precision@%d" % a.top_k,
               "nDCG@%d" % a.top_k, "平均注入数", "冷槽位"))
        for arm in ("naive", "surface"):
            r = by[arm]
            print("%-9s %-8s %-12s %-10s %-12s %-10s" %
                  (arm, r["R@%d" % a.top_k], r["precision@%d" % a.top_k],
                   r["nDCG@%d" % a.top_k], r["avg_injected"], r["cold_slots_used"]))
        print()
        for arm in ("naive", "surface"):
            e = by[arm]["errors"]
            if any(e.values()):
                print("  %s 取数失败分档（§13.2 不合并）：%s" % (arm, e))
        print("装配过滤：skipped_untrusted=%d  skipped_source=%d"
              % (by["surface"]["skipped_untrusted"], by["surface"]["skipped_source"]))
        d = {m: round(by["surface"][m] - by["naive"][m], 4)
             for m in ("R@%d" % a.top_k, "precision@%d" % a.top_k, "nDCG@%d" % a.top_k)}
        print("装配 − 朴素：R@k %+.4f  precision@k %+.4f  nDCG@k %+.4f"
              % (d["R@%d" % a.top_k], d["precision@%d" % a.top_k], d["nDCG@%d" % a.top_k]))
        if d["R@%d" % a.top_k] > 0 and d["precision@%d" % a.top_k] < 0:
            print("⚠ 补位特征：R@k 上升而 precision@k 下降 ⇒ 用更多条目换命中率")

    if a.save_baseline:
        os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump({"surface": by["surface"], "naive": by["naive"],
                       "saved_at": result["ts"], "top_k": a.top_k},
                      fh, ensure_ascii=False, indent=1)
        print()
        print("基线已落：%s" % BASELINE)

    if a.gate:
        base = {}
        if os.path.exists(BASELINE):
            b = json.load(open(BASELINE, encoding="utf-8"))
            base = b.get("surface") or {}
        v = judge(by["surface"], base, a.tol, a.top_k)
        print()
        print("闸门判定：%s —— %s" % (v["verdict"], v["why"]))
        return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}[v["verdict"]]

    os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
    out = os.path.join(ROOT, "output", "opening_injection_precision_%s.json"
                       % time.strftime("%Y%m%d_%H%M%S"))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    print()
    print("产物：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
