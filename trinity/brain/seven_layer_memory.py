# -*- coding: utf-8 -*-
"""trinity/brain/seven_layer_memory.py — 7 层记忆（EXECUTION 308，大脑化）。

借鉴 ZenBrain（2026：Neuroscience-Inspired 7-Layer Memory
Architecture）——神经科学 7 层记忆统一视图：
感官 → 工作 → 情景 → 语义 → 程序 → 自传 → 元记忆。

与分层检索（MEMTIER 分层）互补：分层=检索层；本模块=神经层视图。
Trinity 现在：
  layer_status(): 7 层状态（各层健康/量）
  layer_access(layer): 按层访问
"""
import os
import sys
import json


LAYERS = [
    ("sensory", "感官记忆", ["perception"]),
    ("working", "工作记忆", ["working_memory", "session_context"]),
    ("episodic", "情景记忆", ["self-narrative", "reconstructive", "episodic"]),
    ("semantic", "语义记忆", ["dcpm-schema", "promoted", "semantic"]),
    ("procedural", "程序记忆", ["habit", "action-experience", "skill"]),
    ("autobiographical", "自传记忆", ["self-identity", "identity-anchor"]),
    ("metamemory", "元记忆", ["metamemory", "self-reflection"]),
]


def layer_status() -> dict:
    """7 层状态：各层健康与内容量。"""
    status = {}
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        for key, name, cats in LAYERS:
            total = 0
            for c in cats:
                if c == "session_context":
                    cur.execute("SELECT count(*) FROM session_context")
                else:
                    cur.execute("SELECT count(*) FROM memories WHERE category=%s AND status='active'", (c,))
                total += cur.fetchone()[0]
            status[key] = {"name": name, "items": total,
                           "healthy": total >= 1 or key in ("working",)}
        conn.close()
    except Exception:
        pass
    healthy = sum(1 for v in status.values() if v.get("healthy"))
    return {"layers": status, "healthy": f"{healthy}/7",
            "note": "7 层记忆统一视图（ZenBrain 神经科学架构）"}


def layer_access(layer: str, top_k: int = 2) -> dict:
    """按层访问：取该层代表内容。"""
    layer_map = {k: (name, cats) for k, name, cats in LAYERS}
    if layer not in layer_map:
        return {"error": f"未知层（可用: {list(layer_map.keys())}）"}
    name, cats = layer_map[layer]
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        items = []
        for c in cats:
            if c == "session_context":
                cur.execute("SELECT last_query FROM session_context WHERE last_query IS NOT NULL LIMIT 1")
                r = cur.fetchone()
                if r:
                    items.append(str(r[0])[:40])
                continue
            cur.execute("SELECT left(content, 50) FROM memories WHERE category=%s AND status='active' ORDER BY created_at DESC LIMIT %s", (c, top_k))
            items.extend([r[0] for r in cur.fetchall()])
            if len(items) >= top_k:
                break
        conn.close()
        return {"layer": layer, "name": name, "items": items[:top_k],
                "count": len(items)}
    except Exception as e:
        return {"error": str(e)[:80]}
