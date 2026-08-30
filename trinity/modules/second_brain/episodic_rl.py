"""
# status: frozen (2026-09 EXECUTION 163)
P4-2: Episodic Memory RL Scoring Engine (对标 MemRL)
======================================================

将记忆检索建模为马尔可夫决策过程 (MDP)，用 Q 值对每条记忆打分——
不仅看语义相似度，更看历史成功率。支持非参数 RL 在线更新权重，
无需微调模型权重。

MemRL 核心设计：
  - 状态 (State) s_t: 当前查询上下文 + 已选记忆集合 + 剩余 token 预算
  - 动作 (Action) a_t: 选择一条（或多条）记忆加入上下文
  - 奖励 (Reward) r_t: 下游任务质量信号（用户反馈 / LLM 评分 / 自动评估）
  - Q(s, a): 使用成功记忆 (hit) / 尝试 (try) 加 epsilon 平滑
  - 更新规则: Q_new = Q_old + α * (r + γ * max_a' Q(s', a') - Q_old)

特性：
  - 非参数 RL：无需神经网络，直接维护 Q-table/近似 Q 值
  - 在线更新：每次检索-使用-反馈循环后即时更新
  - 冷启动：新记忆默认 Q 值 = 语义相似度归一化值，随使用逐步收敛
  - 多臂老虎机 (UCB) 探索：上置信界平衡利用与探索

Reference: MemRL (arxiv.org/abs/2601.03192, January 2026)
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────

class FeedbackSignal(Enum):
    """下游任务反馈信号类型。"""
    EXPLICIT_POSITIVE = auto()   # 用户明确好评
    EXPLICIT_NEGATIVE = auto()   # 用户明确差评
    TASK_SUCCESS = auto()        # 任务成功完成
    TASK_FAILURE = auto()        # 任务失败
    LLM_JUDGE_SCORE = auto()     # LLM 评判分数
    IMPLICIT_USE = auto()        # 隐式使用（无反馈，默认小正奖励）


# ── 数据结构 ──────────────────────────────────────────────────────

@dataclass
class MemoryState:
    """记忆条目的 MDP 状态表示。

    Args:
        memory_id: 记忆唯一标识
        content_hash: 内容指纹（用于去重）
        q_value: 当前 Q 值估计
        try_count: 累计被检索召回次数
        hit_count: 累计产生正反馈次数
        last_accessed: 最后访问时间戳
        last_q_update: 最后 Q 值更新时间
        semantic_score: 原始语义相似度（冷启动基准）
        ucb_bonus: 当前 UCB 探索奖励
    """

    memory_id: str
    content_hash: str = ""
    q_value: float = 0.5
    try_count: int = 0
    hit_count: int = 0
    last_accessed: float = field(default_factory=time.time)
    last_q_update: float = field(default_factory=time.time)
    semantic_score: float = 0.5
    ucb_bonus: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalAction:
    """MDP 动作：选择一条记忆加入上下文。

    Args:
        memory_id: 被选中的记忆 ID
        context_slot: 放入上下文的第几个槽位
        token_cost: 该记忆消耗的 token 数
    """

    memory_id: str
    context_slot: int = 0
    token_cost: int = 0


@dataclass
class TransitionRecord:
    """MDP 转移记录（用于批量更新）。"""

    memory_id: str
    old_q: float
    reward: float
    timestamp: float = field(default_factory=time.time)


# ── RL 引擎 ──────────────────────────────────────────────────────

class EpisodicRLScorer:
    """情景记忆 RL 评分引擎 — 对标 MemRL。

    使用方式::

        from trinity.modules.second_brain.episodic_rl import EpisodicRLScorer

        rl = EpisodicRLScorer(learning_rate=0.1, discount_factor=0.95)

        # 注册记忆（冷启动用语义分数）
        rl.register_memory("mem_001", semantic_score=0.72)

        # 检索时获取 RL 增强分数
        scores = rl.score_memories(["mem_001", "mem_002", "mem_003"])
        # → {"mem_001": 0.78, "mem_002": 0.55, "mem_003": 0.62}

        # 收到反馈后更新
        rl.record_feedback("mem_001", FeedbackSignal.TASK_SUCCESS)
        rl.update_q_values()
    """

    # ── 构造函数 ──────────────────────────────────────────────────

    def __init__(
        self,
        learning_rate: float = 0.1,
        discount_factor: float = 0.95,
        epsilon: float = 0.05,              # Q-value 平滑 epsilon
        ucb_c: float = 2.0,                  # UCB 探索参数
        reward_positive: float = 1.0,
        reward_negative: float = -0.5,
        reward_implicit: float = 0.05,
        default_q: float = 0.5,
    ):
        """初始化 RL 评分引擎。

        Args:
            learning_rate: Q-learning 学习率 α
            discount_factor: 折扣因子 γ
            epsilon: Q 值平滑的微小正数（防除零）
            ucb_c: UCB 上置信界探索参数
            reward_positive: 正反馈奖励
            reward_negative: 负反馈惩罚
            reward_implicit: 隐式使用微量奖励
            default_q: 新记忆默认 Q 值
        """
        self.learning_rate = learning_rate
        self.discount_factor = discount_factor
        self.epsilon = epsilon
        self.ucb_c = ucb_c
        self.reward_positive = reward_positive
        self.reward_negative = reward_negative
        self.reward_implicit = reward_implicit
        self.default_q = default_q

        # 内部存储
        self._states: Dict[str, MemoryState] = {}
        self._pending_transitions: List[TransitionRecord] = []
        self._global_try_count: int = 0

    # ── 记忆注册 ─────────────────────────────────────────────────

    def register_memory(
        self, memory_id: str, semantic_score: float = 0.5, content_hash: str = ""
    ) -> MemoryState:
        """注册一条记忆到 RL 评分系统。

        Args:
            memory_id: 记忆唯一标识
            semantic_score: 初始语义相似度（作为冷启动 Q 值）
            content_hash: 内容指纹
        """
        if memory_id in self._states:
            state = self._states[memory_id]
            state.semantic_score = max(state.semantic_score, semantic_score)
            return state

        state = MemoryState(
            memory_id=memory_id,
            content_hash=content_hash,
            q_value=semantic_score if semantic_score > 0 else self.default_q,
            semantic_score=semantic_score,
        )
        self._states[memory_id] = state
        return state

    def register_memories(
        self, scored_memories: Dict[str, float]
    ) -> None:
        """批量注册记忆。scored_memories: {memory_id: semantic_score}"""
        for mid, score in scored_memories.items():
            self.register_memory(mid, score)

    # ── 评分 ──────────────────────────────────────────────────────

    def score_memory(self, memory_id: str) -> float:
        """获取单条记忆的 RL 增强评分：Q(s,a) + UCB bonus。

        Returns:
            RL 增强分数（语义 + 历史成功率 + 探索奖励）
        """
        state = self._states.get(memory_id)
        if state is None:
            return self.default_q

        # touch access time
        state.last_accessed = time.time()

        # UCB 探索奖励
        if state.try_count > 0:
            state.ucb_bonus = self.ucb_c * math.sqrt(
                math.log(max(self._global_try_count, 1) + 1) / state.try_count
            )
        else:
            state.ucb_bonus = float("inf")  # 未尝试的记忆优先探索

        return state.q_value + state.ucb_bonus

    def score_memories(self, memory_ids: List[str]) -> Dict[str, float]:
        """批量评分。

        Returns:
            {memory_id: rl_score} 按 score 降序排列
        """
        scored = {mid: self.score_memory(mid) for mid in memory_ids}
        return dict(sorted(scored.items(), key=lambda x: x[1], reverse=True))

    # ── 反馈处理 ──────────────────────────────────────────────────

    def record_feedback(self, memory_id: str, signal: FeedbackSignal) -> None:
        """记录一条记忆的使用反馈。

        Args:
            memory_id: 记忆 ID
            signal: 反馈信号类型
        """
        if memory_id not in self._states:
            self.register_memory(memory_id)

        state = self._states[memory_id]
        state.try_count += 1
        self._global_try_count += 1

        reward = self._signal_to_reward(signal)
        if reward > 0:
            state.hit_count += 1

        self._pending_transitions.append(
            TransitionRecord(
                memory_id=memory_id,
                old_q=state.q_value,
                reward=reward,
            )
        )

        logger.debug(
            "Feedback recorded: mem=%s signal=%s reward=%.2f hits=%d/%d",
            memory_id, signal.name, reward, state.hit_count, state.try_count,
        )

    def record_batch_feedback(
        self, feedbacks: Dict[str, FeedbackSignal]
    ) -> None:
        """批量记录反馈。"""
        for mid, signal in feedbacks.items():
            self.record_feedback(mid, signal)

    def _signal_to_reward(self, signal: FeedbackSignal) -> float:
        """将反馈信号映射为数值奖励。"""
        mapping = {
            FeedbackSignal.EXPLICIT_POSITIVE: self.reward_positive,
            FeedbackSignal.TASK_SUCCESS: self.reward_positive * 0.8,
            FeedbackSignal.IMPLICIT_USE: self.reward_implicit,
            FeedbackSignal.EXPLICIT_NEGATIVE: self.reward_negative,
            FeedbackSignal.TASK_FAILURE: self.reward_negative * 0.8,
            FeedbackSignal.LLM_JUDGE_SCORE: self.reward_implicit,
        }
        return mapping.get(signal, 0.0)

    # ── Q 值更新 ─────────────────────────────────────────────────

    def update_q_values(self) -> int:
        """执行所有待处理 transitions 的 Q-learning 更新。

        Q_new = Q_old + α * (r + γ * V(s') - Q_old)
        其中 V(s') = max_a Q(s', a)（无模型时用 default_q 近似）

        Returns:
            实际更新的 transition 数量
        """
        pending = self._pending_transitions
        if not pending:
            return 0

        for tr in pending:
            state = self._states.get(tr.memory_id)
            if state is None:
                continue

            # 非参数 Q-learning: V(s') 用历史成功率近似
            success_rate = (
                state.hit_count / max(state.try_count, 1)
                if state.try_count > 0
                else self.default_q
            )
            v_next = self.discount_factor * success_rate

            td_error = tr.reward + v_next - tr.old_q
            new_q = tr.old_q + self.learning_rate * td_error

            # Clip to [0, 1]
            state.q_value = max(0.0, min(1.0, new_q))
            state.last_q_update = time.time()

            logger.debug(
                "Q-update: mem=%s old=%.3f new=%.3f td_error=%.3f",
                tr.memory_id, tr.old_q, state.q_value, td_error,
            )

        count = len(pending)
        self._pending_transitions.clear()
        logger.info("Q-values updated for %d transitions", count)
        return count

    # ── 查询 ──────────────────────────────────────────────────────

    def get_hit_rate(self, memory_id: str) -> Optional[float]:
        """获取记忆的历史命中率。"""
        state = self._states.get(memory_id)
        if state is None:
            return None
        return state.hit_count / max(state.try_count, 1)

    def top_k(self, memory_ids: List[str], k: int = 5) -> List[Tuple[str, float]]:
        """返回 top-k 高分记忆。"""
        scored = self.score_memories(memory_ids)
        return list(scored.items())[:k]

    # ── 持久化（2026-08-17 P0-2 闭环审计修复）────────────────────────
    # 此前 RL 奖励/ Q 值只存内存（无 save/load），API/worker 进程重启即清零，
    # "RL 记忆决策"永远从零开始（学完即忘）。这里导出/恢复完整状态，
    # 由 MemoryAggregator 持久化时顺带落盘 rl_state.json。

    def to_dict(self) -> Dict[str, Any]:
        """导出 RL 状态为可序列化 dict（MemoryState → plain dict）。"""
        return {
            "version": 1,
            "global_try_count": self._global_try_count,
            "states": {
                mid: {
                    "memory_id": s.memory_id,
                    "content_hash": s.content_hash,
                    "q_value": s.q_value,
                    "try_count": s.try_count,
                    "hit_count": s.hit_count,
                    "last_accessed": s.last_accessed,
                    "last_q_update": s.last_q_update,
                    "semantic_score": s.semantic_score,
                    "ucb_bonus": s.ucb_bonus,
                    "metadata": s.metadata,
                }
                for mid, s in self._states.items()
            },
        }

    def save(self, path: str) -> bool:
        """原子写入 RL 状态到 JSON 文件（tmp + os.replace）。"""
        import json
        import os

        try:
            d = self.to_dict()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("EpisodicRLScorer save failed (non-fatal): %s", e)
            return False

    @classmethod
    def load(cls, path: str) -> "EpisodicRLScorer":
        """从 JSON 恢复 RL 状态；文件缺失/损坏时返回空引擎（不中断启动）。"""
        import json

        scorer = cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            scorer._global_try_count = int(d.get("global_try_count", 0))
            for mid, s in (d.get("states") or {}).items():
                scorer._states[mid] = MemoryState(
                    memory_id=s.get("memory_id", mid),
                    content_hash=s.get("content_hash", ""),
                    q_value=float(s.get("q_value", scorer.default_q)),
                    try_count=int(s.get("try_count", 0)),
                    hit_count=int(s.get("hit_count", 0)),
                    last_accessed=float(s.get("last_accessed", time.time())),
                    last_q_update=float(s.get("last_q_update", time.time())),
                    semantic_score=float(s.get("semantic_score", 0.5)),
                    ucb_bonus=float(s.get("ucb_bonus", 0.0)),
                    metadata=dict(s.get("metadata") or {}),
                )
            logger.info(
                "EpisodicRLScorer loaded: %d states, global_try=%d",
                len(scorer._states), scorer._global_try_count,
            )
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("EpisodicRLScorer load failed (non-fatal): %s", e)
        return scorer

    def statistics(self) -> Dict[str, Any]:
        """返回引擎运行时统计。"""
        total = len(self._states)
        if total == 0:
            return {"total_memories": 0, "global_try_count": 0}

        hit_total = sum(s.hit_count for s in self._states.values())
        try_total = sum(s.try_count for s in self._states.values())
        avg_q = sum(s.q_value for s in self._states.values()) / total

        return {
            "total_memories": total,
            "global_try_count": self._global_try_count,
            "total_hits": hit_total,
            "hit_rate": hit_total / max(try_total, 1),
            "avg_q_value": round(avg_q, 4),
            "pending_transitions": len(self._pending_transitions),
            "learning_rate": self.learning_rate,
            "discount_factor": self.discount_factor,
        }
