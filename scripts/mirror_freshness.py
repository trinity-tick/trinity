#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mirror_freshness.py —— **镜像新鲜度**（只读）：SQLite 相对 PG 的派生镜像滞后了多少小时？

## 为什么有它（t159 定案 → G17/t160 落地）
PG 是**唯一写主**，SQLite 是**派生镜像**（镜像脚本 `scripts/backfill_sqlite_from_pg.py`）。
镜像的**滞后上限**此前**没有写进契约** ⇒ 出现"**写进 PG 却读不到**"时，
没人能一眼回答"这是预期滞后，还是镜像坏了"。本脚本把这件事变成**可观测读数**。

数据源（**已存在，无需新写任何库**）：
  SQLite `audit_log` 里 `action IN ('PG_MIRROR_STATUS','PG_BACKFILL')` 的 **`max(timestamp)`**
  —— 2026-10-08 实测：`PG_MIRROR_STATUS` 42,660 条 / `PG_BACKFILL` 76,556 条，
  最近一条 `2026-10-07T19:05:04Z`（= `2026-10-08 03:05:04 +08`）。

## 口径（**限制随数字同行**）
- ⭐ **只读**：以 `file:...?mode=ro` 打开 SQLite；**不写任何库**。
- ⭐ **tz-aware**：时间戳先解析成 **aware datetime** 再相减（t158 D1/D2 的教训：
  裸 `datetime` 相减会给出整齐的 **±28800 s** 假读数）。缺时区的字符串按 **UTC** 解释并**显式标记**
  （输出里的 `assumed_tz`）。
- ⭐ **DB 要报出来**：默认在候选库里挑一个**真的含 `PG_MIRROR_STATUS` 行**的（输出 `db` 字段），
  避免"读的是哪个库"说不清。
- 退出码：**0 = 新鲜** · **1 = 陈旧（超上限）** · **2 = 无法判定**（库缺失/无镜像行）。

## 用法
    python scripts/mirror_freshness.py                       # 默认上限 24h
    python scripts/mirror_freshness.py --max-age-hours 6
    python scripts/mirror_freshness.py --now 2026-10-08T03:05:04Z   # 复盘/对照（把"现在"钉住）
    python scripts/mirror_freshness.py --json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys

#: ⚠️ G17/t160（与 t162/G19 的交接）：本文件**不引入任何 try/except**。
#: t162/G19 先把退出路径的静默 `pass` 改成"吞但计数"的 `swallow(...)`，但**结构门禁仍点名本文件**
#: （`silent_failure:no_growth total=335 > 基线 334`）⇒ 我改为**零异常面**写法：
#: 退出路径用 `hasattr` 判据（不需要 try）、`trinity._swallow` 也不再需要导入 ⇒ 棘轮回到基线。

MIRROR_ACTIONS = ("PG_MIRROR_STATUS", "PG_BACKFILL")
TS_CANDIDATES = ("timestamp", "ts", "created_at", "created")


def parse_ts(raw) -> "_dt.datetime":
    """把时间戳解析成 **aware** datetime（缺时区 ⇒ 按 UTC 并标记）。"""
    if isinstance(raw, _dt.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=_dt.timezone.utc)
    s = str(raw or "").strip()
    if not s:
        raise ValueError("空时间戳")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                    "%Y-%m-%dT%H:%M:%S"):
            try:
                d = _dt.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            raise
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def age_hours(last, now=None) -> float:
    """距上次镜像多少小时（**aware 相减**；负数=时间戳在未来，原样返回）。"""
    a = parse_ts(last)
    b = parse_ts(now) if now is not None else _dt.datetime.now(_dt.timezone.utc)
    return (b - a).total_seconds() / 3600.0


def classify(age_h: float, max_age_h: float) -> str:
    """`fresh` / `stale`（**严格大于**上限才算陈旧 ⇒ 阈值 0 时任何正滞后即陈旧）。"""
    return "stale" if age_h > max_age_h else "fresh"


def _candidates() -> list:
    env = os.environ.get("TRINITY_STORE_DB")
    home = os.path.expanduser("~")
    out = []
    if env:
        out.append(env)
    out += [os.path.join(home, ".trinity", "store-restored", "trinity_store.db"),
            os.path.join(home, ".trinity", "store", "trinity_store.db")]
    seen, res = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            res.append(p)
    return res


def probe(db: str) -> dict:
    """只读探测一个库：返回 {ok, rows:{action:count}, ts_col, last_raw, last_parsed}。"""
    res = {"db": db, "ok": False, "rows": {}, "ts_col": None, "last_raw": None, "error": None}
    if not os.path.isfile(db):
        res["error"] = "文件不存在"
        return res
    try:
        uri = "file:%s?mode=ro" % db.replace("\\", "/")
        con = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            have = {r[1] for r in con.execute("PRAGMA table_info(audit_log)")}
            if not have:
                res["error"] = "没有 audit_log 表"
                return res
            col = next((c for c in TS_CANDIDATES if c in have), None)
            if col is None:
                res["error"] = "audit_log 里找不到时间戳列（候选 %s）" % (TS_CANDIDATES,)
                return res
            res["ts_col"] = col
            for a in MIRROR_ACTIONS:
                n = con.execute("SELECT COUNT(*) FROM audit_log WHERE action=?", (a,)).fetchone()[0]
                res["rows"][a] = int(n)
            row = con.execute(
                "SELECT action, %s FROM audit_log WHERE action IN (%s) "
                "ORDER BY %s DESC LIMIT 1"
                % (col, ",".join("?" * len(MIRROR_ACTIONS)), col), MIRROR_ACTIONS).fetchone()
            if row:
                res["last_action"], res["last_raw"] = str(row[0]), row[1]
                res["ok"] = True
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        res["error"] = repr(e)
    return res


def pick_db(explicit=None):
    """返回 (probe结果, 试过哪些)。显式给 `--db` 就只试它。"""
    tried = []
    for cand in ([explicit] if explicit else _candidates()):
        p = probe(cand)
        tried.append(p)
        if p["ok"]:
            return p, tried
    return (tried[0] if tried else {"db": None, "ok": False, "error": "无候选库"}), tried


def main() -> int:
    ap = argparse.ArgumentParser(description="镜像新鲜度（只读）")
    ap.add_argument("--db", help="显式指定 SQLite 库；缺省自动挑含 PG_MIRROR_STATUS 的")
    ap.add_argument("--max-age-hours", type=float, default=24.0,
                    help="滞后上限（小时）；默认 24 = 镜像契约的'约 1 个镜像周期'")
    ap.add_argument("--now", help="把'现在'钉住（ISO8601，复盘/对照用）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    p, tried = pick_db(a.db)
    out = {"db": p.get("db"), "ok": bool(p.get("ok")), "ts_col": p.get("ts_col"),
           "mirror_rows": p.get("rows") or {}, "max_age_hours": a.max_age_hours,
           "error": p.get("error")}
    if not p.get("ok"):
        out.update({"verdict": "untestable", "reason": p.get("error") or "无镜像行",
                    "tried": [{"db": t.get("db"), "error": t.get("error"),
                               "mirror_rows": t.get("rows") or {}} for t in tried]})
        print(json.dumps(out, ensure_ascii=False, indent=1) if a.json else
              "[untestable] 无法判定：%s（试过：%s）" % (out["reason"], out["db"]))
        return 2
    last = parse_ts(p["last_raw"])
    now = parse_ts(a.now) if a.now else _dt.datetime.now(_dt.timezone.utc)
    h = age_hours(last, now)
    verdict = classify(h, a.max_age_hours)
    out.update({"last_mirror_utc": last.astimezone(_dt.timezone.utc).isoformat(),
                "last_mirror_action": p.get("last_action"),
                "now_utc": now.astimezone(_dt.timezone.utc).isoformat(),
                "age_hours": round(h, 2), "verdict": verdict,
                "assumed_tz": not (isinstance(p["last_raw"], str) and
                                   (p["last_raw"].endswith("Z") or "+" in p["last_raw"][10:]))})
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print("镜像新鲜度（只读）—— 库：%s" % out["db"])
        print("  audit_log 行数：%s" % out["mirror_rows"])
        print("  上次镜像：%s（%s）" % (out["last_mirror_utc"], out.get("last_mirror_action")))
        print("  现在    ：%s" % out["now_utc"])
        print("  滞后    ：%.2f 小时（上限 %.2f）⇒ **%s**" % (h, a.max_age_hours, verdict))
        if out["assumed_tz"]:
            print("  ⚠️ 原时间戳缺时区 ⇒ 已按 UTC 解释（assumed_tz=True）")
    return 0 if verdict == "fresh" else 1


if __name__ == "__main__":
    #: ⚠️ 零异常面：`hasattr` 判据 ⇒ 不写 `try/except`（给静默失败棘轮留零个吞点）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
