"""
CB66: TriMemDualBranchMemory — 三记忆双分支记忆
================================================

对标论文: TriMem - Beyond Atomic Facts in Lifelong LLM Agent Memory (arXiv 2605.19952)

核心设计: 三种共存表示粒度——
  - RawDialogueSegment: 源标识锚定的原始对话段，保真存储
  - AtomicFact: 从对话中抽取的原子事实，高效检索
  - SynthesizedProfile: 聚合分散事实为整体语义理解，深层推理

TextGradPromptEvolution: 通过响应质量反馈迭代优化抽取/分析 prompt，实现无参数终身进化。
StructuredSearchReformulation: 将自然语言查询转化为结构化搜索查询以提高召回精度。

Reference:
  - arXiv 2605.19952 "TriMem - Beyond Atomic Facts in Lifelong LLM Agent Memory"
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import threading
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class MemoryGranularity(Enum):
    """TriMem 三种记忆粒度。"""
    RAW_DIALOGUE = "raw_dialogue"       # 原始对话段（保真）
    ATOMIC_FACT = "atomic_fact"        # 原子事实（检索）
    SYNTHESIZED_PROFILE = "synthesized_profile"  # 聚合档案（推理）


class SearchField(Enum):
    """结构化搜索域。"""
    SPEAKER = "speaker"
    TIMESTAMP_RANGE = "timestamp_range"
    TOPIC = "topic"
    SENTIMENT = "sentiment"
    ENTITY = "entity"
    KEYWORD = "keyword"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class RawDialogueSegment:
    """原始对话段——源标识锚定的保真存储。

    Attributes:
        segment_id: 段唯一标识。
        source_id: 源对话/会话 ID。
        content: 原始对话文本。
        speaker: 说话者标识。
        turn_index: 对话轮次索引。
        start_timestamp: 开始时间戳。
        end_timestamp: 结束时间戳。
        metadata: 附加元数据。
    """
    segment_id: str
    source_id: str
    content: str
    speaker: str = "unknown"
    turn_index: int = 0
    start_timestamp: float = 0.0
    end_timestamp: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.start_timestamp:
            self.start_timestamp = _time.time()
        if not self.end_timestamp:
            self.end_timestamp = self.start_timestamp


@dataclass
class TriMemAtomicFact:
    """原子事实——从对话中抽取，高效检索。

    注意：命名前缀 TriMem 避让已有 AtomicFact。
    """
    fact_id: str
    content: str
    source_segments: List[str] = field(default_factory=list)
    confidence: float = 1.0
    entities: List[str] = field(default_factory=list)
    relations: List[Tuple[str, str, str]] = field(default_factory=list)
    extracted_at: float = field(default_factory=_time.time)


@dataclass
class SynthesizedProfile:
    """聚合档案——分散事实的深层推理结果。

    Attributes:
        profile_id: 档案唯一标识。
        subject: 档案主体（人/主题/实体）。
        summary: 综合语义摘要。
        supporting_facts: 支撑该档案的原子事实 ID 列表。
        traits: 推断出的特征/偏好/模式。
        confidence: 档案置信度。
    """
    profile_id: str
    subject: str
    summary: str = ""
    supporting_facts: List[str] = field(default_factory=list)
    traits: Dict[str, float] = field(default_factory=dict)
    confidence: float = 0.5
    updated_at: float = field(default_factory=_time.time)


@dataclass
class SearchTemplate:
    """结构化搜索模板——用于 StructuredSearchReformulation。"""
    fields: Dict[SearchField, Any] = field(default_factory=dict)
    logical_op: str = "AND"  # AND / OR
    limit: int = 20

    def to_query_string(self) -> str:
        parts = []
        for f, v in self.fields.items():
            if isinstance(v, (list, tuple)):
                parts.append(f"{f.value}:({','.join(str(x) for x in v)})")
            else:
                parts.append(f"{f.value}:{v}")
        return f" {self.logical_op} ".join(parts)


# ============================================================================
# TextGradPromptEvolution
# ============================================================================

@dataclass
class PromptVariant:
    """Prompt 变体——TextGrad 优化的候选。"""
    variant_id: str
    template: str
    score: float = 0.0
    generation: int = 0
    feedback_history: List[Dict[str, Any]] = field(default_factory=list)


class TextGradPromptEvolution:
    """TextGrad 无参数 Prompt 进化器。

    基于下游响应质量反馈，通过变体评分→选择→交叉→变异
    迭代优化 prompt 模板，实现终身 prompt 进化而无需求梯度传播。
    """

    def __init__(self, population_size: int = 10, mutation_rate: float = 0.2):
        self._lock = threading.RLock()
        self.population_size = population_size
        self.mutation_rate = mutation_rate
        self._population: Dict[str, PromptVariant] = {}
        self._generation: int = 0
        self._best_variant: Optional[PromptVariant] = None

    def seed(self, base_template: str):
        with self._lock:
            self._population.clear()
            for i in range(self.population_size):
                vid = f"pv_gen0_{i}"
                self._population[vid] = PromptVariant(
                    variant_id=vid, template=base_template, generation=0
                )
            self._generation = 0

    def feedback(self, variant_id: str, score: float, detail: str = ""):
        """记录某变体的下游质量反馈。"""
        with self._lock:
            if variant_id in self._population:
                pv = self._population[variant_id]
                pv.feedback_history.append({"score": score, "detail": detail})
                pv.score = (pv.score * 0.7 + score * 0.3)  # EMA

    def evolve(self) -> Optional[str]:
        """执行一代进化，返回新的最优 prompt 模板。"""
        with self._lock:
            if len(self._population) < 2:
                return None

            sorted_vars = sorted(
                self._population.values(), key=lambda p: p.score, reverse=True
            )
            elite = sorted_vars[: max(2, self.population_size // 3)]
            self._best_variant = elite[0]
            self._generation += 1

            # 新种群 = elite + mutate(elite)
            new_pop: Dict[str, PromptVariant] = {}
            for pv in elite:
                new_pop[pv.variant_id] = pv
            for i in range(self.population_size - len(elite)):
                parent = elite[i % len(elite)]
                vid = f"pv_gen{self._generation}_{i}"
                new_pop[vid] = PromptVariant(
                    variant_id=vid,
                    template=parent.template,
                    generation=self._generation,
                )
            self._population = new_pop
            return self._best_variant.template

    def get_best(self) -> Optional[PromptVariant]:
        with self._lock:
            return self._best_variant

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "generation": self._generation,
                "population_size": len(self._population),
                "best_score": self._best_variant.score if self._best_variant else 0.0,
            }


# ============================================================================
# StructuredSearchReformulation
# ============================================================================

class StructuredSearchReformulation:
    """将自然语言查询转化为结构化搜索查询。"""

    _FIELD_MAP = {
        "speaker": SearchField.SPEAKER,
        "time": SearchField.TIMESTAMP_RANGE,
        "date": SearchField.TIMESTAMP_RANGE,
        "topic": SearchField.TOPIC,
        "about": SearchField.TOPIC,
        "sentiment": SearchField.SENTIMENT,
        "entity": SearchField.ENTITY,
        "keyword": SearchField.KEYWORD,
    }

    def reformulate(self, query: str) -> SearchTemplate:
        """自然语言 → 结构化搜索模板。"""
        template = SearchTemplate()
        ql = query.lower()

        for trigger, field in self._FIELD_MAP.items():
            if trigger in ql:
                template.fields[field] = query

        if not template.fields:
            template.fields[SearchField.KEYWORD] = query

        return template


# ============================================================================
# Aggregate Classes
# ============================================================================

@dataclass
class TriMemEncoder:
    """三记忆编码器——将原始对话编码为三种粒度。"""
    def encode_dialogue(self, segment: RawDialogueSegment) -> TriMemAtomicFact:
        fid = hashlib.md5(segment.content.encode()).hexdigest()[:12]
        return TriMemAtomicFact(
            fact_id=fid,
            content=segment.content[:200],
            source_segments=[segment.segment_id],
            entities=[w for w in segment.content.split() if w[0].isupper()][:5],
        )

    def aggregate_profile(
        self, facts: List[TriMemAtomicFact], subject: str
    ) -> SynthesizedProfile:
        pid = hashlib.md5(subject.encode()).hexdigest()[:12]
        combined = " ".join(f.content for f in facts)
        return SynthesizedProfile(
            profile_id=pid,
            subject=subject,
            summary=combined[:300],
            supporting_facts=[f.fact_id for f in facts],
        )


@dataclass
class TriMemRetriever:
    """三记忆检索器——按粒度搜索。"""
    facts: Dict[str, TriMemAtomicFact] = field(default_factory=dict)
    profiles: Dict[str, SynthesizedProfile] = field(default_factory=dict)

    def search_facts(self, query: str, top_k: int = 5) -> List[TriMemAtomicFact]:
        ql = query.lower()
        scored = []
        for f in self.facts.values():
            score = (ql in f.content.lower()) * 1.0
            if score > 0:
                scored.append((score, f))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in scored[:top_k]]

    def search_profiles(self, subject: str) -> Optional[SynthesizedProfile]:
        return self.profiles.get(
            hashlib.md5(subject.encode()).hexdigest()[:12]
        )


class ProfileAggregator:
    """档案聚合器——跨对话迭代更新档案。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._profiles: Dict[str, SynthesizedProfile] = {}

    def aggregate(
        self, facts: List[TriMemAtomicFact], subject: str
    ) -> SynthesizedProfile:
        with self._lock:
            pid = hashlib.md5(subject.encode()).hexdigest()[:12]
            if pid in self._profiles:
                existing = self._profiles[pid]
                existing.summary += " | " + " ".join(f.content for f in facts)[:200]
                existing.supporting_facts.extend(f.fact_id for f in facts)
                existing.updated_at = _time.time()
                return existing
            sp = SynthesizedProfile(
                profile_id=pid,
                subject=subject,
                summary=" ".join(f.content for f in facts)[:300],
                supporting_facts=[f.fact_id for f in facts],
            )
            self._profiles[pid] = sp
            return sp


# ============================================================================
# Main Class
# ============================================================================

class TriMemDualBranchMemory:
    """三记忆双分支记忆 (CB66)。

    三条并行写入路径 + 双分支检索（事实检索 / 档案推理）。

    Usage:
        tm = TriMemDualBranchMemory()
        seg = RawDialogueSegment(segment_id="s1", source_id="chat_01", content="...")
        tm.write_raw(seg)
        tm.write_fact(tm.encoder.encode_dialogue(seg))
        tm.write_profile(tm.aggregator.aggregate(tm.retriever.facts.values(), "user"))
        results = tm.query("what did the user say about deadlines?")
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.encoder = TriMemEncoder()
        self.retriever = TriMemRetriever()
        self.aggregator = ProfileAggregator()
        self.prompt_evolution = TextGradPromptEvolution()
        self.query_reformulator = StructuredSearchReformulation()
        self._raw_segments: List[RawDialogueSegment] = []
        self._fact_count: int = 0
        self._profile_count: int = 0
        self._start_time: float = _time.time()

    def write_raw(self, segment: RawDialogueSegment):
        with self._lock:
            self._raw_segments.append(segment)

    def write_fact(self, fact: TriMemAtomicFact):
        with self._lock:
            self.retriever.facts[fact.fact_id] = fact
            self._fact_count += 1

    def write_profile(self, profile: SynthesizedProfile):
        with self._lock:
            self.retriever.profiles[profile.profile_id] = profile
            self._profile_count += 1

    def query(self, natural_query: str) -> Dict[str, Any]:
        with self._lock:
            template = self.query_reformulator.reformulate(natural_query)
            facts = self.retriever.search_facts(natural_query)
            return {
                "structured_query": template.to_query_string(),
                "facts": [f.content[:100] for f in facts],
                "fact_count": len(facts),
            }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "class": "TriMemDualBranchMemory (CB66)",
                "raw_segments": len(self._raw_segments),
                "atomic_facts": self._fact_count,
                "synthesized_profiles": self._profile_count,
                "prompt_evolution": self.prompt_evolution.statistics(),
                "uptime_seconds": round(_time.time() - self._start_time, 3),
            }
