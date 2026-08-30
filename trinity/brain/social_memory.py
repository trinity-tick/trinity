# -*- coding: utf-8 -*-
"""trinity/brain/social_memory.py — 社会记忆（EXECUTION 194，大脑化）。

社会脑：人类通过文化/语言共享知识（一个个体学会的，群体都会）。
Trinity 多 Agent 场景：
  - share_knowledge：Agent A 的某主题经验 → 摘要共享（全局可检索）
  - social_recall：Agent 检索时联合共享知识（社会回忆）

与市场（交易）互补：市场=有偿交易；社会记忆=免费知识传播（文化）。
"""
import os
import sys
import json


def share_knowledge(from_agent: str, topic: str, max_picks: int = 3) -> dict:
    """知识传播：Agent 把某主题经验共享为全局知识（社会记忆）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        # 取该 agent 的该主题记忆
        cur.execute("""
            SELECT content FROM memories
            WHERE status='active' AND content ILIKE %s
              AND category NOT IN ('perception', 'dcpm-core')
            ORDER BY importance DESC NULLS LAST LIMIT %s
        """, (f"%{topic}%", max_picks))
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        if not rows:
            return {"shared": False, "error": "no knowledge"}
        # 摘要共享（social 类别——全局可检索）
        digest = "；".join(str(r)[:80] for r in rows[:2])
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        m.ingest(
            f"[social-share] {from_agent} 分享的『{topic}』经验：{digest[:150]}",
            category="social-memory", tags=["social", topic, from_agent],
            importance=0.75, wait_backfill=True)
        return {"shared": True, "from": from_agent, "topic": topic,
                "sources": len(rows)}
    except Exception as e:
        return {"shared": False, "error": str(e)[:80]}


def social_recall(query: str, agent: str = "default", top_k: int = 5) -> dict:
    """社会回忆：检索自己 + 其他 agent 共享的知识（文化记忆）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        # 自己的记忆
        own = m.search_hybrid(query, top_k=top_k, session_id="default")
        own_items = own if isinstance(own, list) else own.get("results", [])
        # 社会共享记忆
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("""
            SELECT left(content, 100) FROM memories
            WHERE category='social-memory' AND content ILIKE %s
            ORDER BY created_at DESC LIMIT %s
        """, (f"%{query[:20]}%", top_k))
        social = [r[0] for r in cur.fetchall()]
        conn.close()
        return {"own_hits": len(own_items), "social_hits": len(social),
                "social": social[:3]}
    except Exception as e:
        return {"error": str(e)[:80]}
