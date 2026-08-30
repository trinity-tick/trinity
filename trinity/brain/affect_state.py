# -*- coding: utf-8 -*-
"""trinity/brain/affect_state.py — 会话情绪状态机（2026-09，EXECUTION 149）

从"标记情绪"升级为"情绪状态"：每个会话维护情绪状态（valence/arousal
EMA 累积），情绪影响检索策略：
  - 高 arousal（紧迫）→ 检索偏向高价值记忆（决策相关）
  - 负 valence（消极）→ 检索偏向"教训/事故"类记忆（风险意识）

状态持久化于 session_context.affect（EMA 更新后写回）。
"""


def update_state(current, new_affect, alpha: float = 0.3):
    """EMA 更新会话情绪状态。

    current: 现有状态 {valence, arousal, polarity} 或 None
    new_affect: 新评估 {valence, arousal, polarity}
    """
    try:
        nv = float(new_affect.get("valence") or 0.0)
        na = float(new_affect.get("arousal") or 0.0)
        if current and current.get("valence") is not None:
            cv = float(current["valence"])
            ca = float(current.get("arousal") or 0.0)
            v = cv * (1 - alpha) + nv * alpha
            a = ca * (1 - alpha) + na * alpha
        else:
            v, a = nv, na
        v = max(-1.0, min(1.0, v))
        a = max(0.0, min(1.0, a))
        pol = "pos" if v > 0.15 else ("neg" if v < -0.15 else "neu")
        return {"valence": round(v, 2), "arousal": round(a, 2), "polarity": pol}
    except Exception:
        return current or {"valence": 0.0, "arousal": 0.0, "polarity": "neu"}


def retrieval_bias(state):
    """情绪 → 检索策略偏置。

    返回 dict: {"value_boost": float, "category_hint": str|None}
    """
    if not state:
        return {"value_boost": 0.0, "category_hint": None}
    try:
        v = float(state.get("valence") or 0.0)
        a = float(state.get("arousal") or 0.0)
        boost = 0.0
        hint = None
        if a >= 0.6:
            # 高唤醒 → 高价值记忆优先（决策/教训）
            boost = 0.15
        if v <= -0.3:
            # 消极 → 风险/教训记忆优先
            hint = "incident"
        return {"value_boost": boost, "category_hint": hint}
    except Exception:
        return {"value_boost": 0.0, "category_hint": None}
