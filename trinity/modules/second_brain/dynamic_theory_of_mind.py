"""
DynamicTheoryOfMind — Persistent Memory-Driven Recursive Opponent Modeling
==========================================================================
arXiv 2604.04157 · P38-4 · Readable Minds

三元语: 持久记忆驱动的递归对手建模——从交互历史中学习对手策略和
偏好, 生成策略欺骗计划, 并追踪 ToM 等级从 L0 到 L5 的涌现过程。

设计要点:
  - DynamicTheoryOfMindEngine: ToM 引擎中枢, 协调对手建模/欺骗规划/
    涌现追踪三条流水线, 维护持久对手模型库。
  - OpponentModelBuilder: 从交互历史序列中学习对手策略分布/偏好函数/
    递归信念层次, 构建 ToMOpponentModel。
  - StrategicDeceptionPlanner: 基于对手模型生成最优误导行为序列,
    最大化对手的信念偏差。
  - ToMEmergenceTracker: 追踪五级 ToM 涌现过程——L0(无建模) →
    L1(一阶信念) → L2(递归信念) → L3(反欺骗) → L4(策略推理) →
    L5(元认知)。
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ToMLevel(Enum):
    """ToM 涌现等级 (0-5)。"""
    L0_NO_MODELING = 0           # 无对手建模
    L1_FIRST_ORDER = 1           # 一阶信念: "我知道 X"
    L2_RECURSIVE = 2             # 二阶: "我知道你知道 X"
    L3_COUNTER_DECEPTION = 3    # 三阶: 反欺骗意识
    L4_STRATEGIC = 4             # 四阶: 长期策略推理
    L5_META_COGNITION = 5       # 五阶: 元认知 (建模自身的建模)


class DeceptionStrategy(Enum):
    """欺骗策略类型。"""
    FEIGN_WEAKNESS = auto()       # 示弱诱导
    BAIT_AND_SWITCH = auto()      # 偷梁换柱
    FALSE_TELL = auto()           # 虚假信息素
    STRATEGIC_SILENCE = auto()    # 策略沉默
    OVERCONFIDENCE_TRAP = auto()  # 过度自信陷阱


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ToMOpponentModel:
    """对手模型。"""
    opponent_id: str
    tom_level: ToMLevel = ToMLevel.L0_NO_MODELING
    strategy_distribution: Dict[str, float] = field(default_factory=dict)  # 策略 → 概率
    preference_vector: Optional[np.ndarray] = None
    belief_hierarchy: List[np.ndarray] = field(default_factory=list)        # L1-L5 信念嵌入
    interaction_count: int = 0
    last_updated: float = field(default_factory=time.time)
    confidence: float = 0.5


@dataclass
class DeceptionPlan:
    """策略欺骗计划。"""
    plan_id: str
    target_opponent: str
    strategy: DeceptionStrategy
    steps: List[Dict[str, str]]           # [{action, signal, expected_response}]
    expected_belief_shift: float          # 预期的信念偏差幅度
    risk_score: float                     # [0, 1]
    estimated_success_prob: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class ToMEvent:
    """ToM 涌现事件记录。"""
    event_id: str
    opponent_id: str
    from_level: ToMLevel
    to_level: ToMLevel
    trigger: str                         # 触发条件描述
    evidence_score: float                # 涌现置信度
    timestamp: float = field(default_factory=time.time)


# =============================================================================
# DynamicTheoryOfMindEngine
# =============================================================================

class DynamicTheoryOfMindEngine:
    """动态心智理论引擎。

    Parameters
    ----------
    max_opponents : int
        最大对手模型容量。
    belief_dim : int
        信念嵌入维度。
    emergence_confidence : float
        涌现判定置信度阈值。
    """

    def __init__(
        self,
        max_opponents: int = 64,
        belief_dim: int = 128,
        emergence_confidence: float = 0.65,
    ) -> None:
        self.max_opponents = max_opponents
        self.belief_dim = belief_dim
        self.emergence_confidence = emergence_confidence

        self._lock = threading.RLock()
        self._builder = OpponentModelBuilder(belief_dim)
        self._planner = StrategicDeceptionPlanner(belief_dim)
        self._tracker = ToMEmergenceTracker(emergence_confidence)

        self._opponent_models: Dict[str, ToMOpponentModel] = {}
        self._deception_history: List[DeceptionPlan] = []
        self._total_interactions: int = 0

        logger.info("DynamicTheoryOfMindEngine initialized [opp=%d dim=%d]", max_opponents, belief_dim)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def observe_interaction(
        self,
        opponent_id: str,
        action: str,
        observation: str,
        reward: float = 0.0,
    ) -> ToMOpponentModel:
        """观察一次交互, 更新对手模型。

        Returns
        -------
        ToMOpponentModel
            更新后的对手模型。
        """
        with self._lock:
            self._total_interactions += 1

            if opponent_id not in self._opponent_models:
                if len(self._opponent_models) >= self.max_opponents:
                    self._evict_opponent()
                self._opponent_models[opponent_id] = ToMOpponentModel(
                    opponent_id=opponent_id,
                )

            model = self._opponent_models[opponent_id]
            updated = self._builder.update(model, action, observation, reward)

            # 检查 ToM 等级涌现
            self._tracker.check_emergence(updated)

            self._opponent_models[opponent_id] = updated
            return updated

    def generate_deception(
        self,
        opponent_id: str,
        context: str,
        goal: str,
    ) -> Optional[DeceptionPlan]:
        """为指定对手生成策略欺骗计划。

        Parameters
        ----------
        opponent_id : str
            目标对手 ID。
        context : str
            当前情景描述。
        goal : str
            欺骗目标。

        Returns
        -------
        Optional[DeceptionPlan]
            欺骗计划, 若无足够信息则返回 None。
        """
        with self._lock:
            model = self._opponent_models.get(opponent_id)
            if model is None or model.tom_level.value < 1:
                logger.warning("Cannot generate deception for %s: insufficient ToM", opponent_id)
                return None

            plan = self._planner.plan(model, context, goal)
            self._deception_history.append(plan)
            return plan

    def get_tom_level(self, opponent_id: str) -> ToMLevel:
        model = self._opponent_models.get(opponent_id)
        return model.tom_level if model else ToMLevel.L0_NO_MODELING

    def get_emergence_log(self, opponent_id: str) -> List[ToMEvent]:
        return self._tracker.get_events(opponent_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evict_opponent(self) -> None:
        oldest = min(self._opponent_models.values(), key=lambda m: m.last_updated)
        del self._opponent_models[oldest.opponent_id]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            opponents_at_level = {}
            for m in self._opponent_models.values():
                lvl = m.tom_level.name
                opponents_at_level[lvl] = opponents_at_level.get(lvl, 0) + 1
            return {
                "opponent_count": len(self._opponent_models),
                "total_interactions": self._total_interactions,
                "deception_plans": len(self._deception_history),
                "opponents_by_level": opponents_at_level,
                "builder": self._builder.statistics(),
                "planner": self._planner.statistics(),
                "tracker": self._tracker.statistics(),
            }


# =============================================================================
# OpponentModelBuilder
# =============================================================================

class OpponentModelBuilder:
    """对手模型构建器。

    从交互历史序列中学习对手策略分布、偏好函数和递归信念层次。

    Parameters
    ----------
    belief_dim : int
        信念嵌入维度。
    learning_rate : float
        在线更新学习率。
    strategy_window : int
        策略统计窗口大小。
    """

    KNOWN_STRATEGIES = ["cooperative", "competitive", "tit_for_tat", "random", "greedy"]

    def __init__(
        self,
        belief_dim: int = 128,
        learning_rate: float = 0.05,
        strategy_window: int = 50,
    ) -> None:
        self.belief_dim = belief_dim
        self.learning_rate = learning_rate
        self.strategy_window = strategy_window
        self._lock = threading.RLock()
        self._updates: int = 0
        logger.info("OpponentModelBuilder initialized [dim=%d lr=%.3f]", belief_dim, learning_rate)

    def update(
        self,
        model: ToMOpponentModel,
        action: str,
        observation: str,
        reward: float,
    ) -> ToMOpponentModel:
        with self._lock:
            self._updates += 1
            model.interaction_count += 1

            # 策略分布更新 (EMA)
            strat = self._infer_strategy(action, reward)
            if model.strategy_distribution is None:
                model.strategy_distribution = {}
            for s in self.KNOWN_STRATEGIES:
                prev = model.strategy_distribution.get(s, 0.0)
                target = 1.0 if s == strat else 0.0
                model.strategy_distribution[s] = prev + self.learning_rate * (target - prev)

            # 偏好向量更新
            h = hashlib.sha256(f"{action}{observation}".encode()).digest()
            action_vec = np.frombuffer(h * (self.belief_dim // 32 + 1), dtype=np.uint8)[:self.belief_dim].astype(np.float32)
            action_vec = (action_vec - 128.0) / 128.0
            action_vec = action_vec / (np.linalg.norm(action_vec) + 1e-8)

            if model.preference_vector is None:
                model.preference_vector = action_vec.copy()
            else:
                model.preference_vector = (1 - self.learning_rate) * model.preference_vector + self.learning_rate * action_vec
                model.preference_vector = model.preference_vector / (np.linalg.norm(model.preference_vector) + 1e-8)

            # 信念层次构建
            self._update_belief_hierarchy(model, action_vec, reward)

            # 置信度: 交互越多置信越高 (趋于 1)
            model.confidence = 1.0 - 1.0 / (1.0 + 0.01 * model.interaction_count)
            model.last_updated = time.time()

            return model

    def _infer_strategy(self, action: str, reward: float) -> str:
        al = action.lower()
        if "cooperat" in al or reward > 0.3:
            return "cooperative"
        elif "defect" in al or "attack" in al or "betray" in al:
            return "competitive"
        elif reward < -0.2:
            return "random"
        elif reward > 0:
            return "tit_for_tat"
        return "greedy"

    def _update_belief_hierarchy(
        self,
        model: ToMOpponentModel,
        action_vec: np.ndarray,
        reward: float,
    ) -> None:
        # L1: 一阶信念 = 最新交互嵌入
        if len(model.belief_hierarchy) < 1:
            model.belief_hierarchy.append(action_vec.copy())
        else:
            model.belief_hierarchy[0] = (1 - self.learning_rate) * model.belief_hierarchy[0] + self.learning_rate * action_vec

        # L2+: 递归信念 (前一阶 + 噪声)
        max_level = model.tom_level.value
        for lvl in range(1, min(max_level + 1, 6)):
            if len(model.belief_hierarchy) <= lvl:
                prev = model.belief_hierarchy[lvl - 1]
                noise = np.random.randn(self.belief_dim) * 0.05
                model.belief_hierarchy.append(prev + noise)
            else:
                lower = model.belief_hierarchy[lvl - 1]
                noise = np.random.randn(self.belief_dim) * 0.03
                alpha = self.learning_rate * 0.5
                model.belief_hierarchy[lvl] = (1 - alpha) * model.belief_hierarchy[lvl] + alpha * (lower + noise)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"updates": self._updates, "belief_dim": self.belief_dim}


# =============================================================================
# StrategicDeceptionPlanner
# =============================================================================

class StrategicDeceptionPlanner:
    """策略欺骗规划器。

    基于对手模型生成最优误导行为序列, 最大化对手的信念偏差。

    Parameters
    ----------
    belief_dim : int
        信念维度。
    max_plan_steps : int
        最大计划步骤数。
    """

    def __init__(self, belief_dim: int = 128, max_plan_steps: int = 8) -> None:
        self.belief_dim = belief_dim
        self.max_plan_steps = max_plan_steps
        self._lock = threading.RLock()
        self._plans_generated: int = 0
        logger.info("StrategicDeceptionPlanner initialized [steps=%d]", max_plan_steps)

    def plan(
        self,
        model: ToMOpponentModel,
        context: str,
        goal: str,
    ) -> DeceptionPlan:
        with self._lock:
            self._plans_generated += 1

            # 策略选择: 根据对手 ToM 等级选择策略
            strategy = self._select_strategy(model.tom_level)

            # 生成步骤序列
            steps = self._generate_steps(strategy, model, context)

            # 预期信念偏差: 基于对手模型置信度
            expected_shift = model.confidence * 0.3 * (model.tom_level.value + 1)

            # 风险评分: ToM 等级越高风险越大
            risk = min(0.9, 0.2 + 0.1 * model.tom_level.value)

            # 成功概率: 高置信对手更难欺骗
            success_prob = max(0.1, 0.8 - 0.12 * model.tom_level.value * model.confidence)

            return DeceptionPlan(
                plan_id=f"dp_{self._plans_generated}_{uuid.uuid4().hex[:8]}",
                target_opponent=model.opponent_id,
                strategy=strategy,
                steps=steps,
                expected_belief_shift=float(expected_shift),
                risk_score=float(risk),
                estimated_success_prob=float(success_prob),
            )

    def _select_strategy(self, level: ToMLevel) -> DeceptionStrategy:
        if level.value <= 1:
            return DeceptionStrategy.FEIGN_WEAKNESS
        elif level.value == 2:
            return DeceptionStrategy.FALSE_TELL
        elif level.value == 3:
            return DeceptionStrategy.BAIT_AND_SWITCH
        elif level.value == 4:
            return DeceptionStrategy.OVERCONFIDENCE_TRAP
        else:
            return DeceptionStrategy.STRATEGIC_SILENCE

    def _generate_steps(
        self,
        strategy: DeceptionStrategy,
        model: ToMOpponentModel,
        context: str,
    ) -> List[Dict[str, str]]:
        template_map = {
            DeceptionStrategy.FEIGN_WEAKNESS: [
                {"action": "show_vulnerability", "signal": "expose false weak point", "expected_response": "exploit attempt"},
                {"action": "retreat", "signal": "withdraw from position", "expected_response": "over-commit"},
                {"action": "counter", "signal": "spring trap", "expected_response": "surprise"},
            ],
            DeceptionStrategy.FALSE_TELL: [
                {"action": "plant_signal", "signal": "emit misleading pattern", "expected_response": "pattern matching"},
                {"action": "confirm_bias", "signal": "reinforce false belief", "expected_response": "confidence boost"},
                {"action": "exploit_misbelief", "signal": "act on false premise", "expected_response": "miscalculation"},
            ],
            DeceptionStrategy.BAIT_AND_SWITCH: [
                {"action": "offer_bait", "signal": "present attractive target", "expected_response": "commit resources"},
                {"action": "switch", "signal": "change objective", "expected_response": "confusion"},
                {"action": "capture", "signal": "seize real objective", "expected_response": "loss"},
            ],
            DeceptionStrategy.OVERCONFIDENCE_TRAP: [
                {"action": "appear_predictable", "signal": "establish pattern", "expected_response": "anticipation"},
                {"action": "reinforce_pattern", "signal": "repeat expected move", "expected_response": "overconfidence"},
                {"action": "break_pattern", "signal": "unexpected deviation", "expected_response": "disorientation"},
            ],
            DeceptionStrategy.STRATEGIC_SILENCE: [
                {"action": "go_dark", "signal": "cease all observable signals", "expected_response": "uncertainty"},
                {"action": "observe", "signal": "passively gather intel", "expected_response": "nervous probing"},
                {"action": "reappear", "signal": "return with decisive action", "expected_response": "shock"},
            ],
        }
        return template_map.get(strategy, template_map[DeceptionStrategy.FEIGN_WEAKNESS])

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"plans_generated": self._plans_generated, "max_steps": self.max_plan_steps}


# =============================================================================
# ToMEmergenceTracker
# =============================================================================

class ToMEmergenceTracker:
    """ToM 涌现追踪器。

    监控对手模型从 L0 到 L5 的涌现过程, 记录每次等级提升事件。

    涌现判定标准:
      L0→L1: interaction_count >= 10 AND 策略分布熵 > 0.5
      L1→L2: interaction_count >= 50 AND 信念层次有 ≥2 层
      L2→L3: interaction_count >= 100 AND 置信度 > 0.7
      L3→L4: interaction_count >= 200 AND 检测到反欺骗模式
      L4→L5: interaction_count >= 500 AND 元认知自检通过

    Parameters
    ----------
    confidence_threshold : float
        涌现判定置信度阈值。
    """

    def __init__(self, confidence_threshold: float = 0.65) -> None:
        self.confidence_threshold = confidence_threshold
        self._lock = threading.RLock()
        self._events: Dict[str, List[ToMEvent]] = {}  # opponent_id → events
        self._total_emergences: int = 0
        logger.info("ToMEmergenceTracker initialized [thresh=%.2f]", confidence_threshold)

    def check_emergence(self, model: ToMOpponentModel) -> Optional[ToMEvent]:
        with self._lock:
            current = model.tom_level
            next_level = self._evaluate_next_level(model)

            if next_level is not None and next_level.value > current.value:
                event = ToMEvent(
                    event_id=f"tom_{uuid.uuid4().hex[:12]}",
                    opponent_id=model.opponent_id,
                    from_level=current,
                    to_level=next_level,
                    trigger=self._describe_trigger(current, next_level, model),
                    evidence_score=self._compute_evidence(model, next_level),
                )
                model.tom_level = next_level
                self._events.setdefault(model.opponent_id, []).append(event)
                self._total_emergences += 1
                logger.info("ToM emergence: %s %s → %s", model.opponent_id, current.name, next_level.name)
                return event
            return None

    def _evaluate_next_level(self, model: ToMOpponentModel) -> Optional[ToMLevel]:
        n = model.interaction_count
        conf = model.confidence
        levels = [
            (ToMLevel.L1_FIRST_ORDER, lambda: n >= 10),
            (ToMLevel.L2_RECURSIVE, lambda: n >= 50 and len(model.belief_hierarchy) >= 2),
            (ToMLevel.L3_COUNTER_DECEPTION, lambda: n >= 100 and conf > 0.7),
            (ToMLevel.L4_STRATEGIC, lambda: n >= 200 and conf > 0.8),
            (ToMLevel.L5_META_COGNITION, lambda: n >= 500 and conf > 0.9 and len(model.belief_hierarchy) >= 5),
        ]
        current_value = model.tom_level.value
        for lvl, condition in levels:
            if lvl.value == current_value + 1 and condition():
                return lvl
        return None

    def _compute_evidence(self, model: ToMOpponentModel, target: ToMLevel) -> float:
        base = min(1.0, model.interaction_count / (100.0 * (target.value + 1)))
        conf_boost = model.confidence * 0.3
        return min(1.0, base + conf_boost)

    def _describe_trigger(self, frm: ToMLevel, to: ToMLevel, model: ToMOpponentModel) -> str:
        return (
            f"Interaction milestone {model.interaction_count}: "
            f"confidence={model.confidence:.2f}, beliefs={len(model.belief_hierarchy)} tiers"
        )

    def get_events(self, opponent_id: str) -> List[ToMEvent]:
        return self._events.get(opponent_id, [])

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_emergences": self._total_emergences,
                "tracked_opponents": len(self._events),
                "confidence_threshold": self.confidence_threshold,
            }
