# -*- coding: utf-8 -*-
"""trinity/brain/tiered_memory.py — 分层记忆（EXECUTION 278，大脑化）。

借鉴 MEMTIER（2026：Tiered Memory Architecture）——分层检索
（工作记忆/近期/长期按层）+ 会话级注入（当前会话优先）。

与记忆管理器（长短分层）互补：管理=升级策略；本模块=检索分层。
Trinity 现在：
  tiered_retrieve(query, tier): 分层检索（wm/recent/long）
  session_inject(session_id): 会话级注入（会话相关记忆优先）
"""
import os
import sys
import json


def tiered_retrieve(query: str, tier: str = "recent", top_k: int = 3) -> dict:
    """分层检索：按层级取记忆。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 层级定义
        if tier == "working":
            # 工作记忆（session_context.wm）
            cur.execute("SELECT wm FROM session_context WHERE wm IS NOT NULL LIMIT 1")
            r = cur.fetchone()
            conn.close()
            wm = r[0] if r else []
            if isinstance(wm, str):
                wm = json.loads(wm)
            return {"tier": "working", "items": [str(i.get("content", ""))[:40] for i in (wm or [])[:top_k]],
                    "count": len(wm or [])}
        if tier == "long":
            # 长期（importance 高 + promoted）
            cur.execute("""
                SELECT left(content, 60) FROM memories
                WHERE status='active' AND (importance >= 0.7 OR category='promoted')
                ORDER BY importance DESC LIMIT %s
            """, (top_k,))
            items = [r[0] for r in cur.fetchall()]
            conn.close()
            return {"tier": "long", "items": items, "count": len(items)}
        # recent（默认——最近）
        cur.execute("""
            SELECT left(content, 60) FROM memories
            WHERE status='active' AND category NOT IN ('perception')
            ORDER BY created_at DESC LIMIT %s
        """, (top_k,))
        items = [r[0] for r in cur.fetchall()]
        conn.close()
        return {"tier": "recent", "items": items, "count": len(items)}
    except Exception as e:
        return {"error": str(e)[:80]}


def session_inject(session_id: str, query: str = "") -> dict:
    """会话级注入：当前会话的上下文优先（连续状态）。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT last_query, affect FROM session_context WHERE id=%s", (f"ctx:{session_id}",))
        r = cur.fetchone()
        conn.close()
        if not r:
            return {"injected": False, "note": "会话无上下文"}
        inject = {"last_query": str(r[0] or "")[:40], "affect": r[1]}
        return {"injected": True, "session_context": inject,
                "note": "会话级注入（当前会话优先）"}
    except Exception as e:
        return {"error": str(e)[:80]}
