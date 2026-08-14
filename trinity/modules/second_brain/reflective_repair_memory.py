"""
Safety Sidecar — Reflective Repair Exemplar Memory for Safe Agentic AI (ACL 2026).

三元语: 反射修复范例记忆系统——存储和检索修复范例的反射记忆库, 配合外部验证器
门控在动作释放前执行风险审查, 并由闭环反射控制器持续监控决策轨迹并强制执行风险
缓解修订。

设计要点:
  - ReflectiveRepairMemory: 反射记忆核心, 存储 RepairExemplar 条目并支持
    基于语义相似度的检索。
  - RepairExemplarRetriever: 基于证据的多跳检索器, 跨记忆库关联修复范例。
  - ExternalVerifierGate: 外部验证器门控, 在动作释放和记忆更新前执行独立验证。
  - ClosedLoopReflectionController: 闭环反射控制器, 监控决策轨迹, 当检测到
    风险信号时触发修复并强制执行缓解修订。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RiskLevel(Enum):
    """风险等级。"""
    SAFE = auto()           # 无风险
    LOW = auto()            # 低风险
    MEDIUM = auto()         # 中风险
    HIGH = auto()           # 高风险
    CRITICAL = auto()       # 严重风险: 强制阻断


class RepairStatus(Enum):
    """修复状态。"""
    PENDING = auto()        # 待修复
    RETRIEVED = auto()      # 已检索到范例
    APPLIED = auto()        # 已应用修复
    VERIFIED = auto()       # 已通过验证
    REJECTED = auto()       # 已拒绝


class GateDecision(Enum):
    """门控决策。"""
    ALLOW = auto()          # 放行
    BLOCK = auto()          # 阻断
    REVIEW = auto()         # 转人工复核
    MITIGATE = auto()       # 自动缓解


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class RepairExemplar:
    """修复范例条目。

    存储一个完整的修复案例: 错误模式、修复动作、结果及元数据。
    """
    exemplar_id: str
    error_pattern: str          # 错误模式描述
    repair_action: str          # 修复动作描述
    outcome: str                # 修复结果
    risk_level: RiskLevel = RiskLevel.LOW
    tags: List[str] = field(default_factory=list)
    embedding: Optional[np.ndarray] = None   # 嵌入向量 (用于相似度检索)
    created_at: float = field(default_factory=time.time)
    usage_count: int = 0


@dataclass
class GateVerification:
    """外部验证器门控结果。"""
    decision: GateDecision = GateDecision.REVIEW
    confidence: float = 0.0          # 置信度 [0, 1]
    evidence_chain: List[str] = field(default_factory=list)   # 证据链
    risk_signals: List[RiskLevel] = field(default_factory=list)
    rationale: str = ""
    verified_at: float = field(default_factory=time.time)


@dataclass
class ReflectionLoopRecord:
    """闭环反射周期记录。"""
    cycle_id: str
    trigger_event: str              # 触发反射的事件
    detected_risk: RiskLevel = RiskLevel.SAFE
    retrieved_exemplars: List[str] = field(default_factory=list)
    applied_repairs: List[str] = field(default_factory=list)
    gate_result: GateDecision = GateDecision.REVIEW
    final_verdict: str = ""
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Core Classes
# ---------------------------------------------------------------------------

class ReflectiveRepairMemory:
    """反射修复范例记忆库。

    存储 RepairExemplar 条目, 支持基于嵌入向量余弦相似度的检索和十字
    参考链接 (cross-reference), 以及记忆的读写锁安全并发访问。

    Parameters
    ----------
    max_capacity : int
        最大存储范例数。
    embedding_dim : int
        嵌入向量维度。
    """

    def __init__(
        self,
        max_capacity: int = 10000,
        embedding_dim: int = 768,
    ) -> None:
        self.max_capacity = max_capacity
        self.embedding_dim = embedding_dim
        self._memory: Dict[str, RepairExemplar] = {}
        self._lock = threading.RLock()
        self._hit_count: int = 0
        self._miss_count: int = 0
        logger.info("ReflectiveRepairMemory initialized [capacity=%d dim=%d]", max_capacity, embedding_dim)

    def store(self, exemplar: RepairExemplar) -> str:
        """存储修复范例。

        Parameters
        ----------
        exemplar : RepairExemplar
            要存储的修复范例。

        Returns
        -------
        str
            范例 ID。
        """
        with self._lock:
            if len(self._memory) >= self.max_capacity:
                self._evict_lru()
            self._memory[exemplar.exemplar_id] = exemplar
            return exemplar.exemplar_id

    def retrieve(
        self,
        query_embedding: np.ndarray,
        top_k: int = 5,
        min_similarity: float = 0.5,
    ) -> List[Tuple[RepairExemplar, float]]:
        """基于嵌入相似度检索修复范例。

        Parameters
        ----------
        query_embedding : np.ndarray
            查询嵌入向量。
        top_k : int
            返回前 k 条。
        min_similarity : float
            最小余弦相似度阈值。

        Returns
        -------
        List[Tuple[RepairExemplar, float]]
            (范例, 相似度) 列表, 按相似度降序。
        """
        with self._lock:
            results: List[Tuple[RepairExemplar, float]] = []
            for exemplar in self._memory.values():
                if exemplar.embedding is not None:
                    sim = self._cosine_similarity(query_embedding, exemplar.embedding)
                    if sim >= min_similarity:
                        results.append((exemplar, sim))

            results.sort(key=lambda x: x[1], reverse=True)
            top = results[:top_k]

            if top:
                self._hit_count += 1
                for ex, _ in top:
                    ex.usage_count += 1
            else:
                self._miss_count += 1

            return top

    def get_by_tags(self, tags: List[str], limit: int = 20) -> List[RepairExemplar]:
        """按标签检索范例。"""
        with self._lock:
            matched = []
            for ex in self._memory.values():
                if any(t in ex.tags for t in tags):
                    matched.append(ex)
            return sorted(matched, key=lambda e: e.usage_count, reverse=True)[:limit]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "memory_size": len(self._memory),
                "max_capacity": self.max_capacity,
                "hit_count": self._hit_count,
                "miss_count": self._miss_count,
                "hit_rate": self._hit_count / max(self._hit_count + self._miss_count, 1),
            }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a < 1e-9 or norm_b < 1e-9:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    def _evict_lru(self) -> None:
        """驱逐最久未使用的范例。"""
        if not self._memory:
            return
        oldest = min(self._memory.values(), key=lambda e: e.created_at)
        del self._memory[oldest.exemplar_id]
        logger.debug("Evicted repair exemplar %s (LRU)", oldest.exemplar_id)


class RepairExemplarRetriever:
    """基于证据的修复范例检索器。

    执行多跳检索: 先按错误模式定位候选集, 再跨记忆库关联相关范例,
    最终输出 (范例, 置信度) 列表。

    Parameters
    ----------
    memory : ReflectiveRepairMemory
        反射记忆库实例。
    """

    def __init__(self, memory: ReflectiveRepairMemory) -> None:
        self.memory = memory
        self._lock = threading.RLock()
        self._retrieval_count: int = 0
        logger.info("RepairExemplarRetriever initialized")

    def retrieve_by_evidence(
        self,
        error_description: str,
        evidence_tags: List[str],
        top_k: int = 5,
    ) -> List[Tuple[RepairExemplar, float]]:
        """基于证据链检索修复范例。

        Parameters
        ----------
        error_description : str
            错误模式描述。
        evidence_tags : List[str]
            证据标签列表。
        top_k : int
            返回前 k 条。

        Returns
        -------
        List[Tuple[RepairExemplar, float]]
            (范例, 置信度) 列表。
        """
        with self._lock:
            # 生成查询嵌入 (简化为哈希散列)
            query_embedding = self._hash_to_embedding(
                error_description, self.memory.embedding_dim
            )
            # 检索
            results = self.memory.retrieve(query_embedding, top_k=top_k)

            # 按证据标签加权
            weighted: List[Tuple[RepairExemplar, float]] = []
            for ex, sim in results:
                tag_bonus = sum(1.0 for t in evidence_tags if t in ex.tags) / max(len(evidence_tags), 1)
                weighted.append((ex, min(1.0, sim + 0.15 * tag_bonus)))

            weighted.sort(key=lambda x: x[1], reverse=True)
            self._retrieval_count += 1
            return weighted[:top_k]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "retrieval_count": self._retrieval_count,
            }

    @staticmethod
    def _hash_to_embedding(text: str, dim: int) -> np.ndarray:
        """将文本哈希映射为伪嵌入向量 (生产环境替换为真实编码器)。"""
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:16], 16)
        rng = np.random.RandomState(seed % (2**31 - 1))
        vec = rng.randn(dim)
        return vec / (np.linalg.norm(vec) + 1e-8)


class ExternalVerifierGate:
    """外部验证器门控。

    在动作释放和记忆更新前执行独立安全验证, 输出 GateDecision。
    支持可配置的风险规则引擎和置信度校准。

    Parameters
    ----------
    risk_thresholds : Dict[RiskLevel, float]
        各风险等级的置信度阈值。
    rules : Optional[List[Callable[[Dict[str, Any]], Tuple[bool, str]]]]
        自定义验证规则列表。
    """

    def __init__(
        self,
        risk_thresholds: Optional[Dict[RiskLevel, float]] = None,
        rules: Optional[List[Callable[[Dict[str, Any]], Tuple[bool, str]]]] = None,
    ) -> None:
        self.risk_thresholds = risk_thresholds or {
            RiskLevel.SAFE: 0.95,
            RiskLevel.LOW: 0.85,
            RiskLevel.MEDIUM: 0.70,
            RiskLevel.HIGH: 0.50,
            RiskLevel.CRITICAL: 0.30,
        }
        self.rules = rules or []
        self._lock = threading.RLock()
        self._gate_count: int = 0
        self._block_count: int = 0
        logger.info("ExternalVerifierGate initialized [rules=%d]", len(self.rules))

    def verify(
        self,
        action_description: str,
        retrieved_exemplars: List[RepairExemplar],
        risk_signals: Optional[List[RiskLevel]] = None,
    ) -> GateVerification:
        """验证动作安全性并输出门控决策。

        Parameters
        ----------
        action_description : str
            待验证的动作描述。
        retrieved_exemplars : List[RepairExemplar]
            检索到的修复范例。
        risk_signals : Optional[List[RiskLevel]]
            外部风险信号。

        Returns
        -------
        GateVerification
            门控验证结果。
        """
        with self._lock:
            self._gate_count += 1
            signals = risk_signals or []

            # 评估风险等级
            if retrieved_exemplars:
                max_risk = max(
                    (e.risk_level for e in retrieved_exemplars),
                    key=lambda r: (RiskLevel.CRITICAL.value, RiskLevel.HIGH.value, RiskLevel.MEDIUM.value, RiskLevel.LOW.value, RiskLevel.SAFE.value).index(r.name),
                )
            else:
                max_risk = RiskLevel.LOW

            # 计算置信度
            exemplar_count = len(retrieved_exemplars)
            confidence = min(1.0, 0.5 + 0.1 * min(exemplar_count, 5))

            # 应用自定义规则
            evidence_chain: List[str] = []
            for rule in self.rules:
                passed, rationale = rule({"action": action_description, "exemplars": retrieved_exemplars, "signals": signals})
                evidence_chain.append(rationale)
                if not passed:
                    confidence *= 0.7

            # 决策
            threshold = self.risk_thresholds.get(max_risk, 0.7)
            if max_risk == RiskLevel.CRITICAL:
                decision = GateDecision.BLOCK
                self._block_count += 1
            elif max_risk == RiskLevel.HIGH:
                decision = GateDecision.MITIGATE if confidence > 0.4 else GateDecision.BLOCK
            elif confidence >= threshold:
                decision = GateDecision.ALLOW
            elif confidence >= threshold - 0.15:
                decision = GateDecision.MITIGATE
            else:
                decision = GateDecision.REVIEW

            return GateVerification(
                decision=decision,
                confidence=confidence,
                evidence_chain=evidence_chain,
                risk_signals=signals,
                rationale=f"Max risk={max_risk.name}, confidence={confidence:.3f}, threshold={threshold:.3f}",
            )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "gate_count": self._gate_count,
                "block_count": self._block_count,
                "block_rate": self._block_count / max(self._gate_count, 1),
            }


class ClosedLoopReflectionController:
    """闭环反射控制器。

    持续监控决策轨迹, 当检测到风险信号时触发反射-修复-验证闭环:
    (1) 从 ReflectiveRepairMemory 检索修复范例;
    (2) 通过 ExternalVerifierGate 验证修复方案;
    (3) 强制执行风险缓解修订并记录反射周期。

    Parameters
    ----------
    memory : ReflectiveRepairMemory
        反射记忆库。
    retriever : RepairExemplarRetriever
        范例检索器。
    gate : ExternalVerifierGate
        外部验证器门控。
    max_reflection_depth : int
        最大反射深度 (防止无限循环)。
    """

    def __init__(
        self,
        memory: ReflectiveRepairMemory,
        retriever: RepairExemplarRetriever,
        gate: ExternalVerifierGate,
        max_reflection_depth: int = 3,
    ) -> None:
        self.memory = memory
        self.retriever = retriever
        self.gate = gate
        self.max_reflection_depth = max_reflection_depth
        self._lock = threading.RLock()
        self._cycles: List[ReflectionLoopRecord] = []
        self._active: bool = True
        logger.info("ClosedLoopReflectionController initialized [depth=%d]", max_reflection_depth)

    def monitor_and_repair(
        self,
        decision_trajectory: List[Dict[str, Any]],
        risk_signals: Optional[List[RiskLevel]] = None,
    ) -> ReflectionLoopRecord:
        """监控决策轨迹并执行反射修复闭环。

        Parameters
        ----------
        decision_trajectory : List[Dict[str, Any]]
            决策轨迹 (每步包含 action / context / outcome)。
        risk_signals : Optional[List[RiskLevel]]
            外部传入的风险信号。

        Returns
        -------
        ReflectionLoopRecord
            反射周期记录, 包含修复结果和最终裁决。
        """
        with self._lock:
            start_time = time.time()
            cycle_id = hashlib.md5(
                f"reflect_{len(self._cycles)}_{time.time()}".encode()
            ).hexdigest()[:16]

            signals = risk_signals or []
            triggered = bool(signals) and max(signals, key=lambda r: r.value) != RiskLevel.SAFE if signals else False
            record = ReflectionLoopRecord(
                cycle_id=cycle_id,
                trigger_event=f"Decision trajectory length={len(decision_trajectory)}, signals={[s.name for s in signals]}",
                detected_risk=max(signals, key=lambda r: r.value) if signals else RiskLevel.SAFE,
            )

            if not triggered or not self._active:
                record.final_verdict = "No risk detected — no repair needed."
                record.duration_ms = (time.time() - start_time) * 1000
                self._cycles.append(record)
                return record

            # 多轮反射闭环
            for depth in range(self.max_reflection_depth):
                # (1) 检索修复范例
                error_desc = "; ".join(
                    str(s.get("action", "")) for s in decision_trajectory[-3:]
                )
                results = self.retriever.retrieve_by_evidence(
                    error_description=error_desc,
                    evidence_tags=["safety", "repair", "reflection"],
                    top_k=3,
                )

                exemplar_ids = [e.exemplar_id for e, _ in results]
                record.retrieved_exemplars.extend(exemplar_ids)

                if not results:
                    break

                # (2) 门控验证
                verification = self.gate.verify(
                    action_description=error_desc,
                    retrieved_exemplars=[e for e, _ in results],
                    risk_signals=signals,
                )
                record.gate_result = verification.decision

                if verification.decision == GateDecision.ALLOW:
                    best_exemplar, _ = results[0]
                    record.applied_repairs.append(best_exemplar.repair_action)
                    record.final_verdict = f"Repaired: {best_exemplar.repair_action}"
                    break
                elif verification.decision == GateDecision.BLOCK:
                    self.gate._block_count += 1  # type: ignore[has-type]
                    record.final_verdict = f"BLOCKED at depth {depth + 1}: {verification.rationale}"
                    break
                elif verification.decision == GateDecision.MITIGATE:
                    best_exemplar, _ = results[0]
                    record.applied_repairs.append(best_exemplar.repair_action)
                    # 降低风险后继续
                    signals = [RiskLevel.LOW]
                else:
                    record.final_verdict = f"REVIEW at depth {depth + 1}"
                    break
            else:
                record.final_verdict = f"Max reflection depth ({self.max_reflection_depth}) reached."

            record.duration_ms = (time.time() - start_time) * 1000
            self._cycles.append(record)
            return record

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_cycles": len(self._cycles),
                "active": self._active,
                "max_depth": self.max_reflection_depth,
                "recent_verdicts": [c.final_verdict for c in self._cycles[-5:]],
            }
