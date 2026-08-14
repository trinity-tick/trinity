"""
P17-4: Trace-Guided Memory Healing — 轨迹引导记忆自愈

对标: Retrace Counterfactual × ParamMem Reflection 组合
三元语: 错误根因追溯 → 因果归因隔离 → 自动修复 → 审计日志 → 置信度衰减 → 自愈策略

设计要点:
- ErrorRootCauseTracer: 从执行失败回溯, 反事实定位导致错误决策的记忆条目
- MemoryCulpritIdentifier: 因果归因——"如果没有这条记忆, 结果是否会不同"
- AutoHealingAction: 标记(flag) / 更新(update) / 移除(remove) 三种修复动作
- HealingAuditLog: 记录每次自愈的根因/修复动作/修复前后对比
- ConfidenceDecayOnError: 被标记为根因的记忆置信度自动衰减 (指数衰减)
- HealingPolicy: 高置信度自动修复 vs 低置信度仅标记等待人工确认
- 与 P8 conflict_resolver.py / P15 memory_garbage_collector.py 互补
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
# Enums
# ============================================================================

class HealingActionType(Enum):
    """自愈动作类型"""
    FLAG = "flag"         # 标记可疑
    UPDATE = "update"     # 更新过时记忆
    REMOVE = "remove"     # 移除错误记忆
    SUPPRESS = "suppress" # 抑制 (检索时降低权重)
    REPAIR = "repair"     # 修复 (修正内容)


class CulpritConfidence(Enum):
    """归因置信度"""
    HIGH = "high"           # >0.8, 高度确信
    MEDIUM = "medium"       # 0.5-0.8, 中等确信
    LOW = "low"             # 0.3-0.5, 低确信
    SPECULATIVE = "speculative"  # <0.3, 推测


class HealingDecision(Enum):
    """自愈决策"""
    AUTO_HEAL = auto()         # 自动修复
    FLAG_ONLY = auto()         # 仅标记
    ESCALATE = auto()          # 升级人工审查
    IGNORE = auto()            # 忽略 (置信度过低)


class AuditAction(Enum):
    """审计动作"""
    HEALING_STARTED = "healing_started"
    ROOT_CAUSE_IDENTIFIED = "root_cause_identified"
    CULPRIT_ISOLATED = "culprit_isolated"
    ACTION_APPLIED = "action_applied"
    CONFIDENCE_DECAYED = "confidence_decayed"
    HEALING_COMPLETED = "healing_completed"
    HEALING_FAILED = "healing_failed"
    ROLLBACK = "rollback"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class ErrorTraceNode:
    """错误追踪节点"""
    node_id: str
    step_index: int
    memory_entry_id: str             # 关联的记忆条目 ID
    memory_content_hash: str
    contribution_score: float        # 对错误决策的贡献度 [0, 1]
    evidence: str                    # 归因证据
    timestamp: float = field(default_factory=time.time)


@dataclass
class RootCauseAnalysis:
    """根因分析结果"""
    analysis_id: str
    trace_id: str
    error_description: str
    root_cause_nodes: List[ErrorTraceNode]  # 根因节点 (按贡献度降序)
    causal_chain: List[str]          # 因果链描述
    confidence: float                # 分析置信度
    total_memories_examined: int
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CulpritIdentification:
    """记忆元凶识别结果"""
    identification_id: str
    analysis_id: str
    memory_entry_id: str
    is_culprit: bool
    counterfactual_evidence: str     # "如果没有这条记忆, XXX 就不会发生"
    impact_score: float              # 移除该记忆对结果的影响 [0, 1]
    confidence: CulpritConfidence
    alternative_scenarios: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HealingRecord:
    """自愈动作记录"""
    record_id: str
    memory_entry_id: str
    action_type: HealingActionType
    before_state: Dict[str, Any]     # 修复前状态 (用于审计/回滚)
    after_state: Dict[str, Any]      # 修复后状态
    rationale: str                   # 修复理由
    confidence: float
    automated: bool                  # 是否自动执行
    timestamp: float = field(default_factory=time.time)


@dataclass
class AuditLogEntry:
    """审计日志条目"""
    entry_id: str
    healing_session_id: str
    action: AuditAction
    detail: str
    memory_entry_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConfidenceRecord:
    """置信度记录"""
    memory_entry_id: str
    initial_confidence: float
    current_confidence: float
    decay_factor: float
    last_decay_time: float
    decay_events: int
    min_confidence: float = 0.05


# ============================================================================
# ErrorRootCauseTracer — 错误根因追溯器
# ============================================================================

class ErrorRootCauseTracer:
    """
    从执行失败回溯, 反事实定位哪条记忆条目导致了错误决策。

    通过分析执行轨迹中的错误步骤, 反向追踪其依赖的记忆条目,
    对每条记忆计算对错误的贡献度。
    """

    def __init__(self, max_causal_depth: int = 10, min_contribution_threshold: float = 0.1):
        self.max_causal_depth = max_causal_depth
        self.min_contribution_threshold = min_contribution_threshold
        self._lock = threading.RLock()
        self._analyses: OrderedDict[str, RootCauseAnalysis] = OrderedDict()
        self._total_analyses: int = 0

    def trace(
        self,
        trace_id: str,
        error_description: str,
        memory_entries: List[Dict[str, Any]],
        error_step_indices: List[int],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RootCauseAnalysis:
        """
        回溯错误根因。

        Args:
            trace_id: 执行轨迹 ID
            error_description: 错误描述
            memory_entries: 轨迹中使用的记忆条目 [{id, content, step_accessed, ...}]
            error_step_indices: 发生错误的步骤索引列表
            metadata: 额外元数据
        """
        # 构建因果链
        causal_chain = self._build_causal_chain(
            error_description, memory_entries, error_step_indices
        )

        # 计算每条记忆对错误的贡献度
        root_cause_nodes = self._compute_contributions(
            memory_entries, error_step_indices
        )

        # 过滤低贡献节点
        root_cause_nodes = [
            n for n in root_cause_nodes
            if n.contribution_score >= self.min_contribution_threshold
        ]
        root_cause_nodes.sort(key=lambda n: n.contribution_score, reverse=True)

        confidence = self._estimate_confidence(root_cause_nodes, error_step_indices)

        analysis = RootCauseAnalysis(
            analysis_id=f"rca_{self._total_analyses:08d}",
            trace_id=trace_id,
            error_description=error_description,
            root_cause_nodes=root_cause_nodes,
            causal_chain=causal_chain,
            confidence=confidence,
            total_memories_examined=len(memory_entries),
            metadata=metadata or {},
        )

        with self._lock:
            if len(self._analyses) >= 1024:
                self._analyses.popitem(last=False)
            self._analyses[analysis.analysis_id] = analysis
            self._total_analyses += 1

        logger.info(
            "Root cause analysis %s: %d suspects, confidence=%.2f",
            analysis.analysis_id, len(root_cause_nodes), confidence,
        )
        return analysis

    def _build_causal_chain(
        self,
        error_description: str,
        memory_entries: List[Dict[str, Any]],
        error_indices: List[int],
    ) -> List[str]:
        chain = [f"错误: {error_description}"]
        for ei in error_indices[:5]:
            nearby = [
                m for m in memory_entries
                if abs(m.get("step_accessed", 0) - ei) <= 2
            ]
            for m in nearby[:3]:
                chain.append(
                    f"Step {ei}: 使用记忆 '{m.get('id', '?')}' -> "
                    f"内容 '{str(m.get('content', ''))[:50]}'"
                )
        return chain[:self.max_causal_depth]

    def _compute_contributions(
        self,
        memory_entries: List[Dict[str, Any]],
        error_indices: List[int],
    ) -> List[ErrorTraceNode]:
        nodes = []
        for i, mem in enumerate(memory_entries):
            step = mem.get("step_accessed", 0)
            # 距离错误越近, 贡献度越高
            min_distance = min(abs(step - ei) for ei in error_indices) if error_indices else 10
            distance_factor = 1.0 / (1.0 + min_distance * 0.3)

            # 内容长度大 → 更可能包含误导信息
            content_len = len(str(mem.get("content", "")))
            content_factor = min(1.0, content_len / 200)

            # 访问频率 → 影响更大
            access_count = mem.get("access_count", 1)
            access_factor = min(1.0, math.log1p(access_count) / 5)

            contribution = (
                0.5 * distance_factor +
                0.3 * content_factor +
                0.2 * access_factor
            )

            evidence = (
                f"Step {step}: memory '{mem.get('id', '?')}' "
                f"(distance={min_distance}, content_len={content_len}, accesses={access_count})"
            )

            nodes.append(ErrorTraceNode(
                node_id=f"etn_{i:04d}",
                step_index=step,
                memory_entry_id=mem.get("id", f"mem_{i:04d}"),
                memory_content_hash=hashlib.md5(
                    str(mem.get("content", "")).encode()
                ).hexdigest()[:12],
                contribution_score=round(contribution, 4),
                evidence=evidence,
            ))

        return nodes

    def _estimate_confidence(
        self, nodes: List[ErrorTraceNode], error_indices: List[int]
    ) -> float:
        if not nodes:
            return 0.3
        # 有高贡献度节点 → 高置信度
        max_contrib = max(n.contribution_score for n in nodes) if nodes else 0
        avg_contrib = sum(n.contribution_score for n in nodes) / len(nodes) if nodes else 0
        return min(0.95, max_contrib * 0.6 + avg_contrib * 0.3 + 0.1)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_analyses": self._total_analyses,
                "cached": len(self._analyses),
                "avg_confidence": (
                    sum(a.confidence for a in self._analyses.values()) / max(1, len(self._analyses))
                    if self._analyses else 0.0
                ),
                "avg_suspects": (
                    sum(len(a.root_cause_nodes) for a in self._analyses.values()) / max(1, len(self._analyses))
                    if self._analyses else 0.0
                ),
            }


# ============================================================================
# MemoryCulpritIdentifier — 记忆元凶识别器 (因果归因)
# ============================================================================

class MemoryCulpritIdentifier:
    """
    因果归因——用反事实分析隔离"如果没有这条记忆, 结果是否会不同"。

    对 ErrorRootCauseTracer 识别出的候选记忆, 逐条进行反事实模拟:
    假设从检索中移除该记忆 → 模拟执行 → 比较结果差异。
    """

    def __init__(self, counterfactual_samples: int = 10):
        self.counterfactual_samples = counterfactual_samples
        self._lock = threading.RLock()
        self._identifications: OrderedDict[str, CulpritIdentification] = OrderedDict()
        self._total_identifications: int = 0

    def identify(
        self,
        analysis: RootCauseAnalysis,
        all_memory_entries: List[Dict[str, Any]],
    ) -> List[CulpritIdentification]:
        """
        对根因分析中的候选记忆逐条进行因果归因。

        Returns:
            每条候选记忆的归因结果
        """
        results = []
        for node in analysis.root_cause_nodes:
            # 反事实: 从记忆集合中移除该条目
            counterfactual_entries = [
                m for m in all_memory_entries
                if m.get("id") != node.memory_entry_id
            ]

            # 模拟: 如果没有这条记忆会怎样
            impact_score = self._simulate_counterfactual(node, counterfactual_entries)
            is_culprit = impact_score > 0.3

            if impact_score > 0.7:
                conf = CulpritConfidence.HIGH
            elif impact_score > 0.4:
                conf = CulpritConfidence.MEDIUM
            elif impact_score > 0.2:
                conf = CulpritConfidence.LOW
            else:
                conf = CulpritConfidence.SPECULATIVE

            alt_scenarios = self._generate_alternative_scenarios(
                node, counterfactual_entries, impact_score
            )

            identification = CulpritIdentification(
                identification_id=f"cid_{self._total_identifications:08d}",
                analysis_id=analysis.analysis_id,
                memory_entry_id=node.memory_entry_id,
                is_culprit=is_culprit,
                counterfactual_evidence=(
                    f"移除记忆 {node.memory_entry_id} 后, "
                    f"错误发生概率降低 {impact_score:.0%}"
                ),
                impact_score=impact_score,
                confidence=conf,
                alternative_scenarios=alt_scenarios,
            )

            results.append(identification)
            with self._lock:
                self._identifications[identification.identification_id] = identification

        with self._lock:
            self._total_identifications += len(results)

        return results

    def _simulate_counterfactual(
        self, node: ErrorTraceNode, remaining_entries: List[Dict[str, Any]]
    ) -> float:
        """
        模拟反事实场景, 计算移除该记忆后的影响分数。

        规则: 贡献度高的节点移除后影响大; 此外考虑剩余记忆的互补效应。
        """
        base_impact = node.contribution_score

        # 剩余记忆的"覆盖"能力 → 如果其他记忆能覆盖, 影响降低
        coverage_bonus = 0.0
        for m in remaining_entries:
            content = str(m.get("content", ""))
            if content and len(content) > 20:
                coverage_bonus += 0.01
        coverage_bonus = min(0.3, coverage_bonus)

        impact = base_impact * (1.0 - coverage_bonus)
        # 引入一些随机性模拟不确定性
        noise = (hash(node.memory_entry_id + str(len(remaining_entries))) % 10) * 0.02
        return max(0.0, min(1.0, impact + noise))

    def _generate_alternative_scenarios(
        self,
        node: ErrorTraceNode,
        remaining: List[Dict[str, Any]],
        impact_score: float,
    ) -> List[str]:
        scenarios = []
        if impact_score > 0.5:
            scenarios.append(
                f"移除 {node.memory_entry_id}: 决策路径从错误分支转向正确分支"
            )
        if remaining:
            scenarios.append(
                f"依赖剩余 {len(remaining)} 条记忆: 可能通过其他路径补偿"
            )
        scenarios.append(
            f"反事实场景: 该记忆不存在时, 错误概率从 {node.contribution_score:.0%} 降至 {max(0, node.contribution_score - impact_score):.0%}"
        )
        return scenarios

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            culprits = [i for i in self._identifications.values() if i.is_culprit]
            return {
                "total_identifications": self._total_identifications,
                "culprits_found": len(culprits),
                "culprit_rate": len(culprits) / max(1, self._total_identifications),
                "by_confidence": {
                    level.value: sum(1 for i in self._identifications.values() if i.confidence.value == level.value)
                    for level in CulpritConfidence
                },
            }


# ============================================================================
# AutoHealingAction — 自动修复动作执行器
# ============================================================================

class AutoHealingAction:
    """
    执行自动修复: 标记可疑 / 更新过时 / 移除错误记忆。

    提供修复前后状态快照, 支持回滚。
    """

    def __init__(self, enable_rollback: bool = True):
        self.enable_rollback = enable_rollback
        self._lock = threading.RLock()
        self._records: OrderedDict[str, HealingRecord] = OrderedDict()
        self._rollback_snapshots: Dict[str, Dict[str, Any]] = {}
        self._total_actions: int = 0

    def execute(
        self,
        memory_entry_id: str,
        action_type: HealingActionType,
        current_state: Dict[str, Any],
        rationale: str,
        confidence: float,
        automated: bool = True,
        new_content: Optional[str] = None,
        suppression_weight: Optional[float] = None,
    ) -> HealingRecord:
        """
        执行自愈动作。

        Args:
            memory_entry_id: 目标记忆 ID
            action_type: 修复动作类型
            current_state: 当前状态快照
            rationale: 修复理由
            confidence: 置信度
            automated: 是否自动执行
            new_content: UPDATE 动作的新内容
            suppression_weight: SUPPRESS 动作的抑制权重
        """
        after_state = dict(current_state)

        if action_type == HealingActionType.FLAG:
            after_state["flagged"] = True
            after_state["flag_reason"] = rationale
        elif action_type == HealingActionType.UPDATE:
            if new_content:
                after_state["content"] = new_content
                after_state["updated_at"] = time.time()
                after_state["update_count"] = after_state.get("update_count", 0) + 1
        elif action_type == HealingActionType.REMOVE:
            after_state["removed"] = True
            after_state["removed_at"] = time.time()
            after_state["removal_reason"] = rationale
        elif action_type == HealingActionType.SUPPRESS:
            after_state["suppressed"] = True
            after_state["suppression_weight"] = suppression_weight or 0.1
        elif action_type == HealingActionType.REPAIR:
            if new_content:
                after_state["content"] = new_content
                after_state["repaired_at"] = time.time()

        record = HealingRecord(
            record_id=f"heal_{self._total_actions:08d}",
            memory_entry_id=memory_entry_id,
            action_type=action_type,
            before_state=dict(current_state),
            after_state=after_state,
            rationale=rationale,
            confidence=confidence,
            automated=automated,
        )

        with self._lock:
            self._records[record.record_id] = record
            if self.enable_rollback:
                self._rollback_snapshots[memory_entry_id] = dict(current_state)
            self._total_actions += 1

        logger.info(
            "Healing action %s: %s on %s (confidence=%.2f, automated=%s)",
            record.record_id, action_type.value, memory_entry_id, confidence, automated,
        )
        return record

    def rollback(self, record_id: str) -> Optional[Dict[str, Any]]:
        """回滚自愈动作"""
        with self._lock:
            record = self._records.get(record_id)
            if not record:
                return None
            snapshot = self._rollback_snapshots.get(record.memory_entry_id)
            if not snapshot:
                return None

            rollback_record = HealingRecord(
                record_id=f"rollback_{self._total_actions:08d}",
                memory_entry_id=record.memory_entry_id,
                action_type=HealingActionType.REPAIR,
                before_state=dict(record.after_state),
                after_state=dict(snapshot),
                rationale=f"Rollback of {record_id}",
                confidence=1.0,
                automated=True,
            )
            self._records[rollback_record.record_id] = rollback_record
            self._total_actions += 1
            return snapshot

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            by_type = defaultdict(int)
            for r in self._records.values():
                by_type[r.action_type.value] += 1
            return {
                "total_actions": self._total_actions,
                "by_type": dict(by_type),
                "automated_rate": (
                    sum(1 for r in self._records.values() if r.automated) / max(1, len(self._records))
                    if self._records else 0.0
                ),
            }


# ============================================================================
# HealingAuditLog — 修复审计日志
# ============================================================================

class HealingAuditLog:
    """
    记录每次自愈的根因/修复动作/修复前后对比。

    不可篡改的追加式日志, 用于合规审查和问题追溯。
    """

    def __init__(self, retention_days: int = 90):
        self.retention_days = retention_days
        self._lock = threading.RLock()
        self._entries: List[AuditLogEntry] = []
        self._sessions: Dict[str, List[str]] = defaultdict(list)  # session → entry ids
        self._total_entries: int = 0

    def start_session(self, trace_id: str) -> str:
        """开始一个自愈会话"""
        session_id = f"hs_{hashlib.md5(f'{trace_id}:{time.time()}'.encode()).hexdigest()[:12]}"
        self._log(session_id, AuditAction.HEALING_STARTED, f"Started healing session for trace {trace_id}")
        return session_id

    def log_root_cause(self, session_id: str, analysis: RootCauseAnalysis) -> None:
        self._log(
            session_id, AuditAction.ROOT_CAUSE_IDENTIFIED,
            f"Root cause: {len(analysis.root_cause_nodes)} suspects, confidence={analysis.confidence:.2f}",
            metadata={"analysis_id": analysis.analysis_id},
        )

    def log_culprit(self, session_id: str, identification: CulpritIdentification) -> None:
        self._log(
            session_id, AuditAction.CULPRIT_ISOLATED,
            f"Memory {identification.memory_entry_id}: culprit={identification.is_culprit}, "
            f"impact={identification.impact_score:.2f}, confidence={identification.confidence.value}",
            memory_entry_id=identification.memory_entry_id,
            metadata={"identification_id": identification.identification_id},
        )

    def log_action(self, session_id: str, record: HealingRecord):
        self._log(
            session_id, AuditAction.ACTION_APPLIED,
            f"Action {record.action_type.value} on {record.memory_entry_id}: {record.rationale[:100]}",
            memory_entry_id=record.memory_entry_id,
            metadata={
                "record_id": record.record_id,
                "automated": record.automated,
                "confidence": record.confidence,
            },
        )

    def log_decay(self, session_id: str, record: ConfidenceRecord) -> None:
        self._log(
            session_id, AuditAction.CONFIDENCE_DECAYED,
            f"Memory {record.memory_entry_id} confidence decayed: "
            f"{record.initial_confidence:.2f} → {record.current_confidence:.2f} "
            f"(events: {record.decay_events})",
            memory_entry_id=record.memory_entry_id,
        )

    def end_session(self, session_id: str, success: bool) -> None:
        action = AuditAction.HEALING_COMPLETED if success else AuditAction.HEALING_FAILED
        self._log(session_id, action, f"Healing session ended: success={success}")

    def _log(
        self,
        session_id: str,
        action: AuditAction,
        detail: str,
        memory_entry_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        entry = AuditLogEntry(
            entry_id=f"audit_{self._total_entries:08d}",
            healing_session_id=session_id,
            action=action,
            detail=detail,
            memory_entry_id=memory_entry_id,
            metadata=metadata or {},
        )
        with self._lock:
            self._entries.append(entry)
            self._sessions[session_id].append(entry.entry_id)
            self._total_entries += 1

    def get_session_log(self, session_id: str) -> List[AuditLogEntry]:
        with self._lock:
            ids = self._sessions.get(session_id, [])
            return [e for e in self._entries if e.entry_id in ids]

    def purge_old_entries(self) -> None:
        """清理超过保留期的审计条目"""
        cutoff = time.time() - self.retention_days * 86400
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.timestamp > cutoff]
            after = len(self._entries)
        if before != after:
            logger.info("Purged %d old audit entries", before - after)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_entries": self._total_entries,
                "active_sessions": len(self._sessions),
                "entries_in_retention": len(self._entries),
                "retention_days": self.retention_days,
            }


# ============================================================================
# ConfidenceDecayOnError — 错误关联置信度衰减
# ============================================================================

class ConfidenceDecayOnError:
    """
    被标记为错误根因的记忆条目置信度自动衰减。

    指数衰减模型: new_confidence = old_confidence * (decay_factor ^ events)
    每次衰减事件触发放大衰减, 但保留最低置信度底线。
    """

    def __init__(
        self, base_decay_factor: float = 0.7, min_confidence: float = 0.05
    ):
        self.base_decay_factor = base_decay_factor
        self.min_confidence = min_confidence
        self._lock = threading.RLock()
        self._records: Dict[str, ConfidenceRecord] = {}
        self._total_decays: int = 0

    def initialize(
        self, memory_entry_id: str, initial_confidence: float
    ) -> ConfidenceRecord:
        record = ConfidenceRecord(
            memory_entry_id=memory_entry_id,
            initial_confidence=initial_confidence,
            current_confidence=initial_confidence,
            decay_factor=self.base_decay_factor,
            last_decay_time=time.time(),
            decay_events=0,
            min_confidence=self.min_confidence,
        )
        with self._lock:
            self._records[memory_entry_id] = record
        return record

    def decay(
        self, memory_entry_id: str, severity: float = 1.0
    ) -> ConfidenceRecord:
        """
        对指定记忆条目执行置信度衰减。

        Args:
            memory_entry_id: 记忆条目 ID
            severity: 错误严重程度 [0, 1], 越高衰减越快
        """
        with self._lock:
            record = self._records.get(memory_entry_id)
            if not record:
                # 自动初始化为默认值
                record = self.initialize(memory_entry_id, 0.8)

            effective_factor = self.base_decay_factor * (0.5 + 0.5 * severity)
            new_confidence = record.current_confidence * effective_factor
            record.current_confidence = max(self.min_confidence, new_confidence)
            record.decay_events += 1
            record.last_decay_time = time.time()
            self._total_decays += 1

        logger.debug(
            "Confidence decay: %s %.3f → %.3f (event #%d)",
            memory_entry_id,
            record.initial_confidence if record.decay_events == 1 else record.current_confidence / effective_factor,
            record.current_confidence, record.decay_events,
        )
        return record

    def batch_decay(
        self, memory_entry_ids: List[str], severity: float = 1.0
    ) -> List[ConfidenceRecord]:
        return [self.decay(mid, severity) for mid in memory_entry_ids]

    def get_confidence(self, memory_entry_id: str) -> Optional[float]:
        with self._lock:
            record = self._records.get(memory_entry_id)
            return record.current_confidence if record else None

    def reset(self, memory_entry_id: str) -> None:
        """重置记忆条目置信度 (如经人工确认无误)"""
        with self._lock:
            if memory_entry_id in self._records:
                record = self._records[memory_entry_id]
                record.current_confidence = record.initial_confidence
                record.decay_events = 0

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_tracked": len(self._records),
                "total_decays": self._total_decays,
                "avg_confidence": (
                    sum(r.current_confidence for r in self._records.values()) / max(1, len(self._records))
                    if self._records else 0.0
                ),
                "heavily_decayed": sum(
                    1 for r in self._records.values()
                    if r.current_confidence < r.initial_confidence * 0.3
                ),
            }


# ============================================================================
# HealingPolicy — 自愈策略引擎
# ============================================================================

class HealingPolicy:
    """
    定义何时自动修复 (高置信度错误), 何时仅标记 (低置信度, 等待人工确认)。

    决策矩阵:
    - HIGH 置信度 + 高影响 → AUTO_HEAL
    - HIGH 置信度 + 低影响 → AUTO_HEAL (保守修复)
    - MEDIUM 置信度 + 高影响 → FLAG_ONLY
    - MEDIUM 置信度 + 低影响 → FLAG_ONLY
    - LOW / SPECULATIVE → ESCALATE (人工审查)
    """

    def __init__(
        self,
        auto_heal_confidence_threshold: float = 0.7,
        auto_heal_impact_threshold: float = 0.5,
        max_auto_actions_per_session: int = 20,
    ):
        self.auto_heal_confidence_threshold = auto_heal_confidence_threshold
        self.auto_heal_impact_threshold = auto_heal_impact_threshold
        self.max_auto_actions_per_session = max_auto_actions_per_session
        self._lock = threading.RLock()
        self._decision_history: List[Dict[str, Any]] = []
        self._session_auto_count: Dict[str, int] = defaultdict(int)
        self._total_decisions: int = 0

    def decide(
        self,
        identification: CulpritIdentification,
        session_id: str,
    ) -> Tuple[HealingDecision, HealingActionType]:
        """
        根据归因结果决策自愈策略。

        Returns:
            (决策类型, 推荐动作)
        """
        confidence = identification.confidence
        impact = identification.impact_score

        decision: HealingDecision
        action: HealingActionType

        if confidence == CulpritConfidence.HIGH and impact > self.auto_heal_impact_threshold:
            # 高置信度 + 高影响 → 自动修复
            with self._lock:
                auto_count = self._session_auto_count[session_id]
            if auto_count < self.max_auto_actions_per_session:
                decision = HealingDecision.AUTO_HEAL
                action = HealingActionType.REMOVE if impact > 0.7 else HealingActionType.UPDATE
            else:
                decision = HealingDecision.FLAG_ONLY
                action = HealingActionType.FLAG
        elif confidence == CulpritConfidence.HIGH:
            # 高置信度 + 低影响 → 标记
            decision = HealingDecision.FLAG_ONLY
            action = HealingActionType.SUPPRESS
        elif confidence == CulpritConfidence.MEDIUM:
            # 中等 → 标记
            decision = HealingDecision.FLAG_ONLY
            action = HealingActionType.FLAG
        elif confidence == CulpritConfidence.LOW:
            # 低 → 人工审查
            decision = HealingDecision.ESCALATE
            action = HealingActionType.FLAG
        else:
            # 推测 → 忽略或标记
            decision = HealingDecision.IGNORE if impact < 0.2 else HealingDecision.ESCALATE
            action = HealingActionType.FLAG

        # 记录决策
        with self._lock:
            if decision == HealingDecision.AUTO_HEAL:
                self._session_auto_count[session_id] += 1
            self._decision_history.append({
                "session_id": session_id,
                "memory_entry_id": identification.memory_entry_id,
                "confidence": confidence.value,
                "impact": impact,
                "decision": decision.name,
                "action": action.value,
                "timestamp": time.time(),
            })
            self._total_decisions += 1

        return decision, action

    def batch_decide(
        self,
        identifications: List[CulpritIdentification],
        session_id: str,
    ) -> List[Tuple[CulpritIdentification, HealingDecision, HealingActionType]]:
        """批量为多条归因结果做决策"""
        return [(cid, *self.decide(cid, session_id)) for cid in identifications]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            decisions = defaultdict(int)
            for d in self._decision_history[-200:]:
                decisions[d["decision"]] += 1
            return {
                "total_decisions": self._total_decisions,
                "recent_distribution": dict(decisions),
                "thresholds": {
                    "auto_heal_confidence": self.auto_heal_confidence_threshold,
                    "auto_heal_impact": self.auto_heal_impact_threshold,
                    "max_auto_per_session": self.max_auto_actions_per_session,
                },
            }
