#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""registry_freshness_gate.py — 「登记不许腐烂」门（2026-10-02 事故后建立）。

## 为什么要有这道门

本仓的治理强度很大（冻结门集、棘轮、撤回台账、SHA-256 绑定），但**登记文件本身没有棘轮** ——
于是它们会**静默过期**，而所有引用它们的人都会读到过期事实。已实测三例：

  1. `docs/EXTERNAL_VERIFICATION.md` 由 `scores_gate.py --emit-doc` 生成，
     实测只列 **9** 条，而 `docs/SCORES.json` 有 **25** 条（缺 2026-09-17 及以后全部）——
     外部复核包**少了一半以上的条目**。
  2. `docs/GATE_WIRING.json` 把 `doc_truth_gate.py` / `organ_freeze_gate.py` 记为
     `runs_red`（"本地就是红的"），而闸门集整跑里两者 `rc=0` —— 登记与事实**相反**。
  3. `~/.dsh/AGENTS.md` 的快照行（生成物）与 `docs/SCORES.json` 需人工对齐。

  共同点：**没有任何判据要求"登记 == 事实"**。这与本仓 G10 前科
  『恒红/不在集合里的闸门会被忽略』同型 —— 不是有人偷懒，而是**没有任何东西在读它**。

## 判据（全部是**集合关系**，不是数值阈值）

  1. `GATE_SET.json`：`must_include` 与 `gates[].id` 必须**互为子集**（差集为空）。
  2. `GATE_WIRING.json`：所有 `ci_wired` 的脚本名必须**存在于** `scripts/` 或 `dsh-ops/`（防僵尸条目）。
  3. `EXTERNAL_VERIFICATION.md` 的逐条登记表必须**覆盖 `SCORES.json` 的全部 entries**
     （按 id；缺一条即红）—— 这是本次实测发现的缺口（9 vs 25）。
  4. `EXTERNAL_VERIFICATION.md` 里出现的每个 id 必须**确实存在于** `SCORES.json`（防凭空条目）。

## fail-closed

任一文件缺失 / 解析失败 ⇒ rc=1（"读不到"绝不读成"没问题"）。

## 可失败证明（反事实）

  `--selftest`：构造一棵"缺 1 条 SCORES 条目"的合成树，断言**必须判红**；
  再断言完整树判绿 ⇒ 同一个判定函数换输入换结论（判据不是恒真）。

用法：
  python scripts/registry_freshness_gate.py            # 只读报告（有硬违规 rc=1）
  python scripts/registry_freshness_gate.py --json
  python scripts/registry_freshness_gate.py --selftest
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCORES = "docs/SCORES.json"
GATE_SET = "docs/GATE_SET.json"
GATE_WIRING = "docs/GATE_WIRING.json"
EXT_VERIFY = "docs/EXTERNAL_VERIFICATION.md"
_ID_IN_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|")


def _read_json(rel: str, root: str = ROOT):
    p = os.path.join(root, rel)
    with io.open(p, encoding="utf-8-sig") as f:
        return json.load(f)


def collect(root: str = ROOT) -> dict:
    """收集两侧的集合。任何读失败都抛（由调用方判 fail-closed）。"""
    scores = _read_json(SCORES, root)
    entries = scores.get("entries") or []
    score_ids = [e.get("id") for e in entries if isinstance(e, dict)]
    score_ids = [i for i in score_ids if i]

    gs = _read_json(GATE_SET, root)
    must = list(gs.get("must_include") or [])
    gates = [g.get("id") for g in (gs.get("gates") or []) if isinstance(g, dict)]

    gw = _read_json(GATE_WIRING, root)
    ci_wired = list(gw.get("ci_wired") or [])

    ext_path = os.path.join(root, EXT_VERIFY)
    ext_ids: list = []
    if os.path.isfile(ext_path):
        for line in io.open(ext_path, encoding="utf-8"):
            m = _ID_IN_ROW.match(line.strip())
            if m:
                ext_ids.append(m.group(1))
    return {"score_ids": score_ids, "must_include": must, "gates": gates,
            "ci_wired": ci_wired, "ext_ids": ext_ids, "ext_exists": os.path.isfile(ext_path)}


def evaluate(c: dict, root: str = ROOT) -> dict:
    hard: list = []
    soft: list = []

    # ① GATE_SET 双侧一致
    sm, sg = set(c["must_include"]), set(c["gates"])
    if sm - sg:
        hard.append("GATE_SET: must_include 有而 gates 缺: %s" % sorted(sm - sg))
    if sg - sm:
        hard.append("GATE_SET: gates 有而 must_include 缺: %s" % sorted(sg - sm))

    # ② ci_wired 的脚本必须真的存在
    missing = []
    for name in c["ci_wired"]:
        if not (os.path.isfile(os.path.join(root, "scripts", name))
                or os.path.isfile(os.path.join(root, "dsh-ops", name))
                or glob.glob(os.path.join(root, "**", name), recursive=False)):
            missing.append(name)
    if missing:
        hard.append("GATE_WIRING: ci_wired 指向不存在的脚本（僵尸登记）: %s" % missing)

    # ③④ 外部复核包 vs SCORES
    ss, es = set(c["score_ids"]), set(c["ext_ids"])
    if not c["ext_exists"]:
        hard.append("%s 不存在（fail-closed）" % EXT_VERIFY)
    else:
        if ss - es:
            hard.append(
                "%s 漏登 %d 条（SCORES.json 有 %d 条、外部包只列 %d 条）：%s"
                % (EXT_VERIFY, len(ss - es), len(ss), len(es), sorted(ss - es)[:8]))
        if es - ss:
            hard.append("%s 出现 SCORES.json 中不存在的 id: %s" % (EXT_VERIFY, sorted(es - ss)[:8]))
        if es and len(es) != len(c["ext_ids"]):
            soft.append("外部包 id 有重复行")

    return {"hard": hard, "soft": soft,
            "counts": {"scores": len(ss), "ext_verify": len(es),
                       "must_include": len(sm), "gates": len(sg),
                       "ci_wired": len(c["ci_wired"])}}


def selftest() -> int:
    """反事实：缺 1 条 SCORES 条目的合成树必须被判红。"""
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "docs"), exist_ok=True)
        scores = {"entries": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        io.open(os.path.join(td, SCORES), "w", encoding="utf-8").write(json.dumps(scores))
        io.open(os.path.join(td, GATE_SET), "w", encoding="utf-8").write(
            json.dumps({"must_include": ["x"], "gates": [{"id": "x"}]}))
        io.open(os.path.join(td, GATE_WIRING), "w", encoding="utf-8").write(
            json.dumps({"ci_wired": []}))
        # 完整外部包（3/3）⇒ 应绿
        io.open(os.path.join(td, EXT_VERIFY), "w", encoding="utf-8").write(
            "| `a` | 1 |\n| `b` | 2 |\n| `c` | 3 |\n")
        ok = evaluate(collect(td), td)
        # 缺 1 条（2/3）⇒ 应红
        io.open(os.path.join(td, EXT_VERIFY), "w", encoding="utf-8").write(
            "| `a` | 1 |\n| `b` | 2 |\n")
        bad = evaluate(collect(td), td)
        # 凭空条目 ⇒ 应红
        io.open(os.path.join(td, EXT_VERIFY), "w", encoding="utf-8").write(
            "| `a` | 1 |\n| `b` | 2 |\n| `c` | 3 |\n| `zzz` | 4 |\n")
        ghost = evaluate(collect(td), td)

    checks = [
        ("完整外部包 ⇒ 无硬违规", ok["hard"] == [], ok["hard"]),
        ("漏登 1 条 ⇒ 硬违规", any("漏登" in h for h in bad["hard"]), bad["hard"]),
        ("凭空条目 ⇒ 硬违规", any("不存在" in h for h in ghost["hard"]), ghost["hard"]),
    ]
    bad_n = 0
    for name, passed, detail in checks:
        print("  [%s] %s%s" % ("ok" if passed else "FAIL", name,
                               "" if passed else "  <- %s" % detail))
        bad_n += 0 if passed else 1
    print("[selftest] %d/%d" % (len(checks) - bad_n, len(checks)))
    return 0 if bad_n == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()

    try:
        c = collect()
        out = evaluate(c)
    except Exception as e:  # noqa: BLE001 — 读不到就是红
        print("[FAIL] fail-closed：登记文件读不到/解析失败：%s: %s"
              % (type(e).__name__, str(e)[:160]))
        return 1

    out["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print("登记新鲜度（登记 == 事实）—— %s" % out["ts"])
        print("  条数：SCORES %d / 外部包 %d / must_include %d / gates %d / ci_wired %d"
              % (out["counts"]["scores"], out["counts"]["ext_verify"],
                 out["counts"]["must_include"], out["counts"]["gates"],
                 out["counts"]["ci_wired"]))
        for h in out["hard"]:
            print("  [FAIL] %s" % h)
        for s in out["soft"]:
            print("  [WARN] %s" % s)
        if not out["hard"]:
            print("  [PASS] 三份登记录与事实一致")
    return 1 if out["hard"] else 0


if __name__ == "__main__":
    _rc = main()
    import datetime as _dt
    print("[采样时刻] %s（本读数只对该时刻的仓库状态成立）"
          % _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    raise SystemExit(_rc)
