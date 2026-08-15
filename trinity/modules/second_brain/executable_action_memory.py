"""
# status: orphan (2026-08-15 audit, not in runtime path)
EAM — Executable Action Memory via DFS Compression & Q-Guided MCTS Graph Search (ICML 2026).

三元语: 可执行行动记忆——以窗口/控件/输入/实体为节点的结构化知识图谱, 通过
状态感知 DFS 压缩多步操作例程, 并以 Q 函数引导蒙特卡洛树搜索进行图检索与执行规划。

设计要点:
  - ExecutableActionMemoryKG: 结构化 KG, 节点类型包含 Window/Control/Input/Entity,
    边类型含时序/因果/层次关系, 提供 CRUD 与图遍历接口。
  - StateAwareDFSCompressor: 状态感知 DFS 压缩器, 将多步操作例程压缩为可复用宏,
    支持分支检测与子序列去重。
  - QGuidedMCTSGraphSearch: Q 函数引导的 MCTS 图检索, 每步基于 UCT 选择并
    通过 Q 网络评估节点价值, 输出最优行动路径。
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class NodeType(Enum):
    """KG 节点类型。"""
    WINDOW = auto()     # 窗口节点
    CONTROL = auto()    # 控件节点
    INPUT = auto()      # 输入节点
    ENTITY = auto()     # 实体节点
    ROUTINE = auto()    # 例程序列节点 (压缩后)


class EdgeType(Enum):
    """KG 边类型。"""
    TEMPORAL = auto()       # 时序关系: A → B (A 在 B 之前)
    CAUSAL = auto()         # 因果关系: A 导致 B
    HIERARCHICAL = auto()   # 层次关系: A 包含 B
    DATA_FLOW = auto()      # 数据流: A 的输出流入 B


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class EAMNode:
    """EAM 知识图谱节点。"""
    node_id: str
    node_type: NodeType
    properties: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[np.ndarray] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class EAMEdge:
    """EAM 知识图谱边。"""
    source_id: str
    target_id: str
    edge_type: EdgeType
    weight: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EAMRoutine:
    """压缩后的可执行例程 (宏)。"""
    routine_id: str
    steps: List[str]             # 节点 ID 序列
    compressed_actions: List[Dict[str, Any]]   # 压缩后的动作描述
    frequency: int = 0           # 复用频率
    avg_reward: float = 0.0      # 平均奖励


@dataclass
class EAMSearchState:
    """MCTS 搜索状态节点。"""
    state_id: str
    node_ref: str                # EAMNode.node_id
    q_value: float = 0.0         # Q(s, a) 估计值
    visit_count: int = 0
    parent_id: Optional[str] = None
    children: List[str] = field(default_factory=list)
    is_terminal: bool = False


# ---------------------------------------------------------------------------
# Core Classes
# ---------------------------------------------------------------------------

class ExecutableActionMemoryKG:
    """可执行行动记忆知识图谱。

    以窗口/控件/输入/实体为节点, 时序/因果/层次关系为边, 存储 UI 交互
    知识, 并支持图遍历、子图查询与路径检索。

    Parameters
    ----------
    embedding_dim : int
        节点嵌入维度。
    """

    def __init__(self, embedding_dim: int = 256) -> None:
        self.embedding_dim = embedding_dim
        self._nodes: Dict[str, EAMNode] = {}
        self._edges: Dict[Tuple[str, str], EAMEdge] = {}
        self._lock = threading.RLock()
        logger.info("ExecutableActionMemoryKG initialized [dim=%d]", embedding_dim)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_node(self, node: EAMNode) -> str:
        """添加节点。"""
        with self._lock:
            self._nodes[node.node_id] = node
            return node.node_id

    def add_edge(self, edge: EAMEdge) -> None:
        """添加边。"""
        with self._lock:
            if edge.source_id not in self._nodes:
                self._nodes[edge.source_id] = EAMNode(
                    node_id=edge.source_id, node_type=NodeType.ENTITY
                )
            if edge.target_id not in self._nodes:
                self._nodes[edge.target_id] = EAMNode(
                    node_id=edge.target_id, node_type=NodeType.ENTITY
                )
            self._edges[(edge.source_id, edge.target_id)] = edge

    def get_neighbors(
        self,
        node_id: str,
        edge_type: Optional[EdgeType] = None,
    ) -> List[Tuple[str, EAMEdge]]:
        """获取节点的邻居列表。"""
        with self._lock:
            neighbors = []
            for (src, tgt), edge in self._edges.items():
                if src == node_id:
                    if edge_type is None or edge.edge_type == edge_type:
                        neighbors.append((tgt, edge))
                elif tgt == node_id:
                    if edge_type is None or edge.edge_type == edge_type:
                        neighbors.append((src, edge))
            return neighbors

    def get_paths(
        self,
        start_id: str,
        end_id: str,
        max_depth: int = 10,
    ) -> List[List[str]]:
        """BFS 搜索 start → end 的所有路径 (限制深度)。"""
        with self._lock:
            if start_id not in self._nodes or end_id not in self._nodes:
                return []

            all_paths: List[List[str]] = []
            queue: List[Tuple[str, List[str]]] = [(start_id, [start_id])]
            visited_depth: Dict[str, int] = {}

            while queue:
                current, path = queue.pop(0)
                if len(path) > max_depth:
                    continue
                if current == end_id:
                    all_paths.append(path)
                    continue
                if visited_depth.get(current, 999) <= len(path):
                    continue
                visited_depth[current] = len(path)

                for neighbor, _ in self.get_neighbors(current):
                    if neighbor not in path:
                        queue.append((neighbor, path + [neighbor]))

            return all_paths

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            type_counts: Dict[str, int] = {}
            for n in self._nodes.values():
                t = n.node_type.name
                type_counts[t] = type_counts.get(t, 0) + 1
            return {
                "total_nodes": len(self._nodes),
                "total_edges": len(self._edges),
                "node_type_distribution": type_counts,
            }


class StateAwareDFSCompressor:
    """状态感知 DFS 压缩器。

    对多步操作例程执行深度优先遍历压缩: 检测重复子序列 (loop/subroutine),
    合并为宏 (EAMRoutine), 保留分支结构, 并输出压缩后的可复用例程。

    Parameters
    ----------
    kg : ExecutableActionMemoryKG
        关联的知识图谱。
    min_subsequence_length : int
        最小子序列长度 (小于此值不压缩)。
    max_routine_length : int
        最大例程长度。
    """

    def __init__(
        self,
        kg: ExecutableActionMemoryKG,
        min_subsequence_length: int = 3,
        max_routine_length: int = 50,
    ) -> None:
        self.kg = kg
        self.min_subsequence_length = min_subsequence_length
        self.max_routine_length = max_routine_length
        self._routines: Dict[str, EAMRoutine] = {}
        self._lock = threading.RLock()
        self._compress_count: int = 0
        logger.info("StateAwareDFSCompressor initialized [min_len=%d]", min_subsequence_length)

    def compress(
        self,
        action_sequence: List[str],
        state_context: Optional[Dict[str, Any]] = None,
    ) -> EAMRoutine:
        """压缩多步操作例程。

        Parameters
        ----------
        action_sequence : List[str]
            原始动作节点 ID 序列。
        state_context : Optional[Dict[str, Any]]
            当前状态上下文, 用于状态感知分支。

        Returns
        -------
        EAMRoutine
            压缩后的可执行例程。
        """
        with self._lock:
            steps = action_sequence[:self.max_routine_length]
            compressed: List[Dict[str, Any]] = []
            i = 0

            while i < len(steps):
                # 检测重复子序列
                best_len = 0
                best_repeat = 0
                for sub_len in range(self.min_subsequence_length, len(steps) - i + 1):
                    sub = tuple(steps[i : i + sub_len])
                    count = 1
                    j = i + sub_len
                    while j + sub_len <= len(steps) and tuple(steps[j : j + sub_len]) == sub:
                        count += 1
                        j += sub_len
                    if count >= 2 and sub_len * count > best_len * best_repeat:
                        best_len = sub_len
                        best_repeat = count

                if best_len >= self.min_subsequence_length and best_repeat >= 2:
                    # 压缩为循环宏
                    sub_ids = steps[i : i + best_len]
                    compressed.append({
                        "type": "loop",
                        "subroutine": sub_ids,
                        "repeat": best_repeat,
                    })
                    i += best_len * best_repeat
                else:
                    node = self.kg._nodes.get(steps[i])
                    compressed.append({
                        "type": "single",
                        "node_id": steps[i],
                        "node_type": node.node_type.name if node else "UNKNOWN",
                    })
                    i += 1

            # 生成例程 ID
            routine_id = f"routine_{self._compress_count}_{len(compressed)}"
            routine = EAMRoutine(
                routine_id=routine_id,
                steps=steps,
                compressed_actions=compressed,
                frequency=1,
            )
            self._routines[routine_id] = routine
            self._compress_count += 1
            return routine

    def decompress(self, routine: EAMRoutine) -> List[str]:
        """将压缩例程展开为原始动作序列。"""
        result: List[str] = []
        for action in routine.compressed_actions:
            if action["type"] == "loop":
                result.extend(action["subroutine"] * action["repeat"])
            else:
                result.append(action["node_id"])
        return result

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total_compressed = sum(
                len(r.compressed_actions) for r in self._routines.values()
            )
            avg_compression = (
                sum(len(r.steps) / max(len(r.compressed_actions), 1)
                    for r in self._routines.values()) / max(len(self._routines), 1)
            )
            return {
                "total_routines": len(self._routines),
                "total_compressed_actions": total_compressed,
                "avg_compression_ratio": avg_compression,
                "compress_count": self._compress_count,
            }


class QGuidedMCTSGraphSearch:
    """Q 函数引导的蒙特卡洛树搜索图检索器。

    在 ExecutableActionMemoryKG 上执行 MCTS: 选择阶段用 UCT 公式平衡
    探索-利用, 扩展阶段从 KG 邻居采样, 模拟阶段用 Q 网络评估叶节点,
    回传阶段更新路径上所有节点的 Q 值与访问计数。

    Parameters
    ----------
    kg : ExecutableActionMemoryKG
        知识图谱实例。
    exploration_constant : float
        UCT 探索常数 C。
    max_iterations : int
        最大 MCTS 迭代次数。
    max_depth : int
        最大搜索深度。
    """

    def __init__(
        self,
        kg: ExecutableActionMemoryKG,
        exploration_constant: float = math.sqrt(2),
        max_iterations: int = 200,
        max_depth: int = 20,
    ) -> None:
        self.kg = kg
        self.exploration_constant = exploration_constant
        self.max_iterations = max_iterations
        self.max_depth = max_depth
        self._lock = threading.RLock()
        self._search_count: int = 0
        self._states: Dict[str, EAMSearchState] = {}
        logger.info("QGuidedMCTSGraphSearch initialized [C=%.3f iters=%d]", exploration_constant, max_iterations)

    def search(
        self,
        start_node_id: str,
        goal_node_ids: Optional[List[str]] = None,
        q_estimator: Optional[Any] = None,
    ) -> Tuple[List[str], float]:
        """执行 MCTS 图检索。

        Parameters
        ----------
        start_node_id : str
            起始节点 ID。
        goal_node_ids : Optional[List[str]]
            目标节点 ID 列表 (可空, 则最大化 Q 值)。
        q_estimator : Optional[Any]
            Q 函数近似器 (须提供 estimate(node_id, action_id) -> float)。

        Returns
        -------
        Tuple[List[str], float]
            (最优路径节点 ID 序列, 期望 Q 值)。
        """
        with self._lock:
            goals = set(goal_node_ids or [])
            root_state = self._get_or_create_state(start_node_id)
            root_state.visit_count = 1

            for iteration in range(self.max_iterations):
                # 1. Selection
                path, leaf = self._select(root_state, depth=0)

                # 2. Expansion
                if not leaf.is_terminal and leaf.visit_count > 1:
                    children = self._expand(leaf)
                    if children:
                        leaf = random.choice(children)

                # 3. Simulation
                reward = self._simulate(leaf, goals, q_estimator)

                # 4. Backpropagation
                self._backpropagate(path, leaf, reward)

            # 选择最优子节点
            best_child = self._best_child(root_state)
            return self._extract_path(root_state, best_child)

    # ------------------------------------------------------------------
    # MCTS 四阶段
    # ------------------------------------------------------------------

    def _select(
        self, state: EAMSearchState, depth: int
    ) -> Tuple[List[EAMSearchState], EAMSearchState]:
        """UCT 选择: 从根向下选择直到叶节点。"""
        path = [state]
        current = state
        while current.children and depth < self.max_depth:
            best = self._uct_select(current)
            if best is None:
                break
            current = best
            path.append(current)
            depth += 1
        return path, current

    def _uct_select(self, state: EAMSearchState) -> Optional[EAMSearchState]:
        """用 UCT 公式选择最佳子节点。"""
        best_state: Optional[EAMSearchState] = None
        best_value = -float("inf")
        for child_id in state.children:
            child = self._states.get(child_id)
            if child is None:
                continue
            if child.visit_count == 0:
                uct = float("inf")
            else:
                exploitation = child.q_value / child.visit_count
                exploration = self.exploration_constant * math.sqrt(
                    math.log(max(state.visit_count, 1)) / child.visit_count
                )
                uct = exploitation + exploration
            if uct > best_value:
                best_value = uct
                best_state = child
        return best_state

    def _expand(self, state: EAMSearchState) -> List[EAMSearchState]:
        """扩展: 从 KG 邻居生成子状态。"""
        new_states: List[EAMSearchState] = []
        neighbors = self.kg.get_neighbors(state.node_ref)
        for neighbor_id, edge in neighbors:
            child = self._get_or_create_state(neighbor_id, parent_id=state.state_id)
            if child.state_id not in state.children:
                state.children.append(child.state_id)
                new_states.append(child)
        return new_states

    def _simulate(
        self,
        state: EAMSearchState,
        goals: Set[str],
        q_estimator: Optional[Any],
    ) -> float:
        """模拟: 评估叶节点价值。"""
        if state.node_ref in goals:
            state.is_terminal = True
            return 1.0

        if q_estimator is not None:
            return float(q_estimator.estimate(state.node_ref, "") or 0.0)

        # 默认回退: 邻居数量归一化
        neighbors = self.kg.get_neighbors(state.node_ref)
        return min(1.0, max(-1.0, len(neighbors) / 50.0 * 0.5))

    def _backpropagate(
        self,
        path: List[EAMSearchState],
        leaf: EAMSearchState,
        reward: float,
    ) -> None:
        """回传: 更新路径上所有状态的 Q 值和访问计数。"""
        for state in reversed(path):
            state.visit_count += 1
            state.q_value += reward
            reward *= 0.95  # 折扣因子

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_or_create_state(
        self, node_id: str, parent_id: Optional[str] = None
    ) -> EAMSearchState:
        state_id = f"mcts_{node_id}_{hash(node_id) % (10**9)}"
        if state_id not in self._states:
            self._states[state_id] = EAMSearchState(
                state_id=state_id,
                node_ref=node_id,
                parent_id=parent_id,
            )
        return self._states[state_id]

    def _best_child(self, state: EAMSearchState) -> Optional[EAMSearchState]:
        """选择访问次数最多的子节点。"""
        if not state.children:
            return None
        return max(
            (self._states[c] for c in state.children if c in self._states),
            key=lambda s: s.visit_count,
            default=None,
        )

    def _extract_path(
        self, root: EAMSearchState, best: Optional[EAMSearchState]
    ) -> Tuple[List[str], float]:
        """从根到最佳子节点提取路径和期望 Q 值。"""
        if best is None:
            return ([root.node_ref], 0.0)

        path = [root.node_ref, best.node_ref]
        q_total = best.q_value / max(best.visit_count, 1)

        current = best
        depth = 1
        while current.children and depth < self.max_depth:
            next_child = self._best_child(current)
            if next_child is None:
                break
            path.append(next_child.node_ref)
            q_total = next_child.q_value / max(next_child.visit_count, 1)
            current = next_child
            depth += 1

        return (path, q_total)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_states": len(self._states),
                "search_count": self._search_count,
            }
