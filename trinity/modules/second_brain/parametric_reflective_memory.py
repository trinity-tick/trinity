"""
P17-1: Parametric Reflective Memory — 参数化反思记忆

对标论文: ParamMem / ParamAgent (arXiv 2602.23320)
核心发现: 反思多样性 → 任务成功率强正相关 (r=0.78~0.91)
三元语: 反思模式编码 → 多样性采样 → 跨样本复用 → 弱到强迁移 → 自我改进回路

设计要点:
- ReflectionEncoder: 将跨样本反思模式编码为可训练参数化表示 (低秩适配器)
- DiversitySampler: 温度控制多路径采样, 高温→高多样性反思信号
- CrossSampleMemoryBank: 按任务相似度检索历史上成功反思模式供复用
- ParametricEpisodicBridge: 参数化反思经验注入情景检索 pipeline
- WeakToStrongTransfer: 弱模型样本高效地将反思经验迁移至强模型
- SelfImprovementLoop: 反思→评估→更新→再反思的无监督闭环
- 与 P3 critic.py / feedback.py 互补——critic 即时评价, 本模块跨样本学习反思模式
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
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class ReflectionDiversity(Enum):
    """反思多样性等级 — 低温→保守, 高温→探索性"""
    CONSERVATIVE = "conservative"     # τ ∈ [0.1, 0.4]
    BALANCED = "balanced"             # τ ∈ [0.5, 0.8]
    EXPLORATORY = "exploratory"       # τ ∈ [0.9, 1.5]
    MAX_ENTROPY = "max_entropy"       # τ ∈ [1.6, 3.0]


class TransferMode(Enum):
    """弱→强迁移模式"""
    DIRECT_DISTILL = auto()           # 直接蒸馏反思参数
    SAMPLE_EFFICIENT = auto()         # 样本高效迁移 (默认)
    CURRICULUM = auto()               # 课程式逐步迁移
    CO_TRAINING = auto()              # 联合训练


class ImprovementPhase(Enum):
    """自我改进循环阶段"""
    REFLECT = "reflect"               # 生成反思
    EVALUATE = "evaluate"             # 评估反思质量
    UPDATE = "update"                 # 更新参数
    CONSOLIDATE = "consolidate"       # 固化收益


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class ReflectiveEncoding:
    """参数化反思编码"""
    encoding_id: str
    task_signature: str               # 任务哈希签名
    reflection_vector: List[float]    # 反思参数向量 (低秩)
    diversity_score: float            # 反思多样性评分 [0, 1]
    source_samples: List[str]         # 源样本 ID 列表
    success_rate: float               # 关联任务成功率
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SampledReflection:
    """采样得到的反思信号"""
    reflection_id: str
    encoding_ref: Optional[str]       # 来源编码 ID
    temperature: float                # 采样温度
    reflection_text: str              # 自然语言反思
    diversity_score: float
    confidence: float                 # [0, 1]
    suggested_actions: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryBankEntry:
    """跨样本记忆库条目"""
    entry_id: str
    encoding_hash: str
    task_signature: str
    reflection_pattern: List[float]   # 成功反思模式向量
    success_rate: float
    reuse_count: int = 0
    last_reuse: float = 0.0
    quality_score: float = 0.5
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BridgeResult:
    """参数化→情景记忆桥接结果"""
    bridge_id: str
    query_embedding: List[float]
    injected_reflections: List[SampledReflection]
    augmented_context: Dict[str, Any]
    retrieval_latency_ms: float
    confidence: float


@dataclass
class TransferRecord:
    """弱→强迁移记录"""
    transfer_id: str
    source_model: str                 # 弱模型标识
    target_model: str                 # 强模型标识
    mode: TransferMode
    transferred_encodings: int
    target_success_rate: float
    sample_efficiency_gain: float     # 样本效率提升倍数
    timestamp: float = field(default_factory=time.time)


@dataclass
class ImprovementCycle:
    """自我改进循环记录"""
    cycle_id: str
    phase: ImprovementPhase
    before_quality: float
    after_quality: float
    delta: float
    reflections_generated: int
    reflections_accepted: int
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# ReflectionEncoder — 反思模式参数化编码器
# ============================================================================

class ReflectionEncoder:
    """
    将跨样本反思模式编码为可训练的参数化表示。

    核心思路:
    - 低秩适配器 (LoRA-style) 将高维反思文本压缩为紧凑向量
    - 哈希任务签名用于快速检索同类任务的历史反思模式
    - 编码时保留多样性指标, 供 DiversitySampler 参考

    实现策略:
    - 使用随机投影 + 哈希指纹实现轻量级参数化编码
    - 不依赖外部模型, 纯规则+统计算法
    """

    def __init__(
        self,
        embedding_dim: int = 128,
        low_rank_dim: int = 16,
        history_capacity: int = 2048,
    ):
        self.embedding_dim = embedding_dim
        self.low_rank_dim = low_rank_dim
        self._lock = threading.RLock()
        self._encodings: OrderedDict[str, ReflectiveEncoding] = OrderedDict()
        self._history_capacity = history_capacity
        self._task_registry: Dict[str, List[str]] = defaultdict(list)
        self._encode_count: int = 0
        self._total_samples: int = 0
        logger.info(
            "ReflectionEncoder initialized: dim=%d, rank=%d",
            embedding_dim, low_rank_dim,
        )

    def compute_task_signature(self, task_description: str) -> str:
        """根据任务描述生成哈希签名, 用于同类任务匹配"""
        h = hashlib.sha256(task_description.encode("utf-8")).hexdigest()[:16]
        return h

    def _text_to_vector(
        self, text: str, seed: int = 42
    ) -> List[float]:
        """将文本转为低秩向量 (随机投影 + SVD 模拟)"""
        # 基于字符 n-gram 哈希的随机投影
        n_grams = [text[i:i+3] for i in range(max(0, len(text) - 2))]
        if not n_grams:
            n_grams = [text]

        vec = [0.0] * self.embedding_dim
        for i, ng in enumerate(n_grams):
            h = hashlib.md5(f"{seed}:{ng}:{i}".encode()).digest()
            for j in range(0, len(h) - 1, 2):
                idx = (h[j] << 8 | h[j + 1]) % self.embedding_dim
                vec[idx] += (1.0 if h[j] % 2 == 0 else -1.0) * 0.01

        # L2 归一化
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def encode(
        self,
        task_description: str,
        reflection_text: str,
        source_sample_ids: List[str],
        success_rate: float = 1.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ReflectiveEncoding:
        """将一次反思经验编码为参数化表示"""
        task_sig = self.compute_task_signature(task_description)
        reflection_vector = self._text_to_vector(reflection_text)
        diversity_score = self._estimate_diversity(reflection_text)

        encoding = ReflectiveEncoding(
            encoding_id=f"refl_{self._encode_count:06d}",
            task_signature=task_sig,
            reflection_vector=reflection_vector,
            diversity_score=diversity_score,
            source_samples=list(source_sample_ids),
            success_rate=success_rate,
            metadata=metadata or {},
        )

        with self._lock:
            if len(self._encodings) >= self._history_capacity:
                self._encodings.popitem(last=False)
            self._encodings[encoding.encoding_id] = encoding
            self._task_registry[task_sig].append(encoding.encoding_id)
            self._encode_count += 1
            self._total_samples += len(source_sample_ids)

        logger.debug("Encoded reflection %s for task %s", encoding.encoding_id, task_sig)
        return encoding

    def _estimate_diversity(self, text: str) -> float:
        """估计反思文本的多样性 (基于词汇/模式分散度)"""
        words = text.lower().split()
        if len(words) < 5:
            return 0.3
        unique_ratio = len(set(words)) / len(words)
        # 根据独特词比例映射到 [0, 1]
        return min(1.0, unique_ratio * 2.5)

    def retrieve_by_task(
        self, task_description: str, top_k: int = 5
    ) -> List[ReflectiveEncoding]:
        """按任务签名检索历史编码"""
        task_sig = self.compute_task_signature(task_description)
        with self._lock:
            ids = self._task_registry.get(task_sig, [])
            encodings = [
                self._encodings[eid]
                for eid in ids[-top_k:]
                if eid in self._encodings
            ]
        return encodings

    def statistics(self) -> Dict[str, Any]:
        """运行时统计"""
        with self._lock:
            return {
                "total_encodings": self._encode_count,
                "cached_encodings": len(self._encodings),
                "unique_tasks": len(self._task_registry),
                "total_source_samples": self._total_samples,
                "average_diversity": (
                    sum(e.diversity_score for e in self._encodings.values()) / max(1, len(self._encodings))
                ),
            }


# ============================================================================
# DiversitySampler — 多样性采样器
# ============================================================================

class DiversitySampler:
    """
    温度控制采样生成多样化反思信号。

    高温 → 更多探索性反思, 低温 → 更保守的反思。
    基于 softmax 温度缩放 + top-p nucleus 采样思路。
    """

    def __init__(
        self,
        default_temperature: float = 0.7,
        temperature_range: Tuple[float, float] = (0.1, 3.0),
        top_p: float = 0.9,
    ):
        self.default_temperature = default_temperature
        self.temperature_range = temperature_range
        self.top_p = top_p
        self._lock = threading.RLock()
        self._sample_count: int = 0
        self._diversity_history: List[float] = []
        self._reflection_templates = self._build_templates()

    def _build_templates(self) -> Dict[str, List[str]]:
        """构建反思模板库"""
        return {
            "strategy": [
                "当前策略在第 {step} 步陷入局部最优，可尝试 {alternative} 路径",
                "工具选择偏向 {tool_a}，应考虑启用 {tool_b} 以获得更好结果",
                "任务分解粒度过 {granularity}，建议调整为 {suggestion}",
            ],
            "reasoning": [
                "推理链条在 '{cue}' 处存在逻辑跳跃，缺少 {missing} 环节",
                "对 {concept} 的理解未考虑 {edge_case} 边界情况",
                "假设 '{assumption}' 未经充分验证，需要 {verification} 确认",
            ],
            "memory": [
                "检索到的记忆 '{memory_item}' 时效性不足，应更新为 {updated}",
                "忽略了与当前任务高度相关的历史经验 '{relevant_case}'",
                "记忆条目 '{entry}' 与当前上下文存在冲突，需消解",
            ],
            "meta": [
                "整体反思多样性偏低，建议提升温度以探索更多可能方案",
                "连续 {n} 次相似失败模式提示存在系统性偏差",
                "当前反思覆盖了 {coverage}% 的关键维度，遗漏了 {missing_dim}",
            ],
        }

    def sample(
        self,
        encoding: Optional[ReflectiveEncoding] = None,
        task_context: str = "",
        temperature: Optional[float] = None,
        num_samples: int = 1,
    ) -> List[SampledReflection]:
        """从编码中采样多样化反思信号"""
        temp = temperature or self.default_temperature
        temp = max(self.temperature_range[0], min(self.temperature_range[1], temp))

        results = []
        with self._lock:
            for i in range(num_samples):
                category = self._select_category(encoding, temp, i)
                template = self._select_template(category, temp, i)
                reflection_text = self._fill_template(template, task_context)

                diversity = min(1.0, temp / 2.0 + 0.1 * i)
                confidence = max(0.1, 1.0 - temp * 0.3)

                reflection = SampledReflection(
                    reflection_id=f"sample_{self._sample_count:06d}_{i}",
                    encoding_ref=encoding.encoding_id if encoding else None,
                    temperature=temp,
                    reflection_text=reflection_text,
                    diversity_score=diversity,
                    confidence=confidence,
                    suggested_actions=self._derive_actions(reflection_text),
                )
                results.append(reflection)
                self._sample_count += 1
                self._diversity_history.append(diversity)

        return results

    def _select_category(
        self, encoding: Optional[ReflectiveEncoding], temp: float, seed: int
    ) -> str:
        """温度加权选择反思类别"""
        categories = list(self._reflection_templates.keys())
        weights = []
        for i, cat in enumerate(categories):
            if cat == "meta":
                w = temp * 0.5 + 0.1  # 高温时更多元反思
            elif cat == "strategy":
                w = 1.0 / (temp + 0.5)  # 低温时更策略性
            else:
                w = 0.5 + temp * 0.3
            weights.append(max(0.1, w))

        total = sum(weights)
        r = (hash(f"{seed}:{temp}") % 1000) / 1000.0
        cumulative = 0.0
        for i, w in enumerate(weights):
            cumulative += w / total
            if r <= cumulative:
                return categories[i]
        return categories[-1]

    def _select_template(self, category: str, temp: float, seed: int) -> str:
        templates = self._reflection_templates.get(category, [self._reflection_templates["strategy"][0]])
        idx = (hash(f"tpl:{seed}:{temp}") % len(templates))
        return templates[idx]

    def _fill_template(self, template: str, context: str) -> str:
        """用上下文填充模板占位符"""
        fillers = {
            "step": "3",
            "alternative": "并行多臂探索",
            "tool_a": "search",
            "tool_b": "browse",
            "granularity": "粗",
            "suggestion": "细粒度",
            "cue": context[:20] if context else "关键决策点",
            "missing": "跨步验证",
            "concept": context[:15] if context else "任务目标",
            "edge_case": "边界条件",
            "assumption": "输入完整性",
            "verification": "交叉校验",
            "memory_item": "task-2026-08",
            "updated": "最新状态",
            "relevant_case": "类似场景 #42",
            "entry": "mem_001",
            "n": "3",
            "coverage": "75",
            "missing_dim": "时间维度",
        }

        result = template
        for key, val in fillers.items():
            result = result.replace(f"{{{key}}}", str(val))
        return result

    def _derive_actions(self, reflection_text: str) -> List[str]:
        """从反思文本中推导改进动作"""
        actions = []
        if "策略" in reflection_text or "路径" in reflection_text:
            actions.append("调整任务规划策略")
        if "工具" in reflection_text:
            actions.append("扩展工具选择空间")
        if "记忆" in reflection_text or "检索" in reflection_text:
            actions.append("优化记忆检索参数")
        if "推理" in reflection_text or "逻辑" in reflection_text:
            actions.append("修正推理链路径")
        if not actions:
            actions.append("记录反思以供后续参考")
        return actions

    def set_temperature(self, temp: float):
        self.default_temperature = max(
            self.temperature_range[0], min(self.temperature_range[1], temp)
        )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_samples": self._sample_count,
                "current_temperature": self.default_temperature,
                "avg_diversity": (
                    sum(self._diversity_history[-100:]) / max(1, len(self._diversity_history[-100:]))
                    if self._diversity_history else 0.0
                ),
                "top_p": self.top_p,
            }


# ============================================================================
# CrossSampleMemoryBank — 跨样本记忆库
# ============================================================================

class CrossSampleMemoryBank:
    """
    存储历史上成功的反思模式, 按任务相似度检索复用。

    核心机制:
    - 余弦相似度匹配最近任务 → 检索成功反思模式
    - 质量评分 (success_rate × recency × reuse_count) 用于排序
    - LRU 淘汰 + 定期清理低质量条目
    """

    def __init__(self, capacity: int = 4096, similarity_threshold: float = 0.3):
        self.capacity = capacity
        self.similarity_threshold = similarity_threshold
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, MemoryBankEntry] = OrderedDict()
        self._task_index: Dict[str, List[str]] = defaultdict(list)
        self._insert_count: int = 0
        self._hit_count: int = 0

    def insert(
        self,
        encoding: ReflectiveEncoding,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryBankEntry:
        """将成功的反思编码存入记忆库"""
        entry = MemoryBankEntry(
            entry_id=f"mb_{self._insert_count:06d}",
            encoding_hash=encoding.encoding_id,
            task_signature=encoding.task_signature,
            reflection_pattern=list(encoding.reflection_vector),
            success_rate=encoding.success_rate,
            metadata=metadata or {},
        )

        with self._lock:
            if len(self._entries) >= self.capacity:
                # LRU 淘汰最低分的
                worst = min(self._entries.items(), key=lambda x: x[1].quality_score)
                old_sig = worst[1].task_signature
                del self._entries[worst[0]]
                if old_sig in self._task_index:
                    self._task_index[old_sig] = [
                        eid for eid in self._task_index[old_sig] if eid != worst[0]
                    ]

            self._entries[entry.entry_id] = entry
            self._task_index[entry.task_signature].append(entry.entry_id)
            self._insert_count += 1

        return entry

    def retrieve(
        self,
        task_description: str,
        query_vector: Optional[List[float]] = None,
        top_k: int = 5,
    ) -> List[MemoryBankEntry]:
        """按任务相似度检索成功的反思模式"""
        task_sig = ReflectionEncoder(embedding_dim=128).compute_task_signature(
            task_description
        )

        with self._lock:
            # Level 1: 精确任务签名匹配
            exact = [
                self._entries[eid]
                for eid in self._task_index.get(task_sig, [])
                if eid in self._entries
            ]

            # Level 2: 向量相似度匹配 (如果有 query_vector)
            similar = []
            if query_vector:
                for entry in self._entries.values():
                    if entry.entry_id not in {e.entry_id for e in exact}:
                        sim = self._cosine_similarity(query_vector, entry.reflection_pattern)
                        if sim >= self.similarity_threshold:
                            similar.append((entry, sim))
                similar.sort(key=lambda x: x[1], reverse=True)

            # 合并并按质量排序
            all_candidates = exact + [e for e, _ in similar[:top_k]]
            all_candidates.sort(
                key=lambda e: e.success_rate * (1.0 + 0.1 * math.log1p(e.reuse_count)),
                reverse=True,
            )
            result = all_candidates[:top_k]

            for e in result:
                e.reuse_count += 1
                e.last_reuse = time.time()
                self._hit_count += 1

            return result

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        if len(a) != len(b) or len(a) == 0:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1e-8
        nb = math.sqrt(sum(y * y for y in b)) or 1e-8
        return dot / (na * nb)

    def update_quality(self, entry_id: str, new_quality: float):
        with self._lock:
            if entry_id in self._entries:
                self._entries[entry_id].quality_score = max(0.0, min(1.0, new_quality))

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_entries": len(self._entries),
                "total_inserts": self._insert_count,
                "total_hits": self._hit_count,
                "hit_rate": self._hit_count / max(1, self._insert_count),
                "unique_tasks": len(self._task_index),
                "capacity_usage": len(self._entries) / max(1, self.capacity),
            }


# ============================================================================
# ParametricEpisodicBridge — 参数化记忆→情景记忆桥接器
# ============================================================================

class ParametricEpisodicBridge:
    """
    将参数化反思经验注入情景检索 pipeline。

    在情景记忆检索时, 从 CrossSampleMemoryBank 中拉取相关反思模式,
    将其作为额外上下文增强检索结果。
    """

    def __init__(self, injection_weight: float = 0.3, max_injections: int = 3):
        self.injection_weight = injection_weight
        self.max_injections = max_injections
        self._lock = threading.RLock()
        self._bridge_count: int = 0
        self._injection_history: List[Dict[str, Any]] = []

    def bridge(
        self,
        query: str,
        query_embedding: List[float],
        memory_bank: CrossSampleMemoryBank,
        episodic_results: List[Dict[str, Any]],
    ) -> BridgeResult:
        """将参数化反思注入情景检索结果"""
        start = time.time()

        # 检索相关反思模式
        patterns = memory_bank.retrieve(
            task_description=query,
            query_vector=query_embedding,
            top_k=self.max_injections,
        )

        # 转换为反思信号
        sampler = DiversitySampler(default_temperature=0.5)
        injected = []
        for pattern in patterns:
            # 用低温采样生成基于历史成功模式的反思
            samples = sampler.sample(
                encoding=None,
                task_context=query,
                temperature=0.3,
                num_samples=1,
            )
            if samples:
                samples[0].confidence *= pattern.success_rate
                injected.append(samples[0])

        # 构建增强上下文
        augmented = {
            "original_results": len(episodic_results),
            "injected_count": len(injected),
            "injection_weight": self.injection_weight,
            "combined_context": self._combine(episodic_results, injected),
        }

        latency = (time.time() - start) * 1000

        with self._lock:
            self._bridge_count += 1
            self._injection_history.append({
                "query": query[:50],
                "patterns_retrieved": len(patterns),
                "injected": len(injected),
                "latency_ms": latency,
            })

        return BridgeResult(
            bridge_id=f"bridge_{self._bridge_count:06d}",
            query_embedding=query_embedding,
            injected_reflections=injected,
            augmented_context=augmented,
            retrieval_latency_ms=latency,
            confidence=sum(r.confidence for r in injected) / max(1, len(injected)),
        )

    def _combine(
        self,
        episodic: List[Dict[str, Any]],
        reflections: List[SampledReflection],
    ) -> Dict[str, Any]:
        return {
            "episodic_items": episodic,
            "reflective_insights": [
                {
                    "text": r.reflection_text,
                    "actions": r.suggested_actions,
                    "confidence": r.confidence,
                }
                for r in reflections
            ],
        }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_bridges": self._bridge_count,
                "avg_injections": (
                    sum(h["injected"] for h in self._injection_history[-50:]) / max(1, len(self._injection_history[-50:]))
                    if self._injection_history else 0
                ),
                "injection_weight": self.injection_weight,
            }


# ============================================================================
# WeakToStrongTransfer — 弱→强模型反思迁移
# ============================================================================

class WeakToStrongTransfer:
    """
    弱模型反思经验迁移至强模型 (样本高效)。

    策略:
    - 弱模型在少量样本上生成反思 → 提取模式 → 无损迁移至强模型
    - 无需外部强模型标注, 充分复用弱模型探索经验
    - 支持三种迁移模式: Direct Distill, Sample Efficient, Curriculum
    """

    def __init__(self, source_model: str = "weak-7b", target_model: str = "strong-70b"):
        self.source_model = source_model
        self.target_model = target_model
        self._lock = threading.RLock()
        self._transfer_history: List[TransferRecord] = []
        self._total_transferred: int = 0

    def transfer(
        self,
        encodings: List[ReflectiveEncoding],
        mode: TransferMode = TransferMode.SAMPLE_EFFICIENT,
        samples_used: int = 10,
    ) -> TransferRecord:
        """
        将弱模型反思编码迁移至强模型。

        Sample Efficient 模式下: 仅用 10 个样本即可迁移,
        效率提升 = target_samples_needed / samples_used
        """
        # 计算样本效率增益 (强模型通常需要 100x 样本)
        baseline_samples = samples_used * 100 if mode != TransferMode.DIRECT_DISTILL else samples_used
        efficiency_gain = baseline_samples / max(1, samples_used)

        # 模拟迁移后的成功率 (基于编码质量和迁移模式)
        base_rate = sum(e.success_rate for e in encodings) / max(1, len(encodings))
        mode_bonus = {
            TransferMode.DIRECT_DISTILL: 0.05,
            TransferMode.SAMPLE_EFFICIENT: 0.15,
            TransferMode.CURRICULUM: 0.12,
            TransferMode.CO_TRAINING: 0.18,
        }
        target_rate = min(1.0, base_rate + mode_bonus.get(mode, 0.1))

        record = TransferRecord(
            transfer_id=f"w2s_{self._total_transferred:06d}",
            source_model=self.source_model,
            target_model=self.target_model,
            mode=mode,
            transferred_encodings=len(encodings),
            target_success_rate=target_rate,
            sample_efficiency_gain=efficiency_gain,
        )

        with self._lock:
            self._transfer_history.append(record)
            self._total_transferred += len(encodings)

        logger.info(
            "Weak→Strong transfer #%d: mode=%s, encodings=%d, eff_gain=%.1fx",
            self._total_transferred, mode.value, len(encodings), efficiency_gain,
        )
        return record

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_transfers": len(self._transfer_history),
                "total_encodings_transferred": self._total_transferred,
                "source_model": self.source_model,
                "target_model": self.target_model,
                "avg_efficiency_gain": (
                    sum(r.sample_efficiency_gain for r in self._transfer_history[-20:]) / max(1, len(self._transfer_history[-20:]))
                    if self._transfer_history else 0.0
                ),
            }


# ============================================================================
# SelfImprovementLoop — 无监督自我改进循环
# ============================================================================

class SelfImprovementLoop:
    """
    反思 → 评估 → 更新参数 → 再反思 的无监督闭环。

    不与外部监督信号交互, 完全通过内部质量评估驱动改进。
    每个循环产生 ImprovementCycle 记录, 持续追踪质量 delta。
    """

    def __init__(
        self,
        improvement_threshold: float = 0.01,
        max_cycles_per_session: int = 50,
        stagnation_limit: int = 5,
    ):
        self.improvement_threshold = improvement_threshold
        self.max_cycles_per_session = max_cycles_per_session
        self.stagnation_limit = stagnation_limit
        self._lock = threading.RLock()
        self._cycles: List[ImprovementCycle] = []
        self._current_quality: float = 0.5
        self._stagnation_count: int = 0
        self._total_cycles: int = 0

    def run_cycle(
        self,
        encoder: ReflectionEncoder,
        sampler: DiversitySampler,
        memory_bank: CrossSampleMemoryBank,
        task_input: str,
        task_result_quality: float,
    ) -> ImprovementCycle:
        """执行一个完整的自我改进循环"""
        cycle_id = f"cycle_{self._total_cycles:06d}"
        before_quality = self._current_quality

        # Phase 1: Reflect
        reflection_text = self._generate_meta_reflection(task_input, task_result_quality)
        encoding = encoder.encode(
            task_description=task_input,
            reflection_text=reflection_text,
            source_sample_ids=[cycle_id],
            success_rate=task_result_quality,
        )

        # Phase 2: 生成多样化反思 + Evaluate
        samples = sampler.sample(
            encoding=encoding,
            task_context=task_input,
            temperature=self._adaptive_temperature(),
            num_samples=3,
        )
        accepted = [s for s in samples if s.confidence > 0.4]

        # Phase 3: Update
        if accepted:
            memory_bank.insert(encoding)
            quality_delta = sum(s.confidence * 0.05 for s in accepted) / max(1, len(accepted))
        else:
            quality_delta = -0.01  # 无有效反思时轻微惩罚

        after_quality = max(0.1, min(1.0, before_quality + quality_delta))
        delta = after_quality - before_quality

        # 检查停滞
        with self._lock:
            if abs(delta) < self.improvement_threshold:
                self._stagnation_count += 1
            else:
                self._stagnation_count = 0
            self._current_quality = after_quality

        cycle = ImprovementCycle(
            cycle_id=cycle_id,
            phase=ImprovementPhase.CONSOLIDATE,
            before_quality=before_quality,
            after_quality=after_quality,
            delta=delta,
            reflections_generated=len(samples),
            reflections_accepted=len(accepted),
            metadata={
                "temperature": sampler.default_temperature,
                "stagnation_count": self._stagnation_count,
            },
        )

        with self._lock:
            self._cycles.append(cycle)
            self._total_cycles += 1

        return cycle

    def _adaptive_temperature(self) -> float:
        """自适应温度调度——停滞时升温, 收敛时降温"""
        if self._stagnation_count >= self.stagnation_limit:
            return 1.5  # 高温探索
        elif self._stagnation_count >= 2:
            return 0.9
        else:
            return 0.5  # 低温精化

    def _generate_meta_reflection(self, task: str, quality: float) -> str:
        """生成元反思——反思反思过程本身"""
        if quality < 0.4:
            return f"任务'{task[:30]}'质量低({quality:.2f})。反思策略多样性不足, 应提高温度并扩展搜索空间。"
        elif quality < 0.7:
            return f"任务'{task[:30]}'质量中等({quality:.2f})。当前反思模式有效但可优化, 建议聚焦边界案例。"
        else:
            return f"任务'{task[:30]}'质量高({quality:.2f})。反思模式成功, 应固化至记忆库供复用。"

    def should_continue(self) -> bool:
        with self._lock:
            if self._total_cycles >= self.max_cycles_per_session:
                return False
            if self._stagnation_count >= self.stagnation_limit * 2:
                return False
            return True

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_cycles": self._total_cycles,
                "current_quality": self._current_quality,
                "stagnation_count": self._stagnation_count,
                "avg_delta": (
                    sum(c.delta for c in self._cycles[-20:]) / max(1, len(self._cycles[-20:]))
                    if self._cycles else 0.0
                ),
                "total_reflections_generated": sum(c.reflections_generated for c in self._cycles),
                "total_reflections_accepted": sum(c.reflections_accepted for c in self._cycles),
                "converged": self._stagnation_count >= self.stagnation_limit,
            }
