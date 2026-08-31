# -*- coding: utf-8 -*-
"""trinity/brain/autotelic_agency.py — 自主动机（EXECUTION 354）。

借鉴 Tao of Agency（2026：Autotelic AI——Embedded Agency）——
自主动机：自我生成目标（不以外部任务为限——自我决定——
内在目的论）。

与主动发起（内部驱动行动）互补：主动=发起行动；本模块=生成目标。
Trinity 现在：
  self_generate_goal(): 自主动机（自我目标生成）
"""
import os
import sys
import json


def self_generate_goal() -> dict:
    """自主动机：自我生成目标（基于自我状态）。"""
    # 自我状态分析（关注/缺口）
    goals = []
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT content FROM memories WHERE category='self-identity' LIMIT 1")
        r = cur.fetchone()
        identity = str(r[0]) if r else ""
        conn.close()
        if "数据库" in identity or "查询" in identity:
            goals.append({"goal": "深化数据库优化能力", "source": "自我关注"})
        if "系统崩溃" in identity or "稳定" in identity:
            goals.append({"goal": "强化系统稳定性实践", "source": "自我关注"})
    except Exception:
        pass
    if not goals:
        goals.append({"goal": "探索未知知识领域", "source": "自主好奇"})
        goals.append({"goal": "优化自我认知模型", "source": "自我反思"})
    return {"goals": goals[:3], "autotelic": True,
            "note": f"自主动机：自我生成 {len(goals[:3])} 个目标（不以外部任务为限——Autotelic）"}


def autotelic_report() -> dict:
    """自主动机状态。"""
    return {"note": "自主动机：内在目的论（Tao of Agency——自我决定）"}
