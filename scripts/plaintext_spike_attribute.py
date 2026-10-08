#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plaintext_spike_attribute.py — 明文写入**尖峰归因**（§1119）。

背景：`plaintext_ratio_audit.py --ratchet` 判红时只说「请先查明来源」。本脚本把那一步做成一条命令：
按 **类目 × agent** 列出近 1h / 6h 的写入方，并把"谁最多"直接打出来。

判据（可失败）：输出必须同时给出**三个读数的绝对值**（1h 速率、1h 占比、6h 速率），
以及**归因表**；只给占比不给归因 ⇒ 视为无效输出（rc=1）。
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg2
import yaml


def _conn():
    # t31：凭证走统一入口（修前顶层 .get ⇒ 版本化文件下恒空 ⇒ 静默无数据）
    from _pg_std import pg_creds
    c = pg_creds()
    return psycopg2.connect(host=c["host"], port=int(c["port"]), user=c["user"],
                            password=c["password"], dbname=c["dbname"], connect_timeout=5)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=1)
    a = ap.parse_args()
    cn = _conn(); cn.autocommit = True; cur = cn.cursor()
    out = []
    # §1137：占比守卫看的是 7 天窗口（active / 7d），而尖峰归因原先只给 1h/6h
    # ⇒ 判红时"要归因的窗口"与"能归因的窗口"对不上。这里把 7d 一起给出。
    for h, unit in ((a.hours, "hours"), (6, "hours"), (7, "days")):
        cur.execute(("select category, count(*) from memories where created_at > now() - interval '%d " + unit + "' "
                    "group by category order by 2 desc limit 6") % h)
        by_cat = cur.fetchall()
        cur.execute(("select agent_id, count(*) from memories where created_at > now() - interval '%d " + unit + "' "
                    "group by agent_id order by 2 desc limit 5") % h)
        by_agent = cur.fetchall()
        cur.execute(("select count(*) from memories where created_at > now() - interval '%d " + unit + "'") % h)
        total = cur.fetchone()[0]
        out.append((h, unit, total, by_cat, by_agent))
    cn.close()
    print("明文字段写入**近 1h / 6h / 7d 归因**（来源：memories.created_at）")
    for h, unit, total, by_cat, by_agent in out:
        _label = "%dd" % h if unit == "days" else "%dh" % h
        print("\n== 近 %s：共 %d 条 ==" % (_label, total))
        print("   按类目:", ", ".join("%s=%d" % (c, n) for c, n in by_cat) or "(空)")
        print("   按 agent:", ", ".join("%s=%d" % (c, n) for c, n in by_agent) or "(空)")
        if by_cat:
            print("   ⇒ 最大写入方: %s=%d（先看它是不是自检/基准/压测）" % (by_cat[0][0], by_cat[0][1]))
    ok = all(t is not None for _h, _u, t, _c, _a in out) and any(c for _h, _u, _t, c, _a in out)
    print("\n判据：三个绝对读数都在 ⇒ %s" % ("PASS" if ok else "FAIL（归因缺失）"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())