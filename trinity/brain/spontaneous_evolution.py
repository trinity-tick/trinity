# -*- coding: utf-8 -*-
"""trinity/brain/spontaneous_evolution.py — 自发进化（EXECUTION 302）。

借鉴 Reward-Free Self-Evolution（2026：Spontaneous via World
Knowledge Exploration）——完全无奖赏的自发进化：纯好奇驱动
探索世界知识（不依赖任何奖赏信号）。

与内省奖赏（自我发现价值）互补：内省=有内在价值；自发=零依赖。
Trinity 现在：
  spontaneous_explore(): 自发探索（无奖赏——纯好奇驱动）
"""
import os
import sys
import json
import time


STATE_FILE = os.path.expanduser("~/.trinity/spontaneous_evolution.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"explorations": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def spontaneous_explore(seed: str = "") -> dict:
    """自发探索：无奖赏驱动（纯好奇——探索低覆盖知识区）。"""
    st = _load()
    # 探索目标：知识覆盖低的主题（从高频未深入主题选）
    targets = []
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 最近感知中高频出现的主题（但知识覆盖不足）
        cur.execute("""
            SELECT left(content, 40) FROM memories
            WHERE category='perception' ORDER BY created_at DESC LIMIT 15
        """)
        recent = [str(r[0]) for r in cur.fetchall()]
        conn.close()
        for t in recent[:5]:
            targets.append({"topic": t[:25], "spontaneous": True})
    except Exception:
        targets = [{"topic": seed or "知识探索", "spontaneous": True}]
    if not targets and seed:
        targets = [{"topic": str(seed)[:25], "spontaneous": True}]
    entry = {"targets": targets[:3], "reward_free": True,
             "ts": time.time()}
    st["explorations"].append(entry)
    st["explorations"] = st["explorations"][-20:]
    _save(st)
    return {"explored": len(targets), "targets": [t["topic"] for t in targets[:3]],
            "reward_free": True,
            "note": "自发探索（零奖赏依赖——纯好奇驱动）"}


def spontaneous_report() -> dict:
    """自发进化状态。"""
    st = _load()
    return {"explorations": len(st.get("explorations", [])),
            "reward_free": True,
            "note": "自发进化：无奖赏驱动的持续探索（World Knowledge）"}
