"""
# status: orphan (2026-08-15 audit, not in runtime path)
MAGEExecutionStateTree — MAGE Execution State Tree for Agent Trajectories
==========================================================================
arXiv 2606.06090 · P45-1

维护 agent 执行轨迹的 span-tree 结构, 每个 step(reasoning/tool_call/observation)
作为节点入树。支持子树剪枝、时间范围查询、meta-action 模式匹配与轨迹回放。

设计要点:
  - SpanNode: 执行轨迹节点, 含 step 类型/输入/输出/时间戳/子节点
  - MAGEExecutionStateTree: span-tree 创建、遍历、查询
  - MAGEDecoder: 从状态树解码执行上下文, 子树剪枝
  - MetaActionGraph: 跨步骤抽象操作序列, 模式匹配与回放
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class StepType(Enum):
    """执行步骤类型。"""
    REASONING = auto()
    TOOL_CALL = auto()
    OBSERVATION = auto()
    SUBTASK = auto()
    DECISION = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class SpanNode:
    """执行轨迹节点——树中的单个步骤节点。

    Attributes
    ----------
    node_id : str
    step_type : StepType
    input_data : 步骤输入（文本或结构化数据）
    output_data : 步骤输出
    children : 子节点列表
    start_time : 步骤开始时间
    end_time : 步骤结束时间
    metadata : 额外元数据
    """
    node_id: str
    step_type: StepType
    input_data: Any = None
    output_data: Any = None
    children: List[SpanNode] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    end_time: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def duration_ms(self) -> float:
        if self.end_time <= 0:
            return 0.0
        return (self.end_time - self.start_time) * 1000.0


@dataclass
class MAGE_ExecutionContext:
    """执行上下文——从状态树解码的当前上下文片段。"""
    active_path: List[str] = field(default_factory=list)  # node_ids
    recent_observations: List[str] = field(default_factory=list)
    pending_tool_calls: List[str] = field(default_factory=list)
    depth: int = 0
    total_steps: int = 0
    tree_summary: str = ""


# ---------------------------------------------------------------------------
# MAGEExecutionStateTree
# ---------------------------------------------------------------------------

class MAGEExecutionStateTree:
    """MAGE 执行状态树——维护 agent 执行轨迹的 span-tree。

    Parameters
    ----------
    max_depth : int
        树最大深度, 超出自动剪枝。
    """

    def __init__(self, max_depth: int = 20) -> None:
        self.max_depth = max_depth
        self._root: Optional[SpanNode] = None
        self._node_registry: Dict[str, SpanNode] = {}
        self._node_count: int = 0
        self._lock = threading.RLock()

    def add_root(self, step_type: StepType, input_data: Any = None) -> SpanNode:
        """创建根节点。"""
        with self._lock:
            self._node_count += 1
            self._root = SpanNode(
                node_id=f"root_{self._node_count}_{int(time.time()*1e6)}",
                step_type=step_type,
                input_data=input_data,
            )
            self._node_registry[self._root.node_id] = self._root
            return self._root

    def add_child(
        self, parent: SpanNode, step_type: StepType,
        input_data: Any = None, output_data: Any = None,
    ) -> SpanNode:
        """为父节点添加子节点。"""
        with self._lock:
            depth = self._depth_of(parent)
            if depth >= self.max_depth:
                logger.warning("Max depth %d reached, pruning at parent %s", self.max_depth, parent.node_id)
                return parent

            self._node_count += 1
            child = SpanNode(
                node_id=f"node_{self._node_count}_{int(time.time()*1e6)}",
                step_type=step_type,
                input_data=input_data,
                output_data=output_data,
            )
            parent.children.append(child)
            self._node_registry[child.node_id] = child
            return child

    def _depth_of(self, node: SpanNode) -> int:
        d = 0
        # BFS from root to find depth
        if self._root is None:
            return 0
        queue: deque = deque([(self._root, 0)])
        while queue:
            n, depth = queue.popleft()
            if n.node_id == node.node_id:
                return depth
            for c in n.children:
                queue.append((c, depth + 1))
        return 0

    def get_node(self, node_id: str) -> Optional[SpanNode]:
        return self._node_registry.get(node_id)

    def traverse_preorder(self) -> List[SpanNode]:
        """前序遍历。"""
        if self._root is None:
            return []
        result: List[SpanNode] = []
        stack = [self._root]
        while stack:
            node = stack.pop()
            result.append(node)
            stack.extend(reversed(node.children))
        return result

    def query_by_time(self, start: float, end: float) -> List[SpanNode]:
        """按时间范围查询节点。"""
        return [
            n for n in self._node_registry.values()
            if start <= n.start_time <= end
        ]

    def query_by_type(self, step_type: StepType) -> List[SpanNode]:
        """按步骤类型查询节点。"""
        return [
            n for n in self._node_registry.values()
            if n.step_type == step_type
        ]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_nodes": self._node_count,
                "max_depth": self.max_depth,
                "has_root": self._root is not None,
            }


# ---------------------------------------------------------------------------
# MAGEDecoder
# ---------------------------------------------------------------------------

class MAGEDecoder:
    """从状态树解码当前执行上下文。

    支持子树剪枝和按时间范围查询。
    """

    def __init__(self, prune_older_than_seconds: float = 300.0) -> None:
        self.prune_threshold = prune_older_than_seconds
        self._lock = threading.RLock()

    def decode(self, tree: MAGEExecutionStateTree, max_context_nodes: int = 50) -> MAGE_ExecutionContext:
        """解码当前执行上下文。"""
        with self._lock:
            now = time.time()
            ctx = MAGE_ExecutionContext()

            nodes = tree.traverse_preorder()
            ctx.total_steps = len(nodes)

            # 按时间排序, 取最近 max_context_nodes 个
            recent = sorted(nodes, key=lambda n: n.start_time, reverse=True)[:max_context_nodes]

            ctx.active_path = [n.node_id for n in recent]
            ctx.depth = len(ctx.active_path)

            for n in recent:
                if n.step_type == StepType.OBSERVATION and n.output_data:
                    ctx.recent_observations.append(str(n.output_data)[:200])
                if n.step_type == StepType.TOOL_CALL and n.input_data:
                    ctx.pending_tool_calls.append(str(n.input_data)[:100])

            ctx.tree_summary = (
                f"Tree: {tree.statistics()['total_nodes']} nodes, "
                f"active path depth {ctx.depth}"
            )
            return ctx

    def prune_subtree(self, tree: MAGEExecutionStateTree, node_id: str) -> int:
        """剪枝指定子树, 返回移除的节点数。"""
        with self._lock:
            node = tree.get_node(node_id)
            if node is None:
                return 0

            removed: List[str] = []
            stack: List[SpanNode] = [node]
            while stack:
                n = stack.pop()
                removed.append(n.node_id)
                stack.extend(n.children)

            for nid in removed:
                tree._node_registry.pop(nid, None)

            # 从父节点移除
            for n in tree._node_registry.values():
                n.children = [c for c in n.children if c.node_id not in removed]

            return len(removed)

    def prune_by_age(self, tree: MAGEExecutionStateTree) -> int:
        """按时间阈值剪枝旧节点。"""
        now = time.time()
        cutoff = now - self.prune_threshold
        old_nodes = [
            nid for nid, node in tree._node_registry.items()
            if node.start_time < cutoff and node.node_id != tree._root.node_id
        ]
        for nid in old_nodes:
            tree._node_registry.pop(nid, None)
        return len(old_nodes)

    def statistics(self) -> Dict[str, Any]:
        return {"prune_threshold_seconds": self.prune_threshold}


# ---------------------------------------------------------------------------
# MetaActionGraph
# ---------------------------------------------------------------------------

class MetaActionGraph:
    """Meta-Action Graph——跨步骤的抽象操作序列, 支持模式匹配与轨迹回放。

    从执行轨迹中提取高层抽象操作 (meta-actions), 构建可匹配的回放图。
    """

    def __init__(self) -> None:
        self._meta_actions: List[Dict[str, Any]] = []
        self._patterns: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    def extract(self, tree: MAGEExecutionStateTree) -> List[Dict[str, Any]]:
        """从执行轨迹树中提取 meta-actions。"""
        with self._lock:
            nodes = tree.traverse_preorder()
            meta_actions: List[Dict[str, Any]] = []

            # 连接 REASONING → TOOL_CALL → OBSERVATION 为 meta-action 三元组
            i = 0
            while i < len(nodes) - 2:
                a, b, c = nodes[i], nodes[i + 1], nodes[i + 2]
                if (
                    a.step_type == StepType.REASONING
                    and b.step_type == StepType.TOOL_CALL
                    and c.step_type == StepType.OBSERVATION
                ):
                    meta = {
                        "action_id": f"meta_{len(meta_actions)}_{int(time.time()*1e6)}",
                        "reasoning": str(a.output_data)[:200],
                        "tool_call": str(b.input_data)[:200],
                        "observation": str(c.output_data)[:200],
                        "duration_ms": (
                            c.start_time - a.start_time
                        ) * 1000,
                        "timestamp": a.start_time,
                    }
                    meta_actions.append(meta)
                    i += 3
                else:
                    i += 1

            self._meta_actions.extend(meta_actions)
            return meta_actions

    def match_pattern(self, pattern: Dict[str, str]) -> List[Dict[str, Any]]:
        """模式匹配——在历史 meta-actions 中查找匹配模式。"""
        matches = []
        for ma in self._meta_actions:
            score = 0
            for key, value in pattern.items():
                if key in ma and value.lower() in str(ma[key]).lower():
                    score += 1
            if score >= len(pattern) * 0.5:
                matches.append({**ma, "match_score": score})
        return sorted(matches, key=lambda m: m["match_score"], reverse=True)

    def replay(self, action_ids: List[str]) -> List[Dict[str, Any]]:
        """回放指定的 meta-actions——返回操作序列摘要。"""
        return [
            ma for ma in self._meta_actions
            if ma["action_id"] in action_ids
        ]

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_meta_actions": len(self._meta_actions),
            "patterns_registered": len(self._patterns),
        }
