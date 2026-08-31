# -*- coding: utf-8 -*-
"""trinity/brain/episodic_semantic.py — 情景-语义双系统（EXECUTION 227，大脑化）。

借鉴 Episodic-Semantic Memory Architecture（2026：long-horizon agents）——
认知的基础双系统：海马情景记忆（具体事件）+ 皮层语义记忆（一般知识）。

Trinity 现在：
  episodic_to_semantic(): 情景记忆 → 语义概括（提取一般规律）
  semantic_recall(): 语义知识检索（一般知识——区分于具体事件）

与 DCPM（归纳信念）/ 重放（情节→语义）互补：本模块提供显式的
情景-语义分层检索接口。
"""
import os
import sys
import json


def episodic_to_semantic(limit: int = 20) -> dict:
    """情景 → 语义：从具体事件提取一般规律。"""
    try:
        import psycopg2
        from collections import Counter
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        # 取情节类记忆（非感知/自我）
        cur.execute("""
            SELECT left(content, 150) FROM memories
            WHERE status='active' AND category NOT IN ('perception', 'self-identity',
                                                        'dcpm-core', 'self-reflection')
            ORDER BY RANDOM() LIMIT %s
        """, (limit,))
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        # 语义提取：高频词 + 模式（"经验"类内容）
        words = Counter()
        for t in rows:
            t = str(t or "")
            for w in t.split()[:15]:
                w2 = w.strip("[]:，。；、")
                if 2 <= len(w2) <= 12:
                    words[w2] += 1
        # 语义规律：高频概念（>=2 次出现 = 一般规律候选）
        semantics = [w for w, c in words.most_common(10) if c >= 2][:5]
        return {"semantic_concepts": semantics, "source_episodes": len(rows),
                "extracted": len(semantics)}
    except Exception as e:
        return {"error": str(e)[:80]}


def semantic_recall(query: str, top_k: int = 3) -> dict:
    """语义检索：一般知识（概念/规律）——与具体事件区分。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        # 语义类记忆（dcpm-schema = System2 归纳的一般信念）
        cur.execute("""
            SELECT left(content, 100) FROM memories
            WHERE category IN ('dcpm-schema', 'promoted', 'reconstructive')
              AND content ILIKE %s
            ORDER BY created_at DESC LIMIT %s
        """, (f"%{query[:20]}%", top_k))
        semantic = [r[0] for r in cur.fetchall()]
        # 对照：具体事件（情景）
        cur.execute("""
            SELECT left(content, 80) FROM memories
            WHERE status='active' AND category NOT IN ('perception', 'dcpm-schema')
              AND content ILIKE %s
            ORDER BY RANDOM() LIMIT 1
        """, (f"%{query[:20]}%",))
        episodic = [r[0] for r in cur.fetchall()]
        conn.close()
        return {"semantic": semantic, "episodic_example": episodic[:1],
                "semantic_count": len(semantic)}
    except Exception as e:
        return {"error": str(e)[:80]}
