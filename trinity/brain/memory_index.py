# -*- coding: utf-8 -*-
"""trinity/brain/memory_index.py — 记忆索引（EXECUTION 247，大脑化）。

借鉴 The Library Theorem（2026：Indexed Agent Memory）——外部组织
（索引）治理推理容量：图书馆的索引让知识可高效访问。

Trinity 现在：
  build_index(): 构建记忆索引（类别/主题/时间——组织化）
  index_lookup(key): 索引查找（快速定位记忆）
"""
import os
import sys
import json


def build_index(limit: int = 200) -> dict:
    """构建记忆索引：类别索引 + 主题索引。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 类别索引
        cur.execute("SELECT category, count(*) FROM memories WHERE status='active' GROUP BY category ORDER BY count(*) DESC LIMIT 15")
        by_category = {r[0]: r[1] for r in cur.fetchall()}
        # 主题索引（高频词）
        cur.execute("SELECT left(content, 80) FROM memories WHERE status='active' AND category NOT IN ('perception','dcpm-core') ORDER BY RANDOM() LIMIT %s", (limit,))
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        from collections import Counter
        words = Counter()
        for t in rows:
            t = str(t or "")
            for i in range(len(t) - 1):
                if "\u4e00" <= t[i] <= "\u9fff" and "\u4e00" <= t[i+1] <= "\u9fff":
                    words[t[i:i+2]] += 1
        stop = {"系统", "状态", "我的", "进行", "可以", "需要", "这个", "相关", "我们"}
        by_topic = {w: c for w, c in words.most_common(12) if w not in stop}
        return {"by_category": by_category, "by_topic": by_topic,
                "category_count": len(by_category), "topic_count": len(by_topic)}
    except Exception as e:
        return {"error": str(e)[:80]}


def index_lookup(key: str) -> dict:
    """索引查找：按类别或主题定位记忆。"""
    idx = build_index()
    # 类别匹配
    cat_hits = [c for c in idx.get("by_category", {}) if key in c]
    # 主题匹配
    topic_hits = [t for t, c in idx.get("by_topic", {}).items() if key in t]
    return {"category_matches": cat_hits[:3],
            "topic_matches": topic_hits[:5],
            "total_indexed_categories": idx.get("category_count", 0),
            "total_indexed_topics": idx.get("topic_count", 0)}
