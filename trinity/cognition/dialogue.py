#!/usr/bin/env python3
"""trinity/cognition/dialogue.py — 对话层（2026-09，EXECUTION 105.22）

认知主体的对话界面：消息 → 工作记忆（当前关注）→ 记忆检索注入 →
意图理解（LLM）→ 响应生成（记忆上下文）→ 响应回写工作记忆。

对话本身就是认知循环的一部分：每轮对话更新"当前关注"（工作记忆）、
引用经历线（记忆）、自知（缺口提示）。

用法（端点 /cognition/chat）：
  POST /cognition/chat {"message": "...", "session_id": "..."}
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

from ..brain.working_memory import get_working_memory
from ..brain.value_encoder import llm_chat
from ..brain.metacognition import assess_confidence

logger = logging.getLogger("trinity.cognition.dialogue")

NL = chr(10)
MAX_HISTORY = 6  # 工作记忆轮次上限（注意容量）

# 2026-09-02（brain fix）：对话客户端单例——每次调用新建 TrinityClient() 会重复
# 支付引擎初始化/连接/BM25 构建成本（实测单轮对话 28.5s，其中两处各建一个 client）。
_client = None


def _get_client():
    global _client
    if _client is None:
        from ..core.client import TrinityClient
        _client = TrinityClient()
    return _client


def _retrieve(query: str, top_k: int = 4) -> list:
    try:
        c = _get_client()
        data = c.search_hybrid(query=query, top_k=top_k, strategy="rrf")
        results = data.get("results", []) if isinstance(data, dict) else data
        return [str(r.get("content_preview") or r.get("content") or "")[:250]
                for r in results[:top_k]]
    except Exception:
        return []


def _assess(query: str) -> Dict[str, Any]:
    """对话轮次的元认知（信心+缺口提示）。"""
    try:
        c = _get_client()
        data = c.search_hybrid(query=query, top_k=5, strategy="rrf")
        results = data.get("results", []) if isinstance(data, dict) else data
        channels = []
        if isinstance(data, dict):
            channels = (data.get("breakdown") or {}).get("channels", [])
        return assess_confidence(results, channels)
    except Exception:
        return {"confidence": 0.0, "level": "none"}


def chat(message: str, session_id: str = "default") -> Dict[str, Any]:
    """一轮对话：wm 更新 + 记忆注入 + LLM 响应 + 元认知标注。"""
    t0 = time.time()
    wm = get_working_memory()
    # 1) 消息进工作记忆（当前关注，注意容量驱逐）
    wm.push(session_id, "msg:" + str(message)[:40], str(message)[:500],
            importance=0.6)
    # 2) 历史关注（工作记忆）作为上下文
    focus = [i["content"][:120] for i in wm.get(session_id, top_k=MAX_HISTORY)
             if not i["key"].startswith("think:")]
    focus_text = NL.join("- " + f for f in focus[-4:]) or "（无）"
    # 3) 记忆检索注入
    mems = _retrieve(message, top_k=4)
    mem_text = NL.join("- " + m for m in mems) or "（无相关记忆）"
    # 4) 元认知
    meta = _assess(message)
    # 5) LLM 响应（带记忆上下文 + 自我认知）
    prompt = (
        "你是 Trinity 认知主体，正在与用户对话。基于你的记忆（经历线）"
        "和当前关注，自然、简洁地回应（200 字内）。\n"
        "如果记忆不足（confidence 低），明确说明并追问。\n"
        "当前关注：" + focus_text + NL
        + "相关记忆：" + NL + mem_text + NL
        + "消息：" + str(message)[:300]
    )
    reply = llm_chat(prompt, max_tokens=400, temperature=0.5)
    reply = reply.strip() if reply else "（LLM 不可用）"
    # 6) 响应回写工作记忆
    wm.push(session_id, "reply:" + str(message)[:40], str(reply)[:500],
            importance=0.5)
    return {
        "session_id": session_id,
        "reply": reply,
        "metacognition": meta,
        "memories_used": len(mems),
        "latency_s": round(time.time() - t0, 2),
    }
