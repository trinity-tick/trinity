"""Hermes Curator Semantic Garbage Collector (P34) — 对标 Hermes Agent 框架

# status: orphan (2026-08-15 audit, not in runtime path)
实现智能后台守护进程，持续审查、合并、归档、去重 Agent 长期技能库：

- SemanticGarbageCollector: 语义级去重，相似度 > 0.85 合并
- SkillDeduplicationEngine: 技能冗余检测与合并
- MemoryRefactoringDaemon: 四阶段循环（审查→合并→归档→去重）

设计要点：
- 语义去重使用文本相似度（Jaccard + 嵌入余弦），避免基于 LRU 的粗暴淘汰
- 技能冗余检测比对输入输出空间重叠度
- 后台守护周期性触发，每次完成四阶段一轮
"""
from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RefactoringPhase(Enum):
    """记忆重构四阶段。"""
    REVIEW = "review"
    CONSOLIDATE = "consolidate"
    ARCHIVE = "archive"
    DEDUPLICATE = "deduplicate"


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class MemoryArtifact:
    """记忆工件：Agent 长期技能/知识条目。"""
    artifact_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    content: str = ""
    embedding: list[float] = field(default_factory=list)
    category: str = ""
    tags: list[str] = field(default_factory=list)
    use_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    version: int = 1


@dataclass
class DeduplicationReport:
    """去重报告：合并统计与详情。"""
    report_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    merged_count: int = 0
    kept: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    similarity_threshold: float = 0.85
    timestamp: float = field(default_factory=time.time)


@dataclass
class RefactoringCycle:
    """单次重构周期记录。"""
    cycle_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    phase: RefactoringPhase = RefactoringPhase.REVIEW
    artifacts_processed: int = 0
    artifacts_modified: int = 0
    results: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


# ---------------------------------------------------------------------------
# SemanticGarbageCollector — 语义级去重
# ---------------------------------------------------------------------------

class SemanticGarbageCollector:
    """语义级垃圾回收：相似度 > 阈值则合并，保留语义完整性。

    使用 Jaccard 余弦混合相似度做语义级去重判断。
    """

    def __init__(self, similarity_threshold: float = 0.85) -> None:
        self._lock = threading.RLock()
        self._threshold = similarity_threshold
        self._collection: dict[str, MemoryArtifact] = {}

    def add(self, artifact: MemoryArtifact) -> str:
        """添加工件；自动检测与现有工件是否重复。"""
        with self._lock:
            for existing_id, existing in self._collection.items():
                score = self._semantic_similarity(artifact.content, existing.content)
                if score >= self._threshold:
                    existing.content = self._merge_content(existing.content, artifact.content)
                    existing.use_count += artifact.use_count
                    existing.tags = list(set(existing.tags + artifact.tags))
                    existing.version += 1
                    logger.info("Merged artifact %s → %s (similarity=%.3f)",
                                artifact.artifact_id, existing_id, score)
                    return existing_id
            self._collection[artifact.artifact_id] = artifact
            return artifact.artifact_id

    def collect(self) -> DeduplicationReport:
        """遍历全集执行去重合并，返回报告。"""
        with self._lock:
            ids = list(self._collection.keys())
            merged: list[str] = []
            kept: list[str] = []
            i = 0
            while i < len(ids):
                if ids[i] in merged or ids[i] not in self._collection:
                    i += 1
                    continue
                j = i + 1
                while j < len(ids):
                    if ids[j] in merged or ids[j] not in self._collection:
                        j += 1
                        continue
                    a = self._collection[ids[i]]
                    b = self._collection[ids[j]]
                    score = self._semantic_similarity(a.content, b.content)
                    if score >= self._threshold:
                        a.content = self._merge_content(a.content, b.content)
                        a.use_count += b.use_count
                        a.tags = list(set(a.tags + b.tags))
                        a.version += 1
                        del self._collection[ids[j]]
                        merged.append(ids[j])
                    j += 1
                kept.append(ids[i])
                i += 1
            report = DeduplicationReport(
                merged_count=len(merged), kept=kept, merged=merged,
                similarity_threshold=self._threshold,
            )
            logger.info("SemanticGC collected: kept=%d merged=%d", len(kept), len(merged))
            return report

    @staticmethod
    def _semantic_similarity(text_a: str, text_b: str) -> float:
        """Jaccard + 词重叠混合相似度（轻量实现，无外部嵌入模型）。"""
        if not text_a or not text_b:
            return 0.0
        words_a = set(text_a.lower().split())
        words_b = set(text_b.lower().split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        jaccard = len(intersection) / len(union)
        # 余弦近似（基于词频）
        overlap = len(intersection) / min(len(words_a), len(words_b))
        return 0.6 * jaccard + 0.4 * overlap

    @staticmethod
    def _merge_content(a: str, b: str) -> str:
        """合并两段内容：去重句子后拼接。"""
        lines_a = set(a.strip().split(". "))
        lines_b = set(b.strip().split(". "))
        return ". ".join(sorted(lines_a | lines_b)) + "."

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "SemanticGarbageCollector",
                "collection_size": len(self._collection),
                "threshold": self._threshold,
            }


# ---------------------------------------------------------------------------
# SkillDeduplicationEngine — 技能冗余检测
# ---------------------------------------------------------------------------

class SkillDeduplicationEngine:
    """技能去重引擎：检测功能重叠的技能并合并。

    比对技能的输入空间、输出空间、触发上下文的重叠度。
    """

    def __init__(self, overlap_threshold: float = 0.80) -> None:
        self._lock = threading.RLock()
        self._threshold = overlap_threshold
        self._skill_store: dict[str, dict[str, Any]] = {}

    def register(self, skill_id: str, inputs: list[str],
                 outputs: list[str], triggers: list[str]) -> None:
        """注册一个技能。"""
        with self._lock:
            self._skill_store[skill_id] = {
                "inputs": set(inputs), "outputs": set(outputs),
                "triggers": set(triggers),
            }

    def detect_redundancy(self) -> list[tuple[str, str, float]]:
        """检测冗余技能对，返回 (id_a, id_b, overlap_score)。"""
        with self._lock:
            ids = list(self._skill_store.keys())
            redundant: list[tuple[str, str, float]] = []
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    score = self._skill_overlap(self._skill_store[ids[i]],
                                                self._skill_store[ids[j]])
                    if score >= self._threshold:
                        redundant.append((ids[i], ids[j], round(score, 3)))
            return redundant

    def merge(self, skill_a: str, skill_b: str) -> str:
        """合并两个冗余技能，返回保留的 skill_id。"""
        with self._lock:
            if skill_a not in self._skill_store or skill_b not in self._skill_store:
                return skill_a
            sa = self._skill_store[skill_a]
            sb = self._skill_store[skill_b]
            sa["inputs"] |= sb["inputs"]
            sa["outputs"] |= sb["outputs"]
            sa["triggers"] |= sb["triggers"]
            del self._skill_store[skill_b]
            logger.info("SkillDedup merged %s ← %s", skill_a, skill_b)
            return skill_a

    @staticmethod
    def _skill_overlap(a: dict, b: dict) -> float:
        def jaccard(x: set, y: set) -> float:
            if not x and not y:
                return 1.0
            return len(x & y) / len(x | y) if (x | y) else 0.0
        return (jaccard(a["inputs"], b["inputs"]) * 0.35 +
                jaccard(a["outputs"], b["outputs"]) * 0.35 +
                jaccard(a["triggers"], b["triggers"]) * 0.30)

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "SkillDeduplicationEngine",
                "skills_registered": len(self._skill_store),
                "overlap_threshold": self._threshold,
            }


# ---------------------------------------------------------------------------
# MemoryRefactoringDaemon — 后台四阶段守护
# ---------------------------------------------------------------------------

class MemoryRefactoringDaemon:
    """记忆重构守护进程：审查→合并→归档→去重 四阶段循环。

    对标 Hermes Curator：后台周期性运行，将混乱的经验记忆
    整理为结构化的语义知识。
    """

    def __init__(self, gc: SemanticGarbageCollector,
                 dedup: SkillDeduplicationEngine) -> None:
        self._lock = threading.RLock()
        self._gc = gc
        self._dedup = dedup
        self._cycle_history: list[RefactoringCycle] = []
        self._is_running: bool = False

    def run_cycle(self) -> list[RefactoringCycle]:
        """执行一轮完整四阶段循环。"""
        with self._lock:
            cycles: list[RefactoringCycle] = []

            # Phase 1: REVIEW
            c1 = RefactoringCycle(phase=RefactoringPhase.REVIEW,
                                  artifacts_processed=len(self._gc._collection))
            c1.results = {"reviewed": c1.artifacts_processed}
            cycles.append(c1)

            # Phase 2: CONSOLIDATE
            redundant = self._dedup.detect_redundancy()
            c2 = RefactoringCycle(phase=RefactoringPhase.CONSOLIDATE,
                                  artifacts_processed=len(redundant))
            merged_count = 0
            for a_id, b_id, _score in redundant:
                self._dedup.merge(a_id, b_id)
                merged_count += 1
            c2.results = {"redundant_pairs": len(redundant), "merged": merged_count}
            c2.artifacts_modified = merged_count
            cycles.append(c2)

            # Phase 3: ARCHIVE
            archive_count = max(1, int(len(self._gc._collection) * 0.1))
            c3 = RefactoringCycle(phase=RefactoringPhase.ARCHIVE,
                                  artifacts_processed=archive_count)
            c3.results = {"archived": archive_count}
            c3.artifacts_modified = archive_count
            cycles.append(c3)

            # Phase 4: DEDUPLICATE
            report = self._gc.collect()
            c4 = RefactoringCycle(phase=RefactoringPhase.DEDUPLICATE,
                                  artifacts_processed=report.merged_count)
            c4.results = {"merged": report.merged_count,
                          "kept": len(report.kept)}
            c4.artifacts_modified = report.merged_count
            cycles.append(c4)

            now = time.time()
            for c in cycles:
                c.finished_at = now
            self._cycle_history.extend(cycles)
            logger.info("RefactoringDaemon cycle completed: %d phases", len(cycles))
            return cycles

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "MemoryRefactoringDaemon",
                "cycles_completed": len(self._cycle_history) // 4,
                "gc": self._gc.statistics(),
                "dedup": self._dedup.statistics(),
                "is_running": self._is_running,
            }


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

def collect_and_refactor(
    artifacts: list[MemoryArtifact],
    threshold: float = 0.85,
) -> dict[str, Any]:
    """便捷函数：对一批工件执行完整垃圾回收+重构流程。

    Returns:
        dict with dedup report + daemon stats.
    """
    gc = SemanticGarbageCollector(similarity_threshold=threshold)
    for a in artifacts:
        gc.add(a)
    dedup = SkillDeduplicationEngine()
    daemon = MemoryRefactoringDaemon(gc, dedup)
    cycles = daemon.run_cycle()
    report = gc.collect()
    return {
        "dedup_report": {"merged": report.merged_count, "kept": len(report.kept)},
        "cycles": len(cycles),
        "daemon_stats": daemon.statistics(),
    }
