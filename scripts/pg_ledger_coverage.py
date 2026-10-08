# -*- coding: utf-8 -*-
"""G31/t174 ①：**账本覆盖率**只读读数（SQLite 镜像 + PG 同口径），退出码 0/1/2。

## 为什么要有这个脚本（口径教训，逐字）
- t146 与 t154 曾给出**差 24 倍**的账本覆盖率读数，原因是**缺库标签/窗口定义/分母口径**；
- t157 的教训：**"PG 7 天窗口"的确切查询必须写出来**，否则会多出"新口径"的第三个数。
⇒ 本脚本的输出**每条读数都自带**：**库标签 · 窗口定义（原文 SQL）· 状态口径 · 时点 · 耗时**，
   并且**同一库里并列多种窗口/口径**，方便"两法对照"。

## 口径（原文 SQL，逐字）
    窗口 7 天 ：  created_at > now() - interval '7 days'          （**不筛 status** —— 与 t146/t157 同口径）
    窗口 30 天：  created_at > now() - interval '30 days'         （**不筛 status**）
    全量      ：  1=1                                            （**不筛 status**）
    状态口径  ：  all（不筛） / active（status='active'）
  账本定义：该 `memory_id` 在 `audit_log` 里有 ≥1 行 = w/a；在 `memory_versions` 里有 ≥1 行 = w/v；
           两者都没有 = **zero_both**（= "零账本"）。

## 平台差异（**必须说清，否则两库读数不可比**）
- SQLite：`created_at` 是 **TEXT**（本仓写的是本地时区字符串），窗口用 `datetime('now','localtime','-N days')`；
- PG：`created_at` 是真时间戳，窗口用 `now() - interval 'N days'`；
⇒ 两侧都用**各自库的"现在"**，因此**读数带时点**；跨库比较时请连时点一起引。

## 只读保证
- SQLite：`file:...?mode=ro` + `PRAGMA query_only=1`；
- PG：`conn.set_session(readonly=True)` + 只 **SELECT** + 结束 `rollback()`；
- ⛔ 不写库、不建表、不调 `trinity_search`、不跑镜像/回填。

## 退出码
- **0** = 两侧都读到（读数自洽，**不断言阈值**——本仓**没有已声明的账本契约**，见队长决定"不补账本，但做成可观测"）；
- **1** = 检测到**同口径下两库的 zero_both 比例差**超过 `--max-gap-pp`（默认 20 个百分点）⇒ **口径警报**（不是"缺陷"判定）；
- **2** = 任一侧**不可读** ⇒ **UNTESTABLE**（不得当通过）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQLITE_DEFAULT = os.path.expanduser("~/.trinity/store-restored/trinity_store.db")

WINDOWS = {
    # name: (sqlite 谓词, pg 谓词, 人类可读定义)
    "7d": ("m.created_at > datetime('now','localtime','-7 days')",
           "m.created_at > now() - interval '7 days'",
           "created_at > now() - interval '7 days'（**不筛 status**；与 t146/t157 同口径）"),
    "30d": ("m.created_at > datetime('now','localtime','-30 days')",
            "m.created_at > now() - interval '30 days'",
            "created_at > now() - interval '30 days'（**不筛 status**）"),
    "all": ("1=1", "1=1", "全量（**不筛 status**）"),
}
STATUS_FILTERS = {"all": "", "active": "AND m.status = 'active'"}


def _sqlite_read(db_path: str) -> tuple:
    """返回 (conn, label)。mode=ro + query_only。"""
    uri = "file:%s?mode=ro" % db_path.replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.execute("PRAGMA query_only=1")
    return conn, "sqlite:%s" % db_path


def _pg_read():
    """返回 (conn, label)。readonly 会话 + 只 SELECT。"""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from _pg_std import pg_connect, pg_creds
    conn = pg_connect()
    conn.set_session(readonly=True, autocommit=False)
    c = pg_creds()
    return conn, "pg:%s:%s/%s" % (c["host"], c["port"], c["dbname"])


def _ledger_sets(cur) -> tuple:
    """一次取出两本账的**有账 id 集合**（避免逐行 EXISTS ⇒ 实测太慢）。"""
    cur.execute("SELECT DISTINCT memory_id FROM audit_log")
    a = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT memory_id FROM memory_versions")
    v = {r[0] for r in cur.fetchall()}
    return a, v


def _measure(cur, win: str, status_caliber: str, dialect: str, a_ids: set, v_ids: set) -> dict:
    """窗口内取 id 列表（小集合），三个计数在本地算 ⇒ 快且口径写在输出里。"""
    w_sql = WINDOWS[win][0 if dialect == "sqlite" else 1]
    s_sql = STATUS_FILTERS[status_caliber]
    cur.execute("SELECT memory_id FROM memories m WHERE %s %s" % (w_sql, s_sql))
    ids = [r[0] for r in cur.fetchall()]
    n = len(ids)
    wa = sum(1 for i in ids if i in a_ids)
    wv = sum(1 for i in ids if i in v_ids)
    z = sum(1 for i in ids if (i not in a_ids) and (i not in v_ids))
    out = {"window": win, "window_definition": WINDOWS[win][2], "status_caliber": status_caliber,
           "n_rows": n, "with_audit": wa, "with_version": wv, "zero_both": z,
           "zero_both_pct": round(100.0 * z / n, 2) if n else None,
           "zero_both_bp": round(10000.0 * z / n, 1) if n else None}
    return out


def collect(db_path: str = SQLITE_DEFAULT, want_pg: bool = True) -> dict:
    t0 = time.time()
    report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "tz": "+08:00", "stores": {}, "errors": []}
    # ── SQLite ───────────────────────────────────────────────────────────
    try:
        conn, label = _sqlite_read(db_path)
        cur = conn.cursor()
        a_ids, v_ids = _ledger_sets(cur)
        rows = []
        for win in WINDOWS:
            for sc in STATUS_FILTERS:
                rows.append(_measure(cur, win, sc, "sqlite", a_ids, v_ids))
        report["stores"]["sqlite"] = {"label": label, "readings": rows,
                                      "audit_ids": len(a_ids), "version_ids": len(v_ids)}
        conn.close()
    except Exception as e:  # noqa: BLE001
        report["errors"].append("sqlite: %r" % (e,))
    # ── PG ───────────────────────────────────────────────────────────────
    if want_pg:
        try:
            conn, label = _pg_read()
            cur = conn.cursor()
            a_ids, v_ids = _ledger_sets(cur)
            rows = []
            for win in WINDOWS:
                for sc in STATUS_FILTERS:
                    rows.append(_measure(cur, win, sc, "pg", a_ids, v_ids))
            report["stores"]["pg"] = {"label": label, "readings": rows,
                                      "audit_ids": len(a_ids), "version_ids": len(v_ids)}
            conn.rollback()          # ← 只读会话的收尾（硬约束）
            conn.close()
        except Exception as e:  # noqa: BLE001
            report["errors"].append("pg: %r" % (e,))
    report["elapsed_s"] = round(time.time() - t0, 2)
    return report


def gap_pp(report: dict, window: str = "7d", status_caliber: str = "all"):
    """同口径（window × status）下 sqlite 与 pg 的 zero_both 百分点差；缺一侧 ⇒ None。"""
    def pick(store):
        for r in report["stores"].get(store, {}).get("readings", []):
            if r["window"] == window and r["status_caliber"] == status_caliber:
                return r
        return None
    a, b = pick("sqlite"), pick("pg")
    if not a or not b or a["zero_both_pct"] is None or b["zero_both_pct"] is None:
        return None
    return {"window": window, "status_caliber": status_caliber,
            "sqlite_pct": a["zero_both_pct"], "pg_pct": b["zero_both_pct"],
            "gap_pp": round(abs(a["zero_both_pct"] - b["zero_both_pct"]), 2)}


def format_report(r: dict) -> list:
    out = ["[账本覆盖率·只读] 时点 %s %s | 耗时 %ss" % (r["ts"], r["tz"], r["elapsed_s"])]
    for store, d in r["stores"].items():
        out.append("  ── 库标签 %s（%s）" % (d["label"], store))
        for x in d["readings"]:
            out.append("     %-4s %-6s n=%-6d w/a=%-6d w/v=%-6d zero_both=%-6d %s%% "
                       "[窗口: %s | status: %s]"
                       % (x["window"], "", x["n_rows"], x["with_audit"], x["with_version"],
                          x["zero_both"], x["zero_both_pct"], x["window_definition"].split("（")[0],
                          x["status_caliber"]))
    for e in r["errors"]:
        out.append("  ⚠️ 不可读 ⇒ UNTESTABLE: %s" % e)
    g = gap_pp(r)
    if g:
        out.append("  ── 同口径对照（7d/all）：sqlite %s%% vs pg %s%% ⇒ 差 %s 个百分点"
                   % (g["sqlite_pct"], g["pg_pct"], g["gap_pp"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="账本覆盖率只读读数（sqlite + pg）")
    ap.add_argument("--sqlite", default=SQLITE_DEFAULT)
    ap.add_argument("--no-pg", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-gap-pp", type=float, default=20.0)
    a = ap.parse_args()
    r = collect(a.sqlite, want_pg=not a.no_pg)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        for ln in format_report(r):
            print(ln)
    if r["errors"]:
        return 2                                    # UNTESTABLE（不得当通过）
    g = gap_pp(r)
    if g and g["gap_pp"] > a.max_gap_pp:
        print("⚠️ 口径警报：同口径两库 zero_both 差 %.2f 个百分点 > 阈值 %.2f ⇒ 值得查口径（不是缺陷判定）"
              % (g["gap_pp"], a.max_gap_pp))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
