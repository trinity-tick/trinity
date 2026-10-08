#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fok_backlog_trend.py — 按 §1085 定稿量法看 fok 待办趋势（每轮之间，不看墙上时钟）。

判据（三支，来自 EXECUTION §1084/§1085 的 ACK 反向保护）：
  · pending_after 单调下降                      ⇒ ACK 成立（已知·正在收敛）
  · 走平/反弹 且 prewarm_added>0                ⇒ 登记又来了（先看谁在登记）
  · 走平/反弹 且 prewarm_added=0                ⇒ 机制变了 ⇒ 撤 ACK、重新定性
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg2
import yaml


def trend_verdict(seq, prewarm_added=0, tol=0):
    """纯函数：seq 是**按时间正序**的 pending_after 序列 ⇒ 返回 (verdict, note)。"""
    seq = [int(x) for x in seq if x is not None]
    if len(seq) < 2:
        return "NA", "样本不足（少于 2 轮）⇒ 不判"
    delta = seq[-1] - seq[0]
    if delta < -tol:
        return "ACK_OK", "单调下降 %d→%d（%+d 条 / %d 轮）⇒ ACK 成立（已知·正在收敛）" % (
            seq[0], seq[-1], delta, len(seq) - 1)
    if prewarm_added > 0:
        return "REGISTERED", "走平/反弹（%d→%d，%+d）且本轮有新增 %d ⇒ 登记又来了：先看谁在登记" % (
            seq[0], seq[-1], delta, prewarm_added)
    return "MECHANISM_CHANGED", "走平/反弹（%d→%d，%+d）且**无新增** ⇒ 机制变了 ⇒ 撤 ACK、重新定性" % (
        seq[0], seq[-1], delta)


def post_spike_suffix(rows):
    """取**最后一次登记（prewarm_added>0）之后**的轮次。

    2026-09-21（§1087 实测纠正）：判据**不能**拿整个窗口首尾比 —— 实测真实序列
    (0, 57, 1902, 1890, 1878, 1866, 1854, 1842) 首尾比是 **+1842（涨）** ⇒ 会被误判成
    「机制变了」，而真相是「一次登记洪峰 + 其后单调下降」⇒ 判据要在**洪峰之后**看。
    """
    idx = None
    for i, r in enumerate(rows):
        if (r[3] or 0) > 0:
            idx = i
    return rows[idx:] if idx is not None else rows


def _conn():
    # t31：凭证走统一入口（修前顶层 .get ⇒ 版本化文件下恒空 ⇒ 空口令静默失败）
    from _pg_std import pg_creds
    c = pg_creds()
    return psycopg2.connect(host=c["host"], port=int(c["port"]), user=c["user"],
                            password=c["password"], dbname=c["dbname"], connect_timeout=5)


def main() -> int:
    # 2026-09-21（§1089）：控制台代码页可能不是 UTF-8（实测 GBK 下打印 "⇒" 直接 UnicodeEncodeError 崩掉，
    # 于是 rc 反映的是控制台而不是判据）⇒ 先把 stdout 钉成 utf-8/replace，判据的 rc 才可依赖。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=8)
    a = ap.parse_args()
    cn = _conn(); cn.autocommit = True; cur = cn.cursor()
    cur.execute("select ts::timestamp(0), filled, pending_after, prewarm_added "
                "from fok_counts_history order by ts desc limit %s", (a.rounds,))
    rows = cur.fetchall()[::-1]  # 正序
    cur.execute("select count(*) from fok_counts_pending")
    pending = cur.fetchone()[0]
    cur.execute("select count(*) from fok_counts")
    keys = cur.fetchone()[0]
    cn.close()
    eff = post_spike_suffix(rows)
    seq = [r[2] for r in eff]
    pre = eff[-1][3] if eff else 0
    verdict, note = trend_verdict(seq, pre)
    print("fok 待办趋势（按轮，近 %d 轮）" % len(rows))
    for r in rows:
        print("   %s  filled=%-3s pending_after=%-6s prewarm_added=%s" % r)
    print("当前：pending=%d keys=%d" % (pending, keys))
    print("判据窗口：洪峰后 %d 轮（共 %d 轮）" % (len(eff), len(rows)))
    print("verdict=%s ⇒ %s" % (verdict, note))
    return 0 if verdict in ("ACK_OK", "REGISTERED") else 1


if __name__ == "__main__":
    raise SystemExit(main())