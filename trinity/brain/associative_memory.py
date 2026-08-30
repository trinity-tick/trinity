# -*- coding: utf-8 -*-
"""trinity/brain/associative_memory.py — 联想记忆（EXECUTION 192，大脑化）。

激活扩散（spreading activation）：大脑检索一条记忆时会顺带激活
关联记忆（编码时关联）。Trinity 现在：
  - 联想跳跃：从记忆 A 按向量相似找到关联 B（激活扩散）
  - 创造性组合：组合不相关主题生成新关联（发散思维）

实现：associative_jump（A→B 关联发现）+ creative_mix（主题组合）。
"""
import os
import sys
import json


def associative_jump(memory_id: str, top_k: int = 3) -> dict:
    """从记忆 A 联想跳跃到关联记忆 B（激活扩散）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        import psycopg2
        from trinity.core.client._helpers import _get_embedding_engine
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT content, embedding FROM memories WHERE memory_id=%s AND embedding IS NOT NULL", (memory_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return {"jumped": False, "error": "no embedding"}
        content_a, emb = row
        import ast
        vec = ast.literal_eval(emb) if isinstance(emb, str) else emb
        vec_str = "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"
        # 用前 64 维近似检索（避免全维嵌入）
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("""
            SELECT memory_id, left(content,60), 1-(embedding <=> %s::vector) as sim
            FROM memories
            WHERE memory_id != %s AND embedding IS NOT NULL
              AND category NOT IN ('perception', 'dcpm-core')
            ORDER BY embedding <=> %s::vector LIMIT %s
        """, (vec_str, memory_id, vec_str, top_k))
        assoc = [{"memory_id": r[0], "content": r[1], "similarity": round(float(r[2]), 3)}
                 for r in cur.fetchall()]
        conn.close()
        return {"jumped": True, "source": memory_id[:10],
                "associations": assoc, "count": len(assoc)}
    except Exception as e:
        return {"jumped": False, "error": str(e)[:80]}


def creative_mix(topics: list, limit: int = 20) -> dict:
    """创造性组合：从不同主题各取记忆，组合生成新关联（发散）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        picks = {}
        for t in topics[:3]:
            cur.execute("""
                SELECT memory_id, left(content, 50) FROM memories
                WHERE status='active' AND content ILIKE %s
                  AND category NOT IN ('perception', 'dcpm-core')
                ORDER BY RANDOM() LIMIT 1
            """, (f"%{t}%",))
            r = cur.fetchone()
            if r:
                picks[t] = {"memory_id": r[0], "content": r[1]}
        conn.close()
        # 组合叙事（创造性想法：把不同主题连接起来）
        combos = []
        keys = list(picks.keys())
        if len(keys) >= 2:
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    combos.append(f"联想：『{picks[keys[i]]['content']}』×『{picks[keys[j]]['content']}』")
        return {"picks": picks, "combinations": combos[:5], "count": len(combos)}
    except Exception as e:
        return {"error": str(e)[:80]}
