#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""perception_importance_backfill.py — 历史感知行的 importance 回填（2026-09-21 §1035）

背景
----
§1034 把**新写入**的感知 importance 从「通道基线」改成「信号价值」
（0.20 + 0.45 x salience，上限 0.65）。历史 6,684 条 agent=perception 的行仍是旧值，
其中**恰好 0.7 的有 5,583 条**（通道基线的钉死形态）。

口径（先写死、可复核、可失败）
------------------------------
1. **只碰 agent_id='perception'** —— 其它写入方（brain/evolution/doc-fusion…）的 importance
   是各自的语义值，不在本次范围内；
2. **只碰「看起来就是通道基线」的值**：0.3/0.4/0.5/0.6/0.7/0.85（来自
   trinity/brain/perception.py::CHANNEL_BASE，测试断言两处一致）。0.9/1.0 之类**保持不动**
   （它们只可能来自显式赋值或 LLM 价值，不是本次要纠的形态）；
3. **映射**：new = 0.20 + 0.45 x old（与新写入路径同一个公式；即把旧值当作当时的 salience）；
4. **可回滚**：apply 前把 (memory_id, old, new) 落 CSV；回滚 = 按 CSV 写回 old。

为什么不用等 A/B 就能先做口径
------------------------------
perception **不参与语义检索**（trinity/core/client/_search.py 的
_RETRIEVAL_EXCLUDE_CATEGORIES = ["perception"]）⇒ importance 对它们唯一的作用是**保留时长**
（decay / tiers / 归档门）。因此本回填**不改变任何检索结果**，只改变「留多久」；
可观测效果 = 冷库存归档候选数量（scripts/cold_corpus_triage.py 的同口径）。

用法
----
    python scripts/perception_importance_backfill.py              # dry-run（打印影响面，不改库）
    python scripts/perception_importance_backfill.py --apply      # 落库（先写 CSV 备份）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#: 通道基线值（与 trinity/brain/perception.py::CHANNEL_BASE 一致；测试断言二者相等）
PEG_VALUES = (0.3, 0.4, 0.5, 0.6, 0.7, 0.85)
FLOOR, SPAN = 0.20, 0.45


def new_importance(old: float) -> float:
    """与新写入路径同一公式（旧值视为当时的 salience）。"""
    return round(min(1.0, max(0.0, FLOOR + SPAN * float(old))), 4)


def backfill_targets(rows, peg_values=PEG_VALUES):
    """纯函数：挑出要回填的行 → [(memory_id, old, new), ...]。

    判据：agent_id 必须是 perception（由 SQL 侧过滤，这里再断言一次）且旧值在 peg_values 里。
    """
    out = []
    for r in rows or []:
        try:
            if str(r.get("agent_id") or "") != "perception":
                continue
            old = round(float(r.get("importance") or 0.0), 4)
            if old not in tuple(round(float(v), 4) for v in peg_values):
                continue
            nv = new_importance(old)
            if nv == old:
                continue
            out.append((str(r.get("memory_id")), old, nv))
        except Exception:  # noqa: BLE001
            continue
    return out


def _pg():
    # t31：凭证走统一入口（修前顶层 .get ⇒ 版本化文件下恒空 ⇒ 空密码静默 0 行）
    import psycopg2
    from _pg_std import pg_creds
    c = pg_creds()
    return psycopg2.connect(host=c["host"], port=int(c["port"]), user=c["user"],
                            password=c["password"], dbname=c["dbname"],
                            connect_timeout=5)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只报告）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少行（0=全部）")
    a = ap.parse_args(argv)

    conn = _pg()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("select memory_id, importance, agent_id from memories "
                "where status='active' and agent_id='perception'")
    rows = [{"memory_id": r0[0], "importance": r0[1], "agent_id": r0[2]} for r0 in cur.fetchall()]
    targets = backfill_targets(rows)
    by_old = {}
    for _mid, old, _new in targets:
        by_old[str(old)] = by_old.get(str(old), 0) + 1
    below = sum(1 for _m, _o, nv in targets if nv < 0.5)
    print("perception active 行: %d ⇒ 回填目标: %d" % (len(rows), len(targets)))
    print("按旧值分布:", json.dumps(dict(sorted(by_old.items())), ensure_ascii=False))
    print("回填后 importance < 0.5 的行: %d（这些行将进入归档门的候选面）" % below)
    if not a.apply:
        print("（dry-run：未改任何行；加 --apply 才写库，且先落 CSV 备份）")
        return 0
    todo = targets[: max(0, a.limit)] if a.limit else targets
    out_dir = os.path.join(ROOT, "output")
    os.makedirs(out_dir, exist_ok=True)
    csv_p = os.path.join(out_dir, "perception_importance_backfill_%s.csv"
                         % time.strftime("%Y%m%d_%H%M%S"))
    with open(csv_p, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["memory_id", "old_importance", "new_importance"])
        for mid, old, nv in todo:
            w.writerow([mid, old, nv])
    done = 0
    for mid, old, nv in todo:
        # 2026-09-21（自查）：**不动 updated_at**。回填只改"保留权重"这一个语义，
        # 而 updated_at 是"这条记忆最近被改动过"的信号（consolidation/freshness 类判据会读它）
        # —— 顺手刷新 4,836 行的 updated_at 等于伪造"刚更新过"，副作用远超本次目的。
        # 2026-09-21（实测抓到）：memories.importance 是 **real(float4)**，而 psycopg2 把 Python
        # float 作为 float8 传参 ⇒ `importance = 0.7` 比较的是 float4(0.699999988…) 与
        # float8(0.7) ⇒ **永不相等**：首次 --apply 实测只回填了 1 行（0.5，二进制可精确表示），
        # 4,475 条 0.7 与 360 条 0.6 全部漏掉（而 dry-run 的 Python 侧比较是对的 ⇒ 只错在 SQL）。
        # 修法：把参数显式转成 real，两侧同为 float4 才可比。
        cur.execute("update memories set importance=%s "
                    "where memory_id=%s and importance=%s::real", (nv, mid, old))
        done += cur.rowcount
    print("已回填 %d 行（备份 %s）；回滚：按 CSV 把 importance 写回 old_importance"
          % (done, csv_p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
