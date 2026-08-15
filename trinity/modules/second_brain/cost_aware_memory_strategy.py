"""
# status: orphan (2026-08-15 audit, not in runtime path)
P17-5: Cost-Aware Memory Strategy
=================================

对标 AgentMemBench 成本-精度权衡 — 按查询难度动态路由记忆策略。

设计要点：
  - 3 策略动态路由：ICW(轻量 ~200 tok) / CBS(均衡 ~3478 tok) / EKV(深度 ~5126 tok)
  - Token 预算感知自适应分配：按日/周预算动态调整策略优先级
  - Cost-Recall 帕累托前沿优化：在召回精度与 API 成本间求最优折衷
  - 日 API 成本预估模型：基于历史调用分布预测日开销

核心组件：
  - StrategyRouter:      查询难度评估 → 策略动态路由
  - TokenBudgetManager:  Token 预算感知自适应分配
  - ParetoFrontierOptimizer:  cost-recall 帕累托前沿维护
  - DailyCostEstimator:  日 API 成本预估模型
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class CostStrategy(Enum):
    """成本策略等级。"""
    ICW = "icw"      # In-Context Window  ~200 tok
    CBS = "cbs"      # Context-Based Summary ~3478 tok
    EKV = "ekv"      # External Key-Value ~5126 tok


class QueryDifficulty(Enum):
    """查询难度等级。"""
    SIMPLE = "simple"       # 单跳事实检索
    MODERATE = "moderate"   # 多跳推理
    COMPLEX = "complex"     # 深度关联 + 反事实


class BudgetStatus(Enum):
    """预算状态。"""
    AMPLE = "ample"        # 充裕，优先精度
    NORMAL = "normal"      # 正常，均衡
    CONSTRAINED = "constrained"  # 紧张，优先成本
    EXCEEDED = "exceeded"  # 超支，仅 ICW


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class StrategyDecision:
    """单次策略决策。"""
    query_id: str
    difficulty: QueryDifficulty
    strategy: CostStrategy
    estimated_tokens: int
    estimated_cost_usd: float
    reasoning: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class TokenBudget:
    """Token 预算。"""
    daily_limit: int = 100_000
    weekly_limit: int = 700_000
    consumed_today: int = 0
    consumed_this_week: int = 0
    reserved: int = 0
    last_reset: float = field(default_factory=time.time)


@dataclass
class ParetoPoint:
    """帕累托前沿点。"""
    strategy: CostStrategy
    cost_per_query: float
    recall_at_5: float
    tokens_per_query: int
    is_frontier: bool = True


@dataclass
class DailyCostProjection:
    """日成本预估。"""
    date: str
    projected_queries: int
    projected_cost: float
    strategy_distribution: Dict[CostStrategy, int] = field(default_factory=dict)
    confidence: float = 0.0


# ============================================================================
# Strategy Tokens & Cost Constants
# ============================================================================

STRATEGY_TOKENS: Dict[CostStrategy, int] = {
    CostStrategy.ICW: 200,
    CostStrategy.CBS: 3478,
    CostStrategy.EKV: 5126,
}

STRATEGY_COST_PER_1M_TOKENS: Dict[CostStrategy, float] = {
    CostStrategy.ICW: 0.50,
    CostStrategy.CBS: 1.00,
    CostStrategy.EKV: 2.00,
}

DIFFICULTY_DEFAULT: Dict[QueryDifficulty, CostStrategy] = {
    QueryDifficulty.SIMPLE: CostStrategy.ICW,
    QueryDifficulty.MODERATE: CostStrategy.CBS,
    QueryDifficulty.COMPLEX: CostStrategy.EKV,
}


# ============================================================================
# Core Components
# ============================================================================

class StrategyRouter:
    """查询难度评估 → 策略动态路由。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.history: List[StrategyDecision] = []
        self.difficulty_heuristics: Dict[str, float] = {}

    def assess_difficulty(self, query: str, context_keys: Optional[int] = None) -> QueryDifficulty:
        """评估查询难度。"""
        # 基于关键词密度和上下文大小做启发式判断
        word_count = len(query.split())
        has_compare = any(kw in query.lower() for kw in ["比较", "对比", "vs", "difference", "why", "为什么", "如果"])
        has_multi_entity = query.lower().count("and") + query.lower().count("与") + query.lower().count("和") > 1

        if word_count < 4 and not has_compare:
            return QueryDifficulty.SIMPLE
        elif has_compare or has_multi_entity or (context_keys and context_keys > 5):
            return QueryDifficulty.COMPLEX
        else:
            return QueryDifficulty.MODERATE

    def route(
        self,
        query: str,
        budget: Optional["TokenBudget"] = None,
        force_strategy: Optional[CostStrategy] = None,
    ) -> StrategyDecision:
        with self._lock:
            difficulty = self.assess_difficulty(query)

            if force_strategy:
                strategy = force_strategy
            elif budget and budget.consumed_today > budget.daily_limit * 0.85:
                strategy = CostStrategy.ICW
            elif budget and budget.consumed_today > budget.daily_limit * 0.5:
                # 降级一级
                downgrade = {
                    QueryDifficulty.COMPLEX: CostStrategy.CBS,
                    QueryDifficulty.MODERATE: CostStrategy.ICW,
                    QueryDifficulty.SIMPLE: CostStrategy.ICW,
                }
                strategy = downgrade.get(difficulty, DIFFICULTY_DEFAULT[difficulty])
            else:
                strategy = DIFFICULTY_DEFAULT[difficulty]

            tokens = STRATEGY_TOKENS[strategy]
            cost = tokens / 1_000_000 * STRATEGY_COST_PER_1M_TOKENS[strategy]

            decision = StrategyDecision(
                query_id=str(uuid.uuid4())[:8],
                difficulty=difficulty,
                strategy=strategy,
                estimated_tokens=tokens,
                estimated_cost_usd=cost,
                reasoning=f"难度={difficulty.value} → {strategy.value} ({tokens} tok, ${cost:.6f})",
            )
            self.history.append(decision)
            return decision

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            dist: Dict[str, int] = defaultdict(int)
            total_cost = 0.0
            total_tokens = 0
            for d in self.history:
                dist[d.strategy.value] += 1
                total_cost += d.estimated_cost_usd
                total_tokens += d.estimated_tokens
            return {
                "total_decisions": len(self.history),
                "strategy_distribution": dict(dist),
                "total_cost_usd": round(total_cost, 6),
                "total_tokens": total_tokens,
            }


class TokenBudgetManager:
    """Token 预算感知自适应分配。"""

    def __init__(self, daily_limit: int = 100_000, weekly_limit: int = 700_000):
        self._lock = threading.RLock()
        self.budget = TokenBudget(daily_limit=daily_limit, weekly_limit=weekly_limit)
        self._check_reset()

    def _check_reset(self):
        now = time.time()
        if now - self.budget.last_reset > 86400:
            self.budget.consumed_today = 0
            self.budget.last_reset = now

    def consume(self, tokens: int) -> bool:
        with self._lock:
            self._check_reset()
            if self.budget.consumed_today + tokens > self.budget.daily_limit:
                return False
            self.budget.consumed_today += tokens
            self.budget.consumed_this_week += tokens
            return True

    def reserve(self, tokens: int) -> bool:
        with self._lock:
            if self.budget.reserved + tokens > self.budget.daily_limit:
                return False
            self.budget.reserved += tokens
            return True

    def status(self) -> BudgetStatus:
        with self._lock:
            self._check_reset()
            ratio = self.budget.consumed_today / max(self.budget.daily_limit, 1)
            if ratio < 0.3:
                return BudgetStatus.AMPLE
            elif ratio < 0.6:
                return BudgetStatus.NORMAL
            elif ratio < 0.85:
                return BudgetStatus.CONSTRAINED
            else:
                return BudgetStatus.EXCEEDED

    def remaining(self) -> int:
        with self._lock:
            self._check_reset()
            return max(0, self.budget.daily_limit - self.budget.consumed_today - self.budget.reserved)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "daily_limit": self.budget.daily_limit,
                "consumed_today": self.budget.consumed_today,
                "consumed_week": self.budget.consumed_this_week,
                "reserved": self.budget.reserved,
                "remaining": self.remaining(),
                "status": self.status().value,
            }


class ParetoFrontierOptimizer:
    """Cost-Recall 帕累托前沿优化。

    维护 3 策略下的 cost / recall 帕累托前沿。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.points: List[ParetoPoint] = []
        self._init_defaults()

    def _init_defaults(self):
        defaults = [
            (CostStrategy.ICW, 0.0001, 0.32, 200),
            (CostStrategy.CBS, 0.0035, 0.68, 3478),
            (CostStrategy.EKV, 0.0103, 0.87, 5126),
        ]
        for strategy, cost, recall, tokens in defaults:
            self.points.append(ParetoPoint(strategy=strategy, cost_per_query=cost, recall_at_5=recall, tokens_per_query=tokens))

    def get_optimal(self, min_recall: float = 0.0, max_cost: Optional[float] = None) -> Optional[ParetoPoint]:
        with self._lock:
            candidates = [p for p in self.points if p.recall_at_5 >= min_recall]
            if max_cost is not None:
                candidates = [p for p in candidates if p.cost_per_query <= max_cost]
            if not candidates:
                return None
            # 在满足约束下选最低成本
            return min(candidates, key=lambda p: p.cost_per_query)

    def update_point(self, strategy: CostStrategy, cost: float, recall: float, tokens: int):
        with self._lock:
            for p in self.points:
                if p.strategy == strategy:
                    p.cost_per_query = cost
                    p.recall_at_5 = recall
                    p.tokens_per_query = tokens
                    return

    def frontier_summary(self) -> List[Dict[str, Any]]:
        with self._lock:
            # 仅输出前景点（不被任何其他点同时支配 cost 和 recall）
            frontier = []
            for p in sorted(self.points, key=lambda x: x.cost_per_query):
                dominated = any(
                    (o.cost_per_query <= p.cost_per_query and o.recall_at_5 > p.recall_at_5) or
                    (o.cost_per_query < p.cost_per_query and o.recall_at_5 >= p.recall_at_5)
                    for o in self.points if o != p
                )
                frontier.append({
                    "strategy": p.strategy.value,
                    "cost": p.cost_per_query,
                    "recall@5": p.recall_at_5,
                    "tokens": p.tokens_per_query,
                    "on_frontier": not dominated,
                })
            return frontier


class DailyCostEstimator:
    """日 API 成本预估模型。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.history: List[StrategyDecision] = []

    def observe(self, decision: StrategyDecision):
        with self._lock:
            self.history.append(decision)

    def project(self, expected_queries: Optional[int] = None) -> DailyCostProjection:
        with self._lock:
            if not self.history:
                return DailyCostProjection(date=time.strftime("%Y-%m-%d"), projected_queries=0, projected_cost=0.0)

            # 统计策略分布
            dist: Dict[CostStrategy, int] = defaultdict(int)
            total_cost = 0.0
            for d in self.history:
                dist[d.strategy] += 1
                total_cost += d.estimated_cost_usd

            n = len(self.history)
            queries = expected_queries or n * 2

            if n > 0:
                projected = total_cost / n * queries
            else:
                projected = 0.0

            return DailyCostProjection(
                date=time.strftime("%Y-%m-%d"),
                projected_queries=queries,
                projected_cost=round(projected, 6),
                strategy_distribution={k: v for k, v in dist.items()},
                confidence=min(0.95, n / 100),
            )

    def statistics(self) -> Dict[str, Any]:
        proj = self.project()
        return {
            "observed_queries": len(self.history),
            "projected_daily_queries": proj.projected_queries,
            "projected_daily_cost": proj.projected_cost,
            "strategy_distribution": {k.value: v for k, v in proj.strategy_distribution.items()},
        }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P17-5 Cost-Aware Memory Strategy",
        "benchmark": "AgentMemBench Cost-Recall Tradeoff",
        "classes": 4,
        "enums": 3,
        "dataclasses": 5,
        "key_pattern": "Strategy Router(ICW~200/CBS~3478/EKV~5126 tok) + Token Budget + Pareto Frontier + Daily Cost Estimator",
        "key_metric": "Dynamic Strategy Routing with Cost-Recall Pareto Optimization",
        "thread_safe": True,
    }
