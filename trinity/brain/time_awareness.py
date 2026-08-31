# -*- coding: utf-8 -*-
"""trinity/brain/time_awareness.py — 时间意识（EXECUTION 246，大脑化）。

借鉴 ADR-251（Agentic Time as First-Class Primitive）/ chronos
（时间意识修复时间盲）——Trinity 有时间数据但缺"时间感知"：
知道自己"现在"在哪里、多久没做某事、节律是否正常。

Trinity 现在：
  now_context(): 当前时间上下文（时段/今天/星期）
  time_since(event_type): 距上次事件时长（自省/搜索/整合）
  rhythm_status(): 每日节律状态
"""
import os
import sys
import json
import time
from datetime import datetime


def now_context() -> dict:
    """当前时间上下文。"""
    now = datetime.now()
    hour = now.hour
    if hour < 6:
        period = "深夜"
    elif hour < 12:
        period = "上午"
    elif hour < 14:
        period = "中午"
    elif hour < 18:
        period = "下午"
    else:
        period = "晚上"
    weekday = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
    return {"now": now.strftime("%Y-%m-%d %H:%M"), "period": period,
            "weekday": f"星期{weekday}",
            "note": f"现在是{period}（星期{weekday}）"}


def time_since(event_type: str) -> dict:
    """距上次事件时长（自省/搜索/整合等）。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        if event_type == "reflection":
            cur.execute("SELECT max(created_at) FROM memories WHERE category='self-reflection'")
        elif event_type == "consolidation":
            cur.execute("SELECT max(created_at) FROM memories WHERE category='dcpm-schema'")
        else:
            cur.execute("SELECT max(created_at) FROM memories WHERE category='perception'")
        r = cur.fetchone()
        conn.close()
        if not r or not r[0]:
            return {"event": event_type, "since": None, "note": "从未发生"}
        ts = r[0]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        hours = (datetime.now(ts.tzinfo if ts.tzinfo else None) - ts).total_seconds() / 3600
        return {"event": event_type, "since_hours": round(hours, 1),
                "note": f"距上次{event_type}约 {round(hours, 1)} 小时"}
    except Exception as e:
        return {"event": event_type, "error": str(e)[:60]}


def rhythm_status() -> dict:
    """每日节律：自省/整合/感知是否按时（24h 内）。"""
    ref = time_since("reflection")
    con = time_since("consolidation")
    perc = time_since("perception")
    checks = {}
    for name, r in (("reflection", ref), ("consolidation", con), ("perception", perc)):
        h = r.get("since_hours")
        checks[name] = "ok" if (h is not None and h <= 24) else ("stale" if h else "unknown")
    return {"checks": checks,
            "healthy": all(v == "ok" for v in checks.values()),
            "note": "节律正常" if all(v == "ok" for v in checks.values()) else "有环节需要关注"}
