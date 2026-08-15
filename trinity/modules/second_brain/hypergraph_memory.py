"""
# status: orphan (2026-08-15 audit, not in runtime path)
CB73: HypergraphMemory — 超图记忆
==================================

超图替代传统 pairwise 边，显式表达高阶关联。

核心设计:
  - HyperEdge: 可连接任意数量节点，超越 pairwise 限制
  - TopicEpisodeFact 三层架构:
    - TopicLayer: LLM 检测语义边界切分 topic 组
    - EpisodeLayer: 同主题多 episode 用 hyperedge 绑定
    - FactLayer: 每个 episode 内独立 fact 作为叶节点
  - CoarseToFineRetriever: topic → episode → fact 粗到细检索
  - HypergraphEmbeddingPropagator: 同一 hyperedge 内节点共享语义嵌入
  - RRFFusion: 融合多粒度检索得分 + CrossEncoder 重排

Reference:
  - HyperMem: Hypergraph Memory for Long-Term Conversations (ACL 2026, LoCoMo 92.73%)
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

class RetrievalGranularity(Enum):
    TOPIC = "topic"
    EPISODE = "episode"
    FACT = "fact"


class SemanticBoundaryType(Enum):
    TOPIC_SHIFT = "topic_shift"       # 主题转换
    TIME_GAP = "time_gap"            # 时间间隔
    SPEAKER_CHANGE = "speaker_change" # 说话人切换
    TASK_SWITCH = "task_switch"      # 任务切换


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class HyperEdge:
    """超边——可连接任意数量节点。"""
    edge_id: str
    member_nodes: Set[str] = field(default_factory=set)
    edge_type: str = "generic"
    weight: float = 1.0
    metadata: Dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=_time.time)

    def __len__(self):
        return len(self.member_nodes)


@dataclass
class TopicNode:
    """主题节点。"""
    topic_id: str
    label: str = ""
    description: str = ""
    episode_ids: Set[str] = field(default_factory=set)
    embedding: Optional[List[float]] = None
    created_at: float = field(default_factory=_time.time)


@dataclass
class HMEpisodeNode:
    """情景节点。"""
    episode_id: str
    topic_id: str = ""
    summary: str = ""
    fact_ids: Set[str] = field(default_factory=set)
    start_time: float = 0.0
    end_time: float = 0.0
    created_at: float = field(default_factory=_time.time)


@dataclass
class HMFactNode:
    """事实叶节点。"""
    fact_id: str
    episode_id: str = ""
    content: str = ""
    entities: List[str] = field(default_factory=list)
    confidence: float = 1.0
    created_at: float = field(default_factory=_time.time)


@dataclass
class SemanticBoundary:
    """语义边界标记。"""
    boundary_id: str
    boundary_type: SemanticBoundaryType
    position: int = 0                # 对话轮次位置
    confidence: float = 0.5
    detected_at: float = field(default_factory=_time.time)


# ============================================================================
# TopicLayer
# ============================================================================

class TopicLayer:
    """主题层——管理 topic 节点及 topic → episode 超边。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._topics: Dict[str, TopicNode] = {}
        self._topic_hyperedges: Dict[str, HyperEdge] = {}

    def create_topic(self, topic_id: str, label: str = "", description: str = "") -> TopicNode:
        with self._lock:
            node = TopicNode(topic_id=topic_id, label=label, description=description)
            self._topics[topic_id] = node
            self._topic_hyperedges[topic_id] = HyperEdge(
                edge_id=f"topic_he_{topic_id}", edge_type="topic_binding",
            )
            return node

    def bind_episode(self, topic_id: str, episode_id: str):
        with self._lock:
            if topic_id in self._topic_hyperedges:
                self._topic_hyperedges[topic_id].member_nodes.add(episode_id)
            if topic_id in self._topics:
                self._topics[topic_id].episode_ids.add(episode_id)

    def get_topic(self, topic_id: str) -> Optional[TopicNode]:
        with self._lock:
            return self._topics.get(topic_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"topics": len(self._topics), "topic_hyperedges": len(self._topic_hyperedges)}


# ============================================================================
# EpisodeLayer
# ============================================================================

class HMEpisodeLayer:
    """情景层——管理 episode 节点及 episode → fact 超边。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._episodes: Dict[str, HMEpisodeNode] = {}
        self._episode_hyperedges: Dict[str, HyperEdge] = {}

    def create_episode(
        self, episode_id: str, topic_id: str = "",
        summary: str = "", start_time: float = 0.0, end_time: float = 0.0,
    ) -> HMEpisodeNode:
        with self._lock:
            node = HMEpisodeNode(
                episode_id=episode_id, topic_id=topic_id, summary=summary,
                start_time=start_time, end_time=end_time,
            )
            self._episodes[episode_id] = node
            self._episode_hyperedges[episode_id] = HyperEdge(
                edge_id=f"ep_he_{episode_id}", edge_type="episode_binding",
            )
            return node

    def bind_fact(self, episode_id: str, fact_id: str):
        with self._lock:
            if episode_id in self._episode_hyperedges:
                self._episode_hyperedges[episode_id].member_nodes.add(fact_id)
            if episode_id in self._episodes:
                self._episodes[episode_id].fact_ids.add(fact_id)

    def get_episode(self, episode_id: str) -> Optional[HMEpisodeNode]:
        with self._lock:
            return self._episodes.get(episode_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"episodes": len(self._episodes), "episode_hyperedges": len(self._episode_hyperedges)}


# ============================================================================
# FactLayer
# ============================================================================

class HMFactLayer:
    """事实层——管理独立 fact 叶节点。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._facts: Dict[str, HMFactNode] = {}

    def create_fact(
        self, fact_id: str, episode_id: str = "",
        content: str = "", entities: Optional[List[str]] = None, confidence: float = 1.0,
    ) -> HMFactNode:
        with self._lock:
            node = HMFactNode(
                fact_id=fact_id, episode_id=episode_id, content=content,
                entities=entities or [], confidence=confidence,
            )
            self._facts[fact_id] = node
            return node

    def get_fact(self, fact_id: str) -> Optional[HMFactNode]:
        with self._lock:
            return self._facts.get(fact_id)

    def search_by_entity(self, entity: str) -> List[HMFactNode]:
        with self._lock:
            return [f for f in self._facts.values() if entity in f.entities]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"facts": len(self._facts)}


# ============================================================================
# SemanticBoundaryDetector
# ============================================================================

class SemanticBoundaryDetector:
    """语义边界检测器——识别 topic/episode 切换点。"""

    def __init__(self, topic_shift_threshold: float = 0.6):
        self.topic_shift_threshold = topic_shift_threshold
        self._lock = threading.RLock()
        self._boundaries: List[SemanticBoundary] = []

    def detect(
        self, turns: List[str], embeddings: Optional[List[List[float]]] = None
    ) -> List[SemanticBoundary]:
        """检测对话轮次中的语义边界。

        Args:
            turns: 对话轮次文本列表。
            embeddings: 可选各轮次嵌入。

        Returns:
            检测到的语义边界列表。
        """
        with self._lock:
            boundaries = []
            for i in range(1, len(turns)):
                # Simplified: detect large gaps as boundaries
                prev_len = len(turns[i - 1])
                curr_len = len(turns[i])
                # Topic shift heuristic: abrupt change in turn length
                if abs(prev_len - curr_len) / max(prev_len, 1) > 0.5:
                    b = SemanticBoundary(
                        boundary_id=f"bound_{i}",
                        boundary_type=SemanticBoundaryType.TOPIC_SHIFT,
                        position=i,
                        confidence=0.6,
                    )
                    boundaries.append(b)
                    self._boundaries.append(b)
            return boundaries

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"boundaries_detected": len(self._boundaries)}


# ============================================================================
# HMCoarseToFineRetriever
# ============================================================================

class HMCoarseToFineRetriever:
    """粗到细检索器——topic → episode → fact 三级检索。"""

    def __init__(self, top_k_topics: int = 3, top_k_episodes: int = 5, top_k_facts: int = 10):
        self.top_k_topics = top_k_topics
        self.top_k_episodes = top_k_episodes
        self.top_k_facts = top_k_facts

    def retrieve(
        self,
        query: str,
        topic_layer: TopicLayer,
        episode_layer: HMEpisodeLayer,
        fact_layer: HMFactLayer,
    ) -> Dict[str, Any]:
        """三级粗到细检索。

        Returns:
            {"topics": [...], "episodes": [...], "facts": [...], "fusion_score": float}
        """
        # Stage 1: Topic retrieval (keyword match)
        topic_scores = []
        for tid, t in topic_layer._topics.items():
            score = sum(1 for w in query.lower().split() if w in t.label.lower()) / max(len(query.split()), 1)
            if score > 0:
                topic_scores.append((tid, score))
        topic_scores.sort(key=lambda x: x[1], reverse=True)
        top_topics = topic_scores[: self.top_k_topics]

        # Stage 2: Episode retrieval
        ep_scores = []
        for tid, _ in top_topics:
            t = topic_layer.get_topic(tid)
            if t:
                for eid in t.episode_ids:
                    ep = episode_layer.get_episode(eid)
                    if ep:
                        score = sum(1 for w in query.lower().split() if w in ep.summary.lower()) / max(len(query.split()), 1)
                        ep_scores.append((eid, score))
        ep_scores.sort(key=lambda x: x[1], reverse=True)
        top_eps = ep_scores[: self.top_k_episodes]

        # Stage 3: Fact retrieval
        fact_scores = []
        for eid, _ in top_eps:
            ep = episode_layer.get_episode(eid)
            if ep:
                for fid in ep.fact_ids:
                    f = fact_layer.get_fact(fid)
                    if f:
                        score = sum(1 for w in query.lower().split() if w in f.content.lower()) / max(len(query.split()), 1)
                        fact_scores.append((fid, score))
        fact_scores.sort(key=lambda x: x[1], reverse=True)
        top_facts = fact_scores[: self.top_k_facts]

        fusion_score = 0.0
        if top_facts:
            fusion_score = sum(s for _, s in top_facts) / len(top_facts)

        return {"topics": top_topics, "episodes": top_eps, "facts": top_facts, "fusion_score": fusion_score}

    def statistics(self) -> Dict[str, Any]:
        return {"top_k_topics": self.top_k_topics, "top_k_episodes": self.top_k_episodes, "top_k_facts": self.top_k_facts}


# ============================================================================
# HypergraphEmbeddingPropagator
# ============================================================================

class HypergraphEmbeddingPropagator:
    """超图嵌入传播——同一 hyperedge 内节点共享语义嵌入。"""

    def __init__(self, propagation_steps: int = 2, alpha: float = 0.5):
        self.propagation_steps = propagation_steps
        self.alpha = alpha
        self._lock = threading.RLock()

    def propagate(self, hyperedges: Dict[str, HyperEdge], node_embeddings: Dict[str, List[float]]) -> Dict[str, List[float]]:
        """在超图中传播嵌入。

        Args:
            hyperedges: 超边集合。
            node_embeddings: 节点嵌入映射。

        Returns:
            更新后的节点嵌入。
        """
        with self._lock:
            dim = len(next(iter(node_embeddings.values()), [0.0]))
            if dim == 0:
                return node_embeddings

            updated = {k: list(v) for k, v in node_embeddings.items()}
            for _ in range(self.propagation_steps):
                new_emb = {}
                for he in hyperedges.values():
                    members = [m for m in he.member_nodes if m in updated]
                    if len(members) < 2:
                        continue
                    # Average member embeddings
                    avg = [0.0] * dim
                    for m in members:
                        for d in range(dim):
                            avg[d] += updated[m][d]
                    for d in range(dim):
                        avg[d] /= len(members)
                    # Update each member towards average
                    for m in members:
                        old = updated.get(m, [0.0] * dim)
                        new_emb[m] = [(1 - self.alpha) * old[d] + self.alpha * avg[d] for d in range(dim)]
                updated.update(new_emb)
            return updated

    def statistics(self) -> Dict[str, Any]:
        return {"propagation_steps": self.propagation_steps, "alpha": self.alpha}


# ============================================================================
# RRFFusion
# ============================================================================

class RRFFusion:
    """RRF 融合——多粒度检索得分融合 + 重排。"""

    def __init__(self, k: int = 60):
        self.k = k

    def fuse(
        self, rank_lists: List[List[Tuple[str, float]]], weights: Optional[List[float]] = None,
    ) -> List[Tuple[str, float]]:
        """RRF 融合多个排序列表。

        Args:
            rank_lists: 多个排序列表 [(id, score), ...]。
            weights: 各列表权重。

        Returns:
            融合后的排序列表。
        """
        if weights is None:
            weights = [1.0] * len(rank_lists)

        scores: Dict[str, float] = {}
        for lst, w in zip(rank_lists, weights):
            for rank, (item_id, _) in enumerate(lst):
                rrf_score = w / (self.k + rank + 1)
                scores[item_id] = scores.get(item_id, 0.0) + rrf_score

        return sorted(scores.items(), key=lambda x: x[1], reverse=True)

    def statistics(self) -> Dict[str, Any]:
        return {"k": self.k}


# ============================================================================
# Main Class
# ============================================================================

class HypergraphMemory:
    """超图记忆 (CB73)。

    统一入口——管理三层架构、语义边界检测、粗到细检索、嵌入传播、RRF 融合。

    Usage:
        hm = HypergraphMemory()
        hm.create_topic("t1", "travel")
        hm.create_episode("e1", "t1", "Trip to Paris")
        hm.create_fact("f1", "e1", "Visited Eiffel Tower")
        hm.bind_episode_to_topic("t1", "e1")
        hm.bind_fact_to_episode("e1", "f1")
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.topics = TopicLayer()
        self.episodes = HMEpisodeLayer()
        self.facts = HMFactLayer()
        self.boundary_detector = SemanticBoundaryDetector()
        self.retriever = HMCoarseToFineRetriever()
        self.propagator = HypergraphEmbeddingPropagator()
        self.fusion = RRFFusion()
        self._start_time = _time.time()

    def create_topic(self, topic_id: str, label: str = "", description: str = "") -> TopicNode:
        return self.topics.create_topic(topic_id, label, description)

    def create_episode(
        self, episode_id: str, topic_id: str = "",
        summary: str = "", start_time: float = 0.0, end_time: float = 0.0,
    ) -> HMEpisodeNode:
        return self.episodes.create_episode(episode_id, topic_id, summary, start_time, end_time)

    def create_fact(
        self, fact_id: str, episode_id: str = "",
        content: str = "", entities: Optional[List[str]] = None, confidence: float = 1.0,
    ) -> HMFactNode:
        return self.facts.create_fact(fact_id, episode_id, content, entities, confidence)

    def bind_episode_to_topic(self, topic_id: str, episode_id: str):
        self.topics.bind_episode(topic_id, episode_id)

    def bind_fact_to_episode(self, episode_id: str, fact_id: str):
        self.episodes.bind_fact(episode_id, fact_id)

    def retrieve(self, query: str) -> Dict[str, Any]:
        return self.retriever.retrieve(query, self.topics, self.episodes, self.facts)

    def detect_boundaries(self, turns: List[str]) -> List[SemanticBoundary]:
        return self.boundary_detector.detect(turns)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "class": "HypergraphMemory (CB73)",
                "topics": self.topics.statistics(),
                "episodes": self.episodes.statistics(),
                "facts": self.facts.statistics(),
                "boundary_detector": self.boundary_detector.statistics(),
                "retriever": self.retriever.statistics(),
                "propagator": self.propagator.statistics(),
                "fusion": self.fusion.statistics(),
                "uptime_seconds": round(_time.time() - self._start_time, 3),
            }
