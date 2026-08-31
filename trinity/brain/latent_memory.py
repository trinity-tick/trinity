# -*- coding: utf-8 -*-
"""trinity/brain/latent_memory.py — 潜在记忆（EXECUTION 265，大脑化）。

借鉴 FlashMem（2026：Distilling Intrinsic Latent Memory via
Computation Reuse）——把重复计算的结果蒸馏为潜在记忆（复用：
同类查询免重复计算——像"算过的问题记得答案"）。

Trinity 现在：
  distill_latent(query, result): 计算复用蒸馏（缓存结果）
  latent_hit(query): 潜在命中（复用缓存——免重复计算）
"""
import os
import sys
import json
import time


STATE_FILE = os.path.expanduser("~/.trinity/latent_memory.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"latents": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _key(query: str) -> str:
    """查询键（归一化——2 字词签名）。"""
    t = str(query)[:40]
    words = []
    for i in range(len(t) - 1):
        if "\u4e00" <= t[i] <= "\u9fff" and "\u4e00" <= t[i+1] <= "\u9fff":
            words.append(t[i:i+2])
    return "|".join(sorted(set(words))[:5])


def distill_latent(query: str, result: str, importance: float = 0.6) -> dict:
    """计算复用蒸馏：查询→结果缓存（潜在记忆）。"""
    st = _load()
    k = _key(query)
    st["latents"][k] = {"query": str(query)[:40], "result": str(result)[:150],
                        "hits": st["latents"].get(k, {}).get("hits", 0) + 1,
                        "ts": time.time(), "importance": importance}
    st["latents"] = dict(list(st["latents"].items())[-100:])
    _save(st)
    return {"distilled": True, "key": k, "hits": st["latents"][k]["hits"]}


def latent_hit(query: str) -> dict:
    """潜在命中：复用缓存结果（免重复计算）。"""
    st = _load()
    k = _key(query)
    lat = st["latents"].get(k)
    if lat:
        return {"hit": True, "result": lat["result"], "cached_hits": lat["hits"],
                "note": "潜在记忆命中（复用——免重复计算）"}
    return {"hit": False, "note": "无潜在命中（需计算）"}


def latent_report() -> dict:
    """潜在记忆报告（计算复用效率）。"""
    st = _load()
    latents = st.get("latents", {})
    total_hits = sum(l.get("hits", 0) for l in latents.values())
    return {"latents": len(latents), "total_reuses": total_hits,
            "efficiency": "高" if total_hits >= 10 else "积累中",
            "note": "重复计算已蒸馏为潜在记忆（复用）"}
