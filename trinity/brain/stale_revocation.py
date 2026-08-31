# -*- coding: utf-8 -*-
"""trinity/brain/stale_revocation.py — 过期记忆撤销（EXECUTION 239，大脑化）。

借鉴 TEPA（2026：Revoking Stale Memories for Conflict-Robust Agents）——
新旧信息冲突时，撤销过期记忆（记忆一致性维护）。

与遗忘（修剪低价值）区分：遗忘=价值；撤销=冲突解决。
Trinity 现在：
  detect_conflict(new_content, old_content): 冲突检测（同主题矛盾）
  revoke(old_id): 撤销过期记忆（标记 revoked + 审计）
"""
import os
import sys
import json


def detect_conflict(new_content: str, old_content: str) -> dict:
    """冲突检测：同主题且内容矛盾（否定词/对立）。"""
    # 主题相似（共享词）
    import re
    def words(t):
        # 2 字滑动窗口（避免整句当一词）
        _t = str(t)[:100]
        _w = set()
        for i in range(len(_t) - 1):
            if "\u4e00" <= _t[i] <= "\u9fff" and "\u4e00" <= _t[i+1] <= "\u9fff":
                _w.add(_t[i:i+2])
        return _w
    nw, ow = words(new_content), words(old_content)
    shared = nw & ow
    if len(shared) < 2:
        return {"conflict": False, "reason": "不同主题"}
    # 矛盾检测（否定词/更新标记）
    neg_old = any(w in str(old_content) for w in ("不再", "废弃", "停止", "过时"))
    upd_new = any(w in str(new_content) for w in ("更新", "替代", "新方案", "改为", "最新"))
    conflict = neg_old or upd_new
    return {"conflict": conflict, "shared_topics": list(shared)[:3],
            "reason": "新信息更新旧认知" if conflict else "无直接矛盾"}


def revoke(memory_id: str, reason: str = "stale_conflict") -> dict:
    """撤销过期记忆：标记 revoked + 审计（可追溯）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT content FROM memories WHERE memory_id=%s", (memory_id,))
        row = cur.fetchone()
        if not row:
            return {"revoked": False, "error": "not found"}
        cur.execute("UPDATE memories SET status='revoked' WHERE memory_id=%s", (memory_id,))
        conn.commit()
        # 审计
        try:
            from trinity.adapters.postgresql import PostgreSQLAdapter
            a = PostgreSQLAdapter(auto_connect=True)
            a.connect()
            try:
                a.write_audit_log(memory_id=None, action="memory_revoked",
                                  agent_id="stale-revocation",
                                  details={"memory_id": memory_id, "reason": reason})
            finally:
                a.disconnect()
        except Exception:
            pass
        conn.close()
        return {"revoked": True, "memory_id": memory_id[:12], "reason": reason}
    except Exception as e:
        return {"revoked": False, "error": str(e)[:80]}


def revoke_report() -> dict:
    """撤销统计（记忆一致性维护）。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE status='revoked'")
        n = cur.fetchone()[0]
        conn.close()
        return {"revoked_memories": n, "note": "已撤销的过期记忆数"}
    except Exception as e:
        return {"error": str(e)[:80]}
