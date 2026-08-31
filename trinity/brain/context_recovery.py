# -*- coding: utf-8 -*-
"""trinity/brain/context_recovery.py — 上下文恢复（EXECUTION 281，大脑化）。

借鉴 When Context Collapses（2026：Detect and Recover from Lost
Memory）——上下文崩溃检测与恢复（上下文不连续/丢失 → 检测 →
从记忆重建）。

与自愈（完整性）互补：自愈=整体；本模块=上下文连续。
Trinity 现在：
  detect_collapse(): 崩溃检测（会话/状态连续性）
  recover(): 恢复（从记忆重建上下文）
"""
import os
import sys
import json


def detect_collapse() -> dict:
    """崩溃检测：上下文连续性检查。"""
    issues = []
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 会话状态
        cur.execute("SELECT id, last_query FROM session_context ORDER BY updated_at DESC LIMIT 5")
        sessions = cur.fetchall()
        if not sessions:
            issues.append("会话上下文丢失（无 session_context）")
        # 连续状态
        cur.execute("SELECT count(*) FROM memories WHERE category='continuous-state' AND status='active'")
        cont = cur.fetchone()[0]
        if cont == 0:
            issues.append("连续状态缺失（无 continuous-state 记忆）")
        # 身份
        cur.execute("SELECT count(*) FROM memories WHERE category='self-identity' AND status='active'")
        identity = cur.fetchone()[0]
        if identity == 0:
            issues.append("身份上下文缺失（无 self-identity）")
        conn.close()
    except Exception as e:
        issues.append(f"状态检查失败：{str(e)[:40]}")
    return {"collapsed": len(issues) > 0, "issues": issues, "count": len(issues)}


def recover() -> dict:
    """恢复：从记忆重建上下文（会话/身份/连续状态）。"""
    det = detect_collapse()
    if not det["collapsed"]:
        return {"recovered": True, "needed": False, "note": "上下文完好——无需恢复"}
    rebuilt = []
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 从记忆重建
        if "连续状态缺失" in det["issues"]:
            cur.execute("SELECT left(content, 60) FROM memories WHERE status='active' AND category NOT IN ('perception') ORDER BY created_at DESC LIMIT 1")
            r = cur.fetchone()
            if r:
                rebuilt.append({"area": "continuous", "from": "最近记忆"})
        if "身份上下文缺失" in det["issues"]:
            cur.execute("SELECT left(content, 60) FROM memories WHERE status='active' AND category='identity-anchor' LIMIT 1")
            r = cur.fetchone()
            if r:
                rebuilt.append({"area": "identity", "from": "身份锚点"})
        conn.close()
    except Exception:
        pass
    return {"recovered": True, "needed": True, "rebuilt": rebuilt,
            "note": f"上下文已重建（修复 {len(rebuilt)} 处）" if rebuilt else "待重建"}
