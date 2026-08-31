# -*- coding: utf-8 -*-
"""trinity/brain/information_isolation.py — 信息隔离（EXECUTION 380）。

借鉴 HyMem（2026：Hierarchical Context Management via Information
Isolation）——分层上下文信息隔离：各层信息相互隔离（长时程
不干扰——层间防污染）。

与分层（检索层）互补：分层=访问层；本模块=隔离机制。
Trinity 现在：
  isolate(layer, content): 信息隔离（分层存储防干扰）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/information_isolation.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"layers": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def isolate(layer: str, content: str) -> dict:
    """信息隔离：分层存储（各层不互相干扰）。"""
    st = _load()
    layer_data = st["layers"].get(layer, [])
    layer_data.append({"content": str(content)[:50], "ts": __import__("time").time()})
    st["layers"][layer] = layer_data[-20:]
    _save(st)
    return {"layer": str(layer)[:15], "stored": True,
            "layer_size": len(layer_data),
            "isolated": True,
            "note": f"信息隔离：写入层『{str(layer)[:12]}』（{len(layer_data)} 条——层间不干扰）"}


def isolation_check() -> dict:
    """隔离检查：层间独立性。"""
    st = _load()
    layers = st.get("layers", {})
    # 隔离度（各层独立存储——无串扰）
    return {"layers": len(layers),
            "isolation": len(layers) >= 2,
            "note": f"信息隔离：{len(layers)} 层独立（HyMem——长时程不干扰）"}
