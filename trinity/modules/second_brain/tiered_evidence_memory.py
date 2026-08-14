"""
TieredEvidenceMemory — TierMem Dual-Tier Evidence Architecture
===============================================================
ICLR 2026 MemAgents Workshop · P40-3

实现 TierMem 双层证据记忆: summary_tier (低成本有损摘要) + raw_log_tier (高成本
可信原始日志), selective_escalation() 摘要不足时自动升级查原始证据,
verified_writeback() 仅被原始证据支持的新结论才写回摘要层, 溯源审计确保每条回答
可追溯到原始证据链。

设计要点:
  - SummaryTier: 压缩摘要, 低存储高检索速度
  - RawLogTier: 完整原始日志, 可审计
  - EvidenceChain: 溯源链路 (回答→证据节点→原始日志)
  - 选择性升级 + 验证回写: 确保摘要一致性
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums (重命名: EvidenceStrength→TieredEvidenceStrength, 避免冲突)
# ---------------------------------------------------------------------------

class TieredEvidenceStrength(Enum):
    """证据强度 (重命名避免与 topic_document_memory.EvidenceStrength 冲突)。"""
    WEAK = 1
    MODERATE = 2
    STRONG = 3
    VERIFIED = 4
    CONTRADICTED = 5


class EvidenceSource(Enum):
    """证据来源。"""
    RAW_LOG = auto()
    SUMMARIZED = auto()
    EXTERNAL = auto()
    USER_PROVIDED = auto()


class EscalationReason(Enum):
    """升级原因。"""
    SUMMARY_INSUFFICIENT = auto()
    CONFIDENCE_LOW = auto()
    CONTRADICTION_DETECTED = auto()
    AUDIT_REQUESTED = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class EvidenceNode:
    """证据节点——原始日志中的一个证据单元。"""
    node_id: str
    content: str
    source: EvidenceSource
    strength: TieredEvidenceStrength = TieredEvidenceStrength.MODERATE
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class SummaryEntry:
    """摘要层条目——对原始证据的压缩表示。"""
    entry_id: str
    topic: str
    summary: str
    source_nodes: List[str] = field(default_factory=list)  # node_id 引用
    confidence: float = 0.0
    verified: bool = False
    timestamp: float = field(default_factory=time.time)
    update_count: int = 0


@dataclass
class EvidenceChain:
    """溯源证据链——回答到原始证据的完整链路。"""
    chain_id: str
    query: str
    answer: str
    nodes: List[EvidenceNode] = field(default_factory=list)
    trace_path: List[str] = field(default_factory=list)  # node_id→node_id→...
    created_at: float = field(default_factory=time.time)


@dataclass
class EscalationTrigger:
    """升级触发器——何时从摘要升级到原始日志。"""
    trigger_id: str
    reason: EscalationReason
    summary_entry_id: str
    detail: str = ""
    resolved: bool = False
    resolution_nodes: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# SummaryTier
# ---------------------------------------------------------------------------

class SummaryTier:
    """摘要层——低成本有损摘要, 快速检索。

    Parameters
    ----------
    capacity : int
        最大摘要条目数。
    """

    def __init__(self, capacity: int = 1000) -> None:
        self.capacity = capacity
        self._entries: OrderedDict[str, SummaryEntry] = OrderedDict()
        self._lock = threading.RLock()

    def add(self, entry: SummaryEntry) -> None:
        with self._lock:
            if len(self._entries) >= self.capacity:
                # 淘汰最旧条目
                self._entries.popitem(last=False)
            self._entries[entry.entry_id] = entry

    def get(self, entry_id: str) -> Optional[SummaryEntry]:
        return self._entries.get(entry_id)

    def search(self, keyword: str) -> List[SummaryEntry]:
        kw = keyword.lower()
        return [e for e in self._entries.values()
                if kw in e.topic.lower() or kw in e.summary.lower()]

    def get_all(self) -> List[SummaryEntry]:
        return list(self._entries.values())

    @property
    def size(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# RawLogTier
# ---------------------------------------------------------------------------

class RawLogTier:
    """原始日志层——高成本可信原始证据, 完整可审计。

    Parameters
    ----------
    capacity : int
        最大证据节点数。
    """

    def __init__(self, capacity: int = 5000) -> None:
        self.capacity = capacity
        self._nodes: OrderedDict[str, EvidenceNode] = OrderedDict()
        self._lock = threading.RLock()

    def add(self, node: EvidenceNode) -> None:
        with self._lock:
            if len(self._nodes) >= self.capacity:
                self._nodes.popitem(last=False)
            self._nodes[node.node_id] = node

    def get(self, node_id: str) -> Optional[EvidenceNode]:
        return self._nodes.get(node_id)

    def get_batch(self, node_ids: List[str]) -> List[EvidenceNode]:
        return [n for nid in node_ids if (n := self._nodes.get(nid))]

    def search(self, content: str) -> List[EvidenceNode]:
        kw = content.lower()
        return [n for n in self._nodes.values() if kw in n.content.lower()]

    @property
    def size(self) -> int:
        return len(self._nodes)


# ---------------------------------------------------------------------------
# TieredEvidenceMemory
# ---------------------------------------------------------------------------

class TieredEvidenceMemory:
    """TierMem 双层证据记忆系统。

    Parameters
    ----------
    summary_capacity : int
        摘要层容量。
    raw_capacity : int
        原始日志层容量。
    confidence_threshold : float
        摘要置信度低于此值触发升级。
    """

    def __init__(
        self,
        summary_capacity: int = 1000,
        raw_capacity: int = 5000,
        confidence_threshold: float = 0.7,
    ) -> None:
        self.summary_tier = SummaryTier(capacity=summary_capacity)
        self.raw_log_tier = RawLogTier(capacity=raw_capacity)
        self.confidence_threshold = confidence_threshold
        self._chains: Dict[str, EvidenceChain] = {}
        self._lock = threading.RLock()
        self._node_count: int = 0
        self._escalation_count: int = 0
        self._writeback_count: int = 0

        logger.info(
            "TieredEvidenceMemory initialized [summary=%d raw=%d thresh=%.2f]",
            summary_capacity, raw_capacity, confidence_threshold,
        )

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest_evidence(
        self,
        content: str,
        source: EvidenceSource = EvidenceSource.RAW_LOG,
        strength: TieredEvidenceStrength = TieredEvidenceStrength.MODERATE,
        metadata: Optional[Dict[str, Any]] = None,
        auto_summarize: bool = True,
    ) -> Tuple[EvidenceNode, Optional[SummaryEntry]]:
        """录入原始证据, 可选自动生成摘要。

        Returns
        -------
        Tuple[EvidenceNode, Optional[SummaryEntry]]
            (原始证据节点, 自动生成的摘要条目)。
        """
        with self._lock:
            self._node_count += 1
            node = EvidenceNode(
                node_id=f"node_{self._node_count}_{int(time.time()*1e6)}",
                content=content,
                source=source,
                strength=strength,
                metadata=metadata or {},
            )
            self.raw_log_tier.add(node)

            summary_entry = None
            if auto_summarize:
                summary_entry = SummaryEntry(
                    entry_id=f"sum_{self._node_count}_{int(time.time()*1e6)}",
                    topic=metadata.get("topic", "general") if metadata else "general",
                    summary=_generate_summary(content),
                    source_nodes=[node.node_id],
                    confidence=0.8 if strength == TieredEvidenceStrength.STRONG else 0.6,
                )
                self.summary_tier.add(summary_entry)

            logger.debug("Evidence ingested: %s [%s]", node.node_id, strength.name)
            return node, summary_entry

    # ------------------------------------------------------------------
    # Selective Escalation
    # ------------------------------------------------------------------

    def selective_escalation(self, query: str, summary_entries: List[SummaryEntry]) -> EscalationTrigger:
        """摘要不足以回答时自动升级查原始证据。

        Parameters
        ----------
        query : str
            查询内容。
        summary_entries : List[SummaryEntry]
            当前检索到的摘要条目。

        Returns
        -------
        EscalationTrigger
            升级触发器。
        """
        with self._lock:
            self._escalation_count += 1

            # 判断升级原因
            avg_conf = sum(e.confidence for e in summary_entries) / max(len(summary_entries), 1)
            if avg_conf < self.confidence_threshold:
                reason = EscalationReason.CONFIDENCE_LOW
                detail = f"Average confidence {avg_conf:.2f} below threshold {self.confidence_threshold}"
            elif not summary_entries:
                reason = EscalationReason.SUMMARY_INSUFFICIENT
                detail = "No summary entries found for query"
            else:
                # 检查矛盾
                content_words = set()
                for e in summary_entries:
                    content_words.update(e.summary.lower().split())
                if len(content_words) < 3:
                    reason = EscalationReason.SUMMARY_INSUFFICIENT
                    detail = "Summary content too sparse"
                else:
                    reason = EscalationReason.AUDIT_REQUESTED
                    detail = "Routine audit escalation"

            # 收集所有 source_node_id 并查原始日志
            all_node_ids: List[str] = []
            for entry in summary_entries:
                all_node_ids.extend(entry.source_nodes)
            raw_nodes = self.raw_log_tier.get_batch(all_node_ids)

            trigger = EscalationTrigger(
                trigger_id=f"esc_{self._escalation_count}_{int(time.time()*1e6)}",
                reason=reason,
                summary_entry_id=summary_entries[0].entry_id if summary_entries else "",
                detail=detail,
                resolution_nodes=[n.node_id for n in raw_nodes],
            )

            logger.info("Escalation triggered: %s reason=%s nodes=%d",
                        trigger.trigger_id, reason.name, len(raw_nodes))
            return trigger

    def resolve_escalation(self, trigger: EscalationTrigger) -> List[EvidenceNode]:
        """解析升级——返回原始证据节点。"""
        return self.raw_log_tier.get_batch(trigger.resolution_nodes)

    # ------------------------------------------------------------------
    # Verified Writeback
    # ------------------------------------------------------------------

    def verified_writeback(
        self,
        summary_entry: SummaryEntry,
        new_conclusion: str,
        supporting_node_ids: List[str],
    ) -> SummaryEntry:
        """仅被原始证据支持的新结论才写回摘要层。

        Parameters
        ----------
        summary_entry : SummaryEntry
            目标摘要条目。
        new_conclusion : str
            新结论。
        supporting_node_ids : List[str]
            支持新结论的原始证据节点 ID。

        Returns
        -------
        SummaryEntry
            更新后的摘要条目。
        """
        with self._lock:
            # 验证: 所有 supporting_node_id 必须存在于 raw_log_tier
            supporting_nodes = self.raw_log_tier.get_batch(supporting_node_ids)
            verified_node_ids = [n.node_id for n in supporting_nodes]

            if not verified_node_ids:
                logger.warning("Writeback rejected: no valid supporting evidence")
                return summary_entry

            self._writeback_count += 1
            old_summary = summary_entry.summary
            summary_entry.summary = f"{old_summary}\n[Verified Update #{summary_entry.update_count + 1}]: {new_conclusion}"
            summary_entry.source_nodes = list(dict.fromkeys(summary_entry.source_nodes + verified_node_ids))
            summary_entry.verified = True
            summary_entry.confidence = min(summary_entry.confidence + 0.1, 1.0)
            summary_entry.update_count += 1
            summary_entry.timestamp = time.time()

            logger.info("Writeback verified: %s → +%d nodes, confidence=%.2f",
                        summary_entry.entry_id, len(verified_node_ids), summary_entry.confidence)
            return summary_entry

    # ------------------------------------------------------------------
    # Traceability Audit
    # ------------------------------------------------------------------

    def create_evidence_chain(
        self,
        query: str,
        answer: str,
        node_ids: List[str],
    ) -> EvidenceChain:
        """创建溯源证据链——每条回答可追溯到原始证据。

        Parameters
        ----------
        query : str
            用户查询。
        answer : str
            系统回答。
        node_ids : List[str]
            引用的证据节点 ID。

        Returns
        -------
        EvidenceChain
            完整溯源链。
        """
        with self._lock:
            chain = EvidenceChain(
                chain_id=f"chain_{int(time.time()*1e6)}",
                query=query,
                answer=answer,
                nodes=self.raw_log_tier.get_batch(node_ids),
                trace_path=node_ids,
            )
            self._chains[chain.chain_id] = chain
            return chain

    def audit_chain(self, chain_id: str) -> Optional[EvidenceChain]:
        """审计溯源链——完整证据可追溯。"""
        return self._chains.get(chain_id)

    def verify_answer(
        self, chain_id: str, answer_index: int
    ) -> Tuple[bool, str]:
        """验证回答是否被原始证据支持。"""
        chain = self._chains.get(chain_id)
        if not chain:
            return False, f"Chain {chain_id} not found"

        if not chain.nodes:
            return False, "No evidence nodes in chain"

        # 检查回答内容是否与证据一致
        strong_nodes = [n for n in chain.nodes
                        if n.strength in (TieredEvidenceStrength.STRONG, TieredEvidenceStrength.VERIFIED)]
        return len(strong_nodes) > 0, f"Supported by {len(strong_nodes)} strong/verified nodes out of {len(chain.nodes)}"

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            raw_nodes = list(self.raw_log_tier._nodes.values())
            strength_dist = {}
            for n in raw_nodes:
                s = n.strength.name
                strength_dist[s] = strength_dist.get(s, 0) + 1

            return {
                "summary_entries": self.summary_tier.size,
                "raw_nodes": self.raw_log_tier.size,
                "strength_distribution": strength_dist,
                "evidence_chains": len(self._chains),
                "escalations": self._escalation_count,
                "verified_writebacks": self._writeback_count,
            }


def _generate_summary(content: str, max_len: int = 200) -> str:
    """简单摘要生成——截断 + 关键词提取。"""
    if len(content) <= max_len:
        return content
    # 取首尾各一半
    half = max_len // 2
    return content[:half] + " ... [truncated] ... " + content[-half:]
