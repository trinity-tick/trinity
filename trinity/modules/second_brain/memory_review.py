"""
# status: orphan (2026-08-15 audit, not in runtime path)
P6-4: Memory Change Human-in-the-Loop Review (对标 Immutable Audit Trail)
==========================================================================

Suggestion → Review → Commit 管线：
  - AI 提出变更建议（suggestion）
  - 生成变更摘要（diff summary）
  - 等待审查（review）
  - 通过/拒绝/修改（accept/reject/modify）
  - 提交到审计追踪（commit）

支持批量审查、自动通过低风险变更、高风险变更强制人工审查。

Reference: Erturk, "Why Your AI Agent Needs an Immutable Audit Trail
           for Memory", 2026. https://ertyurk.com/posts/...
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

from trinity.modules.second_brain.audit_trail import (
    AuditTrail,
    EventCategory,
    MemoryEvent,
    MemorySuggestion,
    MemoryVersion,
    RiskLevel,
    SourceType,
    SuggestionStatus,
)

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────────

class ReviewDecision(Enum):
    """审查决策。"""
    ACCEPT = "accept"               # 接受
    REJECT = "reject"               # 拒绝
    MODIFY = "modify"               # 修改后接受
    DEFER = "defer"                 # 推迟审查
    AUTO_ACCEPT = "auto_accept"     # 自动通过（低风险）


class ReviewMode(Enum):
    """审查模式。"""
    STRICT = "strict"       # 严格：所有变更需人工审查
    BALANCED = "balanced"   # 平衡：低风险自动通过，中高风险人工
    RELAXED = "relaxed"     # 宽松：低中风险自动通过，仅高风险人工


class BatchStatus(Enum):
    """批量审查状态。"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    PARTIALLY_APPROVED = "partially_approved"


# ── 数据结构 ─────────────────────────────────────────────────────────

@dataclass
class DiffSummary:
    """变更摘要（新旧内容对比）。

    Args:
        doc_id: 文档ID
        suggestion_id: 变更建议ID
        old_content: 旧内容（摘要）
        new_content: 新内容
        diff_lines: unified diff 文本行
        changed_ratio: 变更比例 [0,1]
    """
    doc_id: Optional[str] = None
    suggestion_id: str = ""
    old_content: str = ""
    new_content: str = ""
    diff_lines: str = ""
    changed_ratio: float = 0.0


@dataclass
class ReviewRecord:
    """审查记录。

    Args:
        review_id: 审查记录ID
        suggestion_id: 关联建议ID
        decision: 审查决策
        reviewer: 审查者标识
        note: 审查备注
        modified_content: 修改后的内容（decision=MODIFY时）
        timestamp: 审查时间
    """
    review_id: str = field(default_factory=lambda: f"rev_{uuid.uuid4().hex[:12]}")
    suggestion_id: str = ""
    decision: ReviewDecision = ReviewDecision.ACCEPT
    reviewer: str = "human"
    note: str = ""
    modified_content: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class BatchReview:
    """批量审查批次。

    Args:
        batch_id: 批次ID
        suggestions: 建议ID列表
        status: 批次状态
        decisions: 已做出的审查决策
        created_at: 创建时间
        completed_at: 完成时间
    """
    batch_id: str = field(default_factory=lambda: f"bat_{uuid.uuid4().hex[:12]}")
    suggestions: List[str] = field(default_factory=list)
    status: BatchStatus = BatchStatus.PENDING
    decisions: Dict[str, ReviewDecision] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None


@dataclass
class ReviewReport:
    """审查执行报告。

    Args:
        report_id: 报告ID
        total_reviewed: 总审查数
        accepted: 接受数
        rejected: 拒绝数
        modified: 修改数
        auto_accepted: 自动通过数
        deferred: 推迟数
        records: 审查记录列表
        elapsed_ms: 耗时
    """
    report_id: str = field(default_factory=lambda: f"rr_{uuid.uuid4().hex[:12]}")
    total_reviewed: int = 0
    accepted: int = 0
    rejected: int = 0
    modified: int = 0
    auto_accepted: int = 0
    deferred: int = 0
    records: List[ReviewRecord] = field(default_factory=list)
    elapsed_ms: float = 0.0


# ── 人机协同审查管线 ─────────────────────────────────────────────────

class MemoryReviewPipeline:
    """Suggestion → Review → Commit 人机协同审查管线。

    核心流程：
    1. AI 提出变更建议 → 生成 DiffSummary
    2. 风险评估 → 低风险可自动通过
    3. 人工审查（或自动通过）→ 通过/拒绝/修改
    4. 提交变更到 AuditTrail

    Attributes:
        audit_trail: 关联的审计追踪系统
        mode: 审查模式
        auto_accept_risk_below: 自动通过的风险等级阈值
        records: 审查记录历史
    """

    def __init__(
        self,
        audit_trail: AuditTrail,
        mode: ReviewMode = ReviewMode.BALANCED,
        auto_accept_risk_below: RiskLevel = RiskLevel.LOW,
    ):
        self.audit_trail = audit_trail
        self.mode = mode
        self.auto_accept_risk_below = auto_accept_risk_below
        self.records: deque = deque(maxlen=5000)
        self._lock = threading.RLock()

        self._stats: Dict[str, int] = {
            "total_proposals": 0,
            "total_reviews": 0,
            "auto_accepted": 0,
            "manually_accepted": 0,
            "rejected": 0,
            "modified": 0,
            "deferred": 0,
            "total_commits": 0,
        }

    # ── 变更提议 ─────────────────────────────────────────────────

    def propose_change(
        self,
        doc_id: Optional[str],
        proposed_content: str,
        reason: str,
        risk_level: RiskLevel = RiskLevel.LOW,
        source_type: SourceType = SourceType.CONVERSATION,
        source_id: str = "",
        confidence: float = 0.5,
    ) -> Tuple[MemorySuggestion, DiffSummary]:
        """AI 提出记忆变更建议。

        Args:
            doc_id: 目标文档ID（None=新建）
            proposed_content: 建议内容
            reason: 变更理由
            risk_level: 风险等级
            source_type: 来源类型
            source_id: 来源标识
            confidence: AI 置信度

        Returns:
            (MemorySuggestion, DiffSummary)
        """
        with self._lock:
            # 创建建议
            suggestion = self.audit_trail.create_suggestion(
                doc_id=doc_id,
                proposed_content=proposed_content,
                reason=reason,
                risk_level=risk_level,
                source_type=source_type,
                source_id=source_id,
                confidence=confidence,
            )

            # 生成差异摘要
            diff_summary = self._generate_diff(suggestion)

            self._stats["total_proposals"] += 1

        logger.info(
            "MemoryReview: proposed change for doc=%s risk=%s conf=%.2f",
            doc_id, risk_level.value, confidence,
        )
        return suggestion, diff_summary

    # ── 差异分析 ─────────────────────────────────────────────────

    def _generate_diff(self, suggestion: MemorySuggestion) -> DiffSummary:
        """生成变更差异摘要。"""
        old_content = ""
        if suggestion.doc_id:
            old_content = self.audit_trail.get_current_content(suggestion.doc_id) or ""

        if old_content:
            differ = difflib.unified_diff(
                old_content.splitlines(keepends=True),
                suggestion.proposed_content.splitlines(keepends=True),
                fromfile="old",
                tofile="new",
                lineterm="",
            )
            diff_text = "".join(list(differ)[:200])  # 截断过长diff
            total_lines = max(
                len(suggestion.proposed_content.splitlines()),
                len(old_content.splitlines()),
                1,
            )
            changed_lines = sum(
                1 for line in diff_text.splitlines()
                if line.startswith("+") or line.startswith("-")
            )
            changed_ratio = min(1.0, changed_lines / max(total_lines, 1))
        else:
            diff_text = f"[NEW] {suggestion.proposed_content[:500]}"
            changed_ratio = 1.0

        return DiffSummary(
            doc_id=suggestion.doc_id,
            suggestion_id=suggestion.suggestion_id,
            old_content=old_content[:500],
            new_content=suggestion.proposed_content[:500],
            diff_lines=diff_text,
            changed_ratio=changed_ratio,
        )

    def get_diff(self, suggestion_id: str) -> Optional[DiffSummary]:
        """获取指定建议的差异摘要。"""
        suggestion = self.audit_trail.suggestions.get(suggestion_id)
        if suggestion is None:
            return None
        return self._generate_diff(suggestion)

    # ── 风险判定 ─────────────────────────────────────────────────

    def should_auto_accept(self, risk_level: RiskLevel) -> bool:
        """判断是否应自动通过。

        Args:
            risk_level: 风险等级

        Returns:
            True 如果应自动通过
        """
        if self.mode == ReviewMode.STRICT:
            return False
        if self.mode == ReviewMode.RELAXED:
            return risk_level.value in ("low", "medium")
        # BALANCED
        risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        return risk_order[risk_level.value] <= risk_order[self.auto_accept_risk_below.value]

    # ── 审查执行 ─────────────────────────────────────────────────

    def review_suggestion(
        self,
        suggestion_id: str,
        decision: ReviewDecision,
        reviewer: str = "human",
        note: str = "",
        modified_content: Optional[str] = None,
    ) -> Optional[ReviewRecord]:
        """审查单个变更建议。

        Args:
            suggestion_id: 建议ID
            decision: 审查决策
            reviewer: 审查者
            note: 审查备注
            modified_content: 修改后的内容（decision=MODIFY时）

        Returns:
            ReviewRecord 或 None（建议不存在时）
        """
        with self._lock:
            suggestion = self.audit_trail.suggestions.get(suggestion_id)
            if suggestion is None:
                logger.warning("MemoryReview: suggestion %s not found", suggestion_id)
                return None

            record = ReviewRecord(
                suggestion_id=suggestion_id,
                decision=decision,
                reviewer=reviewer,
                note=note,
                modified_content=modified_content,
            )

            # 更新建议状态
            if decision == ReviewDecision.ACCEPT or decision == ReviewDecision.AUTO_ACCEPT:
                self.audit_trail.update_suggestion_status(
                    suggestion_id, SuggestionStatus.ACCEPTED, reviewer_note=note
                )
                self._stats["manually_accepted" if decision == ReviewDecision.ACCEPT else "auto_accepted"] += 1

            elif decision == ReviewDecision.REJECT:
                self.audit_trail.update_suggestion_status(
                    suggestion_id, SuggestionStatus.REJECTED, reviewer_note=note
                )
                self._stats["rejected"] += 1

            elif decision == ReviewDecision.MODIFY:
                if modified_content:
                    suggestion.proposed_content = modified_content
                self.audit_trail.update_suggestion_status(
                    suggestion_id, SuggestionStatus.MODIFIED, reviewer_note=note
                )
                self._stats["modified"] += 1

            elif decision == ReviewDecision.DEFER:
                self._stats["deferred"] += 1

            self.records.append(record)
            self._stats["total_reviews"] += 1

        return record

    def auto_review_pending(self) -> ReviewReport:
        """自动审查所有待处理的低风险建议。

        Returns:
            ReviewReport
        """
        start = time.perf_counter()
        pending = self.audit_trail.get_pending_suggestions()
        records: List[ReviewRecord] = []

        auto_accept_count = 0
        for suggestion in pending:
            if self.should_auto_accept(suggestion.risk_level):
                record = self.review_suggestion(
                    suggestion.suggestion_id,
                    ReviewDecision.AUTO_ACCEPT,
                    reviewer="system",
                    note=f"Auto-accepted (risk={suggestion.risk_level.value}, mode={self.mode.value})",
                )
                if record:
                    records.append(record)
                    auto_accept_count += 1

        elapsed = (time.perf_counter() - start) * 1000.0
        return ReviewReport(
            total_reviewed=auto_accept_count,
            accepted=0,
            rejected=0,
            modified=0,
            auto_accepted=auto_accept_count,
            deferred=0,
            records=records,
            elapsed_ms=elapsed,
        )

    # ── 提交变更 ─────────────────────────────────────────────────

    def commit_accepted(
        self, suggestion_id: str, actor: str = "system",
    ) -> Optional[Tuple[MemoryVersion, MemoryEvent]]:
        """将已接受的建议提交到审计追踪。

        Args:
            suggestion_id: 建议ID
            actor: 提交者

        Returns:
            (MemoryVersion, MemoryEvent) 或 None
        """
        with self._lock:
            suggestion = self.audit_trail.suggestions.get(suggestion_id)
            if suggestion is None:
                return None

            if suggestion.status not in (SuggestionStatus.ACCEPTED, SuggestionStatus.MODIFIED):
                logger.warning(
                    "MemoryReview: cannot commit suggestion %s with status %s",
                    suggestion_id, suggestion.status.value,
                )
                return None

            if suggestion.doc_id:
                result = self.audit_trail.update_memory(
                    doc_id=suggestion.doc_id,
                    new_content=suggestion.proposed_content,
                    source_type=suggestion.source_type,
                    source_id=suggestion.source_id,
                    suggestion_id=suggestion_id,
                    actor=actor,
                )
            else:
                # 新建文档
                key = f"memory_{suggestion.suggestion_id[:8]}"
                doc, ver, evt = self.audit_trail.create_memory(
                    memory_key=key,
                    content=suggestion.proposed_content,
                    source_type=suggestion.source_type,
                    source_id=suggestion.source_id,
                )
                result = (ver, evt)

            if result:
                self._stats["total_commits"] += 1

            return result

    def commit_all_accepted(self) -> int:
        """批量提交所有已接受的建议。

        Returns:
            成功提交数量
        """
        accepted = [
            s for s in self.audit_trail.suggestions.values()
            if s.status in (SuggestionStatus.ACCEPTED, SuggestionStatus.MODIFIED)
        ]
        committed = 0
        for s in accepted:
            if self.commit_accepted(s.suggestion_id):
                committed += 1
        return committed

    # ── 批量审查 ─────────────────────────────────────────────────

    def create_batch_review(self, suggestion_ids: List[str]) -> BatchReview:
        """创建批量审查批次。"""
        batch = BatchReview(suggestions=suggestion_ids)
        return batch

    def review_batch(
        self, batch: BatchReview,
        decisions: Dict[str, Tuple[ReviewDecision, str]],
        reviewer: str = "human",
    ) -> ReviewReport:
        """执行批量审查。

        Args:
            batch: 审查批次
            decisions: {suggestion_id: (decision, note)}
            reviewer: 审查者

        Returns:
            ReviewReport
        """
        start = time.perf_counter()
        records: List[ReviewRecord] = []
        accepted = rejected = modified = auto_acc = deferred = 0

        batch.status = BatchStatus.IN_PROGRESS
        for sid in batch.suggestions:
            decision, note = decisions.get(sid, (ReviewDecision.DEFER, ""))
            record = self.review_suggestion(sid, decision, reviewer=reviewer, note=note)
            if record:
                records.append(record)
                batch.decisions[sid] = decision
                if decision == ReviewDecision.ACCEPT:
                    accepted += 1
                elif decision == ReviewDecision.AUTO_ACCEPT:
                    auto_acc += 1
                elif decision == ReviewDecision.REJECT:
                    rejected += 1
                elif decision == ReviewDecision.MODIFY:
                    modified += 1
                else:
                    deferred += 1

        batch.status = BatchStatus.COMPLETED
        batch.completed_at = time.time()
        elapsed = (time.perf_counter() - start) * 1000.0

        return ReviewReport(
            total_reviewed=len(records),
            accepted=accepted,
            rejected=rejected,
            modified=modified,
            auto_accepted=auto_acc,
            deferred=deferred,
            records=records,
            elapsed_ms=elapsed,
        )

    # ── 获取审查摘要 ─────────────────────────────────────────────

    def get_pending_for_review(self) -> List[Dict[str, Any]]:
        """获取待人工审查的建议列表（含差异摘要）。"""
        pending = self.audit_trail.get_pending_suggestions()
        # 过滤掉可自动通过的
        requires_human = [
            s for s in pending
            if not self.should_auto_accept(s.risk_level)
        ]
        result = []
        for s in requires_human:
            diff = self._generate_diff(s)
            result.append({
                "suggestion_id": s.suggestion_id,
                "doc_id": s.doc_id,
                "reason": s.reason,
                "risk_level": s.risk_level.value,
                "confidence": s.confidence,
                "source_id": s.source_id,
                "changed_ratio": diff.changed_ratio,
                "diff_preview": diff.diff_lines[:300],
                "created_at": s.created_at,
            })
        return sorted(result, key=lambda x: (
            {"critical": 0, "high": 1, "medium": 2, "low": 3}[x["risk_level"]],
            -x["changed_ratio"],
        ))

    # ── 统计与诊断 ───────────────────────────────────────────────

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            pending = len(self.audit_trail.get_pending_suggestions())
            accepted_pending_commit = len([
                s for s in self.audit_trail.suggestions.values()
                if s.status in (SuggestionStatus.ACCEPTED, SuggestionStatus.MODIFIED)
                and s.doc_id  # 尚未 commit（doc仍存在）
            ])
            return {
                "total_proposals": self._stats["total_proposals"],
                "total_reviews": self._stats["total_reviews"],
                "auto_accepted": self._stats["auto_accepted"],
                "manually_accepted": self._stats["manually_accepted"],
                "rejected": self._stats["rejected"],
                "modified": self._stats["modified"],
                "deferred": self._stats["deferred"],
                "total_commits": self._stats["total_commits"],
                "pending_review": pending,
                "accepted_pending_commit": accepted_pending_commit,
                "review_mode": self.mode.value,
                "auto_accept_threshold": self.auto_accept_risk_below.value,
            }

    def reset(self) -> None:
        """重置审查历史（不重置审计追踪）。"""
        with self._lock:
            self.records.clear()
            for k in self._stats:
                self._stats[k] = 0
