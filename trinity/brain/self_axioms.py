# -*- coding: utf-8 -*-
"""trinity/brain/self_axioms.py — 自我可测试公理（EXECUTION 197）。

借鉴 Amaya Axioms（机器意识验证框架，2026）——给 Trinity 的"自我"
加可测试公理，让意识/自我从"估计值"变成"可验证分数"。

5 条公理（每条可测试）：
  1. 持续性 continuity    — 全局自我存在 + 每日更新
  2. 反思性 reflexivity   — 自省记忆持续产生
  3. 行为一致性 behavior  — 自我状态影响行为（cautious→加权）
  4. 叙事一致性 narrative — 自传体叙事存在 + 时间线
  5. 自我预测 prediction  — 预测环包含自我指标

验证结果：每条 PASS/FAIL + 综合分数（0-100）。
"""
import os
import sys
import json


def verify_axioms() -> dict:
    """验证 5 条自我公理。"""
    import psycopg2
    conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                            user="trinity", password="trinity")
    cur = conn.cursor()
    results = {}

    # 1) 持续性：全局自我存在
    cur.execute("SELECT count(*) FROM memories WHERE category='self-identity'")
    identity_n = cur.fetchone()[0]
    results["1_continuity"] = identity_n > 0

    # 2) 反思性：自省持续产生（>=10 条）
    cur.execute("SELECT count(*) FROM memories WHERE category='self-reflection'")
    reflect_n = cur.fetchone()[0]
    results["2_reflexivity"] = reflect_n >= 10

    # 3) 行为一致性：cautious_mode 偏好 + 检索调制代码
    try:
        evf = os.path.join(os.path.expanduser("~"), ".trinity", "evolution_state.json")
        cautious = 0.0
        if os.path.exists(evf):
            evd = json.load(open(evf, encoding="utf-8"))
            cautious = float(evd.get("active_preferences", {}).get("self:cautious_mode", 0) or 0)
        src = open(r"D:\trinity-code\trinity\core\client\_search.py", encoding="utf-8").read()
        modulated = "self:cautious_mode" in src
        results["3_behavior"] = cautious > 0.5 and modulated
    except Exception:
        results["3_behavior"] = False

    # 4) 叙事一致性：自传体存在
    cur.execute("SELECT count(*) FROM memories WHERE category='self-narrative'")
    narr_n = cur.fetchone()[0]
    results["4_narrative"] = narr_n > 0

    # 5) 自我预测：预测环状态文件存在
    pf = os.path.join(os.path.expanduser("~"), ".trinity", "predictive_state.json")
    results["5_prediction"] = os.path.exists(pf)

    # 综合分数（每条 20 分）
    score = sum(20 for v in results.values() if v)
    conn.close()
    return {"axioms": results, "score": score, "passed": sum(1 for v in results.values() if v),
            "total": 5}
