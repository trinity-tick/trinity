"""
# status: orphan (2026-08-15 audit, not in runtime path)
TopicDocumentMemory — Infini Memory Semantic Unit Architecture
==============================================================
arXiv 2606.10677 · P39-1

将记忆组织为「主题文档」语义单元: 每条记忆归属于一个主题文档,
新观察通过 stage_buffer 暂存后定期 consolidate() 融合进主题文档;
支持 LLM agentic_retrieve() 迭代式阅读 (多次工具调用而非单次检索)。

设计要点:
  - TopicDocument: 一个主题的完整证据集合, 含事实列表与向量索引
  - StageBuffer: 环形缓冲区暂存新观察, 触发 consolidate 时批量融合
  - agentic_retrieve: 模拟 LLM 通过多轮工具调用逐层深入阅读主题文档
  - TopicCoordinator: 协调主题创建/合并/拆分, 维护主题间层级关系
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ConsolidationTrigger(Enum):
    """融合触发条件。"""
    BUFFER_FULL = auto()
    TIMER_EXPIRED = auto()
    MANUAL = auto()
    TOPIC_CLOSED = auto()


class EvidenceStrength(Enum):
    """证据强度等级。"""
    WEAK = auto()
    MODERATE = auto()
    STRONG = auto()
    CONFIRMED = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class TopicEvidenceBlock:
    """主题文档中的一条证据块。"""
    block_id: str
    content: str
    strength: EvidenceStrength = EvidenceStrength.MODERATE
    source: str = ""
    timestamp: float = field(default_factory=time.time)
    embed_vec: Optional[np.ndarray] = None
    citation_count: int = 0


@dataclass
class TopicDocument:
    """一个主题文档——语义单元, 组织所属全部证据。"""
    topic_id: str
    title: str
    description: str = ""
    evidence_blocks: List[TopicEvidenceBlock] = field(default_factory=list)
    sub_topics: List[str] = field(default_factory=list)
    parent_topic_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StageBuffer:
    """暂存缓冲区——新观察在 consolidate 前的存放位置。"""
    capacity: int = 128
    items: List[TopicEvidenceBlock] = field(default_factory=list)

    def is_full(self) -> bool:
        return len(self.items) >= self.capacity

    def push(self, block: TopicEvidenceBlock) -> None:
        if self.is_full():
            logger.warning("StageBuffer overflow, oldest item evicted")
            self.items.pop(0)
        self.items.append(block)

    def flush(self) -> List[TopicEvidenceBlock]:
        items = list(self.items)
        self.items.clear()
        return items

    def __len__(self) -> int:
        return len(self.items)


@dataclass
class IterativeRetrievalSession:
    """迭代式检索会话——记录 LLM 工具调用轨迹。"""
    session_id: str
    topic_id: str
    query: str
    rounds: List[Dict[str, Any]] = field(default_factory=list)
    active: bool = True
    started_at: float = field(default_factory=time.time)


@dataclass
class TopicConsolidationEvent:
    """融合事件记录。"""
    event_id: str
    trigger: ConsolidationTrigger
    blocks_processed: int
    blocks_merged: int
    topics_created: int
    topics_merged: int
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# TopicCoordinator
# ---------------------------------------------------------------------------

class TopicCoordinator:
    """主题协调器——创建/合并/拆分主题, 维护层级关系。

    Parameters
    ----------
    similarity_threshold : float
        两主题合并的相似度阈值。
    """

    def __init__(self, similarity_threshold: float = 0.75) -> None:
        self._topics: Dict[str, TopicDocument] = {}
        self._threshold = similarity_threshold
        self._lock = threading.RLock()
        self._next_id: int = 0

    def find_or_create(self, content: str, title_hint: str = "") -> TopicDocument:
        """查找匹配主题, 若无则创建新主题。"""
        with self._lock:
            for topic in self._topics.values():
                if self._title_match(topic.title, title_hint):
                    return topic
            return self.create_topic(title=title_hint or f"topic_{self._next_id}")

    def create_topic(self, title: str, description: str = "", parent_id: Optional[str] = None) -> TopicDocument:
        with self._lock:
            tid = f"topic_{self._next_id}"
            self._next_id += 1
            topic = TopicDocument(topic_id=tid, title=title, description=description, parent_topic_id=parent_id)
            self._topics[tid] = topic
            if parent_id and parent_id in self._topics:
                self._topics[parent_id].sub_topics.append(tid)
            logger.info("Topic %s created: %s", tid, title)
            return topic

    def merge_topics(self, tid_a: str, tid_b: str) -> Optional[TopicDocument]:
        """合并两主题 (B 并入 A)。"""
        with self._lock:
            a = self._topics.get(tid_a)
            b = self._topics.get(tid_b)
            if not a or not b:
                return None
            a.evidence_blocks.extend(b.evidence_blocks)
            a.sub_topics.extend(b.sub_topics)
            a.description += f" | merged: {b.title}"
            a.updated_at = time.time()
            del self._topics[tid_b]
            logger.info("Merged topic %s into %s", tid_b, tid_a)
            return a

    def split_topic(self, tid: str, split_keywords: List[str]) -> List[TopicDocument]:
        """按关键词拆分主题为多个子主题。"""
        with self._lock:
            source = self._topics.get(tid)
            if not source or not split_keywords:
                return []
            buckets: Dict[str, List[TopicEvidenceBlock]] = {kw: [] for kw in split_keywords}
            buckets["_other"] = []
            for block in source.evidence_blocks:
                matched = False
                for kw in split_keywords:
                    if kw.lower() in block.content.lower():
                        buckets[kw].append(block)
                        matched = True
                        break
                if not matched:
                    buckets["_other"].append(block)

            new_topics = []
            for kw, blocks in buckets.items():
                if not blocks:
                    continue
                child = self.create_topic(f"{source.title}/{kw}", parent_id=tid)
                child.evidence_blocks = blocks
                new_topics.append(child)
            source.evidence_blocks.clear()
            source.updated_at = time.time()
            return new_topics

    def get_topic(self, tid: str) -> Optional[TopicDocument]:
        return self._topics.get(tid)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_topics": len(self._topics),
                "root_topics": sum(1 for t in self._topics.values() if not t.parent_topic_id),
                "total_evidence": sum(len(t.evidence_blocks) for t in self._topics.values()),
            }

    @staticmethod
    def _title_match(a: str, b: str) -> bool:
        if not a or not b:
            return False
        return a.lower().strip() == b.lower().strip()


# ---------------------------------------------------------------------------
# TopicDocumentMemory
# ---------------------------------------------------------------------------

class TopicDocumentMemory:
    """Infini Memory 主题文档记忆系统。

    核心流水线: 新观察 → stage_buffer → consolidate() → 主题文档。
    检索支持 agentic_retrieve() 多轮迭代式深度阅读。

    Parameters
    ----------
    buffer_capacity : int
        stage_buffer 最大容量。
    embedding_dim : int
        嵌入向量维度。
    auto_consolidate : bool
        缓冲区满时是否自动触发 consolidate。
    """

    def __init__(
        self,
        buffer_capacity: int = 128,
        embedding_dim: int = 384,
        auto_consolidate: bool = True,
    ) -> None:
        self.embedding_dim = embedding_dim
        self.auto_consolidate = auto_consolidate
        self._buffer = StageBuffer(capacity=buffer_capacity)
        self._coordinator = TopicCoordinator()
        self._lock = threading.RLock()
        self._consolidation_log: List[TopicConsolidationEvent] = []
        self._observation_count: int = 0
        logger.info("TopicDocumentMemory initialized [buf=%d dim=%d]", buffer_capacity, embedding_dim)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_observation(
        self,
        content: str,
        topic_hint: str = "",
        source: str = "",
        strength: EvidenceStrength = EvidenceStrength.MODERATE,
    ) -> TopicEvidenceBlock:
        """添加一条新观察, 进入 stage_buffer。

        Parameters
        ----------
        content : str
            观察内容文本。
        topic_hint : str
            主题提示 (用于匹配或创建主题文档)。
        source : str
            来源标识。
        strength : EvidenceStrength
            证据强度。

        Returns
        -------
        TopicEvidenceBlock
            创建的证据块。
        """
        with self._lock:
            self._observation_count += 1
            block = TopicEvidenceBlock(
                block_id=f"block_{self._observation_count}",
                content=content,
                strength=strength,
                source=source,
                embed_vec=self._embed(content),
            )
            self._buffer.push(block)
            if self._buffer.is_full() and self.auto_consolidate:
                self.consolidate(trigger=ConsolidationTrigger.BUFFER_FULL)
            return block

    def consolidate(
        self,
        trigger: ConsolidationTrigger = ConsolidationTrigger.MANUAL,
    ) -> TopicConsolidationEvent:
        """将 stage_buffer 中的观察融合到主题文档。

        Parameters
        ----------
        trigger : ConsolidationTrigger
            融合触发原因。

        Returns
        -------
        TopicConsolidationEvent
            融合事件记录。
        """
        with self._lock:
            items = self._buffer.flush()
            if not items:
                return TopicConsolidationEvent(
                    event_id=f"evt_{int(time.time()*1e6)}", trigger=trigger,
                    blocks_processed=0, blocks_merged=0, topics_created=0, topics_merged=0,
                )

            topics_created = 0
            blocks_merged = 0
            # 按内容相似度分配到主题
            for item in items:
                # 提取关键词作为 topic_hint
                topic_hint = self._extract_topic_hint(item.content)
                topic = self._coordinator.find_or_create(item.content, topic_hint)
                is_new = topic.topic_id not in self._coordinator._topics or len(topic.evidence_blocks) == 0
                if is_new:
                    topics_created += 1
                topic.evidence_blocks.append(item)
                topic.updated_at = time.time()
                blocks_merged += 1

            event = TopicConsolidationEvent(
                event_id=f"evt_{int(time.time()*1e6)}",
                trigger=trigger,
                blocks_processed=len(items),
                blocks_merged=blocks_merged,
                topics_created=topics_created,
                topics_merged=0,
            )
            self._consolidation_log.append(event)
            logger.info("Consolidation: %d blocks → %d topics", len(items), topics_created)
            return event

    def update_fact(self, block_id: str, new_content: str) -> bool:
        """更新已有证据块内容 (创建新版本)。"""
        with self._lock:
            for topic in self._coordinator._topics.values():
                for block in topic.evidence_blocks:
                    if block.block_id == block_id:
                        block.content = new_content
                        block.embed_vec = self._embed(new_content)
                        block.timestamp = time.time()
                        topic.updated_at = time.time()
                        return True
            return False

    def delete_fact(self, block_id: str) -> bool:
        """删除证据块。"""
        with self._lock:
            for topic in self._coordinator._topics.values():
                for i, block in enumerate(topic.evidence_blocks):
                    if block.block_id == block_id:
                        topic.evidence_blocks.pop(i)
                        topic.updated_at = time.time()
                        return True
            return False

    def agentic_retrieve(
        self,
        query: str,
        topic_id: Optional[str] = None,
        max_rounds: int = 5,
    ) -> IterativeRetrievalSession:
        """迭代式检索—模拟 LLM 多轮工具调用深度阅读。

        Parameters
        ----------
        query : str
            检索查询。
        topic_id : Optional[str]
            限定主题。
        max_rounds : int
            最大迭代轮次。

        Returns
        -------
        IterativeRetrievalSession
            包含多轮结果的会话。
        """
        with self._lock:
            session = IterativeRetrievalSession(
                session_id=f"session_{int(time.time()*1e6)}",
                topic_id=topic_id or "all",
                query=query,
            )

            q_vec = self._embed(query)
            target_topics = (
                [self._coordinator.get_topic(topic_id)] if topic_id
                else list(self._coordinator._topics.values())
            )
            target_topics = [t for t in target_topics if t is not None]

            # Round 1: 摘要检索 (top-level overview)
            round1 = self._retrieve_round(query, q_vec, target_topics, detail_level=0)
            session.rounds.append(round1)

            # Round 2+: 逐层深入
            for r in range(2, max_rounds + 1):
                # 扩展检索: 子主题 + 引文
                expanded_ids: Set[str] = set()
                for hit in round1.get("hits", [])[:3]:
                    topic = self._coordinator.get_topic(hit.get("topic_id", ""))
                    if topic:
                        expanded_ids.update(topic.sub_topics)

                expanded_topics = [self._coordinator.get_topics(tid) for tid in expanded_ids]
                expanded_topics = [t for t in expanded_topics if t is not None]

                if not expanded_topics:
                    session.rounds.append({"round": r, "hits": [], "note": "no expansion targets"})
                    break

                rnd = self._retrieve_round(query, q_vec, expanded_topics, detail_level=r)
                session.rounds.append(rnd)

                if not rnd.get("hits"):
                    break

            session.active = False
            return session

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "observations_total": self._observation_count,
                "buffer_size": len(self._buffer),
                "buffer_capacity": self._buffer.capacity,
                "consolidations": len(self._consolidation_log),
                **self._coordinator.statistics(),
            }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _embed(self, text: str) -> np.ndarray:
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:16], 16)
        rng = np.random.RandomState(seed % (2 ** 31 - 1))
        vec = rng.randn(self.embedding_dim)
        return vec / (np.linalg.norm(vec) + 1e-8)

    def _retrieve_round(
        self,
        query: str,
        q_vec: np.ndarray,
        topics: List[TopicDocument],
        detail_level: int = 0,
    ) -> Dict[str, Any]:
        """单轮检索: 余弦相似度排序, 返回 top-5。"""
        scored: List[Tuple[TopicEvidenceBlock, float, str]] = []
        for topic in topics:
            for block in topic.evidence_blocks:
                if block.embed_vec is not None:
                    sim = float(np.dot(q_vec, block.embed_vec) /
                                (np.linalg.norm(q_vec) * np.linalg.norm(block.embed_vec) + 1e-8))
                    scored.append((block, sim, topic.topic_id))

        scored.sort(key=lambda x: x[1], reverse=True)
        top_n = min(5, len(scored))
        return {
            "round": detail_level,
            "detail_level": detail_level,
            "hits": [
                {
                    "block_id": b.block_id,
                    "topic_id": tid,
                    "content": b.content[:200],
                    "score": round(s, 4),
                }
                for b, s, tid in scored[:top_n]
            ],
            "total_candidates": len(scored),
        }

    @staticmethod
    def _extract_topic_hint(text: str) -> str:
        """从文本提取主题关键词 (简易版)。"""
        words = text.lower().split()
        stop = {"the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "and", "or", "it", "that", "this"}
        keywords = [w for w in words if w not in stop and len(w) > 2]
        return keywords[0] if keywords else "general"
