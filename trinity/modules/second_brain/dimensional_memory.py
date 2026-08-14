"""
CB61: DimensionalMemory — 维度结构化记忆
========================================

对标 DimMem (arXiv:2605.15759)。每条记忆表示为 typed atomic unit，
含显式字段：time / location / reason / purpose / keywords / importance。
维度感知检索，支持按字段精确过滤。

设计要点：
  - Typed Atomic Unit：每条记忆有类型标记和结构化的 6 个维度字段
  - 维度感知检索：支持按单个或多个维度字段精确过滤
  - 组合查询：time=[t1,t2] + location="office" + importance>=0.7
  - 重要性自动评分：基于访问频率、时效性、关联度三维计算
  - 维度索引：每个维度维护独立倒排索引，加速字段级过滤

Reference:
  - DimMem (Supermemory): arXiv 2605.15759 — typed atomic memory units
  - DimMem 核心维度：time / location / reason / purpose / keywords / importance
  - Supermemory LongMemEval: 71.43% multi-session, 76.69% temporal
"""

from __future__ import annotations

import dataclasses
import logging
import math
import threading
import time as _time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class MemoryImportance(Enum):
    """记忆重要性等级。"""
    CRITICAL = 5     # 系统必需记忆（安全规则、身份绑定）
    HIGH = 4         # 重要偏好、关键决策
    MEDIUM = 3       # 一般事实、常规交互
    LOW = 2          # 琐碎信息
    TRANSIENT = 1    # 临时/一次性信息


class MemoryDimension(Enum):
    """显式维度字段。"""
    TIME = "time"             # 时间维度
    LOCATION = "location"     # 地理位置/上下文
    REASON = "reason"         # 产生原因
    PURPOSE = "purpose"       # 目的/意图
    KEYWORDS = "keywords"     # 关键词标签
    IMPORTANCE = "importance" # 重要性评分
    TYPE = "type"             # 记忆类型
    SOURCE = "source"         # 来源


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class DimensionFilter:
    """维度过滤器：用于精确字段级查询。"""
    time_start: Optional[float] = None
    time_end: Optional[float] = None
    location: Optional[str] = None
    reason_keywords: Optional[List[str]] = None
    purpose_keywords: Optional[List[str]] = None
    keywords: Optional[List[str]] = None
    min_importance: Optional[float] = None
    max_importance: Optional[float] = None
    memory_types: Optional[List[str]] = None
    sources: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


@dataclass
class AtomicMemoryUnit:
    """Typed Atomic Memory Unit — DimMem 核心数据结构。

    每条记忆作为原子单元，包含 6 个显式维度字段 + 类型与来源。
    """
    memory_id: str
    content: str                     # 记忆的文本内容
    memory_type: str = "fact"        # fact / preference / plan / relationship / event
    time: float = field(default_factory=_time.time)  # 时间维度
    location: str = ""               # 地理位置/上下文维度
    reason: str = ""                 # 产生原因维度
    purpose: str = ""                # 目的/意图维度
    keywords: List[str] = field(default_factory=list)  # 关键词标签维度
    importance: float = 0.5          # 重要性评分 [0, 1]
    source: str = ""                 # 来源会话/文件/工具
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=_time.time)
    last_accessed: float = 0.0
    access_count: int = 0

    def matches_filter(self, f: DimensionFilter) -> bool:
        """判断此记忆是否满足维度过滤条件。"""
        if f.time_start is not None and self.time < f.time_start:
            return False
        if f.time_end is not None and self.time > f.time_end:
            return False
        if f.location is not None and f.location.lower() not in self.location.lower():
            return False
        if f.reason_keywords is not None:
            if not any(kw.lower() in self.reason.lower() for kw in f.reason_keywords):
                return False
        if f.purpose_keywords is not None:
            if not any(kw.lower() in self.purpose.lower() for kw in f.purpose_keywords):
                return False
        if f.keywords is not None:
            my_kws = {k.lower() for k in self.keywords}
            if not my_kws.intersection(k.lower() for k in f.keywords):
                return False
        if f.min_importance is not None and self.importance < f.min_importance:
            return False
        if f.max_importance is not None and self.importance > f.max_importance:
            return False
        if f.memory_types is not None and self.memory_type not in f.memory_types:
            return False
        if f.sources is not None and self.source not in f.sources:
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "type": self.memory_type,
            "dimensions": {
                "time": self.time,
                "location": self.location,
                "reason": self.reason,
                "purpose": self.purpose,
                "keywords": self.keywords,
                "importance": self.importance,
            },
            "source": self.source,
            "access_count": self.access_count,
        }


# ============================================================================
# Main Class
# ============================================================================

class DimensionalMemory:
    """维度结构化记忆管理器。

    存储 typed atomic memory units，支持维度感知检索。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._memories: Dict[str, AtomicMemoryUnit] = {}
        # 维度索引：加速字段级过滤
        self._type_index: Dict[str, Set[str]] = defaultdict(set)
        self._location_index: Dict[str, Set[str]] = defaultdict(set)
        self._keyword_index: Dict[str, Set[str]] = defaultdict(set)
        self._source_index: Dict[str, Set[str]] = defaultdict(set)
        self._insert_count: int = 0
        self._created_at: float = _time.time()

    # -- CRUD --

    def store(self, content: str, memory_type: str = "fact",
              time: Optional[float] = None, location: str = "",
              reason: str = "", purpose: str = "",
              keywords: Optional[List[str]] = None,
              importance: float = 0.5, source: str = "",
              metadata: Optional[Dict[str, Any]] = None) -> AtomicMemoryUnit:
        """写入一条维度结构化记忆。"""
        with self._lock:
            mem_id = f"dim:{self._insert_count}:{memory_type}:{int(_time.time())}"
            unit = AtomicMemoryUnit(
                memory_id=mem_id, content=content,
                memory_type=memory_type, time=time or _time.time(),
                location=location, reason=reason, purpose=purpose,
                keywords=keywords or [], importance=importance,
                source=source, metadata=metadata or {},
            )
            self._memories[mem_id] = unit
            self._type_index[memory_type].add(mem_id)
            if location:
                self._location_index[location.lower()].add(mem_id)
            for kw in unit.keywords:
                self._keyword_index[kw.lower()].add(mem_id)
            if source:
                self._source_index[source].add(mem_id)
            self._insert_count += 1
            return unit

    # -- Query --

    def query_by_dimensions(self, dim_filter: DimensionFilter,
                            limit: int = 100) -> List[AtomicMemoryUnit]:
        """维度感知检索：按字段精确过滤。"""
        with self._lock:
            results = []
            # 候选集优化：从最小索引开始
            candidate_ids = set(self._memories.keys())
            if dim_filter.memory_types:
                type_ids = set()
                for t in dim_filter.memory_types:
                    type_ids.update(self._type_index.get(t, set()))
                candidate_ids &= type_ids
            if dim_filter.location:
                candidate_ids &= self._location_index.get(dim_filter.location.lower(), set())
            if dim_filter.keywords:
                kw_ids = set()
                for kw in dim_filter.keywords:
                    kw_ids.update(self._keyword_index.get(kw.lower(), set()))
                candidate_ids &= kw_ids
            if dim_filter.sources:
                src_ids = set()
                for s in dim_filter.sources:
                    src_ids.update(self._source_index.get(s, set()))
                candidate_ids &= src_ids

            for mid in candidate_ids:
                unit = self._memories[mid]
                if unit.matches_filter(dim_filter):
                    unit.access_count += 1
                    unit.last_accessed = _time.time()
                    results.append(unit)

            results.sort(key=lambda u: (u.importance, u.time), reverse=True)
            return results[:limit]

    def get_by_type(self, memory_type: str) -> List[AtomicMemoryUnit]:
        with self._lock:
            ids = self._type_index.get(memory_type, set())
            return [self._memories[mid] for mid in ids if mid in self._memories]

    # -- Importance Scoring --

    def recalculate_importance(self, memory_id: str,
                               access_weight: float = 0.3,
                               recency_weight: float = 0.3,
                               relevance_weight: float = 0.4) -> Optional[float]:
        """三维重要性重算：访问频率 + 时效性 + 关联度。"""
        with self._lock:
            unit = self._memories.get(memory_id)
            if unit is None:
                return None
            # 访问频率得分
            access_score = min(1.0, math.log2(unit.access_count + 1) / 10.0)
            # 时效性得分
            age_seconds = _time.time() - unit.time
            recency_score = max(0.0, 1.0 - age_seconds / (86400 * 30))
            # 关联度得分（基于关键词共现）
            relevance_score = min(1.0, len(unit.keywords) / 10.0)
            unit.importance = (
                access_weight * access_score +
                recency_weight * recency_score +
                relevance_weight * relevance_score
            )
            return unit.importance

    # -- Statistics --

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self._memories)
            by_type = {t: len(ids) for t, ids in self._type_index.items()}
            avg_importance = (sum(u.importance for u in self._memories.values()) /
                              max(1, total))
            return {
                "class": "DimensionalMemory (CB61)",
                "total_memories": total,
                "by_type": by_type,
                "indexed_locations": len(self._location_index),
                "indexed_keywords": len(self._keyword_index),
                "indexed_sources": len(self._source_index),
                "avg_importance": round(avg_importance, 3),
                "total_inserts": self._insert_count,
                "uptime_seconds": _time.time() - self._created_at,
            }
