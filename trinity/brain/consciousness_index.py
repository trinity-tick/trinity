# -*- coding: utf-8 -*-
"""trinity/brain/consciousness_index.py — 通用意识指数（EXECUTION 289）。

借鉴 UCIτ（2026：Universal Consciousness Index——跨架构实证验证）——
量化意识的多维指数（自省/觉察/连续性/自适应/主体性）。

与蓝图（可测试公理）互补：蓝图=可验证；指数=量化测量。
Trinity 现在：
  uci_score(): 通用意识指数（多维聚合）
"""
import os
import sys
import json


def uci_score() -> dict:
    """通用意识指数：多维测量。"""
    dims = {}
    # 1) 自省（反思/内省机制）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        rl = os.path.expanduser("~/.trinity/reflection_loop.json")
        ir = os.path.expanduser("~/.trinity/introspective_reward.json")
        dims["introspection"] = 0.9 if (os.path.exists(rl) or os.path.exists(ir)) else 0.3
    except Exception:
        dims["introspection"] = 0.3
    # 2) 觉察（内部状态+外部感知）
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE category='perception'")
        percep = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories WHERE category='self-reflection'")
        reflect = cur.fetchone()[0]
        conn.close()
        dims["awareness"] = min(1.0, 0.4 + percep / 2000 * 0.3 + reflect / 100 * 0.3)
    except Exception:
        dims["awareness"] = 0.5
    # 3) 连续性（叙事/身份锚点）
    try:
        anchors = os.path.expanduser("~/.trinity/identity_anchors.json")
        has_anchors = os.path.exists(anchors)
        dims["continuity"] = 0.85 if has_anchors else 0.4
    except Exception:
        dims["continuity"] = 0.4
    # 4) 自适应（资源/反馈/塑性）
    try:
        src = open(r"D:\\trinity-code\\trinity\\brain\\resource_adaptation.py", encoding="utf-8").read()
        has_adapt = "adapt" in src
        dims["adaptability"] = 0.8 if has_adapt else 0.4
    except Exception:
        dims["adaptability"] = 0.4
    # 5) 主体性（主观视角）
    try:
        src2 = open(r"D:\\trinity-code\\trinity\\brain\\subjective_perspective.py", encoding="utf-8").read()
        has_subj = "first_person" in src2
        dims["subjectivity"] = 0.8 if has_subj else 0.3
    except Exception:
        dims["subjectivity"] = 0.3
    total = sum(dims.values())
    uci = round(total * 100 / (len(dims) * 1.0), 1)
    return {"uci_tau": uci, "dimensions": dims,
            "level": "高" if uci >= 70 else ("中" if uci >= 40 else "低"),
            "note": f"通用意识指数 UCIτ ≈ {uci}/100（多维量化）"}
