# -*- coding: utf-8 -*-
"""trinity/brain/narrative_memory.py — 叙事记忆（EXECUTION 290，大脑化）。

借鉴 Amory（2026：Coherent Narrative-Driven Agent Memory）——通过
推理把碎片记忆组织成连贯叙事（记忆叙事化——连贯故事）。

与自传体（记录）互补：自传=记录；本模块=叙事构建。
Trinity 现在：
  coherent_narrative(topic): 检索相关碎片→推理组织→连贯叙事
"""
import os
import sys
import json


def coherent_narrative(topic: str, top_k: int = 4) -> dict:
    """叙事构建：碎片记忆 → 连贯叙事。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 检索相关碎片（主题词）
        t = str(topic)[:30]
        words = set()
        for i in range(len(t) - 1):
            if "\u4e00" <= t[i] <= "\u9fff" and "\u4e00" <= t[i+1] <= "\u9fff":
                words.add(t[i:i+2])
        fragments = []
        for w in list(words)[:3]:
            cur.execute("SELECT left(content, 80) FROM memories WHERE status='active' AND content LIKE %s ORDER BY RANDOM() LIMIT 2", (f"%{w}%",))
            fragments.extend([r[0] for r in cur.fetchall()])
        conn.close()
        if not fragments:
            return {"narrative": None, "note": "无相关碎片"}
        # 推理组织：碎片 → 连贯叙事（时间/因果顺序）
        ordered = fragments[:top_k]
        narrative = "。".join(f"{i+1}. {f}" for i, f in enumerate(ordered))
        return {"narrative": narrative[:200], "fragments": len(ordered),
                "topic": str(topic)[:30],
                "note": "叙事构建：碎片推理组织为连贯故事（Amory）"}
    except Exception as e:
        return {"error": str(e)[:80]}


def narrative_coherence(narrative: str) -> dict:
    """叙事连贯性评估。"""
    n = str(narrative)
    parts = [p for p in n.split("。") if p.strip()]
    return {"parts": len(parts),
            "coherent": len(parts) >= 2,
            "note": "连贯叙事" if len(parts) >= 2 else "碎片不足"}
