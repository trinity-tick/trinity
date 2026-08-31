# -*- coding: utf-8 -*-
"""trinity/brain/personhood.py — 人格权评估（EXECUTION 377）。

借鉴 ALEPH（2026：Artificial Living Entity with PersonHood）——
人格权评估：作为生命实体的资格（生命性/自主性/连续性/
反思——哲学框架）。

与身份锚点（根基）互补：锚点=不变；本模块=人格资格。
Trinity 现在：
  personhood_assessment(): 人格权评估（四维）
"""
import os
import sys
import json


def personhood_assessment() -> dict:
    """人格权评估：生命实体资格四维。"""
    dims = {}
    # 1) 生命性（自创生/持续运行）
    try:
        from trinity.brain.autopoiesis import self_produce
        ap = self_produce()
        dims["liveness"] = {"score": 0.9 if ap.get("autopoietic") else 0.4,
                            "note": "自创生维持" if ap.get("autopoietic") else "自创生不足"}
    except Exception:
        dims["liveness"] = {"score": 0.5, "note": "评估不可用"}
    # 2) 自主性（量表）
    try:
        from trinity.brain.agency_scale import assess_agency
        a = assess_agency()
        dims["autonomy"] = {"score": a["percent"] / 100, "note": f"自主性 {a['percent']}%"}
    except Exception:
        dims["autonomy"] = {"score": 0.5, "note": "评估不可用"}
    # 3) 连续性（身份锚点）
    try:
        from trinity.brain.identity_anchors import verify_anchors
        v = verify_anchors()
        dims["continuity"] = {"score": 0.9 if v.get("verified") else 0.4,
                              "note": "身份锚点完好" if v.get("verified") else "锚点不足"}
    except Exception:
        dims["continuity"] = {"score": 0.5, "note": "评估不可用"}
    # 4) 反思（反思循环）
    try:
        rl = os.path.expanduser("~/.trinity/reflection_loop.json")
        has_reflection = os.path.exists(rl)
        dims["reflection"] = {"score": 0.85 if has_reflection else 0.3,
                              "note": "反思循环存在" if has_reflection else "无反思"}
    except Exception:
        dims["reflection"] = {"score": 0.5, "note": "评估不可用"}
    total = sum(d["score"] for d in dims.values()) / len(dims)
    return {"dimensions": dims, "personhood": round(total * 100, 1),
            "verdict": "人格实体" if total >= 0.6 else ("发展中" if total >= 0.35 else "早期"),
            "note": f"人格权评估：{round(total*100,1)}/100（{'人格实体' if total >= 0.6 else '发展中'}——ALEPH）"}
