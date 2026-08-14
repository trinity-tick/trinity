"""
CB75: AnchorFactTracker — 锚定事实追踪
=======================================

锚定事实与关联上下文的协同追踪。

核心设计:
  - AnchorFact: 为关键事实创建稳定锚点(人名/日期/决策)，
    不可变锚定信息锚定记忆
  - AssociativeContext: 将锚定事实与来源对话段、相关实体、推论关联
  - FactEvolutionTracker: 追踪锚定事实随时间演变(新增/修正/否定/过期)，
    维护 FactVersion 版本链
  - ContextualAnchoring: 检索时将查询锚定到最相关事实再展开关联上下文，
    避免语义漂移
  - AFConflictDetector: 冲突检测——新事实与历史矛盾时标记
  - ConfidenceDecayScheduler: 未确认事实随时间降低置信度

Reference:
  - AnchorMem: Anchored Facts with Associative Contexts (ACL 2026)
"""

from __future__ import annotations

import hashlib
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

class FactAction(Enum):
    ADDED = "added"          # 新增
    CORRECTED = "corrected"  # 修正
    NEGATED = "negated"      # 被否定
    EXPIRED = "expired"      # 过期
    CONFIRMED = "confirmed"  # 确认


class AnchorType(Enum):
    PERSON = "person"
    DATE = "date"
    DECISION = "decision"
    LOCATION = "location"
    ENTITY = "entity"
    QUANTITY = "quantity"


class ConflictSeverity(Enum):
    NONE = "none"
    MINOR = "minor"        # 细微差异
    MAJOR = "major"        # 明显矛盾
    CRITICAL = "critical"  # 根本冲突


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class AnchorPoint:
    """事实锚点——不可变核心标识。"""
    anchor_id: str
    anchor_type: AnchorType = AnchorType.ENTITY
    anchor_value: str = ""
    created_at: float = field(default_factory=_time.time)

    def __hash__(self):
        return hash(self.anchor_id)

    def __eq__(self, other):
        if isinstance(other, AnchorPoint):
            return self.anchor_id == other.anchor_id
        return False


@dataclass
class FactVersion:
    """事实版本——追踪事实演变链。

    Attributes:
        version_id: 版本标识。
        fact_content: 事实内容。
        action: 本次变更类型。
        parent_version: 前一版本 ID。
        timestamp: 版本时间戳。
        confidence: 置信度。
        source: 来源标识。
    """
    version_id: str
    fact_content: str
    action: FactAction = FactAction.ADDED
    parent_version: Optional[str] = None
    timestamp: float = field(default_factory=_time.time)
    confidence: float = 1.0
    source: str = ""


@dataclass
class AnchorFact:
    """锚定事实——关键事实 + 锚点 + 版本链。

    Attributes:
        fact_id: 事实唯一标识。
        anchor: 关联锚点。
        current_version: 当前版本。
        version_chain: 完整版本链。
        contexts: 关联上下文列表。
        is_active: 是否活跃(未被否定/过期)。
    """
    fact_id: str
    anchor: AnchorPoint = field(default_factory=lambda: AnchorPoint(anchor_id=""))
    current_version: Optional[FactVersion] = None
    version_chain: List[FactVersion] = field(default_factory=list)
    contexts: List[str] = field(default_factory=list)
    is_active: bool = True
    created_at: float = field(default_factory=_time.time)


@dataclass
class AssociativeContext:
    """关联上下文——事实的来源与推导。"""
    context_id: str
    fact_id: str = ""
    source_dialogue: str = ""
    related_entities: List[str] = field(default_factory=list)
    inferences: List[str] = field(default_factory=list)
    confidence: float = 0.5
    created_at: float = field(default_factory=_time.time)


@dataclass
class ConflictRecord:
    """冲突记录。"""
    conflict_id: str
    fact_a_id: str
    fact_b_id: str
    severity: ConflictSeverity = ConflictSeverity.NONE
    description: str = ""
    detected_at: float = field(default_factory=_time.time)
    resolved: bool = False


# ============================================================================
# FactEvolutionTracker
# ============================================================================

class FactEvolutionTracker:
    """事实演变追踪器——维护版本链。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._facts: Dict[str, AnchorFact] = {}
        self._versions: Dict[str, FactVersion] = {}

    def create_fact(
        self, fact_id: str, content: str, anchor: AnchorPoint,
        confidence: float = 1.0, source: str = "",
    ) -> AnchorFact:
        """创建新锚定事实。"""
        with self._lock:
            v1 = FactVersion(
                version_id=f"{fact_id}_v1", fact_content=content,
                action=FactAction.ADDED, confidence=confidence, source=source,
            )
            fact = AnchorFact(
                fact_id=fact_id, anchor=anchor, current_version=v1,
                version_chain=[v1],
            )
            self._facts[fact_id] = fact
            self._versions[v1.version_id] = v1
            return fact

    def update_fact(self, fact_id: str, new_content: str, action: FactAction) -> Optional[FactVersion]:
        """更新事实，追加版本。

        Args:
            fact_id: 事实 ID。
            new_content: 新内容。
            action: 变更类型。

        Returns:
            新版本对象。
        """
        with self._lock:
            fact = self._facts.get(fact_id)
            if fact is None or not fact.is_active:
                return None

            version_num = len(fact.version_chain) + 1
            new_version = FactVersion(
                version_id=f"{fact_id}_v{version_num}",
                fact_content=new_content,
                action=action,
                parent_version=fact.current_version.version_id if fact.current_version else None,
            )
            fact.version_chain.append(new_version)
            fact.current_version = new_version
            self._versions[new_version.version_id] = new_version

            if action in (FactAction.NEGATED, FactAction.EXPIRED):
                fact.is_active = False

            return new_version

    def get_evolution(self, fact_id: str) -> List[FactVersion]:
        with self._lock:
            fact = self._facts.get(fact_id)
            return list(fact.version_chain) if fact else []

    def get_fact(self, fact_id: str) -> Optional[AnchorFact]:
        with self._lock:
            return self._facts.get(fact_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            active = sum(1 for f in self._facts.values() if f.is_active)
            return {"total_facts": len(self._facts), "active_facts": active, "total_versions": len(self._versions)}


# ============================================================================
# AFConflictDetector
# ============================================================================

class AFConflictDetector:
    """冲突检测器——新事实与历史矛盾时标记。"""

    def __init__(self, similarity_threshold: float = 0.8):
        self.similarity_threshold = similarity_threshold
        self._lock = threading.RLock()
        self._conflicts: Dict[str, ConflictRecord] = {}

    def detect(
        self, new_fact: AnchorFact, existing_facts: Dict[str, AnchorFact],
    ) -> List[ConflictRecord]:
        """检测新事实与既有事实的冲突。

        Args:
            new_fact: 新锚定事实。
            existing_facts: 既有事实字典。

        Returns:
            检测到的冲突列表。
        """
        with self._lock:
            conflicts = []
            new_content = new_fact.current_version.fact_content if new_fact.current_version else ""
            new_words = set(new_content.lower().split())

            for fid, efact in existing_facts.items():
                if fid == new_fact.fact_id or not efact.is_active:
                    continue
                if efact.current_version is None:
                    continue
                old_words = set(efact.current_version.fact_content.lower().split())
                overlap = len(new_words & old_words)
                union = len(new_words | old_words)
                if union == 0:
                    continue
                jaccard = overlap / union

                # High overlap + same anchor type → potential conflict with different content
                if jaccard > self.similarity_threshold and efact.fact_id != new_fact.fact_id:
                    # Check for negation patterns
                    has_negation = any(w in new_content.lower() for w in ("not", "no", "never", "wrong", "incorrect"))
                    severity = ConflictSeverity.CRITICAL if has_negation else ConflictSeverity.MAJOR

                    cr = ConflictRecord(
                        conflict_id=f"cf_{hashlib.md5(f'{new_fact.fact_id}_{fid}'.encode()).hexdigest()[:12]}",
                        fact_a_id=new_fact.fact_id, fact_b_id=fid, severity=severity,
                        description=f"Jaccard={jaccard:.2f} between {new_fact.fact_id} and {fid}",
                    )
                    conflicts.append(cr)
                    self._conflicts[cr.conflict_id] = cr

            return conflicts

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_conflicts": len(self._conflicts),
                "unresolved": sum(1 for c in self._conflicts.values() if not c.resolved),
            }


# ============================================================================
# ConfidenceDecayScheduler
# ============================================================================

class ConfidenceDecayScheduler:
    """置信度衰减调度器——未确认事实随时间降低置信度。"""

    def __init__(self, half_life_hours: float = 168.0, floor: float = 0.1):
        self.half_life_hours = half_life_hours   # 半衰期（默认 7 天）
        self.floor = floor                        # 最低置信度
        self._lock = threading.RLock()
        self._decay_count: int = 0

    def decay(self, fact: AnchorFact, current_time: Optional[float] = None) -> float:
        """计算衰减后的置信度。

        Args:
            fact: 锚定事实。
            current_time: 当前时间。

        Returns:
            衰减后的置信度。
        """
        if current_time is None:
            current_time = _time.time()
        if fact.current_version is None:
            return self.floor

        with self._lock:
            self._decay_count += 1
            age_hours = (current_time - fact.current_version.timestamp) / 3600.0
            decayed = fact.current_version.confidence * (0.5 ** (age_hours / self.half_life_hours))
            return max(decayed, self.floor)

    def apply_decay(self, facts: Dict[str, AnchorFact], current_time: Optional[float] = None):
        """批量衰减，将低于阈值的标记为过期。"""
        if current_time is None:
            current_time = _time.time()
        with self._lock:
            for fact in facts.values():
                if not fact.is_active or fact.current_version is None:
                    continue
                conf = self.decay(fact, current_time)
                if conf <= self.floor and fact.current_version.action != FactAction.CONFIRMED:
                    fact.is_active = False
                    logger.info(f"Fact {fact.fact_id} decayed to expired (conf={conf:.3f})")

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"half_life_hours": self.half_life_hours, "floor": self.floor, "decay_count": self._decay_count}


# ============================================================================
# ContextualAnchoring
# ============================================================================

class ContextualAnchoring:
    """上下文锚定——查询→最相关事实→展开关联上下文。"""

    def __init__(self, max_contexts: int = 5):
        self.max_contexts = max_contexts
        self._lock = threading.RLock()
        self._contexts: Dict[str, AssociativeContext] = {}

    def add_context(self, context: AssociativeContext):
        with self._lock:
            self._contexts[context.context_id] = context

    def anchor_and_expand(
        self, query: str, facts: Dict[str, AnchorFact],
    ) -> List[Tuple[AnchorFact, List[AssociativeContext]]]:
        """锚定查询到最相关事实，展开关联上下文。

        Args:
            query: 查询文本。
            facts: 所有锚定事实。

        Returns:
            [(事实, 关联上下文列表), ...] 按相关性排序。
        """
        with self._lock:
            query_words = set(query.lower().split())
            scored = []
            for fact in facts.values():
                if not fact.is_active or fact.current_version is None:
                    continue
                content_words = set(fact.current_version.fact_content.lower().split())
                score = len(query_words & content_words) / max(len(query_words), 1)
                if score > 0:
                    contexts = [self._contexts[cid] for cid in fact.contexts if cid in self._contexts]
                    scored.append((score, fact, contexts[:self.max_contexts]))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [(f, c) for _, f, c in scored]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"total_contexts": len(self._contexts), "max_contexts": self.max_contexts}


# ============================================================================
# Main Class
# ============================================================================

class AnchorFactTracker:
    """锚定事实追踪 (CB75)。

    统一入口——事实创建/演变/冲突检测/衰减/锚定检索。

    Usage:
        aft = AnchorFactTracker()
        anchor = AnchorPoint(anchor_id="a1", anchor_type=AnchorType.DECISION, anchor_value="Go to Paris")
        fact = aft.create_fact("f1", "Decided to go to Paris in August", anchor)
        aft.update_fact("f1", "Decided to go to Paris in September", FactAction.CORRECTED)
        evolution = aft.get_evolution("f1")
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.evolution = FactEvolutionTracker()
        self.conflict_detector = AFConflictDetector()
        self.decay_scheduler = ConfidenceDecayScheduler()
        self.anchoring = ContextualAnchoring()
        self._start_time = _time.time()

    def create_fact(
        self, fact_id: str, content: str, anchor: AnchorPoint,
        confidence: float = 1.0, source: str = "",
    ) -> AnchorFact:
        fact = self.evolution.create_fact(fact_id, content, anchor, confidence, source)
        with self._lock:
            # Check conflicts against other facts
            self.conflict_detector.detect(fact, self.evolution._facts)
        return fact

    def update_fact(self, fact_id: str, new_content: str, action: FactAction) -> Optional[FactVersion]:
        new_version = self.evolution.update_fact(fact_id, new_content, action)
        return new_version

    def get_evolution(self, fact_id: str) -> List[FactVersion]:
        return self.evolution.get_evolution(fact_id)

    def add_context(self, context: AssociativeContext):
        with self._lock:
            fact = self.evolution.get_fact(context.fact_id)
            if fact:
                fact.contexts.append(context.context_id)
            self.anchoring.add_context(context)

    def anchor_search(self, query: str) -> List[Tuple[AnchorFact, List[AssociativeContext]]]:
        with self._lock:
            return self.anchoring.anchor_and_expand(query, self.evolution._facts)

    def apply_decay(self):
        self.decay_scheduler.apply_decay(self.evolution._facts)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "class": "AnchorFactTracker (CB75)",
                "evolution": self.evolution.statistics(),
                "conflicts": self.conflict_detector.statistics(),
                "decay": self.decay_scheduler.statistics(),
                "anchoring": self.anchoring.statistics(),
                "uptime_seconds": round(_time.time() - self._start_time, 3),
            }
