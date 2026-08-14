"""
CB74: MemoryAwakeningPipeline — 记忆苏醒管线
============================================

三阶段记忆苏醒管线，对标 EverMemOS。

核心设计:
  - Phase1_EpisodicTraceFormation: 对话流 → 结构化事件轨迹，
    提取参与者/时间/位置/行动/结果五元组(TraceQuintuple)
  - Phase2_TraceCompressor: 活跃轨迹 → 精华记忆，
    去除冗余、合并相似、保留关键决策链和意外转折
  - Phase3_MemoryAwakener: 上下文不足时优先唤醒高分记忆注入 LLM，
    基于相关性/新鲜度/重要性三维评分
  - CompressionPolicy: 压缩策略(激进/保守/自适应)和触发阈值
  - MemoryShelf: 记忆保质期标签和遗忘调度

Reference:
  - EverMemOS: Three-Stage Memory Pipeline for Persistent Agent Memory
"""

from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class CompressionMode(Enum):
    AGGRESSIVE = "aggressive"  # 激进：大幅压缩
    CONSERVATIVE = "conservative"  # 保守：保留更多细节
    ADAPTIVE = "adaptive"      # 自适应：根据记忆密度调整


class MemoryShelfState(Enum):
    FRESH = "fresh"            # 新鲜活跃
    STALE = "stale"            # 即将过期
    ARCHIVED = "archived"      # 已归档
    FORGOTTEN = "forgotten"    # 已遗忘


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class TraceQuintuple:
    """事件五元组——结构化事件表示。

    Attributes:
        who: 参与者。
        when: 时间戳。
        where: 位置/上下文。
        action: 行动/操作。
        outcome: 结果/影响。
    """
    who: str = ""
    when: float = 0.0
    where: str = ""
    action: str = ""
    outcome: str = ""
    trace_id: str = ""


@dataclass
class EventTrace:
    """结构化事件轨迹。"""
    trace_id: str
    quintuple: TraceQuintuple = field(default_factory=TraceQuintuple)
    raw_text: str = ""
    confidence: float = 1.0
    metadata: Dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=_time.time)


@dataclass
class AwakeningScore:
    """苏醒评分——决定哪些记忆优先注入 LLM 上下文。"""
    relevance: float = 0.0     # 与当前查询的相关性
    freshness: float = 0.0     # 新鲜度（时间衰减后）
    importance: float = 0.0    # 记忆的重要性标签
    composite: float = 0.0     # 综合评分

    def compute(self, w_relevance: float = 0.4, w_freshness: float = 0.3, w_importance: float = 0.3):
        self.composite = w_relevance * self.relevance + w_freshness * self.freshness + w_importance * self.importance
        return self.composite


@dataclass
class CompressionPolicy:
    """压缩策略定义。"""
    mode: CompressionMode = CompressionMode.ADAPTIVE
    max_traces_per_batch: int = 100
    similarity_threshold: float = 0.75   # 合并相似事件阈值
    min_trace_age_seconds: float = 300.0 # 最小等待时间
    retention_ratio: float = 0.3         # 压缩后保留比例
    trigger_on_trace_count: int = 200    # 触发压缩的轨迹数


@dataclass
class MemoryShelf:
    """记忆书架——带保质期的记忆存储单元。"""
    shelf_id: str
    trace_ids: List[str] = field(default_factory=list)
    compressed_content: str = ""
    state: MemoryShelfState = MemoryShelfState.FRESH
    created_at: float = field(default_factory=_time.time)
    expires_at: float = float("inf")
    importance: float = 0.5
    access_count: int = 0


# ============================================================================
# EpisodicTraceFormation (Phase 1)
# ============================================================================

class EpisodicTraceFormation:
    """阶段一：对话流 → 结构化事件轨迹。

    从原始对话提取五元组，构建 EventTrace。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._traces: Dict[str, EventTrace] = {}
        self._formation_count: int = 0

    def form_from_turn(
        self, turn_text: str, speaker: str = "user",
        context: str = "", turn_index: int = 0,
    ) -> EventTrace:
        """从单轮对话生成事件轨迹。

        Args:
            turn_text: 对话文本。
            speaker: 说话人。
            context: 上下文环境。
            turn_index: 轮次序号。

        Returns:
            生成的事件轨迹。
        """
        with self._lock:
            trace_id = f"et_{self._formation_count:06d}"
            quintuple = TraceQuintuple(
                who=speaker,
                when=_time.time(),
                where=context or "conversation",
                action=self._extract_action(turn_text),
                outcome=self._extract_outcome(turn_text),
                trace_id=trace_id,
            )
            trace = EventTrace(
                trace_id=trace_id, quintuple=quintuple,
                raw_text=turn_text, metadata={"turn_index": str(turn_index)},
            )
            self._traces[trace_id] = trace
            self._formation_count += 1
            return trace

    @staticmethod
    def _extract_action(text: str) -> str:
        # Simplified: use first sentence as action
        sentences = text.replace("!", ".").replace("?", ".").split(".")
        return sentences[0].strip()[:200] if sentences else text[:200]

    @staticmethod
    def _extract_outcome(text: str) -> str:
        sentences = text.replace("!", ".").replace("?", ".").split(".")
        if len(sentences) > 1:
            return sentences[-1].strip()[:200]
        return ""

    def get_traces_since(self, since: float) -> List[EventTrace]:
        with self._lock:
            return [t for t in self._traces.values() if t.created_at >= since]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"traces_formed": self._formation_count, "active_traces": len(self._traces)}


# ============================================================================
# TraceCompressor (Phase 2)
# ============================================================================

class TraceCompressor:
    """阶段二：轨迹压缩——冗余去除、相似合并、保留关键决策链。"""

    def __init__(self, policy: Optional[CompressionPolicy] = None):
        self.policy = policy or CompressionPolicy()
        self._lock = threading.RLock()
        self._compressed_shelves: Dict[str, MemoryShelf] = {}
        self._compress_count: int = 0

    def compress(self, traces: List[EventTrace]) -> MemoryShelf:
        """压缩一组轨迹为一个 MemoryShelf。

        Args:
            traces: 待压缩的轨迹列表。

        Returns:
            压缩后的记忆书架。
        """
        with self._lock:
            self._compress_count += 1
            shelf_id = f"shelf_{self._compress_count:04d}"

            # Deduplicate similar traces
            deduped = self._deduplicate(traces)

            # Generate compressed summary
            actions = [t.quintuple.action for t in deduped if t.quintuple.action]
            compressed = "; ".join(actions[:5])
            if len(actions) > 5:
                compressed += f" ... (+{len(actions) - 5} more)"

            shelf = MemoryShelf(
                shelf_id=shelf_id,
                trace_ids=[t.trace_id for t in deduped],
                compressed_content=compressed,
                created_at=_time.time(),
                expires_at=_time.time() + 86400 * 30,  # 30 days
                importance=max(len(deduped) / 100.0, 0.1),
            )
            self._compressed_shelves[shelf_id] = shelf
            return shelf

    def _deduplicate(self, traces: List[EventTrace]) -> List[EventTrace]:
        if not traces:
            return []
        deduped = [traces[0]]
        for trace in traces[1:]:
            is_dup = False
            for existing in deduped:
                overlap = len(set(trace.raw_text.split()) & set(existing.raw_text.split()))
                total = max(len(set(trace.raw_text.split()) | set(existing.raw_text.split())), 1)
                if overlap / total > self.policy.similarity_threshold:
                    is_dup = True
                    break
            if not is_dup:
                deduped.append(trace)
        return deduped

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "compressions": self._compress_count,
                "shelves": len(self._compressed_shelves),
                "mode": self.policy.mode.value,
            }


# ============================================================================
# MemoryAwakener (Phase 3)
# ============================================================================

class MemoryAwakener:
    """阶段三：记忆苏醒——上下文不足时优先注入高分记忆。"""

    def __init__(
        self, w_relevance: float = 0.4, w_freshness: float = 0.3, w_importance: float = 0.3,
        max_awaken: int = 5,
    ):
        self.w_relevance = w_relevance
        self.w_freshness = w_freshness
        self.w_importance = w_importance
        self.max_awaken = max_awaken
        self._lock = threading.RLock()
        self._awaken_count: int = 0

    def score(
        self, shelf: MemoryShelf, query: str, current_time: Optional[float] = None,
    ) -> AwakeningScore:
        """对记忆书架评分。

        Args:
            shelf: 记忆书架。
            query: 当前查询。
            current_time: 当前时间。

        Returns:
            苏醒评分。
        """
        if current_time is None:
            current_time = _time.time()

        # Relevance: keyword overlap with query
        query_words = set(query.lower().split())
        content_words = set(shelf.compressed_content.lower().split())
        relevance = len(query_words & content_words) / max(len(query_words), 1)

        # Freshness: exponential decay from creation
        age_hours = (current_time - shelf.created_at) / 3600.0
        freshness = 2.0 ** (-age_hours / 24.0)  # half-life ~24h

        score = AwakeningScore(
            relevance=relevance, freshness=freshness, importance=shelf.importance,
        )
        score.compute(self.w_relevance, self.w_freshness, self.w_importance)
        return score

    def awaken(
        self, shelves: List[MemoryShelf], query: str, context_budget_tokens: int = 4096,
    ) -> List[Tuple[MemoryShelf, AwakeningScore]]:
        """唤醒最有价值的记忆注入上下文。

        Args:
            shelves: 候选记忆书架列表。
            query: 当前查询。
            context_budget_tokens: 上下文 token 预算。

        Returns:
            [(shelf, score), ...] 按复合分降序。
        """
        with self._lock:
            scored = []
            for s in shelves:
                aws = self.score(s, query)
                scored.append((s, aws))
            scored.sort(key=lambda x: x[1].composite, reverse=True)
            self._awaken_count += 1
            return scored[:self.max_awaken]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"awaken_calls": self._awaken_count, "max_awaken": self.max_awaken}


# ============================================================================
# Main Class
# ============================================================================

class MemoryAwakeningPipeline:
    """记忆苏醒管线 (CB74)。

    三阶段流水线：
      1. EpisodicTraceFormation: 对话 → 事件轨迹
      2. TraceCompressor: 压缩去重
      3. MemoryAwakener: 按需苏醒注入上下文

    Usage:
        map = MemoryAwakeningPipeline()
        trace = map.form_trace("用户说要去巴黎", speaker="user")
        shelf = map.compress(map.formation.get_traces_since(0))
        awakened = map.awaken(query="巴黎行程")
    """

    def __init__(self, policy: Optional[CompressionPolicy] = None):
        self._lock = threading.RLock()
        self.formation = EpisodicTraceFormation()
        self.compressor = TraceCompressor(policy=policy)
        self.awakener = MemoryAwakener()
        self._shelves: Dict[str, MemoryShelf] = {}
        self._start_time = _time.time()

    def form_trace(self, turn_text: str, speaker: str = "user", context: str = "", turn_index: int = 0) -> EventTrace:
        return self.formation.form_from_turn(turn_text, speaker, context, turn_index)

    def compress(self, traces: List[EventTrace]) -> MemoryShelf:
        shelf = self.compressor.compress(traces)
        with self._lock:
            self._shelves[shelf.shelf_id] = shelf
        return shelf

    def awaken(self, query: str, context_budget_tokens: int = 4096) -> List[Tuple[MemoryShelf, AwakeningScore]]:
        with self._lock:
            shelves = list(self._shelves.values())
        return self.awakener.awaken(shelves, query, context_budget_tokens)

    def get_shelves(self) -> List[MemoryShelf]:
        with self._lock:
            return list(self._shelves.values())

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "class": "MemoryAwakeningPipeline (CB74)",
                "formation": self.formation.statistics(),
                "compressor": self.compressor.statistics(),
                "awakener": self.awakener.statistics(),
                "total_shelves": len(self._shelves),
                "uptime_seconds": round(_time.time() - self._start_time, 3),
            }
