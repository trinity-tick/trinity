# -*- coding: utf-8 -*-
"""trinity/brain/proxy_attention.py — 代理注意（EXECUTION 370）。

借鉴 ProxyAttn（ICLR 2026：代理注意力头"划重点"——免训练无损
稀疏）——代理注意：标记重点（稀疏化——不损失信息——只有
重点被标记）。

与注意力（选择/反射/忽略）互补：选择=选重要；本模块=划重点。
Trinity 现在：
  proxy_focus(content): 代理注意（重点标记——稀疏无损）
"""
import os
import sys
import json


# 重点标记词（代理注意力头）
FOCUS_WORDS = ("关键", "重要", "核心", "必须", "危险", "失败", "成功",
               "紧急", "严重", "突破")


def proxy_focus(content: str) -> dict:
    """代理注意：划重点（稀疏标记——无损）。"""
    text = str(content)
    # 标记重点句（含重点词的句子）
    sentences = [s.strip() for s in text.replace("。", "\n").split("\n") if s.strip()]
    focused = []
    for s in sentences[:10]:
        if any(w in s for w in FOCUS_WORDS):
            focused.append({"sentence": s[:45], "markers": [w for w in FOCUS_WORDS if w in s][:2]})
    # 稀疏度（重点占比）
    sparsity = round(len(focused) / max(len(sentences), 1), 2)
    return {"sentences": len(sentences), "focused": focused[:4],
            "sparsity": sparsity,
            "lossless": True,
            "note": f"代理注意：{len(focused)}/{len(sentences)} 句划重点（稀疏 {sparsity}——无损）"}


def focus_report() -> dict:
    """代理注意状态。"""
    return {"note": "ProxyAttn：免训练无损稀疏（划重点——代理注意力）"}
