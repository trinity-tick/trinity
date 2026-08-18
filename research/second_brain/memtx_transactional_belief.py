"""
# status: orphan (2026-08-15 audit, not in runtime path)
MemTxn Transactional Belief State for Agents
=============================================
arXiv 2607.27834 · P45-4

事务管理器维护暂存区(staging)与提交区(committed)双区。
仅通过验证的暂存信念才能提交; 回滚按事务边界恢复一致性快照。
只有已提交区的信念才能驱动 agent 对外行动。

设计要点:
  - MemTxnTransactionManager: 双区事务管理器
  - BeliefCommitGate: 信念提交门控(验证+提交)
  - MemTxnRollbackHandler: 回滚至上一个一致性快照
  - ActionGatingController: 行动门控, 仅已提交信念可驱动行动
  - TXBoundary: 事务边界
  - StagingBelief: 暂存信念
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TXStatus(Enum):
    """事务状态。"""
    STAGING = auto()      # 暂存中
    COMMITTED = auto()     # 已提交
    ROLLED_BACK = auto()   # 已回滚
    VERIFIED = auto()      # 已验证, 待提交


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class StagingBelief:
    """暂存信念——位于 staging 区, 未提交。

    Attributes
    ----------
    belief_id : str
    content : 信念内容 (文本或结构化)
    source : 信念来源 (observation / reasoning / external)
    confidence : 置信度 0~1
    created_at : 创建时间
    tx_id : 所属事务 ID
    status : 当前状态
    """
    belief_id: str
    content: Any
    source: str = "observation"
    confidence: float = 0.5
    created_at: float = field(default_factory=time.time)
    tx_id: str = ""
    status: TXStatus = TXStatus.STAGING


@dataclass
class TXBoundary:
    """事务边界——标记一组暂存信念的提交/回滚单元。"""
    tx_id: str
    belief_ids: List[str] = field(default_factory=list)
    snapshot: Dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    committed_at: float = 0.0
    status: TXStatus = TXStatus.STAGING


# ---------------------------------------------------------------------------
# BeliefCommitGate
# ---------------------------------------------------------------------------

class BeliefCommitGate:
    """信念提交门控——只有通过验证的暂存信念才能提交。

    验证规则: confidence >= min_confidence, source 不为空, content 不为空。
    """

    def __init__(self, min_confidence: float = 0.6) -> None:
        self.min_confidence = min_confidence
        self._validation_log: deque = deque(maxlen=200)
        self._lock = threading.RLock()

    def validate(self, belief: StagingBelief) -> bool:
        """验证信念是否可提交。"""
        with self._lock:
            reasons: List[str] = []

            if belief.confidence < self.min_confidence:
                reasons.append(f"confidence {belief.confidence:.2f} < {self.min_confidence}")
            if not belief.source:
                reasons.append("empty source")
            if belief.content is None or (isinstance(belief.content, str) and not belief.content.strip()):
                reasons.append("empty content")

            valid = len(reasons) == 0
            self._validation_log.append({
                "belief_id": belief.belief_id,
                "valid": valid,
                "reasons": reasons,
                "timestamp": time.time(),
            })

            if valid:
                belief.status = TXStatus.VERIFIED

            return valid

    def statistics(self) -> Dict[str, Any]:
        return {
            "min_confidence": self.min_confidence,
            "validations_total": len(self._validation_log),
        }


# ---------------------------------------------------------------------------
# MemTxnRollbackHandler
# ---------------------------------------------------------------------------

class MemTxnRollbackHandler:
    """回滚处理器——按事务边界回退到上一个一致性快照。"""

    def __init__(self) -> None:
        self._snapshots: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    def save_snapshot(self, committed_beliefs: Dict[str, StagingBelief]) -> str:
        """保存一致性快照。"""
        with self._lock:
            snap_id = f"snap_{len(self._snapshots)}_{int(time.time()*1e6)}"
            snapshot = {
                "snap_id": snap_id,
                "belief_ids": list(committed_beliefs.keys()),
                "belief_count": len(committed_beliefs),
                "timestamp": time.time(),
            }
            self._snapshots.append(snapshot)
            return snap_id

    def rollback(
        self,
        committed: Dict[str, StagingBelief],
        staging: Dict[str, StagingBelief],
        target_snap_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """回滚——恢复到指定快照 (默认上一个)。"""
        with self._lock:
            if not self._snapshots:
                return {"rolled_back": False, "reason": "No snapshots available"}

            if target_snap_id:
                target_snap = next((s for s in self._snapshots if s["snap_id"] == target_snap_id), None)
            else:
                target_snap = self._snapshots[-1]

            if target_snap is None:
                return {"rolled_back": False, "reason": "Snapshot not found"}

            # 清除 staging
            staging.clear()

            # 清除 committed 中不在快照内的
            target_ids = set(target_snap["belief_ids"])
            removed = [bid for bid in committed if bid not in target_ids]
            for bid in removed:
                del committed[bid]

            return {
                "rolled_back": True,
                "target_snapshot": target_snap["snap_id"],
                "removed_beliefs": len(removed),
                "committed_remaining": len(committed),
            }

    def statistics(self) -> Dict[str, Any]:
        return {"snapshots": len(self._snapshots)}


# ---------------------------------------------------------------------------
# ActionGatingController
# ---------------------------------------------------------------------------

class ActionGatingController:
    """行动门控——只有已提交区的信念才能驱动 agent 对外行动。"""

    def __init__(self) -> None:
        self._action_log: deque = deque(maxlen=200)
        self._lock = threading.RLock()

    def gate_action(
        self, belief: StagingBelief, committed: Dict[str, StagingBelief]
    ) -> Tuple[bool, str]:
        """检查信念是否已提交, 允许驱动行动。

        Returns
        -------
        Tuple[bool, str]
            (是否允许, 原因)
        """
        with self._lock:
            if belief.belief_id not in committed:
                self._action_log.append({
                    "belief_id": belief.belief_id,
                    "allowed": False,
                    "reason": "Belief not in committed region",
                })
                return False, "Belief not in committed region"

            committed_belief = committed[belief.belief_id]
            if committed_belief.status != TXStatus.COMMITTED:
                self._action_log.append({
                    "belief_id": belief.belief_id,
                    "allowed": False,
                    "reason": f"Belief status is {committed_belief.status.name}",
                })
                return False, f"Belief status is {committed_belief.status.name}"

            self._action_log.append({
                "belief_id": belief.belief_id,
                "allowed": True,
                "reason": "OK",
            })
            return True, "OK"

    def statistics(self) -> Dict[str, Any]:
        return {"actions_gated": len(self._action_log)}


# ---------------------------------------------------------------------------
# MemTxnTransactionManager
# ---------------------------------------------------------------------------

class MemTxnTransactionManager:
    """MemTxn 事务管理器——维护暂存区 + 提交区双区。

    Parameters
    ----------
    min_confidence : float
        提交门控的最低置信度。
    """

    def __init__(self, min_confidence: float = 0.6) -> None:
        self._staging: Dict[str, StagingBelief] = {}
        self._committed: Dict[str, StagingBelief] = {}
        self.commit_gate = BeliefCommitGate(min_confidence=min_confidence)
        self.rollback_handler = MemTxnRollbackHandler()
        self.action_gating = ActionGatingController()
        self._belief_count: int = 0
        self._lock = threading.RLock()

        logger.info("MemTxnTransactionManager initialized [min_conf=%.2f]", min_confidence)

    def stage(
        self, content: Any, source: str = "observation", confidence: float = 0.5
    ) -> StagingBelief:
        """暂存信念到 staging 区。"""
        with self._lock:
            self._belief_count += 1
            belief = StagingBelief(
                belief_id=f"blf_{self._belief_count}_{int(time.time()*1e6)}",
                content=content,
                source=source,
                confidence=confidence,
                tx_id=f"tx_{int(time.time()*1e6)}",
            )
            self._staging[belief.belief_id] = belief
            return belief

    def commit(self) -> Dict[str, Any]:
        """提交所有已验证的暂存信念到 committed 区。"""
        with self._lock:
            # 保存快照
            self.rollback_handler.save_snapshot(self._committed)

            committed_count = 0
            rejected_count = 0

            to_commit: List[str] = []
            for bid, belief in self._staging.items():
                if self.commit_gate.validate(belief):
                    to_commit.append(bid)
                else:
                    rejected_count += 1

            for bid in to_commit:
                belief = self._staging.pop(bid)
                belief.status = TXStatus.COMMITTED
                self._committed[bid] = belief
                committed_count += 1

            return {
                "committed": committed_count,
                "rejected": rejected_count,
                "staging_remaining": len(self._staging),
                "committed_total": len(self._committed),
            }

    def rollback(self, target_snap_id: Optional[str] = None) -> Dict[str, Any]:
        """回滚到上一个一致性快照。"""
        return self.rollback_handler.rollback(self._committed, self._staging, target_snap_id)

    def can_act(self, belief_id: str) -> Tuple[bool, str]:
        """检查信念是否可驱动行动。"""
        belief = self._committed.get(belief_id) or self._staging.get(belief_id)
        if belief is None:
            return False, "Belief not found"
        return self.action_gating.gate_action(belief, self._committed)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "staging_count": len(self._staging),
                "committed_count": len(self._committed),
                "total_beliefs": self._belief_count,
                "commit_gate": self.commit_gate.statistics(),
                "rollback": self.rollback_handler.statistics(),
                "action_gating": self.action_gating.statistics(),
            }
