"""
TieredExperienceMemory — SE-GA Three-Tier Memory Experience Framework
======================================================================
ICML 2026 (arXiv 2605.16883) · P41-2

实现 SE-GA 三层经验记忆: TTME 三层 (episodic 短期窗口 + semantic 通用规则 +
experiential 成功经验), experience_retention 成功轨迹实时入库,
hindsight_goal_shifting 失败时后见目标转移重标成功步骤,
experience_retrieval 按相似度检索三层记忆拼接上下文。

设计要点:
  - EpisodicWindow: 滑动窗口短期记忆
  - SemanticRuleBase: 从 episodic 提炼的通用规则
  - ExperientialBase: 成功轨迹长期积累
  - HindsightGoalShifting: 失败→成功步骤重标
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

class MemoryTier(Enum):
    """TTME 三层记忆。"""
    EPISODIC = auto()       # 短期窗口
    SEMANTIC = auto()       # 通用规则
    EXPERIENTIAL = auto()   # 成功经验


class GoalStatus(Enum):
    """目标状态。"""
    SUCCESS = auto()
    FAILURE = auto()
    PARTIAL = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class EpisodicStep:
    """单步 episodic 记录。"""
    step_id: str
    state: Dict[str, Any]
    action: str
    reward: float = 0.0
    goal_achieved: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass
class SemanticRuleEntry:
    """通用规则条目。"""
    rule_id: str
    condition: str
    consequence: str
    confidence: float = 0.0
    source_episodes: List[str] = field(default_factory=list)
    activation_count: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class ExperientialRecord:
    """成功经验记录。"""
    record_id: str
    trajectory: List[EpisodicStep]  # 成功轨迹的步骤序列
    total_reward: float = 0.0
    summary: str = ""
    reuse_count: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class HindsightGoal:
    """后见目标——失败后重标的子目标。"""
    goal_id: str
    original_trajectory: List[EpisodicStep]
    relabeled_steps: List[EpisodicStep]  # 重新标注为"成功"的步骤
    achievement_rate: float = 0.0        # 原轨迹中可重标为成功的比例
    extracted_rules: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# EpisodicWindow
# ---------------------------------------------------------------------------

class EpisodicWindow:
    """短期滑动窗口——近距 episodic 记忆。

    Parameters
    ----------
    window_size : int
        窗口大小。
    """

    def __init__(self, window_size: int = 32) -> None:
        self.window_size = window_size
        self._steps: deque = deque(maxlen=window_size)
        self._lock = threading.RLock()

    def push(self, step: EpisodicStep) -> None:
        with self._lock:
            self._steps.append(step)

    def get_all(self) -> List[EpisodicStep]:
        return list(self._steps)

    def clear(self) -> None:
        self._steps.clear()

    @property
    def size(self) -> int:
        return len(self._steps)


# ---------------------------------------------------------------------------
# SemanticRuleBase
# ---------------------------------------------------------------------------

class SemanticRuleBase:
    """通用规则库——从 episodic 提炼的持久规则。

    Parameters
    ----------
    capacity : int
        最大规则数。
    """

    def __init__(self, capacity: int = 150) -> None:
        self.capacity = capacity
        self._rules: Dict[str, SemanticRuleEntry] = {}
        self._lock = threading.RLock()
        self._rule_count: int = 0

    def add_rule(self, condition: str, consequence: str, confidence: float = 0.5) -> SemanticRuleEntry:
        with self._lock:
            if len(self._rules) >= self.capacity:
                oldest = min(self._rules.items(), key=lambda x: x[1].activation_count)
                del self._rules[oldest[0]]

            self._rule_count += 1
            rule = SemanticRuleEntry(
                rule_id=f"rule_{self._rule_count}_{int(time.time()*1e6)}",
                condition=condition,
                consequence=consequence,
                confidence=confidence,
            )
            self._rules[rule.rule_id] = rule
            return rule

    def match(self, condition: str) -> List[SemanticRuleEntry]:
        kw = condition.lower()
        matched = [r for r in self._rules.values() if kw in r.condition.lower()]
        for r in matched:
            r.activation_count += 1
        return sorted(matched, key=lambda r: r.confidence, reverse=True)

    def statistics(self) -> Dict[str, Any]:
        return {"total_rules": len(self._rules)}


# ---------------------------------------------------------------------------
# ExperientialBase
# ---------------------------------------------------------------------------

class ExperientialBase:
    """成功经验长期积累。

    Parameters
    ----------
    capacity : int
        最大经验记录数。
    """

    def __init__(self, capacity: int = 200) -> None:
        self.capacity = capacity
        self._records: deque = deque(maxlen=capacity)
        self._record_count: int = 0
        self._lock = threading.RLock()

    def add_experience(self, trajectory: List[EpisodicStep], total_reward: float, summary: str = "") -> ExperientialRecord:
        with self._lock:
            self._record_count += 1
            record = ExperientialRecord(
                record_id=f"exp_{self._record_count}_{int(time.time()*1e6)}",
                trajectory=trajectory,
                total_reward=total_reward,
                summary=summary,
            )
            self._records.append(record)
            return record

    def search(self, query: str, k: int = 5) -> List[ExperientialRecord]:
        kw = query.lower()
        scored: List[Tuple[ExperientialRecord, float]] = []
        for rec in self._records:
            score = 0.0
            if kw in rec.summary.lower():
                score += 0.5
            # 匹配步骤中的 action
            for step in rec.trajectory:
                if kw in step.action.lower():
                    score += 0.1
            if score > 0:
                scored.append((rec, min(score, 1.0)))

        scored.sort(key=lambda x: x[1], reverse=True)
        results = [r for r, _ in scored[:k]]
        for r in results:
            r.reuse_count += 1
        return results

    @property
    def size(self) -> int:
        return len(self._records)


# ---------------------------------------------------------------------------
# TieredExperienceMemory
# ---------------------------------------------------------------------------

class TieredExperienceMemory:
    """SE-GA 三层经验记忆系统 (TTME)。

    Parameters
    ----------
    window_size : int
        EpisodicWindow 滑动窗口大小。
    rule_capacity : int
        SemanticRuleBase 容量。
    experiential_capacity : int
        ExperientialBase 容量。
    """

    def __init__(
        self,
        window_size: int = 32,
        rule_capacity: int = 150,
        experiential_capacity: int = 200,
    ) -> None:
        self.episodic = EpisodicWindow(window_size=window_size)
        self.semantic = SemanticRuleBase(capacity=rule_capacity)
        self.experiential = ExperientialBase(capacity=experiential_capacity)
        self._hindsight_goals: List[HindsightGoal] = []
        self._lock = threading.RLock()
        self._trajectory_count: int = 0

        logger.info(
            "TieredExperienceMemory initialized [win=%d rule=%d exp=%d]",
            window_size, rule_capacity, experiential_capacity,
        )

    # ------------------------------------------------------------------
    # Episodic Window
    # ------------------------------------------------------------------

    def record_step(
        self,
        state: Dict[str, Any],
        action: str,
        reward: float = 0.0,
        goal_achieved: bool = False,
    ) -> EpisodicStep:
        """记录一步到 episodic window。"""
        step = EpisodicStep(
            step_id=f"step_{int(time.time()*1e6)}",
            state=state, action=action, reward=reward, goal_achieved=goal_achieved,
        )
        self.episodic.push(step)
        return step

    # ------------------------------------------------------------------
    # Experience Retention
    # ------------------------------------------------------------------

    def experience_retention(
        self,
        trajectory: List[EpisodicStep],
        total_reward: float,
        summary: str = "",
    ) -> Optional[ExperientialRecord]:
        """成功轨迹实时入库积累。"""
        if total_reward <= 0:
            return None

        return self.experiential.add_experience(trajectory, total_reward, summary)

    # ------------------------------------------------------------------
    # Hindsight Goal Shifting
    # ------------------------------------------------------------------

    def hindsight_goal_shifting(
        self,
        trajectory: List[EpisodicStep],
        final_status: GoalStatus,
    ) -> Optional[HindsightGoal]:
        """失败时后见目标转移——重标成功步骤。

        Parameters
        ----------
        trajectory : List[EpisodicStep]
            完整轨迹。
        final_status : GoalStatus
            最终目标状态。

        Returns
        -------
        Optional[HindsightGoal]
            后见目标; 若最终成功则无需重标, 返回 None。
        """
        if final_status != GoalStatus.FAILURE:
            return None

        # 识别可重标为"成功"的步骤 (高 reward 的子步骤)
        relabeled: List[EpisodicStep] = []
        for step in trajectory:
            if step.reward > 0:
                relabeled.append(EpisodicStep(
                    step_id=f"hr_{step.step_id}",
                    state=step.state,
                    action=step.action,
                    reward=step.reward,
                    goal_achieved=True,  # 重标
                ))

        achievement_rate = len(relabeled) / max(len(trajectory), 1)

        # 从重标步骤提取规则
        extracted_rules: List[str] = []
        for step in relabeled:
            rule_str = f"When in {step.state}, {step.action} → success (r={step.reward:.2f})"
            extracted_rules.append(rule_str)
            self.semantic.add_rule(
                condition=str(step.state),
                consequence=step.action,
                confidence=min(step.reward, 1.0),
            )

        goal = HindsightGoal(
            goal_id=f"hg_{int(time.time()*1e6)}",
            original_trajectory=trajectory,
            relabeled_steps=relabeled,
            achievement_rate=achievement_rate,
            extracted_rules=extracted_rules,
        )
        with self._lock:
            self._hindsight_goals.append(goal)

        logger.info(
            "Hindsight goal shifted: %d/%d steps relabeled (%.1f%%)",
            len(relabeled), len(trajectory), achievement_rate * 100,
        )
        return goal

    # ------------------------------------------------------------------
    # Experience Retrieval
    # ------------------------------------------------------------------

    def experience_retrieval(self, query: str, k: int = 5) -> Dict[str, Any]:
        """查询时按相似度检索三层记忆拼接上下文。

        Returns
        -------
        Dict[str, Any]
            {"episodic": [...], "semantic": [...], "experiential": [...], "context": str}。
        """
        # 1. Episodic: 当前窗口所有步骤
        episodic_steps = self.episodic.get_all()

        # 2. Semantic: 匹配规则
        rules = self.semantic.match(query)

        # 3. Experiential: 相似成功经验
        experiences = self.experiential.search(query, k=k)

        # 拼接上下文
        context_parts = []

        if episodic_steps:
            recent_steps = episodic_steps[-5:]
            context_parts.append(
                "[Episodic] Recent steps: " +
                "; ".join(f"{s.action}" for s in recent_steps)
            )

        if rules:
            context_parts.append(
                "[Semantic] Rules: " +
                "; ".join(f"IF {r.condition[:30]} THEN {r.consequence[:30]}" for r in rules[:3])
            )

        if experiences:
            context_parts.append(
                "[Experiential] Past successes: " +
                "; ".join(e.summary[:60] for e in experiences[:3] if e.summary)
            )

        return {
            "episodic": episodic_steps,
            "semantic": rules,
            "experiential": experiences,
            "context": "\n".join(context_parts),
        }

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "tier": "TTME",
                "episodic_steps": self.episodic.size,
                "semantic_rules": self.semantic.statistics()["total_rules"],
                "experiential_records": self.experiential.size,
                "hindsight_goals": len(self._hindsight_goals),
            }
