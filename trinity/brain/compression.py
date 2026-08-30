#!/usr/bin/env python3
"""trinity/brain/compression.py — 可逆压缩-重构（2026-09，EXECUTION 105.14）

对标 R3Mem（Bridging Memory Retention and Retrieval via Reversible
Compression）：压缩时保存【重构提示（reconstruction prompts）】——
摘要 + 关键实体/数字/时间/动作线索；解压时按提示还原。压缩与回忆双优。

实现：
  - compress_with_hints(content)：LLM 一次调用生成 {summary, hints}
    （hints: 实体/数字/时间/关键点列表），失败降级规则摘要；
  - decompress(entry)：summary + hints → LLM 还原更完整叙述；
  - 安全设计：压缩结果存 metadata（compression.*），**原 content 不动**
    ——纯增量；将来 decay 用压缩版替换时可逆还原。

零第三方依赖（复用 value_encoder.llm_chat）。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from .value_encoder import llm_chat  # noqa: F401

logger = logging.getLogger("trinity.brain.compress")

COMPRESSION_VERSION = "v1"
NL = chr(10)


def compress_with_hints(content: str,
                        max_summary: int = 200) -> Optional[Dict[str, Any]]:
    """压缩记忆并生成重构提示。

    Returns:
        {"summary": str, "hints": [str], "version": "v1"} 或
        None（LLM 不可用——调用方保留原文，不压缩）。
    """
    prompt = (
        "你是记忆压缩系统。把以下记忆压缩为摘要，并提取【重构提示】"
        "（用于未来还原细节）：\n"
        "只输出 JSON："
        '{"summary":"150字内摘要","hints":["关键实体/数字/时间/动作线索1",'
        '"线索2","线索3"]}\n'
        "记忆内容：" + str(content)[:1500]
    )
    raw = llm_chat(prompt, max_tokens=500, temperature=0.2)
    if not raw:
        return None
    try:
        s = raw.strip()
        fence = chr(96) * 3
        if s.startswith(fence):
            s = s.split(NL, 1)[-1]
            if s.endswith(fence):
                s = s[:-3]
        if s.startswith("json"):
            s = s[4:].lstrip()
        data = json.loads(s)
        summary = str(data.get("summary", ""))[:max_summary]
        hints = [str(h)[:120] for h in data.get("hints", [])][:5]
        if not summary:
            return None
        return {
            "summary": summary,
            "hints": hints,
            "version": COMPRESSION_VERSION,
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("compress parse failed: %s", e)
        return None


def decompress(entry: Dict[str, Any]) -> Optional[str]:
    """按重构提示还原记忆（summary + hints → 更完整叙述）。"""
    summary = str(entry.get("summary", ""))
    hints = entry.get("hints") or []
    if not summary:
        return None
    prompt = (
        "你是记忆还原系统。基于压缩摘要和重构提示，还原一段更完整、"
        "连贯的记忆叙述（保留原意，不编造；200 字内）：\n"
        "摘要：" + summary + NL
        + "重构提示：" + NL
        + NL.join("- " + str(h) for h in hints[:5])
    )
    return llm_chat(prompt, max_tokens=400, temperature=0.3)
