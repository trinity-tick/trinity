#!/usr/bin/env python3
"""trinity/brain/metacognition.py — 元认知层（2026-09，EXECUTION 105.6）

认知依据：元认知 = "知道自己知道什么 / 不知道什么"。两个能力：
  1. 信心评估：基于检索结果的数量/分数/通道覆盖估计回忆信心；
  2. 知识缺口识别：无结果或低信心时区分"库中确实没有"vs"检索失败"，
     记录 unknown-unknown（对标的 metacognitive monitoring）。

信心模型：
  conf = 0.40 * min(1, count/5)          # 结果量
       + 0.30 * top_score_norm            # 顶分质量（rrf/其他分归一）
       + 0.30 * min(1, n_channels/3)      # 通道覆盖（fts/vector/graph）
空结果 → conf=0，触发缺口分析。
"""

from __future__ import annotations

import logging
import os
import urllib.request
from typing import Any, Dict, List, Optional

from .value_encoder import llm_chat  # noqa: F401 复用 LLM 通道

logger = logging.getLogger("trinity.brain.meta")


def assess_confidence(results: List[Dict[str, Any]],
                      channels: Optional[List[str]] = None) -> Dict[str, Any]:
    """基于检索结果估计回忆信心（0-1）。"""
    n = len(results or [])
    if n == 0:
        return {"confidence": 0.0, "count": 0, "channels": channels or [],
                "level": "none"}
    scores = []
    for r in results:
        s = r.get("score")
        if isinstance(s, (int, float)) and s is not None:
            scores.append(float(s))
    top_norm = 0.0
    low_signal = False
    if scores:
        mx = max(scores)
        top_norm = min(1.0, mx) if mx > 0 else 0.0
        # 2026-09 校准：FTS 无匹配时 score=0.1（CASE ELSE 默认值）——全 0.1
        # 说明检索到的是"低相关兜底"（向量通道恒返回 top-k），信心打折。
        if mx <= 0.15:
            top_norm = 0.0
            low_signal = True
    n_ch = min(1.0, len(channels or []) / 3.0)
    conf = 0.40 * min(1.0, n / 5.0) + 0.30 * top_norm + 0.30 * n_ch
    if low_signal:
        conf *= 0.4  # 低相关兜底打折（防向量通道恒返回 top-k 的虚高）
    conf = round(min(1.0, conf), 3)
    level = "high" if conf >= 0.7 else ("medium" if conf >= 0.4 else "low")
    return {"confidence": conf, "count": n, "channels": channels or [],
            "level": level}


def detect_gap(query: str, results: List[Dict[str, Any]],
               channels: Optional[List[str]] = None,
               use_llm: bool = True,
               low_relevance: bool = False) -> Dict[str, Any]:
    """知识缺口识别：区分"库里没有"vs"检索失败"。

    Args:
        low_relevance: 有结果但向量相关度落入中间地带（0.45-0.65）——
            也交由 LLM 判断（结果可能是兜底而非真知识）。

    Returns:
        {"gap": bool, "reason": str, "suggestion": str}
        无结果/低相关且 LLM 可用时给出认知判断；LLM 失败降级为规则判断。
    """
    n = len(results or [])
    if n > 0 and not low_relevance:
        return {"gap": False, "reason": "检索到相关记忆", "suggestion": ""}
    if not use_llm:
        # 2026-09（EXECUTION 105.12）三态校准：cos 0.45-0.65 中间地带与
        # 无答案查询重叠严重（实测无答案 cos=0.53、弱相关有答案 cos=0.50），
        # 无 LLM 时中间地带判【uncertain】（不误报 gap）；仅明确低相关
        # （<0.45）或无结果才判 gap。
        if n == 0:
            return {"gap": True, "reason": "无检索结果（可能是知识缺失或表述差异）",
                    "suggestion": "建议换关键词重试"}
        # 中间地带（0.45-0.65）：无法确定 → uncertain（不误报 gap）
        return {"gap": False, "reason": "相关性存疑（中间地带）",
                "suggestion": "", "uncertain": True}
    # 2026-09（EXECUTION 105.12）：prompt 区分两种场景——无结果 vs
    # 有结果但相关性存疑（中间地带），避免误导 LLM 误判。
    if n == 0:
        scenario = "查询在记忆库中【没有检索到结果】"
        question = "这是【知识缺失】（从未记录过）还是【检索失败】（表述不同没匹配上）？"
    else:
        scenario = "查询【检索到了结果但相关性存疑】（向量相关度处于中间地带）"
        question = "这是【实际有相关知识但表达较弱】（不算缺口）还是【结果只是语义兜底、实际知识缺失】？"
    prompt = (
        "你是记忆系统的元认知模块。" + scenario + "。\n"
        "请判断：" + question + "\n"
        "只输出 JSON：{\"gap\":true或false,\"reason\":\"一句话\","
        "\"suggestion\":\"建议的改写查询\"}\n"
        "查询：" + str(query)[:200]
    )
    raw = llm_chat(prompt, max_tokens=200, temperature=0.2)
    if raw:
        try:
            s = raw.strip()
            if s.startswith("json"):
                s = s[4:].lstrip()
            data = json.loads(s)
            return {
                "gap": bool(data.get("gap", True)),
                "reason": str(data.get("reason", ""))[:200],
                "suggestion": str(data.get("suggestion", ""))[:200],
            }
        except Exception as e:  # noqa: BLE001
            logger.debug("gap parse failed: %s", e)
    return {"gap": True, "reason": "无检索结果",
            "suggestion": "建议换关键词重试"}


def persist_gap(conn, query: str, meta: Dict[str, Any]) -> None:
    """知识缺口落 PG gaps 表（幂等：同 query 24h 内不重复记录）。"""
    try:
        import json as _json
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gaps (
                gap_id     SERIAL PRIMARY KEY,
                query      TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                reason     TEXT,
                suggestion TEXT,
                detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            INSERT INTO gaps (query, confidence, reason, suggestion)
            SELECT %s, %s, %s, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM gaps
                WHERE query = %s AND detected_at > NOW() - INTERVAL '24 hours'
            )
        """, (query, meta.get("confidence", 0.0),
              meta.get("reason", ""), meta.get("suggestion", ""), query))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("persist_gap failed: %s", e)
