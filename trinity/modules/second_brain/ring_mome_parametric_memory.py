"""
MoME Parametric Internal Memory for Agents
===========================================
arXiv 2608.01630 · P45-3

专家门控为 episodic/semantic/procedural 分配不同专家子网。
参数化内部记忆使用可学习参数矩阵做记忆编码 (非外部存储)。
MoME Router 路由到最相关专家子网并加权融合。

设计要点:
  - MoMEMemoryExpertGate: 专家门控, 分配不同类型记忆到对应专家
  - ParametricInternalMemory: 参数化内部记忆, 可学习参数矩阵编码
  - MoMEMemoryRouter: 查询路由到最相关专家, 加权融合
  - ExpertMemoryCapacity: 管理各专家容量与负载均衡

注意: 模块名 ring_mome_parametric_memory.py, 避免与已有 Ring 命名冲突。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MemoryExpertType(Enum):
    """记忆专家类型。"""
    EPISODIC = auto()
    SEMANTIC = auto()
    PROCEDURAL = auto()
    WORKING = auto()


# ---------------------------------------------------------------------------
# ParametricInternalMemory
# ---------------------------------------------------------------------------

class ParametricInternalMemory:
    """参数化内部记忆——用可学习参数矩阵做记忆编码。

    不使用外部存储, 而是通过参数矩阵 encode/retrieve。
    """

    def __init__(self, memory_dim: int = 128, num_slots: int = 64) -> None:
        self.memory_dim = memory_dim
        self.num_slots = num_slots
        # 可学习参数矩阵 (memory_slots × dim)
        self._memory_matrix: np.ndarray = np.random.randn(num_slots, memory_dim).astype(np.float32) * 0.01
        self._slot_usage: np.ndarray = np.zeros(num_slots, dtype=np.float32)
        self._lock = threading.RLock()

        logger.info("ParametricInternalMemory: %d slots × %d dim", num_slots, memory_dim)

    def encode(self, key: np.ndarray, value: np.ndarray) -> int:
        """编码 key-value 到参数矩阵。

        Returns
        -------
        int
            分配的 slot 索引。
        """
        with self._lock:
            # 找最空闲 slot
            slot = int(np.argmin(self._slot_usage))

            # 简化的写入: 加权平均更新
            alpha = 0.3  # 学习率
            target = (key[:self.memory_dim] + value[:self.memory_dim]) * 0.5
            # 对齐维度
            target_padded = np.zeros(self.memory_dim, dtype=np.float32)
            target_padded[:len(target)] = target
            self._memory_matrix[slot] = (
                (1 - alpha) * self._memory_matrix[slot] + alpha * target_padded
            )
            self._slot_usage[slot] = min(1.0, self._slot_usage[slot] + 0.1)

            return slot

    def retrieve(self, query: np.ndarray, top_k: int = 5) -> List[Tuple[int, float]]:
        """检索最相关的 memory slot。

        Returns
        -------
        List[Tuple[int, float]]
            (slot_index, similarity) 列表。
        """
        with self._lock:
            q = query[:self.memory_dim]
            q_norm = float(np.linalg.norm(q))
            if q_norm == 0:
                return []

            similarities = []
            for i in range(self.num_slots):
                slot_vec = self._memory_matrix[i]
                s_norm = float(np.linalg.norm(slot_vec))
                if s_norm == 0:
                    continue
                sim = float(np.dot(q, slot_vec)) / (q_norm * s_norm)
                similarities.append((i, sim))

            similarities.sort(key=lambda x: x[1], reverse=True)
            return similarities[:top_k]

    def forget(self, ratio: float = 0.1) -> int:
        """遗忘——衰减低使用率 slot。"""
        with self._lock:
            threshold = np.quantile(self._slot_usage, ratio)
            forgotten = 0
            for i in range(self.num_slots):
                if self._slot_usage[i] <= threshold:
                    self._memory_matrix[i] *= 0.5
                    self._slot_usage[i] = max(0.0, self._slot_usage[i] - 0.2)
                    forgotten += 1
            return forgotten

    def statistics(self) -> Dict[str, Any]:
        return {
            "num_slots": self.num_slots,
            "dim": self.memory_dim,
            "mean_usage": float(np.mean(self._slot_usage)),
        }


# ---------------------------------------------------------------------------
# ExpertMemoryCapacity
# ---------------------------------------------------------------------------

class ExpertMemoryCapacity:
    """专家容量与负载均衡管理器。"""

    def __init__(self, num_experts: int = 4, capacity_per_expert: int = 1000) -> None:
        self.num_experts = num_experts
        self.capacity_per_expert = capacity_per_expert
        self._usage: Dict[str, int] = {"EPISODIC": 0, "SEMANTIC": 0, "PROCEDURAL": 0, "WORKING": 0}
        self._lock = threading.RLock()

    def allocate(self, expert_type: MemoryExpertType) -> bool:
        """分配到专家。容量满则拒绝。"""
        with self._lock:
            name = expert_type.name
            if self._usage[name] >= self.capacity_per_expert:
                return False
            self._usage[name] += 1
            return True

    def release(self, expert_type: MemoryExpertType) -> None:
        with self._lock:
            self._usage[expert_type.name] = max(0, self._usage[expert_type.name] - 1)

    def load_factor(self, expert_type: MemoryExpertType) -> float:
        return self._usage[expert_type.name] / self.capacity_per_expert

    def statistics(self) -> Dict[str, Any]:
        return {
            name: {"used": u, "capacity": self.capacity_per_expert}
            for name, u in self._usage.items()
        }


# ---------------------------------------------------------------------------
# MoMEMemoryExpertGate
# ---------------------------------------------------------------------------

class MoMEMemoryExpertGate:
    """MoME 专家门控——为不同类型记忆分配不同专家子网。

    每个专家是一个 ParametricInternalMemory 实例。
    """

    def __init__(self, memory_dim: int = 128) -> None:
        self.memory_dim = memory_dim
        self._experts: Dict[MemoryExpertType, ParametricInternalMemory] = {
            MemoryExpertType.EPISODIC: ParametricInternalMemory(memory_dim, num_slots=32),
            MemoryExpertType.SEMANTIC: ParametricInternalMemory(memory_dim, num_slots=32),
            MemoryExpertType.PROCEDURAL: ParametricInternalMemory(memory_dim, num_slots=32),
            MemoryExpertType.WORKING: ParametricInternalMemory(memory_dim, num_slots=16),
        }
        self.capacity = ExpertMemoryCapacity()
        self._lock = threading.RLock()

    def gate(self, memory_type: MemoryExpertType, key: np.ndarray, value: np.ndarray) -> Optional[int]:
        """门控写入——分配对应专家并编码。"""
        with self._lock:
            if not self.capacity.allocate(memory_type):
                logger.warning("Expert %s at capacity, dropping entry", memory_type.name)
                return None

            expert = self._experts[memory_type]
            slot = expert.encode(key, value)
            return slot

    def retrieve_expert(self, memory_type: MemoryExpertType, query: np.ndarray, top_k: int = 5) -> List[Tuple[int, float]]:
        """从指定专家检索。"""
        return self._experts[memory_type].retrieve(query, top_k)

    def statistics(self) -> Dict[str, Any]:
        return {
            "capacity": self.capacity.statistics(),
            "experts": {t.name: e.statistics() for t, e in self._experts.items()},
        }


# ---------------------------------------------------------------------------
# MoMEMemoryRouter
# ---------------------------------------------------------------------------

class MoMEMemoryRouter:
    """MoME 记忆路由器——查询时路由到最相关专家子网, 返回加权融合结果。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def route(
        self, gate: MoMEMemoryExpertGate, query: np.ndarray, top_k: int = 3
    ) -> Dict[str, Any]:
        """路由查询到所有专家并加权融合。

        Returns
        -------
        Dict
            {results, fused_vector, expert_weights}
        """
        with self._lock:
            expert_results: Dict[str, List[Tuple[int, float]]] = {}
            expert_weights: Dict[str, float] = {}

            for mtype in MemoryExpertType:
                results = gate.retrieve_expert(mtype, query, top_k)
                expert_results[mtype.name] = results

                if results:
                    expert_weights[mtype.name] = float(np.mean([s for _, s in results]))
                else:
                    expert_weights[mtype.name] = 0.0

            # 加权融合: 从各专家的 top slot 取向量平均
            total_weight = sum(expert_weights.values())
            if total_weight == 0:
                return {"results": expert_results, "fused_vector": None, "expert_weights": expert_weights}

            fused = np.zeros(gate.memory_dim, dtype=np.float32)
            for mtype in MemoryExpertType:
                w = expert_weights[mtype.name] / total_weight
                results = expert_results[mtype.name]
                if results:
                    top_slot, _ = results[0]
                    fused += w * gate._experts[mtype]._memory_matrix[top_slot]

            return {
                "results": expert_results,
                "fused_vector": fused.tolist(),
                "expert_weights": expert_weights,
            }

    def statistics(self) -> Dict[str, Any]:
        return {"status": "ready"}
