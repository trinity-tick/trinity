"""
EvidenceReliabilityMemory — MMA Dynamic Reliability Scoring
============================================================
arXiv 2602.16493 · P39-4

动态可靠性评估记忆系统: 综合 source_credibility / temporal_decay /
conflict_consensus 三项评分; 冲突感知网络共识检测并重加权;
当证据支撑不足时 abstain() 返回拒绝回答; 检测 Visual Placebo Effect。

设计要点:
  - EvidenceRecord: 带多维可靠性分数的单条证据
  - ReliabilityScore: source_credibility + temporal_decay + conflict_consensus
  - ConflictingEvidenceSet: 冲突组检测 + 网络共识重加权
  - VisualPlaceboPattern: RAG 视觉偏差检测器
  - abstain(): 证据不足时拒绝回答 (无幻觉)
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ReliabilityDimension(Enum):
    """可靠性维度。"""
    SOURCE_CREDIBILITY = auto()
    TEMPORAL_DECAY = auto()
    CONFLICT_CONSENSUS = auto()


class AbstentionReason(Enum):
    """拒绝回答原因。"""
    INSUFFICIENT_EVIDENCE = auto()
    LOW_RELIABILITY = auto()
    HIGH_CONFLICT = auto()
    VISUAL_PLACEBO_DETECTED = auto()


class SourceCredibility(Enum):
    """来源可信度等级。"""
    VERIFIED = auto()
    TRUSTED = auto()
    UNKNOWN = auto()
    SUSPICIOUS = auto()
    DISCREDITED = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class EvidenceRecord:
    """带可靠性评分的单条证据。

    Parameters
    ----------
    record_id : str
        证据唯一标识。
    content : str
        证据内容。
    source : str
        来源标识。
    credibility : SourceCredibility
        来源可信度。
    reliability_score : float
        综合可靠性分数 (0.0~1.0)。
    source_cred_score : float
        来源可信度得分。
    temporal_decay_score : float
        时间衰减得分。
    conflict_consensus_score : float
        冲突共识得分。
    timestamp : float
        记录创建时间。
    tags : List[str]
        标签。
    visual_placebo_flag : bool
        是否检测到视觉安慰剂效应。
    """
    record_id: str
    content: str
    source: str = ""
    credibility: SourceCredibility = SourceCredibility.UNKNOWN
    reliability_score: float = 0.0
    source_cred_score: float = 0.0
    temporal_decay_score: float = 1.0
    conflict_consensus_score: float = 0.5
    timestamp: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)
    visual_placebo_flag: bool = False


@dataclass
class ReliabilityScore:
    """三维可靠性评分。"""
    source_credibility: float
    temporal_decay: float
    conflict_consensus: float
    composite: float = 0.0

    def __post_init__(self) -> None:
        # Weighted composite: 40% credibility + 30% temporal + 30% consensus
        self.composite = 0.4 * self.source_credibility + 0.3 * self.temporal_decay + 0.3 * self.conflict_consensus


@dataclass
class ConflictingEvidenceSet:
    """冲突证据组——检测到矛盾的多条证据。"""
    set_id: str
    records: List[EvidenceRecord] = field(default_factory=list)
    conflict_ratio: float = 0.0
    consensus_version: Optional[str] = None
    resolved: bool = False


@dataclass
class VisualPlaceboPattern:
    """视觉安慰剂效应模式——RAG 视觉偏差。"""
    pattern_id: str
    description: str
    trigger_keywords: List[str] = field(default_factory=list)
    mitigation: str = ""


# ---------------------------------------------------------------------------
# EvidenceReliabilityMemory
# ---------------------------------------------------------------------------

class EvidenceReliabilityMemory:
    """MMA 动态可靠性评估记忆系统。

    Parameters
    ----------
    temporal_half_life_days : float
        时间衰减半衰期 (天)。
    reliability_threshold : float
        可靠性阈值: 低于此值 abstain() 返回拒绝。
    conflict_threshold : float
        冲突检测阈值。
    """

    def __init__(
        self,
        temporal_half_life_days: float = 30.0,
        reliability_threshold: float = 0.3,
        conflict_threshold: float = 0.6,
    ) -> None:
        self.temporal_half_life_days = temporal_half_life_days
        self.reliability_threshold = reliability_threshold
        self.conflict_threshold = conflict_threshold

        self._records: Dict[str, EvidenceRecord] = {}
        self._lock = threading.RLock()
        self._record_count: int = 0
        self._abstention_log: List[Tuple[str, AbstentionReason]] = []

        # Visual placebo patterns
        self._placebo_patterns: List[VisualPlaceboPattern] = [
            VisualPlaceboPattern(
                pattern_id="vp_001",
                description="权威性视觉偏差: 过度信任格式精美但内容空洞的文档",
                trigger_keywords=["certified", "official", "authoritative", "guaranteed"],
                mitigation="交叉验证内容实质而非文档外观",
            ),
            VisualPlaceboPattern(
                pattern_id="vp_002",
                description="来源光环效应: 知名来源的低质内容仍被高估",
                trigger_keywords=["published by", "from", "according to"],
                mitigation="评估具体论据而非来源声誉",
            ),
        ]

        logger.info("EvidenceReliabilityMemory initialized [τ½=%.1fd θ=%.2f]",
                     temporal_half_life_days, reliability_threshold)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_evidence(
        self,
        content: str,
        source: str = "",
        credibility: SourceCredibility = SourceCredibility.UNKNOWN,
        tags: Optional[List[str]] = None,
    ) -> EvidenceRecord:
        """添加一条新证据并计算初始可靠性。

        Parameters
        ----------
        content : str
            证据内容文本。
        source : str
            来源标识。
        credibility : SourceCredibility
            来源可信度。
        tags : Optional[List[str]]
            标签集合。

        Returns
        -------
        EvidenceRecord
            创建的证据记录。
        """
        with self._lock:
            self._record_count += 1
            record = EvidenceRecord(
                record_id=f"ev_{self._record_count}_{int(time.time()*1e6)}",
                content=content,
                source=source,
                credibility=credibility,
                tags=tags or [],
                source_cred_score=self._map_credibility(credibility),
            )

            # Check visual placebo
            record.visual_placebo_flag = self._detect_visual_placebo(content)

            # Compute full reliability
            self._compute_reliability(record)
            self._records[record.record_id] = record

            if record.visual_placebo_flag:
                logger.warning("Visual Placebo Effect detected in record %s", record.record_id)

            return record

    def get_reliability(self, record_id: str) -> Optional[ReliabilityScore]:
        """获取证据的可靠性评分。"""
        record = self._records.get(record_id)
        if not record:
            return None
        return ReliabilityScore(
            source_credibility=record.source_cred_score,
            temporal_decay=record.temporal_decay_score,
            conflict_consensus=record.conflict_consensus_score,
            composite=record.reliability_score,
        )

    def get_evidence(self, record_id: str) -> Optional[EvidenceRecord]:
        return self._records.get(record_id)

    def query_by_source(self, source: str) -> List[EvidenceRecord]:
        return [r for r in self._records.values() if r.source == source]

    def query_by_tag(self, tag: str) -> List[EvidenceRecord]:
        return [r for r in self._records.values() if tag in r.tags]

    # ------------------------------------------------------------------
    # Conflict Detection & Consensus
    # ------------------------------------------------------------------

    def detect_conflicts(self, record_ids: Optional[List[str]] = None) -> List[ConflictingEvidenceSet]:
        """检测证据间的矛盾并构建冲突组。

        Parameters
        ----------
        record_ids : Optional[List[str]]
            限定检测范围; 默认所有记录。

        Returns
        -------
        List[ConflictingEvidenceSet]
            冲突组列表。
        """
        with self._lock:
            ids = record_ids or list(self._records.keys())
            records = [self._records[rid] for rid in ids if rid in self._records]
            if len(records) < 2:
                return []

            conflict_sets: List[ConflictingEvidenceSet] = []
            # Compare all pairs for contradiction
            for i in range(len(records)):
                for j in range(i + 1, len(records)):
                    sim = self._text_similarity(records[i].content, records[j].content)
                    # Low similarity → possible conflict
                    if sim < self.conflict_threshold:
                        # Check if they claim opposite facts
                        if self._semantic_conflict(records[i].content, records[j].content):
                            cs = ConflictingEvidenceSet(
                                set_id=f"cset_{int(time.time()*1e6)}_{i}_{j}",
                                records=[records[i], records[j]],
                                conflict_ratio=1.0 - sim,
                            )
                            conflict_sets.append(cs)

            # Update conflict_consensus scores for conflicted records
            for cs in conflict_sets:
                for r in cs.records:
                    # Penalize records in conflicts
                    r.conflict_consensus_score *= 0.5
                    self._compute_reliability(r)

            return conflict_sets

    def resolve_conflict(self, set_id: str, consensus: str) -> bool:
        """人工解决冲突: 设置共识版本。"""
        with self._lock:
            for cs in self._detect_or_empty():
                if cs.set_id == set_id:
                    cs.consensus_version = consensus
                    cs.resolved = True
                    # Restore conflict_consensus for resolved records
                    for r in cs.records:
                        r.conflict_consensus_score = 0.75
                        self._compute_reliability(r)
                    return True
            return False

    # ------------------------------------------------------------------
    # Abstention
    # ------------------------------------------------------------------

    def abstain(self, record_ids: Optional[List[str]] = None) -> Tuple[bool, AbstentionReason, str]:
        """主动拒绝回答 (当证据不足时)。

        Parameters
        ----------
        record_ids : Optional[List[str]]
            要评估的证据范围。

        Returns
        -------
        Tuple[bool, AbstentionReason, str]
            (should_abstain, reason, explanation)。
        """
        with self._lock:
            ids = record_ids or list(self._records.keys())
            records = [self._records[rid] for rid in ids if rid in self._records]

            if not records:
                reason = AbstentionReason.INSUFFICIENT_EVIDENCE
                self._abstention_log.append(("no_records", reason))
                return True, reason, "No evidence available for this query."

            # 1. Check visual placebo
            placebo_count = sum(1 for r in records if r.visual_placebo_flag)
            if placebo_count > len(records) * 0.5:
                reason = AbstentionReason.VISUAL_PLACEBO_DETECTED
                self._abstention_log.append(("placebo", reason))
                return True, reason, (
                    f"Visual Placebo Effect detected: {placebo_count}/{len(records)} "
                    "records may have inflated credibility due to visual bias."
                )

            # 2. Check reliability
            avg_rel = float(np.mean([r.reliability_score for r in records]))
            if avg_rel < self.reliability_threshold:
                reason = AbstentionReason.LOW_RELIABILITY
                msg = f"Average reliability {avg_rel:.3f} below threshold {self.reliability_threshold}. Refusing to answer."
                self._abstention_log.append((f"avg_{avg_rel:.3f}", reason))
                return True, reason, msg

            # 3. Check conflict
            conflicts = self.detect_conflicts(ids)
            if conflicts:
                unresolved = [c for c in conflicts if not c.resolved]
                if len(unresolved) >= 2:
                    reason = AbstentionReason.HIGH_CONFLICT
                    msg = f"{len(unresolved)} unresolved conflict sets detected. Cannot provide reliable answer."
                    self._abstention_log.append((f"conflicts_{len(unresolved)}", reason))
                    return True, reason, msg

            return False, AbstentionReason.INSUFFICIENT_EVIDENCE, ""

    # ------------------------------------------------------------------
    # Visual Placebo Effect Detection
    # ------------------------------------------------------------------

    def check_visual_placebo(self, content: str) -> Tuple[bool, List[str]]:
        """检测文本中是否存在视觉安慰剂效应模式。

        Parameters
        ----------
        content : str
            待检测文本。

        Returns
        -------
        Tuple[bool, List[str]]
            (是否检测到, 匹配的模式ID列表)。
        """
        matched = []
        content_lower = content.lower()
        for pattern in self._placebo_patterns:
            for kw in pattern.trigger_keywords:
                if kw.lower() in content_lower:
                    matched.append(pattern.pattern_id)
                    break
        return len(matched) > 0, matched

    def list_placebo_patterns(self) -> List[VisualPlaceboPattern]:
        return list(self._placebo_patterns)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            scores = [r.reliability_score for r in self._records.values()]
            placebo_count = sum(1 for r in self._records.values() if r.visual_placebo_flag)
            return {
                "total_evidence": len(self._records),
                "mean_reliability": float(np.mean(scores)) if scores else 0.0,
                "min_reliability": float(np.min(scores)) if scores else 0.0,
                "visual_placebo_count": placebo_count,
                "abstentions": len(self._abstention_log),
                "credibility_distribution": dict(Counter(
                    r.credibility.name for r in self._records.values()
                )),
            }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _map_credibility(cred: SourceCredibility) -> float:
        mapping = {
            SourceCredibility.VERIFIED: 1.0,
            SourceCredibility.TRUSTED: 0.8,
            SourceCredibility.UNKNOWN: 0.5,
            SourceCredibility.SUSPICIOUS: 0.3,
            SourceCredibility.DISCREDITED: 0.05,
        }
        return mapping.get(cred, 0.5)

    def _compute_temporal_decay(self, record: EvidenceRecord) -> float:
        age_days = (time.time() - record.timestamp) / 86400.0
        if age_days <= 0:
            return 1.0
        decay = math.exp(-math.log(2) * age_days / self.temporal_half_life_days)
        return max(decay, 0.05)

    def _compute_reliability(self, record: EvidenceRecord) -> None:
        record.temporal_decay_score = self._compute_temporal_decay(record)
        record.source_cred_score = self._map_credibility(record.credibility)
        # If visual placebo detected, cap source_cred
        if record.visual_placebo_flag:
            record.source_cred_score *= 0.4
        score = ReliabilityScore(
            source_credibility=record.source_cred_score,
            temporal_decay=record.temporal_decay_score,
            conflict_consensus=record.conflict_consensus_score,
        )
        record.reliability_score = score.composite

    def _detect_visual_placebo(self, content: str) -> bool:
        detected, _ = self.check_visual_placebo(content)
        return detected

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """Simple Jaccard similarity on word sets."""
        wa = set(a.lower().split())
        wb = set(b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    @staticmethod
    def _semantic_conflict(a: str, b: str) -> bool:
        """粗糙语义冲突检测: 否定词 + 核心事实矛盾。"""
        negations = {"not", "no", "never", "cannot", "doesn't", "don't", "isn't", "aren't"}
        has_a_neg = bool(set(a.lower().split()) & negations)
        has_b_neg = bool(set(b.lower().split()) & negations)
        if has_a_neg != has_b_neg:
            # One has negation, check if they share a core fact
            a_set = set(a.lower().split()) - negations
            b_set = set(b.lower().split()) - negations
            overlap = len(a_set & b_set)
            return overlap > 0 and overlap / max(len(a_set | b_set), 1) > 0.3
        return False

    def _detect_or_empty(self) -> List[ConflictingEvidenceSet]:
        """占位: 供外部 resolve_conflict 查找冲突组。"""
        return [cs for cs in []] if False else []
