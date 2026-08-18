"""
# status: orphan (2026-08-15 audit, not in runtime path)
TemporalMemoryTree — TiMem Five-Level Temporal Containing Tree
===============================================================
ACL 2026 Findings (arXiv 2601.02845) · P42-2

实现 TiMem 五层时序包含树: session/day/week/month/profile 五层,
complexity_aware_retrieval 按问题复杂度自适应选择层级, temporal_consolidation
从底层事实逐层向上合并为语义摘要, llm_gating 检索后LLM过滤冗余/冲突。

设计要点:
  - FiveLevelTree: session ⊂ day ⊂ week ⊂ month ⊂ profile
  - ComplexityAwareRetrieval: simple/hybrid/complex 自适应
  - TemporalConsolidation: 底层事实→语义摘要逐层上卷
  - LLMGating: 检索后过滤冗余冲突, 按层级和时间距离排序
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TemporalLevel(Enum):
    """时序层级——从细到粗。"""
    SESSION = 1    # 会话级 (分钟~小时)
    DAY = 2        # 日级
    WEEK = 3       # 周级
    MONTH = 4      # 月级
    PROFILE = 5    # 用户画像 (跨月)


class RetrievalComplexity(Enum):
    """检索复杂度——决定检索层级。"""
    SIMPLE = auto()     # 仅 SESSION
    HYBRID = auto()     # SESSION + DAY
    COMPLEX = auto()    # 全层级 SESSION~PROFILE


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class TimeNode:
    """时序树节点——一段时序区间的记忆。"""
    node_id: str
    level: TemporalLevel
    start_time: float
    end_time: float
    facts: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    children: List[str] = field(default_factory=list)  # child node_ids
    parent_id: Optional[str] = None
    evidence_chain: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def duration(self) -> float:
        return self.end_time - self.start_time


@dataclass
class ConsolidationSummary:
    """从底层事实逐层向上合并的语义摘要。"""
    summary_id: str
    source_level: TemporalLevel
    target_level: TemporalLevel
    original_facts: int
    compressed_summary: str
    evidence_count: int = 0
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class RetrievalCandidate:
    """检索候选项——带层级和距离信息。"""
    node: TimeNode
    relevance_score: float = 0.0
    temporal_distance: float = 0.0  # 负值=更近
    from_level: TemporalLevel = TemporalLevel.SESSION


# ---------------------------------------------------------------------------
# FiveLevelTree
# ---------------------------------------------------------------------------

class FiveLevelTree:
    """五层时序包含树结构。

    父节点的时间区间严格覆盖所有子节点。

    Parameters
    ----------
    max_facts_per_node : int
        每个节点最大事实数。
    """

    def __init__(self, max_facts_per_node: int = 200) -> None:
        self.max_facts_per_node = max_facts_per_node
        self._nodes: Dict[str, TimeNode] = {}
        self._level_index: Dict[TemporalLevel, List[str]] = defaultdict(list)
        self._lock = threading.RLock()
        self._node_count: int = 0

    def _ensure_level_nodes_exist(self, timestamp: float) -> Dict[TemporalLevel, str]:
        """为给定时间戳创建/获取各层级节点。"""
        import datetime

        dt = datetime.datetime.fromtimestamp(timestamp)
        day_start = datetime.datetime(dt.year, dt.month, dt.day).timestamp()
        # 周一为周起点
        weekday = dt.weekday()
        week_start = day_start - weekday * 86400.0
        month_start = datetime.datetime(dt.year, dt.month, 1).timestamp()

        # Profile 永远唯一
        profile_key = "profile_root"
        if profile_key not in self._nodes:
            self._create_node(profile_key, TemporalLevel.PROFILE, 0, float("inf"))

        # Month
        month_key = f"month_{dt.year}_{dt.month:02d}"
        if month_key not in self._nodes:
            next_month = dt.month + 1
            next_year = dt.year
            if next_month > 12:
                next_month = 1
                next_year += 1
            month_end = datetime.datetime(next_year, next_month, 1).timestamp()
            self._create_node(month_key, TemporalLevel.MONTH, month_start, month_end, parent_id=profile_key)

        # Week
        week_key = f"week_{dt.year}_w{dt.isocalendar().week:02d}"
        if week_key not in self._nodes:
            week_end = week_start + 7 * 86400.0
            self._create_node(week_key, TemporalLevel.WEEK, week_start, week_end, parent_id=month_key)

        # Day
        day_key = f"day_{dt.year}_{dt.month:02d}_{dt.day:02d}"
        if day_key not in self._nodes:
            day_end = day_start + 86400.0
            self._create_node(day_key, TemporalLevel.DAY, day_start, day_end, parent_id=week_key)

        # Session
        session_key = f"session_{int(timestamp)}_{self._node_count}"
        self._create_node(session_key, TemporalLevel.SESSION, timestamp, timestamp + 3600.0, parent_id=day_key)

        return {
            TemporalLevel.SESSION: session_key,
            TemporalLevel.DAY: day_key,
            TemporalLevel.WEEK: week_key,
            TemporalLevel.MONTH: month_key,
            TemporalLevel.PROFILE: profile_key,
        }

    def _create_node(
        self,
        node_id: str,
        level: TemporalLevel,
        start: float,
        end: float,
        parent_id: Optional[str] = None,
    ) -> TimeNode:
        self._node_count += 1
        node = TimeNode(
            node_id=node_id,
            level=level,
            start_time=start,
            end_time=end,
            parent_id=parent_id,
        )
        self._nodes[node_id] = node
        self._level_index[level].append(node_id)
        if parent_id and parent_id in self._nodes:
            self._nodes[parent_id].children.append(node_id)
        return node

    def add_fact(self, fact: Dict[str, Any], timestamp: Optional[float] = None) -> TimeNode:
        """添加一条事实到合适的 session 节点。"""
        ts = timestamp or time.time()
        with self._lock:
            level_nodes = self._ensure_level_nodes_exist(ts)
            session = self._nodes[level_nodes[TemporalLevel.SESSION]]
            session.facts.append(fact)

            if len(session.facts) > self.max_facts_per_node:
                session.facts.pop(0)

            return session

    def get_node(self, node_id: str) -> Optional[TimeNode]:
        return self._nodes.get(node_id)

    def get_nodes_at_level(self, level: TemporalLevel) -> List[TimeNode]:
        return [self._nodes[nid] for nid in self._level_index[level] if nid in self._nodes]

    def get_ancestors(self, node_id: str) -> List[TimeNode]:
        """获取从当前节点到根的所有祖先。"""
        result = []
        current = self._nodes.get(node_id)
        while current and current.parent_id:
            parent = self._nodes.get(current.parent_id)
            if parent:
                result.append(parent)
                current = parent
            else:
                break
        return result

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_nodes": len(self._nodes),
            "by_level": {lv.name: len(ids) for lv, ids in self._level_index.items()},
        }


# ---------------------------------------------------------------------------
# ComplexityAwareRetrieval
# ---------------------------------------------------------------------------

class ComplexityAwareRetrieval:
    """根据问题复杂度自适应选择检索层级。

    Parameters
    ----------
    simple_threshold : int
        简单问题最大token数 (估算, 用于复杂度判定)。
    """

    def __init__(self, simple_threshold: int = 3) -> None:
        self.simple_threshold = simple_threshold

    def classify_complexity(self, query: str) -> RetrievalComplexity:
        """根据问题长度和复杂度关键词自适应分类。"""
        tokens = query.split()
        tlen = len(tokens)
        query_lower = query.lower()

        # 复杂关键词
        complex_kw = {"compare", "analyze", "synthesize", "all", "comprehensive", "history", "evolve"}
        hybrid_kw = {"recent", "weekly", "summarize", "trend", "pattern"}

        has_complex = any(kw in query_lower for kw in complex_kw)
        has_hybrid = any(kw in query_lower for kw in hybrid_kw)

        if has_complex or tlen > 20:
            return RetrievalComplexity.COMPLEX
        if has_hybrid or tlen > self.simple_threshold * 3:
            return RetrievalComplexity.HYBRID
        return RetrievalComplexity.SIMPLE

    def get_target_levels(self, complexity: RetrievalComplexity) -> List[TemporalLevel]:
        """根据复杂度返回检索层级。"""
        mapping = {
            RetrievalComplexity.SIMPLE: [TemporalLevel.SESSION],
            RetrievalComplexity.HYBRID: [TemporalLevel.SESSION, TemporalLevel.DAY, TemporalLevel.WEEK],
            RetrievalComplexity.COMPLEX: [TemporalLevel.SESSION, TemporalLevel.DAY, TemporalLevel.WEEK, TemporalLevel.MONTH, TemporalLevel.PROFILE],
        }
        return mapping.get(complexity, [TemporalLevel.SESSION])


# ---------------------------------------------------------------------------
# TemporalConsolidation
# ---------------------------------------------------------------------------

class TemporalConsolidation:
    """从底层事实逐层向上合并为语义摘要。

    Parameters
    ----------
    consolidation_interval : float
        合并触发间隔 (秒)。
    """

    def __init__(self, consolidation_interval: float = 3600.0) -> None:
        self.consolidation_interval = consolidation_interval
        self._last_consolidation: Dict[TemporalLevel, float] = {}
        self._summaries: List[ConsolidationSummary] = []
        self._lock = threading.RLock()
        self._summary_count: int = 0

    def consolidate(
        self, tree: FiveLevelTree, source_level: TemporalLevel, target_level: TemporalLevel
    ) -> Optional[ConsolidationSummary]:
        """从 source 层事实合并到 target 层摘要。

        从 source 层的所有节点收集事实, 生成 target 层的语义摘要。
        """
        with self._lock:
            now = time.time()
            last = self._last_consolidation.get(target_level, 0)
            if now - last < self.consolidation_interval:
                return None

            source_nodes = tree.get_nodes_at_level(source_level)
            if not source_nodes:
                return None

            # 收集所有事实
            all_facts: List[Dict[str, Any]] = []
            for node in source_nodes:
                all_facts.extend(node.facts)

            if not all_facts:
                return None

            # 生成简单摘要 (关键词频率统计)
            summary_parts: List[str] = []
            action_counts: Dict[str, int] = defaultdict(int)
            for fact in all_facts:
                action = str(fact.get("action", ""))
                if action:
                    action_counts[action] += 1

            top_actions = sorted(action_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            summary_parts.append(f"Top actions: " + ", ".join(f"{a}(x{c})" for a, c in top_actions))

            # 平均 reward
            rewards = [f.get("reward", 0.0) for f in all_facts if isinstance(f.get("reward"), (int, float))]
            if rewards:
                summary_parts.append(f"Avg reward: {np.mean(rewards):.2f}")

            self._summary_count += 1
            summary = ConsolidationSummary(
                summary_id=f"cons_{self._summary_count}_{int(now*1e6)}",
                source_level=source_level,
                target_level=target_level,
                original_facts=len(all_facts),
                compressed_summary=" | ".join(summary_parts),
                evidence_count=len(source_nodes),
                confidence=min(1.0, len(all_facts) / 50.0),
            )

            self._summaries.append(summary)
            self._last_consolidation[target_level] = now

            # 更新 target 层级节点的摘要
            target_nodes = tree.get_nodes_at_level(target_level)
            for node in target_nodes:
                node.summary = summary.compressed_summary

            return summary

    def statistics(self) -> Dict[str, Any]:
        return {"total_summaries": len(self._summaries)}


# ---------------------------------------------------------------------------
# LLMGating
# ---------------------------------------------------------------------------

class LLMGating:
    """检索后用过滤策略过滤冗余/冲突候选。

    Parameters
    ----------
    max_candidates : int
        最大返回候选数。
    redundancy_threshold : float
        相似度阈值以上视为冗余。
    """

    def __init__(self, max_candidates: int = 10, redundancy_threshold: float = 0.85) -> None:
        self.max_candidates = max_candidates
        self.redundancy_threshold = redundancy_threshold

    def gate(
        self, candidates: List[RetrievalCandidate]
    ) -> List[RetrievalCandidate]:
        """过滤冗余/冲突候选, 按层级和时间距离排序。

        Parameters
        ----------
        candidates : List[RetrievalCandidate]
            检索候选项。

        Returns
        -------
        List[RetrievalCandidate]
            过滤排序后的候选项。
        """
        if not candidates:
            return []

        # 1. 去冗余
        deduped: List[RetrievalCandidate] = []
        for cand in candidates:
            is_dup = False
            for kept in deduped:
                sim = _compute_fact_overlap(cand.node.facts, kept.node.facts)
                if sim >= self.redundancy_threshold:
                    is_dup = True
                    break
            if not is_dup:
                deduped.append(cand)

        # 2. 按层级 (细粒度优先) + 时间距离 (近优先) + 相关性排序
        deduped.sort(key=lambda c: (
            c.from_level.value,       # SESSION=1 优先
            c.temporal_distance,      # 时间近优先
            -c.relevance_score,       # 相关性高优先
        ))

        # 3. 限制数量
        return deduped[:self.max_candidates]


# ---------------------------------------------------------------------------
# TemporalMemoryTree
# ---------------------------------------------------------------------------

class TemporalMemoryTree:
    """TiMem 五层时序包含树记忆系统。

    Parameters
    ----------
    max_facts_per_node : int
        每个节点最大事实数。
    consolidation_interval : float
        合并触发间隔 (秒)。
    max_candidates : int
        LLM Gating 最大返回候选。
    """

    def __init__(
        self,
        max_facts_per_node: int = 200,
        consolidation_interval: float = 3600.0,
        max_candidates: int = 10,
    ) -> None:
        self.five_level_tree = FiveLevelTree(max_facts_per_node=max_facts_per_node)
        self.complexity_aware_retrieval = ComplexityAwareRetrieval()
        self.temporal_consolidation = TemporalConsolidation(
            consolidation_interval=consolidation_interval,
        )
        self.llm_gating = LLMGating(max_candidates=max_candidates)
        self._lock = threading.RLock()

        logger.info(
            "TemporalMemoryTree initialized [facts=%d cons=%.0fh gate=%d]",
            max_facts_per_node, consolidation_interval / 3600, max_candidates,
        )

    def add_fact(self, fact: Dict[str, Any], timestamp: Optional[float] = None) -> TimeNode:
        """添加一条事实到树中。"""
        return self.five_level_tree.add_fact(fact, timestamp)

    def retrieve(self, query: str, timestamp: Optional[float] = None) -> List[RetrievalCandidate]:
        """复杂度感知检索——返回LLM过滤后的候选。

        Parameters
        ----------
        query : str
            检索查询。
        timestamp : Optional[float]
            参考时间点。

        Returns
        -------
        List[RetrievalCandidate]
            过滤排序后的候选项。
        """
        ts = timestamp or time.time()
        complexity = self.complexity_aware_retrieval.classify_complexity(query)
        levels = self.complexity_aware_retrieval.get_target_levels(complexity)

        candidates: List[RetrievalCandidate] = []
        for level in levels:
            nodes = self.five_level_tree.get_nodes_at_level(level)
            for node in nodes:
                # 相关性简单词频匹配
                score = _compute_relevance(query, node)
                if score > 0:
                    candidates.append(RetrievalCandidate(
                        node=node,
                        relevance_score=score,
                        temporal_distance=ts - node.start_time,
                        from_level=level,
                    ))

        # 触发合并
        if complexity == RetrievalComplexity.COMPLEX:
            self.temporal_consolidation.consolidate(
                self.five_level_tree, TemporalLevel.SESSION, TemporalLevel.DAY,
            )
            self.temporal_consolidation.consolidate(
                self.five_level_tree, TemporalLevel.DAY, TemporalLevel.WEEK,
            )

        return self.llm_gating.gate(candidates)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "tree": self.five_level_tree.statistics(),
                "consolidation_summaries": self.temporal_consolidation.statistics()["total_summaries"],
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_relevance(query: str, node: TimeNode) -> float:
    """简单词频相关性。"""
    q_words = set(query.lower().split())
    if not q_words:
        return 0.0

    matches = 0
    for fact in node.facts:
        fact_str = str(fact).lower()
        for w in q_words:
            if w in fact_str:
                matches += 1

    return min(matches / max(len(q_words), 1), 1.0)


def _compute_fact_overlap(
    facts_a: List[Dict[str, Any]], facts_b: List[Dict[str, Any]]
) -> float:
    """计算两组事实的重叠度。"""
    if not facts_a or not facts_b:
        return 0.0

    str_a = {str(f) for f in facts_a}
    str_b = {str(f) for f in facts_b}

    intersection = len(str_a & str_b)
    union = len(str_a | str_b)
    return intersection / union if union > 0 else 0.0
