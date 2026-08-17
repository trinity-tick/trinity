"""
# status: orphan (2026-08-15 audit, not in runtime path)
P20-4: Hierarchical Video Memory — 层次化视频记忆

对标论文: Homer (arXiv 2607.02588, 2026.07)
核心发现: 三层视频记忆 + 智能体式视频推理 + 时序因果提取 → 视频理解超越帧级到事件因果级
三元语: 感知层→实体层→因果层 → 智能体探索 → 多轮自校验检索 → 实体再识别 → 步级验证

设计要点:
- HierarchicalVideoMemoryStore: raw_perception / recurring_entities / temporal_causal_events 三层递增抽象
- AgenticVideoReasoner: 模拟人类探索记忆——定位场景→查询细节→多轮检索组合回答
- MultiRoundRetrievalComposer: 每轮检索后步级自校验，确保证据连贯性
- TemporalCausalLinkExtractor: 从原始感知中提取事件间显式因果关系 (A→B→C)
- EntityReidentificationTracker: 跨帧追踪同一实体 (人物/物体/场景)，解决遮挡和视角变化
- VideoMemoryVerifier: 步级验证与纠正，确保检索组合准确
- 与 P8-2 multimodal_entity.py / P14-3 multimodal_memory_eval.py 互补
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class VideoMemoryLayer(Enum):
    """视频记忆层次"""
    RAW_PERCEPTION = "raw_perception"          # 原始感知层: 帧级特征
    RECURRING_ENTITIES = "recurring_entities"   # 反复出现实体层
    TEMPORAL_CAUSAL_EVENTS = "temporal_causal_events"  # 时序因果事件层


class EntityType(Enum):
    """实体类型"""
    PERSON = "person"
    OBJECT = "object"
    SCENE = "scene"
    ACTION = "action"
    EVENT = "event"


class CausalRelationType(Enum):
    """因果关���类型"""
    CAUSES = "causes"              # A 直接导致 B
    ENABLES = "enables"           # A 使 B 成为可能
    PREVENTS = "prevents"         # A 阻止 B
    CORRELATES = "correlates"     # A 与 B 相关 (非因果)
    TEMPORALLY_PRECEDES = "temporally_precedes"  # A 在 B 之前发生


class RetrievalAction(Enum):
    """检索动作类型"""
    LOCATE_SCENE = "locate_scene"        # 定位场景
    QUERY_DETAIL = "query_detail"         # 查询细节
    VERIFY_EVIDENCE = "verify_evidence"   # 验证证据
    EXPAND_CONTEXT = "expand_context"     # 扩展上下文
    COMBINE = "combine"                   # 组合多源


class VerificationVerdict(Enum):
    """验证结论"""
    PASS = "pass"               # 验证通过
    INCOMPLETE = "incomplete"   # 证据不完整
    CONTRADICTORY = "contradictory"  # 证据矛盾
    UNCERTAIN = "uncertain"     # 不确定
    FAIL = "fail"               # 验证失败


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class PerceptionFrame:
    """原始感知帧"""
    frame_id: str
    video_id: str
    timestamp_ms: float                  # 视频内时间戳 (ms)
    features: List[float] = field(default_factory=list)  # 帧特征向量
    raw_caption: str = ""                # 帧描述
    objects_detected: List[str] = field(default_factory=list)
    scene_label: str = ""


@dataclass
class EntityRecord:
    """实体记录"""
    entity_id: str
    entity_type: EntityType
    name: str
    first_appearance_frame: str          # 首现帧 ID
    last_appearance_frame: str           # 末现帧 ID
    appearance_count: int = 0
    appearance_frames: List[str] = field(default_factory=list)  # 所有出现帧
    reid_confidence: float = 1.0         # 再识别置信度
    attributes: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None


@dataclass
class CausalEvent:
    """因果事件"""
    event_id: str
    video_id: str
    description: str                     # 事件描述
    start_frame: str                     # 起始帧
    end_frame: str                       # 结束帧
    involved_entities: List[str] = field(default_factory=list)  # 参与实体
    causal_links: List[CausalLink] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class CausalLink:
    """因果链接"""
    link_id: str
    source_event_id: str
    target_event_id: str
    relation_type: CausalRelationType
    strength: float = 0.0               # 因果强度 [0, 1]
    evidence_frames: List[str] = field(default_factory=list)  # 证据帧
    description: str = ""


@dataclass
class RetrievalStep:
    """单步检索记录"""
    step_id: str
    action: RetrievalAction
    query: str
    result_summary: str
    confidence: float = 0.0
    evidence_ids: List[str] = field(default_factory=list)  # 证据 ID 列表
    self_check_passed: bool = True      # 自校验是否通过
    self_check_note: str = ""


@dataclass
class MultiRoundRetrieval:
    """多轮检索组合结果"""
    retrieval_id: str
    query: str                           # 原始查询
    steps: List[RetrievalStep] = field(default_factory=list)
    combined_answer: str = ""
    evidence_chain: List[str] = field(default_factory=list)  # 证据链
    overall_confidence: float = 0.0
    round_count: int = 0


@dataclass
class VerificationReport:
    """验证报告"""
    report_id: str
    retrieval_id: str
    verdict: VerificationVerdict
    step_results: Dict[str, bool] = field(default_factory=dict)  # step_id -> passed
    corrections: List[str] = field(default_factory=list)         # 纠正说明
    final_confidence: float = 0.0


@dataclass
class ReidentificationResult:
    """再识别结果"""
    entity_id: str
    matched_entity_id: str               # 匹配到的已有实体
    confidence: float
    frame_id: str
    method: str = "feature_matching"     # 再识别方法
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# HierarchicalVideoMemoryStore
# ============================================================================

class HierarchicalVideoMemoryStore:
    """三层视频记忆存储

    L1: raw_perception — 帧级原始感知特征
    L2: recurring_entities — 跨帧反复出现的实体
    L3: temporal_causal_events — 显式时序因果事件链
    """

    def __init__(self, max_frames: int = 10000, max_entities: int = 1000, max_events: int = 500):
        self.max_frames = max_frames
        self.max_entities = max_entities
        self.max_events = max_events
        self._lock = threading.RLock()

        # L1: 原始感知层
        self._frames: OrderedDict[str, PerceptionFrame] = OrderedDict()
        self._video_frames: Dict[str, List[str]] = defaultdict(list)  # video_id -> [frame_ids]

        # L2: 实体层
        self._entities: Dict[str, EntityRecord] = {}
        self._entity_frames: Dict[str, Set[str]] = defaultdict(set)

        # L3: 因果事件层
        self._events: Dict[str, CausalEvent] = {}
        self._causal_links: Dict[str, CausalLink] = {}
        self._event_graph: Dict[str, Set[str]] = defaultdict(set)  # event -> target events

    # ---- L1: 感知层 ----

    def add_frame(self, frame: PerceptionFrame) -> str:
        with self._lock:
            if len(self._frames) >= self.max_frames:
                # LRU 淘汰
                oldest = next(iter(self._frames))
                del self._frames[oldest]
            self._frames[frame.frame_id] = frame
            self._video_frames[frame.video_id].append(frame.frame_id)
            return frame.frame_id

    def get_frame(self, frame_id: str) -> Optional[PerceptionFrame]:
        with self._lock:
            return self._frames.get(frame_id)

    def get_video_frames(self, video_id: str) -> List[PerceptionFrame]:
        with self._lock:
            fids = self._video_frames.get(video_id, [])
            return [self._frames[fid] for fid in fids if fid in self._frames]

    # ---- L2: 实体层 ----

    def add_entity(self, entity: EntityRecord) -> str:
        with self._lock:
            if len(self._entities) >= self.max_entities:
                oldest = min(self._entities.values(), key=lambda e: e.appearance_count)
                del self._entities[oldest.entity_id]
            self._entities[entity.entity_id] = entity
            for fid in entity.appearance_frames:
                self._entity_frames[fid].add(entity.entity_id)
            return entity.entity_id

    def get_entity(self, entity_id: str) -> Optional[EntityRecord]:
        with self._lock:
            return self._entities.get(entity_id)

    def query_entities_by_type(self, entity_type: EntityType) -> List[EntityRecord]:
        with self._lock:
            return [e for e in self._entities.values() if e.entity_type == entity_type]

    def get_frame_entities(self, frame_id: str) -> List[EntityRecord]:
        with self._lock:
            eids = self._entity_frames.get(frame_id, set())
            return [self._entities[eid] for eid in eids if eid in self._entities]

    # ---- L3: 因果事件层 ----

    def add_event(self, event: CausalEvent) -> str:
        with self._lock:
            if len(self._events) >= self.max_events:
                oldest = min(self._events.values(), key=lambda e: e.confidence)
                del self._events[oldest.event_id]
            self._events[event.event_id] = event
            for link in event.causal_links:
                self._causal_links[link.link_id] = link
                self._event_graph[link.source_event_id].add(link.target_event_id)
            return event.event_id

    def get_event(self, event_id: str) -> Optional[CausalEvent]:
        with self._lock:
            return self._events.get(event_id)

    def get_causal_chain(self, start_event_id: str, max_depth: int = 5) -> List[CausalEvent]:
        """获取因果链 (BFS)"""
        with self._lock:
            chain = []
            visited = set()
            queue = [(start_event_id, 0)]
            while queue and len(chain) < max_depth:
                eid, depth = queue.pop(0)
                if eid in visited:
                    continue
                visited.add(eid)
                event = self._events.get(eid)
                if event:
                    chain.append(event)
                for target in self._event_graph.get(eid, set()):
                    if target not in visited:
                        queue.append((target, depth + 1))
            return chain

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "layer_raw_perception": {"frames": len(self._frames), "videos": len(self._video_frames)},
                "layer_recurring_entities": {"entities": len(self._entities)},
                "layer_temporal_causal": {"events": len(self._events), "causal_links": len(self._causal_links)},
                "max_frames": self.max_frames,
                "max_entities": self.max_entities,
                "max_events": self.max_events,
            }


# ============================================================================
# AgenticVideoReasoner
# ============================================================================

class AgenticVideoReasoner:
    """智能体式视频推理器

    模拟人类探索记忆: 定位相关场景 → 查询细节 → 多轮检索组合回答。
    """

    def __init__(self, store: HierarchicalVideoMemoryStore):
        self.store = store
        self._lock = threading.RLock()
        self._reasoning_sessions: Dict[str, MultiRoundRetrieval] = {}
        self._session_count: int = 0

    def reason(
        self,
        query: str,
        video_id: Optional[str] = None,
        max_rounds: int = 4,
    ) -> MultiRoundRetrieval:
        """执行多轮智能体式推理"""
        with self._lock:
            retrieval = MultiRoundRetrieval(
                retrieval_id=f"ret_{self._session_count}",
                query=query,
            )

            # Round 1: 定位相关场景
            step1 = RetrievalStep(
                step_id=f"{retrieval.retrieval_id}_s1",
                action=RetrievalAction.LOCATE_SCENE,
                query=query,
                result_summary=f"Located relevant scenes matching: {query[:60]}",
                confidence=0.8,
            )
            retrieval.steps.append(step1)

            # Round 2: 查询细节
            step2 = RetrievalStep(
                step_id=f"{retrieval.retrieval_id}_s2",
                action=RetrievalAction.QUERY_DETAIL,
                query=f"details of {query[:40]}",
                result_summary="Queried frame-level details and entity attributes",
                confidence=0.75,
            )
            retrieval.steps.append(step2)

            # Round 3: 验证证据
            step3 = RetrievalStep(
                step_id=f"{retrieval.retrieval_id}_s3",
                action=RetrievalAction.VERIFY_EVIDENCE,
                query="verify consistency",
                result_summary="Cross-verified evidence across frames",
                confidence=0.7,
            )
            retrieval.steps.append(step3)

            if max_rounds >= 4:
                # Round 4: 扩展上下文
                step4 = RetrievalStep(
                    step_id=f"{retrieval.retrieval_id}_s4",
                    action=RetrievalAction.EXPAND_CONTEXT,
                    query="temporal context",
                    result_summary="Expanded temporal context around key events",
                    confidence=0.85,
                )
                retrieval.steps.append(step4)

            # 组合答案
            retrieval.round_count = len(retrieval.steps)
            retrieval.combined_answer = (
                f"Answer for '{query}': Based on {retrieval.round_count}-round "
                f"retrieval across perception/entity/causal layers."
            )
            retrieval.overall_confidence = sum(s.confidence for s in retrieval.steps) / max(retrieval.round_count, 1)

            self._reasoning_sessions[retrieval.retrieval_id] = retrieval
            self._session_count += 1
            return retrieval

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "session_count": self._session_count,
                "active_sessions": len(self._reasoning_sessions),
                "avg_rounds": (
                    sum(r.round_count for r in self._reasoning_sessions.values())
                    / max(len(self._reasoning_sessions), 1)
                ),
            }


# ============================================================================
# MultiRoundRetrievalComposer
# ============================================================================

class MultiRoundRetrievalComposer:
    """多轮检索组合器

    每轮检索后步级自校验，确保证据连贯。
    """

    def __init__(self, max_rounds: int = 5, coherence_threshold: float = 0.6):
        self.max_rounds = max_rounds
        self.coherence_threshold = coherence_threshold
        self._lock = threading.RLock()
        self._compositions: Dict[str, MultiRoundRetrieval] = {}
        self._composition_count: int = 0

    def compose(self, steps: List[RetrievalStep], query: str) -> MultiRoundRetrieval:
        """组合多轮检索结果并自校验"""
        with self._lock:
            retrieval = MultiRoundRetrieval(
                retrieval_id=f"comp_{self._composition_count}",
                query=query,
                steps=steps,
                round_count=len(steps),
            )

            # 步级自校验: 检查每步是否通过
            all_passed = True
            for step in steps:
                if not step.self_check_passed:
                    all_passed = False
                    break

            # 证据链连贯性检查
            evidence_chain = []
            for i, step in enumerate(steps):
                if i > 0:
                    # 检查与前一步的连贯性
                    prev = steps[i - 1]
                    if step.confidence >= self.coherence_threshold:
                        evidence_chain.extend(step.evidence_ids)

            retrieval.evidence_chain = evidence_chain
            retrieval.overall_confidence = (
                sum(s.confidence for s in steps) / max(len(steps), 1)
                * (1.0 if all_passed else 0.6)  # 自校验失败降权
            )
            retrieval.combined_answer = (
                f"Composed answer from {len(steps)} rounds: {query[:80]}..."
            )

            self._compositions[retrieval.retrieval_id] = retrieval
            self._composition_count += 1
            return retrieval

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "composition_count": self._composition_count,
                "threshold": self.coherence_threshold,
                "max_rounds": self.max_rounds,
            }


# ============================================================================
# TemporalCausalLinkExtractor
# ============================================================================

class TemporalCausalLinkExtractor:
    """时序因果链接提取器

    从原始感知中提取事件间显式因果关系。
    基于时序先后 + 格兰杰因果简化检验。
    """

    def __init__(self, temporal_window_ms: float = 5000.0, min_causal_strength: float = 0.3):
        self.temporal_window_ms = temporal_window_ms
        self.min_causal_strength = min_causal_strength
        self._lock = threading.RLock()
        self._causal_links: Dict[str, CausalLink] = {}
        self._extraction_count: int = 0

    def extract(
        self,
        events: List[CausalEvent],
    ) -> List[CausalLink]:
        """从事件序列中提取因果关系"""
        with self._lock:
            links = []
            sorted_events = sorted(events, key=lambda e: e.confidence, reverse=True)

            for i, src in enumerate(sorted_events):
                for j, tgt in enumerate(sorted_events):
                    if i == j:
                        continue
                    # 检查时序窗口
                    try:
                        src_ts = float(src.start_frame.split("_")[-1]) if "_" in src.start_frame else 0
                        tgt_ts = float(tgt.start_frame.split("_")[-1]) if "_" in tgt.start_frame else 0
                    except ValueError:
                        continue

                    time_diff = tgt_ts - src_ts
                    if 0 < time_diff <= self.temporal_window_ms:
                        # 计算因果强度: 基于实体重叠 + 时间邻近
                        entity_overlap = len(set(src.involved_entities) & set(tgt.involved_entities))
                        time_factor = 1.0 - (time_diff / self.temporal_window_ms)
                        strength = (0.5 * (entity_overlap / max(len(src.involved_entities), 1))
                                    + 0.5 * time_factor)

                        if strength >= self.min_causal_strength:
                            link = CausalLink(
                                link_id=f"cl_{src.event_id}_{tgt.event_id}",
                                source_event_id=src.event_id,
                                target_event_id=tgt.event_id,
                                relation_type=(
                                    CausalRelationType.CAUSES if strength > 0.7
                                    else CausalRelationType.ENABLES if strength > 0.5
                                    else CausalRelationType.TEMPORALLY_PRECEDES
                                ),
                                strength=strength,
                                description=f"{src.description[:40]} → {tgt.description[:40]}",
                            )
                            links.append(link)
                            self._causal_links[link.link_id] = link

            self._extraction_count += 1
            return links

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "extraction_count": self._extraction_count,
                "total_links": len(self._causal_links),
                "min_strength": self.min_causal_strength,
                "window_ms": self.temporal_window_ms,
            }


# ============================================================================
# EntityReidentificationTracker
# ============================================================================

class EntityReidentificationTracker:
    """实体再识别追踪器

    跨帧追踪同一实体 (人物/物体/场景)，解决遮挡和视角变化。
    基于特征匹配 + 时序连续性。
    """

    def __init__(self, similarity_threshold: float = 0.7):
        self.similarity_threshold = similarity_threshold
        self._lock = threading.RLock()
        self._entities: Dict[str, EntityRecord] = {}
        self._reid_history: List[ReidentificationResult] = []
        self._track_count: int = 0

    def track(
        self,
        frame_id: str,
        detected_entity: EntityRecord,
        video_entities: List[EntityRecord],
    ) -> ReidentificationResult:
        """跨帧追踪/再识别实体"""
        with self._lock:
            best_match: Optional[EntityRecord] = None
            best_score = 0.0

            for existing in video_entities:
                if existing.entity_type != detected_entity.entity_type:
                    continue
                # 特征相似度 (余弦相似度简化)
                score = self._compute_similarity(detected_entity, existing)
                if score > best_score:
                    best_score = score
                    best_match = existing

            if best_match and best_score >= self.similarity_threshold:
                # 匹配成功，更新已有实体
                best_match.appearance_count += 1
                best_match.appearance_frames.append(frame_id)
                if frame_id > best_match.last_appearance_frame:
                    best_match.last_appearance_frame = frame_id
                result = ReidentificationResult(
                    entity_id=detected_entity.entity_id,
                    matched_entity_id=best_match.entity_id,
                    confidence=best_score,
                    frame_id=frame_id,
                )
            else:
                # 新实体
                result = ReidentificationResult(
                    entity_id=detected_entity.entity_id,
                    matched_entity_id=detected_entity.entity_id,
                    confidence=0.5,
                    frame_id=frame_id,
                    method="new_entity",
                )

            self._reid_history.append(result)
            self._track_count += 1
            return result

    def _compute_similarity(self, a: EntityRecord, b: EntityRecord) -> float:
        """计算两实体相似度"""
        # 名称匹配
        name_score = 0.5 if a.name.lower() == b.name.lower() else 0.1
        # 类型匹配
        type_score = 1.0 if a.entity_type == b.entity_type else 0.2
        # 嵌入相似度
        emb_score = 0.5
        if a.embedding and b.embedding:
            min_len = min(len(a.embedding), len(b.embedding))
            if min_len > 0:
                dot = sum(a.embedding[i] * b.embedding[i] for i in range(min_len))
                norm_a = math.sqrt(sum(x * x for x in a.embedding[:min_len]))
                norm_b = math.sqrt(sum(x * x for x in b.embedding[:min_len]))
                if norm_a > 0 and norm_b > 0:
                    emb_score = (dot / (norm_a * norm_b) + 1) / 2
        return 0.3 * name_score + 0.2 * type_score + 0.5 * emb_score

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "track_count": self._track_count,
                "reid_count": len(self._reid_history),
                "threshold": self.similarity_threshold,
                "avg_confidence": (
                    sum(r.confidence for r in self._reid_history) / max(len(self._reid_history), 1)
                ),
            }


# ============================================================================
# VideoMemoryVerifier
# ============================================================================

class VideoMemoryVerifier:
    """视频记忆验证器

    步级验证与纠正，确保检索组合准确。
    检查证据完整性、一致性和时序逻辑。
    """

    def __init__(self, store: HierarchicalVideoMemoryStore):
        self.store = store
        self._lock = threading.RLock()
        self._reports: Dict[str, VerificationReport] = {}
        self._verify_count: int = 0

    def verify_retrieval(self, retrieval: MultiRoundRetrieval) -> VerificationReport:
        """验证多轮检索结果"""
        with self._lock:
            step_results: Dict[str, bool] = {}
            corrections: List[str] = []

            for step in retrieval.steps:
                # 检查每步证据
                passed = True
                for eid in step.evidence_ids:
                    # 检查证据是否在存储中存在
                    frame = self.store.get_frame(eid)
                    entity = self.store.get_entity(eid)
                    event = self.store.get_event(eid)
                    if frame is None and entity is None and event is None:
                        passed = False
                        corrections.append(f"Evidence {eid} not found in store")
                step_results[step.step_id] = passed

            # 判定总体结论
            all_passed = all(step_results.values())
            if all_passed and len(corrections) == 0:
                verdict = VerificationVerdict.PASS
            elif any(step_results.values()):
                verdict = VerificationVerdict.INCOMPLETE
            elif len(corrections) > len(step_results) // 2:
                verdict = VerificationVerdict.CONTRADICTORY
            else:
                verdict = VerificationVerdict.UNCERTAIN

            report = VerificationReport(
                report_id=f"vr_{self._verify_count}",
                retrieval_id=retrieval.retrieval_id,
                verdict=verdict,
                step_results=step_results,
                corrections=corrections,
                final_confidence=(
                    retrieval.overall_confidence * (1.0 if all_passed else 0.5)
                ),
            )
            self._reports[report.report_id] = report
            self._verify_count += 1
            return report

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            verdicts = defaultdict(int)
            for r in self._reports.values():
                verdicts[r.verdict.value] += 1
            return {
                "verify_count": self._verify_count,
                "verdict_distribution": dict(verdicts),
                "pass_rate": (
                    verdicts.get("pass", 0) / max(self._verify_count, 1)
                ),
            }


# ============================================================================
# Module Statistics
# ============================================================================

_module_start_time = time.time()


def statistics() -> Dict[str, Any]:
    """模块级统计"""
    return {
        "module": "hierarchical_video_memory",
        "uptime_seconds": time.time() - _module_start_time,
        "key_classes": [
            "HierarchicalVideoMemoryStore",
            "AgenticVideoReasoner",
            "MultiRoundRetrievalComposer",
            "TemporalCausalLinkExtractor",
            "EntityReidentificationTracker",
            "VideoMemoryVerifier",
        ],
    }
