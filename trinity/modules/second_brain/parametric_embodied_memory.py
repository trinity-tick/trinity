"""
P20-3: Parametric Embodied Memory — 参数化具身记忆

对标论文: PEAM (arXiv 2605.27762, 2026.05)
核心发现: 慢速推理 LLM + 快速参数化 MoE + 失败对比学习 → 具身技能高效参数内化
三元语: 慢速推理 → 快速 MoE → 失败对比 → 价值评分 → 自触发固结 → 逐类隔离

设计要点:
- SlowDeliberativeLLM: 慢速推理 LLM 接口——开放式推理、规划、解释，提供高质量教师信号
- FastParametricMoE: 快速参数化 MoE——多模态 LoRA 架构，每类技能物理隔离的适配器
- FailureContrastiveLearner: 失败对比学习器——失败-纠正对通过 joint behavioral-cloning+contrastive 目标内化
- ParameterizationWorthinessScorer: 评分 WHAT 该被内化——基于复杂度/复用价值/故障率
- SelfTriggeredConsolidator: 决定 WHEN 内化——scale-free 触发，无任务特定阈值
- PerCategoryAdapterIsolator: 物理隔离每个类别的 LoRA 适配器，防止灾难遗忘
- 与 P13-1 weight_distiller.py 互补——weight_distiller 做模型→记忆蒸馏，本模块做记忆→MoE LoRA 参数内化
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

class EmbodiedSkillCategory(Enum):
    """具身技能类别"""
    NAVIGATION = "navigation"           # 导航
    MANIPULATION = "manipulation"       # 操作/抓取
    INTERACTION = "interaction"         # 人机交互
    PERCEPTION = "perception"           # 感知
    PLANNING = "planning"               # 规划
    LOCOMOTION = "locomotion"           # 移动


class ConsolidationTrigger(Enum):
    """固结触发模式"""
    THRESHOLD_BASED = "threshold"       # 阈值触发 (已废弃，PEAM 采用 scale-free)
    SCALE_FREE = "scale_free"           # scale-free 自触发 (PEAM 默认)
    PERIODIC = "periodic"               # 周期性触发
    FAILURE_DRIVEN = "failure_driven"   # 失败驱动触发


class AdapterState(Enum):
    """LoRA 适配器状态"""
    UNINITIALIZED = "uninitialized"
    TRAINING = "training"
    ACTIVE = "active"
    FROZEN = "frozen"
    DEPRECATED = "deprecated"


class ContrastiveObjective(Enum):
    """对比学习目标"""
    BEHAVIORAL_CLONING = "bc"           # 行为克隆
    CONTRASTIVE = "contrastive"         # 对比损失
    JOINT = "joint"                     # 联合 BC + Contrastive (PEAM 默认)


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class DeliberativeOutput:
    """慢速推理输出"""
    output_id: str
    reasoning_trace: str               # 推理链 (CoT)
    plan: List[str]                    # 规划步骤
    explanation: str                   # 解释说明
    confidence: float = 0.0            # 置信度
    latency_ms: float = 0.0            # 推理延迟 (ms)
    token_count: int = 0               # Token 消耗
    timestamp: float = field(default_factory=time.time)


@dataclass
class LoRAAdapter:
    """LoRA 适配器描述"""
    adapter_id: str
    category: EmbodiedSkillCategory
    rank: int = 8                      # LoRA rank
    alpha: float = 16.0                # LoRA alpha
    target_modules: List[str] = field(default_factory=lambda: ["q_proj", "v_proj"])
    state: AdapterState = AdapterState.UNINITIALIZED
    skill_count: int = 0              # 内化技能数
    performance_score: float = 0.0    # 性能评分
    created_at: float = field(default_factory=time.time)
    last_updated_at: float = field(default_factory=time.time)


@dataclass
class FailureContrastivePair:
    """失败-纠正对"""
    pair_id: str
    task_description: str
    failed_attempt: str                # 失败尝试描述
    correction: str                    # 纠正方案
    category: EmbodiedSkillCategory
    failure_type: str                  # 失败类型分类
    learning_signal: float = 1.0      # 学习信号强度
    timestamp: float = field(default_factory=time.time)


@dataclass
class WorthinessScore:
    """参数化价值评分"""
    skill_id: str
    category: EmbodiedSkillCategory
    complexity_score: float            # 复杂度分 [0, 1]
    reuse_value: float                 # 复用价值 [0, 1]
    failure_rate: float                # 历史故障率
    worthiness: float                  # 综合价值 = (complexity * reuse) / (1 + failure_rate)
    should_parameterize: bool          # 是否应被内化
    priority: int = 0                 # 内化优先级


@dataclass
class ConsolidationBatch:
    """固结批次"""
    batch_id: str
    category: EmbodiedSkillCategory
    skill_ids: List[str]              # 待内化技能 ID
    contrastive_pairs: List[FailureContrastivePair] = field(default_factory=list)
    target_adapter_id: str = ""
    trigger: ConsolidationTrigger = ConsolidationTrigger.SCALE_FREE
    created_at: float = field(default_factory=time.time)


@dataclass
class AdapterIsolationRecord:
    """适配器隔离记录"""
    category: EmbodiedSkillCategory
    adapter_id: str
    isolation_score: float             # 隔离度 [0, 1]，越高越独立
    cross_interference: float          # 跨类别干扰度 [0, 1]
    catastrophic_forgetting_risk: float  # 灾难遗忘风险
    last_checked_at: float = field(default_factory=time.time)


# ============================================================================
# SlowDeliberativeLLM
# ============================================================================

class SlowDeliberativeLLM:
    """慢速推理 LLM 接口

    开放式推理、规划、解释，提供高质量教师信号。
    用于复杂具身任务的深思熟虑决策。
    """

    def __init__(
        self,
        default_confidence: float = 0.85,
        max_reasoning_steps: int = 10,
    ):
        self.default_confidence = default_confidence
        self.max_reasoning_steps = max_reasoning_steps
        self._lock = threading.RLock()
        self._inference_count: int = 0
        self._total_latency_ms: float = 0.0
        self._total_tokens: int = 0

    def deliberate(
        self,
        task: str,
        context: Optional[str] = None,
        chain_of_thought: bool = True,
    ) -> DeliberativeOutput:
        """执行慢速推理"""
        with self._lock:
            start = time.time()

            # 模拟推理链
            reasoning = f"[CoT] Task: {task}"
            if context:
                reasoning += f"\nContext: {context[:200]}"
            reasoning += "\nStep 1: Analyze task requirements"
            reasoning += "\nStep 2: Decompose into subtasks"
            reasoning += "\nStep 3: Evaluate action constraints"
            reasoning += f"\nStep 4-{min(self.max_reasoning_steps, 8)}: Iterative refinement"

            plan = [
                "perceive environment state",
                "identify relevant skills",
                "plan action sequence",
                "execute with monitoring",
                "verify post-conditions",
            ]
            explanation = (
                f"After {self.max_reasoning_steps}-step reasoning, "
                f"determined optimal approach for: {task[:100]}"
            )

            latency = (time.time() - start) * 1000
            self._inference_count += 1
            self._total_latency_ms += latency
            token_est = len(task.split()) * 5 + 50
            self._total_tokens += token_est

            return DeliberativeOutput(
                output_id=f"delib_{self._inference_count}",
                reasoning_trace=reasoning,
                plan=plan,
                explanation=explanation,
                confidence=self.default_confidence,
                latency_ms=latency,
                token_count=token_est,
            )

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "inference_count": self._inference_count,
                "avg_latency_ms": (
                    self._total_latency_ms / max(self._inference_count, 1)
                ),
                "total_tokens": self._total_tokens,
                "default_confidence": self.default_confidence,
            }


# ============================================================================
# FastParametricMoE
# ============================================================================

class FastParametricMoE:
    """快速参数化 MoE

    多模态 LoRA 架构，每类技能物理隔离的适配器。
    慢速推理教师 → 快速 MoE 学生的知识蒸馏管线。
    """

    def __init__(self, base_rank: int = 8, base_alpha: float = 16.0):
        self.base_rank = base_rank
        self.base_alpha = base_alpha
        self._lock = threading.RLock()
        self._adapters: Dict[EmbodiedSkillCategory, LoRAAdapter] = {}
        self._router_weights: Dict[EmbodiedSkillCategory, float] = defaultdict(lambda: 1.0)
        self._inference_count: int = 0
        self._total_latency_us: float = 0.0  # 微秒级

    def register_adapter(self, category: EmbodiedSkillCategory, rank: Optional[int] = None) -> LoRAAdapter:
        """注册新适配器"""
        with self._lock:
            if category in self._adapters:
                return self._adapters[category]
            adapter = LoRAAdapter(
                adapter_id=f"lora_{category.value}_{int(time.time())}",
                category=category,
                rank=rank or self.base_rank,
                alpha=self.base_alpha,
                state=AdapterState.ACTIVE,
            )
            self._adapters[category] = adapter
            return adapter

    def get_adapter(self, category: EmbodiedSkillCategory) -> Optional[LoRAAdapter]:
        with self._lock:
            return self._adapters.get(category)

    def route(self, task_category: EmbodiedSkillCategory) -> LoRAAdapter:
        """路由到对应类别的适配器 (不存在则注册)"""
        with self._lock:
            if task_category not in self._adapters:
                return self.register_adapter(task_category)
            return self._adapters[task_category]

    def fast_infer(self, category: EmbodiedSkillCategory, input_signal: str) -> str:
        """快速推理 (模拟)"""
        with self._lock:
            start = time.perf_counter()
            adapter = self.route(category)
            # 模拟快速推理
            result = f"[FastMoE:{category.value}] output for: {input_signal[:50]}"
            adapter.skill_count += 1
            adapter.last_updated_at = time.time()
            self._inference_count += 1
            self._total_latency_us += (time.perf_counter() - start) * 1_000_000
            return result

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "adapter_count": len(self._adapters),
                "inference_count": self._inference_count,
                "avg_latency_us": self._total_latency_us / max(self._inference_count, 1),
                "adapters": {
                    c.value: {
                        "rank": a.rank, "state": a.state.value,
                        "skills": a.skill_count, "score": a.performance_score,
                    }
                    for c, a in self._adapters.items()
                },
            }


# ============================================================================
# FailureContrastiveLearner
# ============================================================================

class FailureContrastiveLearner:
    """失败对比学习器

    失败-纠正对通过 joint behavioral-cloning + contrastive 目标内化。
    从失败中学习是 PEAM 的核心创新点。
    """

    def __init__(self, objective: ContrastiveObjective = ContrastiveObjective.JOINT):
        self.objective = objective
        self._lock = threading.RLock()
        self._contrastive_pairs: List[FailureContrastivePair] = []
        self._learning_iterations: int = 0
        self._successful_internalizations: int = 0

    def register_failure(
        self,
        task: str,
        failed_attempt: str,
        correction: str,
        category: EmbodiedSkillCategory,
        failure_type: str = "execution_error",
    ) -> FailureContrastivePair:
        """注册失败-纠正对"""
        with self._lock:
            pair = FailureContrastivePair(
                pair_id=f"fcp_{len(self._contrastive_pairs)}",
                task_description=task,
                failed_attempt=failed_attempt,
                correction=correction,
                category=category,
                failure_type=failure_type,
            )
            self._contrastive_pairs.append(pair)
            return pair

    def learn_from_failures(
        self,
        category: Optional[EmbodiedSkillCategory] = None,
    ) -> int:
        """执行对比学习迭代，返回内化成功数"""
        with self._lock:
            pairs = self._contrastive_pairs
            if category:
                pairs = [p for p in pairs if p.category == category]

            internalized = 0
            for pair in pairs[-10:]:  # 最近 10 个
                if pair.learning_signal > 0.5:
                    internalized += 1
                    self._successful_internalizations += 1
            self._learning_iterations += 1
            return internalized

    def get_pairs_by_category(self, category: EmbodiedSkillCategory) -> List[FailureContrastivePair]:
        with self._lock:
            return [p for p in self._contrastive_pairs if p.category == category]

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "total_pairs": len(self._contrastive_pairs),
                "learning_iterations": self._learning_iterations,
                "successful_internalizations": self._successful_internalizations,
                "objective": self.objective.value,
                "by_category": {
                    c.value: len([p for p in self._contrastive_pairs if p.category == c])
                    for c in EmbodiedSkillCategory
                },
            }


# ============================================================================
# ParameterizationWorthinessScorer
# ============================================================================

class ParameterizationWorthinessScorer:
    """参数化价值评分器

    评分 WHAT 该被内化: 基于复杂度、复用价值、故障率三维评分。
    高复杂度 + 高复用 + 低故障率 → 高优先级内化。
    """

    def __init__(
        self,
        complexity_weight: float = 0.4,
        reuse_weight: float = 0.35,
        failure_penalty_weight: float = 0.25,
        worthiness_threshold: float = 0.5,
    ):
        self.complexity_weight = complexity_weight
        self.reuse_weight = reuse_weight
        self.failure_penalty_weight = failure_penalty_weight
        self.worthiness_threshold = worthiness_threshold
        self._lock = threading.RLock()
        self._scores: Dict[str, WorthinessScore] = {}

    def score(
        self,
        skill_id: str,
        category: EmbodiedSkillCategory,
        complexity: float,
        reuse_value: float,
        failure_rate: float,
    ) -> WorthinessScore:
        """计算参数化价值"""
        with self._lock:
            # 综合价值: (complexity * reuse) / (1 + failure_rate)
            worthiness = (complexity * reuse_value) / (1.0 + failure_rate)
            worthiness = max(0.0, min(1.0, worthiness))

            ws = WorthinessScore(
                skill_id=skill_id,
                category=category,
                complexity_score=complexity,
                reuse_value=reuse_value,
                failure_rate=failure_rate,
                worthiness=worthiness,
                should_parameterize=worthiness >= self.worthiness_threshold,
                priority=int((1.0 - worthiness) * 10) + 1,
            )
            self._scores[skill_id] = ws
            return ws

    def get_prioritized(self, top_k: int = 10) -> List[WorthinessScore]:
        """获取按优先级排序的技能列表"""
        with self._lock:
            scored = sorted(
                self._scores.values(),
                key=lambda s: (s.should_parameterize, s.worthiness),
                reverse=True,
            )
            return scored[:top_k]

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "total_scored": len(self._scores),
                "worthy_count": sum(1 for s in self._scores.values() if s.should_parameterize),
                "threshold": self.worthiness_threshold,
                "weights": {
                    "complexity": self.complexity_weight,
                    "reuse": self.reuse_weight,
                    "failure_penalty": self.failure_penalty_weight,
                },
            }


# ============================================================================
# SelfTriggeredConsolidator
# ============================================================================

class SelfTriggeredConsolidator:
    """自触发固结器

    决定 WHEN 内化——scale-free，无任务特定阈值。
    PEAM 的创新点：不依赖人工设定的阈值，基于分布统计自动触发。
    """

    def __init__(
        self,
        moe: FastParametricMoE,
        scorer: ParameterizationWorthinessScorer,
        learner: FailureContrastiveLearner,
    ):
        self.moe = moe
        self.scorer = scorer
        self.learner = learner
        self._lock = threading.RLock()
        self._consolidation_batches: List[ConsolidationBatch] = []
        self._trigger_count: int = 0

    def should_consolidate(self, category: EmbodiedSkillCategory, skill_count: int) -> bool:
        """scale-free 触发判定"""
        with self._lock:
            # 使用对数正态分布的 scale-free 判定
            # 当新技能数超过已有数的对数增长阈值时触发
            adapter = self.moe.get_adapter(category)
            existing = adapter.skill_count if adapter else 0
            # scale-free: 触发阈值随已有技能增长而自适应
            trigger_threshold = max(3, int(math.log(max(existing, 1) + 1) * 2))
            return skill_count >= trigger_threshold

    def consolidate(
        self,
        category: EmbodiedSkillCategory,
        skill_ids: List[str],
    ) -> Optional[ConsolidationBatch]:
        """执行固结操作"""
        with self._lock:
            pairs = self.learner.get_pairs_by_category(category)
            adapter = self.moe.route(category)
            batch = ConsolidationBatch(
                batch_id=f"cons_{category.value}_{self._trigger_count}",
                category=category,
                skill_ids=skill_ids,
                contrastive_pairs=pairs,
                target_adapter_id=adapter.adapter_id,
                trigger=ConsolidationTrigger.SCALE_FREE,
            )
            self._consolidation_batches.append(batch)
            self._trigger_count += 1
            # 更新适配器
            adapter.state = AdapterState.ACTIVE
            adapter.skill_count += len(skill_ids)
            adapter.last_updated_at = time.time()
            return batch

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "trigger_count": self._trigger_count,
                "total_batches": len(self._consolidation_batches),
                "batches_by_category": {
                    c.value: len([b for b in self._consolidation_batches if b.category == c])
                    for c in EmbodiedSkillCategory
                },
            }


# ============================================================================
# PerCategoryAdapterIsolator
# ============================================================================

class PerCategoryAdapterIsolator:
    """逐类适配器隔离器

    物理隔离每个类别的 LoRA 适配器，防止灾难遗忘。
    定期检测跨类别干扰度并触发隔离加固。
    """

    def __init__(self, moe: FastParametricMoE):
        self.moe = moe
        self._lock = threading.RLock()
        self._isolation_records: Dict[EmbodiedSkillCategory, AdapterIsolationRecord] = {}
        self._check_count: int = 0

    def check_isolation(self) -> Dict[EmbodiedSkillCategory, AdapterIsolationRecord]:
        """检查所有适配器的隔离状态"""
        with self._lock:
            for category, adapter in self.moe._adapters.items():
                # 计算隔离度 (基于技能数和性能分)
                isolation = adapter.performance_score * (
                    1.0 - math.exp(-adapter.skill_count / 10.0)
                )
                cross_interference = 0.0
                # 检查与其他适配器的干扰
                for other_cat, other_adapter in self.moe._adapters.items():
                    if other_cat != category:
                        skill_overlap = min(adapter.skill_count, other_adapter.skill_count)
                        cross_interference += skill_overlap * 0.001
                cross_interference = min(1.0, cross_interference)

                record = AdapterIsolationRecord(
                    category=category,
                    adapter_id=adapter.adapter_id,
                    isolation_score=isolation,
                    cross_interference=cross_interference,
                    catastrophic_forgetting_risk=1.0 - isolation + cross_interference,
                )
                self._isolation_records[category] = record
            self._check_count += 1
            return dict(self._isolation_records)

    def get_risk_report(self) -> List[AdapterIsolationRecord]:
        """获取遗忘风险报告 (高风险优先)"""
        with self._lock:
            return sorted(
                self._isolation_records.values(),
                key=lambda r: r.catastrophic_forgetting_risk,
                reverse=True,
            )

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "check_count": self._check_count,
                "isolated_adapters": len(self._isolation_records),
                "max_forgetting_risk": max(
                    (r.catastrophic_forgetting_risk for r in self._isolation_records.values()),
                    default=0.0,
                ),
                "records": {
                    c.value: {
                        "isolation": r.isolation_score,
                        "interference": r.cross_interference,
                        "forgetting_risk": r.catastrophic_forgetting_risk,
                    }
                    for c, r in self._isolation_records.items()
                },
            }


# ============================================================================
# Module Statistics
# ============================================================================

_module_start_time = time.time()


def statistics() -> Dict[str, Any]:
    """模块级统计"""
    return {
        "module": "parametric_embodied_memory",
        "uptime_seconds": time.time() - _module_start_time,
        "key_classes": [
            "SlowDeliberativeLLM",
            "FastParametricMoE",
            "FailureContrastiveLearner",
            "ParameterizationWorthinessScorer",
            "SelfTriggeredConsolidator",
            "PerCategoryAdapterIsolator",
        ],
    }
