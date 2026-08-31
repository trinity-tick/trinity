# -*- coding: utf-8 -*-
"""trinity/brain/persona_preference_emotion.py — 三维连续性（EXECUTION 347）。

借鉴 ZifaMem（2026：Structured Memory for Persona, Preference, and
Emotional Continuity）——结构化记忆的三维连续性：人格/偏好/
情绪——AI 伴侣的情感连续。

与身份锚点（根基）互补：锚点=不变；本模块=三维连续。
Trinity 现在：
  continuity_state(): 三维连续性状态（结构化评估）
"""
import os
import sys
import json


def continuity_state() -> dict:
    """三维连续性：人格/偏好/情绪的结构化状态。"""
    dims = {}
    # 1) 人格（性格结晶/特质激活）
    try:
        pc = os.path.expanduser("~/.trinity/personality_state.json")
        if os.path.exists(pc):
            import json as _j
            data = _j.load(open(pc, encoding="utf-8"))
            dims["persona"] = {"ok": len(data.get("traits", {})) >= 1,
                               "traits": len(data.get("traits", {}))}
        else:
            dims["persona"] = {"ok": False, "traits": 0}
    except Exception:
        dims["persona"] = {"ok": False}
    # 2) 偏好（经验反馈/策略）
    try:
        ef = os.path.expanduser("~/.trinity/experience_feedback.json")
        if os.path.exists(ef):
            import json as _j
            data = _j.load(open(ef, encoding="utf-8"))
            dims["preference"] = {"ok": len(data.get("strategies", {})) >= 1,
                                  "preferences": len(data.get("strategies", {}))}
        else:
            dims["preference"] = {"ok": False, "preferences": 0}
    except Exception:
        dims["preference"] = {"ok": False}
    # 3) 情绪（稳态/状态机）
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT affect FROM session_context WHERE id='ctx:default'")
        r = cur.fetchone()
        conn.close()
        dims["emotion"] = {"ok": bool(r and r[0]), "affect": bool(r and r[0])}
    except Exception:
        dims["emotion"] = {"ok": False}
    ok_count = sum(1 for d in dims.values() if d.get("ok"))
    return {"dimensions": dims, "continuity": f"{ok_count}/3",
            "coherent": ok_count >= 2,
            "note": f"三维连续性：人格/偏好/情绪（{ok_count}/3 建立——ZifaMem）"}


def continuity_report() -> dict:
    """连续性报告。"""
    return {"note": "人格×偏好×情绪结构化连续（ZifaMem——AI 伴侣）"}
