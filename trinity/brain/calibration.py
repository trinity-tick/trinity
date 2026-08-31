# -*- coding: utf-8 -*-
"""trinity/brain/calibration.py — 元认知校准（EXECUTION 242，大脑化）。

借鉴 MIRROR（2026：Hierarchical Benchmark for Metacognitive
Calibration）——校准是元认知核心质量：说"我知道 0.8"时真的对 80%？

Trinity 现在：
  record(prediction, actual): 记录预测与实际（置信 vs 结果）
  calibration_score(): 校准分数（置信-命中一致性——Brier 类）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/calibration_state.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"records": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def record(prediction: float, actual: bool, context: str = "") -> dict:
    """记录预测与实际（置信度 vs 结果）。"""
    st = _load()
    st["records"].append({"prediction": round(float(prediction), 2),
                          "actual": bool(actual),
                          "context": str(context)[:30],
                          "ts": __import__("time").time()})
    st["records"] = st["records"][-200:]
    _save(st)
    return {"recorded": True, "total": len(st["records"])}


def calibration_score() -> dict:
    """校准分数：置信与命中的一致性（0-1，越高越准）。"""
    st = _load()
    records = st.get("records", [])
    if len(records) < 5:
        return {"calibrated": False, "records": len(records),
                "note": "样本不足（需 >=5）"}
    # Brier 类误差：mean((p - actual)^2)
    brier = sum((r["prediction"] - (1.0 if r["actual"] else 0.0)) ** 2
                for r in records) / len(records)
    # 校准分数 = 1 - brier（0-1 越高越准）
    score = max(0.0, 1.0 - brier)
    # 置信-命中一致性
    conf_avg = sum(r["prediction"] for r in records) / len(records)
    hit_rate = sum(1 for r in records if r["actual"]) / len(records)
    return {"calibrated": True, "score": round(score, 3),
            "brier": round(brier, 3),
            "avg_confidence": round(conf_avg, 2),
            "hit_rate": round(hit_rate, 2),
            "records": len(records),
            "note": ("校准良好" if score >= 0.8 else ("可接受" if score >= 0.6 else "需改进"))}
