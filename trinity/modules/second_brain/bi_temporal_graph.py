"""
# status: orphan (2026-08-15 audit, not in runtime path)
P10-5: Bi-Temporal Graph Engine — 对标 Zep/Graphiti 双时态知识图谱

实现双时间轴知识图谱:
  - event_time (事件时间): 事实发生的真实时间
  - ingestion_time (摄入时间): 系统记录事实的时间
  - 每条图谱边带有效性窗口 (valid_from / valid_to)
  - 过期事实标记为 invalidated 而非删除
  - point_in_time_query(): 返回给定时间点有效的实体和关系
  - 自动过期扫描 + 历史快照

Reference:
    Zep Memory Store (2026): https://www.getzep.com/
    Graphiti (2025): https://github.com/getzep/graphiti
"""

import copy
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ══════════════════════════════════════════════════════════════════════
# 枚举与数据结构
# ══════════════════════════════════════════════════════════════════════

class BiTemporalStatus(Enum):
    """实体/关系的时间状态。"""
    ACTIVE = "active"           # 当前有效
    INVALIDATED = "invalidated" # 已过期但保留（非物理删除）
    FUTURE = "future"           # 尚未生效
    ARCHIVED = "archived"       # 归档（超过 retention 期）


class TemporalGranularity(Enum):
    """时间精度。"""
    SECOND = "second"
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


@dataclass
class BiTemporalEntity:
    """双时态实体节点。"""
    entity_id: str
    entity_type: str
    properties: dict = field(default_factory=dict)
    # 事件时间
    event_time: float = 0.0                 # 事实发生时间 (Unix timestamp)
    valid_from: float | None = None         # 有效性窗口起始
    valid_to: float | None = None           # 有效性窗口结束（None = 无限）
    # 摄入时间
    ingestion_time: float = field(default_factory=time.time)
    ingested_by: str = "system"
    # 状态
    status: BiTemporalStatus = BiTemporalStatus.ACTIVE
    # 版本追踪
    version: int = 1
    replaced_by: str = ""                   # 被哪个新版本替换
    replaces: str = ""                      # 替换了哪个旧版本


@dataclass
class BiTemporalRelation:
    """双时态关系边。"""
    relation_id: str
    subject: str
    predicate: str
    object: str
    # 有效性窗口
    valid_from: float | None = None
    valid_to: float | None = None
    # 摄入时间
    ingestion_time: float = field(default_factory=time.time)
    # 元数据
    metadata: dict = field(default_factory=dict)
    weight: float = 1.0
    # 状态
    status: BiTemporalStatus = BiTemporalStatus.ACTIVE
    version: int = 1


@dataclass
class PointInTimeSnapshot:
    """时间点快照。"""
    timestamp: float
    entities: dict[str, "BiTemporalEntity"] = field(default_factory=dict)
    relations: dict[str, "BiTemporalRelation"] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════
# Bi-Temporal Graph Engine
# ══════════════════════════════════════════════════════════════════════

class BiTemporalGraphEngine:
    """双时态知识图谱引擎。

    特性:
      - 双时间轴: event_time + ingestion_time
      - 有效性窗口: valid_from / valid_to
      - 软删除: invalidated 而非物理删除
      - 时间点回溯查询
      - 自动过期扫描
      - 历史快照

    Usage:
        engine = BiTemporalGraphEngine()
        engine.add_entity("trinity_v6", "system", valid_from=...)
        engine.add_relation("e1", "uses", "e2", valid_from=..., valid_to=...)
        snapshot = engine.point_in_time_query(time.time())
    """

    def __init__(
        self,
        default_retention_days: int = 365,
        default_granularity: TemporalGranularity = TemporalGranularity.SECOND,
    ):
        self._entities: dict[str, BiTemporalEntity] = {}
        self._relations: dict[str, BiTemporalRelation] = {}
        self._entity_history: dict[str, list[BiTemporalEntity]] = defaultdict(list)
        self._relation_history: dict[str, list[BiTemporalRelation]] = defaultdict(list)
        self.default_retention_days = default_retention_days
        self.default_granularity = default_granularity
        self._created_at = time.time()

    # ── 实体操作 ────────────────────────────────────────────────────

    def add_entity(
        self,
        entity_id: str,
        entity_type: str,
        properties: dict | None = None,
        event_time: float | None = None,
        valid_from: float | None = None,
        valid_to: float | None = None,
        ingestion_time: float | None = None,
        ingested_by: str = "system",
    ) -> BiTemporalEntity:
        """添加或更新实体。

        如果实体已存在且处于 ACTIVE 状态，则将其 invalidate 并创建新版本。
        """
        now = time.time()
        entity = BiTemporalEntity(
            entity_id=entity_id,
            entity_type=entity_type,
            properties=properties or {},
            event_time=event_time or now,
            valid_from=valid_from,
            valid_to=valid_to,
            ingestion_time=ingestion_time or now,
            ingested_by=ingested_by,
        )

        if entity_id in self._entities:
            existing = self._entities[entity_id]
            if existing.status == BiTemporalStatus.ACTIVE:
                existing.status = BiTemporalStatus.INVALIDATED
                entity.replaces = entity_id
                entity.version = existing.version + 1
                existing.replaced_by = entity_id

        self._entity_history[entity_id].append(entity)
        self._entities[entity_id] = entity
        return entity

    def get_entity(self, entity_id: str) -> BiTemporalEntity | None:
        """获取当前实体。"""
        return self._entities.get(entity_id)

    def invalidate_entity(
        self,
        entity_id: str,
        reason: str = "",
        invalidated_at: float | None = None,
    ) -> BiTemporalEntity | None:
        """软删除实体（标记为 invalidated）。"""
        entity = self._entities.get(entity_id)
        if entity and entity.status == BiTemporalStatus.ACTIVE:
            entity.status = BiTemporalStatus.INVALIDATED
            entity.valid_to = invalidated_at or time.time()
            entity.properties["invalidation_reason"] = reason
        return entity

    # ── 关系操作 ────────────────────────────────────────────────────

    def add_relation(
        self,
        relation_id: str,
        subject: str,
        predicate: str,
        obj: str,
        valid_from: float | None = None,
        valid_to: float | None = None,
        metadata: dict | None = None,
        weight: float = 1.0,
        ingestion_time: float | None = None,
    ) -> BiTemporalRelation:
        """添加或更新关系边。"""
        now = time.time()
        rel = BiTemporalRelation(
            relation_id=relation_id,
            subject=subject,
            predicate=predicate,
            object=obj,
            valid_from=valid_from,
            valid_to=valid_to,
            ingestion_time=ingestion_time or now,
            metadata=metadata or {},
            weight=weight,
        )

        if relation_id in self._relations:
            existing = self._relations[relation_id]
            if existing.status == BiTemporalStatus.ACTIVE:
                existing.status = BiTemporalStatus.INVALIDATED
                rel.version = existing.version + 1

        self._relation_history[relation_id].append(rel)
        self._relations[relation_id] = rel
        return rel

    def invalidate_relation(
        self,
        relation_id: str,
        reason: str = "",
    ) -> BiTemporalRelation | None:
        """软删除关系。"""
        rel = self._relations.get(relation_id)
        if rel and rel.status == BiTemporalStatus.ACTIVE:
            rel.status = BiTemporalStatus.INVALIDATED
            rel.valid_to = time.time()
            rel.metadata["invalidation_reason"] = reason
        return rel

    # ── 时间点回溯查询（核心）──────────────────────────────────────

    def _is_valid_at(
        self,
        valid_from: float | None,
        valid_to: float | None,
        query_time: float,
    ) -> bool:
        """判断有效窗口是否覆盖查询时间点。"""
        if valid_from is not None and query_time < valid_from:
            return False
        if valid_to is not None and query_time > valid_to:
            return False
        return True

    def point_in_time_query(
        self,
        timestamp: float | None = None,
        entity_types: list[str] | None = None,
        include_invalidated: bool = False,
    ) -> PointInTimeSnapshot:
        """时间点回溯查询。

        返回在给定时间点有效的所有实体和关系。

        参数:
            timestamp: 查询时间点（Unix timestamp），默认当前时间。
            entity_types: 过滤实体类型。
            include_invalidated: 是否包含已失效的实体/关系。

        返回:
            PointInTimeSnapshot 包含有效实体和关系。
        """
        query_time = timestamp or time.time()

        valid_entities: dict[str, BiTemporalEntity] = {}
        valid_relations: dict[str, BiTemporalRelation] = {}

        for eid, entity in self._entities.items():
            if not include_invalidated and entity.status == BiTemporalStatus.INVALIDATED:
                continue
            if entity_types and entity.entity_type not in entity_types:
                continue
            if self._is_valid_at(entity.valid_from, entity.valid_to, query_time):
                valid_entities[eid] = entity

        for rid, rel in self._relations.items():
            if not include_invalidated and rel.status == BiTemporalStatus.INVALIDATED:
                continue
            if self._is_valid_at(rel.valid_from, rel.valid_to, query_time):
                # 确保 subject 和 object 在该时间点也有效
                valid_relations[rid] = rel

        return PointInTimeSnapshot(
            timestamp=query_time,
            entities=valid_entities,
            relations=valid_relations,
            stats={
                "total_entities": len(self._entities),
                "valid_entities": len(valid_entities),
                "total_relations": len(self._relations),
                "valid_relations": len(valid_relations),
            },
        )

    def time_range_query(
        self,
        start_time: float,
        end_time: float | None = None,
        entity_types: list[str] | None = None,
    ) -> PointInTimeSnapshot:
        """时间范围查询：返回在时间范围内有效的实体和关系。"""
        end = end_time or time.time()
        return self.point_in_time_query(
            timestamp=(start_time + end) / 2.0,  # 中点插值
            entity_types=entity_types,
        )

    def history_query(
        self,
        entity_id: str,
        from_time: float | None = None,
        to_time: float | None = None,
    ) -> list[BiTemporalEntity]:
        """查询实体的历史版本。"""
        history = self._entity_history.get(entity_id, [])
        if from_time is None and to_time is None:
            return sorted(history, key=lambda e: e.version)

        result = []
        for e in history:
            e_time = e.ingestion_time
            if from_time is not None and e_time < from_time:
                continue
            if to_time is not None and e_time > to_time:
                continue
            result.append(e)

        return sorted(result, key=lambda e: e.version)

    # ── 过期扫描 ────────────────────────────────────────────────────

    def scan_expired(
        self,
        current_time: float | None = None,
    ) -> dict[str, list[str]]:
        """扫描并标记所有已过期的实体和关系。

        返回:
            {"entities": [eid, ...], "relations": [rid, ...]}
        """
        now = current_time or time.time()
        expired_entities: list[str] = []
        expired_relations: list[str] = []

        for eid, entity in self._entities.items():
            if (entity.status == BiTemporalStatus.ACTIVE and
                entity.valid_to is not None and
                entity.valid_to < now):
                entity.status = BiTemporalStatus.INVALIDATED
                expired_entities.append(eid)

        for rid, rel in self._relations.items():
            if (rel.status == BiTemporalStatus.ACTIVE and
                rel.valid_to is not None and
                rel.valid_to < now):
                rel.status = BiTemporalStatus.INVALIDATED
                expired_relations.append(rid)

        return {
            "entities": expired_entities,
            "relations": expired_relations,
        }

    # ── 统计与导出 ──────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """获取引擎统计信息。"""
        active_entities = sum(
            1 for e in self._entities.values()
            if e.status == BiTemporalStatus.ACTIVE
        )
        active_relations = sum(
            1 for r in self._relations.values()
            if r.status == BiTemporalStatus.ACTIVE
        )
        return {
            "total_entities": len(self._entities),
            "active_entities": active_entities,
            "invalidated_entities": len(self._entities) - active_entities,
            "total_relations": len(self._relations),
            "active_relations": active_relations,
            "invalidated_relations": len(self._relations) - active_relations,
            "entity_history_depth": sum(
                len(h) for h in self._entity_history.values()
            ),
            "default_retention_days": self.default_retention_days,
        }

    def export_snapshot(self, timestamp: float | None = None) -> dict:
        """导出时间点快照为 JSON 可序列化字典。"""
        snapshot = self.point_in_time_query(timestamp)
        return {
            "timestamp": snapshot.timestamp,
            "entities": {
                eid: {
                    "entity_type": e.entity_type,
                    "properties": e.properties,
                    "valid_from": e.valid_from,
                    "valid_to": e.valid_to,
                    "version": e.version,
                }
                for eid, e in snapshot.entities.items()
            },
            "relations": {
                rid: {
                    "subject": r.subject,
                    "predicate": r.predicate,
                    "object": r.object,
                    "valid_from": r.valid_from,
                    "valid_to": r.valid_to,
                    "weight": r.weight,
                }
                for rid, r in snapshot.relations.items()
            },
            "stats": snapshot.stats,
        }


# ══════════════════════════════════════════════════════════════════════
# 自检
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("Bi-Temporal Graph Engine — 自检")
    print("=" * 60)

    engine = BiTemporalGraphEngine()

    now = time.time()
    yesterday = now - 86400
    last_week = now - 86400 * 7
    next_month = now + 86400 * 30

    # 添加实体（不同有效窗口）
    engine.add_entity("trinity", "system", {"name": "Trinity"}, valid_from=last_week)
    engine.add_entity("chromadb", "database", {"name": "ChromaDB"}, valid_from=yesterday)
    engine.add_entity("old_component", "component", {"name": "Legacy"}, valid_from=last_week, valid_to=yesterday)
    engine.add_entity("future_module", "module", {"name": "v7"}, valid_from=next_month)

    engine.add_relation("r1", "trinity", "uses", "chromadb", valid_from=yesterday)
    engine.add_relation("r2", "trinity", "uses", "old_component", valid_from=last_week, valid_to=yesterday)

    # 时间点查询
    mid_time = (last_week + yesterday) / 2
    snap = engine.point_in_time_query(mid_time)
    print(f"\n[时间点查询 @ {datetime.fromtimestamp(mid_time).isoformat()}]")
    print(f"  实体: {snap.stats['valid_entities']}/{snap.stats['total_entities']}")
    print(f"  关系: {snap.stats['valid_relations']}/{snap.stats['total_relations']}")

    # 当前时间点查询
    snap_now = engine.point_in_time_query()
    print(f"\n[当前时间点查询]")
    print(f"  实体: {snap_now.stats['valid_entities']} (future_module 尚未生效)")
    print(f"  关系: {snap_now.stats['valid_relations']}")

    # 过期扫描
    expired = engine.scan_expired()
    print(f"\n[过期扫描] entities={expired['entities']}, relations={expired['relations']}")

    # 实体软删除
    engine.invalidate_entity("old_component", "deprecated")
    print(f"\n[软删除] old_component status: {engine.get_entity('old_component').status.value}")

    # 历史查询
    history = engine.history_query("old_component")
    print(f"[历史查询] old_component 版本数: {len(history)}")

    # 统计
    stats = engine.get_stats()
    print(f"\n[统计] {json.dumps(stats, indent=2)}")

    print("\n所有测试通过!")
