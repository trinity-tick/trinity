# -*- coding: utf-8 -*-
"""trinity/brain/spatiotemporal_memory.py — 时空情景记忆（EXECUTION 224，大脑化）。

借鉴 ARTEM（AAAI 2026：Spatial-Temporal Episodic Memory）——情景
记忆的核心维度是"何时何地发生了什么"。Trinity 现在：
  episode(query, time_window, source): 时空情景检索（时间+来源过滤）
  timeline_with_sources(): 带来源的时间线（叙事的空间维度）

记忆组织：内容（是什么）+ 时间（何时）+ 来源（何处/空间）。
"""
import os
import sys
import json
from datetime import datetime, timedelta


def episode(query: str = "", time_window_days: int = 30, source: str = "",
            limit: int = 5) -> dict:
    """时空情景检索：按时间窗口 + 来源过滤。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        conds = ["status='active'"]
        params = []
        if query:
            conds.append("content ILIKE %s")
            params.append(f"%{query[:30]}%")
        if time_window_days:
            conds.append("created_at::timestamp > NOW() - make_interval(days => %s)")
            params.append(time_window_days)
        if source:
            conds.append("content LIKE %s")
            params.append(f"%[{source}%")
        where = " AND ".join(conds)
        cur.execute(f"SELECT left(content, 80), created_at FROM memories WHERE {where} ORDER BY created_at DESC LIMIT %s",
                    params + [limit])
        eps = [{"content": r[0], "time": str(r[1])[:19]} for r in cur.fetchall()]
        conn.close()
        return {"episodes": eps, "count": len(eps),
                "window_days": time_window_days, "source": source or "all"}
    except Exception as e:
        return {"error": str(e)[:80]}


def timeline_with_sources(days: int = 30, limit: int = 10) -> dict:
    """带来源的时间线（空间+时间叙事）。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("""
            SELECT left(content, 60), created_at FROM memories
            WHERE status='active'
              AND created_at::timestamp > NOW() - make_interval(days => %s)
            ORDER BY created_at DESC LIMIT %s
        """, (days, limit))
        rows = cur.fetchall()
        conn.close()
        timeline = []
        for content, created_at in rows:
            t = str(content or "")
            src = "未知"
            if "[web" in t:
                src = "网络"
            elif "[log" in t:
                src = "日志"
            elif "[filesystem" in t:
                src = "文件"
            elif "[self-reflection]" in t or "[self-" in t:
                src = "自我"
            timeline.append({"time": str(created_at)[:16], "source": src,
                             "content": t[:50]})
        return {"timeline": timeline, "count": len(timeline)}
    except Exception as e:
        return {"error": str(e)[:80]}
