# -*- coding: utf-8 -*-
"""trinity/brain/metamemory.py — 元记忆（EXECUTION 216，大脑化）。

元记忆（metamemory）：知道自己记得什么/不记得什么——
  feeling-of-knowing（检索前预测"我知道吗"）+ 校准（预测准确性跟踪）。

大脑对应：前额叶的记忆监控（TIP-of-the-tongue 现象——知道但说不出，
说明"元记忆"与"记忆"分离）。Trinity 现在：
  feeling_of_knowing(query): 检索前预测（基于记忆覆盖）
  retrieval_check: 预测 vs 实际 → 校准（元记忆准确性）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/metamemory_state.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"calibration": {"predicted": 0, "correct": 0, "history": []}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def feeling_of_knowing(query: str) -> dict:
    """检索前预测：基于记忆覆盖评估"我知道吗"。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        # 覆盖评估（该主题记忆量）
        import re
        # 2-字滑动窗口（中文词提取——避免整句当一词）
        _txt = str(query)[:40]
        words = set()
        for i in range(len(_txt) - 1):
            if "\u4e00" <= _txt[i] <= "\u9fff" and "\u4e00" <= _txt[i+1] <= "\u9fff":
                words.add(_txt[i:i+2])
        cover = 0
        for w in list(words)[:4]:
            cur.execute("SELECT count(*) FROM memories WHERE content ILIKE %s AND status='active'", (f"%{w}%",))
            cover += cur.fetchone()[0]
        conn.close()
        # 已知感觉（0-1）：覆盖度归一化
        fok = min(1.0, cover / 10.0)
        return {"fok": round(fok, 2), "coverage": cover,
                "knows": fok >= 0.5,
                "feeling": "我知道这个" if fok >= 0.5 else "我不太确定"}
    except Exception as e:
        return {"error": str(e)[:80]}


def retrieval_check(query: str, results: list) -> dict:
    """检索后确认：预测 vs 实际 → 校准。"""
    pred = feeling_of_knowing(query)
    actual = len(results) >= 3
    st = _load()
    # 校准跟踪
    cal = st.get("calibration", {"predicted": 0, "correct": 0, "history": []})
    if "fok" in pred:
        cal["predicted"] += 1
        # 预测正确 = （预测知道且实际有结果）或（预测不知道且实际无）
        predicted_knows = pred.get("fok", 0) >= 0.5
        correct = (predicted_knows == actual)
        if correct:
            cal["correct"] += 1
        cal["history"].append({"query": str(query)[:30], "fok": pred.get("fok"),
                               "actual": actual, "correct": correct})
        cal["history"] = cal["history"][-50:]
    st["calibration"] = cal
    _save(st)
    accuracy = round(cal["correct"] * 100 / max(cal["predicted"], 1), 1)
    return {"prediction": pred.get("feeling", "?"), "actual_hits": len(results),
            "prediction_correct": (pred.get("fok", 0) >= 0.5) == actual,
            "calibration_accuracy": accuracy,
            "calibrated": cal["predicted"] >= 5}
