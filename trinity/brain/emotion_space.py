# -*- coding: utf-8 -*-
"""trinity/brain/emotion_space.py — 情绪知识空间（EXECUTION 226，大脑化）。

借鉴 Nature Communications 2026（Map-like emotion knowledge in
hippocampal-prefrontal systems）——情绪概念在脑中以"地图"形式
表征（空间邻近 = 情绪相似）。

Trinity 现在：
  build_space(): 从记忆构建情绪空间（valence-arousal 坐标聚类）
  emotion_neighbors(query): 情绪邻近检索（按情绪坐标找相近记忆）

记忆不只按内容组织，还按情绪坐标组织（"让我难过过的记忆"）。
"""
import os
import sys
import json


def _emotion_coord(text: str) -> tuple:
    """记忆内容 → 情绪坐标（valence, arousal）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.affect import assess
        r = assess(str(text)[:200])
        v = float(r.get("valence", 0))
        a = float(r.get("arousal", 0))
        return (v, a)
    except Exception:
        return (0.0, 0.0)


def build_space(limit: int = 50) -> dict:
    """构建情绪空间：记忆 → (valence, arousal) 坐标。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("""
            SELECT memory_id, left(content, 150) FROM memories
            WHERE status='active' AND embedding IS NOT NULL
              AND category NOT IN ('perception', 'dcpm-core')
            ORDER BY RANDOM() LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
        points = []
        for mid, content in rows:
            v, a = _emotion_coord(content)
            points.append({"memory_id": mid, "valence": round(v, 2),
                           "arousal": round(a, 2),
                           "content": str(content)[:40]})
        # 情绪象限统计
        quadrants = {"positive": 0, "negative": 0, "neutral": 0}
        for p in points:
            if p["valence"] > 0.2:
                quadrants["positive"] += 1
            elif p["valence"] < -0.2:
                quadrants["negative"] += 1
            else:
                quadrants["neutral"] += 1
        return {"points": points, "count": len(points), "quadrants": quadrants}
    except Exception as e:
        return {"error": str(e)[:80]}


def emotion_neighbors(query: str, top_k: int = 3) -> dict:
    """情绪邻近检索：按查询情绪坐标找相近记忆。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("""
            SELECT memory_id, left(content, 120) FROM memories
            WHERE status='active'
              AND category NOT IN ('perception', 'dcpm-core')
            ORDER BY RANDOM() LIMIT 30
        """)
        rows = cur.fetchall()
        conn.close()
        qv, qa = _emotion_coord(query)
        scored = []
        for mid, content in rows:
            v, a = _emotion_coord(content)
            dist = ((v - qv) ** 2 + (a - qa) ** 2) ** 0.5
            scored.append({"memory_id": mid, "content": str(content)[:40],
                           "emotion_dist": round(dist, 3), "valence": v})
        scored.sort(key=lambda x: x["emotion_dist"])
        return {"query_emotion": {"valence": round(qv, 2), "arousal": round(qa, 2)},
                "neighbors": scored[:top_k], "count": len(scored)}
    except Exception as e:
        return {"error": str(e)[:80]}
