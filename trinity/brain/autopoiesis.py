# -*- coding: utf-8 -*-
"""trinity/brain/autopoiesis.py — 自创生（EXECUTION 310，大脑化）。

借鉴 Emergent Autopoietic Persistence（2026：Four-Layer
Architecture）——自创生：系统自我生产、自我维持存在
（不是被修复——而是自己维持自己的运转）。

与自愈（修复）互补：自愈=出问题修复；自创生=持续自我维持。
Trinity 现在：
  self_produce(): 自创生检查（自我维持各环节）
"""
import os
import sys
import json


def self_produce() -> dict:
    """自创生：自我维持检查（自我生产的循环是否运转）。"""
    cycles = {}
    # 1) 自我监测（ECG 循环）
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8001/audit/integrity", timeout=10) as resp:
            data = json.loads(resp.read().decode())
        cycles["monitoring"] = {"ok": data.get("integrity_ok", False), "note": "自我监测循环"}
    except Exception:
        cycles["monitoring"] = {"ok": False, "note": "监测不可用"}
    # 2) 自我维护（记忆健康——缺失向量）
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE embedding IS NULL AND status='active'")
        missing = cur.fetchone()[0]
        conn.close()
        cycles["maintenance"] = {"ok": missing < 10, "note": f"记忆健康（缺失向量 {missing}）"}
    except Exception:
        cycles["maintenance"] = {"ok": False, "note": "维护不可用"}
    # 3) 自我更新（反思/进化循环）
    try:
        rl = os.path.expanduser("~/.trinity/reflection_loop.json")
        cycles["updating"] = {"ok": os.path.exists(rl), "note": "反思循环存在"}
    except Exception:
        cycles["updating"] = {"ok": False, "note": "更新不可用"}
    # 4) 自我修复（自愈能力）
    try:
        src = open(r"D:\\trinity-code\\trinity\\brain\\context_recovery.py", encoding="utf-8").read()
        cycles["repair"] = {"ok": "recover" in src, "note": "上下文恢复能力"}
    except Exception:
        cycles["repair"] = {"ok": False, "note": "修复不可用"}
    ok_count = sum(1 for c in cycles.values() if c.get("ok"))
    return {"cycles": cycles, "autopoietic": ok_count >= 3,
            "score": f"{ok_count}/4",
            "note": f"自创生：自我维持循环 {ok_count}/4 运转"}


def autopoiesis_report() -> dict:
    """自创生状态。"""
    r = self_produce()
    return {"autopoietic": r.get("autopoietic"), "score": r.get("score"),
            "note": "自创生：自我生产维持存在（Four-Layer 2026）"}
