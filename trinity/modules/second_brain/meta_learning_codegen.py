"""
# status: orphan (2026-08-15 audit, not in runtime path)
P12-8: Meta-Learning Code Generation — 对标 ALMA (arXiv 2602.07755)

实现代码空间中的元学习记忆逻辑生成:
  - MetaAgent: 在代码空间中搜索最优记忆逻辑
  - CodeSearchSpace: 定义可组合的记忆原语 (编码器/存储层/检索器/管理器)
  - CodeEvaluator: 在目标任务上运行并评分生成代码
  - evolve_code(): 通过变异+交叉产生新一代代码

Reference:
    ALMA: Automated Learning of Memory Architectures for LLM Agents (arXiv 2602.07755)
"""

import copy
import json
import math
import random
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ══════════════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════════════

class MemoryPrimitive(Enum):
    """记忆原语类型。"""
    ENCODER = "encoder"           # 编码器原语
    STORE = "store"               # 存储层原语
    RETRIEVER = "retriever"       # 检索器原语
    MANAGER = "manager"           # 管理器原语
    CONSOLIDATOR = "consolidator" # 巩固器原语


class CodeMutationOp(Enum):
    """代码变异操作。"""
    ADD_PRIMITIVE = "add_primitive"         # 添加记忆原语
    REMOVE_PRIMITIVE = "remove_primitive"   # 移除记忆原语
    REPLACE_PRIMITIVE = "replace_primitive" # 替换记忆原语
    SWAP_ORDER = "swap_order"               # 交换执行顺序
    RETUNE_HYPERPARAM = "retune_hyperparam" # 调整超参数
    CROSSOVER = "crossover"                 # 交叉重组


class EvaluationMetric(Enum):
    """评估指标。"""
    ACCURACY = "accuracy"               # 任务准确率
    LATENCY = "latency"                 # 执行延迟
    MEMORY_USAGE = "memory_usage"       # 内存占用
    CODE_COMPLEXITY = "code_complexity" # 代码复杂度
    GENERALIZATION = "generalization"   # 泛化能力


# ══════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════

@dataclass
class PrimitiveBlock:
    """记忆原语块。"""
    block_id: str
    primitive_type: MemoryPrimitive
    name: str                           # 原语名称 (如 "TransformerEncoder", "VectorStore")
    code_snippet: str                    # 代码片段
    hyperparameters: dict[str, Any] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)  # 依赖的 block_id
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeIndividual:
    """代码个体 — 一组组合的记忆原语。"""
    individual_id: str
    generation: int = 0
    blocks: list[PrimitiveBlock] = field(default_factory=list)
    execution_order: list[str] = field(default_factory=list)  # block_id order
    parent_ids: list[str] = field(default_factory=list)
    mutation_history: list[CodeMutationOp] = field(default_factory=list)
    fitness: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationResult:
    """评估结果。"""
    individual: CodeIndividual
    metrics: dict[EvaluationMetric, float] = field(default_factory=dict)
    composite_score: float = 0.0
    benchmark_name: str = ""
    error_log: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class EvolutionGeneration:
    """一代进化的快照。"""
    generation: int
    best_score: float
    avg_score: float
    population_size: int
    mutations_applied: list[CodeMutationOp]
    best_individual_id: str
    timestamp: float = field(default_factory=time.time)


# ══════════════════════════════════════════════════════════════════════
# 代码搜索空间
# ══════════════════════════════════════════════════════════════════════

class CodeSearchSpace:
    """定义可组合的记忆原语搜索空间。"""

    PRIMITIVE_LIBRARY: dict[MemoryPrimitive, list[dict]] = {
        MemoryPrimitive.ENCODER: [
            {"name": "TransformerEncoder", "hyperparams": {"dim": 768, "layers": 6}},
            {"name": "E5MistralEncoder", "hyperparams": {"dim": 4096, "layers": 32}},
            {"name": "BGEM3Encoder", "hyperparams": {"dim": 2048, "layers": 24}},
            {"name": "GritLMEncoder", "hyperparams": {"dim": 4096, "layers": 32}},
        ],
        MemoryPrimitive.STORE: [
            {"name": "VectorStore_FAISS", "hyperparams": {"index": "IVF", "nlist": 100}},
            {"name": "VectorStore_ChromaDB", "hyperparams": {"distance": "cosine"}},
            {"name": "GraphStore_Neo4j", "hyperparams": {"max_nodes": 100000}},
            {"name": "KeyValueStore_Redis", "hyperparams": {"ttl_seconds": 86400}},
            {"name": "HierarchicalStore", "hyperparams": {"levels": 3}},
        ],
        MemoryPrimitive.RETRIEVER: [
            {"name": "HybridRetriever", "hyperparams": {"semantic_weight": 0.6}},
            {"name": "KeywordBM25Retriever", "hyperparams": {"k1": 1.2, "b": 0.75}},
            {"name": "GraphTraversalRetriever", "hyperparams": {"max_hops": 3}},
            {"name": "ActiveReconstructionRetriever", "hyperparams": {"max_depth": 5}},
            {"name": "EnsembleVotingRetriever", "hyperparams": {"n_models": 3}},
        ],
        MemoryPrimitive.MANAGER: [
            {"name": "TemporalDecayManager", "hyperparams": {"half_life_hours": 24}},
            {"name": "ImportanceWeightedManager", "hyperparams": {"min_importance": 0.1}},
            {"name": "LFUCacheManager", "hyperparams": {"max_size": 10000}},
            {"name": "SlidingWindowManager", "hyperparams": {"window_size": 1000}},
        ],
        MemoryPrimitive.CONSOLIDATOR: [
            {"name": "TemporalConsolidator", "hyperparams": {"interval_seconds": 3600}},
            {"name": "ImportanceBasedConsolidator", "hyperparams": {"threshold": 0.7}},
            {"name": "HierarchicalConsolidator", "hyperparams": {"levels": 3}},
        ],
    }

    def __init__(self, seed: int = 42):
        self._rng = random.Random(seed)
        self._used_block_ids: set[str] = set()

    def sample_primitive(self, primitive_type: MemoryPrimitive) -> PrimitiveBlock:
        """从库中随机采样一个原语。"""
        candidates = self.PRIMITIVE_LIBRARY.get(primitive_type, [])
        if not candidates:
            return PrimitiveBlock(
                block_id=self._gen_block_id(),
                primitive_type=primitive_type,
                name="NullPrimitive",
                code_snippet="# No matching primitive found",
            )

        candidate = self._rng.choice(candidates)
        bid = self._gen_block_id()
        return PrimitiveBlock(
            block_id=bid,
            primitive_type=primitive_type,
            name=candidate["name"],
            code_snippet=self._generate_snippet(candidate["name"], primitive_type),
            hyperparameters=copy.deepcopy(candidate["hyperparams"]),
        )

    def sample_individual(self) -> CodeIndividual:
        """随机采样一个完整代码个体。"""
        blocks = []
        order = []

        # 每个类型至少一个
        for ptype in [MemoryPrimitive.ENCODER, MemoryPrimitive.STORE,
                       MemoryPrimitive.RETRIEVER, MemoryPrimitive.MANAGER]:
            block = self.sample_primitive(ptype)
            blocks.append(block)
            order.append(block.block_id)

        # 可选巩固器
        if self._rng.random() > 0.5:
            block = self.sample_primitive(MemoryPrimitive.CONSOLIDATOR)
            blocks.append(block)
            order.append(block.block_id)

        return CodeIndividual(
            individual_id=self._gen_ind_id(),
            blocks=blocks,
            execution_order=order,
        )

    def initialize_population(self, size: int) -> list[CodeIndividual]:
        """初始化种群。"""
        return [self.sample_individual() for _ in range(size)]

    def _generate_snippet(self, name: str, ptype: MemoryPrimitive) -> str:
        snippet = f"# {name} — {ptype.value} primitive for Trinity Memory\n"
        snippet += f"# Auto-generated by CodeSearchSpace\n\n"
        snippet += "import logging\n"
        snippet += "logger = logging.getLogger(__name__)\n\n"
        snippet += f"class {name}:\n"
        snippet += f'    """{name} — {ptype.value} primitive."""\n'
        snippet += "    def __init__(self, **kwargs):\n"
        snippet += "        self.config = kwargs\n"
        snippet += "        logger.info(f'Initialized %s', self.__class__.__name__)\n\n"
        snippet += "    def execute(self, input_data):\n"
        snippet += f"        # [{ptype.value}] Transform input\n"
        snippet += "        return input_data\n"
        return snippet

    def _gen_block_id(self) -> str:
        while True:
            bid = f"block_{uuid.uuid4().hex[:8]}"
            if bid not in self._used_block_ids:
                self._used_block_ids.add(bid)
                return bid

    def _gen_ind_id(self) -> str:
        return f"ind_{uuid.uuid4().hex[:8]}"


# ══════════════════════════════════════════════════════════════════════
# 代码评估器
# ══════════════════════════════════════════════════════════════════════

class CodeEvaluator:
    """在目标任务上运行并评分生成代码。"""

    def __init__(self, accuracy_weight: float = 0.4, latency_weight: float = 0.2,
                 memory_weight: float = 0.15, complexity_weight: float = 0.1,
                 generalization_weight: float = 0.15):
        self.accuracy_weight = accuracy_weight
        self.latency_weight = latency_weight
        self.memory_weight = memory_weight
        self.complexity_weight = complexity_weight
        self.generalization_weight = generalization_weight
        self._evaluation_cache: dict[str, EvaluationResult] = {}

    def evaluate(self, individual: CodeIndividual,
                 task_baseline: dict[EvaluationMetric, float] | None = None) -> EvaluationResult:
        """评估代码个体。

        Args:
            individual: 待评估代码个体
            task_baseline: 任务基准指标 (用于归一化)

        Returns:
            EvaluationResult: 评估结果
        """
        # 模拟评估 — 生产环境应实际运行代码
        metrics = self._simulate_evaluation(individual)

        # 如果提供了基准，进行归一化
        if task_baseline:
            normalized = {}
            for metric, value in metrics.items():
                baseline = task_baseline.get(metric, value or 1.0)
                if baseline:
                    normalized[metric] = round(value / baseline, 4)
            metrics = normalized

        # 综合得分
        composite = (
            metrics.get(EvaluationMetric.ACCURACY, 0.5) * self.accuracy_weight
            + (1.0 - min(metrics.get(EvaluationMetric.LATENCY, 0.5), 1.0)) * self.latency_weight
            + (1.0 - min(metrics.get(EvaluationMetric.MEMORY_USAGE, 0.5), 1.0)) * self.memory_weight
            + (1.0 - min(metrics.get(EvaluationMetric.CODE_COMPLEXITY, 0.5), 1.0)) * self.complexity_weight
            + metrics.get(EvaluationMetric.GENERALIZATION, 0.5) * self.generalization_weight
        )

        result = EvaluationResult(
            individual=individual,
            metrics=metrics,
            composite_score=round(composite, 4),
        )
        individual.fitness = composite

        self._evaluation_cache[individual.individual_id] = result
        return result

    def _simulate_evaluation(self, individual: CodeIndividual) -> dict[EvaluationMetric, float]:
        """基于代码结构模拟评估。"""
        block_count = len(individual.blocks)
        primitive_types = {b.primitive_type for b in individual.blocks}

        # 精度：所有 4 种核心原语都存在时最高
        core_types = {MemoryPrimitive.ENCODER, MemoryPrimitive.STORE,
                      MemoryPrimitive.RETRIEVER, MemoryPrimitive.MANAGER}
        acc = len(primitive_types & core_types) / len(core_types)
        if MemoryPrimitive.CONSOLIDATOR in primitive_types:
            acc = min(1.0, acc + 0.05)

        # 延迟：block 越少越快
        lat = max(0.1, 1.0 - block_count * 0.08)

        # 内存使用
        mem = min(1.0, block_count * 0.12)

        # 复杂度
        comp = min(1.0, block_count * 0.06)

        # 泛化：多样性越高越好
        gen = len(primitive_types) / len(MemoryPrimitive)

        return {
            EvaluationMetric.ACCURACY: round(acc, 4),
            EvaluationMetric.LATENCY: round(lat, 4),
            EvaluationMetric.MEMORY_USAGE: round(mem, 4),
            EvaluationMetric.CODE_COMPLEXITY: round(comp, 4),
            EvaluationMetric.GENERALIZATION: round(gen, 4),
        }

    def get_evaluation_history(self) -> dict[str, float]:
        return {k: v.composite_score for k, v in self._evaluation_cache.items()}


# ══════════════════════════════════════════════════════════════════════
# MetaAgent — 元学习进化主循环
# ══════════════════════════════════════════════════════════════════════

class MetaAgent:
    """在代码空间中搜索最优记忆逻辑的元代理。

    通过变异+交叉产生新一代代码，在目标任务上评分后选择精英。
    """

    def __init__(self, search_space: CodeSearchSpace | None = None,
                 population_size: int = 20, max_generations: int = 10,
                 elite_size: int = 3, mutation_rate: float = 0.3,
                 crossover_rate: float = 0.5, seed: int = 42):
        self.search_space = search_space or CodeSearchSpace(seed=seed)
        self.population_size = population_size
        self.max_generations = max_generations
        self.elite_size = elite_size
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        self._rng = random.Random(seed)
        self.evaluator = CodeEvaluator()
        self._best_individual: CodeIndividual | None = None
        self._best_result: EvaluationResult | None = None
        self._evolution_history: list[EvolutionGeneration] = []

    def evolve_code(self, task_baseline: dict[EvaluationMetric, float] | None = None,
                    initial_population: list[CodeIndividual] | None = None) -> tuple[CodeIndividual, EvaluationResult]:
        """通过变异+交叉进化最优代码个体。

        Args:
            task_baseline: 任务基准指标
            initial_population: 初始种群 (可选)

        Returns:
            (最优个体, 评估结果)
        """
        # 初始化种群
        if initial_population:
            population = initial_population
        else:
            population = self.search_space.initialize_population(self.population_size)

        for gen in range(self.max_generations):
            # 评估
            results = [self.evaluator.evaluate(ind, task_baseline) for ind in population]
            results.sort(key=lambda r: r.composite_score, reverse=True)

            best_of_gen = results[0]
            avg = sum(r.composite_score for r in results) / len(results)
            mutations_applied = []
            for r in results:
                mutations_applied.extend(r.individual.mutation_history)

            self._evolution_history.append(EvolutionGeneration(
                generation=gen,
                best_score=best_of_gen.composite_score,
                avg_score=avg,
                population_size=len(population),
                mutations_applied=list(set(mutations_applied)),
                best_individual_id=best_of_gen.individual.individual_id,
            ))

            # 更新全局最优
            if self._best_result is None or best_of_gen.composite_score > self._best_result.composite_score:
                self._best_result = best_of_gen
                self._best_individual = best_of_gen.individual

            # 收敛检测
            if best_of_gen.composite_score >= 0.95:
                break

            # 选择精英
            elites = [r.individual for r in results[:self.elite_size]]
            next_population = list(elites)

            # 生成下一代
            while len(next_population) < self.population_size:
                parent = self._tournament_select(results)

                if self._rng.random() < self.crossover_rate and len(results) >= 2:
                    parent_b = self._tournament_select(results, exclude=parent)
                    if parent_b:
                        child = self._crossover(parent, parent_b)
                        child.generation = gen + 1
                        next_population.append(child)
                        continue

                # 变异
                child = self._mutate(parent)
                child.generation = gen + 1
                next_population.append(child)

            population = next_population[:self.population_size]

        # 未收敛但有空结果
        if self._best_individual is None:
            self._best_individual = population[0] if population else self.search_space.sample_individual()
            self._best_result = EvaluationResult(
                individual=self._best_individual,
                composite_score=0.0,
            )

        return self._best_individual, self._best_result

    def _mutate(self, individual: CodeIndividual) -> CodeIndividual:
        """基因变异。"""
        child = CodeIndividual(
            individual_id=f"ind_{uuid.uuid4().hex[:8]}",
            generation=individual.generation,
            blocks=copy.deepcopy(individual.blocks),
            execution_order=list(individual.execution_order),
            parent_ids=[individual.individual_id],
            mutation_history=list(individual.mutation_history),
        )

        # 随机选择变异操作
        available_ops = list(CodeMutationOp)
        available_ops.remove(CodeMutationOp.CROSSOVER)  # crossover 由外层处理
        op = self._rng.choice(available_ops)

        if op == CodeMutationOp.ADD_PRIMITIVE:
            ptype = self._rng.choice(list(MemoryPrimitive))
            block = self.search_space.sample_primitive(ptype)
            child.blocks.append(block)
            child.execution_order.append(block.block_id)

        elif op == CodeMutationOp.REMOVE_PRIMITIVE and len(child.blocks) > 3:
            idx = self._rng.randrange(len(child.blocks))
            removed = child.blocks.pop(idx)
            if removed.block_id in child.execution_order:
                child.execution_order.remove(removed.block_id)

        elif op == CodeMutationOp.REPLACE_PRIMITIVE and child.blocks:
            idx = self._rng.randrange(len(child.blocks))
            old_block = child.blocks[idx]
            new_block = self.search_space.sample_primitive(old_block.primitive_type)
            child.blocks[idx] = new_block
            # 更新执行顺序
            if old_block.block_id in child.execution_order:
                pos = child.execution_order.index(old_block.block_id)
                child.execution_order[pos] = new_block.block_id

        elif op == CodeMutationOp.SWAP_ORDER and len(child.execution_order) >= 2:
            i, j = self._rng.sample(range(len(child.execution_order)), 2)
            child.execution_order[i], child.execution_order[j] = child.execution_order[j], child.execution_order[i]

        elif op == CodeMutationOp.RETUNE_HYPERPARAM and child.blocks:
            block = self._rng.choice(child.blocks)
            for key in block.hyperparameters:
                if isinstance(block.hyperparameters[key], (int, float)):
                    factor = self._rng.uniform(0.5, 2.0)
                    block.hyperparameters[key] = round(block.hyperparameters[key] * factor, 2)

        child.mutation_history.append(op)
        return child

    def _crossover(self, parent_a: CodeIndividual,
                   parent_b: CodeIndividual) -> CodeIndividual:
        """交叉重组。"""
        child = CodeIndividual(
            individual_id=f"ind_{uuid.uuid4().hex[:8]}",
            parent_ids=[parent_a.individual_id, parent_b.individual_id],
            mutation_history=[CodeMutationOp.CROSSOVER],
        )

        # 均匀交叉：每个位置随机选择父本
        max_len = max(len(parent_a.blocks), len(parent_b.blocks))
        seen_types: set[MemoryPrimitive] = set()

        for i in range(max_len):
            if self._rng.random() < 0.5 and i < len(parent_a.blocks):
                block = copy.deepcopy(parent_a.blocks[i])
            elif i < len(parent_b.blocks):
                block = copy.deepcopy(parent_b.blocks[i])
            else:
                continue

            if block.primitive_type not in seen_types:
                seen_types.add(block.primitive_type)
                child.blocks.append(block)
                child.execution_order.append(block.block_id)

        return child

    def _tournament_select(self, results: list[EvaluationResult],
                           exclude: CodeIndividual | None = None) -> CodeIndividual:
        candidates = [r for r in results if r.individual != exclude]
        if not candidates:
            candidates = results
        k = min(3, len(candidates))
        tournament = self._rng.sample(candidates, k)
        return max(tournament, key=lambda r: r.composite_score).individual

    def get_stats(self) -> dict:
        return {
            "generations_completed": len(self._evolution_history),
            "best_composite_score": self._best_result.composite_score if self._best_result else 0.0,
            "best_individual_id": self._best_individual.individual_id if self._best_individual else "",
            "best_block_count": len(self._best_individual.blocks) if self._best_individual else 0,
            "population_size": self.population_size,
            "mutation_rate": self.mutation_rate,
            "crossover_rate": self.crossover_rate,
            "evaluation_cache_size": len(self.evaluator.get_evaluation_history()),
        }

    def get_evolution_history(self) -> list[EvolutionGeneration]:
        return list(self._evolution_history)


# ══════════════════════════════════════════════════════════════════════
# 模块自测
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("Meta-Learning Code Generation — Self Test")
    print("=" * 60)

    # 搜索空间
    space = CodeSearchSpace(seed=0)
    print(f"\n[CodeSearchSpace] {sum(len(v) for v in space.PRIMITIVE_LIBRARY.values())} primitives in library")

    # 采样个体
    individual = space.sample_individual()
    print(f"\n[Sampled Individual] {individual.individual_id}")
    print(f"  Blocks: {len(individual.blocks)}")
    for block in individual.blocks:
        print(f"    - [{block.primitive_type.value}] {block.name}: {list(block.hyperparameters.keys())}")

    # 评估
    evaluator = CodeEvaluator()
    result = evaluator.evaluate(individual)
    print(f"\n[Evaluation] composite={result.composite_score:.4f}")
    for metric, value in result.metrics.items():
        print(f"  {metric.value}: {value:.4f}")

    # 进化
    meta = MetaAgent(population_size=15, max_generations=5, elite_size=2, seed=0)
    best_ind, best_result = meta.evolve_code()

    print(f"\n[Evolved Best]")
    print(f"  Individual: {best_ind.individual_id}")
    print(f"  Generation: {best_ind.generation}")
    print(f"  Composite score: {best_result.composite_score:.4f}")
    print(f"  Blocks: {len(best_ind.blocks)}")
    for block in best_ind.blocks:
        print(f"    - [{block.primitive_type.value}] {block.name}")
    print(f"  Mutation history: {[m.value for m in best_ind.mutation_history]}")

    print(f"\n[Evolution History]")
    for gen in meta.get_evolution_history():
        print(f"  Gen {gen.generation}: best={gen.best_score:.4f}, avg={gen.avg_score:.4f}, pop={gen.population_size}")
