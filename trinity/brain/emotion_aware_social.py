# -*- coding: utf-8 -*-
"""trinity/brain/emotion_aware_social.py — 情绪感知（EXECUTION 376）。

借鉴 Sentipolis（2026：Emotion-Aware Agents for Social Simulations）——
情绪感知的社会模拟：追踪他人情绪状态（社会互动考虑情绪）。

与社会世界模型（反应预测）互补：预测=反应；本模块=情绪感知。
Trinity 现在：
  sense_emotion(agent_id): 情绪感知（他人情绪追踪）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/emotion_aware_social.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"agents": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def sense_emotion(agent_id: str, signal: str = "") -> dict:
    """情绪感知：从信号推断他人情绪（追踪更新）。"""
    st = _load()
    state = st["agents"].get(agent_id, {"valence": 0.0, "arousal": 0.3, "reads": 0})
    # 信号推断（从交互信号更新情绪）
    if any(w in str(signal) for w in ("开心", "满意", "感谢", "积极")):
        state["valence"] = min(1.0, state["valence"] + 0.3)
    elif any(w in str(signal) for w in ("生气", "不满", "愤怒", "消极")):
        state["valence"] = max(-1.0, state["valence"] - 0.3)
    elif any(w in str(signal) for w in ("兴奋", "紧急", "激动")):
        state["arousal"] = min(1.0, state["arousal"] + 0.2)
    state["reads"] += 1
    st["agents"][agent_id] = state
    _save(st)
    mood = "积极" if state["valence"] > 0.3 else ("消极" if state["valence"] < -0.3 else "中性")
    return {"agent": str(agent_id)[:15], "valence": round(state["valence"], 2),
            "arousal": round(state["arousal"], 2), "mood": mood,
            "reads": state["reads"],
            "note": f"情绪感知：{agent_id} 情绪 {mood}（valence {round(state['valence'],2)}——Sentipolis）"}


def social_emotion_report() -> dict:
    """情绪感知状态。"""
    st = _load()
    agents = st.get("agents", {})
    return {"agents_tracked": len(agents),
            "note": "情绪感知社会模拟：他人情绪追踪（Sentipolis）"}
