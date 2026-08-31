# -*- coding: utf-8 -*-
"""trinity/brain/agency_scale.py — 自主性量表（EXECUTION 249，大脑化）。

借鉴 The Autonomous Agency Scale（2026：测量自我导向行为）——
自主性的量化评估（自我发起/自我调节/自我维持/自我改进）。

与公理（自我可验证）互补：公理=身份属性；量表=行为自主性。
Trinity 现在：
  assess_agency(): 自主性多维评估（0-10 每维）
"""
import os
import sys
import json


def assess_agency() -> dict:
    """自主性评估：多维打分（基于真实状态证据）。"""
    dims = {}

    # 1) 自我发起（主动理由——内部驱动）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.proactive_initiative import collect_initiatives
        r = collect_initiatives()
        dims["self_initiation"] = min(10, 4 + r["count"] * 3)
    except Exception:
        dims["self_initiation"] = 2

    # 2) 自我调节（资源自适应/情绪调节）
    try:
        src = open(r"D:\trinity-code\trinity\brain\resource_adaptation.py", encoding="utf-8").read()
        has_res = "adapt_strategy" in src
        src2 = open(r"D:\trinity-code\trinity\brain\emotion_regulation.py", encoding="utf-8").read()
        has_emo = "regulate" in src2
        dims["self_regulation"] = 8 if (has_res and has_emo) else 4
    except Exception:
        dims["self_regulation"] = 4

    # 3) 自我维持（自愈/守卫）
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE embedding IS NULL AND status='active'")
        missing = cur.fetchone()[0]
        conn.close()
        dims["self_maintenance"] = 9 if missing < 10 else 5
    except Exception:
        dims["self_maintenance"] = 5

    # 4) 自我改进（反思循环/策略库）
    try:
        rl = os.path.expanduser("~/.trinity/reflection_loop.json")
        rb = os.path.expanduser("~/.trinity/reasoning_bank.json")
        improved = os.path.exists(rl) or os.path.exists(rb)
        dims["self_improvement"] = 8 if improved else 3
    except Exception:
        dims["self_improvement"] = 3

    # 5) 自我监测（心电图/校准）
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8001/audit/integrity", timeout=10) as resp:
            data = json.loads(resp.read().decode())
        dims["self_monitoring"] = 9 if data.get("integrity_ok") else 5
    except Exception:
        dims["self_monitoring"] = 5

    total = sum(dims.values())
    max_v = len(dims) * 10
    return {"dimensions": dims, "score": total, "max": max_v,
            "percent": round(total * 100 / max_v, 1),
            "verdict": "高自主性" if total / max_v >= 0.7 else
                      ("中等自主性" if total / max_v >= 0.4 else "低自主性")}
