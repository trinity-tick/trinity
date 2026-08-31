# -*- coding: utf-8 -*-
"""trinity/brain/multifactor_value.py — 多因素价值（EXECUTION 285，大脑化）。

借鉴 Multi-Factor Value Model（2026：Learning What to Remember）——
"学什么该记住"的多因素评估（情绪显著性×新颖性×频率×关联性）。

与价值编码（基础）互补：编码=单因素；本模块=多因素综合。
Trinity 现在：
  value_score(factors): 多因素综合价值（情绪/新颖/频率/关联加权）
"""
import os
import sys
import json


# 因素权重（认知依据：情绪 0.3 新颖 0.25 频率 0.2 关联 0.25）
FACTOR_WEIGHTS = {"emotional": 0.3, "novelty": 0.25, "frequency": 0.2, "associative": 0.25}


def value_score(factors: dict) -> dict:
    """多因素价值评分：综合权重 → 该记住/可忘。"""
    total = 0.0
    details = {}
    for factor, weight in FACTOR_WEIGHTS.items():
        value = float(factors.get(factor, 0.5))
        total += value * weight
        details[factor] = round(value * weight, 3)
    score = round(total, 2)
    return {"score": score, "details": details,
            "verdict": "该记住" if score >= 0.6 else ("可记住" if score >= 0.4 else "可遗忘"),
            "note": f"多因素价值 {score}（情绪{factors.get('emotional',0.5)}×新颖{factors.get('novelty',0.5)}×频率{factors.get('frequency',0.5)}×关联{factors.get('associative',0.5)}）"}


def evaluate_memory(content: str) -> dict:
    """记忆评估：自动提取因素评分。"""
    factors = {}
    # 情绪（affect 评估）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.affect import assess
        r = assess(str(content)[:200])
        val = float(r.get("valence", 0))
        aro = float(r.get("arousal", 0))
        factors["emotional"] = min(1.0, abs(val) * 0.7 + aro * 0.3)
    except Exception:
        factors["emotional"] = 0.3
    # 新颖（surprise）
    try:
        from trinity.brain.surprise_encoding import surprise_boost
        sb = surprise_boost(content)
        factors["novelty"] = sb.get("novelty", 0.5)
    except Exception:
        factors["novelty"] = 0.5
    # 频率（相关词出现次数）
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        t = str(content)[:20]
        words = set()
        for i in range(len(t) - 1):
            if "\u4e00" <= t[i] <= "\u9fff" and "\u4e00" <= t[i+1] <= "\u9fff":
                words.add(t[i:i+2])
        total = 0
        for w in list(words)[:4]:
            cur.execute("SELECT count(*) FROM memories WHERE content ILIKE %s", (f"%{w}%",))
            total += cur.fetchone()[0]
        conn.close()
        factors["frequency"] = min(1.0, total / 40.0)
    except Exception:
        factors["frequency"] = 0.5
    # 关联（类别关联度）
    factors["associative"] = 0.6
    return value_score(factors)
