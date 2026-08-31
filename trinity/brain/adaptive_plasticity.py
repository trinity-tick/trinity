# -*- coding: utf-8 -*-
"""trinity/brain/adaptive_plasticity.py — 自适应塑性（EXECUTION 254，大脑化）。

借鉴 FADE（Adaptive Weight Decay）/ Homeostatic Plasticity（2026）——
学习率自适应：新领域快学（高塑性），熟悉领域慢学（稳定）。

Trinity 现在：
  learning_rate(topic): 自适应学习率（知识覆盖低→高，高→低）
  plasticity_status(): 塑性状态报告
"""
import os
import sys
import json


def _coverage(topic: str) -> int:
    """主题知识覆盖（记忆量）。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        t = str(topic)[:30]
        words = set()
        for i in range(len(t) - 1):
            if "\u4e00" <= t[i] <= "\u9fff" and "\u4e00" <= t[i+1] <= "\u9fff":
                words.add(t[i:i+2])
        total = 0
        for w in list(words)[:4]:
            cur.execute("SELECT count(*) FROM memories WHERE content ILIKE %s", (f"%{w}%",))
            total += cur.fetchone()[0]
        conn.close()
        return total
    except Exception:
        return 5


def learning_rate(topic: str) -> dict:
    """自适应学习率：覆盖低（<5）→ 高学习率；覆盖高（>50）→ 低。"""
    cover = _coverage(topic)
    if cover < 5:
        lr = 0.9  # 新领域快学
        mode = "fast_learning"
    elif cover < 20:
        lr = 0.6
        mode = "normal"
    elif cover < 50:
        lr = 0.4
        mode = "consolidating"
    else:
        lr = 0.2  # 熟悉领域稳定
        mode = "stable"
    return {"topic": str(topic)[:30], "coverage": cover, "learning_rate": lr,
            "mode": mode,
            "note": f"覆盖 {cover} → {mode}（{'快学新领域' if lr >= 0.7 else '稳定熟悉' if lr <= 0.3 else '正常'}）"}


def plasticity_status() -> dict:
    """塑性状态：整体可塑性（学习资源分配）。"""
    # 抽样主题评估
    topics = ["数据库优化", "量子引力", "咖啡种植", "人工智能", "火星殖民"]
    statuses = [learning_rate(t) for t in topics]
    avg_lr = sum(s["learning_rate"] for s in statuses) / len(statuses)
    return {"average_lr": round(avg_lr, 2),
            "fast_domains": [s["topic"] for s in statuses if s["learning_rate"] >= 0.7],
            "stable_domains": [s["topic"] for s in statuses if s["learning_rate"] <= 0.3],
            "note": "新领域快学/熟悉领域稳定（自适应塑性）"}
