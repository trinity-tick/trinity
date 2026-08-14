"""
P20-1: Self-Evolving Skill Memory — 自进化技能记忆

对标论文: SkeMex (arXiv 2606.09365, 2026.06)
核心发现: 轨迹蒸馏 + 三级分支仓库 + 效用驱动自进化 → 技能库无需更新模型权重即可持续演进
三元语: 轨迹→技能蒸馏 → 三级分支存储 → 效用估算 → 选择性保留 → 部署后闭环进化

设计要点:
- TrajectorySkillDistiller: 将原始交互轨迹 (thought/action/observation) 压缩为结构化可重用技能
- MultiBranchSkillRepository: general / task_specific / action_level 三级分支仓库，逐级粒化
- ContextUtilityEstimator: 基于环境反馈估算每个技能对当前上下文的效用值 (0~1)
- SelectiveRetentionGate: 基于效用值 + 访问频次决定保留/遗忘，避免原始轨迹冗余噪声
- PostDeploymentEvolutionLoop: 部署后不更新模型权重即可实现技能库自我演进闭环
- CrossTaskSkillTransfer: 识别相似任务间的可迁移技能组件，复用蒸馏成果
- 与 P13-2 skill_learning.py 互补——skill_learning 从交互学技能，本模块做轨迹蒸馏+三级分支+效用驱动自进化
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
# Enums & Constants
# ============================================================================

class SkillBranch(Enum):
    """技能分支等级 — 从通用到动作级逐级粒化"""
    GENERAL = "general"              # 通用技能 (跨域可复用)
    TASK_SPECIFIC = "task_specific"  # 任务级技能 (特定任务族)
    ACTION_LEVEL = "action_level"    # 动作级技能 (原子操作序列)


class SkillState(Enum):
    """技能生命状态"""
    ACTIVE = "active"                # 活跃可用
    DORMANT = "dormant"              # 休眠 (低频访问)
    DEPRECATED = "deprecated"        # 废弃 (效用过低)
    EVOLVING = "evolving"            # 进化中 (正在蒸馏合并)


class RetentionDecision(Enum):
    """保留决策"""
    KEEP = auto()                     # 保留
    MERGE = auto()                    # 合并到更高层技能
    PRUNE = auto()                    # 遗忘/剪枝
    ARCHIVE = auto()                  # 归档到冷存储


class EvolutionTrigger(Enum):
    """进化触发条件"""
    UTILITY_DECAY = "utility_decay"           # 效用衰减超阈值
    NEW_TRAJECTORY = "new_trajectory"          # 新轨迹到达
    PERIODIC_CONSOLIDATION = "periodic"       # 周期性固结
    CROSS_TASK_SIGNAL = "cross_task_signal"   # 跨任务迁移信号


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class TrajectoryStep:
    """单步交互轨迹记录"""
    step_id: str
    thought: str                      # 推理/思考
    action: str                       # 执行动作
    observation: str                  # 环境观测反馈
    reward: float = 0.0              # 即时奖励
    timestamp: float = field(default_factory=time.time)


@dataclass
class DistilledSkill:
    """蒸馏后的结构化技能"""
    skill_id: str
    name: str
    description: str
    branch: SkillBranch
    preconditions: List[str] = field(default_factory=list)    # 前置条件
    action_sequence: List[str] = field(default_factory=list)  # 动作序列
    postconditions: List[str] = field(default_factory=list)   # 后置条件
    success_rate: float = 0.0        # 历史成功率
    usage_count: int = 0             # 使用次数
    utility_score: float = 0.0       # 当前效用值 [0, 1]
    parent_skill_ids: List[str] = field(default_factory=list)  # 父技能 (合并来源)
    source_trajectory_ids: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    state: SkillState = SkillState.ACTIVE
    embedding: Optional[List[float]] = None  # 技能语义嵌入


@dataclass
class UtilityEstimate:
    """效用估算结果"""
    skill_id: str
    context_relevance: float          # 上下文相关性 [0, 1]
    recency_bonus: float              # 最近使用加成 [0, 1]
    success_quality: float            # 成功质量因子 [0, 1]
    utility: float                    # 综合效用 = w1*relevance + w2*recency + w3*quality
    decay_rate: float                 # 衰减速率 λ
    estimated_half_life: float        # 估计半衰期 (秒)


@dataclass
class EvolutionRecord:
    """进化记录"""
    record_id: str
    trigger: EvolutionTrigger
    skills_before: int                # 进化前技能数
    skills_after: int                 # 进化后技能数
    merged_count: int                 # 合并数
    pruned_count: int                 # 剪枝数
    new_count: int                    # 新增数
    timestamp: float = field(default_factory=time.time)


@dataclass
class TransferMapping:
    """跨任务迁移映射"""
    source_task: str
    target_task: str
    similarity_score: float           # 任务相似度 [0, 1]
    transferable_skills: List[str]    # 可迁移技能 ID 列表
    adaptation_cost: float            # 适配成本估计


# ============================================================================
# TrajectorySkillDistiller
# ============================================================================

class TrajectorySkillDistiller:
    """轨迹→技能蒸馏器

    将原始交互轨迹 (thought/action/observation) 压缩为结构化可重用技能。
    通过识别重复模式、抽象前置/后置条件，将低层轨迹转化为高层技能描述。
    """

    def __init__(
        self,
        min_trajectory_length: int = 3,
        pattern_similarity_threshold: float = 0.7,
        max_skill_per_trajectory: int = 10,
    ):
        self.min_trajectory_length = min_trajectory_length
        self.pattern_similarity_threshold = pattern_similarity_threshold
        self.max_skill_per_trajectory = max_skill_per_trajectory
        self._lock = threading.RLock()
        self._trajectory_buffer: List[TrajectoryStep] = []
        self._distillation_count: int = 0

    def ingest_step(self, step: TrajectoryStep) -> None:
        """摄入单步轨迹"""
        with self._lock:
            self._trajectory_buffer.append(step)

    def distill(self) -> List[DistilledSkill]:
        """执行轨迹蒸馏，提取结构化技能"""
        with self._lock:
            skills: List[DistilledSkill] = []
            buffer_len = len(self._trajectory_buffer)
            if buffer_len < self.min_trajectory_length:
                return skills

            # 滑窗分段提取模式
            window = min(buffer_len, 20)
            for i in range(0, buffer_len - self.min_trajectory_length + 1,
                           max(1, buffer_len // self.max_skill_per_trajectory)):
                segment = self._trajectory_buffer[i:i + window]
                if len(segment) < self.min_trajectory_length:
                    continue

                # 提取动作序列与条件
                actions = [s.action for s in segment]
                preconditions = self._extract_preconditions(segment)
                postconditions = self._extract_postconditions(segment)

                skill_id = hashlib.sha256(
                    f"distill:{self._distillation_count}:{actions[0]}".encode()
                ).hexdigest()[:16]

                skill = DistilledSkill(
                    skill_id=skill_id,
                    name=f"skill_{self._distillation_count}",
                    description=f"蒸馏技能 #{self._distillation_count}: {actions[0][:40]}...",
                    branch=self._classify_branch(segment),
                    preconditions=preconditions,
                    action_sequence=actions,
                    postconditions=postconditions,
                    source_trajectory_ids=[s.step_id for s in segment],
                )
                skills.append(skill)
                self._distillation_count += 1

            # 蒸馏后清空缓冲
            self._trajectory_buffer.clear()
            return skills

    def _extract_preconditions(self, segment: List[TrajectoryStep]) -> List[str]:
        """从轨迹段提取前置条件"""
        conds = []
        for s in segment[:2]:
            if s.observation:
                conds.append(f"state:{s.observation[:60]}")
        return conds[:3]

    def _extract_postconditions(self, segment: List[TrajectoryStep]) -> List[str]:
        """从轨迹段提取后置条件"""
        conds = []
        for s in segment[-2:]:
            if s.observation:
                conds.append(f"result:{s.observation[:60]}")
        return conds[:3]

    def _classify_branch(self, segment: List[TrajectoryStep]) -> SkillBranch:
        """根据轨迹段粒度分类到三级分支"""
        unique_actions = len(set(s.action for s in segment))
        span = len(segment)
        if span <= 3 and unique_actions <= 3:
            return SkillBranch.ACTION_LEVEL
        elif span <= 10:
            return SkillBranch.TASK_SPECIFIC
        else:
            return SkillBranch.GENERAL

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "distillation_count": self._distillation_count,
                "buffer_size": len(self._trajectory_buffer),
                "min_trajectory_length": self.min_trajectory_length,
                "pattern_threshold": self.pattern_similarity_threshold,
            }


# ============================================================================
# MultiBranchSkillRepository
# ============================================================================

class MultiBranchSkillRepository:
    """三级分支技能仓库

    分层存储技能:
    - general: 通用跨域技能，最少、最稳定
    - task_specific: 任务族技能，中等粒度
    - action_level: 原子动作序列，最多、最细粒度
    """

    def __init__(self, max_general: int = 100, max_task: int = 500, max_action: int = 2000):
        self.max_general = max_general
        self.max_task = max_task
        self.max_action = max_action
        self._lock = threading.RLock()
        self._general: OrderedDict[str, DistilledSkill] = OrderedDict()
        self._task_specific: OrderedDict[str, DistilledSkill] = OrderedDict()
        self._action_level: OrderedDict[str, DistilledSkill] = OrderedDict()
        self._index: Dict[str, str] = {}  # skill_id -> branch name

    def add(self, skill: DistilledSkill) -> bool:
        """添加技能到对应分支"""
        with self._lock:
            branch_map = {
                SkillBranch.GENERAL: (self._general, self.max_general),
                SkillBranch.TASK_SPECIFIC: (self._task_specific, self.max_task),
                SkillBranch.ACTION_LEVEL: (self._action_level, self.max_action),
            }
            store, cap = branch_map[skill.branch]
            if len(store) >= cap:
                # LRU 淘汰最低效用技能
                lowest = min(store.values(), key=lambda s: s.utility_score)
                del store[lowest.skill_id]
                self._index.pop(lowest.skill_id, None)
            store[skill.skill_id] = skill
            self._index[skill.skill_id] = skill.branch.value
            return True

    def get(self, skill_id: str) -> Optional[DistilledSkill]:
        """按 ID 检索技能"""
        with self._lock:
            branch_name = self._index.get(skill_id)
            if branch_name is None:
                return None
            branch = {
                "general": self._general,
                "task_specific": self._task_specific,
                "action_level": self._action_level,
            }.get(branch_name)
            return branch.get(skill_id) if branch else None

    def query_by_branch(self, branch: SkillBranch) -> List[DistilledSkill]:
        """按分支查询所有技能"""
        with self._lock:
            branch_map = {
                SkillBranch.GENERAL: self._general,
                SkillBranch.TASK_SPECIFIC: self._task_specific,
                SkillBranch.ACTION_LEVEL: self._action_level,
            }
            return list(branch_map[branch].values())

    def remove(self, skill_id: str) -> bool:
        """移除技能"""
        with self._lock:
            branch_name = self._index.pop(skill_id, None)
            if branch_name is None:
                return False
            branch = {
                "general": self._general,
                "task_specific": self._task_specific,
                "action_level": self._action_level,
            }.get(branch_name)
            if branch and skill_id in branch:
                del branch[skill_id]
                return True
            return False

    def total_skills(self) -> int:
        with self._lock:
            return len(self._index)

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "total_skills": len(self._index),
                "general_count": len(self._general),
                "task_specific_count": len(self._task_specific),
                "action_level_count": len(self._action_level),
                "max_general": self.max_general,
                "max_task": self.max_task,
                "max_action": self.max_action,
            }


# ============================================================================
# ContextUtilityEstimator
# ============================================================================

class ContextUtilityEstimator:
    """环境反馈效用估算器

    基于环境反馈估算每个技能对当前上下文的效用值。
    使用三因子加权模型: 上下文相关性 + 最近使用加成 + 成功质量。
    """

    def __init__(
        self,
        w_relevance: float = 0.5,
        w_recency: float = 0.2,
        w_quality: float = 0.3,
        decay_lambda: float = 0.001,
    ):
        self.w_relevance = w_relevance
        self.w_recency = w_recency
        self.w_quality = w_quality
        self.decay_lambda = decay_lambda
        self._lock = threading.RLock()
        self._estimate_cache: Dict[str, UtilityEstimate] = {}
        self._evaluation_count: int = 0

    def estimate(
        self,
        skill: DistilledSkill,
        context_vector: Optional[List[float]] = None,
    ) -> UtilityEstimate:
        """估算技能对当前上下文的效用"""
        with self._lock:
            # 上下文相关性 (简化为基于最近使用和嵌入相似度)
            context_relevance = self._compute_context_relevance(skill, context_vector)

            # 最近使用加成
            elapsed = max(0, time.time() - skill.last_used_at)
            recency_bonus = math.exp(-self.decay_lambda * elapsed)

            # 成功质量因子
            success_quality = skill.success_rate if skill.success_rate > 0 else 0.5

            # 综合效用
            utility = (
                self.w_relevance * context_relevance
                + self.w_recency * recency_bonus
                + self.w_quality * success_quality
            )
            utility = max(0.0, min(1.0, utility))

            estimate = UtilityEstimate(
                skill_id=skill.skill_id,
                context_relevance=context_relevance,
                recency_bonus=recency_bonus,
                success_quality=success_quality,
                utility=utility,
                decay_rate=self.decay_lambda,
                estimated_half_life=math.log(2) / max(self.decay_lambda, 1e-9),
            )
            self._estimate_cache[skill.skill_id] = estimate
            self._evaluation_count += 1
            return estimate

    def _compute_context_relevance(
        self,
        skill: DistilledSkill,
        context_vector: Optional[List[float]],
    ) -> float:
        """计算上下文相关性 (余弦相似度简化版)"""
        if context_vector is None or skill.embedding is None:
            return 0.5  # 无上下文时默认中等相关
        # 简化余弦相似度
        dot = sum(a * b for a, b in zip(context_vector, skill.embedding[:len(context_vector)]))
        norm_a = math.sqrt(sum(a * a for a in context_vector))
        norm_b = math.sqrt(sum(b * b for b in skill.embedding[:len(context_vector)]))
        if norm_a == 0 or norm_b == 0:
            return 0.5
        return max(0.0, min(1.0, (dot / (norm_a * norm_b) + 1) / 2))

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "evaluation_count": self._evaluation_count,
                "cached_estimates": len(self._estimate_cache),
                "weights": {
                    "relevance": self.w_relevance,
                    "recency": self.w_recency,
                    "quality": self.w_quality,
                },
                "decay_lambda": self.decay_lambda,
            }


# ============================================================================
# SelectiveRetentionGate
# ============================================================================

class SelectiveRetentionGate:
    """选择性保留门

    基于效用值 + 访问频次决定保留/遗忘哪些技能。
    避免原始轨迹冗余噪声污染技能库。
    """

    def __init__(
        self,
        retention_threshold: float = 0.15,
        merge_threshold: float = 0.35,
        min_access_count: int = 2,
    ):
        self.retention_threshold = retention_threshold
        self.merge_threshold = merge_threshold
        self.min_access_count = min_access_count
        self._lock = threading.RLock()
        self._decision_history: List[Tuple[str, RetentionDecision, float]] = []
        self._prune_count: int = 0
        self._merge_count: int = 0

    def decide(
        self,
        skill: DistilledSkill,
        utility: float,
    ) -> RetentionDecision:
        """决定技能的保留/合并/遗忘"""
        with self._lock:
            if utility >= self.merge_threshold:
                decision = RetentionDecision.KEEP
            elif utility >= self.retention_threshold:
                decision = RetentionDecision.MERGE
            elif skill.usage_count < self.min_access_count:
                decision = RetentionDecision.PRUNE
            else:
                decision = RetentionDecision.ARCHIVE

            self._decision_history.append((skill.skill_id, decision, utility))
            if decision == RetentionDecision.PRUNE:
                self._prune_count += 1
            elif decision == RetentionDecision.MERGE:
                self._merge_count += 1
            return decision

    def filter_skills(
        self,
        skills: List[DistilledSkill],
        utility_map: Dict[str, float],
    ) -> Tuple[List[DistilledSkill], List[DistilledSkill]]:
        """批量过滤技能，返回 (保留, 移除)"""
        with self._lock:
            keep: List[DistilledSkill] = []
            remove: List[DistilledSkill] = []
            for skill in skills:
                u = utility_map.get(skill.skill_id, 0.0)
                decision = self.decide(skill, u)
                if decision in (RetentionDecision.KEEP, RetentionDecision.MERGE):
                    keep.append(skill)
                else:
                    remove.append(skill)
            return keep, remove

    @property
    def threshold(self) -> float:
        return self.retention_threshold

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "prune_count": self._prune_count,
                "merge_count": self._merge_count,
                "total_decisions": len(self._decision_history),
                "retention_threshold": self.retention_threshold,
                "merge_threshold": self.merge_threshold,
            }


# ============================================================================
# PostDeploymentEvolutionLoop
# ============================================================================

class PostDeploymentEvolutionLoop:
    """部署后自进化闭环

    不更新模型权重即可实现技能库自我演进。
    周期性运行: 效用评估 → 选择性保留 → 技能合并 → 跨任务迁移。
    """

    def __init__(
        self,
        repository: MultiBranchSkillRepository,
        utility_estimator: ContextUtilityEstimator,
        retention_gate: SelectiveRetentionGate,
        evolution_interval: float = 3600.0,  # 默认每小时一次
    ):
        self.repository = repository
        self.utility_estimator = utility_estimator
        self.retention_gate = retention_gate
        self.evolution_interval = evolution_interval
        self._lock = threading.RLock()
        self._last_evolution_at: float = 0.0
        self._evolution_history: List[EvolutionRecord] = []
        self._generation: int = 0

    def should_evolve(self) -> bool:
        """判断是否应触发进化"""
        return (time.time() - self._last_evolution_at) >= self.evolution_interval

    def evolve(
        self,
        trigger: EvolutionTrigger = EvolutionTrigger.PERIODIC_CONSOLIDATION,
        context_vector: Optional[List[float]] = None,
    ) -> EvolutionRecord:
        """执行一轮进化"""
        with self._lock:
            before_count = self.repository.total_skills()
            pruned = 0
            merged = 0

            # 遍历所有分支评估效用
            for branch in (SkillBranch.GENERAL, SkillBranch.TASK_SPECIFIC, SkillBranch.ACTION_LEVEL):
                skills = self.repository.query_by_branch(branch)
                for skill in skills:
                    estimate = self.utility_estimator.estimate(skill, context_vector)
                    skill.utility_score = estimate.utility
                    decision = self.retention_gate.decide(skill, estimate.utility)
                    if decision == RetentionDecision.PRUNE:
                        self.repository.remove(skill.skill_id)
                        pruned += 1
                    elif decision == RetentionDecision.MERGE:
                        # 标记为合并候选 (实际合并由上层调度)
                        skill.state = SkillState.DORMANT
                        merged += 1

            after_count = self.repository.total_skills()
            record = EvolutionRecord(
                record_id=f"evol_{self._generation}",
                trigger=trigger,
                skills_before=before_count,
                skills_after=after_count,
                merged_count=merged,
                pruned_count=pruned,
                new_count=0,
            )
            self._evolution_history.append(record)
            self._last_evolution_at = time.time()
            self._generation += 1
            return record

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "generation": self._generation,
                "evolution_interval": self.evolution_interval,
                "last_evolution_at": self._last_evolution_at,
                "total_evolution_records": len(self._evolution_history),
                "current_skill_count": self.repository.total_skills(),
            }


# ============================================================================
# CrossTaskSkillTransfer
# ============================================================================

class CrossTaskSkillTransfer:
    """跨任务技能迁移

    识别相似任务间的可迁移技能组件，复用蒸馏成果。
    通过任务签名相似度匹配实现技能复用。
    """

    def __init__(
        self,
        repository: MultiBranchSkillRepository,
        similarity_threshold: float = 0.6,
    ):
        self.repository = repository
        self.similarity_threshold = similarity_threshold
        self._lock = threading.RLock()
        self._task_signatures: Dict[str, List[str]] = defaultdict(list)  # task -> [skill_ids]
        self._transfer_history: List[TransferMapping] = []

    def register_task(self, task_name: str, skill_ids: List[str]) -> None:
        """注册任务与其关联技能"""
        with self._lock:
            self._task_signatures[task_name] = list(skill_ids)

    def find_transferable(
        self,
        source_task: str,
        target_task: str,
    ) -> Optional[TransferMapping]:
        """查找两任务间可迁移技能"""
        with self._lock:
            src_skills = self._task_signatures.get(source_task, [])
            tgt_skills = self._task_signatures.get(target_task, [])

            if not src_skills:
                return None

            # 计算 Jaccard 相似度
            src_set = set(src_skills)
            tgt_set = set(tgt_skills)
            intersection = src_set & tgt_set
            union = src_set | tgt_set
            similarity = len(intersection) / max(len(union), 1)

            if similarity < self.similarity_threshold:
                return None

            # 通用技能无条件可迁移
            general_skills = [
                s.skill_id for s in self.repository.query_by_branch(SkillBranch.GENERAL)
            ]
            transferable = list(set(intersection) | set(general_skills))

            mapping = TransferMapping(
                source_task=source_task,
                target_task=target_task,
                similarity_score=similarity,
                transferable_skills=transferable,
                adaptation_cost=1.0 - similarity,
            )
            self._transfer_history.append(mapping)
            return mapping

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "registered_tasks": len(self._task_signatures),
                "transfer_count": len(self._transfer_history),
                "similarity_threshold": self.similarity_threshold,
            }


# ============================================================================
# Module Statistics
# ============================================================================

_module_start_time = time.time()


def statistics() -> Dict[str, Any]:
    """模块级统计"""
    return {
        "module": "self_evolving_skill_memory",
        "uptime_seconds": time.time() - _module_start_time,
        "key_classes": [
            "TrajectorySkillDistiller",
            "MultiBranchSkillRepository",
            "ContextUtilityEstimator",
            "SelectiveRetentionGate",
            "PostDeploymentEvolutionLoop",
            "CrossTaskSkillTransfer",
        ],
    }
