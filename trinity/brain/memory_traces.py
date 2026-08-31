# -*- coding: utf-8 -*-
"""trinity/brain/memory_traces.py — 记忆轨迹（EXECUTION 315，大脑化）。

借鉴 MemChain（2026：Learning Interpretable Memory Traces）——
推理过程中记忆的调用链（哪些记忆被用了、什么顺序——可解释
使用轨迹）。

与谱系（静态关系）互补：谱系=来源关系；本模块=使用轨迹。
Trinity 现在：
  trace_usage(query): 记忆使用轨迹（调用链记录）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/memory_traces.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"traces": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def trace_usage(query: str, memories: list = None) -> dict:
    """记忆使用轨迹：记录调用链（顺序/贡献）。"""
    chain = []
    if memories is None:
        # 自动检索
        try:
            sys.path.insert(0, r"D:\\trinity-code")
            from trinity import Trinity
            m = Trinity(adapter="postgresql")
            r = m.search_hybrid(query[:30], top_k=3)
            items = r if isinstance(r, list) else r.get("results", [])
            memories = items
        except Exception:
            memories = []
    for i, mem in enumerate(memories[:3]):
        chain.append({"order": i + 1,
                      "content": str(mem.get("content") or "")[:40],
                      "used": True})
    st = _load()
    st["traces"].append({"query": str(query)[:30], "chain": chain,
                         "ts": __import__("time").time()})
    st["traces"] = st["traces"][-20:]
    _save(st)
    return {"query": str(query)[:30], "chain": chain, "depth": len(chain),
            "note": f"记忆轨迹：{len(chain)} 步调用链（可解释）"}


def trace_report() -> dict:
    """轨迹统计。"""
    st = _load()
    return {"traces": len(st.get("traces", [])),
            "note": "记忆轨迹：推理调用链可解释（MemChain）"}
