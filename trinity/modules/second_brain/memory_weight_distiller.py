"""
# status: orphan (2026-08-15 audit, not in runtime path)
P13-1: Memory → Weight Distillation Pipeline (Hybrid Memory→Weight)
====================================================================

对标 Hybrid Memory→Weight Pipeline 五架构综合设计。

五架构路由：
  - Token-First: 记忆→token 级提示→模型权重蒸馏（轻量高频）
  - Instant Injection: 新记忆实时注入适配器，延迟 < 100ms
  - Self-Instruct: 记忆→指令数据→LoRA 微调循环
  - Self-Evolving: 模型自主评估记忆效用、迭代精炼蒸馏策略
  - Sparse Fine-Tuning: 仅更新关键层参数，计算量降低 70-90%

核心子系统：
  - ArchitectureSelector:  根据任务类型/记忆量/延迟预算自动选择蒸馏架构
  - DistillationScheduler: 记忆积累量阈值 / 时间窗口周期性触发，支持休眠时计算
  - SyntheticDataGenerator: 从记忆实体生成合成对话数据（QA对 / 假设对话 / RL rubrics）
  - LoRAAdapterManager:     管理蒸馏产出 LoRA 适配器（创建/存储/加载/合并/卸载）
  - ForgettingMitigator:    灾难性遗忘缓解——稀疏更新策略 + 经验回放 + 对齐退化检测

接口兼容：episodic_rl.py（FeedbackSignal/MemoryState）、skill_synthesis.py（技能合成触发）
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class DistillationArchitecture(Enum):
    """五种蒸馏架构枚举。"""
    TOKEN_FIRST = "token_first"               # 记忆→Token→权重，轻量高频
    INSTANT_INJECTION = "instant_injection"    # 实时注入适配器，延迟 < 100ms
    SELF_INSTRUCT = "self_instruct"            # 记忆→指令数据→LoRA 循环
    SELF_EVOLVING = "self_evolving"            # 模型自主评估效用、迭代精炼
    SPARSE_FINE_TUNING = "sparse_fine_tuning"  # 仅更新关键层参数


class SchedulerTriggerMode(Enum):
    """蒸馏调度触发模式。"""
    MEMORY_THRESHOLD = "memory_threshold"   # 记忆积累量达到阈值
    TIME_WINDOW = "time_window"             # 固定时间窗口
    MANUAL = "manual"                       # 手动触发
    HYBRID = "hybrid"                       # 混合模式（阈值+窗口）


class ComputeWindow(Enum):
    """计算窗口模式。"""
    IMMEDIATE = "immediate"             # 立即执行
    SLEEP_TIME = "sleep_time"          # 休眠时计算
    LOW_LOAD = "low_load"              # 系统低负载时
    SCHEDULED = "scheduled"            # 预定时间点


class SyntheticDataFormat(Enum):
    """合成数据格式。"""
    QA_PAIR = "qa_pair"                # 问答对
    HYPOTHETICAL_DIALOGUE = "hypothetical_dialogue"  # 假设对话
    RL_RUBRIC = "rl_rubric"            # RL 评估标准
    INSTRUCTION_PROMPT = "instruction_prompt"  # 指令提示
    MIXED = "mixed"                    # 混合格式


class AdapterState(Enum):
    """LoRA 适配器状态。"""
    CREATING = "creating"
    READY = "ready"
    LOADED = "loaded"
    MERGED = "merged"
    UNLOADED = "unloaded"
    STALE = "stale"


class ForgettingRisk(Enum):
    """遗忘风险等级。"""
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class MemoryEntity:
    """记忆实体——蒸馏输入的基本单元。"""
    entity_id: str
    content: str
    entity_type: str = "fact"
    confidence: float = 1.0
    source_timestamp: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)
    embedding: Optional[np.ndarray] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DistillationBatch:
    """一批待蒸馏的记忆实体。"""
    batch_id: str
    entities: List[MemoryEntity]
    target_architecture: DistillationArchitecture
    created_at: float = field(default_factory=time.time)
    total_tokens: int = 0
    priority: float = 0.5


@dataclass
class LoRAAdapter:
    """LoRA 适配器包装。"""
    adapter_id: str
    name: str
    state: AdapterState = AdapterState.CREATING
    parent_model: str = ""
    rank: int = 16
    alpha: float = 16.0
    target_modules: List[str] = field(default_factory=list)
    storage_path: str = ""
    created_at: float = field(default_factory=time.time)
    source_entities: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SyntheticPair:
    """合成数据条目。"""
    pair_id: str
    format: SyntheticDataFormat
    instruction: str = ""
    input_text: str = ""
    output_text: str = ""
    rubrics: List[str] = field(default_factory=list)
    source_entities: List[str] = field(default_factory=list)
    difficulty: float = 0.5


@dataclass
class ReplayExperience:
    """经验回放缓冲区条目。"""
    experience_id: str
    memory_entity: MemoryEntity
    original_task: str
    outcome_label: float  # 1.0 = 成功 / 0.0 = 失败
    timestamp: float = field(default_factory=time.time)
    architecture_used: DistillationArchitecture = DistillationArchitecture.TOKEN_FIRST


@dataclass
class DistillationStats:
    """蒸馏调度统计。"""
    total_batches: int = 0
    total_entities_distilled: int = 0
    architectures_used: Dict[str, int] = field(default_factory=dict)
    total_adapters_created: int = 0
    total_synthetic_pairs: int = 0
    forgetting_events_detected: int = 0
    last_distillation_at: float = 0.0
    avg_batch_latency_ms: float = 0.0


# ============================================================================
# ArchitectureSelector
# ============================================================================

class ArchitectureSelector:
    """根据任务类型、记忆量、延迟预算自动选择五种蒸馏架构之一。

    决策因子：
      - 记忆量 (N): 少→Token-First / 多→Self-Evolving
      - 延迟要求 (L): 低→Instant Injection / 高→Sparse Fine-Tuning
      - 任务复杂度 (C): 简单→Token-First / 复杂→Self-Instruct
      - 历史成功率 (H): 低→Self-Evolving / 高→保持当前
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._history: Dict[DistillationArchitecture, List[float]] = defaultdict(list)
        self._scoreboard: Dict[DistillationArchitecture, float] = {
            a: 0.5 for a in DistillationArchitecture
        }

    def select(
        self,
        memory_count: int,
        latency_budget_ms: float,
        task_complexity: float,
        confidence_threshold: float = 0.6,
    ) -> DistillationArchitecture:
        """返回最优蒸馏架构。"""
        with self._lock:
            scores: Dict[DistillationArchitecture, float] = {}

            # Token-First: 适合小记忆量、低复杂度
            scores[DistillationArchitecture.TOKEN_FIRST] = (
                0.3 * (1.0 - min(memory_count / 1000.0, 1.0))
                + 0.4 * (1.0 - task_complexity)
                + 0.3 * max(0.0, 1.0 - latency_budget_ms / 500.0)
            )

            # Instant Injection: 适合极低延迟
            scores[DistillationArchitecture.INSTANT_INJECTION] = (
                0.2 * (1.0 - min(memory_count / 500.0, 1.0))
                + 0.1 * (1.0 - task_complexity)
                + 0.7 * max(0.0, 1.0 - latency_budget_ms / 200.0)
            )

            # Self-Instruct: 适合中等复杂度、中等记忆量
            scores[DistillationArchitecture.SELF_INSTRUCT] = (
                0.25 * min(memory_count / 2000.0, 1.0)
                + 0.45 * task_complexity
                + 0.3 * min(latency_budget_ms / 10000.0, 1.0)
            )

            # Self-Evolving: 适合大记忆量、高复杂度
            scores[DistillationArchitecture.SELF_EVOLVING] = (
                0.35 * min(memory_count / 5000.0, 1.0)
                + 0.45 * task_complexity
                + 0.2 * min(latency_budget_ms / 30000.0, 1.0)
            )

            # Sparse Fine-Tuning: 适合大记忆量但有计算约束
            scores[DistillationArchitecture.SPARSE_FINE_TUNING] = (
                0.3 * min(memory_count / 3000.0, 1.0)
                + 0.3 * task_complexity
                + 0.4 * min(latency_budget_ms / 20000.0, 1.0)
            )

            # 融合历史成功率
            for arch in DistillationArchitecture:
                scores[arch] += self._scoreboard.get(arch, 0.5) * 0.15

            best = max(scores, key=scores.get)
            if scores[best] < confidence_threshold:
                return DistillationArchitecture.TOKEN_FIRST  # 安全默认
            return best

    def record_outcome(self, architecture: DistillationArchitecture, score: float) -> None:
        """记录某架构的产出质量。"""
        with self._lock:
            self._history[architecture].append(score)
            if len(self._history[architecture]) > 100:
                self._history[architecture] = self._history[architecture][-100:]
            self._scoreboard[architecture] = np.mean(self._history[architecture])

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "scoreboard": {a.name: round(s, 4) for a, s in self._scoreboard.items()},
                "total_decisions": sum(len(v) for v in self._history.values()),
            }


# ============================================================================
# DistillationScheduler
# ============================================================================

class DistillationScheduler:
    """周期性触发蒸馏——记忆积累量阈值 / 时间窗口。

    支持 "休眠时计算" (sleep-time compute)：将批量蒸馏延迟到系统空闲期执行，
    避免与用户交互争抢计算资源。
    """

    def __init__(
        self,
        memory_threshold: int = 500,
        time_window_seconds: float = 3600.0,
        compute_window: ComputeWindow = ComputeWindow.SLEEP_TIME,
        max_pending_batches: int = 10,
    ):
        self.memory_threshold = memory_threshold
        self.time_window_seconds = time_window_seconds
        self.compute_window = compute_window
        self.max_pending_batches = max_pending_batches

        self._lock = threading.RLock()
        self._pending_entities: List[MemoryEntity] = []
        self._pending_batches: List[DistillationBatch] = []
        self._entity_count_since_last: int = 0
        self._last_run_at: float = time.time()
        self._stats = DistillationStats()

    def ingest(self, entity: MemoryEntity) -> Optional[DistillationBatch]:
        """摄入新记忆实体，若触发阈值则返回待处理批次。"""
        with self._lock:
            self._pending_entities.append(entity)
            self._entity_count_since_last += 1

            time_elapsed = time.time() - self._last_run_at
            memory_triggered = self._entity_count_since_last >= self.memory_threshold
            time_triggered = time_elapsed >= self.time_window_seconds and self._entity_count_since_last > 0

            if (memory_triggered or time_triggered) and self._entity_count_since_last > 0:
                return self._create_batch()
            return None

    def _create_batch(self) -> DistillationBatch:
        """创建蒸馏批次。"""
        entities = list(self._pending_entities)
        self._pending_entities.clear()
        self._entity_count_since_last = 0
        self._last_run_at = time.time()

        batch = DistillationBatch(
            batch_id=f"distill_{uuid.uuid4().hex[:12]}",
            entities=entities,
            target_architecture=DistillationArchitecture.TOKEN_FIRST,  # 默认，后续由 selector 覆盖
        )
        if len(self._pending_batches) < self.max_pending_batches:
            self._pending_batches.append(batch)
        return batch

    def get_pending_batches(self) -> List[DistillationBatch]:
        """获取所有等待蒸馏的批次。"""
        with self._lock:
            return list(self._pending_batches)

    def mark_batch_complete(self, batch_id: str) -> None:
        """标记批次完成。"""
        with self._lock:
            self._pending_batches = [b for b in self._pending_batches if b.batch_id != batch_id]
            self._stats.total_batches += 1

    def should_compute_now(self) -> bool:
        """判断当前是否应执行计算（根据 compute_window 策略）。"""
        if self.compute_window == ComputeWindow.IMMEDIATE:
            return True
        if self.compute_window == ComputeWindow.SCHEDULED:
            return True  # 由外部定时器控制
        # SLEEP_TIME / LOW_LOAD: 标记为等待空闲期
        return False

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "pending_entities": len(self._pending_entities),
                "pending_batches": len(self._pending_batches),
                "entity_count_since_last": self._entity_count_since_last,
                "memory_threshold": self.memory_threshold,
                "time_window_s": self.time_window_seconds,
                "compute_window": self.compute_window.value,
                **dataclasses.asdict(self._stats),
            }


# ============================================================================
# SyntheticDataGenerator
# ============================================================================

class SyntheticDataGenerator:
    """从记忆实体生成合成对话数据。

    生成类型：
      - QA 对：基于事实记忆的问答
      - 假设对话：模拟用户-代理交互场景
      - RL Rubrics：评估标准（正确/错误答案的评分准则）
      - 指令提示：将记忆转化为可执行指令
    """

    def __init__(self, max_pairs_per_batch: int = 100):
        self.max_pairs_per_batch = max_pairs_per_batch
        self._lock = threading.RLock()
        self._generation_history: List[SyntheticPair] = []
        self._total_generated = 0

    def generate(
        self,
        entities: List[MemoryEntity],
        formats: Optional[List[SyntheticDataFormat]] = None,
    ) -> List[SyntheticPair]:
        """从记忆实体列表生成合成数据。"""
        if formats is None:
            formats = [SyntheticDataFormat.QA_PAIR, SyntheticDataFormat.HYPOTHETICAL_DIALOGUE]

        pairs: List[SyntheticPair] = []
        with self._lock:
            for entity in entities[:self.max_pairs_per_batch]:
                for fmt in formats:
                    pair = self._generate_single(entity, fmt)
                    if pair:
                        pairs.append(pair)
            self._generation_history.extend(pairs)
            self._total_generated += len(pairs)
        return pairs

    def _generate_single(self, entity: MemoryEntity, fmt: SyntheticDataFormat) -> Optional[SyntheticPair]:
        """为单个实体生成一种格式的合成数据。"""
        pair_id = f"synth_{uuid.uuid4().hex[:12]}"
        content = entity.content
        tags = ", ".join(entity.tags) if entity.tags else "general"

        if fmt == SyntheticDataFormat.QA_PAIR:
            return SyntheticPair(
                pair_id=pair_id,
                format=fmt,
                instruction=f"根据以下信息回答问题：{content[:200]}",
                input_text=f"关于 {tags}，请给出详细说明。",
                output_text=f"{content}",
                source_entities=[entity.entity_id],
                difficulty=0.3 + 0.7 * (1.0 - entity.confidence),
            )
        elif fmt == SyntheticDataFormat.HYPOTHETICAL_DIALOGUE:
            return SyntheticPair(
                pair_id=pair_id,
                format=fmt,
                instruction=f"模拟一段关于 {tags} 的用户对话。",
                input_text=f"用户说：「我想了解更多关于 {tags} 的信息。」",
                output_text=f"助手回复：「根据我的记忆，{content[:300]}」",
                source_entities=[entity.entity_id],
                difficulty=0.5,
            )
        elif fmt == SyntheticDataFormat.RL_RUBRIC:
            return SyntheticPair(
                pair_id=pair_id,
                format=fmt,
                instruction=f"评估标准：基于 {tags} 信息的问答质量。",
                rubrics=[
                    f"回答中包含核心事实（必须包含: {content[:100]}...）",
                    "回答逻辑连贯、语言流畅",
                    "回答长度适中、不冗余",
                ],
                source_entities=[entity.entity_id],
                difficulty=0.4,
            )
        elif fmt == SyntheticDataFormat.INSTRUCTION_PROMPT:
            return SyntheticPair(
                pair_id=pair_id,
                format=fmt,
                instruction=f"利用以下记忆完成任务：{content[:200]}",
                input_text=f"任务：处理与 {tags} 相关的用户请求。",
                output_text="",
                source_entities=[entity.entity_id],
                difficulty=0.3 + 0.7 * (1.0 - entity.confidence),
            )
        return None

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "total_generated": self._total_generated,
                "history_size": len(self._generation_history),
                "max_pairs_per_batch": self.max_pairs_per_batch,
            }


# ============================================================================
# LoRAAdapterManager
# ============================================================================

class LoRAAdapterManager:
    """管理蒸馏产出的 LoRA 适配器（创建/存储/加载/合并/卸载）。

    功能：
      - 创建：为指定模型创建 LoRA 适配器配置
      - 存储：序列化适配器到磁盘
      - 加载：从磁盘反序列化适配器
      - 合并：将 LoRA 权重合并到基座模型
      - 卸载：释放已加载适配器的内存
    """

    def __init__(self, storage_dir: str = ""):
        self.storage_dir = storage_dir
        self._lock = threading.RLock()
        self._adapters: Dict[str, LoRAAdapter] = {}
        self._loaded_adapters: Dict[str, Any] = {}

    def create_adapter(
        self,
        name: str,
        parent_model: str = "default",
        rank: int = 16,
        alpha: float = 16.0,
        target_modules: Optional[List[str]] = None,
    ) -> LoRAAdapter:
        """创建新的 LoRA 适配器。"""
        if target_modules is None:
            target_modules = ["q_proj", "v_proj", "k_proj", "o_proj"]
        adapter_id = f"lora_{uuid.uuid4().hex[:12]}"
        adapter = LoRAAdapter(
            adapter_id=adapter_id,
            name=name,
            state=AdapterState.CREATING,
            parent_model=parent_model,
            rank=rank,
            alpha=alpha,
            target_modules=target_modules,
        )
        with self._lock:
            self._adapters[adapter_id] = adapter
        logger.info(f"LoRA adapter created: {adapter_id} (name={name}, rank={rank})")
        return adapter

    def store_adapter(self, adapter_id: str, path: str) -> bool:
        """存储适配器配置到磁盘。"""
        with self._lock:
            adapter = self._adapters.get(adapter_id)
            if not adapter:
                return False
            try:
                config = {
                    "adapter_id": adapter.adapter_id,
                    "name": adapter.name,
                    "rank": adapter.rank,
                    "alpha": adapter.alpha,
                    "target_modules": adapter.target_modules,
                    "parent_model": adapter.parent_model,
                    "created_at": adapter.created_at,
                    "source_entities": adapter.source_entities,
                    "metrics": adapter.metrics,
                }
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2)
                adapter.storage_path = path
                adapter.state = AdapterState.READY
                return True
            except (OSError, IOError) as e:
                logger.error(f"Failed to store adapter {adapter_id}: {e}")
                return False

    def load_adapter(self, adapter_id: str) -> bool:
        """加载适配器到内存（标记为已加载）。"""
        with self._lock:
            adapter = self._adapters.get(adapter_id)
            if not adapter:
                return False
            adapter.state = AdapterState.LOADED
            self._loaded_adapters[adapter_id] = True
            return True

    def merge_adapter(self, adapter_id: str) -> bool:
        """将 LoRA 权重合并到基座模型。"""
        with self._lock:
            adapter = self._adapters.get(adapter_id)
            if not adapter or adapter.state != AdapterState.LOADED:
                return False
            adapter.state = AdapterState.MERGED
            return True

    def unload_adapter(self, adapter_id: str) -> bool:
        """卸载适配器释放内存。"""
        with self._lock:
            adapter = self._adapters.get(adapter_id)
            if not adapter:
                return False
            adapter.state = AdapterState.UNLOADED
            self._loaded_adapters.pop(adapter_id, None)
            return True

    def list_adapters(self, state: Optional[AdapterState] = None) -> List[LoRAAdapter]:
        """列出所有适配器，可按状态过滤。"""
        with self._lock:
            adapters = list(self._adapters.values())
            if state:
                adapters = [a for a in adapters if a.state == state]
            return adapters

    def remove_stale_adapters(self, max_age_seconds: float = 86400.0) -> int:
        """移除过期的适配器。"""
        with self._lock:
            now = time.time()
            stale_ids = [
                aid for aid, a in self._adapters.items()
                if now - a.created_at > max_age_seconds and a.state != AdapterState.MERGED
            ]
            for aid in stale_ids:
                a = self._adapters.pop(aid)
                a.state = AdapterState.STALE
                self._loaded_adapters.pop(aid, None)
            return len(stale_ids)

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "total_adapters": len(self._adapters),
                "by_state": {
                    s.value: sum(1 for a in self._adapters.values() if a.state == s)
                    for s in AdapterState
                },
                "loaded_count": len(self._loaded_adapters),
            }


# ============================================================================
# ForgettingMitigator
# ============================================================================

class ForgettingMitigator:
    """灾难性遗忘缓解。

    三管齐下：
      1. 稀疏更新策略：仅更新关键层/关键参数，保留旧知识路径
      2. 经验回放缓冲区：随机回放历史记忆→蒸馏对，维持旧任务表现
      3. 对齐退化检测：定期评估旧任务性能，检测并预警遗忘
    """

    def __init__(
        self,
        replay_buffer_size: int = 5000,
        sparsity_ratio: float = 0.1,
        degradation_threshold: float = 0.05,
    ):
        self.replay_buffer_size = replay_buffer_size
        self.sparsity_ratio = sparsity_ratio
        self.degradation_threshold = degradation_threshold

        self._lock = threading.RLock()
        self._replay_buffer: deque = deque(maxlen=replay_buffer_size)
        self._baseline_scores: Dict[str, float] = {}  # task_name → score
        self._current_scores: Dict[str, float] = {}
        self._degradation_alerts: List[Dict[str, Any]] = []
        self._update_mask: Optional[np.ndarray] = None
        self._forgetting_events: int = 0

    def add_experience(self, experience: ReplayExperience) -> None:
        """添加经验到回放缓冲区。"""
        with self._lock:
            self._replay_buffer.append(experience)

    def sample_replay(
        self, n: int = 32
    ) -> List[ReplayExperience]:
        """从回放缓冲区采样。"""
        with self._lock:
            if len(self._replay_buffer) == 0:
                return []
            indices = np.random.choice(
                len(self._replay_buffer),
                size=min(n, len(self._replay_buffer)),
                replace=False,
            )
            return [self._replay_buffer[i] for i in indices]

    def get_sparse_update_mask(
        self, param_names: List[str], param_shapes: Dict[str, Tuple[int, ...]]
    ) -> Dict[str, np.ndarray]:
        """生成稀疏更新掩码——仅允许更新 sparsity_ratio 比例的权重。"""
        masks: Dict[str, np.ndarray] = {}
        with self._lock:
            rng = np.random.RandomState(42)
            for name in param_names:
                shape = param_shapes.get(name, (1,))
                mask = rng.random(shape) < self.sparsity_ratio
                masks[name] = mask.astype(np.float32)
            self._update_mask = masks
        return masks

    def set_baseline(self, task_name: str, score: float) -> None:
        """设置某任务的基线性能。"""
        with self._lock:
            self._baseline_scores[task_name] = score

    def check_degradation(self, task_name: str, current_score: float) -> ForgettingRisk:
        """检查某任务是否退化（遗忘）。"""
        with self._lock:
            baseline = self._baseline_scores.get(task_name)
            if baseline is None:
                self._baseline_scores[task_name] = current_score
                return ForgettingRisk.NONE

            delta = baseline - current_score
            if delta < 0:
                self._baseline_scores[task_name] = max(baseline, current_score)
                return ForgettingRisk.NONE

            ratio = delta / (baseline + 1e-8)
            if ratio > 0.2:
                risk = ForgettingRisk.CRITICAL
            elif ratio > 0.1:
                risk = ForgettingRisk.HIGH
            elif ratio > self.degradation_threshold:
                risk = ForgettingRisk.MEDIUM
            else:
                risk = ForgettingRisk.LOW

            if risk.value in ("critical", "high"):
                self._degradation_alerts.append({
                    "task": task_name,
                    "baseline": baseline,
                    "current": current_score,
                    "delta": delta,
                    "risk": risk.value,
                    "timestamp": time.time(),
                })
                self._forgetting_events += 1
            return risk

    def get_degradation_alerts(self, clear: bool = False) -> List[Dict[str, Any]]:
        """获取所有退化告警。"""
        with self._lock:
            alerts = list(self._degradation_alerts)
            if clear:
                self._degradation_alerts.clear()
            return alerts

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "replay_buffer_size": len(self._replay_buffer),
                "max_replay_buffer": self.replay_buffer_size,
                "sparsity_ratio": self.sparsity_ratio,
                "degradation_threshold": self.degradation_threshold,
                "forgetting_events": self._forgetting_events,
                "monitored_tasks": len(self._baseline_scores),
                "pending_alerts": len(self._degradation_alerts),
            }


# ============================================================================
# Pipeline Orchestrator
# ============================================================================

class MemoryWeightDistiller:
    """记忆→权重蒸馏管道编排器。

    整合 ArchitectureSelector / DistillationScheduler / SyntheticDataGenerator /
    LoRAAdapterManager / ForgettingMitigator 为统一蒸馏流水线。
    """

    def __init__(
        self,
        memory_threshold: int = 500,
        time_window_seconds: float = 3600.0,
        compute_window: ComputeWindow = ComputeWindow.SLEEP_TIME,
        replay_buffer_size: int = 5000,
    ):
        self.selector = ArchitectureSelector()
        self.scheduler = DistillationScheduler(
            memory_threshold=memory_threshold,
            time_window_seconds=time_window_seconds,
            compute_window=compute_window,
        )
        self.generator = SyntheticDataGenerator()
        self.adapter_manager = LoRAAdapterManager()
        self.mitigator = ForgettingMitigator(replay_buffer_size=replay_buffer_size)

        self._lock = threading.RLock()
        self._total_distilled = 0

    def ingest_memory(
        self,
        content: str,
        entity_type: str = "fact",
        confidence: float = 1.0,
        tags: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """摄入单条记忆，触发蒸馏流水线。"""
        entity = MemoryEntity(
            entity_id=f"mem_{uuid.uuid4().hex[:12]}",
            content=content,
            entity_type=entity_type,
            confidence=confidence,
            tags=tags or [],
        )

        batch = self.scheduler.ingest(entity)
        if batch is None:
            return None

        return self._process_batch(batch)

    def _process_batch(self, batch: DistillationBatch) -> Dict[str, Any]:
        """处理蒸馏批次：架构选择 → 合成数据 → 适配器管理 → 遗忘缓解。"""
        result = {"batch_id": batch.batch_id, "entities": len(batch.entities)}

        # 1. 架构选择
        memory_count = len(batch.entities)
        arch = self.selector.select(
            memory_count=memory_count,
            latency_budget_ms=10000.0,
            task_complexity=0.5,
        )
        batch.target_architecture = arch
        result["architecture"] = arch.value

        # 2. 合成数据
        pairs = self.generator.generate(batch.entities)
        result["synthetic_pairs"] = len(pairs)

        # 3. LoRA 适配器
        adapter = self.adapter_manager.create_adapter(
            name=f"distill_{batch.batch_id}",
            parent_model="trinity",
        )
        adapter.source_entities = [e.entity_id for e in batch.entities]
        result["adapter_id"] = adapter.adapter_id

        # 4. 遗忘缓解——添加经验回放
        for entity in batch.entities[:100]:
            experience = ReplayExperience(
                experience_id=f"replay_{uuid.uuid4().hex[:12]}",
                memory_entity=entity,
                original_task="distillation",
                outcome_label=1.0,
                architecture_used=arch,
            )
            self.mitigator.add_experience(experience)

        self.scheduler.mark_batch_complete(batch.batch_id)
        self._total_distilled += len(batch.entities)
        return result

    def statistics(self) -> Dict[str, Any]:
        """返回管道整体统计。"""
        with self._lock:
            return {
                "total_distilled": self._total_distilled,
                "selector": self.selector.statistics(),
                "scheduler": self.scheduler.statistics(),
                "generator": self.generator.statistics(),
                "adapter_manager": self.adapter_manager.statistics(),
                "mitigator": self.mitigator.statistics(),
            }
