# -*- coding: utf-8 -*-
"""trinity/brain/latent_mixture.py — 潜在混合（EXECUTION 372）。

借鉴 Dynamic Mixture of Latent Memories（2026：Self-Evolving
Agents）——动态潜在记忆混合：多潜在记忆按上下文动态混合
（权重随上下文变化——自进化）。

与潜在记忆（缓存）互补：缓存=复用；本模块=动态混合。
Trinity 现在：
  mixture(query, latents): 动态混合（上下文→权重）
"""
import os
import sys
import json


def mixture(query: str, latents: dict) -> dict:
    """动态混合：按上下文分配各潜在记忆权重。"""
    q = str(query)
    scored = []
    for name, latent in latents.items():
        content = str(latent.get("content") or "")
        # 上下文相关度（词重叠）
        qw = set(q[:20])
        cw = set(content[:20])
        overlap = len(qw & cw) / max(len(qw), 1) if qw else 0
        # 混合权重（相关×可用性）
        weight = overlap * 0.6 + float(latent.get("reliability", 0.5)) * 0.4
        scored.append({"memory": str(name)[:20], "weight": round(weight, 2)})
    # 归一化权重
    total = sum(s["weight"] for s in scored) or 1
    for s in scored:
        s["weight"] = round(s["weight"] / total, 3)
    scored.sort(key=lambda x: -x["weight"])
    return {"query": q[:25], "mixture": scored,
            "dominant": scored[0]["memory"] if scored else None,
            "dynamic": True,
            "note": f"动态混合：'{scored[0]['memory'] if scored else '无'}' 主导（权重随上下文——自进化）"}


def mixture_report() -> dict:
    """混合体系状态。"""
    return {"note": "动态潜在记忆混合：上下文驱动权重（Self-Evolving）"}
