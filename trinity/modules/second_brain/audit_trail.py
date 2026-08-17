"""
P6-3: Immutable Audit Trail System (对标 Immutable Audit Trail)
=================================================================

四表设计实现不可变审计追踪：
  - MemoryDocument:  当前状态指针
  - MemoryVersion:   每次变更的不可变快照
  - MemorySuggestion: 待审查的变更建议
  - MemoryEvent:     完整审计事件（含 source_id 追溯到源对话/文件）

核心原则：
  - 每次记忆变更自动记录 provenance 链
  - 不删除、不覆盖——所有变更都是追加
  - 完整可追溯：可从当前状态追溯到任意历史版本和来源

Reference: Erturk, "Why Your AI Agent Needs an Immutable Audit Trail
           for Memory", 2026. https://ertyurk.com/posts/...
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto

import numpy as np
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────────

class EventCategory(Enum):
    """审计事件类别。"""
    MEMORY_CREATED = "memory_created"           # 记忆创建
    MEMORY_UPDATED = "memory_updated"           # 记忆更新
    MEMORY_DELETED = "memory_deleted"           # 记忆删除（软删除）
    SUGGESTION_CREATED = "suggestion_created"   # 变更建议创建
    SUGGESTION_ACCEPTED = "suggestion_accepted" # 建议已接受
    SUGGESTION_REJECTED = "suggestion_rejected" # 建议已拒绝
    SUGGESTION_MODIFIED = "suggestion_modified" # 建议已修改
    REVIEW_STARTED = "review_started"           # 审查开始
    REVIEW_COMPLETED = "review_completed"       # 审查完成
    CONSOLIDATION_TRIGGERED = "consolidation_triggered"  # 巩固触发
    ROLLBACK = "rollback"                       # 回滚


class SuggestionStatus(Enum):
    """变更建议状态。"""
    PENDING = "pending"         # 待审查
    UNDER_REVIEW = "under_review"  # 审查中
    ACCEPTED = "accepted"        # 已接受
    REJECTED = "rejected"        # 已拒绝
    MODIFIED = "modified"        # 已修改（接受修改后版本）


class RiskLevel(Enum):
    """变更风险等级。"""
    LOW = "low"           # 低风险：拼写修正、格式调整
    MEDIUM = "medium"     # 中风险：事实更新、实体合并
    HIGH = "high"         # 高风险：核心知识变更、关系重连
    CRITICAL = "critical" # 致命风险：身份信息、安全策略


class SourceType(Enum):
    """来源类型。"""
    CONVERSATION = "conversation"    # 对话提取
    DOCUMENT = "document"            # 文档解析
    MANUAL = "manual"                # 人工录入
    INFERENCE = "inference"          # 推理生成
    IMPORT = "import"                # 外部导入
    CONSOLIDATION = "consolidation"  # 自动巩固


# ── 四表数据结构 ─────────────────────────────────────────────────────

@dataclass
class MemoryDocument:
    """表1: 当前状态指针。

    每条记忆一个 document，指向最新版本。

    Args:
        doc_id: 文档唯一标识
        memory_key: 记忆键名
        current_version_id: 当前活跃版本ID
        created_at: 创建时间
        updated_at: 最后更新时间
        metadata: 额外元数据
    """
    doc_id: str = field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:12]}")
    memory_key: str = ""
    current_version_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "memory_key": self.memory_key,
            "current_version_id": self.current_version_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


@dataclass
class MemoryVersion:
    """表2: 每次变更的不可变快照。

    每次记忆变更创建新版本，旧版本永不删除。

    Args:
        version_id: 版本唯一标识
        doc_id: 所属文档ID
        parent_version_id: 父版本ID（None=首次创建）
        content: 该版本的完整内容
        content_hash: 内容的 SHA-256 哈希
        version_number: 版本序号（从1开始）
        event_id: 关联的审计事件ID
        created_at: 版本创建时间
        metadata: 额外元数据
    """
    version_id: str = field(default_factory=lambda: f"ver_{uuid.uuid4().hex[:12]}")
    doc_id: str = ""
    parent_version_id: Optional[str] = None
    content: str = ""
    content_hash: str = ""
    version_number: int = 1
    event_id: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_id": self.version_id,
            "doc_id": self.doc_id,
            "parent_version_id": self.parent_version_id,
            "content": self.content,
            "content_hash": self.content_hash,
            "version_number": self.version_number,
            "event_id": self.event_id,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }


@dataclass
class MemorySuggestion:
    """表3: 待审查的变更建议。

    AI 提出变更建议后，先存入 suggestion 表等待审查。

    Args:
        suggestion_id: 建议唯一标识
        doc_id: 目标文档ID（None=新建文档）
        proposed_content: 建议的新内容
        previous_version_id: 基于哪个版本提出
        status: 建议状态
        risk_level: 风险等级
        reason: 变更理由
        source_type: 来源类型
        source_id: 来源标识（如 conversation:abc123）
        confidence: AI 置信度 [0,1]
        created_at: 创建时间
        reviewed_at: 审查时间
        reviewer_note: 审查备注
    """
    suggestion_id: str = field(default_factory=lambda: f"sug_{uuid.uuid4().hex[:12]}")
    doc_id: Optional[str] = None
    proposed_content: str = ""
    previous_version_id: Optional[str] = None
    status: SuggestionStatus = SuggestionStatus.PENDING
    risk_level: RiskLevel = RiskLevel.LOW
    reason: str = ""
    source_type: SourceType = SourceType.CONVERSATION
    source_id: str = ""
    confidence: float = 0.5
    created_at: float = field(default_factory=time.time)
    reviewed_at: Optional[float] = None
    reviewer_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "doc_id": self.doc_id,
            "proposed_content": self.proposed_content,
            "previous_version_id": self.previous_version_id,
            "status": self.status.value,
            "risk_level": self.risk_level.value,
            "reason": self.reason,
            "source_type": self.source_type.value,
            "source_id": self.source_id,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "reviewed_at": self.reviewed_at,
            "reviewer_note": self.reviewer_note,
        }


@dataclass
class MemoryEvent:
    """表4: 完整审计事件。

    不可变事件记录，包含 source_id 追溯到源对话/文件。

    Args:
        event_id: 事件唯一标识
        category: 事件类别
        doc_id: 关联文档ID
        version_id: 关联版本ID
        suggestion_id: 关联建议ID（可选）
        source_type: 来源类型
        source_id: 来源标识（conversation:xxx / document:xxx）
        actor: 执行者（ai / human / system）
        details: 事件详情
        timestamp: 事件时间戳
        metadata: 额外元数据
    """
    event_id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}")
    category: EventCategory = EventCategory.MEMORY_CREATED
    doc_id: str = ""
    version_id: str = ""
    suggestion_id: Optional[str] = None
    source_type: SourceType = SourceType.CONVERSATION
    source_id: str = ""
    actor: str = "ai"
    details: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "category": self.category.value,
            "doc_id": self.doc_id,
            "version_id": self.version_id,
            "suggestion_id": self.suggestion_id,
            "source_type": self.source_type.value,
            "source_id": self.source_id,
            "actor": self.actor,
            "details": self.details,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


@dataclass
class ProvenanceChain:
    """溯源链：从当前版本追溯到最初的完整路径。

    Args:
        doc_id: 文档ID
        chain: 按时间排序的版本列表（最旧→最新）
        events: 关联的审计事件列表
        suggestions: 关联的变更建议列表
    """
    doc_id: str = ""
    chain: List[MemoryVersion] = field(default_factory=list)
    events: List[MemoryEvent] = field(default_factory=list)
    suggestions: List[MemorySuggestion] = field(default_factory=list)


# ── 审计追踪系统 ─────────────────────────────────────────────────────

class AuditTrail:
    """不可变审计追踪系统。

    四表设计：
    - documents:  {doc_id: MemoryDocument}
    - versions:   {version_id: MemoryVersion}
    - suggestions: {suggestion_id: MemorySuggestion}
    - events:     按 doc_id 索引的事件列表

    线程安全、事件不可变、完整溯源。
    """

    def __init__(self, max_events_per_doc: int = 10000):
        self.documents: Dict[str, MemoryDocument] = {}
        self.versions: Dict[str, MemoryVersion] = {}
        self.suggestions: Dict[str, MemorySuggestion] = {}
        self._events_by_doc: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=max_events_per_doc)
        )
        self._events_all: deque = deque(maxlen=max_events_per_doc * 10)
        self._lock = threading.RLock()

        self._stats: Dict[str, int] = {
            "total_documents": 0,
            "total_versions": 0,
            "total_suggestions": 0,
            "total_events": 0,
            "total_commits": 0,
            "total_rollbacks": 0,
        }

    # ── 记忆创建 ─────────────────────────────────────────────────

    def create_memory(
        self,
        memory_key: str,
        content: str,
        source_type: SourceType = SourceType.CONVERSATION,
        source_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[MemoryDocument, MemoryVersion, MemoryEvent]:
        """创建新记忆文档。

        Args:
            memory_key: 记忆键名
            content: 初始内容
            source_type: 来源类型
            source_id: 来源标识
            metadata: 额外元数据

        Returns:
            (MemoryDocument, MemoryVersion, MemoryEvent)
        """
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        with self._lock:
            # 创建版本
            version = MemoryVersion(
                doc_id="",  # 先占位
                content=content,
                content_hash=content_hash,
                version_number=1,
            )

            # 创建事件
            event = MemoryEvent(
                category=EventCategory.MEMORY_CREATED,
                doc_id="",  # 先占位
                version_id=version.version_id,
                source_type=source_type,
                source_id=source_id,
                actor="ai",
                details=f"Created memory '{memory_key}'",
                metadata=metadata or {},
            )

            # 创建文档
            doc = MemoryDocument(
                memory_key=memory_key,
                current_version_id=version.version_id,
                metadata=metadata or {},
            )

            # 回填关联
            version.doc_id = doc.doc_id
            version.event_id = event.event_id
            event.doc_id = doc.doc_id

            # 持久化
            self.documents[doc.doc_id] = doc
            self.versions[version.version_id] = version
            self._events_by_doc[doc.doc_id].append(event)
            self._events_all.append(event)

            self._stats["total_documents"] += 1
            self._stats["total_versions"] += 1
            self._stats["total_events"] += 1

        logger.info(
            "AuditTrail: created memory %s → doc=%s ver=%s",
            memory_key, doc.doc_id, version.version_id,
        )
        return doc, version, event

    # ── 记忆更新 ─────────────────────────────────────────────────

    def update_memory(
        self,
        doc_id: str,
        new_content: str,
        source_type: SourceType = SourceType.CONVERSATION,
        source_id: str = "",
        suggestion_id: Optional[str] = None,
        actor: str = "ai",
    ) -> Optional[Tuple[MemoryVersion, MemoryEvent]]:
        """更新记忆——创建新版本，不覆盖旧版本。

        Args:
            doc_id: 文档ID
            new_content: 新内容
            source_type: 来源类型
            source_id: 来源标识
            suggestion_id: 关联的变更建议ID
            actor: 执行者

        Returns:
            (MemoryVersion, MemoryEvent) 或 None（文档不存在时）
        """
        with self._lock:
            doc = self.documents.get(doc_id)
            if doc is None:
                logger.warning("AuditTrail: update failed, doc %s not found", doc_id)
                return None

            parent_version = self.versions.get(doc.current_version_id)
            parent_id = parent_version.version_id if parent_version else None
            version_number = (parent_version.version_number + 1) if parent_version else 1

            content_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()

            version = MemoryVersion(
                doc_id=doc_id,
                parent_version_id=parent_id,
                content=new_content,
                content_hash=content_hash,
                version_number=version_number,
            )

            event = MemoryEvent(
                category=EventCategory.MEMORY_UPDATED,
                doc_id=doc_id,
                version_id=version.version_id,
                suggestion_id=suggestion_id,
                source_type=source_type,
                source_id=source_id,
                actor=actor,
                details=f"Updated memory '{doc.memory_key}' v{version_number}",
            )

            version.event_id = event.event_id
            doc.current_version_id = version.version_id
            doc.updated_at = time.time()

            self.versions[version.version_id] = version
            self._events_by_doc[doc_id].append(event)
            self._events_all.append(event)

            self._stats["total_versions"] += 1
            self._stats["total_events"] += 1
            self._stats["total_commits"] += 1

        logger.info(
            "AuditTrail: updated %s → v%d (%s)",
            doc_id, version_number, version.version_id,
        )
        return version, event

    # ── 变更建议 ─────────────────────────────────────────────────

    def create_suggestion(
        self,
        doc_id: Optional[str],
        proposed_content: str,
        reason: str,
        risk_level: RiskLevel = RiskLevel.LOW,
        source_type: SourceType = SourceType.CONVERSATION,
        source_id: str = "",
        confidence: float = 0.5,
    ) -> MemorySuggestion:
        """创建变更建议。

        Args:
            doc_id: 目标文档ID（None=新建）
            proposed_content: 建议内容
            reason: 变更理由
            risk_level: 风险等级
            source_type: 来源类型
            source_id: 来源标识
            confidence: 置信度

        Returns:
            MemorySuggestion
        """
        with self._lock:
            previous_version_id = None
            if doc_id and doc_id in self.documents:
                previous_version_id = self.documents[doc_id].current_version_id

            suggestion = MemorySuggestion(
                doc_id=doc_id,
                proposed_content=proposed_content,
                previous_version_id=previous_version_id,
                risk_level=risk_level,
                reason=reason,
                source_type=source_type,
                source_id=source_id,
                confidence=confidence,
            )

            event = MemoryEvent(
                category=EventCategory.SUGGESTION_CREATED,
                doc_id=doc_id or "",
                version_id="",
                suggestion_id=suggestion.suggestion_id,
                source_type=source_type,
                source_id=source_id,
                actor="ai",
                details=f"Suggestion: {reason[:120]}",
            )

            self.suggestions[suggestion.suggestion_id] = suggestion
            if doc_id:
                self._events_by_doc[doc_id].append(event)
            self._events_all.append(event)

            self._stats["total_suggestions"] += 1
            self._stats["total_events"] += 1

        logger.info(
            "AuditTrail: suggestion %s for doc=%s risk=%s",
            suggestion.suggestion_id, doc_id, risk_level.value,
        )
        return suggestion

    def update_suggestion_status(
        self,
        suggestion_id: str,
        new_status: SuggestionStatus,
        reviewer_note: str = "",
    ) -> bool:
        """更新变更建议状态。"""
        with self._lock:
            sug = self.suggestions.get(suggestion_id)
            if sug is None:
                return False
            sug.status = new_status
            sug.reviewed_at = time.time()
            sug.reviewer_note = reviewer_note

            category = {
                SuggestionStatus.ACCEPTED: EventCategory.SUGGESTION_ACCEPTED,
                SuggestionStatus.REJECTED: EventCategory.SUGGESTION_REJECTED,
                SuggestionStatus.MODIFIED: EventCategory.SUGGESTION_MODIFIED,
            }.get(new_status, EventCategory.SUGGESTION_MODIFIED)

            event = MemoryEvent(
                category=category,
                doc_id=sug.doc_id or "",
                version_id="",
                suggestion_id=suggestion_id,
                source_type=sug.source_type,
                source_id=sug.source_id,
                actor="human",
                details=f"Suggestion {new_status.value}: {reviewer_note[:120]}",
            )
            if sug.doc_id:
                self._events_by_doc[sug.doc_id].append(event)
            self._events_all.append(event)
            self._stats["total_events"] += 1

        return True

    # ── 溯源查询 ─────────────────────────────────────────────────

    def get_provenance(self, doc_id: str) -> Optional[ProvenanceChain]:
        """获取记忆的完整溯源链。

        Args:
            doc_id: 文档ID

        Returns:
            ProvenanceChain 或 None
        """
        with self._lock:
            doc = self.documents.get(doc_id)
            if doc is None:
                return None

            # 从当前版本向前追溯
            versions: List[MemoryVersion] = []
            seen: Set[str] = set()
            current_id = doc.current_version_id

            while current_id and current_id not in seen:
                ver = self.versions.get(current_id)
                if ver is None:
                    break
                seen.add(current_id)
                versions.append(ver)
                current_id = ver.parent_version_id

            versions.reverse()  # 最旧→最新

            events = list(self._events_by_doc.get(doc_id, []))

            # 收集相关建议
            suggestion_ids = {
                e.suggestion_id for e in events if e.suggestion_id
            }
            suggestions = [
                self.suggestions[sid]
                for sid in suggestion_ids
                if sid in self.suggestions
            ]

            return ProvenanceChain(
                doc_id=doc_id,
                chain=versions,
                events=events,
                suggestions=suggestions,
            )

    def get_current_content(self, doc_id: str) -> Optional[str]:
        """获取记忆当前内容。"""
        with self._lock:
            doc = self.documents.get(doc_id)
            if doc is None:
                return None
            version = self.versions.get(doc.current_version_id)
            return version.content if version else None

    def get_version(self, version_id: str) -> Optional[MemoryVersion]:
        """获取指定版本。"""
        with self._lock:
            return self.versions.get(version_id)

    def list_documents(self) -> List[MemoryDocument]:
        """列出所有文档。"""
        with self._lock:
            return list(self.documents.values())

    def get_pending_suggestions(self) -> List[MemorySuggestion]:
        """获取待审查的变更建议。"""
        with self._lock:
            return [
                s for s in self.suggestions.values()
                if s.status == SuggestionStatus.PENDING
            ]

    def get_events_by_doc(self, doc_id: str, limit: int = 100) -> List[MemoryEvent]:
        """获取某文档的审计事件。"""
        with self._lock:
            events = list(self._events_by_doc.get(doc_id, []))
            return events[-limit:]

    # ── 回滚 ────────────────────────────────────────────────────

    def rollback(self, doc_id: str, target_version_id: str) -> Optional[Tuple[MemoryVersion, MemoryEvent]]:
        """回滚到指定版本。

        回滚本身也是创建一个新版本（内容=目标版本内容），不破坏历史。

        Args:
            doc_id: 文档ID
            target_version_id: 目标版本ID

        Returns:
            (MemoryVersion, MemoryEvent) 或 None
        """
        with self._lock:
            target = self.versions.get(target_version_id)
            if target is None or target.doc_id != doc_id:
                return None

            return self.update_memory(
                doc_id=doc_id,
                new_content=target.content,
                source_type=SourceType.MANUAL,
                source_id=f"rollback_to:{target_version_id}",
                actor="human",
            )

    # ── 统计与诊断 ───────────────────────────────────────────────

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            pending = len(self.get_pending_suggestions())
            version_counts = [
                len([
                    v for v in self.versions.values()
                    if v.doc_id == doc.doc_id
                ])
                for doc in self.documents.values()
            ]
            return {
                "total_documents": len(self.documents),
                "total_versions": len(self.versions),
                "total_suggestions": len(self.suggestions),
                "pending_suggestions": pending,
                "total_events": len(self._events_all),
                "total_commits": self._stats["total_commits"],
                "total_rollbacks": self._stats["total_rollbacks"],
                "avg_versions_per_doc": (
                    float(np.mean(version_counts))
                    if version_counts else 0.0
                ),
                "max_versions_in_doc": (
                    max(version_counts) if version_counts else 0
                ),
            }

    def reset(self) -> None:
        """重置所有数据。"""
        with self._lock:
            self.documents.clear()
            self.versions.clear()
            self.suggestions.clear()
            self._events_by_doc.clear()
            self._events_all.clear()
            for k in self._stats:
                self._stats[k] = 0
