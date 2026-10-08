#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""delivery_ratchet.py — **投递/消费棘轮**（t88 / B4·A5-04，承 A4-#2）。

## 判据（**只比全仓一个数、不按文件归因** —— 沿 t71 的 L-A/L-B 棘轮做法，避免误伤）

    指标 undelivered_bp = round(10000 × (active_memories − distinct_delivered_in_window) / active_memories)

    undelivered_bp > 基线  ⇒ **失败**（投递面退化：有更多活跃记忆在窗口内**一条都没被投递**）

- **一个整数**（万分比），**不按文件/条目标归因** ⇒ 别人改代码/加语料不会误伤，只有"投递面真的变窄"才会红。
- **基线只能下调**：`--write-baseline` 在"新值 > 旧值"时**拒绝写入并报错**（不是口号，是代码里拦住的）。
- **可独立复算**：`evaluate(stats, base)` 是纯函数；`stats` 由本脚本可从两个只读来源重算
  （注入侧投递账本 `opening_surface_deliveries.jsonl` + 受众库 SQLite 只读 `status='active'` 计数）。

## 为什么用"万分比"而不是"条数"

条数会**随语料自然增长而上升**（新记忆天生没被投递过）⇒ 会把"没人动它"也判成红（误伤）。
万分比把语料增长约掉，只反映**投递覆盖面**；而 `top_k` 被调小 ⇒ 被投递的**条目数**塌缩 ⇒ 万分比必然上升 ⇒ **红**。

## 证伪（A5 给的，必须能跑）

    把 opening 的 top_k 设成 1 ⇒ 棘轮必红

受控复现（**不改产品、不重启服务**）：拿真实的投递账本做一份"每条只留 1 个 id"的**降级副本**，
再用 `--deliveries <降级副本>` 跑同一条判据 ⇒ 必须 FAIL（同时用真实账本跑必须 PASS）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
import time
import logging

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(os.path.expanduser("~"), ".trinity", "state")
DEFAULT_DELIVERIES = os.path.join(STATE, "opening_surface_deliveries.jsonl")
DEFAULT_BASELINE = os.path.join(ROOT, "docs", "DELIVERY_RATCHET_BASELINE.json")
STORE_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), ".trinity", "store-restored", "trinity_store.db"),
    os.path.join(os.path.expanduser("~"), ".trinity", "store", "trinity_store.db"),
]
SCHEMA = "trinity.delivery_ratchet.baseline/1"


def active_memories() -> tuple[int, str]:
    """受众库的活跃记忆数（**只读**）。返回 (n, store_path)；取不到抛错（fail-closed）。"""
    last_err = None
    for cand in STORE_CANDIDATES:
        if not os.path.isfile(cand):
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % cand.replace("\\", "/"), uri=True, timeout=15)
            try:
                n = int(con.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0])
                return n, cand
            finally:
                con.close()
        except Exception as e:  # noqa: BLE001
            last_err = "%s: %s" % (cand, e)
    raise RuntimeError("受众库不可读（只读尝试失败）：%s" % (last_err or "没有候选路径存在"))


def delivery_stats(deliveries_path: str, hours: float, anchor: str = "file",
                   now: float | None = None) -> dict:
    """窗口内的投递读数（纯读）。"""
    if not os.path.isfile(deliveries_path):
        raise FileNotFoundError(deliveries_path)
    rows = []
    with io.open(deliveries_path, "rb") as fh:
        for raw in fh:
            s = raw.decode("utf-8", "replace").strip()
            if not s:
                continue
            try:
                rows.append(json.loads(s))
            except json.JSONDecodeError:
                continue
    if not rows:
        raise ValueError("投递账本没有可解析记录：%s" % deliveries_path)
    stamps = [float(r.get("ts") or 0) for r in rows if r.get("ts")]
    end = float(now if now is not None else time.time()) if anchor == "now" else (max(stamps) if stamps else time.time())
    start = end - hours * 3600.0
    ids, calls = [], 0
    for r in rows:
        if start <= float(r.get("ts") or 0) <= end:
            calls += 1
            ids.extend(str(x) for x in (r.get("ids") or []) if x)
    return {"delivery_calls": calls, "delivered_n": len(ids), "distinct_delivered": len(set(ids)),
            "window_start_epoch": round(start, 3), "window_end_epoch": round(end, 3)}


def metrics(deliveries_path: str, hours: float, anchor: str = "file",
            now: float | None = None, frozen_active: int | None = None) -> dict:
    """窗口内的投递读数 + `undelivered_bp`。

    ⚠️ 2026-10-07（t97b / F1-b）**口径冻结**（本指标第二次改口径，理由必须留在文件里，见 docs/DELIVERY_RATCHET_BASELINE.json 的 `_caliber_change_20261007`）：

      · **分母固定为"基线时刻的 active"**（`frozen_active`）：旧口径拿**当前** active 当分母，而语料每天
        +~180 条（实测 2026-10-07 单日 active **+179**）⇒ **分母自己在长**，投递量不变也会把比值推高
        （约 +1 万分点/周）⇒ 那是"**语料在长**"被读成"投递退化"。
      · **窗长固定 168h（7 天）**：24h 窗只覆盖投递账本的 ~18%（101/568 行）⇒ **窗口一滑 distinct 就抖**：
        实测 13 个相邻锚点（每 10 分钟挪一次）distinct ∈ **[34, 45]**、undelivered_bp ∈ **[9984, 9988]**（±2bp）；
        7 天窗 distinct=124 ⇒ 抖动被压掉。
      · **旧口径值仍报出**（`undelivered_bp_at_current_active`）**只供人看，不进判据**（口径不得混用）。

    调用方（`main`）会把窗口/分母与基线里冻结的参数**逐项校验**：不匹配即 rc=2 UNTESTABLE（防口径混用）。
    """
    st = delivery_stats(deliveries_path, hours, anchor, now)
    active_now, store = active_memories()
    denom = int(frozen_active) if frozen_active else active_now
    undelivered = max(0, denom - st["distinct_delivered"])
    bp = int(round(10000.0 * undelivered / float(denom))) if denom else 0
    old_bp = (int(round(10000.0 * max(0, active_now - st["distinct_delivered"]) / float(active_now)))
              if active_now else 0)
    return {"undelivered_bp": bp, "undelivered_n": undelivered,
            "denominator_active": denom, "active_memories": active_now,
            "active_memories_now": active_now,
            "undelivered_bp_at_current_active": old_bp,
            "audience_store": store, "hours": hours, "anchor": anchor, **st}


def evaluate(stats: dict, base: dict) -> tuple[bool, list, str]:
    """纯函数判据：`undelivered_bp` 不得高于基线（基线只能下调）。

    ⚠️ 口径（t97b 起冻结）：分母 = **基线时刻的 active**，窗长 = **基线里的 `window_hours`（168h）**；
    `stats` 由 `metrics(..., frozen_active=基线里的分母)` 产出 ⇒ 判据与基线**同口径**才可比。
    """
    cur = int(stats.get("undelivered_bp") or 0)
    b = int((base or {}).get("undelivered_bp") or 0)
    reasons = []
    if cur > b:
        reasons.append("undelivered_bp %d > 基线 %d（+%d）⇒ 投递覆盖面退化（口径：分母固定 %s / 窗 %sh）"
                       % (cur, b, cur - b, stats.get("denominator_active"),
                          stats.get("hours")))
    detail = ("undelivered_bp=%d 基线=%d（口径：分母固定=%s、窗=%sh；当前 active=%s，"
              "按旧口径会算成 %s——仅参考、不进判据）；窗口内投递调用 %d 次 / distinct %d 条"
              % (cur, b, stats.get("denominator_active"), stats.get("hours"),
                 stats.get("active_memories_now"), stats.get("undelivered_bp_at_current_active"),
                 stats.get("delivery_calls", 0), stats.get("distinct_delivered", 0)))
    return (not reasons), reasons, detail


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/delivery_ratchet.py::main")
    ap = argparse.ArgumentParser(description="投递/消费棘轮（全仓一个数，不按文件归因）")
    ap.add_argument("--deliveries", default=DEFAULT_DELIVERIES)
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--hours", type=float, default=None,
                    help="窗口小时数；**省略则用基线里冻结的 window_hours**（口径不得随手改）")
    ap.add_argument("--anchor", choices=("file", "now"), default="file")
    ap.add_argument("--write-baseline", action="store_true",
                    help="写基线（**只许下调**；更高会被拒绝并报错退出）")
    ap.add_argument("--force-caliber-change", action="store_true",
                    help="口径变更时按新口径写基线（必须同时给 --caliber-note 说明为什么改；留痕不静默）")
    ap.add_argument("--caliber-note", default="", help="口径变更说明（写进基线 JSON 的 _caliber_change_*）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    base = {}
    if os.path.isfile(args.baseline):
        try:
            base = json.loads(io.open(args.baseline, encoding="utf-8").read()) or {}
        except Exception as e:  # noqa: BLE001
            print("delivery_ratchet UNTESTABLE: 基线不可读（%s）：%s" % (args.baseline, e))
            return 2

    # ⚠️ 口径冻结（t97b）：窗长与分母必须与基线一致，否则 rc=2 —— **防"口径混用"**。
    # 例外：**显式的口径变更路径**（`--write-baseline --force-caliber-change --caliber-note "<为什么>"`）
    # 必须能改口径本身（否则"改口径"这件事永远做不了）；该路径在写基线时**强制留痕**（见下）。
    changing_caliber = bool(args.write_baseline and args.force_caliber_change)
    base_hours = float(((base.get("measured") or {}).get("window_hours")) or 0) or None
    hours = float(args.hours) if args.hours is not None else (base_hours or 24.0)
    if base_hours and abs(hours - base_hours) > 1e-9 and not changing_caliber:
        print("delivery_ratchet UNTESTABLE: 窗长 %sh ≠ 基线冻结的 %sh ⇒ 口径混用，拒绝出数。"
              "（要改口径：--write-baseline --force-caliber-change --caliber-note \"<为什么>\"）"
              % (hours, base_hours))
        return 2
    frozen = ((base.get("measured") or {}).get("denominator_active")
              or (base.get("measured") or {}).get("active_memories"))
    try:
        stats = metrics(args.deliveries, hours, args.anchor, frozen_active=frozen)
    except Exception as e:  # noqa: BLE001
        print("delivery_ratchet UNTESTABLE: %s —— fail-closed（不静默当通过）" % e)
        return 2

    if args.write_baseline:
        old_bp = int(base.get("undelivered_bp") or 0)
        caliber_changed = bool(args.force_caliber_change)
        if old_bp and stats["undelivered_bp"] > old_bp and not caliber_changed:
            print("delivery_ratchet 拒绝写基线：新值 %d > 旧值 %d ⇒ **基线只能下调**（这是硬规则）；"
                  "口径变更请显式给 --force-caliber-change 与 --caliber-note"
                  % (stats["undelivered_bp"], old_bp))
            return 1
        if caliber_changed and not args.caliber_note.strip():
            print("delivery_ratchet 拒绝写基线：--force-caliber-change 必须配 --caliber-note（留痕，不静默）")
            return 1
        doc = {"_comment": "投递/消费棘轮基线（t88/B4·A5-04；t97b 冻结口径）。指标 = 窗口内**未被投递**的"
                           "记忆占比（万分比，**分母固定为基线时刻 active**）；只许下调，判据见 scripts/delivery_ratchet.py。",
               "_metric": "undelivered_bp = round(10000 × (denominator_active − distinct_delivered_in_window) / denominator_active)",
               "_caliber": "注入侧（opening_surface_deliveries.jsonl）+ 受众库 SQLite 只读 status='active'；"
                           "与会话侧(pull)账本**禁止相加**",
               "_why_ratio": "条数会随语料增长而上升（新记忆天生未投递）⇒ 会误伤；万分比把语料增长约掉。",
               "_caliber_change_20261007": {
                   "from": "分母 = 当前 active；窗长 = 24h",
                   "to": "分母 = **基线时刻 active %s**（冻结）；窗长 = **%sh**（冻结）" % (stats["denominator_active"], stats["hours"]),
                   "why": ("① 分母自己会长：语料 2026-10-07 单日 active **+179** ⇒ 投递不变也会推高比值（~+1bp/周）；"
                           "② 24h 窗只覆盖投递账本 ~18% ⇒ **窗口一滑 distinct 就抖**：13 个相邻锚点（每 10 分钟）实测 "
                           "distinct ∈ [34,45]、undelivered_bp ∈ [9984,9988]（±2bp），而 7 天窗 distinct=124 ⇒ 抖动被压掉。"
                           "⇒ 旧口径会把「窗滑/语料增长」读成「投递退化」。"),
                   "note": args.caliber_note,
                   "old_caliber_value_kept_for_reference": old_bp,
                   "triggered_by": "full6 里本棘轮首次发火：9987 > 9986（+1）⇒ 定性后改口径，**未把红改成不红而不留痕**",
               },
               "_falsification": "把 opening 的 top_k 调成 1（或等价地让每条投递只含 1 个 id）⇒ 本棘轮必红",
               "_measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "undelivered_bp": stats["undelivered_bp"],
               "measured": {"active_memories": stats["active_memories"],
                            "denominator_active": stats["denominator_active"],
                            "distinct_delivered": stats["distinct_delivered"],
                            "delivery_calls": stats["delivery_calls"],
                            "undelivered_bp_at_current_active": stats["undelivered_bp_at_current_active"],
                            "window_hours": stats["hours"], "anchor": stats["anchor"]}}
        with io.open(args.baseline, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False, indent=1))
        print("投递棘轮基线已写入 %s（undelivered_bp=%d，旧值=%s%s）"
              % (os.path.relpath(args.baseline, ROOT), stats["undelivered_bp"], old_bp or "无",
                 "，口径变更已留痕" if caliber_changed else ""))
        return 0

    ok, reasons, detail = evaluate(stats, base)
    if args.json:
        print(json.dumps({"ok": ok, "detail": detail, "reasons": reasons, "metrics": stats},
                         ensure_ascii=False, indent=2))
    else:
        print("delivery_ratchet %s  %s" % ("OK" if ok else "FAIL", detail))
        for r in reasons:
            print("   - " + r)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
