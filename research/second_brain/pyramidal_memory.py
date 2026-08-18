"""
# status: orphan (2026-08-15 audit, not in runtime path)
P5-1 + P5-3: Pyramidal Multimodal Memory + SIB Adaptive Compression (对标 MM-Mem ACL2026)
============================================================================================

基于模糊痕迹理论 (Fuzzy-Trace Theory) 实现三级记忆金字塔架构，
实现 verbatim → gist 的逐级蒸馏管线，引入语义信息瓶颈 (SIB) 目标函数
和 SIB-GRPO 优化策略，使压缩率自动适配当前任务的信息需求。

MM-Mem 核心设计：
  - L0 Sensory Buffer: 保存原始逐字痕迹 (verbatim)，容量有限、时间衰减快
  - L1 Episodic Stream: 结构化事件序列，定时触发蒸馏，保留关键上下文
  - L2 Symbolic Schema: 高层语义抽象 (gist)，长期稳定存储，支持推理

蒸馏机制：
  - Verbatim → Gist 管线：每级蒸馏由 SIB 目标函数约束
  - SIB 目标：min I(X; T_i) − β·I(T_i; Y)，其中 T_i 为第 i 级表征
  - SIB-GRPO：分组相对策略优化，自适应调整压缩率 β

检索策略：
  - 熵驱动自顶向下：先从 Schema 层检索，熵高于阈值则下沉到 Episodic，
    仍不满足再下沉到 Sensory Buffer

Reference: Lian et al., "From Verbatim to Gist: Distilling Pyramidal Multimodal
           Memory via Semantic Information Bottleneck for Long-Horizon Video Agents"
           ACL 2026 Long Paper, pp. 11601-11617.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── 枚举与常量 ───────────────────────────────────────────────────────

class MemoryLevel(Enum):
    """记忆金字塔层级。"""
    SENSORY_BUFFER = 0   # L0: 原始逐字痕迹
    EPISODIC_STREAM = 1  # L1: 结构化事件序列
    SYMBOLIC_SCHEMA = 2  # L2: 高层语义抽象 (gist)


class TraceType(Enum):
    """痕迹类型：verba（逐字）或 gist（要义）。"""
    VERBATIM = "verbatim"
    GIST = "gist"
    HYBRID = "hybrid"  # 中间态（蒸馏未完成）


class DistillationPhase(Enum):
    """蒸馏阶段。"""
    IDLE = auto()
    ACCUMULATING = auto()     # 累积足够素材
    DISTILLING = auto()       # 正在蒸馏
    CONSOLIDATING = auto()    # 蒸馏后整合


class SIBMode(Enum):
    """语义信息瓶颈工作模式。"""
    FIXED_THRESHOLD = "fixed"       # 固定压缩率（传统模式）
    ADAPTIVE_BETA = "adaptive"      # 自适应 β 调度
    GRPO_OPTIMIZED = "grpo"         # SIB-GRPO 优化


# ── 数据结构 ─────────────────────────────────────────────────────────

@dataclass
class SensoryTrace:
    trace_id: str
    content: str
    timestamp: float = field(default_factory=time.time)
    modality: str = "text"
    activation: float = 1.0
    decay_rate: float = 0.15
    entropy: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EpisodicEvent:
    event_id: str
    summary: str
    source_traces: List[str] = field(default_factory=list)
    participants: List[str] = field(default_factory=list)
    causal_links: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    confidence: float = 0.5
    retained_keywords: List[str] = field(default_factory=list)
    scene_context: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SymbolicSchemaEntry:
    schema_id: str
    concept: str
    description: str
    source_events: List[str] = field(default_factory=list)
    confidence: float = 0.5
    stability: float = 0.3
    references: List[str] = field(default_factory=list)
    activation_count: int = 0
    last_activated: float = field(default_factory=time.time)
    contradiction_schemas: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SIBConfig:
    mode: SIBMode = SIBMode.GRPO_OPTIMIZED
    beta_init: float = 0.5
    beta_min: float = 0.05
    beta_max: float = 5.0
    grpo_learning_rate: float = 0.01
    grpo_clip_epsilon: float = 0.2
    entropy_threshold_high: float = 0.7
    entropy_threshold_low: float = 0.3
    distillation_batch_size: int = 8
    distillation_interval: float = 300.0
    max_sensory_capacity: int = 100
    max_episodic_capacity: int = 500
    max_schema_capacity: int = 200


@dataclass
class RetrievalTrace:
    query: str
    level_accessed: MemoryLevel
    retrieved_ids: List[str]
    entropy_at_retrieval: float
    success: bool
    user_feedback: float = 0.0
    latency_ms: float = 0.0


# ── 工具函数 ─────────────────────────────────────────────────────────

def estimate_entropy(tokens: List[str]) -> float:
    """简易 Token 熵估算（用于检索下沉判断）。"""
    if not tokens:
        return 0.0
    total = len(tokens)
    unique = len(set(tokens))
    return min(1.0, unique / total * math.log2(max(total, 1)) / 8.0)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """余弦相似度，带零向量保护。"""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


# ── _SummaryCompressor ────────────────────────────────────────────────

class _SummaryCompressor:
    """L0→L1 蒸馏压缩器 + SIB-GRPO 自适应优化。

    负责将感知缓冲中的原始痕迹蒸馏为情景事件流，
    并通过语义信息瓶颈 (SIB) 目标函数与 GRPO 策略
    自适应调整压缩率 β。
    """

    def __init__(self, parent: "PyramidalMemory") -> None:
        self._parent = parent

    def trigger_distillation(self, force: bool = False) -> int:
        """触发 L0 → L1 蒸馏，将一批感知痕迹聚合为情景事件。"""
        with self._parent._lock:
            now = time.time()
            if not force:
                if len(self._parent._accumulated_trace_ids) < self._parent.config.distillation_batch_size:
                    return 0
                if (now - self._parent._last_distillation_time) < self._parent.config.distillation_interval:
                    return 0

            self._parent._phase = DistillationPhase.DISTILLING
            batch_ids = self._parent._accumulated_trace_ids[-self._parent.config.distillation_batch_size:]
            self._parent._accumulated_trace_ids = self._parent._accumulated_trace_ids[self._parent.config.distillation_batch_size:]

            traces: List[SensoryTrace] = []
            for tid in batch_ids:
                t = self._parent.get_sensory_trace(tid)
                if t:
                    traces.append(t)

            if not traces:
                self._parent._phase = DistillationPhase.IDLE
                return 0

            new_events = self._distill_batch(traces)
            self._parent._last_distillation_time = now

            while len(self._parent._episodic_stream) > self._parent.config.max_episodic_capacity:
                oldest_id = self._parent._event_order.pop(0) if self._parent._event_order else None
                if oldest_id:
                    self._parent._episodic_stream.pop(oldest_id, None)

            self._parent._phase = DistillationPhase.IDLE
            self._parent._total_distilled += len(new_events)
            return len(new_events)

    def _distill_batch(self, traces: List[SensoryTrace]) -> List[EpisodicEvent]:
        """将一批 L0 痕迹蒸馏为 L1 情景事件（verba → structured event）。"""
        if not traces:
            return []

        groups: List[List[SensoryTrace]] = []
        sorted_traces = sorted(traces, key=lambda t: t.timestamp)
        current_group = [sorted_traces[0]]

        for trace in sorted_traces[1:]:
            if trace.timestamp - current_group[-1].timestamp < 60.0:
                current_group.append(trace)
            else:
                groups.append(current_group)
                current_group = [trace]
        groups.append(current_group)

        events: List[EpisodicEvent] = []
        for group in groups:
            event_id = f"ev_{uuid.uuid4().hex[:12]}"
            combined = " ".join(t.content[:200] for t in group)
            keywords = list(set(
                kw for t in group
                for kw in t.content.split()
                if len(kw) > 3 and kw.isalpha()
            ))[:10]

            summary = self._generate_summary(combined, len(group))

            sib_loss = self._compute_sib_loss(
                original_len=sum(len(t.content) for t in group),
                compressed_len=len(summary),
                semantic_retention=self._estimate_retention(group, summary),
            )
            self._parent._sib_loss_history.append(sib_loss)
            self._grpo_update_beta(sib_loss)

            event = EpisodicEvent(
                event_id=event_id,
                summary=summary,
                source_traces=[t.trace_id for t in group],
                participants=self._extract_participants(combined),
                timestamp=group[0].timestamp,
                confidence=1.0 - min(sib_loss / 10.0, 0.95),
                retained_keywords=keywords,
                scene_context=self._extract_scene_context(combined),
            )

            self._parent._episodic_stream[event_id] = event
            self._parent._event_order.append(event_id)
            events.append(event)

        return events

    def _generate_summary(self, combined_text: str, trace_count: int) -> str:
        """生成事件摘要。启发式压缩：保留首句 + 关键实体 + 动作。"""
        sentences = combined_text.replace("\n", " ").split("。")
        key_sentence = sentences[0].strip() if sentences else combined_text[:200]
        if len(key_sentence) > 300:
            key_sentence = key_sentence[:300] + "..."
        return f"[{trace_count}条痕迹] {key_sentence}"

    def _extract_participants(self, text: str) -> List[str]:
        """简易参与实体提取。"""
        candidates = re.findall(r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*\b', text)
        return list(dict.fromkeys(candidates))[:5]

    def _extract_scene_context(self, text: str) -> str:
        """提取场景上下文。"""
        return text[:200] if len(text) > 200 else text

    def _estimate_retention(self, traces: List[SensoryTrace], summary: str) -> float:
        """估算语义保留率（简化：基于关键词覆盖率）。"""
        if not traces:
            return 0.0
        original_kw = set()
        for t in traces:
            original_kw.update(w.lower() for w in t.content.split() if len(w) > 3)
        summary_kw = set(w.lower() for w in summary.split() if len(w) > 3)
        if not original_kw:
            return 1.0
        return len(summary_kw & original_kw) / len(original_kw)

    def _compute_sib_loss(
        self,
        original_len: int,
        compressed_len: int,
        semantic_retention: float,
    ) -> float:
        """计算语义信息瓶颈损失。SIB 目标：min I(X; T_i) − β · I(T_i; Y)。"""
        if original_len <= 0:
            return 0.0
        compression_ratio = compressed_len / max(original_len, 1)
        compression_loss = 1.0 - math.exp(-3.0 * compression_ratio)
        retention_loss = 1.0 - semantic_retention
        beta = self._parent._current_beta
        return float(compression_loss + beta * retention_loss)

    def _grpo_update_beta(self, sib_loss: float) -> None:
        """SIB-GRPO：分组相对策略优化自适应调整 β。"""
        self._parent._grpo_advantage_buffer.append(sib_loss)
        if len(self._parent._grpo_advantage_buffer) < 4:
            return
        recent = list(self._parent._grpo_advantage_buffer)
        mean_loss = float(np.mean(recent))
        if self._parent._sib_loss_history:
            hist_mean = float(np.mean(self._parent._sib_loss_history[-20:]))
        else:
            hist_mean = mean_loss
        advantage = hist_mean - mean_loss
        lr = self._parent.config.grpo_learning_rate
        eps = self._parent.config.grpo_clip_epsilon
        if advantage > 0:
            delta = lr * min(advantage, eps * abs(self._parent._current_beta))
        else:
            delta = -lr * min(abs(advantage), eps * abs(self._parent._current_beta))
        old_beta = self._parent._current_beta
        self._parent._current_beta = max(
            self._parent.config.beta_min,
            min(self._parent.config.beta_max, self._parent._current_beta + delta),
        )
        if abs(self._parent._current_beta - old_beta) > 0.001:
            logger.debug(
                f"SIB-GRPO: β {old_beta:.4f} → {self._parent._current_beta:.4f} "
                f"(advantage={advantage:.4f}, mean_loss={mean_loss:.4f})"
            )


# ── _LevelRouter ──────────────────────────────────────────────────────

class _LevelRouter:
    """L2 整合 + 熵驱动检索 + 衰减维护。

    负责 L1→L2 符号图式抽象、自顶向下检索路由、
    以及全层级时间衰减调度。
    """

    def __init__(self, parent: "PyramidalMemory") -> None:
        self._parent = parent

    def consolidate_to_schema(self, event_ids: Optional[List[str]] = None) -> int:
        """L1 → L2 整合：将情景事件抽象为符号图式。"""
        with self._parent._lock:
            self._parent._phase = DistillationPhase.CONSOLIDATING
            if event_ids:
                target_ids = [eid for eid in event_ids if eid in self._parent._episodic_stream]
            else:
                target_ids = self._parent._event_order[-20:]
            if not target_ids:
                self._parent._phase = DistillationPhase.IDLE
                return 0
            events = [self._parent._episodic_stream[eid] for eid in target_ids]
            new_schemas = self._abstract_schemas(events)
            while len(self._parent._symbolic_schema) > self._parent.config.max_schema_capacity:
                sorted_schemas = sorted(
                    self._parent._symbolic_schema.values(),
                    key=lambda s: (s.stability, s.last_activated),
                )
                to_remove = sorted_schemas[0]
                self._parent._symbolic_schema.pop(to_remove.schema_id, None)
            self._parent._phase = DistillationPhase.IDLE
            return len(new_schemas)

    def _abstract_schemas(self, events: List[EpisodicEvent]) -> List[SymbolicSchemaEntry]:
        """从事件列表抽象符号图式。"""
        if not events:
            return []
        participant_groups: Dict[str, List[EpisodicEvent]] = defaultdict(list)
        for ev in events:
            for p in ev.participants:
                participant_groups[p].append(ev)
            if not ev.participants:
                participant_groups["_unknown"].append(ev)
        new_schemas: List[SymbolicSchemaEntry] = []
        for participant, group in participant_groups.items():
            if len(group) < 2:
                continue
            schema_id = f"sc_{uuid.uuid4().hex[:12]}"
            all_keywords = list(set(kw for e in group for kw in e.retained_keywords))[:10]
            is_new = True
            for existing in self._parent._symbolic_schema.values():
                overlap = len(set(all_keywords) & set(existing.metadata.get("keywords", [])))
                if overlap > len(all_keywords) * 0.6:
                    existing.stability = min(1.0, existing.stability + 0.05)
                    existing.activation_count += 1
                    existing.last_activated = time.time()
                    existing.source_events.extend(e.event_id for e in group)
                    existing.metadata.setdefault("keywords", []).extend(all_keywords)
                    existing.metadata["keywords"] = list(set(existing.metadata["keywords"]))
                    is_new = False
                    break
            if is_new:
                concept = participant if participant != "_unknown" else "general"
                schema = SymbolicSchemaEntry(
                    schema_id=schema_id,
                    concept=concept,
                    description=f"抽象自 {len(group)} 个事件: {group[0].summary[:100]}...",
                    source_events=[e.event_id for e in group],
                    confidence=sum(e.confidence for e in group) / len(group),
                    stability=0.3,
                    metadata={"keywords": all_keywords, "participant": participant},
                )
                self._parent._symbolic_schema[schema_id] = schema
                new_schemas.append(schema)
        return new_schemas

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """熵驱动自顶向下检索。"""
        with self._parent._lock:
            results: List[Dict[str, Any]] = []
            query_tokens = query.lower().split()
            schema_entropy: float = float("inf")
            event_entropy: float = float("inf")

            schema_candidates = self._search_schema(query_tokens)
            if schema_candidates:
                schema_entropy = estimate_entropy(
                    [s.description for s, _ in schema_candidates]
                )
                for s, score in schema_candidates[:top_k]:
                    results.append({
                        "level": "L2_SYMBOLIC_SCHEMA",
                        "id": s.schema_id,
                        "content": s.description,
                        "concept": s.concept,
                        "score": score,
                        "confidence": s.confidence,
                        "stability": s.stability,
                    })

            if not results or schema_entropy >= self._parent.config.entropy_threshold_high:
                event_candidates = self._search_episodic(query_tokens)
                if event_candidates:
                    event_entropy = estimate_entropy(
                        [e.summary for e, _ in event_candidates]
                    )
                    for ev, score in event_candidates[:top_k]:
                        results.append({
                            "level": "L1_EPISODIC_STREAM",
                            "id": ev.event_id,
                            "content": ev.summary,
                            "participants": ev.participants,
                            "score": score,
                            "confidence": ev.confidence,
                        })

            if not results or event_entropy >= self._parent.config.entropy_threshold_high:
                sensory_candidates = self._search_sensory(query_tokens)
                for trace, score in sensory_candidates[:top_k]:
                    results.append({
                        "level": "L0_SENSORY_BUFFER",
                        "id": trace.trace_id,
                        "content": trace.content[:500],
                        "score": score,
                        "activation": trace.activation,
                    })

            results.sort(key=lambda r: r.get("score", 0), reverse=True)
            final = results[:top_k]

            retrieval_trace = RetrievalTrace(
                query=query,
                level_accessed=(
                    MemoryLevel.SYMBOLIC_SCHEMA if schema_candidates
                    else MemoryLevel.EPISODIC_STREAM if event_candidates
                    else MemoryLevel.SENSORY_BUFFER
                ),
                retrieved_ids=[r["id"] for r in final],
                entropy_at_retrieval=schema_entropy,
                success=len(final) > 0,
                latency_ms=0.0,
            )
            self._parent._retrieval_history.append(retrieval_trace)
            self._parent._total_retrieved += 1

            for r in final:
                if r["level"] == "L2_SYMBOLIC_SCHEMA":
                    schema = self._parent._symbolic_schema.get(r["id"])
                    if schema:
                        schema.activation_count += 1
                        schema.last_activated = time.time()

            return final

    def _search_schema(self, query_tokens: List[str]) -> List[Tuple[SymbolicSchemaEntry, float]]:
        """L2 概念匹配检索。"""
        scored: List[Tuple[SymbolicSchemaEntry, float]] = []
        for schema in self._parent._symbolic_schema.values():
            desc_tokens = schema.description.lower().split()
            concept_tokens = schema.concept.lower().split()
            all_tokens = set(desc_tokens + concept_tokens)
            overlap = len(set(query_tokens) & all_tokens)
            if overlap > 0:
                score = (
                    overlap / max(len(query_tokens), 1) * 0.5
                    + schema.stability * 0.3
                    + schema.confidence * 0.2
                )
                scored.append((schema, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def _search_episodic(self, query_tokens: List[str]) -> List[Tuple[EpisodicEvent, float]]:
        """L1 事件关键词 + 时间序检索。"""
        scored: List[Tuple[EpisodicEvent, float]] = []
        for event in self._parent._episodic_stream.values():
            summary_tokens = event.summary.lower().split()
            kw_tokens = [k.lower() for k in event.retained_keywords]
            all_tokens = set(summary_tokens + kw_tokens)
            overlap = len(set(query_tokens) & all_tokens)
            if overlap > 0:
                recency = math.exp(-0.01 * (time.time() - event.timestamp))
                score = (
                    overlap / max(len(query_tokens), 1) * 0.6
                    + recency * 0.3
                    + event.confidence * 0.1
                )
                scored.append((event, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def _search_sensory(self, query_tokens: List[str]) -> List[Tuple[SensoryTrace, float]]:
        """L0 直接内容匹配。"""
        scored: List[Tuple[SensoryTrace, float]] = []
        for trace in self._parent._sensory_buffer:
            content_tokens = trace.content.lower().split()
            overlap = len(set(query_tokens) & set(content_tokens))
            if overlap > 0 and trace.activation > 0.05:
                score = (
                    overlap / max(len(query_tokens), 1) * 0.7
                    + trace.activation * 0.3
                )
                scored.append((trace, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def apply_decay(self, dt: Optional[float] = None) -> Dict[str, int]:
        """对所有层级应用时间衰减。"""
        dt = dt or 1.0
        counts = {"L0_evicted": 0, "L1_evicted": 0, "L2_evicted": 0}

        with self._parent._lock:
            # L0 衰减（快速）
            to_remove = []
            for i, trace in enumerate(self._parent._sensory_buffer):
                trace.activation *= math.exp(-trace.decay_rate * dt)
                if trace.activation < 0.05:
                    to_remove.append(i)
            for i in reversed(to_remove):
                evicted = self._parent._sensory_buffer[i]
                self._parent._sensory_buffer.remove(evicted)
                counts["L0_evicted"] += 1

            # L1 衰减（中速）
            l1_to_remove = []
            for eid, event in list(self._parent._episodic_stream.items()):
                age = time.time() - event.timestamp
                retention = math.exp(-0.001 * age) * event.confidence
                if retention < 0.1:
                    l1_to_remove.append(eid)
            for eid in l1_to_remove:
                self._parent._episodic_stream.pop(eid, None)
                if eid in self._parent._event_order:
                    self._parent._event_order.remove(eid)
                counts["L1_evicted"] += 1

            # L2 衰减（最慢）
            l2_to_remove = []
            for sid, schema in list(self._parent._symbolic_schema.items()):
                idle_time = time.time() - schema.last_activated
                if idle_time > 86400 * 30 and schema.stability < 0.5:
                    l2_to_remove.append(sid)
            for sid in l2_to_remove:
                self._parent._symbolic_schema.pop(sid, None)
                counts["L2_evicted"] += 1

        return counts


# ── PyramidalMemory (Facade) ──────────────────────────────────────────

class PyramidalMemory:
    """三级记忆金字塔 (Fuzzy-Trace Theory): L0→L1→L2 verba→gist 蒸馏管线 + SIB-GRPO 自适应压缩。"""

    def __init__(self, config: Optional[SIBConfig] = None):
        self.config = config or SIBConfig()
        self._lock = threading.RLock()
        self._sensory_buffer: deque[SensoryTrace] = deque()
        self._episodic_stream: Dict[str, EpisodicEvent] = {}
        self._symbolic_schema: Dict[str, SymbolicSchemaEntry] = {}
        self._keyword_index: Dict[str, Set[str]] = defaultdict(set)
        self._event_order: List[str] = []
        self._current_beta: float = self.config.beta_init
        self._grpo_advantage_buffer: deque[float] = deque(maxlen=100)
        self._last_distillation_time: float = 0.0
        self._phase: DistillationPhase = DistillationPhase.IDLE
        self._accumulated_trace_ids: List[str] = []
        self._total_ingested: int = 0
        self._total_distilled: int = 0
        self._total_retrieved: int = 0
        self._sib_loss_history: List[float] = []
        self._retrieval_history: List[RetrievalTrace] = []
        self._compressor = _SummaryCompressor(self)
        self._router = _LevelRouter(self)

    def ingest(self, content: str, modality: str = "text",
               metadata: Optional[Dict[str, Any]] = None) -> str:
        """向 L0 感知缓冲注入原始痕迹。"""
        with self._lock:
            trace_id = f"st_{uuid.uuid4().hex[:12]}"
            tokens = content.split()
            trace = SensoryTrace(trace_id=trace_id, content=content, timestamp=time.time(),
                                 modality=modality, entropy=estimate_entropy(tokens),
                                 decay_rate=self.config.beta_min + 0.05, metadata=metadata or {})
            self._sensory_buffer.append(trace)
            for token in set(tokens):
                if len(token) > 2:
                    self._keyword_index[token.lower()].add(trace_id)
            while len(self._sensory_buffer) > self.config.max_sensory_capacity:
                evicted = self._sensory_buffer.popleft()
                for kw, ids in list(self._keyword_index.items()):
                    ids.discard(evicted.trace_id)
                    if not ids:
                        del self._keyword_index[kw]
            self._total_ingested += 1
            self._accumulated_trace_ids.append(trace_id)
            return trace_id

    def get_sensory_trace(self, trace_id: str) -> Optional[SensoryTrace]:
        """按 ID 获取 L0 痕迹。"""
        with self._lock:
            for trace in self._sensory_buffer:
                if trace.trace_id == trace_id:
                    return trace
            return None

    def trigger_distillation(self, force: bool = False) -> int:
        return self._compressor.trigger_distillation(force=force)

    def consolidate_to_schema(self, event_ids: Optional[List[str]] = None) -> int:
        return self._router.consolidate_to_schema(event_ids=event_ids)

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return self._router.retrieve(query=query, top_k=top_k)

    def apply_decay(self, dt: Optional[float] = None) -> Dict[str, int]:
        return self._router.apply_decay(dt=dt)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            rl = self._sib_loss_history[-20:] or [0.0]
            return {"total_ingested": self._total_ingested,
                    "total_distilled": self._total_distilled,
                    "total_retrieved": self._total_retrieved,
                    "l0_buffer_size": len(self._sensory_buffer),
                    "l1_events_count": len(self._episodic_stream),
                    "l2_schemas_count": len(self._symbolic_schema),
                    "current_beta": round(self._current_beta, 4),
                    "sib_mode": self.config.mode.value,
                    "recent_sib_loss_mean": round(float(np.mean(rl)), 4),
                    "recent_sib_loss_std": round(float(np.std(rl)), 4) if len(rl) > 1 else 0.0,
                    "distillation_phase": self._phase.name,
                    "grpo_buffer_size": len(self._grpo_advantage_buffer)}


# ── 便捷工厂 ──────────────────────────────────────────────────────────

def create_pyramidal_memory(
    mode: SIBMode = SIBMode.GRPO_OPTIMIZED,
    beta: float = 0.5,
    sensory_capacity: int = 100,
    episodic_capacity: int = 500,
    schema_capacity: int = 200,
) -> PyramidalMemory:
    """创建 PyramidalMemory 实例的便捷工厂。"""
    config = SIBConfig(
        mode=mode,
        beta_init=beta,
        max_sensory_capacity=sensory_capacity,
        max_episodic_capacity=episodic_capacity,
        max_schema_capacity=schema_capacity,
    )
    return PyramidalMemory(config=config)
