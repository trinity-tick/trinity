# -*- coding: utf-8 -*-
"""trinity/brain/resource_adaptation.py — 资源自适应（EXECUTION 210，大脑化）。

借鉴 SAA（Synthetic Agency Architecture，2026）：统一意图/元认知/
资源自适应。大脑对应：资源紧张时降级认知（压力下简化处理）。

Trinity 现在：
  assess_resources(): 记忆量/性能/预算/健康 → 资源状态
  adapt_strategy(): 资源状态 → 自适应策略（饱和→遗忘增强；
                     慢→简化检索；紧张→降级处理）
"""
import os
import sys
import json
import time


def assess_resources() -> dict:
    """资源评估：记忆量/性能/预算/健康。"""
    res = {}
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE status='active'")
        res["active_memories"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories WHERE embedding IS NULL AND status='active'")
        res["missing_vectors"] = cur.fetchone()[0]
        conn.close()
    except Exception:
        pass
    # 性能（检索耗时近似——用最近一次 API 探测）
    try:
        t0 = time.time()
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8001/health", timeout=10) as resp:
            resp.read()
        res["health_ms"] = round((time.time() - t0) * 1000)
    except Exception:
        res["health_ms"] = -1
    # 预算（环境变量）
    res["token_budget"] = os.environ.get("TRINITY_TOKEN_BUDGET", "unset")
    return res


def adapt_strategy(resources: dict) -> dict:
    """资源状态 → 自适应策略。"""
    strategies = []
    active = resources.get("active_memories", 0)
    missing = resources.get("missing_vectors", 0)
    health_ms = resources.get("health_ms", -1)

    # 记忆饱和 → 增强遗忘/压缩
    if active > 20000:
        strategies.append({"area": "forgetting", "action": "strengthen",
                           "reason": f"active {active} 记忆饱和"})
    elif active > 10000:
        strategies.append({"area": "forgetting", "action": "normal",
                           "reason": "记忆量中等"})
    else:
        strategies.append({"area": "forgetting", "action": "relax",
                           "reason": "记忆量充足"})
    # 完整性缺失 → 自愈优先
    if missing > 20:
        strategies.append({"area": "self_heal", "action": "urgent",
                           "reason": f"{missing} 向量缺失"})
    # 性能慢 → 简化检索
    if health_ms > 2000:
        strategies.append({"area": "retrieval", "action": "simplify",
                           "reason": f"health {health_ms}ms 慢"})
    else:
        strategies.append({"area": "retrieval", "action": "normal",
                           "reason": f"health {health_ms}ms 正常"})
    return {"strategies": strategies, "resources": resources}


def adaptation_report() -> dict:
    """自适应报告（当前策略状态）。"""
    r = assess_resources()
    a = adapt_strategy(r)
    return {"strategy_count": len(a["strategies"]),
            "actions": [s["action"] for s in a["strategies"]]}
