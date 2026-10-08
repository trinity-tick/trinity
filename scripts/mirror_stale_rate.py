#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mirror_stale_rate.py —— **per-retrieval 陈旧率** + **退化测试**（与全局 max-age 并列打印）。

## 为什么有它（G56/t199）
Trinity 原本只有一个新鲜度指标：「**镜像本身多旧**」（`scripts/mirror_freshness.py`：全局 max-age）。
⛔ 缺的是「**读到的东西有多旧**」—— 于是 t159 只能写下那句
「本次在 **10.96 h** 内撞到『写进去读不到』，按声明**并不违约**」（全局口径确实没违约，但用户已经读不到）。
本脚本补上这一面，并**与全局指标在同一次运行里一起打印**（**同一张表的两列**）。

## 三个读数（一次运行全给）
1. **[全局] max-age**：复用 `mirror_freshness.py` 的口径（`audit_log` 的 `PG_MIRROR_STATUS.max(timestamp)`）。
2. **[L1] per-retrieval 陈旧率**（两栏，都是"读到的东西有多旧"）：
   - ⭐ **`ceiling`（可读天花板滞后）**：`PG.max(created_at) − SQLite.max(created_at)` ⇒
     **"你现在能读到的最新内容，比源里最新的内容旧多少小时"**（**对每一次检索都成立**）⇒ 给阈值与判定；
   - ⭐ **`sampled rate`（抽样陈旧率）**：采样 SQLite 最近 **N** 行 ⇒ 按 `memory_id` 回查 PG ⇒
     `滞后 = PG.updated_at − SQLite.updated_at` ⇒ **滞后 > 阈值 = 陈旧**；同时单列
     **`PG 无此 id`**、**`副本反而更新`**（滞后 < −阈值）、**`created_at 不一致`** 三类构成。
     ⚠️ 跨库时间**一律先解析成 aware**（t158 D1/D2：裸 datetime 相减会给 ±28800 s 的整齐假读数）。
3. **[L2] 退化测试**：以 **"PG 有而 SQLite 无"** 的近期行为样本（**就是 t159 那个现象**）⇒ 报
   `窗口内 PG 行数`、`SQLite 无的条数`、`最旧一条已存在多久`；**两个时点**用 `--now` 或 `--compare` 对照 ⇒ 看是否**随时间退化**。

## ⚠️ 量级限制（**照抄**既有登记，不自己发明）
`scripts/cross_store_reconcile_probe.py:86-87` 原文：
> 存在性**非单调**（同一库内新旧行是否在对侧出现并非单调）⇒
> lag_seconds 只作**量级**参考，**不得**读成「从某刻起停止同步」

⇒ L1/L2 同属**量级**读数：能回答"**有多少读到的东西比源旧、旧到什么量级**"，
⛔ **不能回答"从哪一刻起镜像停了"**（那是全局 max-age 的活）。

## ⚠️ 已实测的口径限制（本轮诊断 `evidence/g56_diag.py`，**必须随数字同行**）
- ⭐ **`updated_at` 在 SQLite 侧会被本地改写**：实测 `mem_92a3f248aced4ea7` 的
  SQLite `updated_at = 2026-10-08T07:47:35Z`，而 PG `updated_at = 2026-10-08 03:04:47+08`
  ⇒ **滞后 = −12.7 h（负数）** ⇒ 因此本脚本把"**副本反而更新**"**单列一类**，
  ⛔ **不把它算进陈旧**，也**不据此声称"同步正常"**；
- ⭐ **`created_at` 是忠实的**（同一行两侧是**同一瞬间**：`2026-09-11T20:36:43Z` == `2026-09-12 04:36:43+08`）⇒
  **`ceiling` 用 `created_at`**；两侧 `created_at` 不一致的样本单列一类（口径/身份问题）。
- ⭐ **采样面偏向"SQLite 独有写入"**：实测最近 50 条里 **47 条在 PG 里没有**（`mem_…` 形态、分钟级新写入）
  ⇒ 抽样陈旧率**只对"两库都有的行"有意义** ⇒ 本脚本按 **可用样本** 给率，并把三类构成同时报出。

## 口径与限制
- ⛔ **只读**：SQLite `file:...?mode=ro`；PG `set_session(readonly=True)`；**不写任何库**。
- 采样：`--sample-n`（默认 **50**）· 陈旧阈值 `--stale-threshold-hours`（默认 **1.0**）·
  L1 判定 `--l1-max-rate`（默认 **0.0**）· L2 窗口 `--pg-only-window-hours`（默认 **24**）· 上限 `--pg-only-limit`（默认 **500**）。
- 退出码：**0 fresh** · **1 stale** · **2 不可判定**（只缺一侧 ⇒ 另一侧照报，rc 由可测侧决定）。

## 用法
    python scripts/mirror_stale_rate.py
    python scripts/mirror_stale_rate.py --sample-n 200 --stale-threshold-hours 6
    python scripts/mirror_stale_rate.py --now 2026-10-08T03:05:10+08:00      # 钉住"现在"（两个时点对照）
    python scripts/mirror_stale_rate.py --json-out ev/g56_now.json --compare ev/g56_prev.json
    python scripts/mirror_stale_rate.py --skip-per-retrieval                 # PG 不可用时只报全局
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FRESHNESS = os.path.join(HERE, "mirror_freshness.py")
DEFAULT_PG_PORT = 5432
DEFAULT_PG_DB = "trinity"
DEFAULT_PG_USER = "trinity"


def _load_freshness():
    """复用 t160 的解析/判定/选库（同一仓、同一口径）⇒ 两个指标天然可并列。"""
    spec = importlib.util.spec_from_file_location("_g56_freshness", FRESHNESS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_g56_freshness"] = mod
    spec.loader.exec_module(mod)
    return mod


F = _load_freshness()


# ── 纯函数（可判据化；不碰库）──────────────────────────────────────────────
def lag_hours(sqlite_ts, pg_ts) -> float:
    """该条副本比源**旧**多少小时（= PG 时间 − SQLite 时间）；**负数 = 副本反而更新**。"""
    return (F.parse_ts(pg_ts) - F.parse_ts(sqlite_ts)).total_seconds() / 3600.0


def summarize_lags(lags, threshold_h: float) -> dict:
    """n / 陈旧 / 副本更新 / 一致 / 陈旧率 / 最大 / 最小 / 中位（**空样本 ⇒ rate=None**）。"""
    vals = sorted(float(x) for x in lags)
    n = len(vals)
    if n == 0:
        return {"n": 0, "stale": 0, "copy_newer": 0, "aligned": 0, "rate": None,
                "max_h": None, "min_h": None, "median_h": None}
    stale = sum(1 for v in vals if v > threshold_h)
    newer = sum(1 for v in vals if v < -abs(threshold_h))
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    return {"n": n, "stale": stale, "copy_newer": newer, "aligned": n - stale - newer,
            "rate": stale / float(n), "max_h": vals[-1], "min_h": vals[0], "median_h": med}


def classify_rate(rate, max_rate: float) -> str:
    """`fresh`/`stale`/`untestable`（**无样本 ⇒ untestable**，不是"新鲜"）。"""
    if rate is None:
        return "untestable"
    return "stale" if float(rate) > float(max_rate) else "fresh"


# ── 读数（只读）────────────────────────────────────────────────────────────
def _ro(db: str):
    return sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True, timeout=10)


def sqlite_sample(db: str, n: int) -> list:
    """SQLite 最近 n 条：[(memory_id, updated_at, created_at)]（**只读**）。"""
    con = _ro(db)
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(memories)")}
        if "memory_id" not in have or "created_at" not in have:
            raise RuntimeError("memories 表/列不可用")
        tcol = "updated_at" if "updated_at" in have else "created_at"
        rows = con.execute("SELECT memory_id, updated_at, created_at FROM memories "
                           "WHERE %s IS NOT NULL ORDER BY %s DESC LIMIT ?" % (tcol, tcol),
                           (int(n),)).fetchall()
        return [(str(r[0]), r[1], r[2]) for r in rows]
    finally:
        con.close()


def sqlite_max_created(db: str):
    con = _ro(db)
    try:
        r = con.execute("SELECT max(created_at) FROM memories").fetchone()
        return r[0] if r else None
    finally:
        con.close()


def sqlite_has_ids(db: str, ids) -> set:
    if not ids:
        return set()
    con = _ro(db)
    try:
        marks = ",".join("?" * len(ids))
        return {str(r[0]) for r in con.execute(
            "SELECT memory_id FROM memories WHERE memory_id IN (%s)" % marks, list(ids))}
    finally:
        con.close()


def _pg_connect(port: int = DEFAULT_PG_PORT, dbname: str = DEFAULT_PG_DB,
                user: str = DEFAULT_PG_USER):
    """**只读**打开 PG（与 `cross_store_reconcile_probe.py:185-194` 同一入口与只读设置）。"""
    from trinity.security.credentials import resolve_credentials
    creds = dict(resolve_credentials())
    import psycopg2
    con = psycopg2.connect(host=creds.get("host") or "127.0.0.1",
                           port=int(port or creds.get("port") or DEFAULT_PG_PORT),
                           dbname=dbname or str(creds.get("dbname") or DEFAULT_PG_DB),
                           user=user or str(creds.get("user") or DEFAULT_PG_USER),
                           password=creds.get("password") or "", connect_timeout=8)
    con.set_session(readonly=True, autocommit=True)
    return con


def pg_times(ids, port: int = DEFAULT_PG_PORT, dbname: str = DEFAULT_PG_DB,
             user: str = DEFAULT_PG_USER) -> dict:
    """按 memory_id 批量回查 PG 的 (created_at, updated_at)（**只读**）。"""
    if not ids:
        return {}
    con = _pg_connect(port, dbname, user)
    try:
        cur = con.cursor()
        cur.execute("SELECT memory_id::text, created_at, updated_at FROM memories "
                    "WHERE memory_id::text = ANY(%s)", (list(ids),))
        return {str(r[0]): (r[1], r[2]) for r in cur.fetchall()}
    finally:
        con.close()


def pg_max_created(port: int = DEFAULT_PG_PORT, dbname: str = DEFAULT_PG_DB,
                   user: str = DEFAULT_PG_USER):
    con = _pg_connect(port, dbname, user)
    try:
        cur = con.cursor()
        cur.execute("SELECT max(created_at) FROM memories")
        r = cur.fetchone()
        return r[0] if r else None
    finally:
        con.close()


def pg_only_recent(window_hours: float, limit: int, now, port: int = DEFAULT_PG_PORT,
                   dbname: str = DEFAULT_PG_DB, user: str = DEFAULT_PG_USER) -> list:
    """PG 近期行 [(memory_id, created_at)]（**只读**）；SQLite 侧存在性由调用方判。"""
    lo = (F.parse_ts(now) - _dt.timedelta(hours=float(window_hours))).astimezone(_dt.timezone.utc)
    con = _pg_connect(port, dbname, user)
    try:
        cur = con.cursor()
        cur.execute("SELECT memory_id::text, created_at FROM memories WHERE created_at >= %s "
                    "ORDER BY created_at DESC LIMIT %s", (lo, int(limit)))
        return [(str(r[0]), r[1]) for r in cur.fetchall()]
    finally:
        con.close()


# ── L1 / L2 ───────────────────────────────────────────────────────────────
def measure_l1(db: str, sample_n: int, threshold_h: float, pg_kw: dict) -> dict:
    out = {"testable": False, "sample_n": sample_n, "usable": 0, "missing_in_pg": 0,
           "created_mismatch": 0, "threshold_hours": threshold_h, "rate": None, "stale": 0,
           "copy_newer": 0, "aligned": 0, "max_h": None, "min_h": None, "median_h": None,
           "ceiling_lag_hours": None, "ceiling_verdict": "untestable", "error": None}
    try:
        rows = sqlite_sample(db, sample_n)
        times = pg_times([r[0] for r in rows], **pg_kw)
        s_max = sqlite_max_created(db)
        p_max = pg_max_created(**pg_kw)
    except Exception as e:  # noqa: BLE001
        out["error"] = repr(e)
        return out
    lags, missing, mismatch = [], 0, 0
    for mid, s_upd, s_cre in rows:
        pg = times.get(mid)
        if pg is None:
            missing += 1
            continue
        if s_cre is not None and pg[0] is not None:
            try:
                if abs(lag_hours(s_cre, pg[0])) > 1e-6:      #: 同一瞬间应相等（实测成立）
                    mismatch += 1
            except Exception:  # noqa: BLE001
                mismatch += 1
        base = s_upd or s_cre
        ref = pg[1] or pg[0]
        if base is None or ref is None:
            continue
        lags.append(lag_hours(base, ref))
    s = summarize_lags(lags, threshold_h)
    ceil_h = None
    if s_max and p_max:
        ceil_h = lag_hours(s_max, p_max)          #: PG 更新 ⇒ 正数 = "可读天花板"落后多少
    out.update({"testable": True, "missing_in_pg": missing, "created_mismatch": mismatch,
                "usable": len(lags), "ceiling_lag_hours": ceil_h,
                #: ⭐ 天花板判定：`PG.max(created) − SQLite.max(created) > 阈值` ⇒ 有"读不到的更新内容"
                "ceiling_verdict": (classify_rate(None, 0.0) if ceil_h is None else
                                    classify_rate(1.0 if ceil_h > threshold_h else 0.0, 0.0)),
                **s})
    return out


def measure_l2(db: str, window_hours: float, limit: int, now, pg_kw: dict) -> dict:
    out = {"testable": False, "window_hours": window_hours, "pg_recent": 0, "limit_reached": False,
           "pg_only": 0, "oldest_pg_only_hours": None, "error": None}
    try:
        recent = pg_only_recent(window_hours, limit, now, **pg_kw)
        have = sqlite_has_ids(db, [r[0] for r in recent])
    except Exception as e:  # noqa: BLE001
        out["error"] = repr(e)
        return out
    only = [(mid, cre) for mid, cre in recent if mid not in have]
    ages = [F.age_hours(cre, now) for _, cre in only]
    out.update({"testable": True, "pg_recent": len(recent), "pg_only": len(only),
                "limit_reached": len(recent) >= int(limit),
                "oldest_pg_only_hours": (max(ages) if ages else None)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="镜像：全局 max-age + per-retrieval 陈旧率（只读）")
    ap.add_argument("--db")
    ap.add_argument("--max-age-hours", type=float, default=24.0)
    ap.add_argument("--sample-n", type=int, default=50)
    ap.add_argument("--stale-threshold-hours", type=float, default=1.0)
    ap.add_argument("--l1-max-rate", type=float, default=0.0)
    ap.add_argument("--pg-only-window-hours", type=float, default=24.0)
    ap.add_argument("--pg-only-limit", type=int, default=500)
    ap.add_argument("--port", type=int, default=DEFAULT_PG_PORT)
    ap.add_argument("--now")
    ap.add_argument("--skip-per-retrieval", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--json-out")
    ap.add_argument("--compare")
    a = ap.parse_args()

    p, tried = F.pick_db(a.db)
    now = F.parse_ts(a.now) if a.now else _dt.datetime.now(_dt.timezone.utc)
    res = {"ts": now.astimezone(_dt.timezone.utc).isoformat(), "db": p.get("db")}
    if p.get("ok"):
        last = F.parse_ts(p["last_raw"])
        g_age = F.age_hours(last, now)
        res["global"] = {"last_mirror_utc": last.astimezone(_dt.timezone.utc).isoformat(),
                         "age_hours": round(g_age, 2),
                         "verdict": F.classify(g_age, a.max_age_hours),
                         "mirror_rows": p.get("rows") or {}}
    else:
        res["global"] = {"verdict": "untestable", "error": p.get("error"),
                         "tried": [{"db": t.get("db"), "error": t.get("error")} for t in tried]}
    pg_kw = {"port": a.port}
    if a.skip_per_retrieval or not p.get("ok"):
        res["l1"] = {"testable": False, "error": "skipped"}
        res["l2"] = {"testable": False, "error": "skipped"}
    else:
        res["l1"] = measure_l1(p["db"], a.sample_n, a.stale_threshold_hours, pg_kw)
        res["l2"] = measure_l2(p["db"], a.pg_only_window_hours, a.pg_only_limit, now, pg_kw)

    g_v = res["global"].get("verdict", "untestable")
    l1_v = classify_rate(res["l1"].get("rate"), a.l1_max_rate) if res["l1"].get("testable") \
        else "untestable"
    #: ⭐ 判定**必须包含天花板**：样本陈旧率对"镜像行"结构性地≈0（它们是精确副本），
    #: 真正会让人"写进去读不到"的是**天花板**（PG 有更新的内容、SQLite 读不到）⇒ 它进 rc。
    ceil_v = res["l1"].get("ceiling_verdict", "untestable") if res["l1"].get("testable") \
        else "untestable"
    sides = [v for v in (g_v, l1_v, ceil_v) if v != "untestable"]
    res["verdicts"] = {"global": g_v, "l1_rate": l1_v, "l1_ceiling": ceil_v}
    res["rc"] = 1 if "stale" in sides else (0 if sides else 2)

    if a.compare and os.path.isfile(a.compare):
        try:
            prev = json.load(open(a.compare, encoding="utf-8"))
            res["compare"] = {"prev_ts": prev.get("ts"),
                              "global_age_hours": [prev.get("global", {}).get("age_hours"),
                                                   res["global"].get("age_hours")],
                              "l1_rate": [prev.get("l1", {}).get("rate"),
                                          res["l1"].get("rate")],
                              "l1_ceiling_hours": [prev.get("l1", {}).get("ceiling_lag_hours"),
                                                   res["l1"].get("ceiling_lag_hours")],
                              "l2_pg_only": [prev.get("l2", {}).get("pg_only"),
                                             res["l2"].get("pg_only")],
                              "l2_oldest_hours": [prev.get("l2", {}).get("oldest_pg_only_hours"),
                                                  res["l2"].get("oldest_pg_only_hours")]}
        except Exception as e:  # noqa: BLE001
            res["compare"] = {"error": repr(e)}
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        res["json_out"] = a.json_out

    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        g = res["global"]
        print("镜像新鲜度 —— 同一张表两列（只读；时点 %s；库 %s）" % (res["ts"], res["db"]))
        print("  [全局 max-age   ] 上次镜像 %s ⇒ 滞后 %s h（上限 %s）⇒ %s"
              % (g.get("last_mirror_utc"), g.get("age_hours"), a.max_age_hours,
                 g.get("verdict")))
        l1 = res["l1"]
        if l1.get("testable"):
            print("  [L1 可读天花板  ] PG.max(created) − SQLite.max(created) = %s h"
                  "（阈值 %.2f h）⇒ %s"
                  % (l1["ceiling_lag_hours"], l1["threshold_hours"], l1["ceiling_verdict"]))
            print("  [L1 抽样陈旧率  ] 采样 %d 条 ⇒ 可用(两库都有) %d · **PG 无此 id %d** ·"
                  " created_at 不一致 %d"
                  % (l1["sample_n"], l1["usable"], l1["missing_in_pg"], l1["created_mismatch"]))
            print("                    滞后 > %.2f h：**%d/%d = %s**（副本反而更新 %d · 一致 %d）"
                  " · 最大 %s h · 最小 %s h"
                  % (l1["threshold_hours"], l1["stale"], l1["usable"],
                     ("%.3f" % l1["rate"]) if l1["rate"] is not None else "无样本",
                     l1["copy_newer"], l1["aligned"], l1["max_h"], l1["min_h"]))
        else:
            print("  [L1 per-retrieval] 不可判定：%s" % (l1.get("error") or "skipped"))
        l2 = res["l2"]
        if l2.get("testable"):
            print("  [L2 退化测试    ] 窗口 %s h 内 PG 行 %d%s ⇒ **SQLite 无 %d 条**"
                  "（最旧已存在 %s h）"
                  % (l2["window_hours"], l2["pg_recent"],
                     "（**已达 --pg-only-limit，是最低下限**）" if l2["limit_reached"] else "",
                     l2["pg_only"], l2["oldest_pg_only_hours"]))
        else:
            print("  [L2 退化测试    ] 不可判定：%s" % (l2.get("error") or "skipped"))
        if res.get("compare"):
            c = res["compare"]

            def _pair(queue, key):
                v = queue.get(key) or [None, None]
                return (v[0], v[1])

            print("  [两个时点对照  ] 全局 %s→%s h · L1 率 %s→%s · L1 天花板 %s→%s h ·"
                  " L2 PG独有 %s→%s 条（最旧 %s→%s h）"
                  % (_pair(c, "global_age_hours") + _pair(c, "l1_rate")
                     + _pair(c, "l1_ceiling_hours") + _pair(c, "l2_pg_only")
                     + _pair(c, "l2_oldest_hours")))
        print("  量级限制（照抄 cross_store_reconcile_probe.py:86-87）：存在性**非单调** ⇒"
              " L1/L2 只作**量级**参考，**不得**读成「从某刻起停止同步」。")
        print("  口径限制（本轮实测）：SQLite 侧 `updated_at` **会被本地改写** ⇒ 负滞后单列"
              "『副本反而更新』，不计入陈旧；`created_at` 两侧相等（忠实）⇒ 天花板用它。")
        print("  rc：0=fresh · 1=stale · 2=不可判定 ⇒ 本次 rc=%d" % res["rc"])
    return int(res["rc"])


if __name__ == "__main__":
    #: ⚠️ 零异常面：`hasattr` 判据 ⇒ 不写 `try/except`（给静默失败棘轮留零个吞点）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
