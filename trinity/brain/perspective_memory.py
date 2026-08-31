# -*- coding: utf-8 -*-
"""trinity/brain/perspective_memory.py — 视角记忆（EXECUTION 330）。

借鉴 Perspective-Bounded Memory（2026：Staying In Character——
Book-Based Role-Playing）——视角受限记忆：按角色/视角边界
回忆（场景引导细节重建——角色一致性）。

与任务视图（MemPrism）互补：视图=任务组织；本模块=角色边界。
Trinity 现在：
  bounded_recall(character, query): 视角受限回忆（角色边界内）
"""
import os
import sys
import json


# 角色视角边界（回忆范围限定）
PERSPECTIVES = {
    "engineer": {"domains": ["数据库", "性能", "故障", "优化", "备份"], "bias": "技术视角"},
    "analyst": {"domains": ["数据", "趋势", "统计", "报告", "指标"], "bias": "分析视角"},
    "assistant": {"domains": ["任务", "安排", "协助", "对话", "服务"], "bias": "服务视角"},
    "self": {"domains": ["自我", "反思", "经历", "记忆", "成长"], "bias": "自我视角"},
}


def bounded_recall(character: str, query: str) -> dict:
    """视角受限回忆：角色边界内检索（角色一致性）。"""
    persp = PERSPECTIVES.get(character, PERSPECTIVES["self"])
    t = str(query)
    # 边界检查：查询是否在角色域内
    in_domain = any(d in t for d in persp["domains"])
    # 检索（域内记忆优先）
    hits = []
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        for d in persp["domains"][:3]:
            cur.execute("SELECT left(content, 50) FROM memories WHERE status='active' AND content LIKE %s ORDER BY created_at DESC LIMIT 1", (f"%{d}%",))
            r = cur.fetchone()
            if r:
                hits.append(r[0])
        conn.close()
    except Exception:
        pass
    return {"character": character, "perspective": persp["bias"],
            "in_domain": in_domain, "recalled": hits[:2],
            "bounded": True,
            "note": f"视角受限回忆：{persp['bias']}（{'域内' if in_domain else '域外——仅默认'}）"}


def perspective_report() -> dict:
    """视角体系状态。"""
    return {"perspectives": list(PERSPECTIVES.keys()),
            "note": "视角受限记忆：角色边界一致性（Perspective-Bounded）"}
