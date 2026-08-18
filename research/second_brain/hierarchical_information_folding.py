"""
# status: orphan (2026-08-15 audit, not in runtime path)
HIPIF — Hierarchical Information Folding for Long-Context Agents
=================================================================
arXiv 2606.10507 · P37-3

三元语: 层次信息折叠——
将复杂任务分解为子目标层次树,
已完成子目标的历史折叠为紧凑摘要,
以子目标导向过程奖励引导行为,
消除长上下文中已完成步骤的冗余干扰。

设计要点:
  - HierarchicalInformationFolding: 主控制器, 协调分解/折叠/奖励三算子,
    维护子目标树状态。
  - SubgoalDecomposer: 将任务描述递归分解为子目标树,
    深度可配置, 叶子节点为可执行步骤。
  - HistoryFolder: 检测已完成子目标, 将其交互历史
    折叠为结构化摘要 (目标、步骤数、关键决策、结果)。
  - ProcessRewardGuider: 为每个子目标计算过程奖励,
    引导智能体优先完成高价值子目标。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class SubgoalStatus(Enum):
    """子目标状态。"""
    PENDING = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    FOLDED = auto()
    FAILED = auto()


class FoldStrategy(Enum):
    """折叠策略。"""
    SUMMARY_ONLY = auto()          # 仅摘要
    KEYSTEPS = auto()              # 保留关键步骤
    FULL_COMPRESSION = auto()      # 完全压缩为嵌入
    HYBRID = auto()                # 摘要 + 关键步骤


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class SubgoalNode:
    """子目标树节点。"""
    node_id: str
    description: str
    depth: int
    parent_id: Optional[str]
    children: List[str] = field(default_factory=list)
    status: SubgoalStatus = SubgoalStatus.PENDING
    estimated_steps: int = 1
    completed_steps: int = 0
    reward_signal: float = 0.0
    created_at: float = field(default_factory=time.time)


@dataclass
class FoldedSummary:
    """折叠后的历史摘要。"""
    summary_id: str
    subgoal_id: str
    subgoal_description: str
    total_steps: int
    key_decisions: List[str]
    outcome: str                  # "success" / "partial" / "failure"
    compressed_context: str       # 替代原始历史的压缩文本
    fold_timestamp: float = field(default_factory=time.time)


@dataclass
class RewardSignal:
    """过程奖励信号。"""
    subgoal_id: str
    progress: float               # [0, 1], 已完成步数/预估步数
    difficulty: float             # 子目标难度预估
    value: float                  # 综合奖励值
    guidance: str                 # 引导建议


# ============================================================================
# Core Class 1: SubgoalDecomposer
# ============================================================================

class SubgoalDecomposer:
    """子目标分解器。

    将任务描述递归分解为子目标树, 深度可配置。

    Parameters
    ----------
    max_depth : int
        最大递归深度。
    max_children : int
        每层最大子节点数。
    """

    def __init__(
        self,
        max_depth: int = 4,
        max_children: int = 8,
    ) -> None:
        self.max_depth = max_depth
        self.max_children = max_children
        self._lock = threading.RLock()
        self._counter: int = 0
        logger.info("SubgoalDecomposer initialized [depth=%d children=%d]", max_depth, max_children)

    def decompose(self, task_description: str) -> Tuple[SubgoalNode, Dict[str, SubgoalNode]]:
        """递归分解任务为子目标树。

        Parameters
        ----------
        task_description : str
            任务描述。

        Returns
        -------
        Tuple[SubgoalNode, Dict[str, SubgoalNode]]
            (根节点, {node_id → 节点} 字典)。
        """
        with self._lock:
            self._counter += 1
            root = SubgoalNode(
                node_id=f"root_{self._counter}",
                description=task_description,
                depth=0,
                parent_id=None,
                estimated_steps=1,
            )
            nodes: Dict[str, SubgoalNode] = {root.node_id: root}

            self._recursive_decompose(root, nodes, depth=1)
            return root, nodes

    def _recursive_decompose(
        self,
        parent: SubgoalNode,
        nodes: Dict[str, SubgoalNode],
        depth: int,
    ) -> None:
        if depth > self.max_depth:
            return

        # 基于任务描述关键词生成子目标模板
        desc_lower = parent.description.lower()
        subtask_templates = self._generate_subtasks(desc_lower, depth)

        for template in subtask_templates[:self.max_children]:
            self._counter += 1
            child = SubgoalNode(
                node_id=f"sg_{self._counter}",
                description=template,
                depth=depth,
                parent_id=parent.node_id,
                estimated_steps=max(1, self.max_depth - depth + 1),
            )
            parent.children.append(child.node_id)
            nodes[child.node_id] = child

            # 叶子层 (depth==max_depth) 不再分解
            if depth < self.max_depth:
                self._recursive_decompose(child, nodes, depth + 1)

    def _generate_subtasks(self, desc: str, depth: int) -> List[str]:
        """基于任务描述生成子目标模板。"""
        if depth == 1:
            return ["Analyze & understand requirements", "Gather necessary data & tools",
                    "Execute core operations", "Validate & verify results",
                    "Report & finalize"]
        elif depth == 2:
            return ["Identify key entities", "Check preconditions",
                    "Perform main action", "Handle edge cases",
                    "Verify output correctness"]
        else:
            return ["Extract relevant info", "Apply transformation",
                    "Cross-check with constraints", "Format output"]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"total_nodes_created": self._counter, "max_depth": self.max_depth}


# ============================================================================
# Core Class 2: HistoryFolder
# ============================================================================

class HistoryFolder:
    """历史折叠器。

    检测已完成子目标, 将其交互历史折叠为紧凑摘要。

    Parameters
    ----------
    strategy : FoldStrategy
        折叠策略。
    max_keysteps : int
        KEYSTEPS 模式下保留的最大步骤数。
    """

    def __init__(
        self,
        strategy: FoldStrategy = FoldStrategy.HYBRID,
        max_keysteps: int = 5,
    ) -> None:
        self.strategy = strategy
        self.max_keysteps = max_keysteps
        self._folded: Dict[str, FoldedSummary] = {}
        self._lock = threading.RLock()
        self._fold_counter: int = 0
        logger.info("HistoryFolder initialized [strategy=%s max_ks=%d]", strategy.name, max_keysteps)

    def fold(
        self,
        subgoal: SubgoalNode,
        step_history: List[str],
        key_decisions: Optional[List[str]] = None,
        outcome: str = "success",
    ) -> FoldedSummary:
        """将已完成子目标的历史折叠为摘要。

        Parameters
        ----------
        subgoal : SubgoalNode
            已完成的子目标节点。
        step_history : List[str]
            子目标执行过程中的步骤记录。
        key_decisions : Optional[List[str]]
            关键决策点。
        outcome : str
            执行结果。

        Returns
        -------
        FoldedSummary
            折叠后的摘要。
        """
        with self._lock:
            self._fold_counter += 1
            decisions = key_decisions or []

            # 根据策略生成压缩上下文
            if self.strategy == FoldStrategy.SUMMARY_ONLY:
                compressed = f"[FOLDED] {subgoal.description}: {outcome} in {len(step_history)} steps"
            elif self.strategy == FoldStrategy.KEYSTEPS:
                keysteps = step_history[:self.max_keysteps]
                compressed = f"[FOLDED] {subgoal.description} → {' → '.join(keysteps)}"
            elif self.strategy == FoldStrategy.FULL_COMPRESSION:
                compressed = f"[FOLDED|EMBED] {subgoal.description} [{outcome}]"
            else:  # HYBRID
                summary_line = f"[FOLDED] {subgoal.description}: {outcome} ({len(step_history)} steps)"
                if decisions:
                    summary_line += f" | Key: {'; '.join(decisions[:3])}"
                compressed = summary_line

            summary = FoldedSummary(
                summary_id=f"fold_{self._fold_counter}",
                subgoal_id=subgoal.node_id,
                subgoal_description=subgoal.description,
                total_steps=len(step_history),
                key_decisions=decisions,
                outcome=outcome,
                compressed_context=compressed,
            )
            self._folded[subgoal.node_id] = summary
            return summary

    def get_folded_context(self, subgoal_id: str) -> Optional[str]:
        with self._lock:
            summary = self._folded.get(subgoal_id)
            return summary.compressed_context if summary else None

    def get_all_folded_contexts(self) -> List[str]:
        with self._lock:
            return [s.compressed_context for s in self._folded.values()]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"folded_count": len(self._folded), "strategy": self.strategy.name}


# ============================================================================
# Core Class 3: ProcessRewardGuider
# ============================================================================

class ProcessRewardGuider:
    """子目标导向过程奖励引导器。

    为每个子目标计算过程奖励, 引导优先级排序。

    Parameters
    ----------
    difficulty_weight : float
        难度加权因子。
    progress_weight : float
        进度加权因子。
    """

    def __init__(
        self,
        difficulty_weight: float = 0.4,
        progress_weight: float = 0.6,
    ) -> None:
        self.difficulty_weight = difficulty_weight
        self.progress_weight = progress_weight
        self._lock = threading.RLock()
        self._signals: List[RewardSignal] = []
        logger.info("ProcessRewardGuider initialized [diff=%.2f prog=%.2f]",
                    difficulty_weight, progress_weight)

    def compute_rewards(
        self,
        nodes: Dict[str, SubgoalNode],
    ) -> List[RewardSignal]:
        """为所有子目标计算过程奖励并排序。

        Parameters
        ----------
        nodes : Dict[str, SubgoalNode]
            子目标节点字典。

        Returns
        -------
        List[RewardSignal]
            按奖励值降序的奖励信号列表。
        """
        with self._lock:
            signals: List[RewardSignal] = []

            for node_id, node in nodes.items():
                if node.status in (SubgoalStatus.COMPLETED, SubgoalStatus.FOLDED, SubgoalStatus.FAILED):
                    continue

                # 进度: 已完成/预估
                progress = (
                    node.completed_steps / max(node.estimated_steps, 1)
                    if node.estimated_steps > 0
                    else 0.0
                )

                # 难度: 深度越深 → 难度越高, 但叶子节点实际简单
                depth_factor = min(node.depth / 4.0, 0.5) if node.children else 0.1
                difficulty = 0.5 + depth_factor

                # 综合奖励: 进度高 (快完成) 的优先, 难度适中的加分
                value = (
                    self.progress_weight * progress
                    + self.difficulty_weight * difficulty
                )

                # 引导建议
                if progress > 0.7:
                    guidance = f"Nearly done [{progress:.0%}], prioritize to complete"
                elif progress > 0.3:
                    guidance = f"In progress [{progress:.0%}], continue"
                else:
                    guidance = f"Start early — depth {node.depth}, est {node.estimated_steps} steps"

                signal = RewardSignal(
                    subgoal_id=node_id,
                    progress=progress,
                    difficulty=difficulty,
                    value=value,
                    guidance=guidance,
                )
                signals.append(signal)

            signals.sort(key=lambda s: s.value, reverse=True)
            self._signals = signals
            return signals

    def top_priority(self) -> Optional[RewardSignal]:
        """返回当前最高优先级的待完成子目标。"""
        with self._lock:
            if not self._signals:
                return None
            return self._signals[0]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "signals_generated": len(self._signals),
                "top_priority": self._signals[0].subgoal_id if self._signals else None,
            }


# ============================================================================
# Core Class 4: HierarchicalInformationFolding
# ============================================================================

class HierarchicalInformationFolding:
    """层次信息折叠主控制器。

    协调 SubgoalDecomposer / HistoryFolder / ProcessRewardGuider
    三算子, 维护子目标树状态与折叠上下文。

    Parameters
    ----------
    decomposer : SubgoalDecomposer
        子目标分解器。
    folder : HistoryFolder
        历史折叠器。
    guider : ProcessRewardGuider
        过程奖励引导器。
    """

    def __init__(
        self,
        decomposer: Optional[SubgoalDecomposer] = None,
        folder: Optional[HistoryFolder] = None,
        guider: Optional[ProcessRewardGuider] = None,
    ) -> None:
        self.decomposer = decomposer or SubgoalDecomposer()
        self.folder = folder or HistoryFolder()
        self.guider = guider or ProcessRewardGuider()

        self._root: Optional[SubgoalNode] = None
        self._nodes: Dict[str, SubgoalNode] = {}
        self._active_context: List[str] = []

        self._lock = threading.RLock()
        logger.info("HierarchicalInformationFolding initialized")

    def initialize_task(self, task_description: str) -> SubgoalNode:
        """初始化任务, 生成子目标树。

        Parameters
        ----------
        task_description : str
            任务描述。

        Returns
        -------
        SubgoalNode
            根节点。
        """
        with self._lock:
            self._root, self._nodes = self.decomposer.decompose(task_description)
            return self._root

    def mark_completed(
        self,
        node_id: str,
        step_history: List[str],
        key_decisions: Optional[List[str]] = None,
        outcome: str = "success",
    ) -> Optional[FoldedSummary]:
        """标记子目标完成并折叠历史。

        Parameters
        ----------
        node_id : str
            子目标节点 ID。
        step_history : List[str]
            步骤记录。
        key_decisions : Optional[List[str]]
            关键决策。
        outcome : str
            结果。

        Returns
        -------
        Optional[FoldedSummary]
            折叠摘要。
        """
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None:
                return None

            node.status = SubgoalStatus.COMPLETED
            node.completed_steps = len(step_history)

            summary = self.folder.fold(node, step_history, key_decisions, outcome)
            node.status = SubgoalStatus.FOLDED
            return summary

    def get_active_context(self) -> str:
        """获取当前活跃上下文 (未折叠子目标 + 已折叠摘要)。"""
        with self._lock:
            parts: List[str] = []

            # 未折叠的活跃子目标
            for node_id, node in self._nodes.items():
                if node.status in (SubgoalStatus.PENDING, SubgoalStatus.IN_PROGRESS):
                    parts.append(f"[ACTIVE] {node.description} (depth={node.depth})")

            # 已折叠摘要
            folded = self.folder.get_all_folded_contexts()
            parts.extend(folded)

            return "\n".join(parts)

    def get_priority_guidance(self) -> Optional[RewardSignal]:
        """获取当前优先级引导。"""
        with self._lock:
            signals = self.guider.compute_rewards(self._nodes)
            return signals[0] if signals else None

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total_nodes = len(self._nodes)
            folded = sum(1 for n in self._nodes.values() if n.status == SubgoalStatus.FOLDED)
            return {
                "total_subgoals": total_nodes,
                "folded": folded,
                "active": total_nodes - folded,
                "folder": self.folder.statistics(),
                "guider": self.guider.statistics(),
            }
