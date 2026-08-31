# -*- coding: utf-8 -*-
"""trinity/brain/scheduled_forgetting.py — 调度遗忘（EXECUTION 283，大脑化）。

借鉴 SleepGate（2026：Scheduled Forgetting — Interference Horizon）——
按干扰水平调度遗忘（O(log n) 干扰视野：只在干扰高时遗忘——
不随意遗忘也不全保留）。

与遗忘（值修剪）互补：遗忘=值；调度=时机。
Trinity 现在：
  interference_horizon(): 干扰视野（冲突/冗余水平）
  schedule_pass(): 调度遗忘（干扰高→执行遗忘）
"""
import os
import sys
import json
import time


def interference_horizon(limit: int = 30) -> dict:
    """干扰视野：冲突/冗余记忆水平。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 冗余（近似重复——同类别+低重要）
        cur.execute("""
            SELECT count(*) FROM memories
            WHERE status='active' AND importance < 0.3
              AND category NOT IN ('perception', 'dcpm-core')
        """)
        redundant = cur.fetchone()[0]
        # 冲突（revoked 计数——近期撤销量）
        cur.execute("SELECT count(*) FROM memories WHERE status='revoked'")
        revoked = cur.fetchone()[0]
        conn.close()
        interference = min(1.0, (redundant / max(limit, 1)) * 0.6 + (revoked / 50) * 0.4)
        return {"interference": round(interference, 2),
                "redundant_low_value": redundant, "revoked_total": revoked,
                "level": "high" if interference >= 0.6 else ("medium" if interference >= 0.3 else "low")}
    except Exception as e:
        return {"error": str(e)[:80]}


def schedule_pass() -> dict:
    """调度遗忘：干扰高 → 执行遗忘（低价值冗余）。"""
    h = interference_horizon()
    if h.get("level") == "high":
        try:
            import psycopg2
            conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                    user="trinity", password="trinity", connect_timeout=10)
            cur = conn.cursor()
            cur.execute("""
                UPDATE memories SET status='forgotten'
                WHERE status='active' AND importance < 0.3
                  AND category NOT IN ('perception', 'dcpm-core')
                  AND created_at::timestamp < NOW() - interval '7 days'
            """)
            forgotten = cur.rowcount
            conn.commit()
            conn.close()
            return {"forgot": forgotten, "trigger": "high_interference",
                    "note": f"干扰高——调度遗忘 {forgotten} 条冗余"}
        except Exception as e:
            return {"error": str(e)[:80]}
    return {"forgot": 0, "trigger": f"{h.get('level', 'low')}_interference",
            "note": "干扰水平不高——暂不遗忘"}
