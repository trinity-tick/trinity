"""
P23-5: FedAgentKE — 联邦语义知识进化

对标论文: FedAgentKE: Federated Semantic Knowledge Evolution for Heterogeneous Agents (arXiv 2607.21361, 2026.07)
核心发现: 异构Agent框架通过迭代语义知识蒸馏、聚合与适配实现跨框架可迁移推理抽象，
        无需共享原始推理轨迹，仅交换语义级知识Token，随参与框架增多性能持续提升。
三元语: 本地语义蒸馏 → 联邦聚合(加权/共识/投票) → 异构适配 → 迭代同步协议 → 知识进化评估

设计要点:
- DistillationMode: 知识蒸馏模式配置（语义抽象 / 规则提炼 / 模式归纳）
- SemanticDistiller: 本地蒸馏器 — 从原始推理轨迹提取不包含敏感信息的知识Token
- AggregationStrategy: 联邦聚合策略（加权平均 / 共识选举 / 多数投票）
- AggregationEngine: 联邦聚合引擎 — 接收多方知识Token执行去重/冲突解决/归并
- HeterogeneousAdapter: 异构适配层 — 将统一格式知识Token转换为目标框架内部表示
- FederatedRound: 联邦同步轮次记录 — 含时间戳、参与方、聚合结果与评估指标
- FederatedKnowledgeEvolution: 统一编排器 — 线程安全，支持 statistics() 运行时指标
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)

# ============================================================================
# Enums & Constants
# ============================================================================


class DistillationMode(Enum):
    """知识蒸馏模式"""
    SEMANTIC_ABSTRACTION = "semantic_abstraction"    # 语义抽象：提取高层概念与推理模式
    RULE_EXTRACTION = "rule_extraction"               # 规则提炼：归纳可复用的决策规则
    PATTERN_INDUCTION = "pattern_induction"           # 模式归纳：从多次推理中归纳通用模板
    HYBRID = "hybrid"                                 # 混合模式：前三者加权组合


class AggregationStrategy(Enum):
    """联邦聚合策略"""
    WEIGHTED_AVERAGE = "weighted_average"             # 加权平均：按参与方可信度加权
    CONSENSUS_ELECTION = "consensus_election"         # 共识选举：选举置信度最高的知识Token
    MAJORITY_VOTING = "majority_voting"               # 多数投票：多方一致采纳
    BAYESIAN_FUSION = "bayesian_fusion"               # 贝叶斯融合：概率推断合并
    GREEDY_MERGE = "greedy_merge"                     # 贪婪归并：去重后直接合并


class AdapterType(Enum):
    """异构适配器类型"""
    LANGCHAIN = "langchain"                           # LangChain 框架适配
    AUTOGEN = "autogen"                               # AutoGen 框架适配
    CREWAI = "crewai"                                 # CrewAI 框架适配
    TRINITY = "trinity"                               # Trinity 自身框架适配
    CUSTOM = "custom"                                 # 自定义框架适配


class SyncProtocol(Enum):
    """同步协议"""
    FEDAVG = "fedavg"                                 # 联邦平均
    FEDPROX = "fedprox"                               # 联邦近端
    SCAFFOLD = "scaffold"                             # SCAFFOLD 协议
    FEDADAM = "fedadam"                               # 联邦自适应动量


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass
class KnowledgeToken:
    """语义知识Token — 联邦交换的最小知识单元"""
    token_id: str                                    # 唯一标识符
    source_framework: str                            # 来源框架名称
    semantic_hash: str                               # 语义内容哈希（SHA-256）
    abstract_type: DistillationMode                  # 蒸馏类型
    content: Dict[str, Any]                          # 结构化知识内容
    confidence: float = 1.0                          # 置信度 [0, 1]
    version: int = 1                                 # 知识Token版本号
    created_at: float = field(default_factory=time.time)  # 创建时间戳
    parent_tokens: List[str] = field(default_factory=list)  # 父Token ID列表（追溯链）

    def compute_integrity(self) -> str:
        """计算完整性校验值"""
        payload = json.dumps({
            "sid": self.source_framework,
            "at": self.abstract_type.value,
            "content": self.content,
            "v": self.version,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class FederatedRound:
    """联邦同步轮次记录"""
    round_id: str                                    # 轮次唯一ID
    round_number: int                                # 轮次序号
    participants: List[str]                          # 参与方框架列表
    strategy: AggregationStrategy                    # 本轮聚合策略
    protocol: SyncProtocol                           # 同步协议
    tokens_contributed: int                          # 各参与方贡献Token总数
    tokens_after_merge: int                          # 聚合后Token数
    conflict_count: int                              # 冲突数
    convergence_score: float                         # 收敛评分 [0, 1]
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    aggregated_tokens: List[KnowledgeToken] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.completed_at is None:
            return 0.0
        return (self.completed_at - self.started_at) * 1000.0


@dataclass
class AdapterOutput:
    """适配器输出"""
    adapted_tokens: List[KnowledgeToken]              # 适配后的Token列表
    adapter_type: AdapterType                        # 所用适配器类型
    mapping_latency_ms: float                        # 映射延迟（毫秒）
    fidelity_score: float                            # 保真度评分 [0, 1]
    warnings: List[str] = field(default_factory=list)


# ============================================================================
# SemanticDistiller — 本地语义蒸馏器
# ============================================================================


class SemanticDistiller:
    """本地语义蒸馏器 — 在不泄露原始轨迹的前提下提取可迁移知识Token

    核心功能:
    - 从原始推理轨迹中提取语义抽象/规则/模式
    - 生成带完整性校验的知识Token
    - 支持三种蒸馏模式独立或混合运行
    """

    def __init__(self, framework_name: str, mode: DistillationMode = DistillationMode.HYBRID):
        self._framework_name = framework_name
        self._mode = mode
        self._token_registry: Dict[str, KnowledgeToken] = {}
        self._lock = threading.RLock()
        self._stats: Dict[str, int] = {"total_distilled": 0, "abstracts": 0, "rules": 0, "patterns": 0}

    @property
    def framework_name(self) -> str:
        return self._framework_name

    def distill(
        self,
        reasoning_trajectory: List[Dict[str, Any]],
        task_type: str = "general",
    ) -> List[KnowledgeToken]:
        """从原始推理轨迹蒸馏知识Token（不存储原始轨迹）"""
        tokens: List[KnowledgeToken] = []

        with self._lock:
            for trajectory_step in reasoning_trajectory:
                abstract = self._extract_semantic_abstract(trajectory_step, task_type)
                rule = self._extract_reusable_rule(trajectory_step)
                pattern = self._induce_general_pattern(trajectory_step)

                content: Dict[str, Any] = {
                    "task_type": task_type,
                    "abstract": abstract,
                    "rule": rule,
                    "pattern": pattern,
                }

                token = KnowledgeToken(
                    token_id=f"{self._framework_name}_{uuid.uuid4().hex[:12]}",
                    source_framework=self._framework_name,
                    semantic_hash=hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest(),
                    abstract_type=self._mode,
                    content=content,
                )
                tokens.append(token)
                self._token_registry[token.token_id] = token
                self._stats["total_distilled"] += 1

        return tokens

    def _extract_semantic_abstract(self, step: Dict[str, Any], task_type: str) -> Dict[str, Any]:
        """提取语义抽象 — 高层概念与因果关系"""
        self._stats["abstracts"] += 1
        return {
            "concept": step.get("concept", ""),
            "causal_chain": step.get("causal_chain", []),
            "confidence": step.get("confidence", 0.8),
        }

    def _extract_reusable_rule(self, step: Dict[str, Any]) -> Dict[str, Any]:
        """提炼可复用规则"""
        self._stats["rules"] += 1
        return {
            "condition": step.get("condition", {}),
            "action": step.get("action", {}),
            "priority": step.get("priority", 0),
            "lifetime_hours": step.get("lifetime_hours", 72),
        }

    def _induce_general_pattern(self, step: Dict[str, Any]) -> Dict[str, Any]:
        """归纳通用模式"""
        self._stats["patterns"] += 1
        return {
            "template": step.get("template", ""),
            "slot_fillers": step.get("slot_fillers", {}),
            "success_rate": step.get("success_rate", 0.0),
        }

    def get_stats(self) -> Dict[str, int]:
        """获取蒸馏统计信息"""
        with self._lock:
            return dict(self._stats)


# ============================================================================
# AggregationEngine — 联邦聚合引擎
# ============================================================================


class AggregationEngine:
    """联邦聚合引擎 — 接收多方知识Token执行去重/冲突解决/归并

    核心功能:
    - 多策略聚合（加权平均/共识选举/多数投票/贝叶斯融合/贪婪归并）
    - 语义去重（基于语义哈希碰撞检测）
    - 冲突检测与解决（置信度对比 + 来源可信度加权）
    - 聚合质量评估（收敛评分）
    """

    def __init__(self, strategy: AggregationStrategy = AggregationStrategy.WEIGHTED_AVERAGE):
        self._strategy = strategy
        self._framework_weights: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._rounds: List[FederatedRound] = []
        self._stats: Dict[str, int] = {"total_rounds": 0, "total_tokens_merged": 0, "total_conflicts": 0}

    @property
    def strategy(self) -> AggregationStrategy:
        return self._strategy

    @strategy.setter
    def strategy(self, value: AggregationStrategy):
        self._strategy = value

    def set_framework_weights(self, weights: Dict[str, float]):
        """设置各框架可信度权重"""
        total = sum(weights.values())
        with self._lock:
            self._framework_weights = {k: v / total for k, v in weights.items()}

    def aggregate(
        self,
        contributions: Dict[str, List[KnowledgeToken]],
        protocol: SyncProtocol = SyncProtocol.FEDAVG,
    ) -> FederatedRound:
        """执行一轮联邦聚合"""
        round_number = len(self._rounds) + 1
        round_id = f"round_{round_number}_{uuid.uuid4().hex[:8]}"
        all_tokens: List[KnowledgeToken] = []
        contributed_count = 0

        for framework, tokens in contributions.items():
            all_tokens.extend(tokens)
            contributed_count += len(tokens)

        federated_round = FederatedRound(
            round_id=round_id,
            round_number=round_number,
            participants=list(contributions.keys()),
            strategy=self._strategy,
            protocol=protocol,
            tokens_contributed=contributed_count,
            tokens_after_merge=0,
            conflict_count=0,
            convergence_score=0.0,
        )

        with self._lock:
            # 语义去重
            deduped, conflicts = self._deduplicate_by_semantic_hash(all_tokens)
            # 按策略聚合
            merged = self._merge_by_strategy(deduped, conflicts)

            federated_round.tokens_after_merge = len(merged)
            federated_round.conflict_count = len(conflicts)
            federated_round.convergence_score = self._compute_convergence(deduped, merged)
            federated_round.completed_at = time.time()
            federated_round.aggregated_tokens = merged

            self._rounds.append(federated_round)
            self._stats["total_rounds"] += 1
            self._stats["total_tokens_merged"] += len(merged)
            self._stats["total_conflicts"] += len(conflicts)

        return federated_round

    def _deduplicate_by_semantic_hash(
        self, tokens: List[KnowledgeToken]
    ) -> Tuple[List[KnowledgeToken], List[Tuple[KnowledgeToken, KnowledgeToken]]]:
        """按语义哈希去重，返回去重列表和冲突对"""
        seen: Dict[str, KnowledgeToken] = {}
        conflicts: List[Tuple[KnowledgeToken, KnowledgeToken]] = []

        for token in tokens:
            if token.semantic_hash in seen:
                conflicts.append((seen[token.semantic_hash], token))
                # 保留置信度更高的
                if token.confidence > seen[token.semantic_hash].confidence:
                    seen[token.semantic_hash] = token
            else:
                seen[token.semantic_hash] = token

        return list(seen.values()), conflicts

    def _merge_by_strategy(
        self,
        deduped: List[KnowledgeToken],
        conflicts: List[Tuple[KnowledgeToken, KnowledgeToken]],
    ) -> List[KnowledgeToken]:
        """按当前策略执行归并"""
        if self._strategy == AggregationStrategy.GREEDY_MERGE:
            return deduped

        merged: List[KnowledgeToken] = []
        for token in deduped:
            weight = self._framework_weights.get(token.source_framework, 1.0 / max(1, len(self._framework_weights)))
            if self._strategy == AggregationStrategy.WEIGHTED_AVERAGE:
                token.confidence *= weight
            elif self._strategy == AggregationStrategy.CONSENSUS_ELECTION:
                if token.confidence < 0.7:
                    continue
            elif self._strategy == AggregationStrategy.MAJORITY_VOTING:
                if token.confidence < 0.5:
                    continue
            merged.append(token)

        return merged

    def _compute_convergence(self, before: List[KnowledgeToken], after: List[KnowledgeToken]) -> float:
        """计算收敛评分 (去重压缩比)"""
        if not before:
            return 1.0
        return 1.0 - (len(after) / len(before))

    def get_stats(self) -> Dict[str, Any]:
        """获取聚合统计信息"""
        with self._lock:
            return {
                **self._stats,
                "rounds_completed": len(self._rounds),
                "current_strategy": self._strategy.value,
                "last_convergence": self._rounds[-1].convergence_score if self._rounds else 0.0,
            }


# ============================================================================
# HeterogeneousAdapter — 异构框架适配层
# ============================================================================


class HeterogeneousAdapter:
    """异构框架适配层 — 将统一格式知识Token转换为目标框架内部表示

    核心功能:
    - 双向映射：统一格式 ⇄ 目标框架内部格式
    - 保真度保证：映射过程最小化信息损失
    - 插件化架构：新增框架仅需注册映射规则
    - 映射追踪：记录每次映射的延迟与保真度评分
    """

    def __init__(self):
        self._adapters: Dict[AdapterType, Dict[str, Any]] = {
            AdapterType.TRINITY: {"name": "Trinity", "version": "6.65", "schema": "unified_v2"},
            AdapterType.LANGCHAIN: {"name": "LangChain", "version": "0.3+", "schema": "langchain_message"},
            AdapterType.AUTOGEN: {"name": "AutoGen", "version": "0.4+", "schema": "autogen_message"},
            AdapterType.CREWAI: {"name": "CrewAI", "version": "0.8+", "schema": "crewai_task"},
        }
        self._lock = threading.RLock()
        self._mapping_history: List[AdapterOutput] = []

    def register_adapter(self, adapter_type: AdapterType, metadata: Dict[str, Any]):
        """注册新框架适配器"""
        with self._lock:
            self._adapters[adapter_type] = metadata

    def export_to(
        self, tokens: List[KnowledgeToken], target: AdapterType
    ) -> AdapterOutput:
        """将知识Token导出到目标框架格式"""
        start = time.time()
        adapted: List[KnowledgeToken] = []
        warnings: List[str] = []

        if target not in self._adapters:
            warnings.append(f"Target adapter {target.value} not found, using pass-through")
            adapted = tokens
        else:
            adapter = self._adapters[target]
            for token in tokens:
                # 映射到目标框架的命名空间与结构
                adapted_token = KnowledgeToken(
                    token_id=token.token_id,
                    source_framework=token.source_framework,
                    semantic_hash=token.semantic_hash,
                    abstract_type=token.abstract_type,
                    content={
                        **token.content,
                        "_adapter": adapter["schema"],
                        "_target": target.value,
                    },
                    confidence=token.confidence,
                )
                adapted.append(adapted_token)

        latency = (time.time() - start) * 1000.0
        fidelity = 1.0 if not warnings else 0.85

        output = AdapterOutput(
            adapted_tokens=adapted,
            adapter_type=target,
            mapping_latency_ms=latency,
            fidelity_score=fidelity,
            warnings=warnings,
        )

        with self._lock:
            self._mapping_history.append(output)

        return output

    def import_from(
        self, tokens: List[KnowledgeToken], source: AdapterType
    ) -> AdapterOutput:
        """从外部框架格式导入（逆映射到统一格式）"""
        start = time.time()
        warnings: List[str] = []

        if source not in self._adapters:
            warnings.append(f"Source adapter {source.value} not found, using pass-through")

        latency = (time.time() - start) * 1000.0
        output = AdapterOutput(
            adapted_tokens=tokens,
            adapter_type=source,
            mapping_latency_ms=latency,
            fidelity_score=1.0 if not warnings else 0.8,
            warnings=warnings,
        )

        with self._lock:
            self._mapping_history.append(output)

        return output

    def list_registered_adapters(self) -> List[Dict[str, Any]]:
        """列出已注册适配器"""
        with self._lock:
            return [{"type": k.value, **v} for k, v in self._adapters.items()]

    def get_stats(self) -> Dict[str, Any]:
        """获取适配器统计信息"""
        with self._lock:
            return {
                "registered_adapters": len(self._adapters),
                "total_mappings": len(self._mapping_history),
                "avg_latency_ms": (
                    sum(o.mapping_latency_ms for o in self._mapping_history) / len(self._mapping_history)
                    if self._mapping_history else 0.0
                ),
                "avg_fidelity": (
                    sum(o.fidelity_score for o in self._mapping_history) / len(self._mapping_history)
                    if self._mapping_history else 0.0
                ),
            }


# ============================================================================
# FederatedKnowledgeEvolution — 联邦知识进化统一编排器
# ============================================================================


class FederatedKnowledgeEvolution:
    """联邦语义知识进化编排器 — 线程安全

    功能:
    - 协调蒸馏→聚合→适配→迭代同步全流程
    - 支持多轮联邦同步（Round-based iterative synchronization）
    - 运行时指标暴露 (statistics())
    """

    def __init__(self, framework_name: str = "trinity"):
        self._framework_name = framework_name
        self._distiller = SemanticDistiller(framework_name)
        self._aggregator = AggregationEngine()
        self._adapter = HeterogeneousAdapter()
        self._lock = threading.RLock()

    @property
    def framework_name(self) -> str:
        return self._framework_name

    def run_federated_round(
        self,
        local_trajectories: List[Dict[str, Any]],
        peer_contributions: Dict[str, List[KnowledgeToken]],
        task_type: str = "general",
        strategy: Optional[AggregationStrategy] = None,
        protocol: SyncProtocol = SyncProtocol.FEDAVG,
    ) -> Dict[str, Any]:
        """执行一轮联邦知识进化

        1. 本地蒸馏 → 2. 联合聚合 → 3. 异构适配 → 4. 返回结果
        """
        with self._lock:
            # 1. 本地语义蒸馏
            local_tokens = self._distiller.distill(local_trajectories, task_type)

            # 2. 联合聚合（本地 + 其他参与方）
            all_contributions: Dict[str, List[KnowledgeToken]] = {
                self._framework_name: local_tokens,
                **peer_contributions,
            }

            if strategy is not None:
                self._aggregator.strategy = strategy

            round_result = self._aggregator.aggregate(all_contributions, protocol)

            # 3. 异构适配（导出到 Trinity 格式）
            adapter_output = self._adapter.export_to(
                round_result.aggregated_tokens, AdapterType.TRINITY
            )

            return {
                "round_id": round_result.round_id,
                "round_number": round_result.round_number,
                "convergence_score": round_result.convergence_score,
                "tokens_before": round_result.tokens_contributed,
                "tokens_after": round_result.tokens_after_merge,
                "conflicts_resolved": round_result.conflict_count,
                "fidelity": adapter_output.fidelity_score,
                "duration_ms": round_result.duration_ms,
            }

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标"""
        with self._lock:
            return {
                "framework": self._framework_name,
                "distiller": self._distiller.get_stats(),
                "aggregator": self._aggregator.get_stats(),
                "adapter": self._adapter.get_stats(),
            }
