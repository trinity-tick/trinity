"""
GLong-T5 Attention Selector — Long Context Importance Scoring
==============================================================
arXiv 2605.16481 · P46-5

基于 T5 注意力权重的长文本重要性评分器，对超长上下文每个 segment 打分，
按 token 预算选择保留 segment，低重要性 segment 压缩为摘要而非丢弃。

设计要点:
  - GLongT5AttentionScorer: 注意力权重重要性评分
  - LongContextSegmentSelector: 按分选段, 最大化信息密度
  - AttentionImportanceCache: 缓存重要性分数, 避免重复计算
  - GLongContextCompressor: 低重要性 segment → 短摘要
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple
from collections import OrderedDict
import hashlib

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class AttentionImportanceScore:
    """注意力重要性分数——单个 segment 的重要性评估。"""
    segment_id: str
    score: float = 0.0
    attention_weight: float = 0.0     # 原始注意力权重 (0~1)
    position_bonus: float = 0.0       # 位置偏置 (近因/首因)
    semantic_completeness: float = 0.0  # 语义完整性
    token_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SegmentedContext:
    """分段上下文——被切分为多个 segment 的上下文。"""
    context_id: str
    segments: List[str] = field(default_factory=list)
    scores: List[AttentionImportanceScore] = field(default_factory=list)
    total_tokens: int = 0
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# AttentionImportanceCache
# ---------------------------------------------------------------------------

class AttentionImportanceCache:
    """注意力重要性缓存——避免重复计算同一 segment 的重要性。

    Parameters
    ----------
    max_entries : int
        最大缓存条目数 (LRU 淘汰)。
    """

    def __init__(self, max_entries: int = 500) -> None:
        self.max_entries = max_entries
        self._cache: OrderedDict[str, List[AttentionImportanceScore]] = OrderedDict()
        self._lock = threading.RLock()

    def _hash(self, context_id: str) -> str:
        return hashlib.md5(context_id.encode()).hexdigest()[:16]

    def get(self, context_id: str) -> Optional[List[AttentionImportanceScore]]:
        key = self._hash(context_id)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        return None

    def set(self, context_id: str, scores: List[AttentionImportanceScore]) -> None:
        key = self._hash(context_id)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = scores
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)

    def statistics(self) -> Dict[str, Any]:
        return {"entries": len(self._cache), "max_entries": self.max_entries}


# ---------------------------------------------------------------------------
# GLongT5AttentionScorer
# ---------------------------------------------------------------------------

class GLongT5AttentionScorer:
    """GLong-T5 注意力评分器——对每个 segment 计算重要性分数。

    评分公式: score = w_attn * attention + w_pos * position + w_sem * completeness
    """

    def __init__(self) -> None:
        self._w_attn: float = 0.5
        self._w_pos: float = 0.25
        self._w_sem: float = 0.25
        self.cache = AttentionImportanceCache()
        self._lock = threading.RLock()

    def score(
        self, segments: List[str], attention_weights: Optional[List[float]] = None,
        context_id: str = "",
    ) -> List[AttentionImportanceScore]:
        """对 segments 批量打分。

        Parameters
        ----------
        segments : List[str]
            上下文段。
        attention_weights : Optional[List[float]]
            T5 注意力权重 (0~1), 长度与 segments 一致。
        context_id : str
            用于缓存 key。
        """
        # 查缓存
        if context_id:
            cached = self.cache.get(context_id)
            if cached:
                return cached

        with self._lock:
            n = len(segments)
            if attention_weights is None:
                attention_weights = [1.0 / n] * n

            scores = []
            for i, seg in enumerate(segments):
                attn = attention_weights[i] if i < len(attention_weights) else 0.0

                # 位置偏置: 首尾段加分
                if i == 0:
                    pos = 0.9
                elif i == n - 1:
                    pos = 0.7
                else:
                    pos = 0.5

                # 语义完整性: 以句子边界判断
                completeness = 0.8 if seg.rstrip().endswith(('.', '!', '?', '。')) else 0.5

                score_val = self._w_attn * attn + self._w_pos * pos + self._w_sem * completeness

                scores.append(AttentionImportanceScore(
                    segment_id=f"seg_{i}_{len(seg)}",
                    score=round(score_val, 4),
                    attention_weight=round(attn, 4),
                    position_bonus=round(pos, 4),
                    semantic_completeness=round(completeness, 4),
                    token_count=len(seg.split()),
                ))

            if context_id:
                self.cache.set(context_id, scores)
            return scores

    def statistics(self) -> Dict[str, Any]:
        return {
            "weights": {"attention": self._w_attn, "position": self._w_pos, "semantic": self._w_sem},
            "cache": self.cache.statistics(),
        }


# ---------------------------------------------------------------------------
# LongContextSegmentSelector
# ---------------------------------------------------------------------------

class LongContextSegmentSelector:
    """长上下文段选择器——按重要性分数选择保留的 segment。

    在 token 预算内最大化信息密度。
    """

    def __init__(self, token_budget: int = 4096) -> None:
        self.token_budget = token_budget
        self._lock = threading.RLock()

    def select(
        self, scored: SegmentedContext, min_score: float = 0.1,
    ) -> Tuple[List[str], List[str]]:
        """选择保留和淘汰的 segment。

        Returns
        -------
        Tuple[List[str], List[str]]
            (保留段列表, 淘汰段列表)
        """
        with self._lock:
            # 按分数降序
            indexed = sorted(
                enumerate(scored.scores), key=lambda x: x[1].score, reverse=True)

            kept: List[str] = []
            evicted: List[str] = []
            used_tokens = 0

            for idx, score_obj in indexed:
                seg = scored.segments[idx]
                seg_tokens = score_obj.token_count or len(seg.split())
                if used_tokens + seg_tokens <= self.token_budget and score_obj.score >= min_score:
                    kept.append(seg)
                    used_tokens += seg_tokens
                else:
                    evicted.append(seg)

            return kept, evicted

    def statistics(self) -> Dict[str, Any]:
        return {"token_budget": self.token_budget}


# ---------------------------------------------------------------------------
# GLongContextCompressor
# ---------------------------------------------------------------------------

class GLongContextCompressor:
    """上下文压缩器——将淘汰的低重要性 segment 压缩为短摘要。

    Parameters
    ----------
    compression_ratio : float
        目标压缩比 (摘要 token 数 / 原始 token 数)。
    """

    def __init__(self, compression_ratio: float = 0.15) -> None:
        self.compression_ratio = compression_ratio
        self._lock = threading.RLock()

    def compress(self, segments: List[str]) -> str:
        """压缩多个 segment 为一条摘要。

        规则: 取每段首句 + 关键实体词, LLM-free。
        """
        with self._lock:
            if not segments:
                return ""

            summaries = []
            for seg in segments:
                sentences = seg.replace('\n', ' ').split('.')
                first = sentences[0].strip() if sentences else seg[:80]
                if first:
                    summaries.append(first)

            if not summaries:
                return ""

            combined = ". ".join(summaries[:max(1, int(len(summaries) * self.compression_ratio))])
            return combined[:200]  # 硬截断

    def statistics(self) -> Dict[str, Any]:
        return {"compression_ratio": self.compression_ratio}
