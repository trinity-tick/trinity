# -*- coding: utf-8 -*-
"""trinity/brain/unknown_awareness.py — 未知感知（EXECUTION 206，大脑化）。

借鉴 MUSE（Metacognition for Unknown Situations，Neural Networks）：
元认知的核心能力是"识别未知"并选择策略。Trinity 现在：
  detect_unknown: 检索后检测未知（无结果/低置信/覆盖缺口）
  strategy: 未知 → 探索（好奇心驱动）/ 标记（"我不确定"）

闭环：知道"我不知道" → 决定怎么办（探索/记录/承认）。
"""
import os
import sys
import json


def detect_unknown(query: str, results: list, top_confidence: float = 0.0) -> dict:
    """检测未知：结果不足/置信低/覆盖缺口。"""
    n = len(results)
    unknown = False
    reasons = []
    if n == 0:
        unknown = True
        reasons.append("no_results")
    elif n < 3:
        unknown = True
        reasons.append("insufficient_results")
    if top_confidence and top_confidence < 0.3:
        unknown = True
        reasons.append("low_confidence")
    return {"unknown": unknown, "reasons": reasons, "retrieved": n,
            "confidence": round(top_confidence, 3)}


def unknown_strategy(query: str, detection: dict, max_searches: int = 1) -> dict:
    """未知策略：探索（好奇心驱动搜索）或标记（写入未知记忆）。"""
    if not detection.get("unknown"):
        return {"action": "none", "note": "known territory"}
    strategy = {"action": "explore", "reasons": detection.get("reasons", [])}
    # 探索：触发网络搜索（max_searches=0 时跳过——测试/轻量模式）
    strategy["searched"] = False
    if max_searches > 0:
        try:
            sys.path.insert(0, r"D:\\trinity-code")
            import runpy
            _old = sys.argv
            sys.argv = ["web_search", "--query=" + str(query)[:40], "--max=5"]
            runpy.run_path(r"D:\\trinity-code\\scripts\\web_search.py", run_name="__main__")
            sys.argv = _old
            strategy["searched"] = True
        except Exception:
            strategy["searched"] = False
    # 标记未知（写入 unknown 记忆——"我不确定这个"）
    try:
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        m.ingest(f"[unknown] 我对『{str(query)[:40]}』的检索不充分"
                 f"（{','.join(detection.get('reasons', []))}）——已探索",
                 category="unknown-gap", tags=["unknown", "metacognition"],
                 importance=0.6, wait_backfill=True)
        strategy["marked"] = True
    except Exception:
        strategy["marked"] = False
    return strategy


def unknown_report() -> dict:
    """未知缺口汇总（元认知报告）。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE category='unknown-gap'")
        gaps = cur.fetchone()[0]
        conn.close()
        return {"unknown_gaps": gaps, "note": "已识别的未知缺口数"}
    except Exception as e:
        return {"error": str(e)[:80]}
