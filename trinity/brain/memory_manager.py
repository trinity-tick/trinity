# -*- coding: utf-8 -*-
"""trinity/brain/memory_manager.py — 记忆管理器（EXECUTION 205，大脑化）。

借鉴 Agentic Memory（ACL 2026 long）：统一长短期记忆管理。
大脑对应：工作记忆（短期）→ 海马巩固 → 长期皮层（升级）。

策略：
  1. promote：工作记忆（session_context.wm）高重要性项 → 升级长期记忆
  2. stabilize：长期记忆高频访问 → 提升重要性（巩固）
  3. 管理报告：短期/长期比例 + 升级统计
"""
import os
import sys
import json


def promote_working_memory(min_importance: float = 0.7, max_promote: int = 5) -> dict:
    """工作记忆 → 长期升级（重要项转长期记忆）。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        # 收集工作记忆项（session_context.wm JSONB）
        cur.execute("SELECT id, wm FROM session_context WHERE wm IS NOT NULL")
        promoted = 0
        for sid, wm in cur.fetchall():
            if not wm:
                continue
            if isinstance(wm, str):
                wm = json.loads(wm)
            for item in wm[:max_promote]:
                try:
                    imp = float(item.get("importance") or 0)
                    content = str(item.get("content") or "").strip()
                except Exception:
                    continue
                if imp >= min_importance and len(content) >= 10:
                    # 检查是否已存在（避免重复升级）
                    cur.execute("SELECT count(*) FROM memories WHERE content=%s AND category='promoted'", (content,))
                    if cur.fetchone()[0] == 0:
                        cur.execute("""
                            INSERT INTO memories (memory_id, content, category, status, importance, agent_id)
                            VALUES (gen_random_uuid()::text, %s, 'promoted', 'active', %s, 'memory-manager')
                        """, (content[:300], imp))
                        promoted += 1
        conn.commit()
        conn.close()
        return {"promoted": promoted, "min_importance": min_importance}
    except Exception as e:
        return {"error": str(e)[:80]}


def stabilize(top_n: int = 10) -> dict:
    """长期巩固：访问最多的记忆提升重要性（使用→巩固）。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("""
            UPDATE memories SET importance = LEAST(importance + 0.01, 0.95)
            WHERE memory_id IN (
                SELECT memory_id FROM memories
                WHERE status='active' AND access_count > 0
                ORDER BY access_count DESC LIMIT %s
            )
        """, (top_n,))
        conn.commit()
        conn.close()
        return {"stabilized": top_n}
    except Exception as e:
        return {"error": str(e)[:80]}


def memory_report() -> dict:
    """记忆管理报告（短期/长期比例）。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE category='promoted'")
        promoted = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM session_context WHERE wm IS NOT NULL")
        wm_sessions = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories WHERE status='active'")
        total = cur.fetchone()[0]
        conn.close()
        return {"promoted_to_long": promoted, "wm_sessions": wm_sessions,
                "active_total": total,
                "short_to_long_ratio": round(promoted * 100 / max(total, 1), 2)}
    except Exception as e:
        return {"error": str(e)[:80]}
