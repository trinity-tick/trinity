# -*- coding: utf-8 -*-
"""trinity/brain/reflex_attention.py — 反射注意（EXECUTION 300，大脑化）。

借鉴 Reflex Attention（2026：Short-Horizon Deviation Flag for
Non-Persistent Anomaly Detection）——反射注意：无意识的自动偏差
检测（快速异常标志——不占认知资源）。

与主动注意（选择）互补：主动=有意识选择；反射=自动捕获。
Trinity 现在：
  reflex_flag(input): 反射标志（自动检测偏差/异常——快速）
"""
import os
import sys
import json


# 偏差关键词（自动捕获）
ANOMALY_WORDS = ("异常", "失败", "错误", "崩溃", "超时", "拒绝", "丢失",
                 "危险", "警告", "不一致", "波动")


def reflex_flag(text: str) -> dict:
    """反射标志：自动检测偏差（快速无意识——不占资源）。"""
    content = str(text)
    hits = [w for w in ANOMALY_WORDS if w in content]
    if hits:
        return {"flagged": True, "anomalies": hits[:3],
                "severity": "high" if len(hits) >= 2 else "medium",
                "reaction": "automatic_capture",
                "note": f"反射注意捕获异常：{hits[:3]}（自动——无需主动注意）"}
    # 数值波动检测（连续状态）
    import re
    nums = re.findall(r"\d+\.?\d*", content)
    if len(nums) >= 2:
        try:
            vals = [float(n) for n in nums[:5]]
            spread = max(vals) - min(vals)
            if spread > 10:
                return {"flagged": True, "anomalies": ["数值波动"],
                        "severity": "medium", "reaction": "automatic_capture",
                        "note": f"反射注意捕获数值波动（跨度 {round(spread)}）"}
        except Exception:
            pass
    return {"flagged": False, "anomalies": [],
            "note": "无异常（反射注意平静）"}


def reflex_scan(items: list) -> dict:
    """反射扫描：批量自动检测（不占主动注意）。"""
    flagged = []
    for it in items[:20]:
        r = reflex_flag(str(it.get("content") or ""))
        if r.get("flagged"):
            flagged.append({"content": str(it.get("content"))[:40],
                            "anomalies": r["anomalies"]})
    return {"flagged_count": len(flagged), "flagged": flagged[:5],
            "note": "反射扫描完成（自动捕获——零主动注意开销）"}
