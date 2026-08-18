"""
# status: orphan (2026-08-15 audit, not in runtime path)
SocialHippocampusMemory — Hippocampal-Inspired Multi-Agent Memory Sharing
=========================================================================
arXiv 2603.25614 · P38-2 · SoHip

三元语: 社会海马体记忆学习。海马体启发的异构 Agent 间记忆共享
与巩固——短期记忆抽象 → 海马体巩固 → 集体长期融合，增强
本地预测在未见场景下的泛化能力。

设计要点:
  - SocialHippocampusMemory: 主控制器, 协调短期抽象/海马巩固/
    集体融合三条流水线, 维护 Agent 间的共享记忆库。
  - ShortTermMemoryAbstractor: 短期记忆抽象器, 从本地交互表征
    中提取关键事件、实体和关系, 生成 SocialMemoryPacket。
  - HippocampalConsolidationEngine: 海马体巩固引擎, 模拟海马体
    CA1-CA3-DG 回路模式分离与模式完成, 将短期记忆巩固为长期记忆。
  - CollectiveLongTermMemoryFuser: 集体长期记忆融合器, 聚合来自
    多个异构 Agent 的巩固记忆, 通过加权投票 + 置信度校准增强预测。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class HippoConsolidationPhase(Enum):
    """海马巩固阶段 (模拟海马体子区)。"""
    DG_PATTERN_SEPARATION = auto()  # 齿状回: 模式分离
    CA3_PATTERN_COMPLETION = auto()  # CA3: 模式完成
    CA1_INTEGRATION = auto()        # CA1: 信息整合
    CORTICAL_TRANSFER = auto()      # 皮层转移: 长期固化


class ConsolidationMode(Enum):
    """巩固模式。"""
    IMMEDIATE = auto()     # 即刻巩固 (高信息量)
    OFF_LINE = auto()     # 离线重放巩固
    INCREMENTAL = auto()  # 增量整合


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class SocialMemoryPacket:
    """短期记忆抽象包——Agent 间共享的记忆单元。"""
    packet_id: str
    agent_id: str
    content: str
    entities: List[str] = field(default_factory=list)
    relations: List[Tuple[str, str, str]] = field(default_factory=list)  # (sub, rel, obj)
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.time)
    embedding: Optional[np.ndarray] = None


@dataclass
class HippoConsolidationRecord:
    """海马巩固记录。"""
    record_id: str
    source_packet_id: str
    phase: HippoConsolidationPhase
    content: str
    separated_pattern: Optional[np.ndarray] = None   # DG 输出
    completed_pattern: Optional[np.ndarray] = None    # CA3 输出
    integrated_representation: Optional[np.ndarray] = None  # CA1 输出
    stability_score: float = 0.0
    consolidation_time: float = field(default_factory=time.time)


@dataclass
class CollectiveFusionResult:
    """集体融合结果。"""
    fusion_id: str
    fused_content: str
    contributors: List[str]     # 贡献 Agent ID 列表
    consensus_score: float      # 加权共识分数 [0, 1]
    confidence_calibrated: float
    prediction_enhancement: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class CollectiveMemoryEntry:
    """集体长期记忆条目。"""
    entry_id: str
    content: str
    fusion_result: Optional[CollectiveFusionResult] = None
    agent_sources: List[str] = field(default_factory=list)
    access_count: int = 0
    created_at: float = field(default_factory=time.time)
    embedding: Optional[np.ndarray] = None


# =============================================================================
# SocialHippocampusMemory
# =============================================================================

class SocialHippocampusMemory:
    """社会海马体记忆主控制器。

    Parameters
    ----------
    local_agent_id : str
        本地 Agent 标识。
    stm_capacity : int
        短期记忆包容量。
    ltm_capacity : int
        集体长期记忆容量。
    consolidation_mode : ConsolidationMode
        默认巩固模式。
    """

    def __init__(
        self,
        local_agent_id: str = "local",
        stm_capacity: int = 256,
        ltm_capacity: int = 4096,
        consolidation_mode: ConsolidationMode = ConsolidationMode.INCREMENTAL,
    ) -> None:
        self.local_agent_id = local_agent_id
        self.stm_capacity = stm_capacity
        self.ltm_capacity = ltm_capacity
        self.consolidation_mode = consolidation_mode

        self._lock = threading.RLock()
        self._abstractor = ShortTermMemoryAbstractor()
        self._consolidator = HippocampalConsolidationEngine()
        self._fuser = CollectiveLongTermMemoryFuser()

        self._stm: List[SocialMemoryPacket] = []
        self._ltm: Dict[str, CollectiveMemoryEntry] = {}
        self._agent_contributions: Dict[str, int] = {}
        self._share_count: int = 0

        logger.info("SocialHippocampusMemory initialized [agent=%s mode=%s]", local_agent_id, consolidation_mode.name)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_local_interaction(
        self,
        content: str,
        entities: Optional[List[str]] = None,
        relations: Optional[List[Tuple[str, str, str]]] = None,
    ) -> SocialMemoryPacket:
        """处理本地交互: 抽象为短期记忆包。"""
        with self._lock:
            packet = self._abstractor.abstract(
                agent_id=self.local_agent_id,
                content=content,
                entities=entities or [],
                relations=relations or [],
            )
            self._stm.append(packet)
            self._truncate_stm()
            return packet

    def consolidate(self, packets: Optional[List[SocialMemoryPacket]] = None) -> List[HippoConsolidationRecord]:
        """海马巩固: 短期记忆 → 长期记忆。"""
        with self._lock:
            target = packets or self._stm
            records = self._consolidator.consolidate(target, mode=self.consolidation_mode)
            return records

    def fuse(self, external_packets: List[SocialMemoryPacket]) -> CollectiveFusionResult:
        """集体融合: 聚合外部 Agent 的巩固记忆。"""
        with self._lock:
            result = self._fuser.fuse(external_packets)
            entry = CollectiveMemoryEntry(
                entry_id=f"cme_{uuid.uuid4().hex[:12]}",
                content=result.fused_content,
                fusion_result=result,
                agent_sources=result.contributors,
            )
            self._ltm[entry.entry_id] = entry
            for c in result.contributors:
                self._agent_contributions[c] = self._agent_contributions.get(c, 0) + 1
            self._truncate_ltm()
            return result

    def share_memory(self, agent_id: str, content: str) -> SocialMemoryPacket:
        """与另一个 Agent 共享记忆。"""
        with self._lock:
            self._share_count += 1
            packet = SocialMemoryPacket(
                packet_id=f"smp_share_{self._share_count}",
                agent_id=agent_id,
                content=content,
                confidence=0.9,
            )
            return packet

    def query_collective(self, query: str, top_k: int = 5) -> List[CollectiveMemoryEntry]:
        """查询集体长期记忆。"""
        with self._lock:
            scored = []
            q_hash = hashlib.sha256(query.encode()).digest()
            q_vec = np.frombuffer(q_hash[:64], dtype=np.float32)
            q_vec = q_vec / (np.linalg.norm(q_vec) + 1e-8)

            for entry in self._ltm.values():
                e_hash = hashlib.sha256(entry.content.encode()).digest()
                e_vec = np.frombuffer(e_hash[:64], dtype=np.float32)
                e_vec = e_vec / (np.linalg.norm(e_vec) + 1e-8)
                sim = float(np.dot(q_vec, e_vec))
                scored.append((entry, sim))

            scored.sort(key=lambda x: x[1], reverse=True)
            return [entry for entry, _ in scored[:top_k]]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _truncate_stm(self) -> None:
        while len(self._stm) > self.stm_capacity:
            oldest = min(self._stm, key=lambda p: p.timestamp)
            self._stm.remove(oldest)

    def _truncate_ltm(self) -> None:
        while len(self._ltm) > self.ltm_capacity:
            oldest = min(self._ltm.values(), key=lambda e: e.created_at)
            del self._ltm[oldest.entry_id]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "stm_size": len(self._stm),
                "ltm_size": len(self._ltm),
                "share_count": self._share_count,
                "agent_contributions": dict(self._agent_contributions),
                "abstractor": self._abstractor.statistics(),
                "consolidator": self._consolidator.statistics(),
                "fuser": self._fuser.statistics(),
            }


# =============================================================================
# ShortTermMemoryAbstractor
# =============================================================================

class ShortTermMemoryAbstractor:
    """短期记忆抽象器。

    从本地交互表征中提取关键事件、实体和关系, 生成可共享的
    SocialMemoryPacket。

    Parameters
    ----------
    max_entities : int
        单包最大实体数。
    max_relations : int
        单包最大关系数。
    """

    def __init__(self, max_entities: int = 32, max_relations: int = 16) -> None:
        self.max_entities = max_entities
        self.max_relations = max_relations
        self._lock = threading.RLock()
        self._abstracted_count: int = 0
        logger.info("ShortTermMemoryAbstractor initialized [ent=%d rel=%d]", max_entities, max_relations)

    def abstract(
        self,
        agent_id: str,
        content: str,
        entities: List[str],
        relations: List[Tuple[str, str, str]],
    ) -> SocialMemoryPacket:
        with self._lock:
            self._abstracted_count += 1

            # 去重 + 截断
            seen_ent: Set[str] = set()
            filtered_entities: List[str] = []
            for e in entities:
                norm = e.strip().lower()
                if norm not in seen_ent and len(filtered_entities) < self.max_entities:
                    seen_ent.add(norm)
                    filtered_entities.append(e)

            filtered_relations = relations[:self.max_relations]

            # 置信度: 实体越多置信越低 (不确定性上升)
            confidence = max(0.4, 1.0 - 0.02 * len(filtered_entities) / max(len(entities), 1))

            packet_id = f"smp_{self._abstracted_count}_{uuid.uuid4().hex[:8]}"
            return SocialMemoryPacket(
                packet_id=packet_id,
                agent_id=agent_id,
                content=content,
                entities=filtered_entities,
                relations=filtered_relations,
                confidence=confidence,
            )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"abstracted": self._abstracted_count}


# =============================================================================
# HippocampalConsolidationEngine
# =============================================================================

class HippocampalConsolidationEngine:
    """海马体巩固引擎。

    模拟海马体 DG→CA3→CA1→皮层四阶段巩固回路:
      DG: 模式分离 (正交化表示减少干扰)
      CA3: 模式完成 (关联记忆补全)
      CA1: 信息整合 (融合多源输入)
      皮层: 系统巩固 (稳定化写入长期存储)

    Parameters
    ----------
    pattern_dim : int
        模式表示维度。
    dg_sparsity : float
        DG 稀疏度 (0-1, 值越高越稀疏)。
    """

    def __init__(self, pattern_dim: int = 256, dg_sparsity: float = 0.05) -> None:
        self.pattern_dim = pattern_dim
        self.dg_sparsity = dg_sparsity
        self._lock = threading.RLock()
        self._consolidated: int = 0
        logger.info("HippocampalConsolidationEngine initialized [dim=%d dg=%.3f]", pattern_dim, dg_sparsity)

    def consolidate(
        self,
        packets: List[SocialMemoryPacket],
        mode: ConsolidationMode = ConsolidationMode.INCREMENTAL,
    ) -> List[HippoConsolidationRecord]:
        with self._lock:
            records: List[HippoConsolidationRecord] = []

            for packet in packets:
                record_id = f"hcr_{self._consolidated}_{uuid.uuid4().hex[:8]}"
                self._consolidated += 1

                # Phase 1: DG — pattern separation (稀疏正交化)
                raw = self._packet_to_vector(packet.content)
                mask = np.random.rand(self.pattern_dim) < self.dg_sparsity
                separated = raw * mask.astype(np.float32)
                separated = separated / (np.linalg.norm(separated) + 1e-8)

                phase = HippoConsolidationPhase.DG_PATTERN_SEPARATION

                # Phase 2: CA3 — pattern completion (关联恢复)
                if mode != ConsolidationMode.IMMEDIATE and len(packets) > 1:
                    neighbor_embeds = [self._packet_to_vector(p.content) for p in packets[:5]]
                    avg_neighbor = np.mean(neighbor_embeds, axis=0)
                    completed = 0.6 * separated + 0.4 * (avg_neighbor / (np.linalg.norm(avg_neighbor) + 1e-8))
                    phase = HippoConsolidationPhase.CA3_PATTERN_COMPLETION
                else:
                    completed = separated.copy()

                # Phase 3: CA1 — integration
                integrated = 0.7 * completed + 0.3 * raw
                integrated = integrated / (np.linalg.norm(integrated) + 1e-8)

                # Phase 4: cortical transfer — stability check
                stability = float(np.dot(integrated, raw))
                stability = max(0.0, min(1.0, stability))

                record = HippoConsolidationRecord(
                    record_id=record_id,
                    source_packet_id=packet.packet_id,
                    phase=phase,
                    content=packet.content,
                    separated_pattern=separated.copy(),
                    completed_pattern=completed.copy(),
                    integrated_representation=integrated.copy(),
                    stability_score=stability,
                )
                records.append(record)

            return records

    def _packet_to_vector(self, content: str) -> np.ndarray:
        h = hashlib.sha256(content.encode()).digest()
        vec = np.frombuffer(h * (self.pattern_dim // 32 + 1), dtype=np.uint8)[:self.pattern_dim].astype(np.float32)
        vec = (vec - 128.0) / 128.0
        return vec / (np.linalg.norm(vec) + 1e-8)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"consolidated": self._consolidated, "pattern_dim": self.pattern_dim}


# =============================================================================
# CollectiveLongTermMemoryFuser
# =============================================================================

class CollectiveLongTermMemoryFuser:
    """集体长期记忆融合器。

    聚合来自多个异构 Agent 的巩固记忆, 加权投票 + 置信度校准。

    Parameters
    ----------
    min_contributors : int
        最小贡献者数。
    confidence_decay : float
        置信度衰变系数。
    """

    def __init__(self, min_contributors: int = 2, confidence_decay: float = 0.95) -> None:
        self.min_contributors = min_contributors
        self.confidence_decay = confidence_decay
        self._lock = threading.RLock()
        self._fusion_count: int = 0
        logger.info("CollectiveLongTermMemoryFuser initialized [min=%d decay=%.2f]", min_contributors, confidence_decay)

    def fuse(self, packets: List[SocialMemoryPacket]) -> CollectiveFusionResult:
        with self._lock:
            self._fusion_count += 1

            if len(packets) < self.min_contributors:
                return CollectiveFusionResult(
                    fusion_id=f"cf_{self._fusion_count}",
                    fused_content="insufficient_contributors",
                    contributors=[p.agent_id for p in packets],
                    consensus_score=0.0,
                    confidence_calibrated=0.0,
                )

            # 加权投票: confidence 加权余弦相似度矩阵 → 中心向量
            agents = list({p.agent_id for p in packets})
            centroids: Dict[str, Tuple[List[str], float]] = {}

            for p in packets:
                h = hashlib.sha256(p.content.encode()).digest()
                vec = np.frombuffer(h[:64], dtype=np.float32)
                vec = vec / (np.linalg.norm(vec) + 1e-8)

                # 分配到最近的已有群组
                best_group = None
                best_sim = -1.0
                for group_id, (contents, _) in centroids.items():
                    for c in contents:
                        ch = hashlib.sha256(c.encode()).digest()
                        cv = np.frombuffer(ch[:64], dtype=np.float32)
                        cv = cv / (np.linalg.norm(cv) + 1e-8)
                        sim = float(np.dot(vec, cv))
                        if sim > best_sim:
                            best_sim = sim
                            best_group = group_id

                if best_sim >= 0.7 and best_group:
                    centroids[best_group][0].append(p.content)
                    centroids[best_group] = (centroids[best_group][0], centroids[best_group][1] + p.confidence)
                else:
                    centroids[p.packet_id] = ([p.content], p.confidence)

            # 选择最大权重群组
            best_id = max(centroids, key=lambda k: centroids[k][1])
            fused_content = centroids[best_id][0][0]

            total_weight = sum(p.confidence for p in packets)
            consensus_score = centroids[best_id][1] / max(total_weight, 1e-8)

            # 置信度校准
            calibrated = consensus_score * self.confidence_decay ** (self._fusion_count - 1)

            return CollectiveFusionResult(
                fusion_id=f"cf_{self._fusion_count}",
                fused_content=fused_content,
                contributors=agents,
                consensus_score=float(consensus_score),
                confidence_calibrated=float(calibrated),
                prediction_enhancement=float(max(0.0, calibrated - 0.5) * 0.3),
            )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"fusions": self._fusion_count, "min_contributors": self.min_contributors}
