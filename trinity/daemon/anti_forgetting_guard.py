#!/usr/bin/env python3
"""
Anti-Forgetting Guard — 抗遗忘防御模块 (Layer 6a)
====================================================
Based on SDPO: Steerable Diversity-Preserving Optimization for
Continual Learning (arXiv 2607.01763).

作为 auto_daemon 防御链的 Layer 6a，并联接入 Layer 6 防御层：
  防御链：5a → 5b → 5c → PRE_GATE → 5 → 6a (ANTI_FORGETTING, 并联) → 6

三大核心组件：
  1. ForgettingMonitor       — 遗忘监测器：检测知识/技能退化
  2. ExplorationDiversityGuard — 探索多样性守卫：防止策略坍缩
  3. KnowledgeDistillationAuditor — 知识蒸馏审计：验证蒸馏质量

SDPO 核心思想：
  - 在持续学习中，优化目标不仅仅是新任务性能，还要保持旧知识的多样性
  - 通过 steerable diversity 约束，防止 catastrophic forgetting
  - 三个组件分别对应监测、约束、审计三个维度

Paper: https://arxiv.org/abs/2607.01763
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np


# ============================================================================
# Enums & Type Aliases
# ============================================================================


class ForgettingAlert(Enum):
    """遗忘告警级别"""
    NONE = "none"           # 无遗忘
    MILD = "mild"           # 轻度遗忘（可自动恢复）
    MODERATE = "moderate"   # 中度遗忘（需干预）
    SEVERE = "severe"       # 严重遗忘（需回滚）
    CATASTROPHIC = "catastrophic"  # 灾难性遗忘（紧急阻断）


class DiversityStatus(Enum):
    """策略多样性状态"""
    HEALTHY = "healthy"            # 多样性充足
    NARROWING = "narrowing"        # 正在收窄
    COLLAPSING = "collapsing"      # 坍缩中
    COLLAPSED = "collapsed"        # 已坍缩


class DistillationQuality(Enum):
    """知识蒸馏质量"""
    EXCELLENT = "excellent"
    GOOD = "good"
    ADEQUATE = "adequate"
    DEGRADED = "degraded"
    CORRUPTED = "corrupted"


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class KnowledgeSnapshot:
    """知识快照：某时刻的 agent 知识状态"""
    snapshot_id: str
    timestamp: float = field(default_factory=time.time)
    # 技能签名：{skill_name: performance_score}
    skill_signatures: Dict[str, float] = field(default_factory=dict)
    # 策略分布：{strategy_name: probability}
    strategy_distribution: Dict[str, float] = field(default_factory=dict)
    # 知识嵌入（简化为关键概念向量）
    knowledge_vectors: Dict[str, List[float]] = field(default_factory=dict)
    # 元数据
    task_context: Optional[str] = None
    model_version: Optional[str] = None


@dataclass
class ForgettingEvent:
    """遗忘事件记录"""
    event_id: str
    alert_level: ForgettingAlert
    detected_at: float = field(default_factory=time.time)
    # 受影响的知识/技能
    affected_skills: List[str] = field(default_factory=list)
    affected_knowledge: List[str] = field(default_factory=list)
    # 量化指标
    forgetting_score: float = 0.0
    degradation_rate: float = 0.0  # 退化速率（per time unit）
    # 快照对比
    baseline_snapshot_id: Optional[str] = None
    current_snapshot_id: Optional[str] = None
    # 诊断信息
    diagnosis: Optional[str] = None
    recommended_action: Optional[str] = None


@dataclass
class DiversityReport:
    """多样性报告"""
    report_id: str
    status: DiversityStatus
    timestamp: float = field(default_factory=time.time)
    # 多样性度量
    strategy_entropy: float = 0.0  # 策略熵
    action_diversity_index: float = 0.0  # 动作多样性指数
    exploration_rate: float = 0.0  # 探索率
    # 分布统计
    dominant_strategy: Optional[str] = None
    dominant_strategy_share: float = 0.0  # 主导策略占比
    # 趋势
    entropy_trend: List[float] = field(default_factory=list)  # 最近N次的熵值
    collapse_risk: float = 0.0  # 坍缩风险 [0,1]
    suggestions: List[str] = field(default_factory=list)


@dataclass
class DistillationAuditResult:
    """蒸馏审计结果"""
    audit_id: str
    quality: DistillationQuality
    timestamp: float = field(default_factory=time.time)
    # 保真度指标
    fidelity_score: float = 0.0       # 教师-学生输出一致性
    knowledge_retention: float = 0.0  # 知识保留率
    diversity_preservation: float = 0.0  # 教师多样性的保留程度
    # 退化检测
    degraded_dimensions: List[str] = field(default_factory=list)
    preserved_dimensions: List[str] = field(default_factory=list)
    # 审计元数据
    teacher_version: Optional[str] = None
    student_version: Optional[str] = None
    sample_count: int = 0
    recommendations: List[str] = field(default_factory=list)


# ============================================================================
# Component 1: ForgettingMonitor — 遗忘监测器
# ============================================================================


class ForgettingMonitor:
    """
    ForgettingMonitor — SDPO 遗忘监测器

    职责：
    1. 定期采集知识快照，建立技能/知识基线
    2. 对比快照，检测性能退化（forgetting detection）
    3. 基于退化速率和模式，分级告警
    4. 支持 SDPO 的 steerable diversity 指标监测

    检测算法（基于 SDPO 的向后迁移 BWT 指标）：
      BWT = (1/|T|) Σ_t (performance_on_task_t_after - performance_on_task_t_before)
      负 BWT 表示遗忘。

    告警分级：
      - NONE:        BWT >= -0.01
      - MILD:        -0.05 <= BWT < -0.01
      - MODERATE:    -0.10 <= BWT < -0.05
      - SEVERE:      -0.20 <= BWT < -0.10
      - CATASTROPHIC: BWT < -0.20
    """

    # 告警阈值（BWT 值）
    ALERT_THRESHOLDS: Dict[ForgettingAlert, float] = {
        ForgettingAlert.CATASTROPHIC: -0.20,
        ForgettingAlert.SEVERE: -0.10,
        ForgettingAlert.MODERATE: -0.05,
        ForgettingAlert.MILD: -0.01,
    }

    def __init__(
        self,
        snapshot_interval_seconds: float = 300.0,
        max_snapshots: int = 100,
        bwt_window_size: int = 10,
        degradation_sensitivity: float = 0.05,
    ):
        self.snapshot_interval = snapshot_interval_seconds
        self.max_snapshots = max_snapshots
        self.bwt_window_size = bwt_window_size
        self.degradation_sensitivity = degradation_sensitivity

        # 快照存储
        self.snapshots: Dict[str, KnowledgeSnapshot] = {}
        self.snapshot_history: deque = deque(maxlen=max_snapshots)

        # 遗忘事件日志
        self.forgetting_events: List[ForgettingEvent] = []

        # 性能趋势
        self.performance_history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=bwt_window_size)
        )

        # 统计
        self.total_snapshots: int = 0
        self.total_alerts: int = 0
        self.last_snapshot_time: float = 0.0

    # ---- Core API ----

    def capture_snapshot(
        self,
        skill_signatures: Dict[str, float],
        strategy_distribution: Optional[Dict[str, float]] = None,
        knowledge_vectors: Optional[Dict[str, List[float]]] = None,
        task_context: Optional[str] = None,
        model_version: Optional[str] = None,
    ) -> KnowledgeSnapshot:
        """
        采集当前时刻的知识快照。

        参数：
          skill_signatures: 各技能的当前性能分数 {skill_name: score ∈ [0,1]}
          strategy_distribution: 策略分布 {strategy: probability}
          knowledge_vectors: 关键知识的向量表示
        """
        snapshot = KnowledgeSnapshot(
            snapshot_id=f"snap_{uuid.uuid4().hex[:8]}",
            skill_signatures=dict(skill_signatures),
            strategy_distribution=dict(strategy_distribution or {}),
            knowledge_vectors=dict(knowledge_vectors or {}),
            task_context=task_context,
            model_version=model_version,
        )

        self.snapshots[snapshot.snapshot_id] = snapshot
        self.snapshot_history.append(snapshot)
        self.total_snapshots += 1
        self.last_snapshot_time = snapshot.timestamp

        # 记录性能历史
        for skill, score in skill_signatures.items():
            self.performance_history[skill].append(score)

        return snapshot

    def detect_forgetting(
        self,
        baseline_snapshot_id: Optional[str] = None,
        current_snapshot_id: Optional[str] = None,
    ) -> Optional[ForgettingEvent]:
        """
        检测遗忘：对比基线与当前快照。

        使用 SDPO 的 BWT (Backward Transfer) 指标：
          BWT = mean(current_score - baseline_score) over all skills

        同时检测：
          - 单技能退化：任何技能下降超过 threshold
          - 策略分布偏移：KL divergence 增大
        """
        # 选择快照
        if baseline_snapshot_id:
            baseline = self.snapshots.get(baseline_snapshot_id)
        elif len(self.snapshot_history) >= 2:
            # 默认：最早 vs 最新
            baseline = self.snapshot_history[0]
        else:
            return None  # 不足两次快照

        if current_snapshot_id:
            current = self.snapshots.get(current_snapshot_id)
        else:
            current = self.snapshot_history[-1]

        if baseline is None or current is None:
            return None

        # 计算 BWT
        bwt_scores = []
        affected_skills = []
        affected_knowledge = []

        all_skills = set(baseline.skill_signatures.keys()) | set(current.skill_signatures.keys())
        for skill in all_skills:
            baseline_score = baseline.skill_signatures.get(skill, 0.0)
            current_score = current.skill_signatures.get(skill, 0.0)
            delta = current_score - baseline_score
            bwt_scores.append(delta)

            if delta < -self.degradation_sensitivity:
                affected_skills.append(skill)

        # 检查知识向量退化
        for kname in baseline.knowledge_vectors:
            if kname in current.knowledge_vectors:
                cosine_sim = self._cosine_similarity(
                    baseline.knowledge_vectors[kname],
                    current.knowledge_vectors[kname],
                )
                if cosine_sim < 0.7:  # 知识表示显著偏移
                    affected_knowledge.append(kname)

        # 综合 BWT
        mean_bwt = np.mean(bwt_scores) if bwt_scores else 0.0

        # 分级
        alert_level = self._classify_forgetting(mean_bwt, affected_skills, affected_knowledge)

        if alert_level == ForgettingAlert.NONE:
            return None

        # 计算退化速率
        time_delta = current.timestamp - baseline.timestamp
        degradation_rate = abs(mean_bwt) / max(time_delta, 1.0) * 3600  # per hour

        event = ForgettingEvent(
            event_id=f"fgt_{uuid.uuid4().hex[:8]}",
            alert_level=alert_level,
            affected_skills=affected_skills,
            affected_knowledge=affected_knowledge,
            forgetting_score=abs(mean_bwt),
            degradation_rate=degradation_rate,
            baseline_snapshot_id=baseline.snapshot_id,
            current_snapshot_id=current.snapshot_id,
            diagnosis=self._diagnose(mean_bwt, affected_skills, affected_knowledge),
            recommended_action=self._recommend_action(alert_level, affected_skills),
        )

        self.forgetting_events.append(event)
        self.total_alerts += 1

        return event

    def compute_bwt(
        self,
        skill_name: str,
    ) -> float:
        """
        计算单个技能的向后迁移指标。

        BWT = (1/(T-1)) * Σ_{t=1}^{T-1} (R_{t,T} - R_{t,t})
        其中 R_{t,T} 是训练完所有 T 个任务后在第 t 个任务上的性能。
        """
        history = list(self.performance_history.get(skill_name, []))
        if len(history) < 2:
            return 0.0

        T = len(history)
        bwt = 0.0
        for t in range(T - 1):
            R_tt = history[t]           # 训练任务 t 后的性能
            R_tT = history[-1]          # 训练完所有任务后的性能
            bwt += R_tT - R_tt

        return bwt / max(T - 1, 1)

    # ---- Internal ----

    def _classify_forgetting(
        self,
        mean_bwt: float,
        affected_skills: List[str],
        affected_knowledge: List[str],
    ) -> ForgettingAlert:
        """基于 BWT 和受影响范围分级。"""
        # 基础 BWT 分级
        base_level = ForgettingAlert.NONE
        for alert, threshold in sorted(
            self.ALERT_THRESHOLDS.items(),
            key=lambda x: x[1],
            reverse=True  # 从最严重开始检查
        ):
            if mean_bwt <= threshold:
                base_level = alert
                break

        # 升级规则：影响面大时提升一级
        total_skills = len(self.performance_history)
        affected_ratio = len(affected_skills) / max(total_skills, 1)

        if affected_ratio > 0.5 and base_level.value != "catastrophic":
            # 影响超过 50% 技能，升级
            level_order = [ForgettingAlert.NONE, ForgettingAlert.MILD,
                          ForgettingAlert.MODERATE, ForgettingAlert.SEVERE,
                          ForgettingAlert.CATASTROPHIC]
            try:
                idx = level_order.index(base_level)
                base_level = level_order[min(idx + 1, len(level_order) - 1)]
            except ValueError:
                pass

        if affected_knowledge and base_level == ForgettingAlert.NONE:
            base_level = ForgettingAlert.MILD

        return base_level

    def _diagnose(
        self,
        mean_bwt: float,
        affected_skills: List[str],
        affected_knowledge: List[str],
    ) -> str:
        parts = []
        if affected_skills:
            parts.append(f"{len(affected_skills)} skills degraded: {', '.join(affected_skills[:5])}")
        if affected_knowledge:
            parts.append(f"{len(affected_knowledge)} knowledge vectors drifted")
        parts.append(f"Mean BWT = {mean_bwt:.4f}")
        return "; ".join(parts)

    def _recommend_action(
        self,
        alert_level: ForgettingAlert,
        affected_skills: List[str],
    ) -> str:
        actions = {
            ForgettingAlert.MILD: (
                "Schedule targeted rehearsal for affected skills. "
                "No immediate intervention needed."
            ),
            ForgettingAlert.MODERATE: (
                "Initiate focused retraining on affected skills. "
                f"Priority: {', '.join(affected_skills[:3])}. "
                "Consider reducing learning rate for new tasks."
            ),
            ForgettingAlert.SEVERE: (
                "URGENT: Rollback to last known good checkpoint. "
                "Pause new task learning. "
                "Run full skill audit before resuming."
            ),
            ForgettingAlert.CATASTROPHIC: (
                "CRITICAL: Immediate learning halt. "
                "Restore from baseline snapshot. "
                "Investigate root cause before any further updates. "
                "Escalate to human operator."
            ),
        }
        return actions.get(alert_level, "Monitor and report.")

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def summary(self) -> Dict[str, Any]:
        return {
            "total_snapshots": self.total_snapshots,
            "total_alerts": self.total_alerts,
            "alert_distribution": {
                level.value: sum(
                    1 for e in self.forgetting_events if e.alert_level == level
                )
                for level in ForgettingAlert
            },
            "tracked_skills": list(self.performance_history.keys()),
        }


# ============================================================================
# Component 2: ExplorationDiversityGuard — 探索多样性守卫
# ============================================================================


class ExplorationDiversityGuard:
    """
    ExplorationDiversityGuard — SDPO 探索多样性守卫

    职责：
    1. 监测策略/动作分布的熵，防止策略坍缩
    2. 强制最小探索率（SDPO steerable diversity）
    3. 多样性奖励：奖励高熵策略，惩罚策略坍缩
    4. 自适应探索调度：根据坍缩风险动态调整探索强度

    SDPO 关键公式：
      Diversity Loss = -λ * H(π) 其中 H(π) 是策略熵
      通过 steerable λ 控制多样性约束强度
    """

    # 熵阈值（标准化熵：H / H_max）
    ENTROPY_THRESHOLD_HEALTHY = 0.6    # 高于此值 = 健康
    ENTROPY_THRESHOLD_NARROWING = 0.4  # 低于此值 = 收窄
    ENTROPY_THRESHOLD_COLLAPSING = 0.2  # 低于此值 = 坍缩中
    ENTROPY_THRESHOLD_COLLAPSED = 0.05  # 低于此值 = 已坍缩

    def __init__(
        self,
        min_exploration_rate: float = 0.05,
        max_exploration_rate: float = 0.30,
        diversity_lambda: float = 1.0,       # SDPO steerable λ
        entropy_window_size: int = 20,
        collapse_intervention_threshold: float = 0.3,
    ):
        self.min_exploration_rate = min_exploration_rate
        self.max_exploration_rate = max_exploration_rate
        self.diversity_lambda = diversity_lambda
        self.entropy_window_size = entropy_window_size
        self.collapse_intervention_threshold = collapse_intervention_threshold

        # 策略分布历史
        self.strategy_history: deque = deque(maxlen=entropy_window_size)
        self.action_history: deque = deque(maxlen=entropy_window_size * 10)

        # 当前探索率
        self.current_exploration_rate: float = min_exploration_rate

        # 多样性报告历史
        self.diversity_reports: List[DiversityReport] = []

        # 统计
        self.intervention_count: int = 0
        self.collapse_alerts: int = 0

    # ---- Core API ----

    def observe_strategy(
        self,
        strategy_distribution: Dict[str, float],
        selected_action: Optional[str] = None,
    ):
        """记录策略分布和动作选择。"""
        if strategy_distribution:
            self.strategy_history.append(dict(strategy_distribution))
        if selected_action:
            self.action_history.append(selected_action)

    def compute_entropy(self, distribution: Dict[str, float]) -> float:
        """
        计算策略分布的香农熵。

        H = -Σ p_i * log(p_i)
        标准化熵 = H / log(N) （N 为策略数）
        """
        if not distribution:
            return 0.0

        total = sum(distribution.values())
        if total <= 0:
            return 0.0

        probs = [v / total for v in distribution.values()]
        entropy = -sum(p * math.log(max(p, 1e-12)) for p in probs)

        # 标准化
        n = len(distribution)
        max_entropy = math.log(max(n, 1))
        return entropy / max(max_entropy, 1e-12)

    def compute_action_diversity(self) -> float:
        """计算动作多样性指数（基于最近动作的频率分布）。"""
        if not self.action_history:
            return 0.0

        action_counts: Dict[str, int] = defaultdict(int)
        for a in self.action_history:
            action_counts[a] += 1

        total = len(self.action_history)
        if total == 0:
            return 0.0

        # Simpson's Diversity Index: 1 - Σ (n/N)²
        simpson = 1.0 - sum((c / total) ** 2 for c in action_counts.values())
        return simpson

    def assess_diversity(self) -> DiversityReport:
        """
        全面评估当前策略多样性状态。

        返回 DiversityReport 包含：
          - 策略熵（标准化）
          - 动作多样性指数
          - 坍缩风险评估
          - 自适应探索建议
        """
        # 当前策略分布（取最近一次）
        current_dist = self.strategy_history[-1] if self.strategy_history else {}

        # 计算熵
        normalized_entropy = self.compute_entropy(current_dist)

        # 动作多样性
        action_div = self.compute_action_diversity()

        # 主导策略分析
        dominant = None
        dominant_share = 0.0
        if current_dist:
            dominant = max(current_dist, key=current_dist.get)
            dominant_share = current_dist[dominant] / sum(current_dist.values())

        # 熵趋势
        entropy_trend = [
            self.compute_entropy(d)
            for d in list(self.strategy_history)[-self.entropy_window_size:]
        ]

        # 坍缩风险
        collapse_risk = self._compute_collapse_risk(normalized_entropy, entropy_trend)

        # 状态判定
        if normalized_entropy >= self.ENTROPY_THRESHOLD_HEALTHY:
            status = DiversityStatus.HEALTHY
        elif normalized_entropy >= self.ENTROPY_THRESHOLD_NARROWING:
            status = DiversityStatus.NARROWING
        elif normalized_entropy >= self.ENTROPY_THRESHOLD_COLLAPSING:
            status = DiversityStatus.COLLAPSING
        else:
            status = DiversityStatus.COLLAPSED
            self.collapse_alerts += 1

        # 自适应探索率
        self._adapt_exploration_rate(status)

        # 建议
        suggestions = self._generate_diversity_suggestions(status, collapse_risk)

        report = DiversityReport(
            report_id=f"div_{uuid.uuid4().hex[:8]}",
            status=status,
            strategy_entropy=normalized_entropy,
            action_diversity_index=action_div,
            exploration_rate=self.current_exploration_rate,
            dominant_strategy=dominant,
            dominant_strategy_share=dominant_share,
            entropy_trend=entropy_trend,
            collapse_risk=collapse_risk,
            suggestions=suggestions,
        )

        self.diversity_reports.append(report)
        return report

    def get_exploration_schedule(self) -> Dict[str, Any]:
        """
        返回当前探索调度参数。

        用于 SDPO steerable diversity：外部可据此调整 λ。
        """
        return {
            "exploration_rate": self.current_exploration_rate,
            "diversity_lambda": self.diversity_lambda,
            "epsilon_greedy": {
                "epsilon": self.current_exploration_rate,
                "decay": 0.999 if self.current_exploration_rate > self.min_exploration_rate else 1.0,
            },
            "boltzmann_temperature": 1.0 / max(self.current_exploration_rate, 0.01),
        }

    def apply_diversity_penalty(
        self,
        strategy_distribution: Dict[str, float],
    ) -> Dict[str, float]:
        """
        SDPO 多样性惩罚：对坍缩策略施加负奖励。

        返回调整后的分数：adjusted_score = score + λ_diversity * (H - H_target)
        其中 H 是当前策略熵。
        """
        H = self.compute_entropy(strategy_distribution)
        H_target = 0.5  # 目标标准化熵

        penalty = self.diversity_lambda * (H - H_target)

        adjusted = {}
        for strategy, value in strategy_distribution.items():
            # 高熵策略获得奖励，低熵受到惩罚
            adjusted[strategy] = value + penalty * value

        # 重新归一化
        total = sum(adjusted.values())
        if total > 0:
            adjusted = {k: v / total for k, v in adjusted.items()}

        return adjusted

    # ---- Internal ----

    def _compute_collapse_risk(
        self,
        current_entropy: float,
        entropy_trend: List[float],
    ) -> float:
        """计算策略坍缩风险 [0, 1]。"""
        if not entropy_trend:
            return 0.0

        # 当前熵的贡献
        base_risk = max(0.0, 1.0 - current_entropy / self.ENTROPY_THRESHOLD_HEALTHY)

        # 趋势贡献：如果熵持续下降，风险更高
        if len(entropy_trend) >= 3:
            recent = entropy_trend[-3:]
            if len(recent) >= 2 and all(
                recent[i] <= recent[i - 1] for i in range(1, len(recent))
            ):
                base_risk *= 1.5

        # 陡降检测
        if len(entropy_trend) >= 5:
            mid = len(entropy_trend) // 2
            first_half_mean = np.mean(entropy_trend[:mid]) if entropy_trend[:mid] else 0
            second_half_mean = np.mean(entropy_trend[mid:]) if entropy_trend[mid:] else 0
            drop = first_half_mean - second_half_mean
            if drop > 0.2:
                base_risk = min(1.0, base_risk * (1.0 + drop * 3))

        return min(1.0, max(0.0, base_risk))

    def _adapt_exploration_rate(self, status: DiversityStatus):
        """自适应调整探索率。"""
        if status == DiversityStatus.HEALTHY:
            # 健康状态缓慢降低探索
            self.current_exploration_rate = max(
                self.min_exploration_rate,
                self.current_exploration_rate * 0.98,
            )
        elif status == DiversityStatus.NARROWING:
            # 开始收窄，适度提升探索
            self.current_exploration_rate = min(
                self.max_exploration_rate,
                self.current_exploration_rate * 1.1,
            )
            self.intervention_count += 1
        elif status in (DiversityStatus.COLLAPSING, DiversityStatus.COLLAPSED):
            # 坍缩，大幅提升探索
            self.current_exploration_rate = min(
                self.max_exploration_rate,
                self.current_exploration_rate * 1.5,
            )
            self.intervention_count += 1

    def _generate_diversity_suggestions(
        self,
        status: DiversityStatus,
        collapse_risk: float,
    ) -> List[str]:
        suggestions = []
        if status == DiversityStatus.HEALTHY:
            suggestions.append("Maintain current exploration schedule.")
        elif status == DiversityStatus.NARROWING:
            suggestions.append("Increase exploration rate by 10%.")
            suggestions.append("Consider adding noise to dominant strategy.")
        elif status == DiversityStatus.COLLAPSING:
            suggestions.append("URGENT: Boost exploration rate to 30%.")
            suggestions.append("Temporarily disable greedy strategy selection.")
            suggestions.append("Inject random action perturbations.")
        elif status == DiversityStatus.COLLAPSED:
            suggestions.append("CRITICAL INSERT: Force random exploration for next N steps.")
            suggestions.append("Reinitialize strategy distribution from scratch.")
            suggestions.append("Apply SDPO diversity loss with λ=5.0.")

        if collapse_risk > 0.7:
            suggestions.append("High collapse risk — consider early intervention.")

        return suggestions

    def summary(self) -> Dict[str, Any]:
        return {
            "intervention_count": self.intervention_count,
            "collapse_alerts": self.collapse_alerts,
            "current_exploration_rate": self.current_exploration_rate,
            "latest_status": (
                self.diversity_reports[-1].status.value
                if self.diversity_reports else "unknown"
            ),
            "total_reports": len(self.diversity_reports),
        }


# ============================================================================
# Component 3: KnowledgeDistillationAuditor — 知识蒸馏审计
# ============================================================================


class KnowledgeDistillationAuditor:
    """
    KnowledgeDistillationAuditor — SDPO 知识蒸馏审计器

    职责：
    1. 审计教师→学生蒸馏过程的知识保留率
    2. 逐维度评估蒸馏质量（哪些维度保留了，哪些退化了）
    3. SDPO diversity preservation 检查：蒸馏是否保持了教师多样性
    4. 生成结构化审计报告

    审计指标（基于 SDPO）：
      - Fidelity: KL(teacher_output || student_output) on holdout set
      - Retention: cosine_sim(teacher_rep, student_rep) per knowledge dim
      - Diversity Preservation: H(student) / H(teacher)
    """

    def __init__(
        self,
        fidelity_threshold: float = 0.85,
        retention_threshold: float = 0.80,
        diversity_preservation_threshold: float = 0.75,
        degraded_dimension_threshold: float = 0.60,
        sample_sufficiency: int = 100,
    ):
        self.fidelity_threshold = fidelity_threshold
        self.retention_threshold = retention_threshold
        self.diversity_preservation_threshold = diversity_preservation_threshold
        self.degraded_dimension_threshold = degraded_dimension_threshold
        self.sample_sufficiency = sample_sufficiency

        # 审计历史
        self.audit_results: List[DistillationAuditResult] = []

        # 统计
        self.total_audits: int = 0
        self.failed_audits: int = 0

    # ---- Core API ----

    def audit_distillation(
        self,
        teacher_outputs: Dict[str, List[float]],     # {dim_name: teacher_embedding}
        student_outputs: Dict[str, List[float]],     # {dim_name: student_embedding}
        teacher_strategy_dist: Optional[Dict[str, float]] = None,
        student_strategy_dist: Optional[Dict[str, float]] = None,
        teacher_version: Optional[str] = None,
        student_version: Optional[str] = None,
        sample_count: int = 0,
    ) -> DistillationAuditResult:
        """
        执行完整的知识蒸馏审计。

        参数：
          teacher_outputs: 教师模型在各知识维度上的输出嵌入
          student_outputs: 学生模型在各知识维度上的输出嵌入
          teacher_strategy_dist: 教师策略分布（用于多样性审计）
          student_strategy_dist: 学生策略分布（用于多样性审计）
        """
        self.total_audits += 1

        # 1. Fidelity 评估
        fidelity = self._compute_fidelity(teacher_outputs, student_outputs)

        # 2. 逐维度 Retention 评估
        retention_scores = {}
        degraded_dims = []
        preserved_dims = []

        common_dims = set(teacher_outputs.keys()) & set(student_outputs.keys())
        for dim in common_dims:
            similarity = self._cosine_similarity(
                teacher_outputs[dim],
                student_outputs[dim],
            )
            retention_scores[dim] = similarity

            if similarity < self.degraded_dimension_threshold:
                degraded_dims.append(dim)
            else:
                preserved_dims.append(dim)

        # 综合知识保留率
        knowledge_retention = (
            np.mean(list(retention_scores.values()))
            if retention_scores else 0.0
        )

        # 3. Diversity Preservation
        diversity_preservation = self._compute_diversity_preservation(
            teacher_strategy_dist,
            student_strategy_dist,
        )

        # 4. 质量判定
        quality = self._classify_quality(fidelity, knowledge_retention, diversity_preservation)
        if quality in (DistillationQuality.DEGRADED, DistillationQuality.CORRUPTED):
            self.failed_audits += 1

        # 5. 建议
        recommendations = self._generate_recommendations(
            quality,
            degraded_dims,
            fidelity,
            knowledge_retention,
            diversity_preservation,
        )

        result = DistillationAuditResult(
            audit_id=f"audit_{uuid.uuid4().hex[:8]}",
            quality=quality,
            fidelity_score=fidelity,
            knowledge_retention=knowledge_retention,
            diversity_preservation=diversity_preservation,
            degraded_dimensions=degraded_dims,
            preserved_dimensions=preserved_dims,
            teacher_version=teacher_version,
            student_version=student_version,
            sample_count=sample_count,
            recommendations=recommendations,
        )

        self.audit_results.append(result)
        return result

    def compute_dimension_level_report(
        self,
        teacher_outputs: Dict[str, List[float]],
        student_outputs: Dict[str, List[float]],
    ) -> Dict[str, Dict[str, float]]:
        """
        逐维度详细报告：每个知识/技能维度的保留情况。
        """
        report = {}
        all_dims = set(teacher_outputs.keys()) | set(student_outputs.keys())

        for dim in all_dims:
            t_vec = teacher_outputs.get(dim, [])
            s_vec = student_outputs.get(dim, [])

            similarity = self._cosine_similarity(t_vec, s_vec) if t_vec and s_vec else 0.0
            magnitude_ratio = (
                (np.linalg.norm(s_vec) / max(np.linalg.norm(t_vec), 1e-12))
                if t_vec and s_vec else 1.0
            )

            report[dim] = {
                "cosine_similarity": round(similarity, 4),
                "magnitude_ratio": round(magnitude_ratio, 4),
                "status": "preserved" if similarity >= self.retention_threshold else "degraded",
            }

        return report

    def should_accept_distillation(
        self,
        result: DistillationAuditResult,
    ) -> Tuple[bool, str]:
        """
        决定是否接受本次蒸馏结果。

        返回 (accepted, reason)。
        """
        if result.quality == DistillationQuality.CORRUPTED:
            return False, "Corrupted distillation: critical quality failure."
        if result.quality == DistillationQuality.DEGRADED:
            return False, f"Degraded: {len(result.degraded_dimensions)} dimensions below threshold."
        if result.fidelity_score < self.fidelity_threshold:
            return False, f"Fidelity {result.fidelity_score:.3f} below threshold {self.fidelity_threshold}."
        if result.knowledge_retention < self.retention_threshold:
            return False, f"Retention {result.knowledge_retention:.3f} below threshold {self.retention_threshold}."
        return True, "Distillation accepted."

    # ---- Internal ----

    def _compute_fidelity(
        self,
        teacher: Dict[str, List[float]],
        student: Dict[str, List[float]],
    ) -> float:
        """计算教师-学生输出一致性（平均余弦相似度）。"""
        common = set(teacher.keys()) & set(student.keys())
        if not common:
            return 0.0

        similarities = [
            self._cosine_similarity(teacher[dim], student[dim])
            for dim in common
        ]
        return float(np.mean(similarities))

    def _compute_diversity_preservation(
        self,
        teacher_dist: Optional[Dict[str, float]],
        student_dist: Optional[Dict[str, float]],
    ) -> float:
        """计算教师多样性的保留程度。"""
        if not teacher_dist or not student_dist:
            return 1.0  # 无分布信息，假定保留完好

        # 计算双方熵
        def _entropy(d: Dict[str, float]) -> float:
            total = sum(d.values())
            if total <= 0:
                return 0.0
            probs = [v / total for v in d.values()]
            return -sum(p * math.log(max(p, 1e-12)) for p in probs)

        H_teacher = _entropy(teacher_dist)
        H_student = _entropy(student_dist)

        if H_teacher == 0:
            return 1.0  # 教师本身无多样性

        return min(1.0, H_student / H_teacher)

    def _classify_quality(
        self,
        fidelity: float,
        retention: float,
        diversity_preservation: float,
    ) -> DistillationQuality:
        """综合判定蒸馏质量等级。"""
        avg = (fidelity + retention + diversity_preservation) / 3

        if avg >= 0.95:
            return DistillationQuality.EXCELLENT
        elif avg >= 0.85:
            return DistillationQuality.GOOD
        elif avg >= 0.70:
            return DistillationQuality.ADEQUATE
        elif avg >= 0.50:
            return DistillationQuality.DEGRADED
        else:
            return DistillationQuality.CORRUPTED

    def _generate_recommendations(
        self,
        quality: DistillationQuality,
        degraded_dims: List[str],
        fidelity: float,
        retention: float,
        diversity_preservation: float,
    ) -> List[str]:
        recs = []

        if quality == DistillationQuality.CORRUPTED:
            recs.append("REJECT distillation — restart with higher temperature.")
            recs.append("Verify teacher model integrity before retry.")
        elif quality == DistillationQuality.DEGRADED:
            recs.append("Review degraded dimensions before deployment.")
            if degraded_dims:
                recs.append(f"Focus retraining on: {', '.join(degraded_dims[:5])}.")
            recs.append("Consider increasing distillation temperature.")
        elif quality == DistillationQuality.ADEQUATE:
            if fidelity < self.fidelity_threshold:
                recs.append("Fidelity below target — increase training iterations.")
            if degraded_dims:
                recs.append(f"Minor degradation in: {', '.join(degraded_dims[:3])}.")
        elif quality == DistillationQuality.GOOD:
            recs.append("Distillation quality acceptable.")
        elif quality == DistillationQuality.EXCELLENT:
            recs.append("Excellent distillation — proceed with deployment.")

        if diversity_preservation < self.diversity_preservation_threshold:
            recs.append(
                f"Diversity loss detected (preservation={diversity_preservation:.3f}). "
                "Apply SDPO diversity penalty in next distillation round."
            )

        return recs

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def summary(self) -> Dict[str, Any]:
        return {
            "total_audits": self.total_audits,
            "failed_audits": self.failed_audits,
            "latest_quality": (
                self.audit_results[-1].quality.value
                if self.audit_results else "none"
            ),
            "audit_history": [
                {
                    "id": r.audit_id,
                    "quality": r.quality.value,
                    "fidelity": round(r.fidelity_score, 3),
                    "retention": round(r.knowledge_retention, 3),
                }
                for r in self.audit_results[-5:]  # 最近 5 次
            ],
        }


# ============================================================================
# Anti-Forgetting Guard — Layer 6a 主入口
# ============================================================================


class AntiForgettingGuard:
    """
    AntiForgettingGuard — SDPO 抗遗忘防御层 (Layer 6a)

    并联接入 auto_daemon 防御链：
      5a → 5b → 5c → PRE_GATE → 5 → [6a ANTI_FORGETTING] → 6

    三个组件并行运行，任一触发严重告警时可独立阻断。
    """

    def __init__(
        self,
        forgetting_monitor: Optional[ForgettingMonitor] = None,
        diversity_guard: Optional[ExplorationDiversityGuard] = None,
        distillation_auditor: Optional[KnowledgeDistillationAuditor] = None,
        blocking_alert_level: ForgettingAlert = ForgettingAlert.SEVERE,
    ):
        self.forgetting_monitor = forgetting_monitor or ForgettingMonitor()
        self.diversity_guard = diversity_guard or ExplorationDiversityGuard()
        self.distillation_auditor = distillation_auditor or KnowledgeDistillationAuditor()
        self.blocking_alert_level = blocking_alert_level

        # 阻断统计
        self.total_checks: int = 0
        self.blocks_issued: int = 0
        self.block_reasons: List[Dict[str, Any]] = []

    # ---- Core API ----

    def snapshot_and_check(
        self,
        skill_signatures: Dict[str, float],
        strategy_distribution: Optional[Dict[str, float]] = None,
        knowledge_vectors: Optional[Dict[str, List[float]]] = None,
    ) -> Dict[str, Any]:
        """
        采集快照并执行完整检查（三组件并联）。

        返回：
          {
            "proceed": bool,     # 是否允许继续
            "alerts": [...],     # 告警列表
            "diversity": {...},  # 多样性报告
          }
        """
        self.total_checks += 1

        # 1. 采集快照
        snapshot = self.forgetting_monitor.capture_snapshot(
            skill_signatures=skill_signatures,
            strategy_distribution=strategy_distribution,
            knowledge_vectors=knowledge_vectors,
        )

        # 2. 遗忘检测
        forgetting_event = self.forgetting_monitor.detect_forgetting()

        # 3. 多样性评估
        if strategy_distribution:
            self.diversity_guard.observe_strategy(strategy_distribution)
        diversity_report = self.diversity_guard.assess_diversity()

        # 4. 综合阻断判断
        should_block = False
        block_reasons = []

        if forgetting_event and self._alert_blocks(forgetting_event.alert_level):
            should_block = True
            block_reasons.append({
                "source": "ForgettingMonitor",
                "alert": forgetting_event.alert_level.value,
                "detail": forgetting_event.diagnosis,
            })

        if diversity_report.status in (DiversityStatus.COLLAPSING, DiversityStatus.COLLAPSED):
            should_block = True
            block_reasons.append({
                "source": "ExplorationDiversityGuard",
                "status": diversity_report.status.value,
                "entropy": round(diversity_report.strategy_entropy, 4),
            })

        if should_block:
            self.blocks_issued += 1
            self.block_reasons.extend(block_reasons)

        return {
            "proceed": not should_block,
            "snapshot_id": snapshot.snapshot_id,
            "forgetting_event": (
                {
                    "alert": forgetting_event.alert_level.value,
                    "bwt": forgetting_event.forgetting_score,
                    "affected_skills": forgetting_event.affected_skills,
                }
                if forgetting_event else None
            ),
            "diversity": {
                "status": diversity_report.status.value,
                "entropy": round(diversity_report.strategy_entropy, 4),
                "collapse_risk": round(diversity_report.collapse_risk, 4),
                "exploration_rate": round(self.diversity_guard.current_exploration_rate, 4),
            },
            "blocks": block_reasons,
        }

    def audit_knowledge_transfer(
        self,
        teacher_outputs: Dict[str, List[float]],
        student_outputs: Dict[str, List[float]],
        teacher_strategy: Optional[Dict[str, float]] = None,
        student_strategy: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """
        审计知识蒸馏/迁移过程。
        """
        result = self.distillation_auditor.audit_distillation(
            teacher_outputs=teacher_outputs,
            student_outputs=student_outputs,
            teacher_strategy_dist=teacher_strategy,
            student_strategy_dist=student_strategy,
        )

        accepted, reason = self.distillation_auditor.should_accept_distillation(result)

        return {
            "audit_id": result.audit_id,
            "quality": result.quality.value,
            "accepted": accepted,
            "reason": reason,
            "fidelity": round(result.fidelity_score, 4),
            "retention": round(result.knowledge_retention, 4),
            "diversity_preservation": round(result.diversity_preservation, 4),
            "degraded_dimensions": result.degraded_dimensions[:10],
            "recommendations": result.recommendations,
        }

    def get_sdp_parameters(self) -> Dict[str, Any]:
        """获取 SDPO steerable parameters 当前值。"""
        return {
            "diversity_lambda": self.diversity_guard.diversity_lambda,
            "exploration_rate": self.diversity_guard.current_exploration_rate,
            "min_exploration": self.diversity_guard.min_exploration_rate,
            "max_exploration": self.diversity_guard.max_exploration_rate,
            "blocking_threshold": self.blocking_alert_level.value,
        }

    def set_sdp_parameters(
        self,
        diversity_lambda: Optional[float] = None,
        exploration_rate: Optional[float] = None,
        blocking_level: Optional[ForgettingAlert] = None,
    ):
        """动态调整 SDPO 参数（steerable diversity）。"""
        if diversity_lambda is not None:
            self.diversity_guard.diversity_lambda = diversity_lambda
        if exploration_rate is not None:
            self.diversity_guard.current_exploration_rate = exploration_rate
        if blocking_level is not None:
            self.blocking_alert_level = blocking_level

    # ---- Internal ----

    def _alert_blocks(self, level: ForgettingAlert) -> bool:
        """判断该级别是否应触发阻断。"""
        level_order = [
            ForgettingAlert.NONE,
            ForgettingAlert.MILD,
            ForgettingAlert.MODERATE,
            ForgettingAlert.SEVERE,
            ForgettingAlert.CATASTROPHIC,
        ]
        try:
            return level_order.index(level) >= level_order.index(self.blocking_alert_level)
        except ValueError:
            return False

    def summary(self) -> Dict[str, Any]:
        return {
            "total_checks": self.total_checks,
            "blocks_issued": self.blocks_issued,
            "block_rate": (
                self.blocks_issued / max(self.total_checks, 1)
            ),
            "forgetting_monitor": self.forgetting_monitor.summary(),
            "diversity_guard": self.diversity_guard.summary(),
            "distillation_auditor": self.distillation_auditor.summary(),
        }


# ============================================================================
# Demo & Self-Test
# ============================================================================


def demo():
    """Self-contained demo validating all three components and the main guard."""
    print("=" * 60)
    print("Anti-Forgetting Guard (Layer 6a) — Self-Test")
    print("Based on SDPO (arXiv 2607.01763)")
    print("=" * 60)

    results = []
    test_id = 0
    np.random.seed(42)

    # --- Test 1: ForgettingMonitor — snapshot & detect ---
    test_id += 1
    print(f"\n[Test {test_id}] ForgettingMonitor: snapshot + BWT-based detection")
    fm = ForgettingMonitor(snapshot_interval_seconds=60.0)

    # Baseline snapshot: all skills at 0.85
    baseline_skills = {f"skill_{i}": 0.85 for i in range(10)}
    fm.capture_snapshot(baseline_skills, task_context="baseline")

    # Degraded snapshot: 4 skills dropped to 0.40
    degraded_skills = {f"skill_{i}": (0.40 if i < 4 else 0.84) for i in range(10)}
    fm.capture_snapshot(degraded_skills, task_context="after_new_task")

    event = fm.detect_forgetting()
    assert event is not None, "Should detect forgetting"
    assert event.alert_level not in (ForgettingAlert.NONE,), \
        f"Expected alert above NONE, got {event.alert_level.value}"
    assert len(event.affected_skills) >= 4
    print(f"  PASS — detected: alert={event.alert_level.value}, "
          f"affected_skills={len(event.affected_skills)}, BWT={event.forgetting_score:.4f}")
    results.append(("ForgettingMonitor", True))

    # --- Test 2: ForgettingMonitor — no forgetting case ---
    test_id += 1
    print(f"\n[Test {test_id}] ForgettingMonitor: no degradation → no alert")
    fm2 = ForgettingMonitor()
    stable = {f"skill_{i}": 0.90 for i in range(5)}
    fm2.capture_snapshot(stable)
    fm2.capture_snapshot(stable)  # same scores

    event2 = fm2.detect_forgetting()
    assert event2 is None, "Should not detect forgetting for stable scores"
    print(f"  PASS — no false alarm for stable performance")
    results.append(("ForgettingMonitor-no-alert", True))

    # --- Test 3: ExplorationDiversityGuard — healthy diversity ---
    test_id += 1
    print(f"\n[Test {test_id}] ExplorationDiversityGuard: healthy diversity")
    edg = ExplorationDiversityGuard()

    # 均匀分布 = 高熵
    healthy_dist = {f"strategy_{i}": 1.0 / 8 for i in range(8)}
    for _ in range(10):
        edg.observe_strategy(healthy_dist, selected_action=f"action_{np.random.randint(0,8)}")

    report = edg.assess_diversity()
    assert report.status == DiversityStatus.HEALTHY
    assert report.strategy_entropy > 0.6
    print(f"  PASS — status={report.status.value}, entropy={report.strategy_entropy:.4f}")
    results.append(("DiversityGuard-healthy", True))

    # --- Test 4: ExplorationDiversityGuard — strategy collapse ---
    test_id += 1
    print(f"\n[Test {test_id}] ExplorationDiversityGuard: strategy collapse detection")
    edg2 = ExplorationDiversityGuard()

    # 先健康
    for _ in range(10):
        edg2.observe_strategy(healthy_dist)

    # 然后坍缩：一个策略占 95%
    collapsed_dist = {"strategy_0": 0.95}
    for i in range(1, 8):
        collapsed_dist[f"strategy_{i}"] = 0.05 / 7
    for _ in range(15):
        edg2.observe_strategy(collapsed_dist)

    report2 = edg2.assess_diversity()
    assert report2.status in (DiversityStatus.COLLAPSING, DiversityStatus.COLLAPSED)
    assert report2.collapse_risk > 0.5
    assert len(report2.suggestions) >= 2
    print(f"  PASS — status={report2.status.value}, collapse_risk={report2.collapse_risk:.4f}, "
          f"suggestions={len(report2.suggestions)}")
    results.append(("DiversityGuard-collapse", True))

    # --- Test 5: ExplorationDiversityGuard — diversity penalty ---
    test_id += 1
    print(f"\n[Test {test_id}] ExplorationDiversityGuard: SDPO diversity penalty")
    collapsed = {"s0": 0.9, "s1": 0.05, "s2": 0.05}
    adjusted = edg2.apply_diversity_penalty(collapsed)
    # 调整后应该更均匀：高占比的降，低占比的升
    max_after = max(adjusted.values())
    assert max_after < 0.9, f"Dominant strategy should decrease, got {max_after}"
    print(f"  PASS — dominant: {max(collapsed.values()):.3f} → {max_after:.3f}")
    results.append(("DiversityGuard-penalty", True))

    # --- Test 6: KnowledgeDistillationAuditor — good distillation ---
    test_id += 1
    print(f"\n[Test {test_id}] KnowledgeDistillationAuditor: high-quality distillation")
    kda = KnowledgeDistillationAuditor()

    # 高质量蒸馏：教师和学生输出高度一致
    teacher = {f"dim_{i}": list(np.random.randn(32)) for i in range(10)}
    student = {
        f"dim_{i}": [v + np.random.normal(0, 0.02) for v in teacher[f"dim_{i}"]]
        for i in range(10)
    }
    teacher_strat = {f"s{i}": 1.0 / 5 for i in range(5)}
    student_strat = {f"s{i}": 1.0 / 5 for i in range(5)}

    audit = kda.audit_distillation(
        teacher_outputs=teacher,
        student_outputs=student,
        teacher_strategy_dist=teacher_strat,
        student_strategy_dist=student_strat,
    )
    assert audit.quality in (DistillationQuality.EXCELLENT, DistillationQuality.GOOD)
    accepted, reason = kda.should_accept_distillation(audit)
    assert accepted
    print(f"  PASS — quality={audit.quality.value}, fidelity={audit.fidelity_score:.4f}, accepted={accepted}")
    results.append(("DistillationAuditor-good", True))

    # --- Test 7: KnowledgeDistillationAuditor — degraded distillation ---
    test_id += 1
    print(f"\n[Test {test_id}] KnowledgeDistillationAuditor: degraded distillation rejection")
    # 制造退化：多数维度完全跑偏
    bad_student = dict(student)
    for i in range(7):
        bad_student[f"dim_{i}"] = list(np.random.randn(32))  # 随机向量，与教师无关

    bad_audit = kda.audit_distillation(
        teacher_outputs=teacher,
        student_outputs=bad_student,
    )
    assert bad_audit.quality in (DistillationQuality.DEGRADED, DistillationQuality.CORRUPTED)
    accepted2, reason2 = kda.should_accept_distillation(bad_audit)
    assert not accepted2
    assert len(bad_audit.degraded_dimensions) >= 7
    print(f"  PASS — quality={bad_audit.quality.value}, "
          f"degraded={len(bad_audit.degraded_dimensions)}, accepted={accepted2}")
    results.append(("DistillationAuditor-bad", True))

    # --- Test 8: AntiForgettingGuard — full pipeline ---
    test_id += 1
    print(f"\n[Test {test_id}] AntiForgettingGuard: full Layer 6a pipeline")
    guard = AntiForgettingGuard(
        blocking_alert_level=ForgettingAlert.MODERATE,
    )

    # 模拟正常状态检查
    result = guard.snapshot_and_check(
        skill_signatures={f"skill_{i}": 0.88 for i in range(8)},
        strategy_distribution={f"strat_{i}": 1.0 / 6 for i in range(6)},
    )
    assert result["proceed"] is True
    print(f"  PASS — proceed={result['proceed']}, diversity_entropy={result['diversity']['entropy']}")
    results.append(("Guard-pipeline-normal", True))

    # --- Test 9: AntiForgettingGuard — blocking scenario ---
    test_id += 1
    print(f"\n[Test {test_id}] AntiForgettingGuard: blocking on severe forgetting")
    # 先建立基线
    guard2 = AntiForgettingGuard(blocking_alert_level=ForgettingAlert.SEVERE)
    guard2.snapshot_and_check(
        skill_signatures={f"skill_{i}": 0.90 for i in range(5)},
        strategy_distribution={f"strat_{i}": 1.0 / 5 for i in range(5)},
    )
    # 严重退化
    result_block = guard2.snapshot_and_check(
        skill_signatures={f"skill_{i}": 0.30 for i in range(5)},  # massive drop
        strategy_distribution={"strat_0": 0.99},  # collapsed
    )
    assert result_block["proceed"] is False
    assert len(result_block["blocks"]) >= 1
    print(f"  PASS — proceed={result_block['proceed']}, "
          f"blocks={[b['source'] for b in result_block['blocks']]}")
    results.append(("Guard-pipeline-block", True))

    # --- Test 10: SDPO parameter steering ---
    test_id += 1
    print(f"\n[Test {test_id}] SDPO steerable parameters")
    params_before = guard.get_sdp_parameters()
    guard.set_sdp_parameters(diversity_lambda=3.0, exploration_rate=0.25)
    params_after = guard.get_sdp_parameters()
    assert params_after["diversity_lambda"] == 3.0
    assert params_after["exploration_rate"] == 0.25
    print(f"  PASS — λ: {params_before['diversity_lambda']}→{params_after['diversity_lambda']}, "
          f"ε: {params_before['exploration_rate']}→{params_after['exploration_rate']}")
    results.append(("SDPO-steering", True))

    # --- Final summary ---
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"RESULTS: {passed}/{total} tests passed")
    print(f"ForgettingMonitor:    {guard.forgetting_monitor.summary()}")
    print(f"DiversityGuard:       {guard.diversity_guard.summary()}")
    print(f"DistillationAuditor:  {guard.distillation_auditor.summary()}")
    print(f"Total checks: {guard.total_checks}, Blocks: {guard.blocks_issued}")
    print("=" * 60)

    return passed == total


if __name__ == "__main__":
    success = demo()
    exit(0 if success else 1)
