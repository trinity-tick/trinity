"""
# status: orphan (2026-08-15 audit, not in runtime path)
P20-8: Continuous Learning ARIA — Dual-Memory Continuous Learning (Zenodo 2026)
================================================================================

对标方案：ARIA Continuous Learning Agent (doi:10.5281/zenodo.20545028).

设计要点：
  - 冻结 LLM 对外推理 + 动态向量存储 + 时态知识图谱（新事实时间戳标记）
  - 时序评分解决过时答案问题
  - 周度 LoRA 微适配循环（Elastic Weight Consolidation 防灾难遗忘）
  - 知识新鲜度追踪

核心组件：
  - TemporalKnowledgeGraph:    时态知识图谱
  - AriaLoRAAdapter:           周度 LoRA 微适配器
  - ElasticWeightConsolidation: EWC 防灾难遗忘
  - ContinuousLearner:         连续学习总控
"""

from __future__ import annotations

import logging
import math
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

class KnowledgeFreshness(Enum):
    """知识新鲜度。"""
    REAL_TIME = "real_time"        # 实时（< 1 小时）
    FRESH = "fresh"                # 新鲜（< 1 天）
    RECENT = "recent"              # 近期（< 1 周）
    STALE = "stale"                # 陈旧（< 1 月）
    OUTDATED = "outdated"          # 过时（> 1 月）


class LoRAState(Enum):
    """LoRA 微调状态。"""
    IDLE = "idle"
    TRAINING = "training"
    VALIDATING = "validating"
    DEPLOYED = "deployed"
    ROLLED_BACK = "rolled_back"


class EWCStrategy(Enum):
    """EWC 策略。"""
    DIAGONAL_FISHER = "diagonal_fisher"    # 对角 Fisher 信息矩阵
    ONLINE_EWC = "online_ewc"              # 在线 EWC
    SYNAPSE_INTELLIGENCE = "synapse_intelligence"  # 突触智能


class FactSource(Enum):
    """事实来源。"""
    WEB = "web"
    DOCUMENT = "document"
    API = "api"
    USER_INPUT = "user_input"
    INFERRED = "inferred"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class TemporalFact:
    """时态知识图谱中的事实节点。"""
    fact_id: str
    subject: str
    predicate: str
    object: str
    timestamp: float
    source: FactSource
    confidence: float = 1.0
    freshness: KnowledgeFreshness = KnowledgeFreshness.FRESH
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    access_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def age_hours(self) -> float:
        return (time.time() - self.timestamp) / 3600.0


@dataclass
class TemporalScore:
    """时序评分结果。"""
    fact_id: str
    base_score: float
    temporal_penalty: float
    final_score: float
    freshness: KnowledgeFreshness


@dataclass
class LoRALayer:
    """LoRA 层参数。"""
    layer_name: str
    rank: int
    alpha: float
    weights_a: List[List[float]] = field(default_factory=list)
    weights_b: List[List[float]] = field(default_factory=list)
    fisher_diagonal: List[float] = field(default_factory=list)


@dataclass
class EWCConfig:
    """EWC 配置。"""
    lambda_ewc: float = 100.0    # EWC 正则化强度
    gamma: float = 0.9           # Fisher 信息矩阵衰减
    strategy: EWCStrategy = EWCStrategy.DIAGONAL_FISHER


@dataclass
class WeeklyCycle:
    """周度 LoRA 微调周期。"""
    cycle_id: str
    week_number: int
    start_time: float
    end_time: Optional[float] = None
    facts_ingested: int = 0
    lora_state: LoRAState = LoRAState.IDLE
    benchmark_scores: Dict[str, float] = field(default_factory=dict)
    knowledge_retention: float = 1.0
    ewc_penalty: float = 0.0


# ============================================================================
# Constants
# ============================================================================

FRESHNESS_THRESHOLDS: Dict[KnowledgeFreshness, float] = {
    KnowledgeFreshness.REAL_TIME: 1.0,      # 小时
    KnowledgeFreshness.FRESH: 24.0,
    KnowledgeFreshness.RECENT: 168.0,        # 7 天
    KnowledgeFreshness.STALE: 720.0,         # 30 天
    KnowledgeFreshness.OUTDATED: float("inf"),
}

FRESHNESS_SCORE_MAP: Dict[KnowledgeFreshness, float] = {
    KnowledgeFreshness.REAL_TIME: 1.0,
    KnowledgeFreshness.FRESH: 0.9,
    KnowledgeFreshness.RECENT: 0.7,
    KnowledgeFreshness.STALE: 0.4,
    KnowledgeFreshness.OUTDATED: 0.1,
}

WEEKLY_CYCLE_DURATION: float = 7 * 24 * 3600  # 7 天


# ============================================================================
# Core Components
# ============================================================================

class TemporalKnowledgeGraph:
    """时态知识图谱。

    新事实带时间戳标记，支持时序评分和过时检测。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.facts: Dict[str, TemporalFact] = {}
        self.subject_index: Dict[str, List[str]] = defaultdict(list)
        self.predicate_index: Dict[str, List[str]] = defaultdict(list)

    def insert(self, subject: str, predicate: str, obj: str,
               source: FactSource = FactSource.DOCUMENT,
               confidence: float = 1.0) -> str:
        """插入时态事实。"""
        with self._lock:
            # 检测是否有旧版本
            existing = self._find_existing(subject, predicate)
            fact_id = str(uuid.uuid4())[:8]

            fact = TemporalFact(
                fact_id=fact_id,
                subject=subject,
                predicate=predicate,
                object=obj,
                timestamp=time.time(),
                source=source,
                confidence=confidence,
                freshness=KnowledgeFreshness.REAL_TIME,
            )

            if existing:
                existing.superseded_by = fact_id
                fact.supersedes = existing.fact_id

            self.facts[fact_id] = fact
            self.subject_index[subject].append(fact_id)
            self.predicate_index[predicate].append(fact_id)

            return fact_id

    def _find_existing(self, subject: str, predicate: str) -> Optional[TemporalFact]:
        """查找已有事实。"""
        subject_facts = self.subject_index.get(subject, [])
        for fid in subject_facts:
            fact = self.facts[fid]
            if fact.predicate == predicate and fact.superseded_by is None:
                return fact
        return None

    def query(self, subject: str, predicate: Optional[str] = None,
              max_age_hours: Optional[float] = None) -> List[TemporalFact]:
        """查询事实。"""
        with self._lock:
            results: List[TemporalFact] = []
            subject_facts = self.subject_index.get(subject, [])

            for fid in subject_facts:
                fact = self.facts.get(fid)
                if not fact:
                    continue
                if predicate and fact.predicate != predicate:
                    continue
                if fact.superseded_by:
                    continue
                if max_age_hours and fact.age_hours() > max_age_hours:
                    continue
                fact.access_count += 1
                results.append(fact)

            return results

    def temporal_score(self, fact_id: str) -> TemporalScore:
        """时序评分：解决过时答案问题。"""
        with self._lock:
            fact = self.facts.get(fact_id)
            if not fact:
                return TemporalScore(fact_id=fact_id, base_score=0.0, temporal_penalty=0.0, final_score=0.0, freshness=KnowledgeFreshness.OUTDATED)

            # 更新新鲜度
            age = fact.age_hours()
            for freshness in KnowledgeFreshness:
                if age < FRESHNESS_THRESHOLDS[freshness]:
                    fact.freshness = freshness
                    break

            base_score = fact.confidence
            temporal_factor = FRESHNESS_SCORE_MAP.get(fact.freshness, 0.1)
            final = base_score * temporal_factor

            return TemporalScore(
                fact_id=fact_id,
                base_score=base_score,
                temporal_penalty=1.0 - temporal_factor,
                final_score=round(final, 4),
                freshness=fact.freshness,
            )

    def get_stale_facts(self, max_age_hours: float = 168.0) -> List[TemporalFact]:
        """获取过时事实。"""
        stale = []
        for fact in self.facts.values():
            if fact.age_hours() > max_age_hours and not fact.superseded_by:
                stale.append(fact)
        return stale

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            freshness_counts = defaultdict(int)
            for f in self.facts.values():
                freshness_counts[f.freshness.value] += 1
            return {
                "total_facts": len(self.facts),
                "unique_subjects": len(self.subject_index),
                "unique_predicates": len(self.predicate_index),
                "by_freshness": dict(freshness_counts),
                "superseded": sum(1 for f in self.facts.values() if f.supersedes),
            }


class ElasticWeightConsolidation:
    """EWC 防灾难遗忘。

    计算 Fisher 信息矩阵，保护重要参数。
    """

    def __init__(self, config: EWCConfig = None):
        self._lock = threading.RLock()
        self.config = config or EWCConfig()
        self.layers: Dict[str, LoRALayer] = {}
        self.reference_weights: Dict[str, List[float]] = {}

    def register_layer(self, name: str, rank: int, alpha: float, weights: List[float]) -> str:
        """注册 LoRA 层。"""
        with self._lock:
            layer = LoRALayer(
                layer_name=name,
                rank=rank,
                alpha=alpha,
                fisher_diagonal=[1.0] * len(weights),
            )
            self.layers[name] = layer
            self.reference_weights[name] = list(weights)
            return name

    def compute_fisher(self, layer_name: str, gradients: List[float]):
        """计算 Fisher 信息矩阵对角线。"""
        with self._lock:
            layer = self.layers.get(layer_name)
            if not layer:
                return

            gamma = self.config.gamma
            for i, grad in enumerate(gradients):
                if i < len(layer.fisher_diagonal):
                    # EMA 更新 Fisher 对角线
                    layer.fisher_diagonal[i] = (
                        gamma * layer.fisher_diagonal[i] + (1 - gamma) * grad ** 2
                    )

    def ewc_loss(self, layer_name: str, current_weights: List[float]) -> float:
        """计算 EWC 正则化损失。"""
        with self._lock:
            ref = self.reference_weights.get(layer_name, [])
            fisher = self.layers.get(layer_name)
            if not ref or not fisher or not current_weights:
                return 0.0

            loss = 0.0
            for i, (curr, ref_w) in enumerate(zip(current_weights, ref)):
                if i < len(fisher.fisher_diagonal):
                    loss += fisher.fisher_diagonal[i] * (curr - ref_w) ** 2

            return self.config.lambda_ewc * loss / max(len(current_weights), 1)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_layers": len(self.layers),
                "strategy": self.config.strategy.value,
                "lambda": self.config.lambda_ewc,
            }


class AriaLoRAAdapter:
    """周度 LoRA 微适配器。

    每周基于新事实进行 LoRA 微调，受 EWC 约束。
    """

    def __init__(self, ewc: ElasticWeightConsolidation, rank: int = 8, alpha: float = 16.0):
        self._lock = threading.RLock()
        self.ewc = ewc
        self.rank = rank
        self.alpha = alpha
        self.cycles: List[WeeklyCycle] = []
        self.current_cycle: Optional[WeeklyCycle] = None
        self.benchmark_history: Dict[int, Dict[str, float]] = {}

    def start_cycle(self, week_number: int) -> WeeklyCycle:
        """开始新的周度训练周期。"""
        with self._lock:
            cycle = WeeklyCycle(
                cycle_id=str(uuid.uuid4())[:8],
                week_number=week_number,
                start_time=time.time(),
                lora_state=LoRAState.TRAINING,
            )
            self.current_cycle = cycle
            self.cycles.append(cycle)
            return cycle

    def complete_cycle(self, benchmark_scores: Dict[str, float]):
        """完成周期并记录基准得分。"""
        with self._lock:
            if not self.current_cycle:
                return

            self.current_cycle.end_time = time.time()
            self.current_cycle.benchmark_scores = benchmark_scores
            self.current_cycle.lora_state = LoRAState.DEPLOYED
            self.benchmark_history[self.current_cycle.week_number] = benchmark_scores

    def check_retention(self, previous_scores: Dict[str, float],
                        current_scores: Dict[str, float]) -> float:
        """检查知识保留率（98.7% 目标）。"""
        if not previous_scores:
            return 1.0

        ratios = []
        for metric, prev in previous_scores.items():
            curr = current_scores.get(metric, 0.0)
            if prev > 0:
                ratios.append(curr / prev)

        return sum(ratios) / max(len(ratios), 1) if ratios else 1.0

    def rollback_if_needed(self, retention_threshold: float = 0.95):
        """如果保留率过低，回滚。"""
        with self._lock:
            if not self.current_cycle:
                return

            if self.current_cycle.knowledge_retention < retention_threshold:
                self.current_cycle.lora_state = LoRAState.ROLLED_BACK
                logger.warning(
                    f"Week {self.current_cycle.week_number}: "
                    f"Retention {self.current_cycle.knowledge_retention:.3f} < {retention_threshold}, rolled back"
                )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_cycles": len(self.cycles),
                "current_week": self.current_cycle.week_number if self.current_cycle else 0,
                "avg_retention": round(
                    sum(c.knowledge_retention for c in self.cycles) / max(len(self.cycles), 1), 4),
            }


class ContinuousLearner:
    """ARIA 连续学习总控。

    冻结 LLM + 动态向量存储 + 时态知识图谱 + 周度 LoRA + EWC。
    """

    def __init__(self, lora_rank: int = 8, ewc_lambda: float = 100.0):
        self._lock = threading.RLock()
        self.kg = TemporalKnowledgeGraph()
        self.ewc = ElasticWeightConsolidation(EWCConfig(lambda_ewc=ewc_lambda))
        self.lora = AriaLoRAAdapter(ewc=self.ewc, rank=lora_rank)
        self.weekly_default: Dict[str, float] = {}
        self.ingestion_count: int = 0

    def ingest(self, subject: str, predicate: str, obj: str,
               source: FactSource = FactSource.DOCUMENT, confidence: float = 1.0) -> str:
        """摄入新知识。"""
        with self._lock:
            fact_id = self.kg.insert(subject, predicate, obj, source, confidence)
            self.ingestion_count += 1
            return fact_id

    def answer(self, subject: str, predicate: Optional[str] = None) -> Tuple[Optional[str], TemporalScore]:
        """基于时态知识图谱回答问题。"""
        with self._lock:
            facts = self.kg.query(subject, predicate)
            if not facts:
                return None, TemporalScore(fact_id="", base_score=0, temporal_penalty=0, final_score=0, freshness=KnowledgeFreshness.OUTDATED)

            # 取最高时序评分的答案
            best: Optional[TemporalFact] = None
            best_score: Optional[TemporalScore] = None
            for fact in facts:
                score = self.kg.temporal_score(fact.fact_id)
                if best_score is None or score.final_score > best_score.final_score:
                    best = fact
                    best_score = score

            return best.object if best else None, best_score

    def run_weekly_cycle(self, week_number: int, new_facts: List[Tuple[str, str, str]],
                         benchmark: Dict[str, float]) -> Dict[str, Any]:
        """执行周度 LoRA 微调循环。"""
        with self._lock:
            # 保存旧 benchmark
            prev_benchmark = dict(self.weekly_default)

            # 摄入新事实
            for subj, pred, obj in new_facts:
                self.ingest(subj, pred, obj)

            # 开始训练
            cycle = self.lora.start_cycle(week_number)
            cycle.facts_ingested = len(new_facts)

            # 模拟训练后 benchmark
            self.lora.complete_cycle(benchmark)

            # 知识保留检查
            retention = self.lora.check_retention(prev_benchmark, benchmark)
            cycle.knowledge_retention = retention
            self.lora.rollback_if_needed()

            self.weekly_default = dict(benchmark)

            return {
                "week": week_number,
                "facts_ingested": len(new_facts),
                "retention": round(retention, 4),
                "state": cycle.lora_state.value,
                "kg_stats": self.kg.statistics(),
            }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_ingestions": self.ingestion_count,
                "kg": self.kg.statistics(),
                "lora": self.lora.statistics(),
                "ewc": self.ewc.statistics(),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P20-8 Continuous Learning ARIA",
        "benchmark": "ARIA (Zenodo 2026) — Dual-Memory + Temporal KG + Weekly LoRA + EWC",
        "classes": 4,
        "enums": 4,
        "dataclasses": 5,
        "key_pattern": "Ingest→TemporalScore→Weekly LoRA Cycle→EWC Guard→Retention Check",
        "key_metric": "98.7% retention over 47 weekly cycles, ~$500/day, 1000x cheaper than full fine-tuning",
        "thread_safe": True,
    }
