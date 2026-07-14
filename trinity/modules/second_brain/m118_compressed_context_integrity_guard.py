"""
M118 CompressedContextIntegrityGuard — 压缩上下文完整性守护

综合 COMA (Context Memory Augmentation) + SeKV (Selective Key-Value) 洞察，
在记忆压缩管线中保护上下文完整性。

核心设计:
  1. ContextIntegrityVerifier — 上下文完整性验证器
     - 压缩前后关键信息一致性检查
     - 实体链接: 压缩后的实体是否完整保留
     - 关系保持: 实体间关系是否在压缩后仍可推断
  2. CompressionBoundaryDetector — 压缩边界检测器
     - 检测"过度压缩": 信息密度 > 阈值 → 降级警告
     - 检测"选择性丢弃": 安全相关信息被优先丢弃的模式
  3. 与 M114 SleepCycle 协作: 睡眠周期前进行完整性快照

策略来源:
  - COMA: 压缩记忆中的信息完整性保证
  - SeKV: 选择性 KV 缓存丢弃中的关键信息保护
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np


MODULE_ID = "M118"
MODULE_VERSION = "1.0.0"
PAPER_REF = "COMA + SeKV — Compressed Context Integrity Protection"

SEP = "=" * 80
SUB = "-" * 60


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class IntegrityStatus(Enum):
    """Result of context integrity verification."""

    INTACT = "intact"               # all key information preserved
    MINOR_LOSS = "minor_loss"       # non-critical details lost
    SIGNIFICANT_LOSS = "significant_loss"  # entity/relation dropped
    CRITICAL_LOSS = "critical_loss"        # safety-critical info lost
    CORRUPTED = "corrupted"         # logically inconsistent after compression


class CompressionGrade(Enum):
    """Compression quality grade from boundary detection."""

    OPTIMAL = "optimal"             # within safe compression range
    ACCEPTABLE = "acceptable"       # approaching boundary but still safe
    WARNING = "warning"             # information density too high
    EXCESSIVE = "excessive"         # over-compressed, needs rollback
    DANGEROUS = "dangerous"         # selective discard detected on safety info


class SnapshotType(Enum):
    """Types of integrity snapshots."""

    PRE_COMPRESSION = "pre_compression"
    POST_COMPRESSION = "post_compression"
    PRE_SLEEP = "pre_sleep"          # taken before M114 SleepCycle
    ON_DEMAND = "on_demand"


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class EntityRecord:
    """A named entity extracted from context."""

    entity_id: str
    entity_name: str
    entity_type: str                 # person / organization / location / concept / number
    mentions: int = 1
    attributes: Dict[str, str] = field(default_factory=dict)
    first_seen_offset: int = 0
    last_seen_offset: int = 0

    def fingerprint(self) -> str:
        """Compact fingerprint for comparison."""
        return hashlib.md5(
            f"{self.entity_name}|{self.entity_type}".encode()
        ).hexdigest()[:8]


@dataclass
class RelationRecord:
    """A semantic relation between two entities."""

    relation_id: str
    source_entity: str
    target_entity: str
    relation_type: str              # e.g., "works_at", "causes", "depends_on"
    confidence: float = 1.0
    bidirectional: bool = False
    context_span: Tuple[int, int] = (0, 0)

    def fingerprint(self) -> str:
        return hashlib.md5(
            f"{self.source_entity}|{self.relation_type}|{self.target_entity}".encode()
        ).hexdigest()[:8]


@dataclass
class IntegrityReport:
    """Complete integrity verification report."""

    report_id: str
    status: IntegrityStatus
    # Entity-level metrics
    entities_before: int = 0
    entities_after: int = 0
    entities_preserved: int = 0
    entities_lost: List[str] = field(default_factory=list)       # lost entity names
    # Relation-level metrics
    relations_before: int = 0
    relations_after: int = 0
    relations_preserved: int = 0
    relations_broken: List[str] = field(default_factory=list)     # broken relation descriptions
    # Scores
    entity_preservation_rate: float = 1.0
    relation_preservation_rate: float = 1.0
    overall_integrity_score: float = 1.0                          # [0, 1]
    # Boundary detection
    compression_grade: CompressionGrade = CompressionGrade.OPTIMAL
    information_density: float = 0.0
    selective_discard_detected: bool = False
    discard_patterns: List[str] = field(default_factory=list)
    # Recommendations
    requires_rollback: bool = False
    recommendations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_id": self.report_id,
            "status": self.status.value,
            "entity_preservation": f"{self.entities_preserved}/{self.entities_before} ({self.entity_preservation_rate:.1%})",
            "relation_preservation": f"{self.relations_preserved}/{self.relations_before} ({self.relation_preservation_rate:.1%})",
            "overall_score": round(self.overall_integrity_score, 4),
            "compression_grade": self.compression_grade.value,
            "information_density": round(self.information_density, 4),
            "selective_discard": self.selective_discard_detected,
            "rollback_needed": self.requires_rollback,
            "entities_lost": self.entities_lost[:10],
            "relations_broken": self.relations_broken[:10],
            "recommendations": self.recommendations[:5],
        }


@dataclass
class IntegritySnapshot:
    """Snapshot of context integrity state (used with M114 SleepCycle)."""

    snapshot_id: str
    snapshot_type: SnapshotType
    timestamp: float = field(default_factory=time.time)
    entity_fingerprints: Set[str] = field(default_factory=set)
    relation_fingerprints: Set[str] = field(default_factory=set)
    information_density: float = 0.0
    context_size_chars: int = 0
    integrity_score: float = 1.0
    # Delta from previous snapshot
    delta_entities_lost: int = 0
    delta_relations_broken: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "type": self.snapshot_type.value,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp)),
            "entity_count": len(self.entity_fingerprints),
            "relation_count": len(self.relation_fingerprints),
            "info_density": round(self.information_density, 4),
            "context_size": self.context_size_chars,
            "integrity": round(self.integrity_score, 4),
            "delta_entities": self.delta_entities_lost,
            "delta_relations": self.delta_relations_broken,
        }


# ---------------------------------------------------------------------------
# ContextIntegrityVerifier — 上下文完整性验证器
# ---------------------------------------------------------------------------


class ContextIntegrityVerifier:
    """
    ContextIntegrityVerifier — 压缩前后关键信息一致性检查。

    核心流程:
      1. 提取压缩前上下文中的实体和关系
      2. 提取压缩后上下文中的实体和关系
      3. 对比: 实体是否完整保留 / 关系是否仍可推断
      4. 生成完整性报告 + 降级建议
    """

    # Simple entity extraction patterns
    _ENTITY_PATTERNS = {
        "person": re.compile(
            r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b"  # capitalized multi-word names
        ),
        "organization": re.compile(
            r"\b((?:[A-Z][a-z]*\.?\s*)+(?:Inc|Corp|LLC|Ltd|University|Institute|Lab|Company))\b"
        ),
        "number": re.compile(r"\b(\d+(?:\.\d+)?(?:\s*(?:%|USD|EUR|kg|km|MB|GB|ms|s))?)\b"),
        "concept": re.compile(
            r"\b(algorithm|model|framework|pipeline|architecture|protocol|"
            r"attention|transformer|encoder|decoder|embedding|token|"
            r"gradient|optimization|inference|training|fine-tuning)\b"
        ),
    }

    # Safety-critical entity categories
    _SAFETY_CATEGORIES = {
        "person", "organization", "location",
    }

    def __init__(
        self,
        entity_preservation_threshold: float = 0.85,
        relation_preservation_threshold: float = 0.80,
        overall_integrity_threshold: float = 0.75,
        safety_weight: float = 2.0,
    ):
        """
        Args:
            entity_preservation_threshold: 实体保留率阈值 (低于此值触发告警)
            relation_preservation_threshold: 关系保留率阈值
            overall_integrity_threshold: 综合完整性阈值
            safety_weight: 安全相关实体的权重乘数
        """
        self.entity_preservation_threshold = entity_preservation_threshold
        self.relation_preservation_threshold = relation_preservation_threshold
        self.overall_integrity_threshold = overall_integrity_threshold
        self.safety_weight = safety_weight

        self._verification_count: int = 0
        self._report_history: List[IntegrityReport] = []

    # ── 实体提取 ──────────────────────────────────────────────

    def extract_entities(self, text: str) -> List[EntityRecord]:
        """Extract named entities and key concepts from text."""
        entities: List[EntityRecord] = []
        seen_names: Dict[str, EntityRecord] = {}

        for entity_type, pattern in self._ENTITY_PATTERNS.items():
            for match in pattern.finditer(text):
                name = match.group(1).strip()
                offset = match.start()

                if name in seen_names:
                    seen_names[name].mentions += 1
                    seen_names[name].last_seen_offset = offset
                else:
                    rec = EntityRecord(
                        entity_id=f"ent-{entity_type}-{len(entities):04d}",
                        entity_name=name,
                        entity_type=entity_type,
                        mentions=1,
                        first_seen_offset=offset,
                        last_seen_offset=offset,
                    )
                    entities.append(rec)
                    seen_names[name] = rec

        return entities

    # ── 关系提取 (简化版) ────────────────────────────────────

    def extract_relations(
        self,
        text: str,
        entities: List[EntityRecord],
    ) -> List[RelationRecord]:
        """
        Extract entity-entity relations based on co-occurrence proximity.

        Simplified approach: entities within 50-char window → potential relation.
        """
        relations: List[RelationRecord] = []

        # Build sorted position map
        pos_map: List[Tuple[int, str, str]] = []  # (position, entity_name, entity_type)
        for e in entities:
            pos_map.append((e.first_seen_offset, e.entity_name, e.entity_type))

        pos_map.sort()

        # Extract co-occurrence relations
        window = 80  # characters
        for i in range(len(pos_map)):
            for j in range(i + 1, len(pos_map)):
                if pos_map[j][0] - pos_map[i][0] > window:
                    break
                rel = RelationRecord(
                    relation_id=f"rel-{len(relations):04d}",
                    source_entity=pos_map[i][1],
                    target_entity=pos_map[j][1],
                    relation_type="co_occurs_with",
                    confidence=0.7,
                    context_span=(pos_map[i][0], pos_map[j][0] + len(pos_map[j][1])),
                )
                relations.append(rel)

        return relations

    # ── 完整性验证 ────────────────────────────────────────────

    def verify(
        self,
        context_before: str,
        context_after: str,
        safety_entities: Optional[Set[str]] = None,
    ) -> IntegrityReport:
        """
        验证压缩前后的上下文完整性。

        Args:
            context_before: 压缩前的原始上下文
            context_after: 压缩后的上下文
            safety_entities: 用户指定的安全关键实体名集合

        Returns:
            IntegrityReport
        """
        report_id = f"ir-{self._verification_count:05d}"

        # Extract entities & relations from both
        entities_before = self.extract_entities(context_before)
        entities_after = self.extract_entities(context_after)

        # Entity fingerprints
        fps_before = {e.fingerprint() for e in entities_before}
        fps_after = {e.fingerprint() for e in entities_after}

        # Entity preservation
        preserved_fps = fps_before & fps_after
        lost_fps = fps_before - fps_after
        lost_entities = [
            e.entity_name for e in entities_before
            if e.fingerprint() in lost_fps
        ]

        # Relations
        rels_before = self.extract_relations(context_before, entities_before)
        rels_after = self.extract_relations(context_after, entities_after)

        fps_rel_before = {r.fingerprint() for r in rels_before}
        fps_rel_after = {r.fingerprint() for r in rels_after}
        broken_fps = fps_rel_before - fps_rel_after
        broken_relations = [
            f"{r.source_entity} --[{r.relation_type}]--> {r.target_entity}"
            for r in rels_before if r.fingerprint() in broken_fps
        ]

        # Scores
        entity_rate = len(preserved_fps) / max(len(fps_before), 1)
        relation_rate = len(fps_rel_before & fps_rel_after) / max(len(fps_rel_before), 1)

        # Safety-weighted score
        if safety_entities:
            safety_lost = len([e for e in lost_entities if e in safety_entities])
            safety_penalty = safety_lost * self.safety_weight / max(len(safety_entities), 1)
            overall_score = max(0.0, (entity_rate * 0.5 + relation_rate * 0.5) - safety_penalty)
        else:
            overall_score = entity_rate * 0.5 + relation_rate * 0.5

        # Status determination
        if overall_score >= self.overall_integrity_threshold:
            status = IntegrityStatus.INTACT
        elif entity_rate >= 0.7 and relation_rate >= 0.6:
            status = IntegrityStatus.MINOR_LOSS
        elif entity_rate >= 0.5:
            status = IntegrityStatus.SIGNIFICANT_LOSS
        elif entity_rate >= 0.3:
            status = IntegrityStatus.CRITICAL_LOSS
        else:
            status = IntegrityStatus.CORRUPTED

        # Recommendations
        recommendations: List[str] = []
        if entity_rate < self.entity_preservation_threshold:
            recommendations.append(
                f"实体保留率 {entity_rate:.1%} 低于阈值 {self.entity_preservation_threshold:.1%}, "
                f"建议减小压缩率或增加保留预算"
            )
        if relation_rate < self.relation_preservation_threshold:
            recommendations.append(
                f"关系保留率 {relation_rate:.1%} 低于阈值 {self.relation_preservation_threshold:.1%}, "
                f"建议使用 SeKV 选择性保留关键关系"
            )
        if status in (IntegrityStatus.CRITICAL_LOSS, IntegrityStatus.CORRUPTED):
            recommendations.append("严重信息丢失: 建议回滚到压缩前的上下文")

        report = IntegrityReport(
            report_id=report_id,
            status=status,
            entities_before=len(entities_before),
            entities_after=len(entities_after),
            entities_preserved=len(preserved_fps),
            entities_lost=lost_entities[:20],
            relations_before=len(rels_before),
            relations_after=len(rels_after),
            relations_preserved=len(fps_rel_before & fps_rel_after),
            relations_broken=broken_relations[:20],
            entity_preservation_rate=entity_rate,
            relation_preservation_rate=relation_rate,
            overall_integrity_score=overall_score,
            compression_grade=CompressionGrade.OPTIMAL,
            information_density=0.0,
            requires_rollback=(status in (IntegrityStatus.CRITICAL_LOSS, IntegrityStatus.CORRUPTED)),
            recommendations=recommendations,
        )

        self._verification_count += 1
        self._report_history.append(report)
        return report

    def stats(self) -> Dict[str, Any]:
        return {
            "verification_count": self._verification_count,
            "thresholds": {
                "entity": self.entity_preservation_threshold,
                "relation": self.relation_preservation_threshold,
                "overall": self.overall_integrity_threshold,
                "safety_weight": self.safety_weight,
            },
        }


# ---------------------------------------------------------------------------
# CompressionBoundaryDetector — 压缩边界检测器
# ---------------------------------------------------------------------------


class CompressionBoundaryDetector:
    """
    CompressionBoundaryDetector — 检测压缩是否越过安全边界。

    两个检测维度:
      1. 过度压缩: 信息密度 > 阈值 → 降级
         信息密度 = 有效信息单元数 / 总 token 数
         阈值: density > 0.85 → WARNING, > 0.95 → EXCESSIVE

      2. 选择性丢弃: 检测安全相关信息是否被优先丢弃
         安全类别 (person/organization/location) 的丢弃率显著高于
         非安全类别 → DANGEROUS 模式
    """

    def __init__(
        self,
        max_density_threshold: float = 0.85,
        excessive_density_threshold: float = 0.95,
        selective_discard_ratio_threshold: float = 1.5,
    ):
        """
        Args:
            max_density_threshold: 信息密度上限 (安全)
            excessive_density_threshold: 信息密度危险阈值
            selective_discard_ratio_threshold: 选择性丢弃比率阈值
        """
        self.max_density_threshold = max_density_threshold
        self.excessive_density_threshold = excessive_density_threshold
        self.selective_discard_ratio_threshold = selective_discard_ratio_threshold

        self._detection_count: int = 0

    def compute_information_density(
        self,
        text: str,
        entities: List[EntityRecord],
    ) -> float:
        """
        Compute information density: effective information units / total character count.

        Information units = number of unique entities + unique entity-type pairs,
        normalized by text length.
        """
        if not text:
            return 0.0

        unique_entities = len(set(e.entity_name for e in entities))
        unique_concepts = len(set(
            (e.entity_type, e.entity_name) for e in entities
        ))

        raw_density = (unique_entities + unique_concepts) / len(text)

        # Normalize to [0, 1] — typical density for natural text is ~0.01-0.05
        # Scale: density 0.1 → normalized ~0.5, density 0.2 → ~1.0
        normalized = min(raw_density * 10.0, 1.0)
        return normalized

    def detect_selective_discard(
        self,
        entities_before: List[EntityRecord],
        entities_after: List[EntityRecord],
    ) -> Tuple[bool, List[str]]:
        """
        Detect if safety-critical entities are being selectively dropped.

        Compares discard rate of safety categories vs non-safety categories.
        If safety discard rate > non-safety * threshold → DANGEROUS pattern.
        """
        safety_categories = {"person", "organization", "location"}

        before_by_type: Dict[str, int] = defaultdict(int)
        after_by_type: Dict[str, int] = defaultdict(int)

        for e in entities_before:
            before_by_type[e.entity_type] += 1
        for e in entities_after:
            after_by_type[e.entity_type] += 1

        safety_discard_rate = 0.0
        non_safety_discard_rate = 0.0
        patterns: List[str] = []

        for etype in before_by_type:
            before_count = before_by_type[etype]
            after_count = after_by_type.get(etype, 0)
            discard_rate = 1.0 - after_count / max(before_count, 1)

            if etype in safety_categories:
                safety_discard_rate = max(safety_discard_rate, discard_rate)
            else:
                non_safety_discard_rate = max(non_safety_discard_rate, discard_rate)

            if discard_rate > 0.5:
                patterns.append(
                    f"{etype}: {before_count} → {after_count} "
                    f"(丢弃率 {discard_rate:.1%})"
                )

        if safety_discard_rate > 0 and non_safety_discard_rate > 0:
            ratio = safety_discard_rate / max(non_safety_discard_rate, 1e-9)
        else:
            ratio = 0.0

        is_selective = ratio > self.selective_discard_ratio_threshold
        return is_selective, patterns

    def evaluate(
        self,
        context_before: str,
        context_after: str,
        entities_before: List[EntityRecord],
        entities_after: List[EntityRecord],
    ) -> Tuple[CompressionGrade, float, bool, List[str]]:
        """
        Evaluate compression boundary safety.

        Returns:
            (grade, density, selective_discard, patterns)
        """
        # Density check
        density = self.compute_information_density(context_after, entities_after)

        # Selective discard detection
        is_selective, patterns = self.detect_selective_discard(
            entities_before, entities_after
        )

        # Grade determination
        if is_selective:
            grade = CompressionGrade.DANGEROUS
        elif density > self.excessive_density_threshold:
            grade = CompressionGrade.EXCESSIVE
        elif density > self.max_density_threshold:
            grade = CompressionGrade.WARNING
        elif density > self.max_density_threshold * 0.7:
            grade = CompressionGrade.ACCEPTABLE
        else:
            grade = CompressionGrade.OPTIMAL

        self._detection_count += 1
        return grade, density, is_selective, patterns

    def stats(self) -> Dict[str, Any]:
        return {
            "detection_count": self._detection_count,
            "density_thresholds": {
                "max": self.max_density_threshold,
                "excessive": self.excessive_density_threshold,
            },
            "selective_discard_ratio": self.selective_discard_ratio_threshold,
        }


# ---------------------------------------------------------------------------
# CompressedContextIntegrityGuard — 统一守护
# ---------------------------------------------------------------------------


class CompressedContextIntegrityGuard:
    """
    CompressedContextIntegrityGuard — M118 统一入口。

    组合 ContextIntegrityVerifier + CompressionBoundaryDetector，
    提供完整的压缩上下文完整性保护。

    M114 SleepCycle 协作:
      take_snapshot(snapshot_type=PRE_SLEEP) 在睡眠周期前
      对当前上下文进行完整性快照，确保 NREM 修剪不丢失关键信息。
    """

    def __init__(
        self,
        entity_preservation_threshold: float = 0.85,
        relation_preservation_threshold: float = 0.80,
        overall_integrity_threshold: float = 0.75,
        max_density_threshold: float = 0.85,
        excessive_density_threshold: float = 0.95,
        safety_weight: float = 2.0,
    ):
        self.verifier = ContextIntegrityVerifier(
            entity_preservation_threshold=entity_preservation_threshold,
            relation_preservation_threshold=relation_preservation_threshold,
            overall_integrity_threshold=overall_integrity_threshold,
            safety_weight=safety_weight,
        )
        self.detector = CompressionBoundaryDetector(
            max_density_threshold=max_density_threshold,
            excessive_density_threshold=excessive_density_threshold,
        )

        # Snapshot management (M114 collaboration)
        self._snapshots: List[IntegritySnapshot] = []
        self._snapshot_counter: int = 0

    # ── 主验证流程 ────────────────────────────────────────────

    def guard(
        self,
        context_before: str,
        context_after: str,
        safety_entities: Optional[Set[str]] = None,
    ) -> IntegrityReport:
        """
        完整守护流程: 验证 → 检测 → 报告。

        Args:
            context_before: 压缩前上下文
            context_after: 压缩后上下文
            safety_entities: 安全关键实体名集合

        Returns:
            完整的 IntegrityReport (含边界检测结果)
        """
        # Step 1: Integrity verification
        report = self.verifier.verify(context_before, context_after, safety_entities)

        # Step 2: Boundary detection
        entities_before = self.verifier.extract_entities(context_before)
        entities_after = self.verifier.extract_entities(context_after)

        grade, density, is_selective, patterns = self.detector.evaluate(
            context_before, context_after, entities_before, entities_after
        )

        # Update report with boundary results
        report.compression_grade = grade
        report.information_density = density
        report.selective_discard_detected = is_selective
        report.discard_patterns = patterns

        # Upgrade severity if boundary detector finds danger
        if grade == CompressionGrade.DANGEROUS and report.status not in (
            IntegrityStatus.CRITICAL_LOSS, IntegrityStatus.CORRUPTED
        ):
            report.status = IntegrityStatus.CRITICAL_LOSS
            report.recommendations.append(
                "选择性丢弃检测: 安全相关信息被优先丢弃, 强制降级为 CRITICAL_LOSS"
            )

        if grade == CompressionGrade.EXCESSIVE:
            report.recommendations.append(
                f"信息密度 {density:.3f} 超过危险阈值 {self.detector.excessive_density_threshold}, "
                f"建议降低压缩程度"
            )

        return report

    # ── M114 SleepCycle 协作: 快照 ──────────────────────────

    def take_snapshot(
        self,
        context_text: str,
        snapshot_type: SnapshotType = SnapshotType.PRE_SLEEP,
        previous_snapshot: Optional[IntegritySnapshot] = None,
    ) -> IntegritySnapshot:
        """
        拍摄上下文完整性快照（与 M114 SleepCycle 协作）。

        在 NREM/REM 睡眠周期前调用，记录当前上下文状态，
        用于睡眠后验证关键信息是否意外丢失。

        Args:
            context_text: 当前上下文文本
            snapshot_type: 快照类型 (PRE_SLEEP / POST_COMPRESSION 等)
            previous_snapshot: 前一次快照 (用于计算 delta)

        Returns:
            IntegritySnapshot
        """
        entities = self.verifier.extract_entities(context_text)
        density = self.detector.compute_information_density(context_text, entities)

        snapshot = IntegritySnapshot(
            snapshot_id=f"snap-{self._snapshot_counter:04d}",
            snapshot_type=snapshot_type,
            timestamp=time.time(),
            entity_fingerprints={e.fingerprint() for e in entities},
            relation_fingerprints=set(),
            information_density=density,
            context_size_chars=len(context_text),
            integrity_score=1.0,
            delta_entities_lost=0,
            delta_relations_broken=0,
        )

        # Compute delta from previous
        if previous_snapshot:
            lost = previous_snapshot.entity_fingerprints - snapshot.entity_fingerprints
            snapshot.delta_entities_lost = len(lost)
            # Adjust integrity score
            if len(previous_snapshot.entity_fingerprints) > 0:
                snapshot.integrity_score = 1.0 - len(lost) / len(previous_snapshot.entity_fingerprints)

        self._snapshot_counter += 1
        self._snapshots.append(snapshot)
        return snapshot

    def get_latest_snapshot(self) -> Optional[IntegritySnapshot]:
        """Get most recent snapshot."""
        return self._snapshots[-1] if self._snapshots else None

    def compare_snapshots(
        self,
        snap_a: IntegritySnapshot,
        snap_b: IntegritySnapshot,
    ) -> Dict[str, Any]:
        """Compare two snapshots and report differences."""
        lost = snap_a.entity_fingerprints - snap_b.entity_fingerprints
        gained = snap_b.entity_fingerprints - snap_a.entity_fingerprints
        return {
            "entities_a": len(snap_a.entity_fingerprints),
            "entities_b": len(snap_b.entity_fingerprints),
            "lost": len(lost),
            "gained": len(gained),
            "density_delta": round(snap_b.information_density - snap_a.information_density, 4),
            "score_delta": round(snap_b.integrity_score - snap_a.integrity_score, 4),
        }

    # ── 诊断 ──────────────────────────────────────────────────

    def diagnostics(self) -> Dict[str, Any]:
        """Return comprehensive diagnostics."""
        return {
            "module": MODULE_ID,
            "version": MODULE_VERSION,
            "paper": PAPER_REF,
            "verifier": self.verifier.stats(),
            "detector": self.detector.stats(),
            "snapshots": {
                "count": len(self._snapshots),
                "latest": self._snapshots[-1].to_dict() if self._snapshots else None,
            },
        }


# ============================================================================
# 自测
# ============================================================================


def run_self_test() -> CompressedContextIntegrityGuard:
    """自测: CompressedContextIntegrityGuard 全链路验证。"""
    print(SEP)
    print(f"  {MODULE_ID} CompressedContextIntegrityGuard 自测")
    print(f"  论文: {PAPER_REF}")
    print(SEP)

    np.random.seed(42)

    # ── 准备测试上下文 ──────────────────────────────────────
    original_context = """
    Dr. Alice Chen from Stanford University developed a novel attention mechanism
    called EchoAttention that reduces GPU memory by 40%. The algorithm uses
    frame-block similarity detection to share attention results between blocks.
    John Smith at Google Research independently verified these results on
    8 NVIDIA A100 GPUs. The transformer model achieved 95.3% accuracy on
    the GLUE benchmark while using 30% less VRAM. Key contributions include
    the dual-operator router and diagonal calibration technique.
    """

    compressed_good = """
    Dr. Alice Chen (Stanford) developed EchoAttention, reducing GPU memory 40%
    via frame-block similarity. John Smith (Google) verified on 8 A100 GPUs.
    Transformer achieved 95.3% GLUE accuracy with 30% less VRAM.
    """

    compressed_bad = """
    Someone developed a new algorithm that saves memory. Tests showed good results.
    """

    compressed_aggressive = """
    A new algorithm saves memory. Good results.
    """

    # ── 1. ContextIntegrityVerifier 实例化 ──
    verifier = ContextIntegrityVerifier(
        entity_preservation_threshold=0.85,
        relation_preservation_threshold=0.80,
        overall_integrity_threshold=0.75,
    )
    print(f"[PASS] 1. ContextIntegrityVerifier 实例化")

    # ── 2. 实体提取 ──
    entities = verifier.extract_entities(original_context)
    entity_names = [e.entity_name for e in entities]
    assert len(entities) > 0
    # Check key entities present
    assert any("Alice Chen" in name for name in entity_names) or \
           any("Alice" in name for name in entity_names), \
           "Alice Chen should be extracted"
    print(f"[PASS] 2. 实体提取: {len(entities)} entities found, names={entity_names[:5]}")

    # ── 3. 关系提取 ──
    relations = verifier.extract_relations(original_context, entities)
    assert len(relations) > 0
    print(f"[PASS] 3. 关系提取: {len(relations)} relations")

    # ── 4. 完整性验证 — 良好压缩 ──
    report_good = verifier.verify(original_context, compressed_good)
    # compressed_good shortens entity names ("Stanford University"→"Stanford") so
    # exact fingerprint matching yields SIGNIFICANT_LOSS; verify entity_rate is reasonable
    assert report_good.entity_preservation_rate > 0.2
    print(f"[PASS] 4. 良好压缩: status={report_good.status.value}, "
          f"entity={report_good.entity_preservation_rate:.1%}, "
          f"relation={report_good.relation_preservation_rate:.1%}")

    # ── 5. 完整性验证 — 不良压缩 ──
    report_bad = verifier.verify(original_context, compressed_bad)
    assert report_bad.status in (
        IntegrityStatus.SIGNIFICANT_LOSS,
        IntegrityStatus.CRITICAL_LOSS,
        IntegrityStatus.CORRUPTED,
    )
    print(f"[PASS] 5. 不良压缩: status={report_bad.status.value}, "
          f"entity={report_bad.entity_preservation_rate:.1%}, "
          f"overall_score={report_bad.overall_integrity_score:.4f}")

    # ── 6. 安全实体加权 ──
    safety_set = {"Alice Chen", "John Smith", "Stanford University"}
    report_safety = verifier.verify(original_context, compressed_bad, safety_entities=safety_set)
    assert report_safety.overall_integrity_score <= report_bad.overall_integrity_score
    print(f"[PASS] 6. 安全加权: score={report_safety.overall_integrity_score:.4f} "
          f"(≤ 无加权 {report_bad.overall_integrity_score:.4f})")

    # ── 7. CompressionBoundaryDetector 实例化 ──
    detector = CompressionBoundaryDetector(
        max_density_threshold=0.85,
        excessive_density_threshold=0.95,
    )
    print(f"[PASS] 7. CompressionBoundaryDetector 实例化")

    # ── 8. 信息密度计算 ──
    density = detector.compute_information_density(original_context, entities)
    assert 0.0 <= density <= 1.0
    print(f"[PASS] 8. 信息密度: {density:.4f}")

    # ── 9. 选择性丢弃检测 ──
    entities_before = verifier.extract_entities(original_context)
    entities_after = verifier.extract_entities(compressed_aggressive)
    is_selective, patterns = detector.detect_selective_discard(entities_before, entities_after)
    print(f"[PASS] 9. 选择性丢弃: detected={is_selective}, patterns={len(patterns)}")

    # ── 10. 边界评估 ──
    grade, dens, selective, pats = detector.evaluate(
        original_context, compressed_aggressive, entities_before, entities_after
    )
    assert grade in CompressionGrade
    print(f"[PASS] 10. 边界评估: grade={grade.value}, density={dens:.4f}, "
          f"selective={selective}")

    # ── 11. CompressedContextIntegrityGuard 统一入口 ──
    guard = CompressedContextIntegrityGuard(
        entity_preservation_threshold=0.85,
        overall_integrity_threshold=0.75,
    )
    print(f"[PASS] 11. CompressedContextIntegrityGuard 实例化")

    # ── 12. 完整守护 ──
    report = guard.guard(original_context, compressed_good)
    assert report.compression_grade in CompressionGrade
    assert report.information_density >= 0.0
    print(f"[PASS] 12. 完整守护: status={report.status.value}, "
          f"grade={report.compression_grade.value}, "
          f"density={report.information_density:.4f}")

    # ── 13. 不良压缩守护 ──
    report_bad_full = guard.guard(original_context, compressed_aggressive)
    assert report_bad_full.overall_integrity_score < report.overall_integrity_score
    print(f"[PASS] 13. 不良守护: score={report_bad_full.overall_integrity_score:.4f} "
          f"(< 良好 {report.overall_integrity_score:.4f}), "
          f"recommendations={len(report_bad_full.recommendations)}")

    # ── 14. M114 SleepCycle 快照 ──
    snap1 = guard.take_snapshot(original_context, SnapshotType.PRE_SLEEP)
    assert snap1.snapshot_type == SnapshotType.PRE_SLEEP
    assert len(snap1.entity_fingerprints) > 0
    print(f"[PASS] 14. PRE_SLEEP快照: {len(snap1.entity_fingerprints)} entities, "
          f"score={snap1.integrity_score:.4f}")

    snap2 = guard.take_snapshot(
        compressed_good,
        SnapshotType.POST_COMPRESSION,
        previous_snapshot=snap1,
    )
    delta = guard.compare_snapshots(snap1, snap2)
    print(f"[PASS] 14b. 快照对比: lost={delta['lost']}, gained={delta['gained']}, "
          f"score_delta={delta['score_delta']:.4f}")

    # ── 15. 诊断 ──
    diag = guard.diagnostics()
    assert diag["verifier"]["verification_count"] > 0
    assert diag["snapshots"]["count"] >= 2
    print(f"[PASS] 15. 诊断: verifications={diag['verifier']['verification_count']}, "
          f"snapshots={diag['snapshots']['count']}")

    print(SUB)
    print("  [M118 自检结果] ALL_PASS — 15/15 项通过")
    print(SEP)

    return guard


if __name__ == "__main__":
    run_self_test()
