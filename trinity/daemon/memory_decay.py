#!/usr/bin/env python3
"""
Memory Decay Module — 记忆衰减引擎 (Layer 6b)
================================================
Based on Ebbinghaus forgetting curve: R = e^(-t/S)

Exponential decay with configurable rate per memory type:
  score = importance * exp(-lambda * days_since_creation)

衰减调度器定期扫描所有活跃记忆，将衰退分数低于阈值的标记为"待压缩"。

Decay rates per memory_type:
  - handoff   (较快衰减): λ = 0.05 (half-life ~14 days)
  - knowledge (较慢衰减): λ = 0.01 (half-life ~69 days)
  - general   (默认)    : λ = 0.02 (half-life ~35 days)

Reference: Ebbinghaus, H. (1885). Memory: A Contribution to Experimental Psychology.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Types
# ============================================================================


class DecayStatus(Enum):
    """记忆衰减状态"""
    HEALTHY = "healthy"               # 分数充足，无需处理
    DECAYING = "decaying"             # 正在衰减
    PENDING_COMPRESSION = "pending_compression"  # 低于阈值，待压缩
    ARCHIVED = "archived"             # 已归档


class MemoryType(Enum):
    """记忆类型，对应不同衰减速率"""
    HANDOFF = "handoff"         # 交接记忆（快速衰减）
    KNOWLEDGE = "knowledge"     # 知识记忆（缓慢衰减）
    GENERAL = "general"         # 通用记忆（默认）
    CONVERSATION = "conversation"  # 对话记忆（中等衰减）
    SYSTEM = "system"           # 系统记忆（极慢衰减）


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class DecayConfig:
    """记忆衰减配置"""
    # 各类型记忆的 λ 值（衰减速率）
    lambda_per_type: Dict[str, float] = field(default_factory=lambda: {
        MemoryType.HANDOFF.value: 0.05,
        MemoryType.KNOWLEDGE.value: 0.01,
        MemoryType.GENERAL.value: 0.02,
        MemoryType.CONVERSATION.value: 0.03,
        MemoryType.SYSTEM.value: 0.002,
    })

    # 压缩阈值：score 低于此值标记为 pending_compression
    compression_threshold: float = 0.15

    # 调度间隔（秒）
    scan_interval_seconds: float = 3600.0  # 默认每小时扫描一次

    # 单次扫描最大记忆数
    max_memories_per_scan: int = 500

    # 批量压缩最小条数
    min_batch_size: int = 3

    # 批量压缩最大条数
    max_batch_size: int = 20

    # ── 多因子遗忘（2026-08-15，对齐业界算法遗忘方案）─────────────
    # 访问频率因子：access_count 每满 access_boost_interval 次，
    # 分数提升 access_boost_step（上限 access_boost_max）。
    access_boost_enabled: bool = True
    access_boost_interval: int = 5
    access_boost_step: float = 0.05
    access_boost_max: float = 0.20
    # 最近访问保护：days_since_access <= recency_protection_days 时
    # 分数不低于 recency_floor（刚被检索/使用过的记忆不归档）。
    recency_protection_days: float = 7.0
    recency_floor: float = 0.50


@dataclass
class DecayResult:
    """单条记忆衰减计算结果"""
    memory_id: str
    memory_type: str
    importance: float
    decay_lambda: float
    days_since_creation: float
    decay_score: float
    status: DecayStatus
    created_at: str
    content_preview: str = ""
    access_count: int = 0
    days_since_access: Optional[float] = None


@dataclass
class DecayScanReport:
    """衰减扫描报告"""
    scan_id: str
    scanned_at: float
    total_scanned: int
    healthy_count: int
    decaying_count: int
    pending_compression_count: int
    results: List[DecayResult] = field(default_factory=list)
    config: Optional[DecayConfig] = None


# ============================================================================
# MemoryDecayEngine
# ============================================================================


class MemoryDecayEngine:
    """记忆衰减引擎

    核心职责：
    1. 根据记忆类型和创建时间计算衰减分数
    2. 扫描所有活跃记忆，识别衰退到阈值以下的记忆
    3. 生成待压缩清单供 MemoryCompressor 消费

    衰减公式：
      score = importance * exp(-λ * days_since_creation)
            × (1 + 访问频率提升)          # 频繁访问的记忆衰减更慢
            , 且最近访问保护兜底            # 7 天内访问过 → 分数不低于地板

      其中：
        importance ∈ [0, 1]  记忆初始重要性
        λ > 0                衰减速率（越大衰减越快）
        days_since_creation  自创建以来的天数
    """

    def __init__(self, config: Optional[DecayConfig] = None):
        self.config = config or DecayConfig()
        self._last_scan_time: float = 0.0
        self._scan_reports: List[DecayScanReport] = []

    # ── Core Decay Calculation ──────────────────────────────────

    @staticmethod
    def calculate_decay_score(
        importance: float,
        decay_lambda: float,
        days_since_creation: float,
        access_count: int = 0,
        days_since_access: Optional[float] = None,
        access_boost_enabled: bool = True,
        access_boost_interval: int = 5,
        access_boost_step: float = 0.05,
        access_boost_max: float = 0.20,
        recency_protection_days: float = 7.0,
        recency_floor: float = 0.50,
    ) -> float:
        """计算单条记忆的衰减分数（多因子）。

        Args:
            importance: 原始重要性 [0, 1]
            decay_lambda: 衰减速率 λ
            days_since_creation: 自创建以来的天数
            access_count: 历史访问/检索次数（访问频率因子）
            days_since_access: 距最近一次访问的天数（最近访问保护）
            access_boost_* / recency_*: 多因子遗忘参数

        Returns:
            衰减后分数，范围 [0, 1]
        """
        if importance <= 0:
            return 0.0
        score = importance * math.exp(-decay_lambda * days_since_creation)

        # 访问频率因子：频繁检索的记忆衰减更慢（对齐 2026 算法遗忘方案）
        if access_boost_enabled and access_count > 0:
            boost = min(
                (access_count // access_boost_interval) * access_boost_step,
                access_boost_max,
            )
            score *= 1.0 + boost

        # 最近访问保护：刚用过的记忆不归档
        if (
            days_since_access is not None
            and recency_protection_days > 0
            and days_since_access <= recency_protection_days
        ):
            score = max(score, recency_floor)

        return max(0.0, min(1.0, score))

    def get_lambda_for_type(self, memory_type: str) -> float:
        """获取指定记忆类型的衰减速率 λ。"""
        return self.config.lambda_per_type.get(
            memory_type,
            self.config.lambda_per_type[MemoryType.GENERAL.value],
        )

    def compute_days_since(self, created_at: Any) -> float:
        """计算自创建以来的天数。"""
        now = datetime.now(timezone.utc)
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at)
            except (ValueError, TypeError):
                return 0.0
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            delta = now - created_at
            return delta.total_seconds() / 86400.0
        return 0.0

    def determine_status(self, score: float) -> DecayStatus:
        """根据分数判定衰减状态."""
        threshold = self.config.compression_threshold
        if score <= threshold:
            return DecayStatus.PENDING_COMPRESSION
        elif score < 0.4:
            return DecayStatus.DECAYING
        else:
            return DecayStatus.HEALTHY

    def evaluate_memory(
        self,
        memory_id: str,
        importance: float,
        memory_type: str,
        created_at: Any,
        content: str = "",
        access_count: int = 0,
        last_accessed_at: Any = None,
    ) -> DecayResult:
        """评估单条记忆的衰减状态。

        Args:
            memory_id: 记忆唯一标识
            importance: 原始重要性
            memory_type: 记忆类型（决定 λ）
            created_at: 创建时间
            content: 记忆内容（可选，用于报告）
            access_count: 历史访问次数（多因子遗忘）
            last_accessed_at: 最近访问时间（多因子遗忘）

        Returns:
            DecayResult 包含衰减评估结果
        """
        decay_lambda = self.get_lambda_for_type(memory_type)
        days = self.compute_days_since(created_at)
        days_since_access = self.compute_days_since(last_accessed_at) if last_accessed_at else None
        score = self.calculate_decay_score(
            importance,
            decay_lambda,
            days,
            access_count=access_count,
            days_since_access=days_since_access,
            access_boost_enabled=self.config.access_boost_enabled,
            access_boost_interval=self.config.access_boost_interval,
            access_boost_step=self.config.access_boost_step,
            access_boost_max=self.config.access_boost_max,
            recency_protection_days=self.config.recency_protection_days,
            recency_floor=self.config.recency_floor,
        )
        status = self.determine_status(score)

        return DecayResult(
            memory_id=memory_id,
            memory_type=memory_type,
            importance=importance,
            decay_lambda=decay_lambda,
            days_since_creation=round(days, 4),
            decay_score=round(score, 6),
            status=status,
            created_at=str(created_at),
            content_preview=(content[:80] + "..." if len(content) > 80 else content),
            access_count=access_count,
            days_since_access=round(days_since_access, 4) if days_since_access is not None else None,
        )

    # ── Batch Scanning ──────────────────────────────────────────

    def scan_memories(
        self,
        memories: List[Dict[str, Any]],
    ) -> DecayScanReport:
        """批量扫描记忆列表，生成衰减报告。

        Args:
            memories: 记忆字典列表，每个需含 memory_id, importance,
                      category (用作 memory_type), created_at, content

        Returns:
            DecayScanReport
        """
        import uuid

        report = DecayScanReport(
            scan_id=f"scan_{uuid.uuid4().hex[:12]}",
            scanned_at=datetime.now(timezone.utc).timestamp(),
            total_scanned=len(memories),
            healthy_count=0,
            decaying_count=0,
            pending_compression_count=0,
            config=self.config,
        )

        for mem in memories:
            mem_id = str(mem.get("memory_id", ""))
            importance = float(mem.get("importance", 0.5))
            # Use category as memory_type for decay routing
            memory_type = str(mem.get("category", MemoryType.GENERAL.value))
            created_at = mem.get("created_at")
            content = str(mem.get("content", ""))
            access_count = int(mem.get("access_count") or 0)
            last_accessed_at = mem.get("last_accessed_at")

            result = self.evaluate_memory(
                memory_id=mem_id,
                importance=importance,
                memory_type=memory_type,
                created_at=created_at,
                content=content,
                access_count=access_count,
                last_accessed_at=last_accessed_at,
            )
            report.results.append(result)

            if result.status == DecayStatus.HEALTHY:
                report.healthy_count += 1
            elif result.status == DecayStatus.DECAYING:
                report.decaying_count += 1
            elif result.status == DecayStatus.PENDING_COMPRESSION:
                report.pending_compression_count += 1

        self._last_scan_time = report.scanned_at
        self._scan_reports.append(report)
        return report

    def get_pending_compression(
        self, report: DecayScanReport,
    ) -> List[DecayResult]:
        """从扫描报告中提取 pending_compression 的记忆列表。

        按 decay_score 升序排列（最需要压缩的排在前面）。
        """
        pending = [
            r for r in report.results
            if r.status == DecayStatus.PENDING_COMPRESSION
        ]
        pending.sort(key=lambda r: r.decay_score)
        return pending

    # ── Batch Batching ──────────────────────────────────────────

    def create_compression_batches(
        self,
        pending: List[DecayResult],
    ) -> List[List[DecayResult]]:
        """将待压缩记忆按配置分组为批次。

        按 memory_type 分组后再分批，确保同类型记忆一起压缩。

        Returns:
            批次列表
        """
        min_size = self.config.min_batch_size
        max_size = self.config.max_batch_size

        # Group by memory_type
        by_type: Dict[str, List[DecayResult]] = {}
        for item in pending:
            by_type.setdefault(item.memory_type, []).append(item)

        batches: List[List[DecayResult]] = []
        for mem_type, items in by_type.items():
            items.sort(key=lambda r: r.decay_score)
            for i in range(0, len(items), max_size):
                batch = items[i : i + max_size]
                if len(batch) >= min_size:
                    batches.append(batch)
                else:
                    # 如果该类型的剩余不足 min_batch_size（如只有 1-2 条），
                    # 仍然纳入压缩（标记为小批次），由 MemoryCompressor 决定是否合并
                    batches.append(batch)

        return batches

    # ── Statistics ──────────────────────────────────────────────

    def get_half_life(self, memory_type: str) -> float:
        """计算指定类型的半衰期（天数）。

        half-life = ln(2) / λ
        """
        decay_lambda = self.get_lambda_for_type(memory_type)
        if decay_lambda <= 0:
            return float("inf")
        return math.log(2) / decay_lambda

    def summary(self) -> Dict[str, Any]:
        """返回引擎状态摘要。"""
        half_lives = {
            mt.value: round(self.get_half_life(mt.value), 1)
            for mt in MemoryType
        }
        return {
            "decay_config": {
                "lambda_per_type": dict(self.config.lambda_per_type),
                "compression_threshold": self.config.compression_threshold,
                "scan_interval_seconds": self.config.scan_interval_seconds,
            },
            "half_lives_days": half_lives,
            "total_scans": len(self._scan_reports),
            "last_scan_time": self._last_scan_time,
        }


# ============================================================================
# 可调用的衰减计算函数（便于外部使用）
# ============================================================================


def compute_decay(
    importance: float,
    decay_lambda: float = 0.01,
    days_since_creation: float = 0.0,
) -> float:
    """便捷函数：计算指数衰减分数。

    Args:
        importance: 初始重要性 [0, 1]
        decay_lambda: 衰减速率，默认 0.01
        days_since_creation: 创建后经过的天数

    Returns:
        衰减后分数
    """
    return MemoryDecayEngine.calculate_decay_score(
        importance, decay_lambda, days_since_creation,
    )
