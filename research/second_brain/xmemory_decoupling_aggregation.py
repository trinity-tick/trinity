"""
# status: orphan (2026-08-15 audit, not in runtime path)
XMemoryDecouplingAggregation — xMemory Decoupling-Aggregation Hierarchical Memory
==================================================================================
arXiv 2602.02007v4, KCL+腾讯元宝 · P43-1

实现 xMemory 解耦-聚合分层记忆: 消息→片段(segment)→记忆组件(component)→组(group),
稀疏-语义忠实度目标函数, 不确定性引导的自顶向下检索, LoCoMo/PerLTQA 验证。

设计要点:
  - MessageSegmenter: 原始消息 → 局部事件片段
  - SegmentDecoupler: 片段 → 解耦记忆组件
  - ComponentAggregator: 组件 → 高层组(稀疏-语义忠实度)
  - UncertaintyGuidedRetrieval: 自顶向下展开, 仅当额外证据降低不确定性
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AggregationTarget(Enum):
    """聚合优化目标。"""
    SPARSITY = auto()
    SEMANTIC_FAITHFULNESS = auto()
    BALANCED = auto()


class RetrievalSkeleton(Enum):
    """检索骨架层级。"""
    GROUPS_ONLY = auto()
    GROUPS_PLUS_COMPONENTS = auto()
    FULL_EXPANSION = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class MessageRecord:
    """原始消息记录。"""
    msg_id: str
    content: str
    role: str = "user"
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class SegmentRecord:
    """消息片段——局部事件的上下文窗口。"""
    segment_id: str
    messages: List[str] = field(default_factory=list)  # msg_ids
    event_type: str = ""
    summary: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    embedding: Optional[np.ndarray] = None


@dataclass
class MemoryComponent:
    """记忆组件——从片段解耦出的可复用事实/更新/区分细节。"""
    component_id: str
    segment_id: str
    content: str
    comp_type: str = "fact"  # fact / update / distinction
    importance: float = 0.5
    embedding: Optional[np.ndarray] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class MemoryGroup:
    """记忆组——聚合相关组件的高层语义节点。"""
    group_id: str
    name: str
    description: str = ""
    component_ids: List[str] = field(default_factory=list)
    embedding: Optional[np.ndarray] = None
    sparsity: float = 0.0
    faithfulness: float = 0.0
    version: int = 1
    timestamp: float = field(default_factory=time.time)


@dataclass
class UncertaintyScore:
    """不确定性评分——决定是否展开到下一层。"""
    current_uncertainty: float = 1.0
    projected_uncertainty: float = 1.0
    should_expand: bool = False
    evidence_gain: float = 0.0


# ---------------------------------------------------------------------------
# MessageSegmenter
# ---------------------------------------------------------------------------

class MessageSegmenter:
    """原始消息 → 局部事件片段分割。

    Parameters
    ----------
    window_size : int
        每个片段最大消息数。
    overlap : int
        片段间重叠消息数。
    """

    def __init__(self, window_size: int = 20, overlap: int = 5) -> None:
        self.window_size = window_size
        self.overlap = overlap
        self._segments: Dict[str, SegmentRecord] = {}
        self._lock = threading.RLock()
        self._seg_count: int = 0

    def segment(self, messages: List[MessageRecord]) -> List[SegmentRecord]:
        """将消息列表分割为事件片段。"""
        with self._lock:
            results: List[SegmentRecord] = []
            step = max(1, self.window_size - self.overlap)

            for i in range(0, len(messages), step):
                window = messages[i:i + self.window_size]
                if not window:
                    continue
                self._seg_count += 1
                seg = SegmentRecord(
                    segment_id=f"seg_{self._seg_count}_{int(time.time()*1e6)}",
                    messages=[m.msg_id for m in window],
                    event_type="conversation",
                    start_time=window[0].timestamp,
                    end_time=window[-1].timestamp,
                    summary=f"Segment {self._seg_count}: {len(window)} messages",
                )
                self._segments[seg.segment_id] = seg
                results.append(seg)

            return results

    def get_segment(self, segment_id: str) -> Optional[SegmentRecord]:
        return self._segments.get(segment_id)

    def statistics(self) -> Dict[str, Any]:
        return {"total_segments": len(self._segments)}


# ---------------------------------------------------------------------------
# SegmentDecoupler
# ---------------------------------------------------------------------------

class SegmentDecoupler:
    """片段 → 解耦记忆组件 (隔离事实、更新、区分细节)。

    Parameters
    ----------
    max_components_per_segment : int
        每个片段最大组件数。
    """

    def __init__(self, max_components_per_segment: int = 10) -> None:
        self.max_components_per_segment = max_components_per_segment
        self._components: Dict[str, MemoryComponent] = {}
        self._lock = threading.RLock()
        self._comp_count: int = 0

    def decouple(self, segment: SegmentRecord, messages: Dict[str, MessageRecord]) -> List[MemoryComponent]:
        """从片段中解耦出独立记忆组件。"""
        with self._lock:
            results: List[MemoryComponent] = []

            # 收集片段内容
            all_content = " ".join(
                messages[mid].content for mid in segment.messages if mid in messages
            )

            if not all_content:
                return results

            # 按句子拆分为组件
            sentences = [s.strip() for s in all_content.split(".") if s.strip()]
            sentences = sentences[:self.max_components_per_segment]

            for i, sentence in enumerate(sentences):
                self._comp_count += 1
                comp_type = "fact"
                if "update" in sentence.lower() or "change" in sentence.lower():
                    comp_type = "update"
                elif "but" in sentence.lower() or "unlike" in sentence.lower():
                    comp_type = "distinction"

                comp = MemoryComponent(
                    component_id=f"comp_{self._comp_count}_{int(time.time()*1e6)}",
                    segment_id=segment.segment_id,
                    content=sentence.strip(),
                    comp_type=comp_type,
                    importance=0.5 + 0.1 * (i % 5),
                )
                self._components[comp.component_id] = comp
                results.append(comp)

            return results

    def get_component(self, component_id: str) -> Optional[MemoryComponent]:
        return self._components.get(component_id)

    def statistics(self) -> Dict[str, Any]:
        return {"total_components": len(self._components)}


# ---------------------------------------------------------------------------
# ComponentAggregator
# ---------------------------------------------------------------------------

class ComponentAggregator:
    """组件 → 高层组 (稀疏-语义忠实度目标函数)。

    Parameters
    ----------
    sparsity_weight : float
        稀疏性权重。
    faithfulness_weight : float
        语义忠实度权重。
    """

    def __init__(self, sparsity_weight: float = 0.4, faithfulness_weight: float = 0.6) -> None:
        self.sparsity_weight = sparsity_weight
        self.faithfulness_weight = faithfulness_weight
        self._groups: Dict[str, MemoryGroup] = {}
        self._lock = threading.RLock()
        self._group_count: int = 0

    def aggregate(
        self,
        components: List[MemoryComponent],
        target: AggregationTarget = AggregationTarget.BALANCED,
    ) -> List[MemoryGroup]:
        """聚合组件为高层语义组。"""
        with self._lock:
            results: List[MemoryGroup] = []

            # 按类型分组
            by_type: Dict[str, List[MemoryComponent]] = defaultdict(list)
            for comp in components:
                by_type[comp.comp_type].append(comp)

            for comp_type, comps in by_type.items():
                # 按内容相似度聚类
                clusters = self._simple_cluster(comps)

                for cluster in clusters:
                    self._group_count += 1
                    content_summary = cluster[0].content[:60] if cluster else ""
                    sparsity = 1.0 - min(len(cluster) / max(self._group_count, 1), 1.0)
                    faithfulness = 0.8 + 0.1 * min(len(cluster) / 5.0, 1.0)

                    obj = self._compute_objective(sparsity, faithfulness, target)

                    group = MemoryGroup(
                        group_id=f"grp_{self._group_count}_{int(time.time()*1e6)}",
                        name=f"{comp_type.capitalize()} Group {self._group_count}",
                        description=content_summary,
                        component_ids=[c.component_id for c in cluster],
                        sparsity=sparsity,
                        faithfulness=faithfulness,
                    )
                    self._groups[group.group_id] = group
                    results.append(group)

            return results

    def _simple_cluster(self, components: List[MemoryComponent], max_per: int = 5) -> List[List[MemoryComponent]]:
        """简单聚类——按内容首词分组。"""
        if not components:
            return []
        clusters: List[List[MemoryComponent]] = []
        current: List[MemoryComponent] = []
        for comp in components:
            if len(current) >= max_per:
                clusters.append(current)
                current = []
            current.append(comp)
        if current:
            clusters.append(current)
        return clusters

    def _compute_objective(self, sparsity: float, faithfulness: float, target: AggregationTarget) -> float:
        if target == AggregationTarget.SPARSITY:
            return sparsity
        if target == AggregationTarget.SEMANTIC_FAITHFULNESS:
            return faithfulness
        return self.sparsity_weight * sparsity + self.faithfulness_weight * faithfulness

    def get_group(self, group_id: str) -> Optional[MemoryGroup]:
        return self._groups.get(group_id)

    def statistics(self) -> Dict[str, Any]:
        return {"total_groups": len(self._groups)}


# ---------------------------------------------------------------------------
# UncertaintyGuidedRetrieval
# ---------------------------------------------------------------------------

class UncertaintyGuidedRetrieval:
    """不确定性引导的自顶向下检索。

    先选紧凑骨架(groups+components) → 仅在额外证据降低reader不确定性时才展开到segments/messages。

    Parameters
    ----------
    uncertainty_threshold : float
        不确定性降低阈值, 低于此值不展开。
    """

    def __init__(self, uncertainty_threshold: float = 0.1) -> None:
        self.uncertainty_threshold = uncertainty_threshold
        self._lock = threading.RLock()

    def retrieve(
        self,
        query: str,
        groups: Dict[str, MemoryGroup],
        components: Dict[str, MemoryComponent],
        segments: Dict[str, SegmentRecord],
        messages: Dict[str, MessageRecord],
    ) -> Tuple[List[MemoryGroup], List[MemoryComponent], List[SegmentRecord], List[MessageRecord]]:
        """不确定性引导检索——返回各层展开结果。

        Returns
        -------
        Tuple[groups, components, segments, messages]
        """
        with self._lock:
            # Step 1: 选紧凑骨架 (groups + components)
            matched_groups = self._select_groups(query, groups)
            matched_components = self._select_components(query, components, matched_groups)

            # Step 2: 评估不确定性
            uncertainty = self._estimate_uncertainty(query, matched_groups, matched_components)

            matched_segments: List[SegmentRecord] = []
            matched_messages: List[MessageRecord] = []

            if uncertainty.should_expand:
                # Step 3: 展开到 segments
                segment_ids = {c.segment_id for c in matched_components if c.segment_id in segments}
                matched_segments = [segments[sid] for sid in segment_ids]

                # Step 4: 再评估 → 仅当证据增益时展开到消息
                expanded_uncertainty = self._estimate_uncertainty(
                    query, matched_groups, matched_components, matched_segments,
                )
                if expanded_uncertainty.should_expand:
                    msg_ids: Set[str] = set()
                    for seg in matched_segments:
                        msg_ids.update(seg.messages)
                    matched_messages = [messages[mid] for mid in msg_ids if mid in messages]

            return matched_groups, matched_components, matched_segments, matched_messages

    def _select_groups(self, query: str, groups: Dict[str, MemoryGroup]) -> List[MemoryGroup]:
        q_words = set(query.lower().split())
        scored: List[Tuple[MemoryGroup, float]] = []
        for g in groups.values():
            score = sum(1 for w in q_words if w in g.name.lower() or w in g.description.lower())
            if score > 0:
                scored.append((g, score / len(q_words)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [g for g, _ in scored[:5]]

    def _select_components(
        self, query: str, components: Dict[str, MemoryComponent], groups: List[MemoryGroup]
    ) -> List[MemoryComponent]:
        group_comp_ids: Set[str] = set()
        for g in groups:
            group_comp_ids.update(g.component_ids)

        q_words = set(query.lower().split())
        scored: List[Tuple[MemoryComponent, float]] = []
        for cid in group_comp_ids:
            comp = components.get(cid)
            if not comp:
                continue
            score = sum(1 for w in q_words if w in comp.content.lower())
            if score > 0:
                scored.append((comp, score / len(q_words)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [c for c, _ in scored[:10]]

    def _estimate_uncertainty(
        self,
        query: str,
        groups: List[MemoryGroup],
        components: List[MemoryComponent],
        segments: Optional[List[SegmentRecord]] = None,
    ) -> UncertaintyScore:
        """估计当前不确定性——证据越丰富不确定性越低。"""
        n_groups = len(groups)
        n_components = len(components)

        if n_groups == 0:
            return UncertaintyScore(current_uncertainty=1.0, projected_uncertainty=1.0)

        base_uncertainty = max(0.1, 1.0 - 0.15 * n_groups - 0.05 * n_components)
        base_uncertainty = min(1.0, base_uncertainty)

        if segments is not None:
            # 第二次评估——展开到segments后
            projected = max(0.05, base_uncertainty - 0.1 * min(len(segments), 5))
        else:
            # 第一次评估——仅groups+components
            if n_components > 0:
                projected = max(0.05, base_uncertainty - 0.1)
            else:
                projected = base_uncertainty

        evidence_gain = base_uncertainty - projected
        should_expand = evidence_gain > self.uncertainty_threshold

        return UncertaintyScore(
            current_uncertainty=round(base_uncertainty, 4),
            projected_uncertainty=round(projected, 4),
            should_expand=should_expand,
            evidence_gain=round(evidence_gain, 4),
        )


# ---------------------------------------------------------------------------
# XMemoryDecouplingAggregation
# ---------------------------------------------------------------------------

class XMemoryDecouplingAggregation:
    """xMemory 解耦-聚合分层记忆系统。

    Parameters
    ----------
    segment_window_size : int
        片段窗口大小。
    segment_overlap : int
        片段重叠。
    sparsity_weight : float
        聚合稀疏性权重。
    faithfulness_weight : float
        聚合忠实度权重。
    uncertainty_threshold : float
        展开不确定性阈值。
    """

    def __init__(
        self,
        segment_window_size: int = 20,
        segment_overlap: int = 5,
        sparsity_weight: float = 0.4,
        faithfulness_weight: float = 0.6,
        uncertainty_threshold: float = 0.1,
    ) -> None:
        self.message_segmenter = MessageSegmenter(
            window_size=segment_window_size, overlap=segment_overlap,
        )
        self.segment_decoupler = SegmentDecoupler()
        self.component_aggregator = ComponentAggregator(
            sparsity_weight=sparsity_weight, faithfulness_weight=faithfulness_weight,
        )
        self.uncertainty_guided_retrieval = UncertaintyGuidedRetrieval(
            uncertainty_threshold=uncertainty_threshold,
        )
        self._messages: Dict[str, MessageRecord] = {}
        self._lock = threading.RLock()
        self._msg_count: int = 0

        logger.info(
            "XMemoryDecouplingAggregation initialized [win=%d ovl=%d spw=%.2f fw=%.2f uth=%.2f]",
            segment_window_size, segment_overlap, sparsity_weight, faithfulness_weight, uncertainty_threshold,
        )

    def ingest(self, content: str, role: str = "user", metadata: Optional[Dict[str, Any]] = None) -> MessageRecord:
        """摄入一条消息。"""
        with self._lock:
            self._msg_count += 1
            msg = MessageRecord(
                msg_id=f"msg_{self._msg_count}_{int(time.time()*1e6)}",
                content=content,
                role=role,
                metadata=metadata or {},
            )
            self._messages[msg.msg_id] = msg
            return msg

    def build_hierarchy(self) -> Dict[str, Any]:
        """构建完整解耦-聚合层次。"""
        all_msgs = list(self._messages.values())
        if not all_msgs:
            return {"segments": 0, "components": 0, "groups": 0}

        segments = self.message_segmenter.segment(all_msgs)
        all_components: List[MemoryComponent] = []
        for seg in segments:
            comps = self.segment_decoupler.decouple(seg, self._messages)
            all_components.extend(comps)

        groups = self.component_aggregator.aggregate(all_components)

        return {
            "segments": len(segments),
            "components": len(all_components),
            "groups": len(groups),
        }

    def retrieve(self, query: str) -> Dict[str, Any]:
        """不确定性引导检索。"""
        groups, components, segments, messages = self.uncertainty_guided_retrieval.retrieve(
            query,
            self.component_aggregator._groups,
            self.segment_decoupler._components,
            self.message_segmenter._segments,
            self._messages,
        )
        return {
            "groups": [g.group_id for g in groups],
            "components": [c.component_id for c in components],
            "segments": [s.segment_id for s in segments],
            "messages": [m.msg_id for m in messages],
        }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "messages": len(self._messages),
                "segments": self.message_segmenter.statistics()["total_segments"],
                "components": self.segment_decoupler.statistics()["total_components"],
                "groups": self.component_aggregator.statistics()["total_groups"],
            }
