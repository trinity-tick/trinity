"""
CB63: HierarchicalNavigationRetrieval（HORMA）— 分层导航检索
============================================================

对标 arXiv 2606.11680。将记忆组织为文件系统式层级结构，构造模块迭代细化，
导航模块用轻量 RL 选择最小充分上下文。

设计要点：
  - structured_construction: 将记忆三元组构建为树节点（parent/children）
  - navigation_based_retrieval: 从根节点出发，RL 策略逐层选择最优子节点
  - 最小充分上下文：导航路径深度自适应，到达信息充分节点即停止
  - 与 ZeroLLMRetrieval (CB59) 配合：HORMA 做结构导航，ZLR 做无 LLM 检索
  - 轻量 RL：ε-greedy Q-learning，状态 = (node_id, query_hash)

Reference:
  - arXiv 2606.11680 "Hierarchical Navigation Retrieval for Agent Memory"
  - HORMA: Hierarchical ORganized Memory Architecture
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import math
import random
import threading
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class ConstructionPhase(Enum):
    """构造阶段。"""
    INGEST = "ingest"                # 摄入：原始记忆 → 三元组
    CLUSTER = "cluster"              # 聚类：三元组 → 分组
    HIERARCHIZE = "hierarchize"       # 分层：分组 → 树


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class MemoryTreeNode:
    """记忆树节点。

    Attributes:
        node_id: 唯一节点 ID（BFS 路径编码）。
        content: 节点内的记忆内容摘要。
        children: 子节点 ID 列表。
        depth: 深度（根=0）。
        memory_count: 本节点覆盖的记忆数量。
        relevance_score: 与查询的相关性得分（导航时填充）。
    """
    node_id: str
    content: str = ""
    children: List[str] = field(default_factory=list)
    depth: int = 0
    memory_count: int = 0
    relevance_score: float = 0.0


@dataclass
class NavigationContext:
    """导航上下文——一次检索的完整导航路径。

    Attributes:
        query: 检索查询。
        path: 从根到目标节点的 node_id 序列。
        depth_reached: 到达的深度。
        termination_reason: 导航停止原因。
    """
    query: str
    path: List[str] = field(default_factory=list)
    depth_reached: int = 0
    termination_reason: str = ""


@dataclass
class NavigationPolicy:
    """轻量 RL Q-learning 策略。

    Attributes:
        epsilon: 探索率。
        alpha: 学习率。
        gamma: 折扣因子。
        q_table: (state_key, action) → Q-value 映射。
    """
    epsilon: float = 0.15
    alpha: float = 0.1
    gamma: float = 0.9
    q_table: Dict[Tuple[str, str], float] = field(default_factory=dict)


@dataclass
class HORMAConfig:
    """HORMA 配置。

    Attributes:
        max_depth: 最大导航深度。
        min_relevance: 最小相关性阈值。
        branching_factor: 每层最大展开子节点数。
    """
    max_depth: int = 8
    min_relevance: float = 0.3
    branching_factor: int = 5


# ============================================================================
# Main Class
# ============================================================================

class HierarchicalNavigationRetrieval:
    """分层导航检索 (CB63)。

    两阶段流水线：
      1. structured_construction: 将记忆构建为层级树
      2. navigation_based_retrieval: RL 导航检索

    Usage:
        horma = HierarchicalNavigationRetrieval()
        horma.construct(memories=[("work", "parent1"), ("meeting", "work"), ...])
        result = horma.navigate("find meeting notes about budget")
    """

    def __init__(self, config: Optional[HORMAConfig] = None):
        self.config = config or HORMAConfig()
        self._lock = threading.RLock()
        self._nodes: Dict[str, MemoryTreeNode] = {}
        self._root_id: Optional[str] = None
        self._policy = NavigationPolicy()
        self._nav_count: int = 0
        self._construct_count: int = 0
        self._start_time: float = _time.time()

    # ------------------------------------------------------------------
    # Phase 1: Structured Construction
    # ------------------------------------------------------------------

    def construct(self, memories: List[Tuple[str, str]]):
        """构建层级树。

        Args:
            memories: [(content, parent_label), ...]
                parent_label 表示归属标签，空字符串或 None 为根级。
        """
        with self._lock:
            self._nodes.clear()
            self._root_id = "root"
            root = MemoryTreeNode(node_id=self._root_id, content="ROOT", depth=0)
            self._nodes[self._root_id] = root

            # 按 parent_label 分组
            groups: Dict[str, List[str]] = {}
            for idx, (content, parent) in enumerate(memories):
                label = parent if parent else "root"
                groups.setdefault(label, []).append(content)

            # 构建子节点
            for label, contents in groups.items():
                parent_id = self._root_id if label == "root" else f"node_{hash(label) & 0xFFFFF:05d}"
                if parent_id not in self._nodes:
                    self._nodes[parent_id] = MemoryTreeNode(
                        node_id=parent_id, content=label, depth=1, memory_count=0,
                    )
                    self._nodes[self._root_id].children.append(parent_id)

                for ci, content in enumerate(contents):
                    leaf_id = f"{parent_id}_leaf_{ci}"
                    leaf = MemoryTreeNode(
                        node_id=leaf_id,
                        content=content[:120],
                        depth=self._nodes[parent_id].depth + 1,
                        memory_count=1,
                    )
                    self._nodes[leaf_id] = leaf
                    self._nodes[parent_id].children.append(leaf_id)
                    self._nodes[parent_id].memory_count += 1

            # 递归更新 memory_count
            self._update_counts(self._root_id)
            self._construct_count += 1

    def _update_counts(self, node_id: str) -> int:
        """递归更新子树 memory_count。"""
        node = self._nodes[node_id]
        total = node.memory_count
        for child_id in node.children:
            total += self._update_counts(child_id)
        node.memory_count = total
        return total

    # ------------------------------------------------------------------
    # Phase 2: Navigation-Based Retrieval
    # ------------------------------------------------------------------

    def navigate(self, query: str) -> NavigationContext:
        """执行 RL 导航检索，返回最佳路径。

        Args:
            query: 自然语言查询。

        Returns:
            NavigationContext: 导航路径与结果。
        """
        with self._lock:
            if self._root_id is None:
                return NavigationContext(query=query, termination_reason="empty_tree")

            ctx = NavigationContext(query=query)
            current = self._root_id
            query_hash = hashlib.md5(query.encode()).hexdigest()[:8]

            for step in range(self.config.max_depth):
                ctx.path.append(current)
                ctx.depth_reached = step

                node = self._nodes[current]
                if not node.children:
                    ctx.termination_reason = "leaf_reached"
                    break

                # 评分 + 选择
                scored = self._score_children(current, query_hash)
                if not scored:
                    ctx.termination_reason = "no_children"
                    break

                # ε-greedy
                if random.random() < self._policy.epsilon:
                    chosen = random.choice(scored)
                else:
                    chosen = max(scored, key=lambda x: x[0])  # by score desc

                score, child_id = chosen[0], chosen[1]
                current = child_id

                # 奖励信号
                reward = score - self.config.min_relevance
                state_key = (node.node_id, query_hash)
                self._update_q(state_key, child_id, reward)

                if score < self.config.min_relevance:
                    ctx.termination_reason = "below_threshold"
                    break

            self._nav_count += 1
            return ctx

    def _score_children(
        self, node_id: str, query_hash: str
    ) -> List[Tuple[float, str]]:
        """为子节点评分（基于内容匹配 + Q 值）。"""
        node = self._nodes[node_id]
        scored = []
        for child_id in node.children[:self.config.branching_factor]:
            child = self._nodes[child_id]
            # 简单 TF 相似度（基于 shared tokens）
            content_score = self._token_overlap(
                child.content.lower(), child.node_id.lower()
            ) + 0.1 * math.log(child.memory_count + 1)
            content_score = min(1.0, content_score)

            q_val = self._policy.q_table.get(((node_id, query_hash), child_id), 0.5)
            combined = 0.6 * content_score + 0.4 * q_val
            scored.append((combined, child_id))
        return scored

    def _token_overlap(self, a: str, b: str) -> float:
        """简单 token 重叠率。"""
        tokens_a = set(a.split())
        tokens_b = set(b.split())
        if not tokens_a or not tokens_b:
            return 0.0
        return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)

    def _update_q(self, state_key: Tuple[str, str], action: str, reward: float):
        """Q-learning 更新。"""
        key = (state_key, action)
        old_q = self._policy.q_table.get(key, 0.5)
        # 简化：下一状态无后继则 max_next=0
        new_q = old_q + self._policy.alpha * (
            reward + self._policy.gamma * 0.0 - old_q
        )
        self._policy.q_table[key] = new_q

    def get_node(self, node_id: str) -> Optional[MemoryTreeNode]:
        """获取节点详情。"""
        with self._lock:
            return self._nodes.get(node_id)

    def tree_size(self) -> int:
        with self._lock:
            return len(self._nodes)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "class": "HierarchicalNavigationRetrieval (CB63)",
                "total_nodes": len(self._nodes),
                "total_navigations": self._nav_count,
                "total_constructions": self._construct_count,
                "config": {
                    "max_depth": self.config.max_depth,
                    "min_relevance": self.config.min_relevance,
                    "branching_factor": self.config.branching_factor,
                    "epsilon": self._policy.epsilon,
                },
                "q_table_size": len(self._policy.q_table),
                "uptime_seconds": round(_time.time() - self._start_time, 3),
            }
