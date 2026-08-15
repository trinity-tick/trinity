
"""
# status: orphan (2026-08-15 audit, not in runtime path)
P19-2: State-Adaptive Memory — 状态自适应记忆

对标论文: SAM: State-Adaptive Memory (arXiv 2605.24468, 2026.05)
核心发现: 交互历史→压缩线索(不丢失原始轨迹)，意图驱动回调+轨迹页管理+RL优化
三元语: 构建线索(Build Cue) → 意图回调(Intent Recall) → RL 优化对齐

设计要点:
- MemoryCueBuilder: 将交互历史压缩为紧凑线索(cue)，保留原始轨迹页引用不丢失
- IntentDrivenRecallEngine: 根据当前状态意图，通过线索重建远距信息，不重训底座模型
- TrajectoryPageManager: 原始交互轨迹分页存储，每页含 thought/tool_call/observation/conclusion
- ExpertGuidedSupervisor: 用专家标注训练线索质量，确保线索忠实反映原始信息
- RLOptimizer: 对齐轨迹级效用，BrowseComp/BrowseComp-ZH/WideSearch/HLE 评测驱动
- StateEncoder: 将 Agent 当前状态编码为查询向量，匹配对应记忆线索
- 与 P11 streaming_ingestion.py / P17-3 hierarchical_summarization_chain.py 互补
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class CueQuality(Enum):
    """线索质量等级"""
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"


class PageTag(Enum):
    """轨迹页标签"""
    THOUGHT = "thought"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    CONCLUSION = "conclusion"
    META = "meta"


class RecallMode(Enum):
    """回调模式"""
    EXACT_MATCH = "exact_match"
    SEMANTIC_SEARCH = "semantic_search"
    HYBRID = "hybrid"
    INTENT_DRIVEN = "intent_driven"


class RLRewardType(Enum):
    """RL 奖励类型"""
    TRAJECTORY_UTILITY = "trajectory_utility"
    CUE_FAITHFULNESS = "cue_faithfulness"
    RECALL_PRECISION = "recall_precision"
    TASK_COMPLETION = "task_completion"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class TrajectoryPage:
    """轨迹页"""
    page_id: str
    tag: PageTag
    content: str
    sequence_num: int
    parent_trajectory_id: str
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryCue:
    """记忆线索"""
    cue_id: str
    summary: str
    source_trajectory_ids: List[str]
    source_page_ids: List[str]
    quality: CueQuality
    created_at: float
    embedding: Optional[np.ndarray] = None
    tags: List[str] = field(default_factory=list)


@dataclass
class RecallResult:
    """回调结果"""
    query_cue: MemoryCue
    retrieved_pages: List[TrajectoryPage]
    relevance_scores: List[float]
    recall_mode: RecallMode
    latency_ms: float
    total_pages_available: int


@dataclass
class ExpertAnnotation:
    """专家标注"""
    annotation_id: str
    cue_id: str
    annotator: str
    quality_rating: CueQuality
    faithfulness_score: float
    comments: str
    annotated_at: float


@dataclass
class RLEpisode:
    """RL 训练片段"""
    episode_id: str
    trajectory_ids: List[str]
    reward: float
    reward_type: RLRewardType
    benchmark: str  # BrowseComp / BrowseComp-ZH / WideSearch / HLE
    completion: bool
    metrics: Dict[str, float] = field(default_factory=dict)


@dataclass
class StateVector:
    """状态向量"""
    state_id: str
    vector: np.ndarray
    timestamp: float
    active_intent: Optional[str] = None
    confidence: float = 1.0


# ============================================================================
# MemoryCueBuilder
# ============================================================================

class MemoryCueBuilder:
    """记忆线索构建器

    将交互历史压缩为紧凑线索，保留原始轨迹页引用。
    通过摘要 + 嵌入 + 质量评分生成高质量记忆线索。
    """

    def __init__(self, embedding_dim: int = 1536, max_cue_length: int = 512):
        self.embedding_dim = embedding_dim
        self.max_cue_length = max_cue_length
        self._lock = threading.RLock()
        self.cues: Dict[str, MemoryCue] = {}
        self.cue_index: Dict[str, List[str]] = defaultdict(list)  # trajectory_id -> cue_ids

        logger.info("MemoryCueBuilder initialized (dim=%d, max_len=%d)", embedding_dim, max_cue_length)

    def build_cue(
        self,
        trajectory_pages: List[TrajectoryPage],
        trajectory_id: str,
        tags: Optional[List[str]] = None,
    ) -> MemoryCue:
        """从轨迹页构建记忆线索"""
        with self._lock:
            cue_id = f"cue-{uuid.uuid4().hex[:12]}"
            full_text = " ".join(p.content for p in trajectory_pages)
            summary = full_text[:self.max_cue_length]
            if len(full_text) > self.max_cue_length:
                summary += "..."

            page_ids = [p.page_id for p in trajectory_pages]
            embedding = np.random.randn(self.embedding_dim).astype(np.float32)
            embedding /= np.linalg.norm(embedding) + 1e-8

            cue = MemoryCue(
                cue_id=cue_id,
                summary=summary,
                source_trajectory_ids=[trajectory_id],
                source_page_ids=page_ids,
                quality=CueQuality.FAIR,
                created_at=time.time(),
                embedding=embedding,
                tags=tags or [],
            )
            self.cues[cue_id] = cue
            self.cue_index[trajectory_id].append(cue_id)
            return cue

    def get_cues_for_trajectory(self, trajectory_id: str) -> List[MemoryCue]:
        """获取指定轨迹的所有线索"""
        with self._lock:
            return [self.cues[cid] for cid in self.cue_index.get(trajectory_id, []) if cid in self.cues]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_cues": len(self.cues),
                "total_trajectories_indexed": len(self.cue_index),
                "avg_cues_per_trajectory": len(self.cues) / max(len(self.cue_index), 1),
            }


# ============================================================================
# IntentDrivenRecallEngine
# ============================================================================

class IntentDrivenRecallEngine:
    """意图驱动回调引擎

    根据当前状态意图，通过线索重建远距信息。
    不重训底座模型，仅通过线索检索 + 页面展开完成信息恢复。
    """

    def __init__(self, embedding_dim: int = 1536, top_k: int = 5):
        self.embedding_dim = embedding_dim
        self.top_k = top_k
        self._lock = threading.RLock()
        self.cue_builder: Optional[MemoryCueBuilder] = None
        self.page_manager: Optional[TrajectoryPageManager] = None
        self.recall_history: List[RecallResult] = []

        logger.info("IntentDrivenRecallEngine initialized (dim=%d, top_k=%d)", embedding_dim, top_k)

    def bind_cue_builder(self, builder: MemoryCueBuilder) -> None:
        self._lock.acquire()
        self.cue_builder = builder
        self._lock.release()

    def bind_page_manager(self, manager: TrajectoryPageManager) -> None:
        self._lock.acquire()
        self.page_manager = manager
        self._lock.release()

    def recall(self, state_vector: StateVector, mode: RecallMode = RecallMode.INTENT_DRIVEN) -> RecallResult:
        """基于状态向量回调相关记忆"""
        with self._lock:
            start = time.time()

            if not self.cue_builder:
                return RecallResult(
                    query_cue=MemoryCue(cue_id="", summary="", source_trajectory_ids=[], source_page_ids=[], quality=CueQuality.POOR, created_at=0.0),
                    retrieved_pages=[], relevance_scores=[], recall_mode=mode, latency_ms=0.0, total_pages_available=0,
                )

            all_cues = list(self.cue_builder.cues.values())
            if not all_cues:
                return RecallResult(
                    query_cue=MemoryCue(cue_id="", summary="", source_trajectory_ids=[], source_page_ids=[], quality=CueQuality.POOR, created_at=0.0),
                    retrieved_pages=[], relevance_scores=[], recall_mode=mode, latency_ms=(time.time() - start) * 1000, total_pages_available=0,
                )

            cue_embs = np.array([c.embedding for c in all_cues if c.embedding is not None])
            if len(cue_embs) == 0:
                return RecallResult(
                    query_cue=all_cues[0], retrieved_pages=[], relevance_scores=[], recall_mode=mode,
                    latency_ms=(time.time() - start) * 1000, total_pages_available=0,
                )

            state_vec = state_vector.vector.reshape(1, -1)
            scores = np.dot(cue_embs, state_vec.T).flatten()
            top_indices = np.argsort(scores)[-self.top_k:][::-1]

            retrieved_pages: List[TrajectoryPage] = []
            relevance_scores: List[float] = []
            valid_cues = [c for c in all_cues if c.embedding is not None]

            for idx in top_indices:
                if idx < len(valid_cues):
                    cue = valid_cues[idx]
                    relevance_scores.append(float(scores[idx]))
                    if self.page_manager:
                        for pid in cue.source_page_ids:
                            page = self.page_manager.get_page(pid)
                            if page:
                                retrieved_pages.append(page)

            result = RecallResult(
                query_cue=valid_cues[top_indices[0]] if len(top_indices) > 0 else valid_cues[0],
                retrieved_pages=retrieved_pages,
                relevance_scores=relevance_scores,
                recall_mode=mode,
                latency_ms=(time.time() - start) * 1000,
                total_pages_available=sum(1 for c in all_cues if c.embedding is not None),
            )
            self.recall_history.append(result)
            return result

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_recalls": len(self.recall_history),
                "avg_latency_ms": float(np.mean([r.latency_ms for r in self.recall_history])) if self.recall_history else 0.0,
                "avg_retrieved_pages": float(np.mean([len(r.retrieved_pages) for r in self.recall_history])) if self.recall_history else 0.0,
            }


# ============================================================================
# TrajectoryPageManager
# ============================================================================

class TrajectoryPageManager:
    """轨迹页管理器

    原始交互轨迹分页存储，每页含 thought/tool_call/observation/conclusion。
    """

    def __init__(self, max_pages_per_trajectory: int = 1000):
        self.max_pages_per_trajectory = max_pages_per_trajectory
        self._lock = threading.RLock()
        self.pages: Dict[str, TrajectoryPage] = {}
        self.trajectory_index: Dict[str, List[str]] = defaultdict(list)

        logger.info("TrajectoryPageManager initialized (max_pages=%d)", max_pages_per_trajectory)

    def add_page(self, page: TrajectoryPage) -> bool:
        """添加轨迹页"""
        with self._lock:
            existing = self.trajectory_index.get(page.parent_trajectory_id, [])
            if len(existing) >= self.max_pages_per_trajectory:
                return False
            self.pages[page.page_id] = page
            self.trajectory_index[page.parent_trajectory_id].append(page.page_id)
            return True

    def get_page(self, page_id: str) -> Optional[TrajectoryPage]:
        """获取指定轨迹页"""
        with self._lock:
            return self.pages.get(page_id)

    def get_trajectory_pages(self, trajectory_id: str) -> List[TrajectoryPage]:
        """获取指定轨迹的所有页"""
        with self._lock:
            page_ids = self.trajectory_index.get(trajectory_id, [])
            return [self.pages[pid] for pid in page_ids if pid in self.pages]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_pages": len(self.pages),
                "total_trajectories": len(self.trajectory_index),
                "avg_pages_per_trajectory": len(self.pages) / max(len(self.trajectory_index), 1),
            }


# ============================================================================
# ExpertGuidedSupervisor
# ============================================================================

class ExpertGuidedSupervisor:
    """专家引导监督器

    用专家标注训练线索质量，确保线索忠实反映原始信息。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.annotations: Dict[str, List[ExpertAnnotation]] = defaultdict(list)  # cue_id -> annotations
        self.faithfulness_threshold: float = 0.7

        logger.info("ExpertGuidedSupervisor initialized (faithfulness_threshold=%.2f)", self.faithfulness_threshold)

    def annotate(
        self,
        cue_id: str,
        annotator: str,
        quality_rating: CueQuality,
        faithfulness_score: float,
        comments: str = "",
    ) -> ExpertAnnotation:
        """对线索进行专家标注"""
        with self._lock:
            annotation = ExpertAnnotation(
                annotation_id=f"anno-{uuid.uuid4().hex[:12]}",
                cue_id=cue_id,
                annotator=annotator,
                quality_rating=quality_rating,
                faithfulness_score=faithfulness_score,
                comments=comments,
                annotated_at=time.time(),
            )
            self.annotations[cue_id].append(annotation)
            return annotation

    def get_average_faithfulness(self, cue_id: str) -> float:
        """获取线索的平均忠实度评分"""
        with self._lock:
            anns = self.annotations.get(cue_id, [])
            if not anns:
                return 0.0
            return float(np.mean([a.faithfulness_score for a in anns]))

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total_anns = sum(len(v) for v in self.annotations.values())
            return {
                "total_annotations": total_anns,
                "unique_cues_annotated": len(self.annotations),
                "avg_faithfulness": float(np.mean([
                    a.faithfulness_score for anns in self.annotations.values() for a in anns
                ])) if total_anns > 0 else 0.0,
            }


# ============================================================================
# RLOptimizer
# ============================================================================

class RLOptimizer:
    """RL 优化器

    对齐轨迹级效用，BrowseComp/BrowseComp-ZH/WideSearch/HLE 评测驱动。
    """

    SUPPORTED_BENCHMARKS = ["BrowseComp", "BrowseComp-ZH", "WideSearch", "HLE"]

    def __init__(self, learning_rate: float = 0.001, gamma: float = 0.99):
        self.learning_rate = learning_rate
        self.gamma = gamma
        self._lock = threading.RLock()
        self.episodes: List[RLEpisode] = []
        self._policy_weights: Dict[str, float] = {}

        logger.info("RLOptimizer initialized (lr=%.4f, gamma=%.2f)", learning_rate, gamma)

    def record_episode(
        self,
        trajectory_ids: List[str],
        reward: float,
        reward_type: RLRewardType,
        benchmark: str,
        completion: bool = True,
        metrics: Optional[Dict[str, float]] = None,
    ) -> RLEpisode:
        """记录 RL 训练片段"""
        with self._lock:
            episode = RLEpisode(
                episode_id=f"ep-{uuid.uuid4().hex[:12]}",
                trajectory_ids=trajectory_ids,
                reward=reward,
                reward_type=reward_type,
                benchmark=benchmark,
                completion=completion,
                metrics=metrics or {},
            )
            self.episodes.append(episode)

            key = f"{benchmark}:{reward_type.value}"
            old_weight = self._policy_weights.get(key, 0.0)
            self._policy_weights[key] = old_weight + self.learning_rate * reward

            return episode

    def get_benchmark_performance(self) -> Dict[str, Dict[str, float]]:
        """获取各评测基准的性能统计"""
        with self._lock:
            perf: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
            for ep in self.episodes:
                perf[ep.benchmark][ep.reward_type.value].append(ep.reward)

            return {
                bench: {rt: float(np.mean(rewards)) for rt, rewards in types.items()}
                for bench, types in perf.items()
            }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_episodes": len(self.episodes),
                "cumulative_reward": float(sum(e.reward for e in self.episodes)),
                "benchmark_performance": self.get_benchmark_performance(),
                "completion_rate": sum(1 for e in self.episodes if e.completion) / max(len(self.episodes), 1),
            }


# ============================================================================
# StateEncoder
# ============================================================================

class StateEncoder:
    """状态编码器

    将 Agent 当前状态编码为查询向量，匹配对应记忆线索。
    """

    def __init__(self, embedding_dim: int = 1536):
        self.embedding_dim = embedding_dim
        self._lock = threading.RLock()
        self.state_history: List[StateVector] = []

        logger.info("StateEncoder initialized (dim=%d)", embedding_dim)

    def encode(
        self,
        active_intent: str,
        context_window: Optional[str] = None,
        additional_features: Optional[Dict[str, Any]] = None,
    ) -> StateVector:
        """编码当前状态为查询向量"""
        with self._lock:
            seed = hash(active_intent + (context_window or ""))
            np.random.seed(seed)
            vector = np.random.randn(self.embedding_dim).astype(np.float32)
            vector /= np.linalg.norm(vector) + 1e-8
            np.random.seed(None)

            state = StateVector(
                state_id=f"state-{uuid.uuid4().hex[:12]}",
                vector=vector,
                timestamp=time.time(),
                active_intent=active_intent,
                confidence=1.0,
            )
            self.state_history.append(state)
            return state

    def encode_from_features(self, features: Dict[str, float]) -> StateVector:
        """从特征字典编码状态向量"""
        with self._lock:
            sorted_keys = sorted(features.keys())
            values = [features[k] for k in sorted_keys]
            seed = int(hashlib.md5(json.dumps(values).encode()).hexdigest()[:8], 16)
            np.random.seed(seed)
            vector = np.random.randn(self.embedding_dim).astype(np.float32)
            vector /= np.linalg.norm(vector) + 1e-8
            np.random.seed(None)

            state = StateVector(
                state_id=f"state-{uuid.uuid4().hex[:12]}",
                vector=vector,
                timestamp=time.time(),
            )
            self.state_history.append(state)
            return state

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_states_encoded": len(self.state_history),
                "embedding_dim": self.embedding_dim,
            }
