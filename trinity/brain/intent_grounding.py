# -*- coding: utf-8 -*-
"""trinity/brain/intent_grounding.py — 意图锚定（EXECUTION 259，大脑化）。

借鉴 Grounding Agent Memory in Contextual Intent（ACL 2026）——
当前意图/上下文作为检索线索（编码特异性：什么意图下记得什么）。

与情境检索（连续状态）互补：情境=过去状态；意图=当前目标。
Trinity 现在：
  ground_query(intent, query): 意图锚定（意图+上下文→增强检索线索）
"""
import os
import sys
import json


def ground_query(intent: str, query: str, context: str = "") -> dict:
    """意图锚定：当前意图注入检索线索。"""
    grounded = []
    if intent:
        grounded.append(f"[intent:{str(intent)[:30]}]")
    if context:
        grounded.append(str(context)[:50])
    grounded.append(str(query)[:80])
    grounded_query = " ".join(grounded)
    return {"grounded_query": grounded_query,
            "intent": str(intent)[:30] or "无",
            "components": len(grounded),
            "note": "意图锚定检索（编码特异性——按意图回忆）"}


def intent_retrieval(intent: str, query: str, top_k: int = 3) -> dict:
    """意图检索：用意图+查询检索记忆（意图匹配优先）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 意图相关记忆（含意图词的记忆优先）
        cur.execute("""
            SELECT left(content, 60), created_at FROM memories
            WHERE status='active' AND content ILIKE %s
            ORDER BY created_at DESC LIMIT %s
        """, (f"%{str(intent)[:15]}%", top_k))
        intent_hits = [r[0] for r in cur.fetchall()]
        conn.close()
        return {"intent": str(intent)[:30], "query": str(query)[:30],
                "intent_hits": intent_hits, "count": len(intent_hits)}
    except Exception as e:
        return {"error": str(e)[:80]}
