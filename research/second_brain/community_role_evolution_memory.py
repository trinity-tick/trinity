"""
# status: orphan (2026-08-15 audit, not in runtime path)
P21-8: Community Role Evolution Memory — Role Clustering + Leadership Tracking
===============================================================================

对标方案：Role Evolution in Agent Societies, Leadership Emergence (2026).

设计要点：
  - 基于行为模式向量的角色聚类（社交/任务/领导/跟随多维度）
  - 角色转换轨迹存储（时间序列 of 角色标签）
  - 领导力涌现追踪（介数中心性/影响力评分时序）
  - 角色预测器（基于历史轨迹预测未来角色）

核心组件：
  - RoleEvolutionMemory:  角色演化记忆总控
  - RoleClusterer:        行为模式角色聚类器
  - LeadershipTracker:    领导力追踪器
  - RolePredictor:        角色预测器
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class RoleLabel(Enum):
    """角色标签（社交/任务/领导/跟随 四象限）。"""
    LEADER = "leader"
    FACILITATOR = "facilitator"      # 协调者
    EXECUTOR = "executor"            # 执行者
    FOLLOWER = "follower"
    INNOVATOR = "innovator"          # 创新者
    MEDIATOR = "mediator"            # 调解者
    ISOLATE = "isolate"              # 孤立者
    BRIDGE = "bridge"                # 桥接者
    SAGE = "sage"                    # 智者/顾问
    NEWCOMER = "newcomer"            # 新成员


class BehaviorDimension(Enum):
    """行为模式维度。"""
    SOCIAL_INITIATION = "social_initiation"    # 社交发起频率
    TASK_COMPLETION = "task_completion"         # 任务完成率
    LEADERSHIP_ACTION = "leadership_action"     # 领导行为频率
    FOLLOWING_BEHAVIOR = "following_behavior"   # 跟随行为频率
    CONFLICT_RESOLUTION = "conflict_resolution"  # 冲突解决
    KNOWLEDGE_SHARING = "knowledge_sharing"     # 知识共享
    COMMUNICATION = "communication"             # 沟通频率
    INFLUENCE = "influence"                     # 影响力


class EvolutionPhase(Enum):
    """角色演化阶段。"""
    EXPLORATION = "exploration"    # 探索期
    STABILIZATION = "stabilization"  # 稳定期
    TRANSITION = "transition"      # 转换期
    CONSOLIDATION = "consolidation"  # 巩固期


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class BehaviorVector:
    """行为模式向量。"""
    agent_id: str
    timestamp: float
    dimensions: Dict[BehaviorDimension, float] = field(default_factory=dict)
    dominant_role: Optional[RoleLabel] = None

    def norm(self) -> float:
        return math.sqrt(sum(v ** 2 for v in self.dimensions.values())) or 1.0

    def cosine_similarity(self, other: BehaviorVector) -> float:
        if not self.dimensions or not other.dimensions:
            return 0.0
        all_dims = set(self.dimensions) | set(other.dimensions)
        dot = sum(self.dimensions.get(d, 0) * other.dimensions.get(d, 0) for d in all_dims)
        return dot / (self.norm() * other.norm())


@dataclass
class RoleSnapshot:
    """角色快照。"""
    snapshot_id: str
    agent_id: str
    role: RoleLabel
    confidence: float
    behavior_vector: BehaviorVector
    timestamp: float = field(default_factory=time.time)
    phase: EvolutionPhase = EvolutionPhase.EXPLORATION


@dataclass
class RoleTrajectory:
    """角色转换轨迹。"""
    agent_id: str
    snapshots: List[RoleSnapshot] = field(default_factory=list)
    role_durations: Dict[RoleLabel, float] = field(default_factory=dict)  # 累计逗留时间
    transition_matrix: Dict[Tuple[RoleLabel, RoleLabel], int] = field(default_factory=dict)

    def add_snapshot(self, snapshot: RoleSnapshot):
        self.snapshots.append(snapshot)
        if len(self.snapshots) >= 2:
            prev = self.snapshots[-2].role
            curr = self.snapshots[-1].role
            key = (prev, curr)
            self.transition_matrix[key] = self.transition_matrix.get(key, 0) + 1

    def current_role(self) -> Optional[RoleLabel]:
        return self.snapshots[-1].role if self.snapshots else None

    def role_stability(self) -> float:
        """角色稳定性：最近 5 个快照中是否一致。"""
        if len(self.snapshots) < 2:
            return 1.0
        recent = [s.role for s in self.snapshots[-5:]]
        return sum(1 for r in recent if r == recent[-1]) / len(recent)


@dataclass
class LeadershipScore:
    """领导力评分。"""
    agent_id: str
    betweenness_centrality: float = 0.0
    influence_score: float = 0.0
    follower_count: int = 0
    composite_leadership: float = 0.0
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# Constants
# ============================================================================

# 角色原型行为向量（归一化）
ROLE_PROTOTYPES: Dict[RoleLabel, Dict[BehaviorDimension, float]] = {
    RoleLabel.LEADER: {
        BehaviorDimension.LEADERSHIP_ACTION: 0.9,
        BehaviorDimension.INFLUENCE: 0.8,
        BehaviorDimension.COMMUNICATION: 0.7,
        BehaviorDimension.SOCIAL_INITIATION: 0.6,
        BehaviorDimension.TASK_COMPLETION: 0.5,
    },
    RoleLabel.FACILITATOR: {
        BehaviorDimension.COMMUNICATION: 0.9,
        BehaviorDimension.CONFLICT_RESOLUTION: 0.8,
        BehaviorDimension.SOCIAL_INITIATION: 0.7,
        BehaviorDimension.KNOWLEDGE_SHARING: 0.5,
    },
    RoleLabel.EXECUTOR: {
        BehaviorDimension.TASK_COMPLETION: 0.95,
        BehaviorDimension.FOLLOWING_BEHAVIOR: 0.4,
        BehaviorDimension.COMMUNICATION: 0.3,
    },
    RoleLabel.FOLLOWER: {
        BehaviorDimension.FOLLOWING_BEHAVIOR: 0.9,
        BehaviorDimension.SOCIAL_INITIATION: 0.2,
        BehaviorDimension.LEADERSHIP_ACTION: 0.1,
    },
    RoleLabel.INNOVATOR: {
        BehaviorDimension.KNOWLEDGE_SHARING: 0.9,
        BehaviorDimension.SOCIAL_INITIATION: 0.6,
        BehaviorDimension.TASK_COMPLETION: 0.5,
    },
    RoleLabel.MEDIATOR: {
        BehaviorDimension.CONFLICT_RESOLUTION: 0.95,
        BehaviorDimension.COMMUNICATION: 0.7,
        BehaviorDimension.SOCIAL_INITIATION: 0.4,
    },
    RoleLabel.ISOLATE: {
        BehaviorDimension.COMMUNICATION: 0.1,
        BehaviorDimension.SOCIAL_INITIATION: 0.1,
    },
    RoleLabel.BRIDGE: {
        BehaviorDimension.SOCIAL_INITIATION: 0.8,
        BehaviorDimension.COMMUNICATION: 0.7,
        BehaviorDimension.KNOWLEDGE_SHARING: 0.5,
    },
    RoleLabel.SAGE: {
        BehaviorDimension.KNOWLEDGE_SHARING: 0.9,
        BehaviorDimension.INFLUENCE: 0.7,
        BehaviorDimension.LEADERSHIP_ACTION: 0.3,
    },
    RoleLabel.NEWCOMER: {
        BehaviorDimension.SOCIAL_INITIATION: 0.3,
        BehaviorDimension.TASK_COMPLETION: 0.3,
        BehaviorDimension.COMMUNICATION: 0.3,
    },
}

STABILITY_WINDOW: int = 5     # 稳定判定窗口
TRANSITION_THRESHOLD: float = 0.3  # 角色变化余弦距离阈值


# ============================================================================
# Core Components
# ============================================================================

class RoleClusterer:
    """行为模式角色聚类器。

    基于行为向量与角色原型余弦相似度聚类。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.prototypes: Dict[RoleLabel, BehaviorVector] = {}
        for role, dims in ROLE_PROTOTYPES.items():
            self.prototypes[role] = BehaviorVector(
                agent_id="_prototype",
                timestamp=0.0,
                dimensions=dict(dims),
                dominant_role=role,
            )

    def classify(self, behavior: BehaviorVector) -> Tuple[RoleLabel, float, Dict[RoleLabel, float]]:
        """分类行为向量到最近角色。"""
        with self._lock:
            scores: Dict[RoleLabel, float] = {}
            for role, proto in self.prototypes.items():
                scores[role] = behavior.cosine_similarity(proto)

            best_role = max(scores, key=scores.get)
            confidence = scores[best_role]

            behavior.dominant_role = best_role
            return best_role, round(confidence, 4), {k: round(v, 4) for k, v in scores.items()}

    def update_prototype(self, role: RoleLabel, behavior: BehaviorVector, learning_rate: float = 0.1):
        """在线更新角色原型（EMA）。"""
        with self._lock:
            proto = self.prototypes.get(role)
            if not proto:
                return
            for dim in BehaviorDimension:
                old = proto.dimensions.get(dim, 0.0)
                new = behavior.dimensions.get(dim, 0.0)
                proto.dimensions[dim] = old * (1 - learning_rate) + new * learning_rate

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "prototype_count": len(self.prototypes),
            }


class LeadershipTracker:
    """领导力涌现追踪器。

    追踪介数中心性、影响力评分、跟随者数量的时序变化。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.scores: Dict[str, List[LeadershipScore]] = defaultdict(list)
        self.relationship_graph: Dict[str, Set[str]] = defaultdict(set)

    def update_relationship(self, influencer: str, influenced: str):
        """更新影响力关系。"""
        with self._lock:
            self.relationship_graph[influencer].add(influenced)

    def compute(self, agent_id: str) -> LeadershipScore:
        """计算领导力评分。"""
        with self._lock:
            # 介数中心性：该节点出现在多少对之间
            all_agents = list(self.relationship_graph.keys())
            betweenness = 0.0
            if len(all_agents) > 2:
                for a in all_agents:
                    for b in all_agents:
                        if a == b or a == agent_id or b == agent_id:
                            continue
                        # 简化：如果 a 影响 agent_id 且 agent_id 影响 b
                        if (agent_id in self.relationship_graph.get(a, set())
                                and b in self.relationship_graph.get(agent_id, set())):
                            betweenness += 1
                betweenness /= max((len(all_agents) - 1) * (len(all_agents) - 2), 1)

            # 跟随者计数
            followers = self.relationship_graph.get(agent_id, set())

            # 影响力评分
            influence = min(len(followers) / max(len(all_agents), 1), 1.0)

            score = LeadershipScore(
                agent_id=agent_id,
                betweenness_centrality=round(betweenness, 4),
                influence_score=round(influence, 4),
                follower_count=len(followers),
                composite_leadership=round(betweenness * 0.4 + influence * 0.6, 4),
            )
            self.scores[agent_id].append(score)
            return score

    def leadership_trend(self, agent_id: str, window: int = 5) -> List[float]:
        """领导力趋势（最近 window 个时间点）。"""
        history = self.scores.get(agent_id, [])
        if not history:
            return []
        return [s.composite_leadership for s in history[-window:]]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            all_scores = [s for scores in self.scores.values() for s in scores]
            return {
                "tracked_agents": len(self.scores),
                "total_measurements": len(all_scores),
                "avg_leadership": round(
                    sum(s.composite_leadership for s in all_scores) /
                    max(len(all_scores), 1), 4),
            }


class RolePredictor:
    """角色预测器。

    基于角色转换矩阵和历史轨迹预测未来角色。
    """

    def __init__(self):
        self._lock = threading.RLock()

    def predict(self, trajectory: RoleTrajectory, horizon: int = 1) -> List[Tuple[RoleLabel, float]]:
        """预测未来角色。"""
        with self._lock:
            if not trajectory.snapshots:
                return [(RoleLabel.NEWCOMER, 0.5)]

            current_role = trajectory.current_role()

            # 基于转换矩阵
            transitions: Dict[RoleLabel, int] = defaultdict(int)
            for (fr, to), count in trajectory.transition_matrix.items():
                if fr == current_role:
                    transitions[to] += count

            total = sum(transitions.values()) or 1
            predictions = [(role, count / total) for role, count in transitions.items()]
            predictions.sort(key=lambda x: x[1], reverse=True)

            # 如果无历史转换，预测保持当前角色
            if not predictions:
                stability = trajectory.role_stability()
                predictions = [(current_role, stability)]

            # 为罕见角色添加平滑
            smoothed: Dict[RoleLabel, float] = defaultdict(float)
            for role, prob in predictions:
                smoothed[role] = prob
            # 给其他角色微小概率
            other_mass = 0.05 / max(len(RoleLabel) - len(smoothed), 1)
            for role in RoleLabel:
                if role not in smoothed:
                    smoothed[role] = other_mass

            result = sorted(smoothed.items(), key=lambda x: x[1], reverse=True)
            # 归一化
            total_smoothed = sum(p for _, p in result)
            return [(r, round(p / total_smoothed, 4)) for r, p in result[:5]]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "model": "transition-matrix-based",
                "smoothing": 0.05,
            }


class RoleEvolutionMemory:
    """社区角色演化记忆总控。

    整合行为向量采集、角色聚类、领导力追踪、角色预测。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.clusterer = RoleClusterer()
        self.leadership = LeadershipTracker()
        self.predictor = RolePredictor()
        self.trajectories: Dict[str, RoleTrajectory] = {}
        self.behavior_history: Dict[str, deque[BehaviorVector]] = defaultdict(lambda: deque(maxlen=100))

    def record_behavior(self, agent_id: str, behavior: BehaviorVector) -> RoleSnapshot:
        """记录行为向量并分类角色。"""
        with self._lock:
            # 分类
            role, confidence, _ = self.clusterer.classify(behavior)

            # 快照
            snapshot = RoleSnapshot(
                snapshot_id=str(uuid.uuid4())[:8],
                agent_id=agent_id,
                role=role,
                confidence=confidence,
                behavior_vector=behavior,
            )

            # 判断演化阶段
            traj = self.trajectories.get(agent_id, RoleTrajectory(agent_id=agent_id))
            if len(traj.snapshots) < STABILITY_WINDOW:
                snapshot.phase = EvolutionPhase.EXPLORATION
            elif traj.role_stability() > 0.8:
                snapshot.phase = EvolutionPhase.CONSOLIDATION
            else:
                snapshot.phase = EvolutionPhase.TRANSITION

            traj.add_snapshot(snapshot)
            self.trajectories[agent_id] = traj
            self.behavior_history[agent_id].append(behavior)

            # 更新原型
            self.clusterer.update_prototype(role, behavior, learning_rate=0.05)

            return snapshot

    def get_role(self, agent_id: str) -> Optional[RoleLabel]:
        """获取当前角色。"""
        traj = self.trajectories.get(agent_id)
        return traj.current_role() if traj else None

    def get_phase(self, agent_id: str) -> Optional[EvolutionPhase]:
        """获取当前演化阶段。"""
        traj = self.trajectories.get(agent_id)
        return traj.snapshots[-1].phase if traj and traj.snapshots else None

    def get_role_history(self, agent_id: str) -> List[Dict[str, Any]]:
        """获取角色历史轨迹。"""
        traj = self.trajectories.get(agent_id)
        if not traj:
            return []
        return [
            {
                "role": s.role.value,
                "confidence": s.confidence,
                "phase": s.phase.value,
                "timestamp": s.timestamp,
            }
            for s in traj.snapshots[-20:]
        ]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            role_counts = defaultdict(int)
            for traj in self.trajectories.values():
                role = traj.current_role()
                if role:
                    role_counts[role.value] += 1
            return {
                "total_agents": len(self.trajectories),
                "role_distribution": dict(role_counts),
                "clusterer": self.clusterer.statistics(),
                "leadership": self.leadership.statistics(),
                "predictor": self.predictor.statistics(),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P21-8 Community Role Evolution Memory",
        "benchmark": "Role Clustering + Leadership Emergence + Role Transition Prediction (2026)",
        "classes": 4,
        "enums": 3,
        "dataclasses": 4,
        "key_pattern": "BehaviorVector→CosineClustering(10 roles)→RoleTrajectory→LeaderTracking→RolePredict",
        "key_metric": "10 role prototypes (Leader/Sage/Bridge/...), betweenness+influence leadership, transition-matrix predictor",
        "thread_safe": True,
    }
