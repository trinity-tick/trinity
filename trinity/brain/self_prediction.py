# -*- coding: utf-8 -*-
"""trinity/brain/self_prediction.py — 自我预测（EXECUTION 199，大脑化）。

借鉴 Active Inference "The Game of Self"（Hirsh 2026）：身份不是
静态标签，而是"预测模型"——持续预测自己下一步的状态（关注/情绪），
预测误差驱动自我更新（"我变了"）。

实现：
  predict_self(): 从历史自省/身份序列预测下一步关注主题与情绪趋势
  实际 vs 预测 → 自我预测误差 → 身份演进信号
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/self_prediction.json")


def _history() -> dict:
    """自我历史：自省记忆中的关注/情绪序列。"""
    hist = {"focus": [], "moods": []}
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT content FROM memories WHERE category='self-reflection' ORDER BY created_at DESC LIMIT 10")
        for (content,) in cur.fetchall():
            t = str(content or "")
            if "我在关注" in t:
                hist["focus"].append(t.split("我在关注：")[-1][:20])
            if "我的状态" in t:
                if "谨慎" in t:
                    hist["moods"].append("cautious")
                elif "积极" in t:
                    hist["moods"].append("positive")
                else:
                    hist["moods"].append("neutral")
        conn.close()
    except Exception:
        pass
    return hist


def predict_self() -> dict:
    """预测自我：下一关注主题 + 情绪趋势（EMA）。"""
    hist = _history()
    st = {"focus_ema": {}, "mood_count": {}}
    try:
        if os.path.exists(STATE_FILE):
            st = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        pass

    # 关注预测（当前焦点 = 最近关注；趋势 = 出现频率）
    focus_pred = hist["focus"][0] if hist["focus"] else "无"
    # 情绪趋势（EMA 平滑）
    for m in hist["moods"]:
        st["mood_count"][m] = st["mood_count"].get(m, 0) + 1
    if hist["moods"]:
        latest = hist["moods"][0]
        st["focus_ema"][latest] = st["focus_ema"].get(latest, 0) * 0.7 + 1 * 0.3
    dominant = max(st["mood_count"], key=st["mood_count"].get) if st["mood_count"] else "neutral"

    prediction = {
        "predicted_focus": focus_pred,
        "predicted_mood": dominant,
        "history_size": len(hist["focus"]),
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    return prediction


def self_prediction_error(actual_focus: str = "") -> dict:
    """自我预测误差：预测 vs 实际 → 身份演进信号。"""
    try:
        pred = predict_self()
        if not actual_focus:
            return {"prediction": pred, "error": None, "note": "无实际对照"}
        err = 0 if pred["predicted_focus"] == actual_focus else 1
        return {"prediction": pred, "error": err,
                "shifted": bool(err),  # 关注转移 = 自我演进
                "note": "关注转移（自我演进）" if err else "关注稳定"}
    except Exception as e:
        return {"error": str(e)[:80]}
