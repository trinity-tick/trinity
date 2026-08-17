"""
# status: orphan (2026-08-15 audit, not in runtime path)
CWL — Context Window Lifecycle Management for Agent Episodes
=============================================================
arXiv 2606.11213 · P46-3

为 agent 轨迹打类型标注并构建 episode 依赖图。确定性 LLM-free 淘汰策略
按优先级 (已持久化 < 孤立 < 当前活跃 < 用户轮次) 淘汰超出预算的 episode。

设计要点:
  - CWLEpisodeAnnotator: 轨迹类型标注 + episode 依赖图构建
  - EpisodeDependencyGraph: 依赖关系与持久化副作用追踪
  - StructuredEvictionPolicy: 确定性 LLM-free 四级优先级淘汰
  - CWLBudgetTracker: token 预算追踪与触发淘汰
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EpisodeType(Enum):
    REASONING = auto()
    TOOL_CALL = auto()
    OBSERVATION = auto()
    USER_TURN = auto()
    SYSTEM = auto()


class EvictionPriority(Enum):
    """淘汰优先级 (数值越小越先被淘汰)。"""
    PERSISTED = 0       # 已持久化副作用 — 最先淘汰
    ISOLATED = 1        # 孤立无依赖
    ACTIVE = 2          # 当前活跃链
    USER_TURN = 3       # 用户轮次 — 最后淘汰


class EpisodeStatus(Enum):
    ACTIVE = auto()
    PERSISTED = auto()   # 副作用已持久化
    EVICTED = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class TypedEpisode:
    """带类型标注的 episode。"""
    episode_id: str
    episode_type: EpisodeType
    content: str = ""
    token_count: int = 0
    timestamp: float = field(default_factory=time.time)
    status: EpisodeStatus = EpisodeStatus.ACTIVE
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EpisodeEdge:
    """Episode 间依赖边。"""
    source_id: str
    target_id: str
    relation: str = "depends_on"  # depends_on / triggers / persists
    persistent: bool = False       # 副作用是否已持久化


# ---------------------------------------------------------------------------
# CWLEpisodeAnnotator
# ---------------------------------------------------------------------------

class CWLEpisodeAnnotator:
    """Episode 标注器——为 agent 轨迹打类型标注并构建依赖图。"""

    def __init__(self) -> None:
        self._episodes: Dict[str, TypedEpisode] = {}
        self._edges: List[EpisodeEdge] = []
        self._last_episode_id: Optional[str] = None
        self._lock = threading.RLock()

    def annotate(
        self, content: str, episode_type: EpisodeType, token_count: int = 0,
    ) -> TypedEpisode:
        """标注并注册新 episode, 自动与前一个 episode 建立依赖边。"""
        with self._lock:
            eid = f"ep_{len(self._episodes)}_{int(time.time()*1e6)}"
            ep = TypedEpisode(
                episode_id=eid, episode_type=episode_type,
                content=content[:200], token_count=token_count,
            )
            self._episodes[eid] = ep

            # 与前一个 episode 建立依赖
            if self._last_episode_id and self._last_episode_id in self._episodes:
                prev = self._episodes[self._last_episode_id]
                edge = EpisodeEdge(
                    source_id=self._last_episode_id, target_id=eid,
                    relation="triggers" if ep.episode_type == EpisodeType.TOOL_CALL else "depends_on",
                )
                self._edges.append(edge)

            self._last_episode_id = eid
            return ep

    def mark_persisted(self, episode_id: str) -> None:
        """标记 episode 副作用已持久化。"""
        with self._lock:
            if episode_id in self._episodes:
                self._episodes[episode_id].status = EpisodeStatus.PERSISTED
            for edge in self._edges:
                if edge.source_id == episode_id:
                    edge.persistent = True

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_episodes": len(self._episodes),
            "total_edges": len(self._edges),
        }


# ---------------------------------------------------------------------------
# EpisodeDependencyGraph
# ---------------------------------------------------------------------------

class EpisodeDependencyGraph:
    """Episode 依赖图——追踪 episode 间依赖关系与持久化状态。"""

    def __init__(self) -> None:
        self._adj_out: Dict[str, List[str]] = {}
        self._adj_in: Dict[str, List[str]] = {}
        self._persisted: Set[str] = set()
        self._lock = threading.RLock()

    def add_edge(self, source_id: str, target_id: str, persistent: bool = False) -> None:
        with self._lock:
            self._adj_out.setdefault(source_id, []).append(target_id)
            self._adj_in.setdefault(target_id, []).append(source_id)
            if persistent:
                self._persisted.add(source_id)

    def is_isolated(self, episode_id: str) -> bool:
        """判断 episode 是否孤立 (无入边且无出边中活跃依赖)。"""
        in_edges = self._adj_in.get(episode_id, [])
        out_edges = self._adj_out.get(episode_id, [])
        return len(in_edges) == 0 and len(out_edges) == 0

    def get_dependents(self, episode_id: str) -> List[str]:
        return self._adj_out.get(episode_id, [])

    def get_dependencies(self, episode_id: str) -> List[str]:
        return self._adj_in.get(episode_id, [])

    def statistics(self) -> Dict[str, Any]:
        return {
            "nodes": len(set(list(self._adj_out) + list(self._adj_in))),
            "edges": sum(len(v) for v in self._adj_out.values()),
            "persisted": len(self._persisted),
        }


# ---------------------------------------------------------------------------
# StructuredEvictionPolicy
# ---------------------------------------------------------------------------

class StructuredEvictionPolicy:
    """确定性 LLM-free 淘汰策略——四级优先级:

    Priority (低→高): PERSISTED < ISOLATED < ACTIVE < USER_TURN
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def compute_priority(
        self, episode: TypedEpisode, dep_graph: EpisodeDependencyGraph,
    ) -> EvictionPriority:
        """计算 episode 淘汰优先级。"""
        with self._lock:
            if episode.episode_type == EpisodeType.USER_TURN:
                return EvictionPriority.USER_TURN
            if episode.status == EpisodeStatus.PERSISTED:
                return EvictionPriority.PERSISTED
            if dep_graph.is_isolated(episode.episode_id):
                return EvictionPriority.ISOLATED
            return EvictionPriority.ACTIVE

    def select_victims(
        self, episodes: Dict[str, TypedEpisode], dep_graph: EpisodeDependencyGraph,
        target_tokens: int,
    ) -> List[str]:
        """选择被淘汰的 episode ID 列表, 直到释放 target_tokens 为止。"""
        with self._lock:
            # 按优先级排序 (低优先先淘汰)
            scored: List[Tuple[int, str, int]] = []
            for eid, ep in episodes.items():
                if ep.status == EpisodeStatus.EVICTED:
                    continue
                priority = self.compute_priority(ep, dep_graph)
                scored.append((priority.value, eid, ep.token_count))

            # 按优先级升序, 再按时间戳升序 (旧的先淘汰)
            scored.sort(key=lambda x: (x[0], episodes[x[1]].timestamp))

            freed = 0
            victims: List[str] = []
            for _, eid, tokens in scored:
                if freed >= target_tokens:
                    break
                victims.append(eid)
                freed += tokens

            return victims

    def statistics(self) -> Dict[str, Any]:
        return {"priority_levels": [p.name for p in EvictionPriority]}


# ---------------------------------------------------------------------------
# CWLBudgetTracker
# ---------------------------------------------------------------------------

class CWLBudgetTracker:
    """Token 预算追踪器——超出上限时触发淘汰。

    Parameters
    ----------
    token_budget : int
        总 token 预算上限。
    """

    def __init__(self, token_budget: int = 128000) -> None:
        self.token_budget = token_budget
        self._used: int = 0
        self._evicted_total: int = 0
        self._lock = threading.RLock()

    def consume(self, tokens: int) -> bool:
        """消耗 token, 返回是否在预算内。"""
        with self._lock:
            self._used += tokens
            return self._used <= self.token_budget

    def reclaim(self, tokens: int) -> None:
        """回收 token 配额。"""
        with self._lock:
            self._used = max(0, self._used - tokens)
            self._evicted_total += tokens

    def over_budget(self) -> bool:
        return self._used > self.token_budget

    def usage_ratio(self) -> float:
        if self.token_budget == 0:
            return 1.0
        return self._used / self.token_budget

    def statistics(self) -> Dict[str, Any]:
        return {
            "budget": self.token_budget,
            "used": self._used,
            "ratio": round(self.usage_ratio(), 4),
            "evicted_total": self._evicted_total,
        }
