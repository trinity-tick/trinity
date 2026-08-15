"""
# status: orphan (2026-08-15 audit, not in runtime path)
P20-7: Tool Usage Memory — Structured Tool Call Memory (Dakera 2026)
====================================================================

对标方案：Dakera Tool Usage Learning (2026).

设计要点：
  - ToolOutcomeRecord：工具名/任务类型/结果质量/延迟/成本
  - 重要性评分加权存储（高质量结果保留更久）
  - 调用前检索最相关历史成果辅助选工具
  - 成功率统计与指数衰减（近期数据权重更高）

核心组件：
  - ToolOutcomeRecord:     结构化工具调用记录
  - ToolUsageMemoryBank:   工具使用记忆库
  - ToolSelector:           基于历史记忆的工具选择器
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
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class OutcomeQuality(Enum):
    """结果质量等级。"""
    EXCELLENT = "excellent"    # 完美结果
    GOOD = "good"              # 良好
    ACCEPTABLE = "acceptable"  # 可接受
    POOR = "poor"              # 差
    FAILED = "failed"          # 失败


class DecayFunction(Enum):
    """衰减函数类型。"""
    EXPONENTIAL = "exponential"
    LINEAR = "linear"
    NONE = "none"


class SelectionStrategy(Enum):
    """选择策略。"""
    SUCCESS_RATE = "success_rate"        # 纯成功率
    COST_AWARE = "cost_aware"            # 成本感知
    LATENCY_OPTIMIZED = "latency_optimized"  # 延迟优化
    WEIGHTED_HYBRID = "weighted_hybrid"  # 加权混合


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class ToolOutcomeRecord:
    """工具调用结果记录。

    对标 Dakera 的 structured outcome record。
    """
    record_id: str
    tool_name: str
    task_type: str
    result_quality: OutcomeQuality
    latency_ms: float
    cost: float  # API 成本（美元）
    input_summary: str = ""
    output_summary: str = ""
    importance_score: float = 0.5
    success: bool = False
    error_message: str = ""
    tags: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    decay_weight: float = 1.0

    def age_hours(self) -> float:
        """记录年龄（小时）。"""
        return (time.time() - self.timestamp) / 3600.0


@dataclass
class ToolStats:
    """工具统计画像。"""
    tool_name: str
    total_calls: int = 0
    success_count: int = 0
    avg_latency_ms: float = 0.0
    avg_cost: float = 0.0
    success_rate: float = 0.0
    last_used: float = 0.0
    quality_distribution: Dict[str, int] = field(default_factory=dict)


@dataclass
class SelectionResult:
    """工具选择结果。"""
    result_id: str
    tool_name: str
    score: float
    supporting_records: int
    success_rate: float
    avg_latency_ms: float
    avg_cost: float
    recommendation_reason: str = ""


# ============================================================================
# Constants
# ============================================================================

QUALITY_WEIGHTS: Dict[OutcomeQuality, float] = {
    OutcomeQuality.EXCELLENT: 1.0,
    OutcomeQuality.GOOD: 0.8,
    OutcomeQuality.ACCEPTABLE: 0.5,
    OutcomeQuality.POOR: 0.2,
    OutcomeQuality.FAILED: -0.3,
}

DECAY_HALF_LIFE_HOURS: float = 168.0  # 7 天半衰期
MAX_RECORDS_PER_TOOL: int = 1000


# ============================================================================
# Core Components
# ============================================================================

class ToolUsageMemoryBank:
    """工具使用记忆库。

    存储结构化工具调用记录，支持重要性评分加权和时间衰减。
    """

    def __init__(self, decay_half_life_hours: float = DECAY_HALF_LIFE_HOURS,
                 decay_function: DecayFunction = DecayFunction.EXPONENTIAL):
        self._lock = threading.RLock()
        self.records: deque[ToolOutcomeRecord] = deque(maxlen=MAX_RECORDS_PER_TOOL * 10)
        self.decay_half_life = decay_half_life_hours
        self.decay_function = decay_function

    def record(self, tool_name: str, task_type: str, quality: OutcomeQuality,
               latency_ms: float, cost: float, success: bool,
               input_summary: str = "", output_summary: str = "",
               error_message: str = "", tags: List[str] = None) -> str:
        """记录一次工具调用。"""
        with self._lock:
            record = ToolOutcomeRecord(
                record_id=str(uuid.uuid4())[:8],
                tool_name=tool_name,
                task_type=task_type,
                result_quality=quality,
                latency_ms=latency_ms,
                cost=cost,
                input_summary=input_summary,
                output_summary=output_summary,
                importance_score=QUALITY_WEIGHTS.get(quality, 0.5),
                success=success,
                error_message=error_message,
                tags=tags or [],
            )
            self.records.append(record)
            return record.record_id

    def query(self, task_type: Optional[str] = None, tool_name: Optional[str] = None,
              min_success_rate: float = 0.0, top_k: int = 10,
              apply_decay: bool = True) -> List[ToolOutcomeRecord]:
        """查询最相关的工具调用记录。"""
        with self._lock:
            candidates: List[Tuple[float, ToolOutcomeRecord]] = []

            for rec in self.records:
                # 过滤
                if tool_name and rec.tool_name != tool_name:
                    continue
                if task_type and task_type.lower() not in rec.task_type.lower():
                    continue

                # 评分
                score = rec.importance_score
                if apply_decay:
                    decay = self._compute_decay(rec.age_hours())
                    rec.decay_weight = decay
                    score *= decay

                if score >= min_success_rate * 0.5:
                    candidates.append((score, rec))

            candidates.sort(key=lambda x: x[0], reverse=True)
            return [rec for _, rec in candidates[:top_k]]

    def _compute_decay(self, age_hours: float) -> float:
        """时间衰减计算。"""
        if self.decay_function == DecayFunction.EXPONENTIAL:
            # 指数衰减：weight = 2^(-age/half_life)
            return 2.0 ** (-age_hours / self.decay_half_life)
        elif self.decay_function == DecayFunction.LINEAR:
            total = self.decay_half_life * 4
            return max(1.0 - age_hours / total, 0.0)
        else:
            return 1.0

    def get_stats(self, tool_name: Optional[str] = None) -> ToolStats:
        """获取工具统计画像。"""
        with self._lock:
            relevant = [r for r in self.records if not tool_name or r.tool_name == tool_name]
            if not relevant:
                return ToolStats(tool_name=tool_name or "unknown")

            names = set(r.tool_name for r in relevant)
            # 取第一个工具的统计
            target = tool_name or next(iter(names))
            target_records = [r for r in relevant if r.tool_name == target]

            successes = [r for r in target_records if r.success]
            quality_dist = defaultdict(int)
            for r in target_records:
                quality_dist[r.result_quality.value] += 1

            return ToolStats(
                tool_name=target,
                total_calls=len(target_records),
                success_count=len(successes),
                avg_latency_ms=round(sum(r.latency_ms for r in target_records) / len(target_records), 2),
                avg_cost=round(sum(r.cost for r in target_records) / len(target_records), 4),
                success_rate=round(len(successes) / len(target_records), 4),
                last_used=max(r.timestamp for r in target_records),
                quality_distribution=dict(quality_dist),
            )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            tool_counts = defaultdict(int)
            quality_counts = defaultdict(int)
            for r in self.records:
                tool_counts[r.tool_name] += 1
                quality_counts[r.result_quality.value] += 1
            return {
                "total_records": len(self.records),
                "unique_tools": len(tool_counts),
                "by_tool": dict(tool_counts),
                "by_quality": dict(quality_counts),
            }


class ToolSelector:
    """基于历史记忆的工具选择器。

    调用前检索最相关历史成果，计算多因子加权分数。
    """

    def __init__(self, bank: ToolUsageMemoryBank, strategy: SelectionStrategy = SelectionStrategy.WEIGHTED_HYBRID):
        self._lock = threading.RLock()
        self.bank = bank
        self.strategy = strategy
        self.selections: List[SelectionResult] = []

    def select(self, task_type: str, available_tools: List[str],
               top_k: int = 3) -> List[SelectionResult]:
        """选择最适合任务的工具。"""
        with self._lock:
            results: List[SelectionResult] = []

            for tool_name in available_tools:
                records = self.bank.query(tool_name=tool_name, task_type=task_type, top_k=50)
                if not records:
                    # 无历史，给中等默认分
                    results.append(SelectionResult(
                        result_id=str(uuid.uuid4())[:8],
                        tool_name=tool_name,
                        score=0.5,
                        supporting_records=0,
                        success_rate=0.5,
                        avg_latency_ms=500.0,
                        avg_cost=0.01,
                        recommendation_reason="No historical data; defaults applied",
                    ))
                    continue

                stats = self._compute_tool_stats(records)
                score = self._compute_score(stats, records)

                reason = self._generate_reason(records, stats, tool_name)
                results.append(SelectionResult(
                    result_id=str(uuid.uuid4())[:8],
                    tool_name=tool_name,
                    score=round(score, 4),
                    supporting_records=len(records),
                    success_rate=stats.success_rate,
                    avg_latency_ms=stats.avg_latency_ms,
                    avg_cost=stats.avg_cost,
                    recommendation_reason=reason,
                ))

            results.sort(key=lambda x: x.score, reverse=True)
            selected = results[:top_k]
            self.selections.extend(selected)
            return selected

    def _compute_tool_stats(self, records: List[ToolOutcomeRecord]) -> ToolStats:
        successes = [r for r in records if r.success]
        decay_weights = [r.decay_weight for r in records]

        total_weight = sum(decay_weights) or 1
        weighted_success = sum(
            (1.0 if r.success else 0.0) * r.decay_weight for r in records
        )

        return ToolStats(
            tool_name=records[0].tool_name,
            total_calls=len(records),
            success_count=len(successes),
            avg_latency_ms=round(sum(r.latency_ms * r.decay_weight for r in records) / total_weight, 2),
            avg_cost=round(sum(r.cost * r.decay_weight for r in records) / total_weight, 4),
            success_rate=round(weighted_success / total_weight, 4),
            last_used=max(r.timestamp for r in records),
        )

    def _compute_score(self, stats: ToolStats, records: List[ToolOutcomeRecord]) -> float:
        """多因子加权评分。"""
        if self.strategy == SelectionStrategy.SUCCESS_RATE:
            return stats.success_rate * 1.0

        if self.strategy == SelectionStrategy.COST_AWARE:
            cost_factor = 1.0 / (1.0 + stats.avg_cost * 100)
            return stats.success_rate * 0.6 + cost_factor * 0.4

        if self.strategy == SelectionStrategy.LATENCY_OPTIMIZED:
            latency_factor = 1.0 / (1.0 + stats.avg_latency_ms / 1000)
            return stats.success_rate * 0.5 + latency_factor * 0.5

        # WEIGHTED_HYBRID
        cost_factor = 1.0 / (1.0 + stats.avg_cost * 100)
        latency_factor = 1.0 / (1.0 + stats.avg_latency_ms / 1000)
        recency_factor = min((time.time() - stats.last_used) / 86400, 7.0) / 7.0

        return (
            stats.success_rate * 0.4
            + cost_factor * 0.2
            + latency_factor * 0.2
            + recency_factor * 0.1
            + min(len(records) / 100, 1.0) * 0.1
        )

    def _generate_reason(self, records: List[ToolOutcomeRecord], stats: ToolStats,
                         tool_name: str) -> str:
        """生成推荐理由。"""
        successes = [r for r in records if r.success]
        if stats.success_rate > 0.8 and len(successes) > 5:
            return f"{tool_name}: High success rate ({stats.success_rate:.0%}) over {len(records)} calls"
        if len(records) < 3:
            return f"{tool_name}: Limited history ({len(records)} calls), moderate confidence"
        return f"{tool_name}: {stats.success_rate:.0%} success, avg {stats.avg_latency_ms:.0f}ms, ${stats.avg_cost:.4f}/call"

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_selections": len(self.selections),
                "strategy": self.strategy.value,
                "bank": self.bank.statistics(),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P20-7 Tool Usage Memory",
        "benchmark": "Dakera 2026 — Structured Tool Outcome Records + Importance-Weighted Retrieval",
        "classes": 3,
        "enums": 3,
        "dataclasses": 3,
        "key_pattern": "Record→Decay→Query Stats→Select with Success/Cost/Latency/Rencency",
        "key_metric": "Exponential decay (168h half-life) + quality-weighted importance + 4-strategy selection",
        "thread_safe": True,
    }
