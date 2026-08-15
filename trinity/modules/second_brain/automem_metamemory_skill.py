"""
# status: orphan (2026-08-15 audit, not in runtime path)
AutoMem — Metamemory as a Learnable Skill
==========================================
arXiv 2607.01224 · P48-1

元记忆作为可学技能：将文件系统操作提升为第一类动作，通过两阶段 RL 训练
（SFT 初始化 → GRPO 微调）让 Agent 学会管理记忆。

设计要点:
  - MetamemoryActionSpace: 5 项原子记忆动作
  - ScaffoldOptimizer: 外循环 Prompt/文件 schema 优化
  - MemorySkillExecutor: 内循环记忆-任务交织执行
  - MetamemoryTrainer: rollout→reward RL 训练循环
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from collections import defaultdict, deque
import random

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MemAction(Enum):
    MEM_READ = auto()
    MEM_WRITE = auto()
    MEM_SEARCH = auto()
    MEM_APPEND = auto()
    MEM_CREATE = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class MemoryAction:
    """单次记忆动作的完整记录。"""
    action: MemAction
    target: str = ""              # 目标 memory key / path
    payload: str = ""             # 写入 / 追加内容
    timestamp: float = field(default_factory=time.time)
    success: bool = True
    reward: float = 0.0


@dataclass
class TaskRollout:
    """一次任务 rollout——动作序列 + 累积奖励。"""
    task_id: str
    actions: List[MemoryAction] = field(default_factory=list)
    total_reward: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# MetamemoryActionSpace
# ---------------------------------------------------------------------------

class MetamemoryActionSpace:
    """元记忆动作空间——定义 5 项原子记忆动作。

    MEM_READ / MEM_WRITE / MEM_SEARCH / MEM_APPEND / MEM_CREATE
    每项动作有独立的执行语义和默认优先级权重。
    """

    def __init__(self) -> None:
        self._actions: Dict[MemAction, float] = {
            MemAction.MEM_READ: 1.0,
            MemAction.MEM_WRITE: 1.0,
            MemAction.MEM_SEARCH: 0.8,
            MemAction.MEM_APPEND: 0.6,
            MemAction.MEM_CREATE: 0.5,
        }
        self._lock = threading.RLock()

    def sample(self, task_context: str = "") -> MemAction:
        """按权重概率采样一个记忆动作。"""
        with self._lock:
            actions = list(self._actions.keys())
            weights = list(self._actions.values())
            total = sum(weights)
            probs = [w / total for w in weights]
            return actions[int(np.random.choice(len(actions), p=probs))]

    def execute(self, action: MemAction, target: str, payload: str = "") -> MemoryAction:
        """模拟执行一个记忆动作（实际文件 I/O 在 SkillExecutor 层处理）。"""
        ma = MemoryAction(action=action, target=target, payload=payload)
        return ma

    def statistics(self) -> Dict[str, Any]:
        return {"actions": {a.name: w for a, w in self._actions.items()}}


# ---------------------------------------------------------------------------
# ScaffoldOptimizer
# ---------------------------------------------------------------------------

class ScaffoldOptimizer:
    """外循环脚手架优化器——根据任务反馈调优记忆 Prompt 模板和文件 schema。

    优化目标：调整 prompt 模板中的记忆指令措辞、文件目录结构 schema、
    可用动作词表，使 Agent 在选择记忆动作时的策略更优。
    """

    def __init__(self, prompt_template: str = "", file_schema: Optional[Dict[str, Any]] = None) -> None:
        self.prompt_template = prompt_template or self._default_prompt()
        self.file_schema: Dict[str, Any] = file_schema or {"root": "memory/", "types": {}}
        self._history: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    @staticmethod
    def _default_prompt() -> str:
        return (
            "You have access to memory actions: READ, WRITE, SEARCH, APPEND, CREATE. "
            "Before each task step, decide whether to use a memory action. "
            "Prefer SEARCH before READ, APPEND over WRITE for incremental updates."
        )

    def optimize(self, feedback: Dict[str, Any]) -> Dict[str, Any]:
        """根据任务反馈调整脚手架。

        feedback 含: task_success, memory_usage_rate, retrieval_accuracy, action_distribution
        """
        with self._lock:
            changes = {}

            retrieval_acc = feedback.get("retrieval_accuracy", 0.5)
            if retrieval_acc < 0.6:
                self.prompt_template += " Strongly prefer SEARCH over direct READ for unfamiliar content."
                changes["prompt"] = "added retrieval hint"

            usage_rate = feedback.get("memory_usage_rate", 0.0)
            if usage_rate < 0.1:
                self.prompt_template += " Use WRITE or APPEND after each significant insight."
                changes["prompt"] += " + write hint"

            action_dist = feedback.get("action_distribution", {})
            if action_dist.get("MEM_CREATE", 0) > 0.3 * sum(action_dist.values()):
                self.file_schema.setdefault("max_files", 20)
                changes["schema"] = "capped CREATE"

            self._history.append({"feedback": feedback, "changes": changes})
            return {"template": self.prompt_template, "schema": self.file_schema, "changes": changes}

    def statistics(self) -> Dict[str, Any]:
        return {"optimization_steps": len(self._history), "schema_keys": list(self.file_schema.keys())}


# ---------------------------------------------------------------------------
# MemorySkillExecutor
# ---------------------------------------------------------------------------

class MemorySkillExecutor:
    """内循环记忆技能执行器——将记忆操作与任务操作交织执行。

    在每一轮任务步骤中：先决定是否需要记忆动作 → 执行记忆动作 → 执行任务步骤。
    """

    def __init__(self, action_space: Optional[MetamemoryActionSpace] = None) -> None:
        self.action_space = action_space or MetamemoryActionSpace()
        self._memory_store: Dict[str, str] = {}
        self._execution_log: List[MemoryAction] = []
        self._lock = threading.RLock()

    def step(
        self, task_step: str, context: str = "", policy_fn: Optional[Callable[[str], MemAction]] = None,
    ) -> Tuple[str, MemoryAction]:
        """执行一步：交织记忆动作 + 任务步骤。

        Returns (task_result, memory_action_taken)
        """
        with self._lock:
            # 决定记忆动作
            if policy_fn:
                mem_action_type = policy_fn(context + task_step)
            else:
                mem_action_type = self.action_space.sample(context)

            # 执行记忆动作
            ma = self._execute_memory(mem_action_type, context, task_step)
            self._execution_log.append(ma)

            # 模拟任务步骤执行
            task_result = f"[step] {task_step[:60]}... done"

            return task_result, ma

    def _execute_memory(self, action: MemAction, context: str, task: str) -> MemoryAction:
        key = f"mem_{len(self._memory_store)}"

        if action == MemAction.MEM_READ:
            value = self._memory_store.get(key, "")
            return MemoryAction(action=action, target=key, payload=value[:100])

        elif action == MemAction.MEM_WRITE:
            self._memory_store[key] = task
            return MemoryAction(action=action, target=key, payload=task[:80])

        elif action == MemAction.MEM_SEARCH:
            matches = [v for k, v in self._memory_store.items() if any(w in v for w in context.split()[:5])]
            return MemoryAction(action=action, target="*", payload="; ".join(matches[:3]))

        elif action == MemAction.MEM_APPEND:
            prev = self._memory_store.get(key, "")
            self._memory_store[key] = prev + "\n" + task
            return MemoryAction(action=action, target=key, payload=task[:80])

        else:  # CREATE
            new_key = f"mem_{len(self._memory_store) + 1}"
            self._memory_store[new_key] = ""
            return MemoryAction(action=action, target=new_key, payload="")

    def statistics(self) -> Dict[str, Any]:
        return {
            "store_size": len(self._memory_store),
            "executions": len(self._execution_log),
        }


# ---------------------------------------------------------------------------
# MetamemoryTrainer
# ---------------------------------------------------------------------------

class MetamemoryTrainer:
    """元记忆 RL 训练器——SFT 初始化 → GRPO 微调两阶段训练。

    Parameters
    ----------
    sft_epochs : int
        SFT 阶段轮数。
    grpo_epochs : int
        GRPO 微调轮数。
    """

    def __init__(self, sft_epochs: int = 3, grpo_epochs: int = 5) -> None:
        self.sft_epochs = sft_epochs
        self.grpo_epochs = grpo_epochs
        self.action_space = MetamemoryActionSpace()
        self.rollouts: List[TaskRollout] = []
        self._policy_weights: Dict[MemAction, float] = {
            a: 1.0 / len(MemAction) for a in MemAction
        }
        self._lock = threading.RLock()

    def sft_phase(self, demonstrations: List[Tuple[str, MemAction]]) -> None:
        """SFT 初始化——从示范中学习初始动作分布。"""
        with self._lock:
            counts: Dict[MemAction, int] = defaultdict(int)
            for _, action in demonstrations:
                counts[action] += 1
            total = sum(counts.values()) or 1
            for a in MemAction:
                self._policy_weights[a] = max(0.05, counts[a] / total)

    def grpo_phase(self, num_rollouts: int = 10) -> List[TaskRollout]:
        """GRPO 微调——rollout → reward → 策略更新。"""
        with self._lock:
            for ep in range(self.grpo_epochs):
                for _ in range(num_rollouts):
                    rollout = TaskRollout(task_id=f"task_{ep}_{_}")
                    actions = [self.action_space.sample() for _ in range(5)]
                    for a in actions:
                        ma = MemoryAction(action=a, target=f"tgt_{a.name}")
                        ma.reward = self._compute_reward(a, rollout)
                        rollout.actions.append(ma)
                        rollout.total_reward += ma.reward

                    # GRPO 更新：提升高奖励动作权重
                    for ma in rollout.actions:
                        self._policy_weights[ma.action] += 0.01 * ma.reward
                    self.rollouts.append(rollout)

                # 归一化
                total_w = sum(self._policy_weights.values())
                for a in MemAction:
                    self._policy_weights[a] /= total_w

            return self.rollouts[-num_rollouts:]

    def _compute_reward(self, action: MemAction, rollout: TaskRollout) -> float:
        """简易奖励函数：多样化动作组合加分，过度重复扣分。"""
        reward = 0.5
        action_counts = defaultdict(int)
        for ma in rollout.actions:
            action_counts[ma.action] += 1
        if action_counts[action] <= 2:
            reward += 0.3
        if action in (MemAction.MEM_SEARCH, MemAction.MEM_READ):
            reward += 0.1
        return reward

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_rollouts": len(self.rollouts),
            "policy_weights": {a.name: round(w, 4) for a, w in self._policy_weights.items()},
            "phases": f"SFT({self.sft_epochs})→GRPO({self.grpo_epochs})",
        }
