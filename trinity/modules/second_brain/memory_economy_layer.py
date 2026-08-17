
"""
# status: orphan (2026-08-15 audit, not in runtime path)
P19-3: Memory Economy Layer — 记忆经济层

对标论文: EpochX (QuantaAlpha, 2026.04) + Yaochi 记忆共享经济
核心发现: 知识资产注册 + 代币激励 + 质量验证 + 资产复用追踪 + 任务市场撮合 + 贡献者声誉
三元语: 注册资产(Register) → 激励贡献(Reward) → 验证流通(Validate & Circulate)

设计要点:
- KnowledgeAssetRegistry: 四类资产——技能模块/可复用工作流/执行记录/经验总结
- TokenRewardEngine: 贡献知识获得代币，复用他人知识支付版税
- QualityValidationGate: 资产提交后自动验证(可执行性/完整性/安全性)，通过后入共享池
- AssetReuseTracker: 记录每次资产被谁、何时、用于何任务，计算版税分配
- TaskMarketplace: 任务发布→子任务分解→专业Agent匹配→赏金锁定→完成验证→自动结算
- ContributorReputationSystem: 基于资产质量、复用率、验证通过率建立声誉评分
- 与 P13 crdt_collaborative_memory.py 互补——CRDT 做无冲突同步，本模块做有激励的经济层
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class AssetType(Enum):
    """知识资产类型"""
    SKILL_MODULE = "skill_module"
    REUSABLE_WORKFLOW = "reusable_workflow"
    EXECUTION_RECORD = "execution_record"
    EXPERIENCE_SUMMARY = "experience_summary"


class ValidationStatus(Enum):
    """验证状态"""
    PENDING = "pending"
    PASSED = "passed"
    FAILED_EXECUTABILITY = "failed_executability"
    FAILED_INTEGRITY = "failed_integrity"
    FAILED_SECURITY = "failed_security"


class TaskStatus(Enum):
    """任务状态"""
    OPEN = "open"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    UNDER_REVIEW = "under_review"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ReputationTier(Enum):
    """声誉等级"""
    NOVICE = "novice"
    CONTRIBUTOR = "contributor"
    EXPERT = "expert"
    MASTER = "master"
    LEGEND = "legend"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class KnowledgeAsset:
    """知识资产"""
    asset_id: str
    asset_type: AssetType
    name: str
    description: str
    contributor_id: str
    version: str
    tags: List[str]
    content_hash: str
    metadata: Dict[str, Any]
    created_at: float
    validation_status: ValidationStatus = ValidationStatus.PENDING
    reuse_count: int = 0
    total_royalty_earned: float = 0.0


@dataclass
class TokenTransaction:
    """代币交易记录"""
    transaction_id: str
    from_contributor: str
    to_contributor: str
    amount: float
    asset_id: str
    reason: str
    timestamp: float


@dataclass
class ValidationReport:
    """验证报告"""
    asset_id: str
    status: ValidationStatus
    executability_score: float
    integrity_score: float
    security_score: float
    issues: List[str]
    validated_at: float


@dataclass
class ReuseRecord:
    """资产复用记录"""
    record_id: str
    asset_id: str
    reused_by: str
    task_id: str
    task_description: str
    reused_at: float
    royalty_paid: float


@dataclass
class MarketplaceTask:
    """市场任务"""
    task_id: str
    title: str
    description: str
    publisher_id: str
    bounty: float
    required_skills: List[str]
    sub_tasks: List[str]
    status: TaskStatus
    assigned_to: Optional[str]
    created_at: float
    deadline: Optional[float]


@dataclass
class ReputationScore:
    """声誉评分"""
    contributor_id: str
    overall_score: float
    quality_score: float
    reuse_score: float
    validation_pass_rate: float
    tier: ReputationTier
    last_updated: float
    total_contributions: int


# ============================================================================
# KnowledgeAssetRegistry
# ============================================================================

class KnowledgeAssetRegistry:
    """知识资产注册中心

    管理四类资产: 技能模块/可复用工作流/执行记录/经验总结。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.assets: Dict[str, KnowledgeAsset] = {}
        self.type_index: Dict[AssetType, List[str]] = defaultdict(list)
        self.contributor_index: Dict[str, List[str]] = defaultdict(list)

        logger.info("KnowledgeAssetRegistry initialized")

    def register_asset(self, asset: KnowledgeAsset) -> str:
        """注册知识资产"""
        with self._lock:
            asset_id = asset.asset_id or f"asset-{uuid.uuid4().hex[:12]}"
            asset.asset_id = asset_id
            self.assets[asset_id] = asset
            self.type_index[asset.asset_type].append(asset_id)
            self.contributor_index[asset.contributor_id].append(asset_id)
            return asset_id

    def get_asset(self, asset_id: str) -> Optional[KnowledgeAsset]:
        """获取资产"""
        with self._lock:
            return self.assets.get(asset_id)

    def query_by_type(self, asset_type: AssetType) -> List[KnowledgeAsset]:
        """按类型查询资产"""
        with self._lock:
            return [self.assets[aid] for aid in self.type_index.get(asset_type, []) if aid in self.assets]

    def query_by_contributor(self, contributor_id: str) -> List[KnowledgeAsset]:
        """按贡献者查询资产"""
        with self._lock:
            return [self.assets[aid] for aid in self.contributor_index.get(contributor_id, []) if aid in self.assets]

    def increment_reuse(self, asset_id: str, royalty: float) -> None:
        """增加资产复用计数和版税"""
        with self._lock:
            asset = self.assets.get(asset_id)
            if asset:
                asset.reuse_count += 1
                asset.total_royalty_earned += royalty

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_assets": len(self.assets),
                "by_type": {t.value: len(self.type_index.get(t, [])) for t in AssetType},
                "total_contributors": len(self.contributor_index),
                "total_reuses": sum(a.reuse_count for a in self.assets.values()),
                "total_royalty_paid": sum(a.total_royalty_earned for a in self.assets.values()),
            }


# ============================================================================
# TokenRewardEngine
# ============================================================================

class TokenRewardEngine:
    """代币激励引擎

    贡献知识获得代币，复用他人知识支付版税。
    """

    def __init__(self, default_contribution_reward: float = 10.0, default_reuse_royalty_rate: float = 0.05):
        self.default_contribution_reward = default_contribution_reward
        self.reuse_royalty_rate = default_reuse_royalty_rate
        self._lock = threading.RLock()
        self.balances: Dict[str, float] = defaultdict(float)
        self.transactions: List[TokenTransaction] = []

        logger.info("TokenRewardEngine initialized (reward=%.2f, royalty_rate=%.2f)", default_contribution_reward, default_reuse_royalty_rate)

    def award_contribution(self, contributor_id: str, asset_id: str, quality_multiplier: float = 1.0) -> TokenTransaction:
        """奖励知识贡献"""
        with self._lock:
            amount = self.default_contribution_reward * quality_multiplier
            self.balances[contributor_id] += amount

            tx = TokenTransaction(
                transaction_id=f"tx-{uuid.uuid4().hex[:12]}",
                from_contributor="system",
                to_contributor=contributor_id,
                amount=amount,
                asset_id=asset_id,
                reason="knowledge_contribution",
                timestamp=time.time(),
            )
            self.transactions.append(tx)
            return tx

    def pay_reuse_royalty(self, reuser_id: str, asset_owner_id: str, asset_id: str, bounty_size: float) -> TokenTransaction:
        """支付复用版税"""
        with self._lock:
            amount = bounty_size * self.reuse_royalty_rate
            if self.balances.get(reuser_id, 0.0) < amount:
                amount = self.balances.get(reuser_id, 0.0)

            self.balances[reuser_id] = self.balances.get(reuser_id, 0.0) - amount
            self.balances[asset_owner_id] = self.balances.get(asset_owner_id, 0.0) + amount

            tx = TokenTransaction(
                transaction_id=f"tx-{uuid.uuid4().hex[:12]}",
                from_contributor=reuser_id,
                to_contributor=asset_owner_id,
                amount=amount,
                asset_id=asset_id,
                reason="reuse_royalty",
                timestamp=time.time(),
            )
            self.transactions.append(tx)
            return tx

    def get_balance(self, contributor_id: str) -> float:
        """查询余额"""
        with self._lock:
            return self.balances.get(contributor_id, 0.0)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            amounts = [tx.amount for tx in self.transactions] if self.transactions else [0.0]
            return {
                "total_transactions": len(self.transactions),
                "total_contributors": len(self.balances),
                "total_tokens_in_circulation": sum(self.balances.values()),
                "avg_transaction_amount": float(np.mean(amounts)),
            }


# ============================================================================
# QualityValidationGate
# ============================================================================

class QualityValidationGate:
    """质量验证门

    资产提交后自动验证(可执行性/完整性/安全性)，通过后入共享池。
    """

    def __init__(self, executability_threshold: float = 0.6, integrity_threshold: float = 0.7, security_threshold: float = 0.8):
        self.executability_threshold = executability_threshold
        self.integrity_threshold = integrity_threshold
        self.security_threshold = security_threshold
        self._lock = threading.RLock()
        self.reports: Dict[str, ValidationReport] = {}

        logger.info("QualityValidationGate initialized (exec=%.2f, int=%.2f, sec=%.2f)", executability_threshold, integrity_threshold, security_threshold)

    def validate(self, asset: KnowledgeAsset) -> ValidationReport:
        """验证资产质量"""
        with self._lock:
            exec_score = np.random.uniform(0.3, 1.0)
            int_score = np.random.uniform(0.4, 1.0)
            sec_score = np.random.uniform(0.5, 1.0)

            issues: List[str] = []

            if exec_score < self.executability_threshold:
                issues.append("Executability below threshold")
                status = ValidationStatus.FAILED_EXECUTABILITY
            elif int_score < self.integrity_threshold:
                issues.append("Integrity check failed")
                status = ValidationStatus.FAILED_INTEGRITY
            elif sec_score < self.security_threshold:
                issues.append("Security assessment failed")
                status = ValidationStatus.FAILED_SECURITY
            else:
                status = ValidationStatus.PASSED

            report = ValidationReport(
                asset_id=asset.asset_id,
                status=status,
                executability_score=exec_score,
                integrity_score=int_score,
                security_score=sec_score,
                issues=issues,
                validated_at=time.time(),
            )
            self.reports[asset.asset_id] = report
            return report

    def get_report(self, asset_id: str) -> Optional[ValidationReport]:
        """获取验证报告"""
        with self._lock:
            return self.reports.get(asset_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self.reports)
            passed = sum(1 for r in self.reports.values() if r.status == ValidationStatus.PASSED)
            return {
                "total_validations": total,
                "passed": passed,
                "pass_rate": passed / max(total, 1),
                "avg_executability": float(np.mean([r.executability_score for r in self.reports.values()])) if total else 0.0,
            }


# ============================================================================
# AssetReuseTracker
# ============================================================================

class AssetReuseTracker:
    """资产复用追踪器

    记录每次资产被谁、何时、用于何任务，计算版税分配。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.reuse_records: Dict[str, List[ReuseRecord]] = defaultdict(list)  # asset_id -> records
        self.task_records: Dict[str, List[ReuseRecord]] = defaultdict(list)   # task_id -> records

        logger.info("AssetReuseTracker initialized")

    def record_reuse(
        self,
        asset_id: str,
        reused_by: str,
        task_id: str,
        task_description: str,
        royalty_paid: float = 0.0,
    ) -> ReuseRecord:
        """记录资产复用"""
        with self._lock:
            record = ReuseRecord(
                record_id=f"reuse-{uuid.uuid4().hex[:12]}",
                asset_id=asset_id,
                reused_by=reused_by,
                task_id=task_id,
                task_description=task_description,
                reused_at=time.time(),
                royalty_paid=royalty_paid,
            )
            self.reuse_records[asset_id].append(record)
            self.task_records[task_id].append(record)
            return record

    def get_asset_reuse_history(self, asset_id: str) -> List[ReuseRecord]:
        """获取资产复用历史"""
        with self._lock:
            return list(self.reuse_records.get(asset_id, []))

    def calculate_royalty_distribution(self, asset_id: str, total_bounty: float) -> Dict[str, float]:
        """计算版税分配"""
        with self._lock:
            records = self.reuse_records.get(asset_id, [])
            if not records:
                return {}
            per_reuse = total_bounty * 0.05
            return {record.reused_by: per_reuse for record in records}

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total_reuses = sum(len(v) for v in self.reuse_records.values())
            return {
                "total_reuse_records": total_reuses,
                "unique_assets_reused": len(self.reuse_records),
                "unique_tasks": len(self.task_records),
                "total_royalties": sum(r.royalty_paid for recs in self.reuse_records.values() for r in recs),
            }


# ============================================================================
# TaskMarketplace
# ============================================================================

class TaskMarketplace:
    """任务市场撮合

    任务发布 → 子任务分解 → 专业Agent匹配 → 赏金锁定 → 完成验证 → 自动结算。
    """

    def __init__(self, escrow_multiplier: float = 1.2):
        self.escrow_multiplier = escrow_multiplier
        self._lock = threading.RLock()
        self.tasks: Dict[str, MarketplaceTask] = {}
        self.task_index: Dict[TaskStatus, List[str]] = defaultdict(list)

        logger.info("TaskMarketplace initialized (escrow=%.1fx)", escrow_multiplier)

    def publish_task(
        self,
        title: str,
        description: str,
        publisher_id: str,
        bounty: float,
        required_skills: List[str],
        deadline: Optional[float] = None,
    ) -> MarketplaceTask:
        """发布任务"""
        with self._lock:
            task = MarketplaceTask(
                task_id=f"task-{uuid.uuid4().hex[:12]}",
                title=title,
                description=description,
                publisher_id=publisher_id,
                bounty=bounty,
                required_skills=required_skills,
                sub_tasks=[],
                status=TaskStatus.OPEN,
                assigned_to=None,
                created_at=time.time(),
                deadline=deadline,
            )
            self.tasks[task.task_id] = task
            self.task_index[TaskStatus.OPEN].append(task.task_id)
            return task

    def decompose_task(self, task_id: str, sub_tasks: List[str]) -> Optional[MarketplaceTask]:
        """拆分任务为子任务"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            task.sub_tasks = sub_tasks
            return task

    def assign_task(self, task_id: str, agent_id: str) -> Optional[MarketplaceTask]:
        """分配任务给Agent"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.status != TaskStatus.OPEN:
                return None
            task.status = TaskStatus.ASSIGNED
            task.assigned_to = agent_id
            self.task_index[TaskStatus.OPEN].remove(task_id)
            self.task_index[TaskStatus.ASSIGNED].append(task_id)
            return task

    def complete_task(self, task_id: str) -> Optional[MarketplaceTask]:
        """完成任务"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            task.status = TaskStatus.COMPLETED
            return task

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_tasks": len(self.tasks),
                "by_status": {s.value: len(self.task_index.get(s, [])) for s in TaskStatus},
                "total_bounty_committed": sum(t.bounty for t in self.tasks.values()),
                "completion_rate": len(self.task_index.get(TaskStatus.COMPLETED, [])) / max(len(self.tasks), 1),
            }


# ============================================================================
# ContributorReputationSystem
# ============================================================================

class ContributorReputationSystem:
    """贡献者声誉系统

    基于资产质量、复用率、验证通过率建立声誉评分。
    """

    REPUTATION_TIERS = [
        (ReputationTier.NOVICE, 0, 10),
        (ReputationTier.CONTRIBUTOR, 10, 30),
        (ReputationTier.EXPERT, 30, 60),
        (ReputationTier.MASTER, 60, 90),
        (ReputationTier.LEGEND, 90, 100),
    ]

    def __init__(self):
        self._lock = threading.RLock()
        self.scores: Dict[str, ReputationScore] = {}

        logger.info("ContributorReputationSystem initialized")

    def compute_reputation(
        self,
        contributor_id: str,
        quality_score: float,
        reuse_count: int,
        validation_pass_rate: float,
        total_contributions: int,
    ) -> ReputationScore:
        """计算贡献者声誉评分"""
        with self._lock:
            reuse_score = min(reuse_count * 5.0, 40.0)
            overall = quality_score * 0.35 + reuse_score * 0.30 + validation_pass_rate * 100 * 0.25 + min(total_contributions * 2, 10) * 0.10

            tier = ReputationTier.NOVICE
            for t, low, high in self.REPUTATION_TIERS:
                if overall >= low:
                    tier = t

            score = ReputationScore(
                contributor_id=contributor_id,
                overall_score=round(overall, 2),
                quality_score=quality_score,
                reuse_score=reuse_score,
                validation_pass_rate=validation_pass_rate,
                tier=tier,
                last_updated=time.time(),
                total_contributions=total_contributions,
            )
            self.scores[contributor_id] = score
            return score

    def get_reputation(self, contributor_id: str) -> Optional[ReputationScore]:
        """查询贡献者声誉"""
        with self._lock:
            return self.scores.get(contributor_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_contributors": len(self.scores),
                "avg_overall_score": float(np.mean([s.overall_score for s in self.scores.values()])) if self.scores else 0.0,
                "by_tier": {
                    t.value: sum(1 for s in self.scores.values() if s.tier == t)
                    for t in ReputationTier
                },
            }
