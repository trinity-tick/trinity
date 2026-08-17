"""
# status: orphan (2026-08-15 audit, not in runtime path)
MementoGUI — Plug-and-Play Multimodal Memory Controller for Long-Horizon GUI Agents
====================================================================================
arXiv 2605.18652 · P36-2

三元语: 即插即用多模态记忆控制器框架, 将工作记忆与情景记忆分离——
工作记忆通过 learned selection 选择性保留任务相关的 GUI 事件
(文本摘要 + ROI 级视觉证据), 情景记忆通过相关性选择检索可复用轨迹。

设计要点:
  - MementoCore: 记忆核心控制器, 在线记忆选择/压缩/检索的调度中枢,
    将工作记忆与情景记忆分离, 协调下游算子流水线。
  - WorkingMemorySelector: learned selection 机制, 判断每个 GUI 步骤
    是否值得保留; 保留时生成文本摘要和裁剪 ROI 视觉区域。
  - EpisodicMemoryWriter: 将完整成功轨迹压缩为可复用模板, 写入情景
    记忆库 (slot-based 存储, 支持时间衰减与访问频率加权)。
  - EpisodicRetrievalSelector: 基于当前 GUI 状态学习相关性分数,
    从情景记忆库检索最匹配的过去轨迹。
  - MemoryOperators: 可插拔算子集——StepProcessor / MemoryCompressor /
    EpisodicWriter / EpisodicSelector, 统一算子接口。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class GUIRetentionDecision(Enum):
    """工作记忆保留决策。"""
    RETAIN = auto()              # 保留: 写入摘要 + ROI
    DISCARD = auto()             # 丢弃: 低信息量步骤
    DEFER = auto()               # 暂缓: 留给最终轨迹判断


class OperatorLifecycle(Enum):
    """算子生命周期状态。"""
    UNINITIALIZED = auto()
    READY = auto()
    PROCESSING = auto()
    DONE = auto()
    ERROR = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class MementoROI:
    """ROI 级视觉证据区域。"""
    roi_id: str
    bbox: Tuple[int, int, int, int]   # (x, y, w, h)
    screenshot_hash: str
    feature_vector: Optional[np.ndarray] = None
    caption: str = ""


@dataclass
class MementoEvent:
    """单个 GUI 交互事件 (可保留单元)。"""
    event_id: str
    step_index: int
    action_type: str            # click, type, scroll, long_press, etc.
    target_element: str         # 目标控件的描述/ID
    text_summary: str           # 文本摘要
    rois: List[MementoROI] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    retention_score: float = 0.0
    decision: GUIRetentionDecision = GUIRetentionDecision.DEFER


@dataclass
class MementoWorkStep:
    """工作记忆中保留的单步记录。"""
    step_id: str
    event: MementoEvent
    text_summary: str
    rois: List[MementoROI] = field(default_factory=list)
    embed_vec: Optional[np.ndarray] = None
    priority: float = 1.0


@dataclass
class MementoEpisode:
    """情景记忆中的一条完整轨迹模板。"""
    episode_id: str
    task_description: str
    steps: List[MementoWorkStep]
    success: bool
    compressed_routine: List[Dict[str, Any]] = field(default_factory=list)
    retrieval_score: float = 0.0
    access_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)


@dataclass
class MementoRetrievalResult:
    """情景检索返回的单条匹配结果。"""
    episode: MementoEpisode
    relevance: float                    # [0, 1]
    matched_slots: List[str] = field(default_factory=list)
    rank: int = 0


# ---------------------------------------------------------------------------
# Core Classes
# ---------------------------------------------------------------------------

class MementoCore:
    """即插即用多模态记忆核心控制器。

    将工作记忆 (WorkingMemory) 与情景记忆 (EpisodicMemory) 分离,
    协调 MemoryOperators 中的 StepProcessor、MemoryCompressor、
    EpisodicWriter、EpisodicSelector 算子。

    Parameters
    ----------
    work_capacity : int
        工作记忆最大保留步数。
    episode_capacity : int
        情景记忆最大轨迹模板数。
    embedding_dim : int
        多模态嵌入向量维度。
    """

    def __init__(
        self,
        work_capacity: int = 64,
        episode_capacity: int = 1024,
        embedding_dim: int = 512,
    ) -> None:
        self.work_capacity = work_capacity
        self.episode_capacity = episode_capacity
        self.embedding_dim = embedding_dim

        # 记忆存储
        self._working_memory: List[MementoWorkStep] = []
        self._episodic_memory: Dict[str, MementoEpisode] = {}

        # 算子
        self._selector = WorkingMemorySelector(embedding_dim)
        self._episodic_writer = EpisodicMemoryWriter(episode_capacity)
        self._retriever = EpisodicRetrievalSelector(embedding_dim)
        self._operators = MemoryOperators(self)

        # 线程安全
        self._lock = threading.RLock()
        self._step_count: int = 0
        self._retrieve_count: int = 0

        logger.info(
            "MementoCore initialized [work_cap=%d ep_cap=%d dim=%d]",
            work_capacity, episode_capacity, embedding_dim,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_step(
        self,
        action_type: str,
        target_element: str,
        screenshot_rois: Optional[List[MementoROI]] = None,
    ) -> MementoWorkStep:
        """处理单个 GUI 步骤, 经选择器判断是否保留到工作记忆。

        Parameters
        ----------
        action_type : str
            动作类型 (click / type / scroll / long_press)。
        target_element : str
            目标控件描述。
        screenshot_rois : Optional[List[MementoROI]]
            可选的 ROI 列表。

        Returns
        -------
        MementoWorkStep
            处理后的工作记忆步骤记录。
        """
        with self._lock:
            self._step_count += 1

            event = MementoEvent(
                event_id=f"evt_{self._step_count}_{int(time.time()*1e6)}",
                step_index=self._step_count,
                action_type=action_type,
                target_element=target_element,
                text_summary=f"[{action_type}] {target_element}",
                rois=screenshot_rois or [],
            )

            # 工作记忆选择
            decision, score = self._selector.decide(event)
            event.retention_score = score
            event.decision = decision

            # 生成摘要与 ROI
            if decision == GUIRetentionDecision.RETAIN:
                summary = self._selector.summarize(event)
                selected_rois = self._selector.select_rois(event)
            else:
                summary = event.text_summary
                selected_rois = []

            step = MementoWorkStep(
                step_id=f"step_{self._step_count}",
                event=event,
                text_summary=summary,
                rois=selected_rois,
                embed_vec=self._make_embedding(summary),
            )

            if decision == GUIRetentionDecision.RETAIN:
                self._working_memory.append(step)
                self._evict_overflow()

            return step

    def commit_episode(
        self,
        task_description: str,
        success: bool = True,
    ) -> MementoEpisode:
        """将当前工作记忆提交为一条情景记忆轨迹。

        Parameters
        ----------
        task_description : str
            任务描述 (用于后续检索匹配)。
        success : bool
            轨迹是否成功完成。

        Returns
        -------
        MementoEpisode
            存入情景记忆的轨迹模板。
        """
        with self._lock:
            episode = self._episodic_writer.write(
                steps=list(self._working_memory),
                task_description=task_description,
                success=success,
            )
            # 写入情景记忆库
            self._episodic_memory[episode.episode_id] = episode
            # 清空工作记忆
            self._working_memory.clear()
            return episode

    def retrieve_episodes(
        self,
        current_state: MementoWorkStep,
        top_k: int = 5,
    ) -> List[MementoRetrievalResult]:
        """基于当前 GUI 状态检索最匹配的情景记忆。

        Parameters
        ----------
        current_state : MementoWorkStep
            当前工作记忆步骤。
        top_k : int
            返回前 k 条。

        Returns
        -------
        List[MementoRetrievalResult]
            按相关性降序的检索结果。
        """
        with self._lock:
            self._retrieve_count += 1
            results = self._retriever.retrieve(
                query_step=current_state,
                episodes=list(self._episodic_memory.values()),
                top_k=top_k,
            )
            # 更新访问计数
            for r in results:
                r.episode.access_count += 1
                r.episode.last_accessed = time.time()
            return results

    def compress_working_memory(self) -> int:
        """压缩工作记忆 (去重、合并相邻同类步骤)。

        Returns
        -------
        int
            压缩后减少的条目数。
        """
        with self._lock:
            original_len = len(self._working_memory)
            compressor = self._operators.compressor
            self._working_memory = compressor.compress(self._working_memory)
            return original_len - len(self._working_memory)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_steps_processed": self._step_count,
                "working_memory_size": len(self._working_memory),
                "work_capacity": self.work_capacity,
                "episodic_memory_size": len(self._episodic_memory),
                "episode_capacity": self.episode_capacity,
                "retrieve_count": self._retrieve_count,
                "selector_stats": self._selector.statistics(),
            }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evict_overflow(self) -> None:
        """工作记忆溢出时驱逐最低优先级步骤。"""
        while len(self._working_memory) > self.work_capacity:
            min_idx = min(
                range(len(self._working_memory)),
                key=lambda i: self._working_memory[i].priority,
            )
            evicted = self._working_memory.pop(min_idx)
            logger.debug("Evicted work step %s (priority=%.3f)", evicted.step_id, evicted.priority)

    def _make_embedding(self, text: str) -> np.ndarray:
        """生产嵌入 (哈希降维代理, 生产环境替换为 CLIP/LLaVA 编码器)。"""
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:16], 16)
        rng = np.random.RandomState(seed % (2 ** 31 - 1))
        vec = rng.randn(self.embedding_dim)
        return vec / (np.linalg.norm(vec) + 1e-8)


# =============================================================================
# WorkingMemorySelector
# =============================================================================

class WorkingMemorySelector:
    """工作记忆选择器。

    Learned selection 机制: 对每个 GUI 步骤计算保留分值, 判断是否
    值得保留; 保留时生成文本摘要并裁剪 ROI 视觉区域。

    Parameters
    ----------
    embedding_dim : int
        嵌入维度。
    retention_threshold : float
        保留阈值 (score >= threshold → RETAIN)。
    defer_threshold : float
        暂缓下界 (defer_threshold <= score < retention_threshold → DEFER)。
    """

    def __init__(
        self,
        embedding_dim: int = 512,
        retention_threshold: float = 0.6,
        defer_threshold: float = 0.3,
    ) -> None:
        self.embedding_dim = embedding_dim
        self.retention_threshold = retention_threshold
        self.defer_threshold = defer_threshold
        self._lock = threading.RLock()
        self._total_scored: int = 0
        self._total_retained: int = 0
        self._total_discarded: int = 0
        # 学习的权重 (简化为特征重要性向量, 生产环境用神经网络)
        self._feature_weights: np.ndarray = np.ones(5) / 5.0
        logger.info("WorkingMemorySelector initialized [thresh=%.2f/%.2f]", retention_threshold, defer_threshold)

    def decide(self, event: MementoEvent) -> Tuple[GUIRetentionDecision, float]:
        """判断该 GUI 事件是否值得保留。

        Parameters
        ----------
        event : MementoEvent
            待判断的 GUI 事件。

        Returns
        -------
        Tuple[GUIRetentionDecision, float]
            (决策, 保留分数 [0, 1])。
        """
        with self._lock:
            # 特征提取: [action_complexity, element_specificity, has_visuals, text_len, step_index_recent]
            features = np.array([
                self._action_complexity(event.action_type),
                self._element_specificity(event.target_element),
                1.0 if event.rois else 0.0,
                min(len(event.text_summary) / 200.0, 1.0),
                1.0,  # 近因偏置 (生产环境按全局步数归一化)
            ])
            score = float(np.dot(features, self._feature_weights))
            score = 1.0 / (1.0 + np.exp(-5.0 * (score - 0.5)))  # sigmoid 映射
            score = float(score)

            self._total_scored += 1
            if score >= self.retention_threshold:
                decision = GUIRetentionDecision.RETAIN
                self._total_retained += 1
            elif score >= self.defer_threshold:
                decision = GUIRetentionDecision.DEFER
            else:
                decision = GUIRetentionDecision.DISCARD
                self._total_discarded += 1

            return decision, score

    def summarize(self, event: MementoEvent) -> str:
        """为保留事件生成文本摘要。

        Parameters
        ----------
        event : MementoEvent
            待摘要的 GUI 事件。

        Returns
        -------
        str
            文本摘要。
        """
        parts = [f"[{event.action_type}]"]
        if event.target_element:
            parts.append(f"target={event.target_element[:80]}")
        if event.rois:
            parts.append(f"rois={len(event.rois)}")
        parts.append(f"step={event.step_index}")
        return " ".join(parts)

    def select_rois(self, event: MementoEvent) -> List[MementoROI]:
        """裁剪 ROI 视觉区域 (选择最相关的 1-2 个区域)。

        Parameters
        ----------
        event : MementoEvent
            含 ROI 列表的事件。

        Returns
        -------
        List[MementoROI]
            选中的 ROI 列表。
        """
        if not event.rois:
            return []
        # 按面积排序取前 2 个最大区域 (生产环境可用显著性检测)
        sorted_rois = sorted(
            event.rois,
            key=lambda r: r.bbox[2] * r.bbox[3],
            reverse=True,
        )
        return sorted_rois[:2]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_scored": self._total_scored,
                "total_retained": self._total_retained,
                "total_discarded": self._total_discarded,
                "retain_rate": self._total_retained / max(self._total_scored, 1),
            }

    # ------------------------------------------------------------------
    # Feature extractors
    # ------------------------------------------------------------------

    @staticmethod
    def _action_complexity(action_type: str) -> float:
        """动作复杂度评分。"""
        complexity_map = {
            "click": 0.3, "type": 0.7, "scroll": 0.5,
            "long_press": 0.6, "swipe": 0.4, "drag": 0.8,
        }
        return complexity_map.get(action_type, 0.5)

    @staticmethod
    def _element_specificity(target_element: str) -> float:
        """目标控件特异性评分 (越具体越高)。"""
        if not target_element:
            return 0.3
        # 有 content-desc / resource-id 等具体属性则高分
        specific_markers = ("id:", "name:", "content-desc:", "//", "@")
        return 0.7 if any(m in target_element for m in specific_markers) else 0.4


# =============================================================================
# EpisodicMemoryWriter
# =============================================================================

class EpisodicMemoryWriter:
    """情景记忆写入器。

    将完整成功轨迹压缩为可复用模板 (合并冗余步骤、提取关键决策点),
    写入情景记忆库。

    Parameters
    ----------
    capacity : int
        情景记忆最大容量。
    compression_enabled : bool
        是否启用轨迹压缩。
    """

    def __init__(
        self,
        capacity: int = 1024,
        compression_enabled: bool = True,
    ) -> None:
        self.capacity = capacity
        self.compression_enabled = compression_enabled
        self._lock = threading.RLock()
        self._write_count: int = 0
        logger.info("EpisodicMemoryWriter initialized [cap=%d comp=%s]", capacity, compression_enabled)

    def write(
        self,
        steps: List[MementoWorkStep],
        task_description: str,
        success: bool,
    ) -> MementoEpisode:
        """将工作记忆轨迹写入情景记忆。

        Parameters
        ----------
        steps : List[MementoWorkStep]
            工作记忆步骤序列。
        task_description : str
            任务描述。
        success : bool
            轨迹是否成功。

        Returns
        -------
        MementoEpisode
            压缩后的情景记忆轨迹。
        """
        with self._lock:
            # 压缩轨迹
            if self.compression_enabled and len(steps) > 3:
                compressed = self._compress_trajectory(steps)
            else:
                compressed = [
                    {
                        "action": s.event.action_type,
                        "target": s.event.target_element,
                        "summary": s.text_summary,
                    }
                    for s in steps
                ]

            episode_id = f"ep_{self._write_count}_{int(time.time() * 1e6)}"
            episode = MementoEpisode(
                episode_id=episode_id,
                task_description=task_description,
                steps=list(steps),
                success=success,
                compressed_routine=compressed,
            )
            self._write_count += 1
            return episode

    def _compress_trajectory(self, steps: List[MementoWorkStep]) -> List[Dict[str, Any]]:
        """轨迹压缩: 合并连续同类动作, 去除无信息量步骤。"""
        if not steps:
            return []

        compressed: List[Dict[str, Any]] = []
        i = 0
        while i < len(steps):
            current = steps[i]
            merge_count = 1
            # 合并连续同类动作 (如连续 scroll)
            j = i + 1
            while j < len(steps) and steps[j].event.action_type == current.event.action_type:
                if steps[j].event.target_element == current.event.target_element:
                    merge_count += 1
                    j += 1
                else:
                    break

            compressed.append({
                "action": current.event.action_type,
                "target": current.event.target_element,
                "summary": current.text_summary,
                "repeat": merge_count,
                "step_range": (current.event.step_index, steps[j - 1].event.step_index if j > i + 1 else current.event.step_index),
            })
            i = j

        return compressed

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "write_count": self._write_count,
                "capacity": self.capacity,
            }


# =============================================================================
# EpisodicRetrievalSelector
# =============================================================================

class EpisodicRetrievalSelector:
    """情景检索选择器。

    基于当前 GUI 状态学习相关性分数, 综合语义相似度、任务描述匹配度
    和轨迹成功率进行加权排序, 从情景记忆库检索最匹配的过去轨迹。

    Parameters
    ----------
    embedding_dim : int
        嵌入向量维度。
    semantic_weight : float
        语义相似度权重。
    task_match_weight : float
        任务描述匹配权重。
    success_bonus : float
        成功轨迹加权系数。
    """

    def __init__(
        self,
        embedding_dim: int = 512,
        semantic_weight: float = 0.5,
        task_match_weight: float = 0.3,
        success_bonus: float = 0.2,
    ) -> None:
        self.embedding_dim = embedding_dim
        self.semantic_weight = semantic_weight
        self.task_match_weight = task_match_weight
        self.success_bonus = success_bonus
        self._lock = threading.RLock()
        self._retrieval_count: int = 0
        logger.info("EpisodicRetrievalSelector initialized [sem=%.2f task=%.2f succ=%.2f]",
                    semantic_weight, task_match_weight, success_bonus)

    def retrieve(
        self,
        query_step: MementoWorkStep,
        episodes: List[MementoEpisode],
        top_k: int = 5,
    ) -> List[MementoRetrievalResult]:
        """从情景记忆库检索最匹配轨迹。

        Parameters
        ----------
        query_step : MementoWorkStep
            当前 GUI 状态的步骤记录。
        episodes : List[MementoEpisode]
            情景记忆库中的所有轨迹。
        top_k : int
            返回前 k 条结果。

        Returns
        -------
        List[MementoRetrievalResult]
            按相关性降序的检索结果。
        """
        with self._lock:
            scored: List[Tuple[MementoEpisode, float]] = []

            for ep in episodes:
                # (1) 语义相似度 (基于嵌入余弦)
                if query_step.embed_vec is not None and ep.steps:
                    ep_embeds = [s.embed_vec for s in ep.steps if s.embed_vec is not None]
                    if ep_embeds:
                        avg_embed = np.mean(ep_embeds, axis=0)
                        sem_sim = float(np.dot(query_step.embed_vec, avg_embed) /
                                        (np.linalg.norm(query_step.embed_vec) *
                                         np.linalg.norm(avg_embed) + 1e-8))
                    else:
                        sem_sim = 0.0
                else:
                    sem_sim = 0.0

                # (2) 任务描述匹配 (关键词重叠简化为 Jaccard)
                query_words = set(query_step.text_summary.lower().split())
                task_words = set(ep.task_description.lower().split())
                jaccard = len(query_words & task_words) / max(len(query_words | task_words), 1)

                # (3) 时间衰变 + 成功奖励
                decay = np.exp(-(time.time() - ep.last_accessed) / 3600.0)
                recency_bonus = min(1.0, decay * 0.5 + 0.5)
                success_factor = 1.0 + self.success_bonus if ep.success else 1.0

                relevance = (
                    self.semantic_weight * sem_sim
                    + self.task_match_weight * jaccard
                    + 0.1 * recency_bonus
                ) * success_factor

                scored.append((ep, relevance))

            scored.sort(key=lambda x: x[1], reverse=True)
            top = scored[:top_k]

            results = [
                MementoRetrievalResult(episode=ep, relevance=rel, rank=i + 1)
                for i, (ep, rel) in enumerate(top)
            ]
            self._retrieval_count += 1
            return results

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "retrieval_count": self._retrieval_count,
                "semantic_weight": self.semantic_weight,
                "task_match_weight": self.task_match_weight,
            }


# =============================================================================
# MemoryOperators
# =============================================================================

class MemoryOperators:
    """可插拔记忆操作器集。

    统一算子接口, 包含:
      - StepProcessor: 处理单步 GUI 事件
      - MemoryCompressor: 压缩工作记忆
      - EpisodicWriter: 写入情景记忆
      - EpisodicSelector: 检索情景记忆

    Parameters
    ----------
    core : MementoCore
        关联的记忆核心实例 (供算子访问内部状态)。
    """

    def __init__(self, core: MementoCore) -> None:
        self.core = core
        self._lock = threading.RLock()
        self.processor = _StepProcessor(core)
        self.compressor = _MemoryCompressor(core)
        self.episodic_writer = _EpisodicWriterOperator(core)
        self.episodic_selector = _EpisodicSelectorOperator(core)
        self._lifecycle: Dict[str, OperatorLifecycle] = {
            "processor": OperatorLifecycle.READY,
            "compressor": OperatorLifecycle.READY,
            "episodic_writer": OperatorLifecycle.READY,
            "episodic_selector": OperatorLifecycle.READY,
        }
        logger.info("MemoryOperators initialized [4 operators ready]")

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "lifecycle": {k: v.name for k, v in self._lifecycle.items()},
                "compressor": self.compressor.statistics(),
            }


# ---------------------------------------------------------------------------
# Private Operator Implementations
# ---------------------------------------------------------------------------

class _StepProcessor:
    """步骤处理算子: 处理单步 GUI 事件并路由到工作记忆。"""

    def __init__(self, core: MementoCore) -> None:
        self.core = core
        self._processed: int = 0

    def __call__(
        self,
        action_type: str,
        target_element: str,
        rois: Optional[List[MementoROI]] = None,
    ) -> MementoWorkStep:
        self._processed += 1
        return self.core.process_step(action_type, target_element, rois)

    def statistics(self) -> Dict[str, Any]:
        return {"processed": self._processed}


class _MemoryCompressor:
    """记忆压缩算子: 去重与合并相邻同类步骤。"""

    def __init__(self, core: MementoCore) -> None:
        self._lock = threading.RLock()
        self._compress_count: int = 0

    def compress(self, steps: List[MementoWorkStep]) -> List[MementoWorkStep]:
        """压缩工作记忆。"""
        with self._lock:
            if len(steps) < 2:
                return steps

            compressed: List[MementoWorkStep] = []
            i = 0
            while i < len(steps):
                current = steps[i]
                j = i + 1

                # 合并连续同类动作
                merge_count = 1
                while j < len(steps):
                    if (steps[j].event.action_type == current.event.action_type
                            and steps[j].event.target_element == current.event.target_element):
                        merge_count += 1
                        j += 1
                    else:
                        break

                if merge_count > 1:
                    current.text_summary += f" (x{merge_count})"
                    # 保留最高优先级的 ROI
                    current.rois = list(current.rois)

                compressed.append(current)
                i = j

            self._compress_count += 1
            return compressed

    def statistics(self) -> Dict[str, Any]:
        return {"compress_count": self._compress_count}


class _EpisodicWriterOperator:
    """情景写入算子: 封装 EpisodicMemoryWriter 调用。"""

    def __init__(self, core: MementoCore) -> None:
        self.core = core
        self._count: int = 0

    def __call__(self, task: str, success: bool = True) -> MementoEpisode:
        self._count += 1
        return self.core.commit_episode(task, success)

    def statistics(self) -> Dict[str, Any]:
        return {"written": self._count}


class _EpisodicSelectorOperator:
    """情景选择算子: 封装 EpisodicRetrievalSelector 调用。"""

    def __init__(self, core: MementoCore) -> None:
        self.core = core
        self._count: int = 0

    def __call__(self, query_step: MementoWorkStep, top_k: int = 5) -> List[MementoRetrievalResult]:
        self._count += 1
        return self.core.retrieve_episodes(query_step, top_k)

    def statistics(self) -> Dict[str, Any]:
        return {"selections": self._count}
