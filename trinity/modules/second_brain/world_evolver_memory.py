"""
WorldEvolverMemory — WorldEvolver Action-Transformation World Simulator
=======================================================================
arXiv 2606.30639 · P41-1

实现 WorldEvolver 世界演化记忆: episodic_world_memory 基于检索的动作转换模拟
预测下一步状态, semantic_rule_memory 从预测-观察不匹配中提取持久启发式规则,
selective_foresight 过滤低置信度预测, test_time_revision 部署时记忆修正不修改下游参数。

设计要点:
  - EpisodicWorldMemory: 检索历史交互, 模拟动作→下一状态
  - SemanticRuleMemory: 预测误差→启发式规则
  - SelectiveForesight: 置信度阈值过滤
  - TestTimeRevision: 在线修正, 不改变下游Agent权重
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PredictionConfidence(Enum):
    """预测置信度等级。"""
    HIGH = auto()
    MEDIUM = auto()
    LOW = auto()
    REJECT = auto()


class RuleScope(Enum):
    """规则作用域。"""
    GLOBAL = auto()
    CONTEXT_BOUND = auto()
    AGENT_SPECIFIC = auto()


class RevisionStatus(Enum):
    """修正状态。"""
    PENDING = auto()
    APPLIED = auto()
    REJECTED = auto()
    SUPERSEDED = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class WE_WorldState:
    """WorldEvolver 世界状态 (重命名: WorldState→WE_WorldState 避免冲突)。"""
    state_id: str
    features: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[np.ndarray] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class EpisodicRecord:
    """单条 episodic 交互记录: (state, action) → next_state。"""
    record_id: str
    state: WE_WorldState
    action: str
    next_state: WE_WorldState
    reward: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class SemanticRule:
    """从预测-观察不匹配中提取的启发式规则。"""
    rule_id: str
    condition: str          # 触发条件 (自然语言或表达式)
    expected_outcome: str   # 预期结果
    actual_outcome: str     # 观察到的实际结果
    confidence: float = 0.0
    scope: RuleScope = RuleScope.CONTEXT_BOUND
    activation_count: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class ForesightPrediction:
    """前瞻预测——一次状态预测结果。"""
    prediction_id: str
    from_state: WE_WorldState
    action: str
    predicted_state: WE_WorldState
    confidence: PredictionConfidence = PredictionConfidence.MEDIUM
    confidence_score: float = 0.5
    timestamp: float = field(default_factory=time.time)


@dataclass
class RevisionRecord:
    """部署时修正记录。"""
    revision_id: str
    original_prediction: str
    revision: str
    reason: str
    status: RevisionStatus = RevisionStatus.PENDING
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# EpisodicWorldMemory
# ---------------------------------------------------------------------------

class EpisodicWorldMemory:
    """基于检索的动作转换模拟——从真实交互中预测下一步状态。

    Parameters
    ----------
    capacity : int
        最大 episodic 记录数。
    similarity_threshold : float
        检索相似度阈值。
    """

    def __init__(self, capacity: int = 500, similarity_threshold: float = 0.6) -> None:
        self.capacity = capacity
        self.similarity_threshold = similarity_threshold
        self._records: deque = deque(maxlen=capacity)
        self._lock = threading.RLock()
        self._record_count: int = 0

    def add_episode(
        self,
        state: WE_WorldState,
        action: str,
        next_state: WE_WorldState,
        reward: float = 0.0,
    ) -> EpisodicRecord:
        """记录一条 episodic 交互。"""
        with self._lock:
            self._record_count += 1
            record = EpisodicRecord(
                record_id=f"ep_{self._record_count}_{int(time.time()*1e6)}",
                state=state,
                action=action,
                next_state=next_state,
                reward=reward,
            )
            self._records.append(record)
            return record

    def predict_next_state(self, state: WE_WorldState, action: str) -> Tuple[Optional[WE_WorldState], float]:
        """基于检索的动作转换模拟——预测下一步状态。

        Returns
        -------
        Tuple[Optional[WE_WorldState], float]
            (预测的下一状态, 置信度)。
        """
        with self._lock:
            if not self._records:
                return None, 0.0

            matches = []
            for rec in self._records:
                if rec.action != action:
                    continue
                sim = _compute_state_similarity(state, rec.state)
                if sim >= self.similarity_threshold:
                    matches.append((rec, sim))

            if not matches:
                return None, 0.0

            # 加权平均: 相似度越高的记录权重越大
            total_w = sum(s for _, s in matches)
            if total_w == 0:
                return None, 0.0

            # 选择最佳匹配的 next_state
            matches.sort(key=lambda x: x[1], reverse=True)
            best_rec, best_sim = matches[0]

            predicted = WE_WorldState(
                state_id=f"pred_{int(time.time()*1e6)}",
                features=dict(best_rec.next_state.features),
                embedding=best_rec.next_state.embedding.copy() if best_rec.next_state.embedding is not None else None,
            )
            return predicted, float(best_sim)

    def retrieve_similar(self, state: WE_WorldState, k: int = 5) -> List[EpisodicRecord]:
        """检索相似状态记录。"""
        with self._lock:
            scored = [(rec, _compute_state_similarity(state, rec.state)) for rec in self._records]
            scored.sort(key=lambda x: x[1], reverse=True)
            return [rec for rec, _ in scored[:k]]

    def statistics(self) -> Dict[str, Any]:
        return {"total_records": len(self._records), "capacity": self.capacity}


# ---------------------------------------------------------------------------
# SemanticRuleMemory
# ---------------------------------------------------------------------------

class SemanticRuleMemory:
    """从预测-观察不匹配中提取持久启发式规则。

    Parameters
    ----------
    capacity : int
        最大规则数。
    extraction_threshold : float
        触发提取的不匹配幅度阈值。
    """

    def __init__(self, capacity: int = 200, extraction_threshold: float = 0.3) -> None:
        self.capacity = capacity
        self.extraction_threshold = extraction_threshold
        self._rules: Dict[str, SemanticRule] = {}
        self._lock = threading.RLock()
        self._rule_count: int = 0

    def extract_rule(
        self,
        condition: str,
        expected_outcome: str,
        actual_outcome: str,
        confidence: float = 0.0,
        scope: RuleScope = RuleScope.CONTEXT_BOUND,
    ) -> Optional[SemanticRule]:
        """从不匹配中提取规则。

        Parameters
        ----------
        condition : str
            触发条件。
        expected_outcome : str
            预期结果。
        actual_outcome : str
            观察到的实际结果。
        confidence : float
            规则置信度 (基于不匹配幅度计算)。

        Returns
        -------
        Optional[SemanticRule]
            提取的规则; 置信度不足时返回 None。
        """
        with self._lock:
            if confidence < self.extraction_threshold:
                return None

            if len(self._rules) >= self.capacity:
                # 淘汰最少激活的规则
                oldest = min(self._rules.items(), key=lambda x: x[1].activation_count)
                del self._rules[oldest[0]]

            self._rule_count += 1
            rule = SemanticRule(
                rule_id=f"rule_{self._rule_count}_{int(time.time()*1e6)}",
                condition=condition,
                expected_outcome=expected_outcome,
                actual_outcome=actual_outcome,
                confidence=confidence,
                scope=scope,
                activation_count=1,
            )
            self._rules[rule.rule_id] = rule
            logger.info("Rule extracted: %s [conf=%.3f]", rule.rule_id, confidence)
            return rule

    def match_rules(self, condition: str) -> List[SemanticRule]:
        """匹配当前条件适用的规则。"""
        kw = condition.lower()
        matched = [r for r in self._rules.values() if kw in r.condition.lower() or r.condition.lower() in kw]
        for r in matched:
            r.activation_count += 1
        return sorted(matched, key=lambda r: r.confidence, reverse=True)

    def statistics(self) -> Dict[str, Any]:
        return {"total_rules": len(self._rules), "capacity": self.capacity}


# ---------------------------------------------------------------------------
# SelectiveForesight
# ---------------------------------------------------------------------------

class SelectiveForesight:
    """过滤低置信度预测, 仅将可信预测注入推理上下文。

    Parameters
    ----------
    confidence_threshold : float
        预测低于此置信度则拒绝注入。
    """

    def __init__(self, confidence_threshold: float = 0.7) -> None:
        self.confidence_threshold = confidence_threshold

    def filter(
        self, prediction: ForesightPrediction
    ) -> Tuple[bool, PredictionConfidence]:
        """过滤预测——返回 (是否通过, 置信度等级)。"""
        if prediction.confidence_score >= self.confidence_threshold + 0.15:
            return True, PredictionConfidence.HIGH
        elif prediction.confidence_score >= self.confidence_threshold:
            return True, PredictionConfidence.MEDIUM
        elif prediction.confidence_score >= self.confidence_threshold - 0.15:
            return False, PredictionConfidence.LOW
        return False, PredictionConfidence.REJECT


# ---------------------------------------------------------------------------
# WorldEvolverMemory
# ---------------------------------------------------------------------------

class WorldEvolverMemory:
    """WorldEvolver 世界演化记忆系统。

    Parameters
    ----------
    episodic_capacity : int
        EpisodicWorldMemory 容量。
    rule_capacity : int
        SemanticRuleMemory 容量。
    foresight_threshold : float
        SelectiveForesight 置信度阈值。
    """

    def __init__(
        self,
        episodic_capacity: int = 500,
        rule_capacity: int = 200,
        foresight_threshold: float = 0.7,
    ) -> None:
        self.episodic_world_memory = EpisodicWorldMemory(capacity=episodic_capacity)
        self.semantic_rule_memory = SemanticRuleMemory(capacity=rule_capacity)
        self._foresight = SelectiveForesight(confidence_threshold=foresight_threshold)
        self._revisions: List[RevisionRecord] = []
        self._lock = threading.RLock()
        self._prediction_count: int = 0

        logger.info(
            "WorldEvolverMemory initialized [ep_cap=%d rule_cap=%d foresight=%.2f]",
            episodic_capacity, rule_capacity, foresight_threshold,
        )

    # ------------------------------------------------------------------
    # Episodic World Memory
    # ------------------------------------------------------------------

    def add_interaction(
        self,
        state_features: Dict[str, Any],
        action: str,
        next_state_features: Dict[str, Any],
        reward: float = 0.0,
    ) -> EpisodicRecord:
        """记录一次真实交互到 episodic memory。"""
        state = WE_WorldState(
            state_id=f"s_{int(time.time()*1e6)}",
            features=state_features,
        )
        next_state = WE_WorldState(
            state_id=f"ns_{int(time.time()*1e6)}",
            features=next_state_features,
        )
        return self.episodic_world_memory.add_episode(state, action, next_state, reward)

    # ------------------------------------------------------------------
    # Selective Foresight
    # ------------------------------------------------------------------

    def selective_foresight(
        self, state_features: Dict[str, Any], action: str
    ) -> Optional[ForesightPrediction]:
        """选择性前瞻——预测下一步并过滤。

        Returns
        -------
        Optional[ForesightPrediction]
            若置信度足够则返回预测; 否则 None。
        """
        state = WE_WorldState(features=state_features, state_id=f"q_{int(time.time()*1e6)}")
        predicted, confidence = self.episodic_world_memory.predict_next_state(state, action)

        if predicted is None:
            return None

        self._prediction_count += 1
        pred = ForesightPrediction(
            prediction_id=f"pred_{self._prediction_count}_{int(time.time()*1e6)}",
            from_state=state,
            action=action,
            predicted_state=predicted,
            confidence_score=confidence,
        )

        passed, level = self._foresight.filter(pred)
        pred.confidence = level
        if not passed:
            return None
        return pred

    # ------------------------------------------------------------------
    # Semantic Rule Memory
    # ------------------------------------------------------------------

    def learn_from_mismatch(
        self,
        condition: str,
        expected: str,
        actual: str,
        mismatch_magnitude: float,
    ) -> Optional[SemanticRule]:
        """从预测-观察不匹配中学习规则。

        Parameters
        ----------
        condition : str
            触发条件。
        expected : str
            预期结果。
        actual : str
            观察到的实际结果。
        mismatch_magnitude : float
            不匹配幅度 (0~1, 越大越应提取规则)。

        Returns
        -------
        Optional[SemanticRule]
        """
        return self.semantic_rule_memory.extract_rule(
            condition=condition,
            expected_outcome=expected,
            actual_outcome=actual,
            confidence=mismatch_magnitude,
        )

    # ------------------------------------------------------------------
    # Test-Time Revision
    # ------------------------------------------------------------------

    def test_time_revision(
        self,
        original_prediction: str,
        revision: str,
        reason: str,
    ) -> RevisionRecord:
        """部署时记忆修正——不修改下游 Agent 参数。

        Parameters
        ----------
        original_prediction : str
            原始预测。
        revision : str
            修正后的内容。
        reason : str
            修正原因。

        Returns
        -------
        RevisionRecord
        """
        with self._lock:
            rec = RevisionRecord(
                revision_id=f"rev_{int(time.time()*1e6)}",
                original_prediction=original_prediction,
                revision=revision,
                reason=reason,
                status=RevisionStatus.APPLIED,
            )
            self._revisions.append(rec)
            logger.info("Test-time revision: %s → %s", original_prediction[:30], revision[:30])
            return rec

    def get_rules(self, condition: str) -> List[SemanticRule]:
        """获取适用的规则。"""
        return self.semantic_rule_memory.match_rules(condition)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "episodic_records": len(self.episodic_world_memory._records),
                "semantic_rules": len(self.semantic_rule_memory._rules),
                "predictions": self._prediction_count,
                "revisions": len(self._revisions),
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_state_similarity(a: WE_WorldState, b: WE_WorldState) -> float:
    """计算状态相似度——基于特征键交集。"""
    keys_a = set(a.features.keys())
    keys_b = set(b.features.keys())
    if not keys_a or not keys_b:
        return 0.0

    common = keys_a & keys_b
    if not common:
        return 0.0

    matches = 0
    for k in common:
        if str(a.features[k]) == str(b.features[k]):
            matches += 1

    return matches / len(common) * (len(common) / len(keys_a | keys_b))
