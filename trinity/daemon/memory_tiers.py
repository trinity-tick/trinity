#!/usr/bin/env python3
"""
Letta-Style Three-Tier Memory Lifecycle Manager
===============================================
Implements Core / Recall / Archival memory tiers with automatic
upgrade/downgrade lifecycle management.

Tiers (aligned with Letta/MemGPT architecture):
  Core Memory     — Context-window-resident blocks (< 500 tokens),
                    Agent directly reads/writes. Stores persona and
                    most important user facts.
  Recall Memory   — Searchable conversation history outside context,
                    Agent queries via tool calls.
  Archival Memory — Long-term cold storage, vector-indexed.
                    Agent queries via tool calls.

Upgrade / Downgrade Rules:
  Core overflow         → Recall (eviction)
  Recall high-frequency → Core (promotion)
  Recall low-frequency  → Archival (demotion)

Safety:
  - Per-block max token limit
  - Read-only persona block protection
  - Importance-weighted eviction on overflow

Reference:
  Letta (formerly MemGPT) — "LLM-as-OS" memory tier model (2026).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================


class MemoryTier(Enum):
    """三层记忆层级"""
    CORE = "core"           # 上下文窗口内
    RECALL = "recall"       # 可搜索对话历史
    ARCHIVAL = "archival"   # 长期冷存储向量索引


class BlockType(Enum):
    """记忆块类型"""
    PERSONA = "persona"          # Agent 角色定义（只读保护）
    HUMAN = "human"              # 用户事实
    TASK = "task"                # 当前任务上下文
    CONVERSATION = "conversation"  # 对话历史
    KNOWLEDGE = "knowledge"      # 知识记忆


# Default token estimates (rough: ~4 chars ≈ 1 token)
DEFAULT_CHARS_PER_TOKEN = 4

# Core Memory token budget
DEFAULT_CORE_TOKEN_LIMIT = 500

# Upgrade / downgrade thresholds
DEFAULT_RECALL_PROMOTION_THRESHOLD = 5       # access counts to promote to Core
DEFAULT_RECALL_DEMOTION_THRESHOLD = 0.02     # access frequency to demote to Archival
DEFAULT_RECALL_EVICTION_SCORE_THRESHOLD = 0.3  # importance below this = first eviction candidate


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class MemoryBlock:
    """单个记忆块 — 三层记忆的基本单元。

    Each block has:
      - tier: Current memory tier (CORE / RECALL / ARCHIVAL)
      - label: Human-readable label (e.g. "persona", "human_facts")
      - content: The actual text content
      - block_type: Semantic type (persona/knowledge etc.)
      - is_readonly: If True, Agent cannot modify this block (persona protection)
    """
    block_id: str
    label: str
    content: str
    tier: MemoryTier = MemoryTier.CORE
    block_type: BlockType = BlockType.KNOWLEDGE
    importance: float = 0.5          # [0, 1]
    is_readonly: bool = False
    access_count: int = 0
    last_accessed: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)
    source_memory_id: str = ""       # Reference to PostgreSQL memory_id if synced

    @property
    def estimated_tokens(self) -> int:
        """估算 token 数量（粗略：4 字符 ≈ 1 token）。"""
        return max(1, math.ceil(len(self.content) / DEFAULT_CHARS_PER_TOKEN))

    @property
    def age_seconds(self) -> float:
        """自创建以来的秒数。"""
        return time.time() - self.created_at

    @property
    def access_frequency(self) -> float:
        """访问频率 = access_count / age_hours，避免除零。"""
        hours = max(self.age_seconds / 3600.0, 0.01)
        return self.access_count / hours

    def to_dict(self) -> Dict[str, Any]:
        return {
            "block_id": self.block_id,
            "label": self.label,
            "tier": self.tier.value,
            "block_type": self.block_type.value,
            "importance": self.importance,
            "is_readonly": self.is_readonly,
            "access_count": self.access_count,
            "last_accessed": self.last_accessed,
            "created_at": self.created_at,
            "estimated_tokens": self.estimated_tokens,
            "access_frequency": round(self.access_frequency, 4),
            "content_preview": self.content[:100],
        }


@dataclass
class TierMigrationRecord:
    """记忆块层级迁移记录"""
    record_id: str
    block_id: str
    from_tier: MemoryTier
    to_tier: MemoryTier
    reason: str
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CoreMemoryStats:
    """Core Memory 统计信息"""
    total_blocks: int = 0
    total_tokens: int = 0
    token_limit: int = DEFAULT_CORE_TOKEN_LIMIT
    readonly_blocks: int = 0
    writable_blocks: int = 0
    utilization_pct: float = 0.0


# ============================================================================
# CoreMemory — Context-Window Memory Manager
# ============================================================================


class CoreMemory:
    """Core Memory 管理器

    管理驻留在上下文窗口中的小块记忆（< 500 tokens total）。
    直接读写，Agent 可见。实现 per-block token 限制和 persona 只读保护。

    Usage:
        core = CoreMemory(token_limit=500)
        core.set_block("persona", "I am a helpful assistant.", readonly=True)
        core.set_block("human_facts", "User is a Python developer.")
        stats = core.stats()
    """

    def __init__(self, token_limit: int = DEFAULT_CORE_TOKEN_LIMIT):
        self._blocks: Dict[str, MemoryBlock] = {}
        self.token_limit = token_limit

    # ── Block CRUD ────────────────────────────────────────────────

    def set_block(
        self,
        label: str,
        content: str,
        block_type: BlockType = BlockType.KNOWLEDGE,
        importance: float = 0.5,
        readonly: bool = False,
        tags: Optional[List[str]] = None,
    ) -> MemoryBlock:
        """创建或更新一个 Core 记忆块。

        Args:
            label: 块标签（如 "persona", "human_facts"）
            content: 块内容
            block_type: 语义类型
            importance: 重要性 [0, 1]
            readonly: 是否只读（persona 块应设为 True）
            tags: 标签列表

        Returns:
            创建或更新的 MemoryBlock

        Raises:
            ValueError: 如果块是只读的且尝试修改
        """
        now = time.time()

        # Check if block already exists
        existing = self._blocks.get(label)
        if existing and existing.is_readonly:
            raise ValueError(
                f"Block '{label}' is read-only (persona protection). "
                f"Cannot overwrite. Use force_set_block() to bypass."
            )

        if existing:
            # Update existing
            existing.content = content
            existing.importance = importance
            existing.updated_at = now
            if tags is not None:
                existing.tags = tags
            return existing

        # Create new
        block = MemoryBlock(
            block_id=f"core_{label}_{uuid.uuid4().hex[:8]}",
            label=label,
            content=content,
            tier=MemoryTier.CORE,
            block_type=block_type,
            importance=importance,
            is_readonly=readonly,
            created_at=now,
            updated_at=now,
            tags=tags or [],
        )
        self._blocks[label] = block

        # Check for overflow after insertion
        if self.total_tokens > self.token_limit:
            logger.warning(
                "Core Memory token overflow: %d/%d tokens after setting block '%s'",
                self.total_tokens, self.token_limit, label,
            )

        return block

    def force_set_block(
        self,
        label: str,
        content: str,
        block_type: BlockType = BlockType.KNOWLEDGE,
        importance: float = 0.5,
        readonly: bool = False,
        tags: Optional[List[str]] = None,
    ) -> MemoryBlock:
        """强制设置块，绕过只读保护（仅管理员调用）。"""
        now = time.time()
        block = MemoryBlock(
            block_id=f"core_{label}_{uuid.uuid4().hex[:8]}",
            label=label,
            content=content,
            tier=MemoryTier.CORE,
            block_type=block_type,
            importance=importance,
            is_readonly=readonly,
            created_at=now,
            updated_at=now,
            tags=tags or [],
        )
        self._blocks[label] = block
        return block

    def get_block(self, label: str) -> Optional[MemoryBlock]:
        """通过标签获取块。"""
        block = self._blocks.get(label)
        if block:
            block.access_count += 1
            block.last_accessed = time.time()
        return block

    def remove_block(self, label: str) -> Optional[MemoryBlock]:
        """移除块并返回（用于 eviction）。"""
        block = self._blocks.pop(label, None)
        if block and block.is_readonly:
            logger.warning("Removed read-only block '%s'", label)
        return block

    def list_blocks(self) -> List[MemoryBlock]:
        """列出所有 Core 块（按重要性降序）。"""
        return sorted(
            self._blocks.values(),
            key=lambda b: (b.is_readonly, b.importance),
            reverse=True,
        )

    def get_all_blocks(self) -> Dict[str, MemoryBlock]:
        """获取所有块的字典（label → MemoryBlock）。"""
        return dict(self._blocks)

    # ── Token Accounting ──────────────────────────────────────────

    @property
    def total_tokens(self) -> int:
        """所有 Core 块的 token 估计总和。"""
        return sum(b.estimated_tokens for b in self._blocks.values())

    @property
    def is_overflowing(self) -> bool:
        """Core Memory 是否溢出。"""
        return self.total_tokens > self.token_limit

    def stats(self) -> CoreMemoryStats:
        """返回 Core Memory 统计信息。"""
        readonly = sum(1 for b in self._blocks.values() if b.is_readonly)
        return CoreMemoryStats(
            total_blocks=len(self._blocks),
            total_tokens=self.total_tokens,
            token_limit=self.token_limit,
            readonly_blocks=readonly,
            writable_blocks=len(self._blocks) - readonly,
            utilization_pct=round(
                self.total_tokens / max(self.token_limit, 1) * 100, 1
            ),
        )

    # ── Overflow Eviction ─────────────────────────────────────────

    def get_eviction_candidates(
        self,
        target_reduction: Optional[int] = None,
    ) -> List[MemoryBlock]:
        """获取逐出候选列表（用于 Core → Recall 降级）。

        候选排序规则：
        1. 非只读块优先（只读块最后考虑）
        2. 非 persona/human 类型优先
        3. importance 升序（低重要性先逐出）
        4. 访问频率升序（冷门先逐出）

        Args:
            target_reduction: 目标减少的 token 数。None 表示需要多少给多少。

        Returns:
            按逐出优先级排序的候选块列表
        """
        candidates = [
            b for b in self._blocks.values()
            if not b.is_readonly
        ]

        if not candidates:
            # All blocks are readonly — include them but log warning
            logger.warning(
                "All Core blocks are read-only, eviction will include protected blocks!"
            )
            candidates = list(self._blocks.values())

        candidates.sort(
            key=lambda b: (
                b.is_readonly,          # readonly last
                0 if b.block_type in (BlockType.PERSONA, BlockType.HUMAN) else 1,
                b.importance,           # low importance first
                b.access_frequency,     # cold first
            )
        )

        if target_reduction is not None:
            collected = 0
            result = []
            for c in candidates:
                if collected >= target_reduction:
                    break
                result.append(c)
                collected += c.estimated_tokens
            return result

        return candidates

    # ── Context Window Assembly ──────────────────────────────────

    def assemble_context(self) -> str:
        """将 Core Memory 组装为上下文窗口文本。"""
        parts = []
        for block in self.list_blocks():
            parts.append(f"[{block.label}]\n{block.content}\n")
        return "\n".join(parts)


# ============================================================================
# RecallMemory — Searchable Conversation History
# ============================================================================


class RecallMemory:
    """Recall Memory 管理器

    可搜索的对话历史，位于上下文窗口外。Agent 通过工具调用查询。
    跟踪访问频率，用于升级/降级决策。

    Usage:
        recall = RecallMemory()
        recall.add_entry("User asked about Python async.", importance=0.6)
        results = recall.search("Python async")
        recall.record_access(results[0].block_id)
    """

    def __init__(self, pg_adapter: Any = None):
        self._blocks: Dict[str, MemoryBlock] = {}
        self.pg_adapter = pg_adapter

    # ── Block Management ──────────────────────────────────────────

    def add_block(self, block: MemoryBlock) -> MemoryBlock:
        """添加一个 Recall 块。"""
        block.tier = MemoryTier.RECALL
        if not block.block_id:
            block.block_id = f"recall_{uuid.uuid4().hex[:12]}"
        self._blocks[block.block_id] = block
        return block

    def add_entry(
        self,
        content: str,
        label: str = "",
        importance: float = 0.3,
        tags: Optional[List[str]] = None,
        source_memory_id: str = "",
    ) -> MemoryBlock:
        """添加一条 Recall 记录（便捷方法）。

        Args:
            content: 记录内容
            label: 标签
            importance: 重要性
            tags: 标签列表
            source_memory_id: PostgreSQL memory_id 引用

        Returns:
            创建的 MemoryBlock
        """
        block = MemoryBlock(
            block_id=f"recall_{uuid.uuid4().hex[:12]}",
            label=label or f"recall_{len(self._blocks) + 1}",
            content=content,
            tier=MemoryTier.RECALL,
            block_type=BlockType.CONVERSATION,
            importance=importance,
            is_readonly=False,
            created_at=time.time(),
            updated_at=time.time(),
            tags=tags or [],
            source_memory_id=source_memory_id,
        )
        self._blocks[block.block_id] = block
        return block

    def get_block(self, block_id: str) -> Optional[MemoryBlock]:
        """获取块（同时记录访问）。"""
        block = self._blocks.get(block_id)
        if block:
            block.access_count += 1
            block.last_accessed = time.time()
        return block

    def remove_block(self, block_id: str) -> Optional[MemoryBlock]:
        """移除块。"""
        return self._blocks.pop(block_id, None)

    def list_blocks(self) -> List[MemoryBlock]:
        """列出所有 Recall 块（按访问频率降序）。"""
        return sorted(
            self._blocks.values(),
            key=lambda b: b.access_frequency,
            reverse=True,
        )

    def record_access(self, block_id: str) -> bool:
        """记录对某个块的访问（不获取内容）。"""
        block = self._blocks.get(block_id)
        if block:
            block.access_count += 1
            block.last_accessed = time.time()
            return True
        return False

    # ── Search ────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 10,
        min_importance: float = 0.0,
    ) -> List[Tuple[MemoryBlock, float]]:
        """关键词搜索 Recall 记忆。

        简单 BM25-like 评分：匹配词数加权 × importance。

        Args:
            query: 搜索查询
            top_k: 返回结果数
            min_importance: 最低重要性阈值

        Returns:
            (MemoryBlock, score) 列表
        """
        query_terms = query.lower().split()
        scored: List[Tuple[MemoryBlock, float]] = []

        for block in self._blocks.values():
            if block.importance < min_importance:
                continue

            content_lower = block.content.lower()
            label_lower = block.label.lower()

            # Simple term-match scoring
            matches = 0
            for term in query_terms:
                if term in content_lower or term in label_lower:
                    matches += 1

            if matches > 0:
                # Score: match ratio × importance × recency bonus
                match_ratio = matches / len(query_terms)
                recency = max(0.0, 1.0 - block.age_seconds / (86400 * 30))
                score = match_ratio * (0.5 + 0.5 * block.importance) * (1.0 + 0.2 * recency)
                scored.append((block, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def search_by_tags(
        self, tags: List[str], match_all: bool = False,
    ) -> List[MemoryBlock]:
        """按标签搜索。

        Args:
            tags: 要匹配的标签列表
            match_all: True 则要求全部匹配，False 则任一匹配

        Returns:
            匹配的 MemoryBlock 列表
        """
        if match_all:
            return [
                b for b in self._blocks.values()
                if all(t in b.tags for t in tags)
            ]
        else:
            return [
                b for b in self._blocks.values()
                if any(t in b.tags for t in tags)
            ]

    @property
    def size(self) -> int:
        return len(self._blocks)

    def stats(self) -> Dict[str, Any]:
        """Recall 统计信息。"""
        if not self._blocks:
            return {"total_blocks": 0, "avg_importance": 0, "total_accesses": 0}

        accesses = sum(b.access_count for b in self._blocks.values())
        avg_imp = sum(b.importance for b in self._blocks.values()) / len(self._blocks)
        return {
            "total_blocks": len(self._blocks),
            "avg_importance": round(avg_imp, 4),
            "total_accesses": accesses,
            "avg_access_freq": round(
                sum(b.access_frequency for b in self._blocks.values()) / len(self._blocks), 4
            ),
        }


# ============================================================================
# ArchivalMemory — Long-Term Cold Storage
# ============================================================================


class ArchivalMemory:
    """Archival Memory 管理器

    长期冷存储，Agent 通过工具调用查询。存储低频/旧记忆。

    Usage:
        archival = ArchivalMemory()
        archival.archive_block(block)
        results = archival.search("project Alpha setup")
    """

    def __init__(self, pg_adapter: Any = None):
        self._blocks: Dict[str, MemoryBlock] = {}
        self.pg_adapter = pg_adapter

    def archive_block(self, block: MemoryBlock) -> MemoryBlock:
        """将块归档到 Archival 层。"""
        block.tier = MemoryTier.ARCHIVAL
        if not block.block_id:
            block.block_id = f"arch_{uuid.uuid4().hex[:12]}"
        block.updated_at = time.time()
        self._blocks[block.block_id] = block
        return block

    def get_block(self, block_id: str) -> Optional[MemoryBlock]:
        """获取块。"""
        block = self._blocks.get(block_id)
        if block:
            block.access_count += 1
            block.last_accessed = time.time()
        return block

    def remove_block(self, block_id: str) -> Optional[MemoryBlock]:
        return self._blocks.pop(block_id, None)

    def search(
        self,
        query: str,
        top_k: int = 10,
    ) -> List[Tuple[MemoryBlock, float]]:
        """搜索 Archival 记忆（简单关键词匹配）。

        Archival 搜索性能可能低于 Recall，因为数据量大。
        生产环境应使用向量索引（FAISS/pgvector）。

        Args:
            query: 搜索查询
            top_k: 返回结果数

        Returns:
            (MemoryBlock, score) 列表
        """
        query_terms = query.lower().split()
        scored: List[Tuple[MemoryBlock, float]] = []

        for block in self._blocks.values():
            content_lower = block.content.lower()
            matches = sum(1 for t in query_terms if t in content_lower)

            if matches > 0:
                match_ratio = matches / len(query_terms)
                # Archival: heavier weight on recency
                recency = max(0.0, math.exp(-block.age_seconds / (86400 * 90)))
                score = match_ratio * block.importance * recency
                scored.append((block, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def search_by_tags(
        self, tags: List[str], match_all: bool = False,
    ) -> List[MemoryBlock]:
        """按标签搜索。"""
        if match_all:
            return [
                b for b in self._blocks.values()
                if all(t in b.tags for t in tags)
            ]
        else:
            return [
                b for b in self._blocks.values()
                if any(t in b.tags for t in tags)
            ]

    @property
    def size(self) -> int:
        return len(self._blocks)

    def stats(self) -> Dict[str, Any]:
        """Archival 统计信息。"""
        if not self._blocks:
            return {"total_blocks": 0, "avg_age_days": 0}

        avg_age = sum(b.age_seconds for b in self._blocks.values()) / len(self._blocks)
        return {
            "total_blocks": len(self._blocks),
            "avg_age_days": round(avg_age / 86400, 1),
            "avg_importance": round(
                sum(b.importance for b in self._blocks.values()) / len(self._blocks), 4
            ),
        }


# ============================================================================
# MemoryTierManager — Orchestrator
# ============================================================================


class MemoryTierManager:
    """三层记忆生命周期管理编排器

    核心职责：
    1. 管理 Core / Recall / Archival 三层
    2. 执行升级/降级规则：
       - Core 溢出 → Recall（eviction）
       - Recall 高频访问 → Core（promotion）
       - Recall 低频访问 → Archival（demotion）
    3. 生成迁移审计记录
    4. PostgreSQL 同步（可选）

    Weighted Scoring for auto-tiering:
      score = w_recency × recency_score + w_importance × importance
            + w_access × access_frequency_score

      where:
        recency_score = exp(-age_days / 30)
        access_frequency_score = min(1.0, access_freq / peak_freq)

    Usage:
        manager = MemoryTierManager(pg_adapter=adapter)
        manager.core.set_block("persona", "I am helpful.", readonly=True)
        manager.evict_from_core()
        manager.promote_to_core()
        manager.demote_to_archival()
    """

    def __init__(
        self,
        pg_adapter: Any = None,
        core_token_limit: int = DEFAULT_CORE_TOKEN_LIMIT,
        promotion_threshold: int = DEFAULT_RECALL_PROMOTION_THRESHOLD,
        demotion_threshold: float = DEFAULT_RECALL_DEMOTION_THRESHOLD,
        # Scoring weights
        w_recency: float = 0.4,
        w_importance: float = 0.35,
        w_access: float = 0.25,
    ):
        self.core = CoreMemory(token_limit=core_token_limit)
        self.recall = RecallMemory(pg_adapter=pg_adapter)
        self.archival = ArchivalMemory(pg_adapter=pg_adapter)

        self.pg_adapter = pg_adapter
        self.promotion_threshold = promotion_threshold
        self.demotion_threshold = demotion_threshold
        self.w_recency = w_recency
        self.w_importance = w_importance
        self.w_access = w_access

        # Migration audit trail
        self._migrations: List[TierMigrationRecord] = []

    # ── Weighted Scoring ──────────────────────────────────────────

    def compute_tier_score(self, block: MemoryBlock) -> float:
        """计算加权层级分数。

        score = w_recency × recency_score + w_importance × importance
              + w_access × access_frequency_score

        分数越高越适合留在/提升到 Core。

        Args:
            block: 记忆块

        Returns:
            加权分数 [0, 1]
        """
        # Recency: exponential decay over 30 days
        days = block.age_seconds / 86400.0
        recency_score = math.exp(-days / 30.0)

        # Access frequency: normalize against a 10-access/hour peak
        peak_freq = 10.0
        af_score = min(1.0, block.access_frequency / peak_freq)

        score = (
            self.w_recency * recency_score
            + self.w_importance * block.importance
            + self.w_access * af_score
        )
        return max(0.0, min(1.0, score))

    # ── Eviction: Core → Recall ──────────────────────────────────

    def evict_from_core(
        self,
        target_tokens: Optional[int] = None,
    ) -> List[TierMigrationRecord]:
        """Core 溢出时逐出到 Recall。

        Args:
            target_tokens: 目标减少的 token 数量。None 时减少到 token_limit 以下。

        Returns:
            迁移记录列表
        """
        if not self.core.is_overflowing:
            return []

        overflow = self.core.total_tokens - self.core.token_limit
        target = target_tokens or max(overflow, 1)

        candidates = self.core.get_eviction_candidates(target_reduction=target)
        records: List[TierMigrationRecord] = []

        for block in candidates:
            # Remove from Core
            removed = self.core.remove_block(block.label)
            if removed is None:
                continue

            # Move to Recall
            self.recall.add_block(removed)

            record = TierMigrationRecord(
                record_id=f"migrate_{uuid.uuid4().hex[:8]}",
                block_id=removed.block_id,
                from_tier=MemoryTier.CORE,
                to_tier=MemoryTier.RECALL,
                reason=f"Core overflow ({self.core.total_tokens}/{self.core.token_limit} tokens)",
                metadata={"token_count": removed.estimated_tokens},
            )
            self._migrations.append(record)
            records.append(record)

            # Stop if we've reduced enough
            if self.core.total_tokens <= self.core.token_limit:
                break

        if records:
            logger.info(
                "Evicted %d blocks from Core → Recall (freed %d tokens, now %d/%d)",
                len(records),
                sum(r.metadata.get("token_count", 0) for r in records),
                self.core.total_tokens,
                self.core.token_limit,
            )

        return records

    # ── Promotion: Recall → Core ─────────────────────────────────

    def promote_to_core(self) -> List[TierMigrationRecord]:
        """将 Recall 中高频访问的记忆提升到 Core。

        条件：
        - access_count >= promotion_threshold
        - Core token limit 允许
        - 非 persona 类型（persona 只读块不自动提升）

        Returns:
            迁移记录列表
        """
        candidates = [
            b for b in self.recall.list_blocks()
            if b.access_count >= self.promotion_threshold
            and b.block_type != BlockType.PERSONA
        ]

        records: List[TierMigrationRecord] = []

        for block in candidates:
            block_tokens = block.estimated_tokens

            # Check if Core has room
            if self.core.total_tokens + block_tokens > self.core.token_limit:
                # Try eviction first
                evicted = self.evict_from_core(target_tokens=block_tokens)
                if self.core.total_tokens + block_tokens > self.core.token_limit:
                    logger.debug(
                        "Cannot promote block '%s': Core still full after eviction",
                        block.label,
                    )
                    continue

            # Remove from Recall
            removed = self.recall.remove_block(block.block_id)
            if removed is None:
                continue

            # Add to Core
            try:
                self.core.set_block(
                    label=removed.label,
                    content=removed.content,
                    block_type=removed.block_type,
                    importance=removed.importance,
                    readonly=False,
                )
            except ValueError as e:
                logger.warning("Promotion failed for '%s': %s", removed.label, e)
                # Put back in Recall
                self.recall.add_block(removed)
                continue

            record = TierMigrationRecord(
                record_id=f"migrate_{uuid.uuid4().hex[:8]}",
                block_id=removed.block_id,
                from_tier=MemoryTier.RECALL,
                to_tier=MemoryTier.CORE,
                reason=f"High access frequency (count={removed.access_count}, freq={removed.access_frequency:.2f}/h)",
                metadata={"access_count": removed.access_count},
            )
            self._migrations.append(record)
            records.append(record)

        if records:
            logger.info("Promoted %d blocks from Recall → Core", len(records))

        return records

    # ── Demotion: Recall → Archival ──────────────────────────────

    def demote_to_archival(self) -> List[TierMigrationRecord]:
        """将 Recall 中低频/低分记忆降级到 Archival。

        条件：
        - 加权分数低于 demotion_threshold
        - 年龄 > 7 天（保护近期记忆）

        Returns:
            迁移记录列表
        """
        candidates = [
            b for b in self.recall.list_blocks()
            if self.compute_tier_score(b) < self.demotion_threshold
            and b.age_seconds > 86400 * 7    # at least 7 days old
        ]

        records: List[TierMigrationRecord] = []

        for block in candidates:
            removed = self.recall.remove_block(block.block_id)
            if removed is None:
                continue

            self.archival.archive_block(removed)

            record = TierMigrationRecord(
                record_id=f"migrate_{uuid.uuid4().hex[:8]}",
                block_id=removed.block_id,
                from_tier=MemoryTier.RECALL,
                to_tier=MemoryTier.ARCHIVAL,
                reason=f"Low tier score ({self.compute_tier_score(removed):.4f} < {self.demotion_threshold})",
                metadata={
                    "tier_score": round(self.compute_tier_score(removed), 4),
                    "age_days": round(removed.age_seconds / 86400, 1),
                    "access_count": removed.access_count,
                },
            )
            self._migrations.append(record)
            records.append(record)

        if records:
            logger.info("Demoted %d blocks from Recall → Archival", len(records))

        return records

    # ── Full Lifecycle Run ────────────────────────────────────────

    def run_lifecycle(self) -> List[TierMigrationRecord]:
        """执行完整生命周期：Core eviction → Recall promotion → Recall demotion。

        按顺序执行以确保：
        1. 先腾出 Core 空间
        2. 再评估升级
        3. 最后清理低频

        Returns:
            所有迁移记录列表
        """
        all_records: List[TierMigrationRecord] = []

        # Step 1: Core overflow eviction
        all_records.extend(self.evict_from_core())

        # Step 2: Recall → Core promotion
        all_records.extend(self.promote_to_core())

        # Step 3: Recall → Archival demotion
        all_records.extend(self.demote_to_archival())

        return all_records

    # ── Statistics ────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """三层状态快照。"""
        return {
            "core": {
                "blocks": self.core.stats().__dict__,
                "blocks_detail": [b.to_dict() for b in self.core.list_blocks()],
            },
            "recall": self.recall.stats(),
            "archival": self.archival.stats(),
            "migrations": len(self._migrations),
            "last_migration": (
                self._migrations[-1].timestamp if self._migrations else None
            ),
        }

    def get_migrations(
        self, limit: int = 50,
    ) -> List[TierMigrationRecord]:
        """获取最近的迁移记录。"""
        return self._migrations[-limit:]

    # ── PostgreSQL Sync ──────────────────────────────────────────

    def sync_to_postgresql(self) -> Dict[str, int]:
        """将当前 Core + Recall 块同步到 PostgreSQL。

        使用已有的 PostgreSQLAdapter 接口。

        Returns:
            同步统计 {"synced": N, "failed": M}
        """
        if not self.pg_adapter:
            return {"synced": 0, "failed": 0, "reason": "no pg_adapter"}

        stats = {"synced": 0, "failed": 0}

        # Sync Core blocks
        for block in self.core.list_blocks():
            try:
                tags = list(block.tags) if block.tags else []
                tags.append(f"tier:core")
                tags.append(f"type:{block.block_type.value}")
                if block.is_readonly:
                    tags.append("readonly")

                self.pg_adapter.store_memory(
                    content=block.content,
                    persona_id="system",
                    importance=block.importance,
                    tags=tags,
                    category=f"core_{block.block_type.value}",
                    role="system",
                )
                stats["synced"] += 1
            except Exception as e:
                logger.error("Failed to sync Core block '%s': %s", block.label, e)
                stats["failed"] += 1

        # Sync Recall blocks
        for block in self.recall.list_blocks():
            try:
                tags = list(block.tags) if block.tags else []
                tags.append(f"tier:recall")
                tags.append(f"type:{block.block_type.value}")

                self.pg_adapter.store_memory(
                    content=block.content,
                    persona_id="system",
                    importance=block.importance,
                    tags=tags,
                    category=f"recall_{block.block_type.value}",
                    role="system",
                )
                stats["synced"] += 1
            except Exception as e:
                logger.error("Failed to sync Recall block '%s': %s", block.label, e)
                stats["failed"] += 1

        return stats

    def load_from_postgresql(self) -> Dict[str, int]:
        """从 PostgreSQL 加载活跃记忆到三层结构。

        Returns:
            加载统计 {"loaded": N, "core": C, "recall": R, "archival": A}
        """
        if not self.pg_adapter:
            return {"loaded": 0, "reason": "no pg_adapter"}

        stats = {"loaded": 0, "core": 0, "recall": 0, "archival": 0}

        try:
            memories = self.pg_adapter.get_all_memories(limit=200)
        except Exception as e:
            logger.error("Failed to load memories from PostgreSQL: %s", e)
            return {"loaded": 0, "failed": 1, "reason": str(e)}

        for mem in memories:
            content = str(mem.get("content", ""))
            tags = mem.get("tags") or []
            category = str(mem.get("category", ""))
            importance = float(mem.get("importance", 0.5))
            mem_id = str(mem.get("memory_id", ""))

            # Determine tier from tags
            tier_tag = None
            for t in tags:
                if t.startswith("tier:"):
                    tier_tag = t.split(":", 1)[1]
                    break

            if tier_tag == "core":
                label = f"pg_core_{mem_id[:8]}"
                try:
                    self.core.set_block(
                        label=label,
                        content=content,
                        block_type=BlockType.KNOWLEDGE,
                        importance=importance,
                        tags=tags,
                    )
                    stats["core"] += 1
                except ValueError:
                    pass
            elif tier_tag == "recall":
                self.recall.add_entry(
                    content=content,
                    label=f"pg_recall_{mem_id[:8]}",
                    importance=importance,
                    tags=tags,
                    source_memory_id=mem_id,
                )
                stats["recall"] += 1
            else:
                block = MemoryBlock(
                    block_id=f"pg_arch_{mem_id[:8]}",
                    label=f"pg_archival_{mem_id[:8]}",
                    content=content,
                    tier=MemoryTier.ARCHIVAL,
                    block_type=BlockType.KNOWLEDGE,
                    importance=importance,
                    tags=tags,
                    source_memory_id=mem_id,
                    created_at=time.time(),  # will be overwritten by age
                )
                self.archival.archive_block(block)
                stats["archival"] += 1

            stats["loaded"] += 1

        return stats


# ============================================================================
# Convenience API
# ============================================================================


def create_memory_tier_manager(
    pg_host: str = "localhost",
    pg_port: int = 5432,
    pg_dbname: str = "trinity",
    pg_user: str = "postgres",
    pg_password: str = "postgres",
    core_token_limit: int = DEFAULT_CORE_TOKEN_LIMIT,
) -> MemoryTierManager:
    """创建带 PostgreSQL 连接的三层记忆管理器（便捷工厂）。

    Args:
        pg_host: PostgreSQL 主机
        pg_port: PostgreSQL 端口
        pg_dbname: PostgreSQL 数据库名
        pg_user: PostgreSQL 用户
        pg_password: PostgreSQL 密码
        core_token_limit: Core Memory token 限制

    Returns:
        已初始化的 MemoryTierManager
    """
    try:
        from trinity.adapters.postgresql import PostgreSQLAdapter
        adapter = PostgreSQLAdapter(
            host=pg_host, port=pg_port, dbname=pg_dbname,
            user=pg_user, password=pg_password,
            min_conn=1, max_conn=3,
        )
        adapter.connect()
        logger.info("Connected to PostgreSQL for MemoryTierManager")
    except Exception as e:
        logger.warning("PostgreSQL not available, running in offline mode: %s", e)
        adapter = None

    return MemoryTierManager(
        pg_adapter=adapter,
        core_token_limit=core_token_limit,
    )
