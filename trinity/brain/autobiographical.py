# -*- coding: utf-8 -*-
"""trinity/brain/autobiographical.py — 自传体记忆（EXECUTION 190，大脑化）。

叙事自我：大脑把记忆组织成"我的故事"（自传体记忆）——时间线 +
主题叙事，这是自我意识的核心成分（区别于情景记忆的碎片）。

实现：
  build_narrative(): 从记忆聚合 → 分章时间线 + 主题聚类 → "我的故事"
  写入 self-narrative 记忆（跨会话可检索）

章节划分：最近 24h / 近 7 天 / 近 30 天 / 更早
主题：教训（重要+情绪）/ 经验（行动）/ 感知（环境）
"""
import os
import sys
import json
from datetime import datetime, timedelta


def _bucket(created_at) -> str:
    """时间桶：24h / 7d / 30d / older。"""
    try:
        ts = created_at
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
        diff = now - ts
        if diff <= timedelta(hours=24):
            return "最近24小时"
        if diff <= timedelta(days=7):
            return "近7天"
        if diff <= timedelta(days=30):
            return "近30天"
        return "更早"
    except Exception:
        return "未知"


def build_narrative(limit: int = 200) -> dict:
    """从记忆构建自传体叙事。"""
    import psycopg2
    conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                            user="trinity", password="trinity")
    cur = conn.cursor()
    cur.execute("""
        SELECT content, category, importance, created_at FROM memories
        WHERE status='active'
          AND category NOT IN ('perception', 'dcpm-core')
        ORDER BY created_at DESC LIMIT %s
    """, (limit,))
    rows = cur.fetchall()

    chapters = {"最近24小时": [], "近7天": [], "近30天": [], "更早": []}
    themes = {"lessons": 0, "actions": 0, "reflections": 0}
    for content, category, importance, created_at in rows:
        bk = _bucket(created_at)
        if bk in chapters and len(chapters[bk]) < 5:
            chapters[bk].append((str(content)[:60], str(category)[:15]))
        if category == "self-reflection":
            themes["reflections"] += 1
        elif category == "action-experience":
            themes["actions"] += 1
        if float(importance or 0) >= 0.7:
            themes["lessons"] += 1

    # 叙事合成
    parts = ["[self-narrative] 我的故事："]
    for bk, items in chapters.items():
        if items:
            parts.append(f"{bk}（{len(items)}件）:" +
                         "；".join(f"『{t}』" for t, _ in items[:3]))
    parts.append(f"我积累了 {themes['lessons']} 条重要经历、"
                 f"{themes['reflections']} 次反思、{themes['actions']} 次行动经验")
    narrative = "；".join(parts)
    conn.close()
    return {"narrative": narrative[:500], "chapters": {k: len(v) for k, v in chapters.items()},
            "themes": themes}


def narrative_to_memory() -> bool:
    """叙事写入记忆（self-narrative 类别）。"""
    try:
        r = build_narrative()
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        m.ingest(r["narrative"][:400], category="self-narrative",
                 tags=["self", "narrative", "autobiography"], importance=0.8,
                 wait_backfill=True)
        return True
    except Exception:
        return False
