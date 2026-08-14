"""
P12-6: Self-Evolving Architecture — 对标 MemEvolve (ICML 2026)

实现四维诊断器 + 架构变异 + 适应度评估循环:
  - FourDimDiagnostics: 对 Encode/Store/Retrieve/Manage 四个阶段分别评分
  - ArchitectureMutator: 产生架构变异 (新增存储层/替换检索策略/调整压缩率)
  - FitnessEvaluator: 基于任务成功率选择最优变异
  - evolve(): 返回最优架构配置

Reference:
    MemEvolve: Self-Evolving Memory Architecture for Continual Agents (ICML 2026)
"""

import copy
import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════════════

class MemoryStage(Enum):
    """记忆流水线四阶段。"""
    ENCODE = "encode"      # 编码阶段
    STORE = "store"        # 存储阶段
    RETRIEVE = "retrieve"  # 检索阶段
    MANAGE = "manage"      # 管理/清理阶段


class MutationType(Enum):
    """架构变异类型。"""
    ADD_STORAGE_LAYER = "add_storage_layer"       # 新增存储层 (如 +graph/ +vector)
    REPLACE_RETRIEVER = "replace_retriever"        # 替换检索策略
    ADJUST_COMPRESSION = "adjust_compression"       # 调整压缩率
    RETUNE_ENCODER = "retune_encoder"               # 微调编码器参数
    CHANGE_CONSOLIDATION = "change_consolidation"   # 修改记忆巩固策略
    REBALANCE_ALLOCATION = "rebalance_allocation"   # 重新分配存储配额


class FitnessTrend(Enum):
    """适应度趋势。"""
    IMPROVING = "improving"
    STABLE = "stable"
    DEGRADING = "degrading"


# ══════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════

@dataclass
class StageDiagnosis:
    """单阶段诊断报告。"""
    stage: MemoryStage
    latency_ms: float           # 平均延迟 (ms)
    error_rate: float           # 错误率 (0~1)
    throughput: float            # 吞吐量 (ops/sec)
    memory_usage_mb: float       # 内存占用 (MB)
    quality_score: float         # 输出质量 (0~1)
    bottleneck: bool = False     # 是否为瓶颈
    recommendations: list[str] = field(default_factory=list)


@dataclass
class FourDimDiagnostics:
    """四维诊断报告。"""
    stages: dict[MemoryStage, StageDiagnosis] = field(default_factory=dict)
    overall_score: float = 0.0
    critical_bottlenecks: list[MemoryStage] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def get_bottleneck_stage(self) -> MemoryStage | None:
        """返回最严重的瓶颈阶段。"""
        if not self.stages:
            return None
        return min(self.stages, key=lambda s: self.stages[s].quality_score)


@dataclass
class ArchitectureConfig:
    """架构配置。"""
    generation: int = 0
    storage_layers: list[str] = field(default_factory=lambda: ["episodic", "semantic"])
    retriever_strategy: str = "hybrid_weighted"
    compression_ratio: float = 0.7         # 压缩比 (0~1, 越低越压缩)
    encoder_type: str = "transformer_base"
    consolidation_policy: str = "temporal_decay"
    storage_quota: dict[str, float] = field(default_factory=dict)  # 各层配额 MB
    hyperparameters: dict[str, Any] = field(default_factory=dict)
    parent_id: str = ""                    # 变异的父配置 ID
    mutation_trace: list[MutationType] = field(default_factory=list)

    @property
    def config_id(self) -> str:
        return f"arch_gen{self.generation}_{len(self.mutation_trace)}mut"


@dataclass
class FitnessScore:
    """适应度评分。"""
    config: ArchitectureConfig
    task_success_rate: float        # 任务成功率
    avg_latency_ms: float            # 平均延迟
    memory_efficiency: float          # 记忆效率 (命中率/占用)
    composite_score: float            # 综合得分
    trend: FitnessTrend = FitnessTrend.STABLE
    benchmark_name: str = ""


# ══════════════════════════════════════════════════════════════════════
# 四维诊断器
# ══════════════════════════════════════════════════════════════════════

class FourDimDiagnoser:
    """对 Encode/Store/Retrieve/Manage 四阶段分别评分。"""

    def __init__(self, latency_weight: float = 0.2, error_weight: float = 0.3,
                 quality_weight: float = 0.35, memory_weight: float = 0.15):
        self.latency_weight = latency_weight
        self.error_weight = error_weight
        self.quality_weight = quality_weight
        self.memory_weight = memory_weight

    def diagnose(self, stage_metrics: dict[MemoryStage, dict]) -> FourDimDiagnostics:
        """执行四维诊断。

        Args:
            stage_metrics: {stage: {"latency_ms": ..., "error_rate": ..., ...}}

        Returns:
            FourDimDiagnostics: 包含各阶段诊断和总体评分
        """
        stages = {}
        scores = []

        for stage, metrics in stage_metrics.items():
            latency = metrics.get("latency_ms", 100.0)
            error = metrics.get("error_rate", 0.0)
            throughput = metrics.get("throughput", 1000.0)
            mem = metrics.get("memory_usage_mb", 100.0)
            quality = metrics.get("quality_score", 0.8)

            # 瓶颈判定：任一指标超过阈值
            is_bottleneck = latency > 200 or error > 0.1 or quality < 0.5

            # 阶段质量分
            stage_quality = (
                quality * self.quality_weight
                + (1.0 - min(error, 1.0)) * self.error_weight
                + (1.0 - min(latency / 500, 1.0)) * self.latency_weight
                + (1.0 - min(mem / 500, 1.0)) * self.memory_weight
            )

            recs = self._generate_recommendations(stage, latency, error, quality, is_bottleneck)

            stages[stage] = StageDiagnosis(
                stage=stage,
                latency_ms=latency,
                error_rate=error,
                throughput=throughput,
                memory_usage_mb=mem,
                quality_score=quality,
                bottleneck=is_bottleneck,
                recommendations=recs,
            )
            scores.append(stage_quality)

        overall = sum(scores) / max(len(scores), 1)
        bottlenecks = [s for s in stages if stages[s].bottleneck]

        return FourDimDiagnostics(
            stages=stages,
            overall_score=round(overall, 4),
            critical_bottlenecks=bottlenecks,
        )

    def _generate_recommendations(self, stage: MemoryStage, latency: float,
                                   error: float, quality: float,
                                   bottleneck: bool) -> list[str]:
        recs = []
        if bottleneck:
            if latency > 200:
                recs.append(f"High latency ({latency:.0f}ms) in {stage.value} — consider batching or caching")
            if error > 0.1:
                recs.append(f"Elevated error rate ({error:.1%}) in {stage.value} — validate data pipeline")
            if quality < 0.5:
                recs.append(f"Low quality ({quality:.2f}) in {stage.value} — upgrade encoder or retriever")
        if stage == MemoryStage.RETRIEVE and quality < 0.6:
            recs.append("Consider replacing retriever strategy with hybrid semantic+keyword")
        if stage == MemoryStage.MANAGE and latency > 150:
            recs.append("Consider adjusting consolidation frequency or compression ratio")
        return recs


# ══════════════════════════════════════════════════════════════════════
# 架构变异器
# ══════════════════════════════════════════════════════════════════════

class ArchitectureMutator:
    """产生架构变异。"""

    MUTATION_POOL = list(MutationType)

    def __init__(self, max_generations: int = 10, population_size: int = 20,
                 mutation_rate: float = 0.3, seed: int = 42):
        self.max_generations = max_generations
        self.population_size = population_size
        self.mutation_rate = mutation_rate
        self._rng = random.Random(seed)
        self._history: list[ArchitectureConfig] = []

    def initialize_population(self) -> list[ArchitectureConfig]:
        """初始化多样性种群。"""
        population = []
        base = ArchitectureConfig(generation=0)
        for i in range(self.population_size):
            cfg = copy.deepcopy(base)
            cfg.generation = 0
            # 随机轻微的初始扰动
            cfg.compression_ratio = round(self._rng.uniform(0.4, 0.9), 2)
            cfg.retriever_strategy = self._rng.choice(
                ["hybrid_weighted", "semantic_only", "keyword_boosted", "graph_traversal"]
            )
            cfg.encoder_type = self._rng.choice(
                ["transformer_base", "transformer_large", "e5_mistral", "bge_m3"]
            )
            population.append(cfg)
        self._history.extend(population)
        return population

    def mutate(self, parent: ArchitectureConfig,
               diagnosis: FourDimDiagnostics) -> list[ArchitectureConfig]:
        """基于诊断结果产生定向变异。

        Args:
            parent: 父配置
            diagnosis: 四维诊断报告

        Returns:
            变异后代列表
        """
        offspring = []

        # 根据瓶颈选择变异类型
        targeted_mutations = self._target_mutations(diagnosis)

        num_mutations = max(1, int(self.mutation_rate * len(self.MUTATION_POOL)))
        for _ in range(num_mutations):
            mutation_type = self._rng.choice(
                targeted_mutations if targeted_mutations else list(self.MUTATION_POOL)
            )
            child = self._apply_mutation(copy.deepcopy(parent), mutation_type)
            child.generation = parent.generation + 1
            child.parent_id = parent.config_id
            child.mutation_trace = parent.mutation_trace + [mutation_type]
            offspring.append(child)

        self._history.extend(offspring)
        return offspring

    def crossover(self, parent_a: ArchitectureConfig,
                  parent_b: ArchitectureConfig) -> ArchitectureConfig:
        """交叉组合两个优秀配置。"""
        child = ArchitectureConfig(
            generation=max(parent_a.generation, parent_b.generation) + 1,
            storage_layers=self._rng.choice([parent_a.storage_layers, parent_b.storage_layers]),
            retriever_strategy=self._rng.choice([parent_a.retriever_strategy, parent_b.retriever_strategy]),
            compression_ratio=round((parent_a.compression_ratio + parent_b.compression_ratio) / 2, 2),
            encoder_type=self._rng.choice([parent_a.encoder_type, parent_b.encoder_type]),
            consolidation_policy=self._rng.choice([parent_a.consolidation_policy, parent_b.consolidation_policy]),
            parent_id=f"{parent_a.config_id}+{parent_b.config_id}",
        )
        return child

    def _target_mutations(self, diagnosis: FourDimDiagnostics) -> list[MutationType]:
        """根据诊断瓶颈产生定向变异类型。"""
        mutations = []
        for stage in diagnosis.critical_bottlenecks:
            if stage == MemoryStage.ENCODE:
                mutations.append(MutationType.RETUNE_ENCODER)
            elif stage == MemoryStage.STORE:
                mutations.extend([MutationType.ADD_STORAGE_LAYER, MutationType.REBALANCE_ALLOCATION])
            elif stage == MemoryStage.RETRIEVE:
                mutations.append(MutationType.REPLACE_RETRIEVER)
            elif stage == MemoryStage.MANAGE:
                mutations.extend([MutationType.ADJUST_COMPRESSION, MutationType.CHANGE_CONSOLIDATION])
        # 去重
        return list(set(mutations))

    def _apply_mutation(self, config: ArchitectureConfig,
                        mutation: MutationType) -> ArchitectureConfig:
        """执行单个变异操作。"""
        if mutation == MutationType.ADD_STORAGE_LAYER:
            new_layers = list(config.storage_layers)
            candidates = ["graph", "vector", "key_value", "hierarchical", "temporal"]
            available = [l for l in candidates if l not in new_layers]
            if available:
                new_layers.append(self._rng.choice(available))
                config.storage_layers = new_layers

        elif mutation == MutationType.REPLACE_RETRIEVER:
            config.retriever_strategy = self._rng.choice(
                ["hybrid_weighted", "semantic_only", "keyword_boosted", "graph_traversal",
                 "ensemble_voting", "active_reconstruction"]
            )

        elif mutation == MutationType.ADJUST_COMPRESSION:
            delta = self._rng.uniform(-0.3, 0.3)
            config.compression_ratio = max(0.1, min(1.0, config.compression_ratio + delta))

        elif mutation == MutationType.RETUNE_ENCODER:
            config.encoder_type = self._rng.choice(
                ["transformer_base", "transformer_large", "e5_mistral", "bge_m3", "grit_lm"]
            )
            config.hyperparameters["encoder_dim"] = self._rng.choice([768, 1024, 4096])

        elif mutation == MutationType.CHANGE_CONSOLIDATION:
            config.consolidation_policy = self._rng.choice(
                ["temporal_decay", "importance_weighted", "fifo", "lfu", "sliding_window"]
            )

        elif mutation == MutationType.REBALANCE_ALLOCATION:
            layers = config.storage_layers
            total = sum(config.storage_quota.values()) if config.storage_quota else 1000.0
            weights = [self._rng.uniform(0.5, 2.0) for _ in layers]
            w_total = sum(weights)
            config.storage_quota = {l: round(total * w / w_total, 1) for l, w in zip(layers, weights)}

        return config

    def get_mutation_history(self) -> list[ArchitectureConfig]:
        return list(self._history)


# ══════════════════════════════════════════════════════════════════════
# 适应度评估器
# ══════════════════════════════════════════════════════════════════════

class FitnessEvaluator:
    """基于任务成功率评估配置适应度。"""

    def __init__(self, success_weight: float = 0.5, latency_weight: float = 0.2,
                 efficiency_weight: float = 0.3):
        self.success_weight = success_weight
        self.latency_weight = latency_weight
        self.efficiency_weight = efficiency_weight
        self._evaluation_cache: dict[str, FitnessScore] = {}

    def evaluate(self, config: ArchitectureConfig,
                 task_results: list[dict]) -> FitnessScore:
        """在给定任务结果上评估配置。

        Args:
            config: 待评估架构配置
            task_results: 每个任务的结果 dict，含 "success", "latency_ms", "hit_rate"

        Returns:
            FitnessScore: 适应度评分
        """
        if not task_results:
            return FitnessScore(
                config=config,
                task_success_rate=0.0,
                avg_latency_ms=999.0,
                memory_efficiency=0.0,
                composite_score=0.0,
            )

        success_rate = sum(1 for r in task_results if r.get("success", False)) / len(task_results)
        avg_latency = sum(r.get("latency_ms", 100) for r in task_results) / len(task_results)
        hit_rate = sum(r.get("hit_rate", 0.5) for r in task_results) / len(task_results)

        # 延迟归一化 (假设 500ms 及格线)
        latency_norm = max(0, 1.0 - avg_latency / 500)

        composite = (
            success_rate * self.success_weight
            + latency_norm * self.latency_weight
            + hit_rate * self.efficiency_weight
        )

        score = FitnessScore(
            config=config,
            task_success_rate=round(success_rate, 4),
            avg_latency_ms=round(avg_latency, 2),
            memory_efficiency=round(hit_rate, 4),
            composite_score=round(composite, 4),
        )

        self._evaluation_cache[config.config_id] = score
        return score

    def compare(self, score_a: FitnessScore, score_b: FitnessScore) -> FitnessScore:
        """比较两个配置，返回更优者。"""
        if score_a.composite_score >= score_b.composite_score:
            winner = score_a
        else:
            winner = score_b

        if score_a.composite_score > score_b.composite_score:
            winner.trend = FitnessTrend.IMPROVING
        elif abs(score_a.composite_score - score_b.composite_score) < 0.01:
            winner.trend = FitnessTrend.STABLE
        else:
            winner.trend = FitnessTrend.DEGRADING

        return winner

    def get_best(self, scores: list[FitnessScore]) -> FitnessScore:
        return max(scores, key=lambda s: s.composite_score)

    def get_evaluation_history(self) -> dict:
        return {k: v.composite_score for k, v in self._evaluation_cache.items()}


# ══════════════════════════════════════════════════════════════════════
# 进化主循环
# ══════════════════════════════════════════════════════════════════════

class SelfEvolvingArchitecture:
    """自进化架构主类。

    整合诊断→变异→评估循环，返回最优架构配置。
    """

    def __init__(self, max_generations: int = 10, elite_size: int = 3,
                 tournament_size: int = 3, seed: int = 42):
        self.max_generations = max_generations
        self.elite_size = elite_size
        self.tournament_size = tournament_size
        self._rng = random.Random(seed)

        self.diagnoser = FourDimDiagnoser()
        self.mutator = ArchitectureMutator(
            max_generations=max_generations, population_size=20, seed=seed
        )
        self.evaluator = FitnessEvaluator()
        self._best_config: ArchitectureConfig | None = None
        self._best_score: FitnessScore | None = None
        self._evolution_log: list[dict] = []

    def evolve(self, task_results: list[dict],
               stage_metrics: dict[MemoryStage, dict]) -> tuple[ArchitectureConfig, FitnessScore]:
        """执行完整进化循环。

        Args:
            task_results: 初始任务结果用于评估
            stage_metrics: 初始阶段指标用于诊断

        Returns:
            (最优架构配置, 适应度评分)
        """
        # 1. 初始诊断
        diagnosis = self.diagnoser.diagnose(stage_metrics)
        logger.info(
            "Initial diagnosis — overall=%.4f, bottlenecks=%s",
            diagnosis.overall_score,
            [s.value for s in diagnosis.critical_bottlenecks],
        )

        # 2. 初始化种群
        population = self.mutator.initialize_population()

        # 3. 进化循环
        for gen in range(self.max_generations):
            # 3a. 评估当前种群
            scores: list[FitnessScore] = []
            for cfg in population:
                score = self.evaluator.evaluate(cfg, task_results)
                scores.append(score)

            # 3b. 排序并保留精英
            scores.sort(key=lambda s: s.composite_score, reverse=True)
            best_of_gen = scores[0]
            if self._best_score is None or best_of_gen.composite_score > self._best_score.composite_score:
                self._best_score = best_of_gen
                self._best_config = best_of_gen.config

            self._evolution_log.append({
                "generation": gen,
                "best_score": best_of_gen.composite_score,
                "avg_score": sum(s.composite_score for s in scores) / len(scores),
                "best_config_id": best_of_gen.config.config_id,
            })

            # 收敛检测
            if best_of_gen.composite_score >= 0.95:
                logger.info("Converged at generation %d (score=%.4f)", gen, best_of_gen.composite_score)
                break

            # 3c. 选择 + 变异生成下一代
            elites = [s.config for s in scores[:self.elite_size]]
            next_population = list(elites)

            # 重新诊断以引导变异
            if gen % 2 == 0:
                diagnosis = self.diagnoser.diagnose(stage_metrics)

            while len(next_population) < self.mutator.population_size:
                # 锦标赛选择
                parent = self._tournament_select(scores)
                offspring = self.mutator.mutate(parent, diagnosis)
                next_population.extend(offspring)

                # 交叉
                if len(next_population) < self.mutator.population_size:
                    parent_b = self._tournament_select(scores, exclude=parent)
                    if parent_b:
                        child = self.mutator.crossover(parent, parent_b)
                        next_population.append(child)

            population = next_population[:self.mutator.population_size]

        if self._best_config is None:
            self._best_config = population[0] if population else ArchitectureConfig()
            self._best_score = FitnessScore(
                config=self._best_config,
                task_success_rate=0.0,
                avg_latency_ms=0.0,
                memory_efficiency=0.0,
                composite_score=0.0,
            )

        return self._best_config, self._best_score

    def _tournament_select(self, scores: list[FitnessScore],
                           exclude: ArchitectureConfig | None = None) -> ArchitectureConfig:
        candidates = [s for s in scores if s.config != exclude]
        if not candidates:
            candidates = scores
        tournament = self._rng.sample(candidates, min(self.tournament_size, len(candidates)))
        return max(tournament, key=lambda s: s.composite_score).config

    def get_stats(self) -> dict:
        return {
            "generations_completed": len(self._evolution_log),
            "best_composite_score": self._best_score.composite_score if self._best_score else 0.0,
            "best_config_summary": (
                {"layers": self._best_config.storage_layers,
                 "retriever": self._best_config.retriever_strategy,
                 "compression": self._best_config.compression_ratio,
                 "generation": self._best_config.generation}
                if self._best_config else {}
            ),
            "mutation_history_len": len(self.mutator.get_mutation_history()),
            "evaluation_cache_size": len(self.evaluator.get_evaluation_history()),
        }


# ══════════════════════════════════════════════════════════════════════
# 模块自测
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("Self-Evolving Architecture — Self Test")
    print("=" * 60)

    # 诊断
    diagnoser = FourDimDiagnoser()
    stage_metrics = {
        MemoryStage.ENCODE: {"latency_ms": 80, "error_rate": 0.02, "throughput": 5000, "memory_usage_mb": 150, "quality_score": 0.85},
        MemoryStage.STORE: {"latency_ms": 50, "error_rate": 0.01, "throughput": 8000, "memory_usage_mb": 300, "quality_score": 0.90},
        MemoryStage.RETRIEVE: {"latency_ms": 250, "error_rate": 0.08, "throughput": 2000, "memory_usage_mb": 200, "quality_score": 0.55},
        MemoryStage.MANAGE: {"latency_ms": 180, "error_rate": 0.03, "throughput": 3000, "memory_usage_mb": 100, "quality_score": 0.70},
    }

    diagnosis = diagnoser.diagnose(stage_metrics)
    print(f"\n[Diagnosis] overall={diagnosis.overall_score:.4f}")
    print(f"  Bottlenecks: {[s.value for s in diagnosis.critical_bottlenecks]}")
    for stage, d in diagnosis.stages.items():
        print(f"  {stage.value}: quality={d.quality_score:.2f}, bottleneck={d.bottleneck}")
        if d.recommendations:
            for r in d.recommendations:
                print(f"    -> {r}")

    # 进化
    task_results = [
        {"success": True, "latency_ms": 150, "hit_rate": 0.72},
        {"success": False, "latency_ms": 320, "hit_rate": 0.45},
        {"success": True, "latency_ms": 200, "hit_rate": 0.68},
        {"success": True, "latency_ms": 180, "hit_rate": 0.75},
        {"success": False, "latency_ms": 280, "hit_rate": 0.50},
    ]

    evolver = SelfEvolvingArchitecture(max_generations=5, elite_size=2)
    best_cfg, best_score = evolver.evolve(task_results, stage_metrics)

    print(f"\n[Evolved Architecture]")
    print(f"  Composite score: {best_score.composite_score:.4f}")
    print(f"  Task success rate: {best_score.task_success_rate:.2%}")
    print(f"  Avglatency: {best_score.avg_latency_ms:.0f}ms")
    print(f"  Config: gen={best_cfg.generation}, layers={best_cfg.storage_layers}")
    print(f"  Retriever: {best_cfg.retriever_strategy}, compression={best_cfg.compression_ratio}")
    print(f"  Consolidation: {best_cfg.consolidation_policy}")
    print(f"\n[Evolution Log]")
    for entry in evolver._evolution_log:
        print(f"  Gen {entry['generation']}: best={entry['best_score']:.4f}, avg={entry['avg_score']:.4f}")
