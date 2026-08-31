# -*- coding: utf-8 -*-
"""trinity/brain/novelty_gate.py — 新颖门（EXECUTION 307，大脑化）。

借鉴 SAGE（2026：Novelty Gate for Efficient Memory Evolution）——
写入前的新颖性路由：新事实 → ADD；冗余 → SKIP；模糊 → LLM
判定（3.4× 便宜 2.5× 快——只写值得写的）。

与写入门控（质量）互补：门控=长度/价值；本模块=新颖路由。
Trinity 现在：
  gate_fact(fact): 新颖门（ADD/SKIP/ambiguous 三路）
"""
import os
import sys
import json


def _coverage(fact: str) -> float:
    """事实覆盖度（与现有记忆的重叠——0 全新 1 完全重复）。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        t = str(fact)[:50]
        words = set()
        for i in range(len(t) - 1):
            if "\u4e00" <= t[i] <= "\u9fff" and "\u4e00" <= t[i+1] <= "\u9fff":
                words.add(t[i:i+2])
        if not words:
            return 0.0
        hits = 0
        for w in list(words)[:5]:
            cur.execute("SELECT count(*) FROM memories WHERE content ILIKE %s AND status='active'", (f"%{w}%",))
            if cur.fetchone()[0] > 0:
                hits += 1
        conn.close()
        return hits / max(len(words), 1)
    except Exception:
        return 0.0


def gate_fact(fact: str) -> dict:
    """新颖门：覆盖度 → ADD/SKIP/ambiguous。"""
    coverage = _coverage(fact)
    if coverage < 0.3:
        return {"route": "ADD", "coverage": round(coverage, 2),
                "note": "新颖事实——写入（SAGE：ADD）"}
    if coverage >= 0.7:
        return {"route": "SKIP", "coverage": round(coverage, 2),
                "note": "冗余事实——跳过（省写入成本）"}
    return {"route": "ambiguous", "coverage": round(coverage, 2),
            "note": "模糊——需 LLM 判定（罕见情况）"}


def gate_batch(facts: list) -> dict:
    """批量新颖门。"""
    routes = {"ADD": 0, "SKIP": 0, "ambiguous": 0}
    for f in facts[:20]:
        r = gate_fact(str(f.get("content") or ""))
        routes[r["route"]] += 1
    return {"routes": routes, "saved": routes["SKIP"],
            "note": f"新颖门批量：ADD {routes['ADD']}/SKIP {routes['SKIP']}（省 {routes['SKIP']} 次写入）"}
