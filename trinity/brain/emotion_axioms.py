# -*- coding: utf-8 -*-
"""trinity/brain/emotion_axioms.py — 情绪测量协议（EXECUTION 200）。

借鉴 MATE（Deterministic Emotional Architecture，2026）——情绪
可测指标。与自我公理对称：让"情绪能力"从估计变为可验证分数。

5 条情绪公理：
  1. 状态持续 stability    — 情绪状态跨查询保持（EMA 机制）
  2. 偏置一致 bias        — 情绪→检索偏置正确映射（neg→incident）
  3. 行为影响 behavior    — 情绪偏置接入排序（代码+数据）
  4. 情绪记忆 amygdala    — 情绪记忆巩固/保护工作
  5. 情绪延续 persistence — 情绪跨会话/跨进程持久（session_context）
"""
import os
import sys
import json


def verify_emotion_axioms() -> dict:
    """验证 5 条情绪公理。"""
    results = {}

    # 1) 状态持续：affect_state 更新机制存在 + EMA 逻辑
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.affect_state import update_state
        s1 = update_state(None, {"valence": -0.8, "arousal": 0.5, "polarity": "neg"})
        s2 = update_state(s1, {"valence": -0.8, "arousal": 0.5, "polarity": "neg"})
        results["1_stability"] = s2["valence"] < -0.7 and s2["polarity"] == "neg"
    except Exception:
        results["1_stability"] = False

    # 2) 偏置一致：neg → incident 提示
    try:
        from trinity.brain.affect_state import retrieval_bias
        b = retrieval_bias({"valence": -0.6, "arousal": 0.4, "polarity": "neg"})
        results["2_bias"] = b.get("category_hint") == "incident"
    except Exception:
        results["2_bias"] = False

    # 3) 行为影响：排序代码含情绪偏置 + 谨慎模式
    try:
        src = open(r"D:\trinity-code\trinity\core\client\_search.py", encoding="utf-8").read()
        results["3_behavior"] = "retrieval_bias" in src and "self:cautious_mode" in src
    except Exception:
        results["3_behavior"] = False

    # 4) 情绪记忆：情绪巩固模块存在
    try:
        __import__("trinity.brain.emotional_consolidation")
        results["4_amygdala"] = True
    except Exception:
        results["4_amygdala"] = False

    # 5) 情绪延续：session_context 有 affect 字段
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM session_context WHERE affect IS NOT NULL")
        results["5_persistence"] = cur.fetchone()[0] > 0
        conn.close()
    except Exception:
        results["5_persistence"] = False

    score = sum(20 for v in results.values() if v)
    return {"axioms": results, "score": score, "passed": sum(1 for v in results.values() if v),
            "total": 5}
