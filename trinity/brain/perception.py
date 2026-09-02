#!/usr/bin/env python3
"""trinity/brain/perception.py — 感知事件流（2026-09，EXECUTION 105.7）

认知依据：具身记忆由持续感知流塑造——感官输入经显著性筛选进入记忆
（刺激-反应编码）；【习惯化】是神经适应特性：重复刺激显著性递减
（Grokking 式神经适应/习惯化，对应海兔缩鳃反射的经典研究）。

实现：
  - 感知通道 API：/memory/perceive {channel, signal, importance?, session_id?}
  - 显著性评估：规则基线（alert/error/critical 通道高显著）+ 可选 LLM 价值；
  - 习惯化：同 (channel, signal 归一) 24h 内重复 → 显著性衰减/去重；
  - 编码：高显著信号写入 PG memories（category='perception'，
    importance=评估值）；感知日志落 perceptions 表。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, Optional

from .value_encoder import estimate_value  # noqa: F401

logger = logging.getLogger("trinity.brain.perception")

# 通道显著性基线（0-1）
CHANNEL_BASE = {
    "alert": 0.7, "error": 0.7, "critical": 0.85, "warning": 0.5,
    "system": 0.3, "session": 0.4, "user": 0.5, "external": 0.4,
    "monitor": 0.6, "web": 0.6, "websearch": 0.6, "vision": 0.6,  # 2026-09-02 (EXECUTION 458): 视觉通道（语义截图感知）
    "default": 0.4,
}
# 习惯化窗口（秒）
HABITUATION_WINDOW = 86400


def _signal_key(channel: str, signal: str) -> str:
    norm = " ".join(str(signal).split())[:200]
    return hashlib.sha256((channel + "|" + norm).encode()).hexdigest()[:24]


class PerceptionEngine:
    """感知引擎（进程内单例）：显著性评估 + 习惯化 + 感知编码。"""

    def __init__(self):
        # signal_key -> last_seen_ts（习惯化记忆，进程内+PG 幂等去重）
        self._seen: Dict[str, float] = {}
        self._counters: Dict[str, int] = {}

    def evaluate(self, channel: str, signal: str,
                 importance: Optional[float] = None) -> Dict[str, Any]:
        """显著性评估：通道基线 + 习惯化衰减 + 可选 LLM 价值。"""
        base = CHANNEL_BASE.get(channel, CHANNEL_BASE["default"])
        key = _signal_key(channel, signal)
        now = time.time()
        last = self._seen.get(key)
        repeat = 0
        if last is not None and now - last < HABITUATION_WINDOW:
            repeat = self._counters.get(key, 1)
        # 习惯化：重复次数 → 显著性衰减（第 2 次 0.7x，第 3 次 0.5x，之后 0.3x）
        habituation = 1.0
        if repeat >= 3:
            habituation = 0.3
        elif repeat == 2:
            habituation = 0.5
        elif repeat == 1:
            habituation = 0.7
        self._seen[key] = now
        self._counters[key] = repeat + 1
        salience = base * habituation
        # 2026-09（EXECUTION 105.10）：感知默认规则优先（TRINITY_PERCEPTION_LLM
        # =1 才启用 LLM 校准——感知高频场景，规则显著性足够，省 LLM 成本）
        value = None
        if (importance is None and salience >= 0.5
                and os.environ.get("TRINITY_PERCEPTION_LLM", "0") == "1"):
            value = estimate_value(signal)
        final_imp = min(1.0, max(0.0, value["value"] if value else salience))
        return {
            "channel": channel,
            "salience": round(salience, 3),
            "habituation": round(habituation, 3),
            "repeat": repeat,
            "importance": round(final_imp, 3),
        }

    def attend_filter(self, signals: list, goal_focus: str = "", top_n: int = 5) -> list:
        """注意力筛选（EXECUTION 207）：多信号竞争——显著性×价值×目标。"""
        try:
            from trinity.brain.attention_control import attend
            cands = [{"signal": s.get("signal", ""), "salience": s.get("salience", 0.5),
                      "value": s.get("importance", 0.5)} for s in signals]
            r = attend(cands, goal_focus=goal_focus, top_n=top_n)
            attended = {a["item"] for a in r["attended"]}
            return [s for s in signals if (s.get("signal") or "") in attended]
        except Exception:
            return signals


    def should_encode(self, salience: float) -> bool:
        """高显著才编码进长期记忆（感知门控）。"""
        return salience >= 0.45


# 进程级单例
_PERCEPTION: Optional[PerceptionEngine] = None


def get_perception_engine() -> PerceptionEngine:
    global _PERCEPTION
    if _PERCEPTION is None:
        _PERCEPTION = PerceptionEngine()
    return _PERCEPTION
# -*- coding: utf-8 -*-
# EXECUTION 128: 感知记忆向量+分词异步回填（独立函数，供 API 调用）
def backfill_signal_async(signal_text: str) -> None:
    import threading as _th
    _sig = str(signal_text)[:800]
    if not _sig.strip():
        return

    def _work() -> None:
        try:
            import sys as _sys, os as _os
            _root = r"D:\\trinity-code"
            if _root not in _sys.path:
                _sys.path.insert(0, _root)
            _os.environ.setdefault("HF_HUB_OFFLINE", "1")
            from trinity.core.client._helpers import _get_embedding_engine
            import psycopg2 as _pg
            _eng = _get_embedding_engine()
            _vec = None
            if _eng is not None:
                _v = _eng.embed(_sig)
                _vec = [float(x) for x in _v] if hasattr(_v, '__iter__') else None
            _tsv = None
            try:
                import jieba as _jb
                _jb.setLogLevel(60)
                _words = [w.strip() for w in _jb.cut(_sig) if w.strip() and len(w.strip()) >= 2][:12]
                if _words:
                    _tsv = " | ".join(_words)
            except Exception:
                pass
            if _vec or _tsv:
                _conn = _pg.connect(host="127.0.0.1", port=5432,
                                    dbname="trinity", user="trinity", password="trinity")
                _conn.autocommit = True
                _cur = _conn.cursor()
                _sql = "UPDATE memories SET embedding = %s, content_tsv_zh = to_tsvector('simple', %s) WHERE content = %s AND category = 'perception'"
                _cur.execute(_sql, (_vec, _tsv or "", _sig))
                _conn.close()
        except Exception:
            pass

    _th.Thread(target=_work, daemon=True, name="perception-backfill").start()
