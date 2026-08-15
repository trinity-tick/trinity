"""
# status: orphan (2026-08-15 audit, not in runtime path)
P11-3: Structured GUI State-Action Memory Graph (对标 EAM arXiv 2605.12294)
============================================================================

将 GUI 界面记忆为状态机，支持：
  - GUIStateMachine: 状态 / available_actions / valid_transitions
  - StateAwareDFSExplorer: 状态感知 DFS 探索策略，最小冗余覆盖
  - ActionGroupMiner: 压缩频繁多步例程为可复用高阶动作
  - ValueGuidedPathPlanner: 轻量 Q 函数引导的约束 KG 蒙卡树搜索

与 P10 graph_router.py 接口兼容：
  - 可被其 Planner/Executor/Summarizer 调用
  - 输出可执行路径供 router 决策使用

Reference:
  - EAM — Executable Agent Memory, arXiv:2605.12294
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import threading
import time
import uuid
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── 枚举 ────────────────────────────────────────────────────────────

class GUIStateType(Enum):
    """GUI 状态类型"""
    PAGE = "page"                 # 页面级别
    DIALOG = "dialog"             # 弹窗/对话框
    MENU = "menu"                 # 菜单项
    FORM = "form"                 # 表单
    SPLASH = "splash"             # 启动/加载页
    ERROR = "error"               # 错误状态
    UNKNOWN = "unknown"


class ActionType(Enum):
    """动作类型"""
    CLICK = "click"
    INPUT = "input"
    SCROLL = "scroll"
    SWIPE = "swipe"
    LONG_PRESS = "long_press"
    DRAG = "drag"
    KEY_PRESS = "key_press"
    WAIT = "wait"
    BACK = "back"


class ExplorationStrategy(Enum):
    """探索策略"""
    BFS = "bfs"                   # 广度优先
    DFS = "dfs"                   # 深度优先
    MCTS = "mcts"                 # 蒙卡树搜索
    HEURISTIC = "heuristic"       # 启发式


# ── 数据类 ──────────────────────────────────────────────────────────

@dataclass
class GUIState:
    """GUI 状态节点"""
    state_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state_type: GUIStateType = GUIStateType.PAGE
    name: str = ""                        # 状态描述名
    ui_elements: List[str] = field(default_factory=list)    # 可检测的 UI 元素 id
    screenshot_hash: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    last_visited: float = field(default_factory=time.time)
    visit_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state_id": self.state_id,
            "state_type": self.state_type.value,
            "name": self.name,
            "ui_elements": self.ui_elements,
            "metadata": self.metadata,
            "visit_count": self.visit_count,
        }


@dataclass
class GUIAction:
    """GUI 动作"""
    action_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    action_type: ActionType = ActionType.CLICK
    target_element: str = ""              # 目标 UI 元素 id
    params: Dict[str, Any] = field(default_factory=dict)   # 动作参数
    description: str = ""
    cost: float = 1.0                     # 执行代价（时间/资源）


@dataclass
class GUITransition:
    """状态转移"""
    transition_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    from_state: str = ""
    to_state: str = ""
    action: GUIAction = field(default_factory=GUIAction)
    success_rate: float = 1.0
    avg_latency_ms: float = 0.0
    execution_count: int = 0
    last_executed: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.from_state}->{self.to_state}:{self.action.action_id}"


@dataclass
class PathResult:
    """路径搜索结果"""
    path: List[GUITransition] = field(default_factory=list)
    total_cost: float = 0.0
    success: bool = False
    nodes_visited: int = 0
    search_strategy: ExplorationStrategy = ExplorationStrategy.DFS


@dataclass
class ActionGroup:
    """高阶动作组（频繁多步例程压缩结果）"""
    group_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    sub_actions: List[GUIAction] = field(default_factory=list)
    frequency: int = 0                    # 出现频率
    support: float = 0.0                  # 最小支持度
    avg_cost: float = 0.0
    description: str = ""


# ── GUIStateMachine ─────────────────────────────────────────────────

class GUIStateMachine:
    """GUI 状态机：将 GUI 界面记忆为状态与转移图。

    核心数据结构：
      - states: Dict[state_id, GUIState]
      - transitions: Dict[(from, to, action_id), GUITransition]
      - adjacency: Dict[state_id, List[GUITransition]] 邻接表
    """

    def __init__(self, name: str = "default"):
        self.name = name
        self.states: Dict[str, GUIState] = {}
        self.transitions: Dict[str, GUITransition] = {}
        self.adjacency: Dict[str, List[str]] = defaultdict(list)  # state_id -> [transition_id]
        self._lock = threading.RLock()
        logger.info(f"[GUIStateMachine:{name}] Initialized")

    def add_state(self, state: GUIState) -> GUIState:
        """添加或更新状态。"""
        with self._lock:
            self.states[state.state_id] = state
            if state.state_id not in self.adjacency:
                self.adjacency[state.state_id] = []
            return state

    def add_transition(self, transition: GUITransition) -> GUITransition:
        """添加转移边。"""
        with self._lock:
            self.transitions[transition.transition_id] = transition
            if transition.from_state not in self.adjacency:
                self.adjacency[transition.from_state] = []
            self.adjacency[transition.from_state].append(transition.transition_id)
            return transition

    def get_available_actions(self, state_id: str) -> List[GUIAction]:
        """获取某状态下的可用动作列表。"""
        with self._lock:
            if state_id not in self.adjacency:
                return []
            actions = []
            for tid in self.adjacency[state_id]:
                t = self.transitions.get(tid)
                if t:
                    actions.append(t.action)
            return actions

    def get_valid_transitions(self, state_id: str) -> List[GUITransition]:
        """获取某状态下的所有有效转移。"""
        with self._lock:
            if state_id not in self.adjacency:
                return []
            return [
                self.transitions[tid]
                for tid in self.adjacency[state_id]
                if tid in self.transitions
            ]

    def get_state(self, state_id: str) -> Optional[GUIState]:
        return self.states.get(state_id)

    def state_count(self) -> int:
        return len(self.states)

    def transition_count(self) -> int:
        return len(self.transitions)

    def to_graph_dict(self) -> Dict[str, Any]:
        """导出为图表示（兼容 graph_router 消费）。"""
        with self._lock:
            nodes = [
                {
                    "id": s.state_id,
                    "type": "gui_state",
                    "label": s.name,
                    "state_type": s.state_type.value,
                    "ui_elements": s.ui_elements,
                    "visit_count": s.visit_count,
                }
                for s in self.states.values()
            ]
            edges = [
                {
                    "id": t.transition_id,
                    "from": t.from_state,
                    "to": t.to_state,
                    "action": t.action.action_type.value,
                    "target": t.action.target_element,
                    "cost": t.action.cost,
                    "success_rate": t.success_rate,
                }
                for t in self.transitions.values()
            ]
            return {"name": self.name, "nodes": nodes, "edges": edges}

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "states": len(self.states),
                "transitions": len(self.transitions),
                "state_types": {
                    t.value: len([s for s in self.states.values() if s.state_type == t])
                    for t in GUIStateType
                },
            }


# ── StateAwareDFSExplorer ───────────────────────────────────────────

class StateAwareDFSExplorer:
    """状态感知 DFS 探索策略：最小冗余覆盖任务相关转移。

    核心思想：
      - 以当前状态为根，DFS 探索可达状态空间
      - 使用 visited 集合避免重复探索
      - 支持深度限制和转移预算
      - 覆盖与 task_keywords 匹配的状态节点
    """

    def __init__(self, max_depth: int = 10, max_transitions: int = 100):
        self.max_depth = max_depth
        self.max_transitions = max_transitions
        logger.info(
            f"[StateAwareDFSExplorer] Initialized "
            f"(max_depth={max_depth}, max_transitions={max_transitions})"
        )

    def explore(
        self,
        state_machine: GUIStateMachine,
        start_state_id: str,
        task_keywords: Optional[List[str]] = None,
    ) -> PathResult:
        """从 start_state 出发执行 DFS 探索。

        Args:
            state_machine: 状态机
            start_state_id: 起始状态 ID
            task_keywords: 任务关键词，用于筛选相关状态

        Returns:
            PathResult 包含探索路径和覆盖统计
        """
        visited: Set[str] = set()
        path: List[GUITransition] = []
        nodes_visited = 0

        def _dfs(state_id: str, depth: int) -> bool:
            nonlocal nodes_visited

            if depth > self.max_depth or nodes_visited >= self.max_transitions:
                return False

            visited.add(state_id)
            nodes_visited += 1

            state = state_machine.get_state(state_id)
            if state and task_keywords:
                # 检查是否匹配任务关键词
                if self._state_matches(state, task_keywords):
                    return True

            transitions = state_machine.get_valid_transitions(state_id)
            for t in transitions:
                if t.to_state in visited:
                    continue
                path.append(t)
                if _dfs(t.to_state, depth + 1):
                    return True
                path.pop()

            return False

        found = _dfs(start_state_id, 0)
        total_cost = sum(t.action.cost for t in path)

        return PathResult(
            path=path,
            total_cost=total_cost,
            success=found,
            nodes_visited=nodes_visited,
            search_strategy=ExplorationStrategy.DFS,
        )

    @staticmethod
    def _state_matches(state: GUIState, keywords: List[str]) -> bool:
        """检查状态是否匹配关键词。"""
        text = (state.name + " " + " ".join(state.ui_elements)).lower()
        return any(kw.lower() in text for kw in keywords)

    def statistics(self) -> Dict[str, Any]:
        return {
            "max_depth": self.max_depth,
            "max_transitions": self.max_transitions,
        }


# ── ActionGroupMiner ────────────────────────────────────────────────

class ActionGroupMiner:
    """动作组挖掘器：压缩频繁多步例程为可复用高阶动作。

    使用滑动窗口 + 频率计数识别频繁子序列，
    将满足 min_support 的多步序列压缩为 ActionGroup。
    """

    def __init__(self, min_support: float = 0.1, window_size: int = 5):
        self.min_support = min_support
        self.window_size = window_size
        self._action_groups: Dict[str, ActionGroup] = {}
        self._execution_history: List[GUIAction] = []
        self._lock = threading.RLock()
        logger.info(
            f"[ActionGroupMiner] Initialized "
            f"(min_support={min_support}, window={window_size})"
        )

    def record_execution(self, action: GUIAction) -> None:
        """记录一次动作执行。"""
        with self._lock:
            self._execution_history.append(action)

    def record_sequence(self, actions: List[GUIAction]) -> None:
        """记录动作序列。"""
        with self._lock:
            self._execution_history.extend(actions)

    def mine(self) -> List[ActionGroup]:
        """执行挖掘，返回频繁动作组。

        使用滑动窗口提取子序列，计数后按 min_support 筛选。
        """
        with self._lock:
            if len(self._execution_history) < self.window_size:
                return []

            patterns: Dict[str, Tuple[List[GUIAction], int]] = {}
            total_windows = 0

            for i in range(len(self._execution_history) - self.window_size + 1):
                window = self._execution_history[i : i + self.window_size]
                total_windows += 1

                # 所有长度 >= 2 的子序列
                for length in range(2, self.window_size + 1):
                    for start in range(self.window_size - length + 1):
                        subseq = window[start : start + length]
                        key = "|".join(a.action_id for a in subseq)
                        if key not in patterns:
                            patterns[key] = (subseq[:], 0)
                        count_tuple = patterns[key]
                        patterns[key] = (count_tuple[0], count_tuple[1] + 1)

            results: List[ActionGroup] = []
            for key, (subseq, count) in patterns.items():
                support = count / max(total_windows, 1)
                if support >= self.min_support:
                    group = ActionGroup(
                        name=f"group_{key[:8]}",
                        sub_actions=subseq,
                        frequency=count,
                        support=support,
                        avg_cost=sum(a.cost for a in subseq),
                        description=f"Frequent {len(subseq)}-step routine (support={support:.2f})",
                    )
                    results.append(group)
                    self._action_groups[group.group_id] = group

            # 按频率降序
            results.sort(key=lambda g: g.frequency, reverse=True)
            logger.info(
                f"[ActionGroupMiner] Mined {len(results)} groups "
                f"from {total_windows} windows"
            )
            return results

    def get_group(self, group_id: str) -> Optional[ActionGroup]:
        return self._action_groups.get(group_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "history_length": len(self._execution_history),
                "groups_mined": len(self._action_groups),
                "min_support": self.min_support,
                "window_size": self.window_size,
            }


# ── ValueGuidedPathPlanner ──────────────────────────────────────────

class ValueGuidedPathPlanner:
    """价值引导路径规划器：轻量 Q 函数引导的约束 KG 蒙卡树搜索。

    对标 EAM 的 ValueGuidedMCTS：
      - 在状态机图上执行 MCTS
      - Q 值估计转移成功率 + 代价
      - UCB1 探索-利用平衡
      - 输出可执行路径（供 graph_router 消费）

    与 P10 graph_router.py 接口兼容：
      - 输出路径可被 Planner 路由到 Executor
      - 状态信息可被 Summarizer 消费
    """

    def __init__(
        self,
        exploration_weight: float = 1.414,
        max_iterations: int = 200,
        max_depth: int = 15,
        discount: float = 0.95,
    ):
        self.exploration_weight = exploration_weight
        self.max_iterations = max_iterations
        self.max_depth = max_depth
        self.discount = discount
        logger.info(
            f"[ValueGuidedPathPlanner] Initialized "
            f"(c={exploration_weight}, iterations={max_iterations}, "
            f"depth={max_depth})"
        )

    def plan(
        self,
        state_machine: GUIStateMachine,
        start_state_id: str,
        goal_keywords: Optional[List[str]] = None,
        cost_budget: float = float("inf"),
    ) -> PathResult:
        """在状态机图上执行 MCTS 规划。

        Args:
            state_machine: 状态机
            start_state_id: 起始状态
            goal_keywords: 目标状态关键词
            cost_budget: 代价预算上限

        Returns:
            PathResult 包含最优路径
        """
        # MCTS 内部节点
        class _MCTSNode:
            def __init__(self, state_id: str, transition: Optional[GUITransition] = None, parent=None):
                self.state_id = state_id
                self.transition = transition
                self.parent = parent
                self.children: List["_MCTSNode"] = []
                self.visits = 0
                self.value = 0.0
                self.untried_transitions: List[GUITransition] = []

        root = _MCTSNode(start_state_id)
        root.untried_transitions = state_machine.get_valid_transitions(start_state_id)

        def _is_goal(state_id: str) -> bool:
            if not goal_keywords:
                return False
            state = state_machine.get_state(state_id)
            if state is None:
                return False
            text = (state.name + " " + " ".join(state.ui_elements)).lower()
            return any(kw.lower() in text for kw in goal_keywords)

        def _rollout(state_id: str, depth: int, cost_so_far: float) -> float:
            """随机模拟 rollout。"""
            if depth >= self.max_depth or cost_so_far >= cost_budget:
                return 0.0
            transitions = state_machine.get_valid_transitions(state_id)
            if not transitions:
                return 0.0

            # 优先选择高成功率转移
            t = random.choice(transitions)
            reward = t.success_rate - t.action.cost * 0.01
            if _is_goal(t.to_state):
                reward += 2.0
            return reward + self.discount * _rollout(
                t.to_state, depth + 1, cost_so_far + t.action.cost
            )

        def _ucb1(node: _MCTSNode, parent_visits: int) -> float:
            if node.visits == 0:
                return float("inf")
            return node.value / node.visits + self.exploration_weight * math.sqrt(
                math.log(parent_visits) / node.visits
            )

        def _select(node: _MCTSNode) -> "_MCTSNode":
            """选择最佳子节点 (UCB1)。"""
            while node.untried_transitions or node.children:
                if node.untried_transitions:
                    t = node.untried_transitions.pop()
                    child = _MCTSNode(t.to_state, transition=t, parent=node)
                    child.untried_transitions = state_machine.get_valid_transitions(t.to_state)
                    node.children.append(child)
                    return child
                node = max(node.children, key=lambda c: _ucb1(c, node.visits))

                # 平衡探索：如果所有子节点都非零，进入 UCB1 选中
                if node.children:
                    node = max(node.children, key=lambda c: _ucb1(c, node.visits))
            return node

        def _backpropagate(node: _MCTSNode, reward: float) -> None:
            while node is not None:
                node.visits += 1
                node.value += reward
                node = node.parent
                reward *= self.discount

        # MCTS 主循环
        for i in range(self.max_iterations):
            leaf = _select(root)

            # 检查是否到达目标
            found_goal = _is_goal(leaf.state_id)
            reward = 5.0 if found_goal else _rollout(leaf.state_id, 0, 0.0)
            _backpropagate(leaf, reward)

        # 提取最优路径
        best_path: List[GUITransition] = []
        current: Optional[_MCTSNode] = root
        while current and current.children:
            current = max(current.children, key=lambda c: c.visits)
            if current.transition:
                best_path.append(current.transition)

        total_cost = sum(t.action.cost for t in best_path)

        return PathResult(
            path=best_path,
            total_cost=total_cost,
            success=len(best_path) > 0,
            nodes_visited=root.visits,
            search_strategy=ExplorationStrategy.MCTS,
        )

    def statistics(self) -> Dict[str, Any]:
        return {
            "exploration_weight": self.exploration_weight,
            "max_iterations": self.max_iterations,
            "max_depth": self.max_depth,
            "discount": self.discount,
        }
