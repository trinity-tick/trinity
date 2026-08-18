"""
# status: orphan (2026-08-15 audit, not in runtime path)
P6-1: Logic Rule Memory Layer (对标 NS-Mem)
============================================

在现有情景层 (second_brain/curation_state) 和语义层 (kgraph) 之上构建
第三层——逻辑规则层。实现规则形式化（IF-THEN、约束、偏好推理）、
SK-Gen 风格自动规则构建（从多模态经验中巩固结构化知识）、增量更新
（新增规则不覆盖旧规则，冲突自动标记）。

NS-Mem 三层记忆架构：
  Layer 1 — Episodic Layer:   情景记忆（原始对话/事件流）
  Layer 2 — Semantic Layer:   语义记忆（实体/关系/知识图谱）
  Layer 3 — Logic Rule Layer: 逻辑规则记忆（IF-THEN/约束/偏好）

SK-Gen 自动构建：
  - 从累积的多模态经验中自动巩固结构化知识
  - 增量更新神经表征和符号规则

Reference: Jiang et al., "Advancing Multimodal Agent Reasoning with
           Long-Term Neuro-Symbolic Memory", arXiv:2603.15280, Mar 2026.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────────

class RuleCategory(Enum):
    """规则类别。"""
    IF_THEN = "if_then"           # 条件式规则: IF condition THEN action
    CONSTRAINT = "constraint"     # 约束规则: 禁止/限制条件
    PREFERENCE = "preference"     # 偏好规则: soft preference
    DEDUCTION = "deduction"       # 演绎规则: 从已知推导未知
    HEURISTIC = "heuristic"       # 启发式规则: 经验法则


class RuleStatus(Enum):
    """规则状态。"""
    ACTIVE = "active"             # 活跃使用中
    SUPERSEDED = "superseded"     # 被更新的规则替换
    CONFLICTING = "conflicting"   # 与其他规则冲突
    DEPRECATED = "deprecated"     # 已废弃
    PENDING_VERIFICATION = "pending"  # 待验证


class ConflictType(Enum):
    """规则冲突类型。"""
    DIRECT_CONTRADICTION = "direct_contradiction"  # 直接矛盾
    OVERLAP_INCONSISTENCY = "overlap_inconsistency"  # 重叠但不一致
    PRECEDENCE_CONFLICT = "precedence_conflict"    # 优先级冲突
    SCOPE_INTERSECTION = "scope_intersection"      # 作用域交叉


class ConsolidationStrategy(Enum):
    """SK-Gen 风格的规则巩固策略。"""
    PATTERN_EXTRACTION = "pattern_extraction"    # 从重复模式提取
    FREQUENCY_THRESHOLD = "frequency_threshold"  # 基于频率阈值
    CONFIDENCE_WEIGHTED = "confidence_weighted"  # 基于置信度加权
    TEMPORAL_CONSISTENCY = "temporal_consistency"  # 时序一致性
    CONTRASTIVE_REFINEMENT = "contrastive_refinement"  # 对比精炼


# ── 数据结构 ─────────────────────────────────────────────────────────

@dataclass
class LogicRule:
    """形式化逻辑规则。

    Args:
        rule_id: 唯一标识
        category: 规则类别
        condition: 条件表达式（自然语言或结构化）
        action: 动作/结论表达式
        confidence: 置信度 [0.0, 1.0]
        priority: 优先级（越大越优先）
        source_episodes: 来源情景ID列表（可追溯）
        created_at: 创建时间戳
        updated_at: 最后更新时间戳
        status: 规则状态
    """
    rule_id: str = field(default_factory=lambda: f"rule_{uuid.uuid4().hex[:12]}")
    category: RuleCategory = RuleCategory.IF_THEN
    condition: str = ""
    action: str = ""
    confidence: float = 0.5
    priority: int = 0
    source_episodes: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    status: RuleStatus = RuleStatus.ACTIVE
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category.value,
            "condition": self.condition,
            "action": self.action,
            "confidence": self.confidence,
            "priority": self.priority,
            "source_episodes": self.source_episodes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status.value,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LogicRule":
        return cls(
            rule_id=d.get("rule_id", ""),
            category=RuleCategory(d.get("category", "if_then")),
            condition=d.get("condition", ""),
            action=d.get("action", ""),
            confidence=d.get("confidence", 0.5),
            priority=d.get("priority", 0),
            source_episodes=d.get("source_episodes", []),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            status=RuleStatus(d.get("status", "active")),
            metadata=d.get("metadata", {}),
        )


@dataclass
class RuleConflict:
    """规则冲突记录。

    Args:
        conflict_id: 冲突唯一标识
        rule_a: 冲突方A
        rule_b: 冲突方B
        conflict_type: 冲突类型
        description: 冲突描述
        detected_at: 检测时间戳
        resolved: 是否已解决
        resolution: 解决方式描述
    """
    conflict_id: str = field(default_factory=lambda: f"cfl_{uuid.uuid4().hex[:12]}")
    rule_a: str = ""
    rule_b: str = ""
    conflict_type: ConflictType = ConflictType.DIRECT_CONTRADICTION
    description: str = ""
    detected_at: float = field(default_factory=time.time)
    resolved: bool = False
    resolution: str = ""


@dataclass
class ConsolidationTrace:
    """SK-Gen 规则巩固的执行轨迹。

    Args:
        trace_id: 唯一标识
        strategy: 使用的巩固策略
        input_episode_count: 输入情景数量
        extracted_rules: 提取到的规则ID列表
        confidence_scores: 各规则的置信度
        timestamp: 执行时间
    """
    trace_id: str = field(default_factory=lambda: f"skg_{uuid.uuid4().hex[:12]}")
    strategy: ConsolidationStrategy = ConsolidationStrategy.PATTERN_EXTRACTION
    input_episode_count: int = 0
    extracted_rules: List[str] = field(default_factory=list)
    confidence_scores: Dict[str, float] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class QueryContext:
    """推理查询上下文。

    Args:
        entities: 相关实体列表
        relations: 已知关系
        constraints: 约束条件
        preferences: 偏好
        max_rules: 最大激活规则数
    """
    entities: List[str] = field(default_factory=list)
    relations: Dict[str, str] = field(default_factory=dict)
    constraints: List[str] = field(default_factory=list)
    preferences: List[str] = field(default_factory=list)
    max_rules: int = 20


# ── 逻辑规则层 ───────────────────────────────────────────────────────

class LogicRuleLayer:
    """NS-Mem 风格的逻辑规则记忆层。

    在情景记忆和语义记忆之上，构建第三层——逻辑规则层。
    支持 IF-THEN 规则、约束、偏好推理，以及 SK-Gen 自动规则构建。

    Attributes:
        rules: 规则存储（rule_id → LogicRule）
        rule_index: 按类别索引规则
        conflicts: 冲突记录列表
        consolidation_history: SK-Gen 巩固历史
        max_conflicts_history: 最大冲突历史保留数
    """

    def __init__(self, max_conflicts_history: int = 1000):
        self.rules: Dict[str, LogicRule] = {}
        self._rule_index: Dict[RuleCategory, List[str]] = defaultdict(list)
        self.conflicts: deque = deque(maxlen=max_conflicts_history)
        self.consolidation_history: deque = deque(maxlen=500)
        self._lock = threading.RLock()

        self._stats: Dict[str, int] = {
            "total_rules_added": 0,
            "total_rules_superseded": 0,
            "total_conflicts_detected": 0,
            "total_consolidations": 0,
            "total_queries": 0,
            "total_inferences": 0,
        }

    # ── 规则管理 ─────────────────────────────────────────────────

    def add_rule(
        self,
        category: RuleCategory,
        condition: str,
        action: str,
        confidence: float = 0.5,
        priority: int = 0,
        source_episodes: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LogicRule:
        """添加新规则。增量更新——不覆盖旧规则。

        Args:
            category: 规则类别
            condition: 条件表达式
            action: 动作/结论
            confidence: 置信度 [0.0, 1.0]
            priority: 优先级
            source_episodes: 来源情景
            metadata: 附加元数据

        Returns:
            新创建的 LogicRule 对象
        """
        rule = LogicRule(
            category=category,
            condition=condition,
            action=action,
            confidence=max(0.0, min(1.0, confidence)),
            priority=priority,
            source_episodes=source_episodes or [],
            metadata=metadata or {},
        )

        with self._lock:
            self.rules[rule.rule_id] = rule
            self._rule_index[category].append(rule.rule_id)
            self._stats["total_rules_added"] += 1

        logger.debug(
            "LogicRuleLayer: added rule %s [%s] IF %s THEN %s (conf=%.3f)",
            rule.rule_id, category.value, condition[:80], action[:80], confidence,
        )
        return rule

    def get_rule(self, rule_id: str) -> Optional[LogicRule]:
        """按ID获取规则。"""
        with self._lock:
            return self.rules.get(rule_id)

    def list_rules(
        self, category: Optional[RuleCategory] = None,
        status: Optional[RuleStatus] = None,
        min_confidence: float = 0.0,
    ) -> List[LogicRule]:
        """列出规则，支持按类别/状态/置信度过滤。

        Args:
            category: 按类别过滤（None=全部）
            status: 按状态过滤（None=全部）
            min_confidence: 最小置信度阈值

        Returns:
            符合条件的规则列表
        """
        with self._lock:
            if category is not None:
                rule_ids = self._rule_index.get(category, [])
                rules = [self.rules[rid] for rid in rule_ids if rid in self.rules]
            else:
                rules = list(self.rules.values())

            if status is not None:
                rules = [r for r in rules if r.status == status]
            rules = [r for r in rules if r.confidence >= min_confidence]
            return sorted(rules, key=lambda r: (-r.priority, -r.confidence))

    def update_rule_status(self, rule_id: str, new_status: RuleStatus) -> bool:
        """更新规则状态。"""
        with self._lock:
            rule = self.rules.get(rule_id)
            if rule is None:
                return False
            old_status = rule.status
            rule.status = new_status
            rule.updated_at = time.time()
            if new_status == RuleStatus.SUPERSEDED:
                self._stats["total_rules_superseded"] += 1
            logger.debug(
                "LogicRuleLayer: rule %s status %s → %s", rule_id, old_status.value, new_status.value
            )
            return True

    def increase_confidence(self, rule_id: str, delta: float = 0.1) -> bool:
        """提升规则置信度。"""
        with self._lock:
            rule = self.rules.get(rule_id)
            if rule is None:
                return False
            rule.confidence = min(1.0, rule.confidence + delta)
            rule.updated_at = time.time()
            return True

    # ── 冲突检测 ─────────────────────────────────────────────────

    def detect_conflicts(self, new_rule: LogicRule) -> List[RuleConflict]:
        """检测新规则与已有规则的冲突。

        使用简化语义冲突检测：相同类别、条件中包含互斥关键词、或
        相同条件→不同结论视为冲突。

        Args:
            new_rule: 新添加的规则

        Returns:
            检测到的冲突列表
        """
        conflicts: List[RuleConflict] = []
        condition_lower = new_rule.condition.lower()
        action_lower = new_rule.action.lower()

        with self._lock:
            existing = [
                r for r in self.rules.values()
                if r.rule_id != new_rule.rule_id and r.category == new_rule.category
            ]
            for old_rule in existing:
                old_cond_lower = old_rule.condition.lower()
                old_action_lower = old_rule.action.lower()

                # 直接矛盾检测：条件相似但结论相反
                cond_overlap = self._jaccard_similarity_words(
                    condition_lower.split(), old_cond_lower.split()
                )
                if cond_overlap > 0.5 and old_action_lower != action_lower:
                    conflict = RuleConflict(
                        rule_a=new_rule.rule_id,
                        rule_b=old_rule.rule_id,
                        conflict_type=ConflictType.DIRECT_CONTRADICTION,
                        description=f"相似条件({cond_overlap:.2f})下结论不一致: "
                                    f"'{new_rule.action}' vs '{old_rule.action}'",
                    )
                    conflicts.append(conflict)

                # 作用域交叉检测
                if cond_overlap > 0.3 and old_action_lower == action_lower:
                    conflict = RuleConflict(
                        rule_a=new_rule.rule_id,
                        rule_b=old_rule.rule_id,
                        conflict_type=ConflictType.SCOPE_INTERSECTION,
                        description=f"条件重叠({cond_overlap:.2f})且结论相同，"
                                    f"可能存在冗余或优先级冲突",
                    )
                    conflicts.append(conflict)

            for c in conflicts:
                self.conflicts.append(c)
                self._stats["total_conflicts_detected"] += 1

        return conflicts

    def get_unresolved_conflicts(self) -> List[RuleConflict]:
        """获取未解决的冲突。"""
        with self._lock:
            return [c for c in self.conflicts if not c.resolved]

    def resolve_conflict(
        self, conflict_id: str, resolution: str,
        winner_rule_id: Optional[str] = None, loser_new_status: RuleStatus = RuleStatus.SUPERSEDED,
    ) -> bool:
        """解决冲突。

        Args:
            conflict_id: 冲突ID
            resolution: 解决方案描述
            winner_rule_id: 胜出规则ID（可选）
            loser_new_status: 失败规则的新状态
        """
        with self._lock:
            for conflict in self.conflicts:
                if conflict.conflict_id == conflict_id and not conflict.resolved:
                    conflict.resolved = True
                    conflict.resolution = resolution
                    if winner_rule_id:
                        loser_id = (
                            conflict.rule_b
                            if winner_rule_id == conflict.rule_a
                            else conflict.rule_a
                        )
                        self.update_rule_status(loser_id, loser_new_status)
                    return True
        return False

    # ── SK-Gen 自动规则构建 ──────────────────────────────────────

    def consolidate_episodes(
        self,
        episodes: List[Dict[str, Any]],
        strategy: ConsolidationStrategy = ConsolidationStrategy.PATTERN_EXTRACTION,
        min_confidence: float = 0.3,
        min_occurrence: int = 2,
    ) -> ConsolidationTrace:
        """SK-Gen 风格：从多模态经验中自动巩固结构化知识。

        从累积的情景中提取 IF-THEN 模式，构建逻辑规则。

        Args:
            episodes: 情景列表，每个 episode 含 'condition' 和 'outcome'
            strategy: 巩固策略
            min_confidence: 最小置信度阈值
            min_occurrence: 最小出现次数阈值

        Returns:
            巩固执行轨迹
        """
        trace = ConsolidationTrace(
            strategy=strategy,
            input_episode_count=len(episodes),
        )

        if not episodes:
            return trace

        # 统计 condition→outcome 的共现频率
        pattern_counts: Dict[Tuple[str, str], int] = defaultdict(int)
        for ep in episodes:
            cond = ep.get("condition", "").strip()
            outcome = ep.get("outcome", "").strip()
            if cond and outcome:
                pattern_counts[(cond, outcome)] += 1

        with self._lock:
            for (cond, outcome), count in pattern_counts.items():
                if count < min_occurrence:
                    continue
                # 基于频率计算置信度
                total_related = sum(
                    v for (c, o), v in pattern_counts.items() if c == cond
                )
                confidence = count / total_related if total_related > 0 else 0.5

                if confidence < min_confidence:
                    continue

                # 规则类别判定
                if "must" in cond.lower() or "cannot" in cond.lower():
                    category = RuleCategory.CONSTRAINT
                elif "prefer" in cond.lower() or "usually" in cond.lower():
                    category = RuleCategory.PREFERENCE
                elif "if" in cond.lower() or "when" in cond.lower():
                    category = RuleCategory.IF_THEN
                else:
                    category = RuleCategory.HEURISTIC

                rule = self.add_rule(
                    category=category,
                    condition=cond,
                    action=outcome,
                    confidence=min(1.0, confidence),
                    priority=max(0, count),
                    source_episodes=[ep.get("episode_id", "") for ep in episodes
                                     if ep.get("condition") == cond],
                    metadata={"occurrence_count": count, "strategy": strategy.value},
                )
                trace.extracted_rules.append(rule.rule_id)
                trace.confidence_scores[rule.rule_id] = confidence

        trace.timestamp = time.time()
        self.consolidation_history.append(trace)
        self._stats["total_consolidations"] += 1

        logger.info(
            "SK-Gen consolidation: %d episodes → %d rules (strategy=%s)",
            len(episodes), len(trace.extracted_rules), strategy.value,
        )
        return trace

    # ── 推理查询 ─────────────────────────────────────────────────

    def query_rules(
        self, context: QueryContext,
        categories: Optional[List[RuleCategory]] = None,
    ) -> List[LogicRule]:
        """基于上下文查询匹配的逻辑规则。

        扫描规则库，按条件与上下文实体的匹配度排序。

        Args:
            context: 查询上下文
            categories: 限制规则类别（None=全部）

        Returns:
            匹配的规则列表，按优先级+置信度排序
        """
        self._stats["total_queries"] += 1
        results: List[Tuple[LogicRule, float]] = []

        with self._lock:
            candidates = self.list_rules(status=RuleStatus.ACTIVE)
            if categories:
                candidates = [r for r in candidates if r.category in categories]

            for rule in candidates:
                score = self._compute_rule_match_score(rule, context)
                if score > 0:
                    results.append((rule, score))

        results.sort(key=lambda x: (x[1], x[0].priority, x[0].confidence), reverse=True)
        matched = [r for r, _ in results[:context.max_rules]]
        return matched

    def infer(
        self, context: QueryContext,
        categories: Optional[List[RuleCategory]] = None,
    ) -> Dict[str, Any]:
        """执行逻辑推理。

        基于匹配的规则进行推理，返回推理结论和依据。

        Args:
            context: 查询上下文
            categories: 限制规则类别

        Returns:
            {"conclusions": [...], "supporting_rules": [...], "confidence": float}
        """
        self._stats["total_inferences"] += 1
        rules = self.query_rules(context, categories=categories)
        conclusions: List[str] = []
        supporting_rule_ids: List[str] = []

        for rule in rules:
            conclusions.append(rule.action)
            supporting_rule_ids.append(rule.rule_id)

        avg_confidence = (
            np.mean([r.confidence for r in rules]) if rules else 0.0
        )

        return {
            "conclusions": conclusions,
            "supporting_rules": supporting_rule_ids,
            "confidence": float(avg_confidence),
            "rule_count": len(rules),
        }

    # ── 辅助方法 ─────────────────────────────────────────────────

    def _compute_rule_match_score(
        self, rule: LogicRule, context: QueryContext,
    ) -> float:
        """计算规则与上下文匹配分数（简化词重叠）。"""
        cond_tokens = set(rule.condition.lower().split())
        context_tokens = set()
        for entity in context.entities:
            context_tokens.update(entity.lower().split())
        for constraint in context.constraints:
            context_tokens.update(constraint.lower().split())
        for pref in context.preferences:
            context_tokens.update(pref.lower().split())

        if not cond_tokens or not context_tokens:
            return 0.0

        intersection = cond_tokens & context_tokens
        return len(intersection) / max(len(cond_tokens), 1)

    @staticmethod
    def _jaccard_similarity_words(
        words_a: List[str], words_b: List[str],
    ) -> float:
        """计算两组词的 Jaccard 相似度。"""
        set_a, set_b = set(words_a), set(words_b)
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / len(set_a | set_b)

    # ── 统计与诊断 ───────────────────────────────────────────────

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            active = sum(1 for r in self.rules.values() if r.status == RuleStatus.ACTIVE)
            conflicting = sum(1 for r in self.rules.values() if r.status == RuleStatus.CONFLICTING)
            unresolved = sum(1 for c in self.conflicts if not c.resolved)
            by_category = {
                cat.value: len(ids)
                for cat, ids in self._rule_index.items()
            }
            return {
                "total_rules": len(self.rules),
                "active_rules": active,
                "conflicting_rules": conflicting,
                "total_conflicts": len(self.conflicts),
                "unresolved_conflicts": unresolved,
                "total_consolidations": self._stats["total_consolidations"],
                "total_queries": self._stats["total_queries"],
                "total_inferences": self._stats["total_inferences"],
                "rules_by_category": by_category,
                "avg_rule_confidence": float(
                    np.mean([r.confidence for r in self.rules.values()])
                ) if self.rules else 0.0,
            }

    def reset(self) -> None:
        """重置所有规则和状态。"""
        with self._lock:
            self.rules.clear()
            self._rule_index.clear()
            self.conflicts.clear()
            self.consolidation_history.clear()
            for k in self._stats:
                self._stats[k] = 0
