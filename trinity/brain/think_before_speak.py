# -*- coding: utf-8 -*-
"""trinity/brain/think_before_speak.py — 先想后说（EXECUTION 319）。

借鉴 Think-Before-Speak（2026：From Internal Evaluation to Public
Expression）——表达前内部评估：情绪/准确/影响三维检查 →
表达/修正/暂缓。

与先感受后表达（情绪检查）互补：感受=情绪维度；本模块=完整评估。
Trinity 现在：
  internal_eval(response): 内部评估（三维→表达决策）
"""
import os
import sys
import json


def internal_eval(response: str) -> dict:
    """内部评估：情绪/准确/影响三维检查。"""
    checks = {}
    # 1) 情绪（先感受后表达）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.affect import assess
        r = assess(str(response)[:200])
        val = float(r.get("valence", 0))
        aro = float(r.get("arousal", 0.3))
        checks["emotion"] = {"ok": val >= -0.5, "valence": round(val, 2),
                             "note": "情绪稳定" if val >= -0.5 else "情绪强烈需缓和"}
    except Exception:
        checks["emotion"] = {"ok": True}
    # 2) 准确（无绝对化断言/未经验证）
    text = str(response)
    overclaims = [w for w in ("一定", "绝对", "必然", "100%") if w in text]
    checks["accuracy"] = {"ok": not overclaims, "overclaims": overclaims,
                          "note": "表述谨慎" if not overclaims else "有绝对化表述"}
    # 3) 影响（敏感/负面词语）
    sensitive = [w for w in ("攻击", "贬低", "歧视", "侮辱") if w in text]
    checks["impact"] = {"ok": not sensitive, "sensitive": sensitive,
                        "note": "表达安全" if not sensitive else "含敏感词"}
    # 综合决策
    ok_count = sum(1 for c in checks.values() if c.get("ok"))
    if ok_count == 3:
        decision = "express"
    elif ok_count == 2:
        decision = "revise"
    else:
        decision = "hold"
    return {"checks": checks, "decision": decision,
            "note": {"express": "评估通过——表达", "revise": "需修正——修改后表达",
                     "hold": "暂缓——不表达"}[decision]}
