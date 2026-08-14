"""
TransferableMemoryOS — MindMemOS Cross-Agent Transferable Memory OS
====================================================================
华为诺亚方舟, Aug 2026 · P42-1

实现 MindMemOS 可迁移记忆操作系统: entity_attribute_time_model 实体-属性-时间
三维记忆结构, mind_schema 可配置记忆提取规则, feedback_driven_evolution 从用户
纠偏中演进, cross_agent_migration 跨框架跨应用记忆迁移复用。

设计要点:
  - EntityAttributeTimeModel: 实体/属性/时间三维结构 + 演化轨迹
  - MindSchema: 可配置提取规则, 围绕任务定制
  - FeedbackDrivenEvolution: 从纠偏信号持续优化
  - CrossAgentMigration: Agent 解耦, 跨框架迁移
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AttributeType(Enum):
    """属性类型。"""
    STRING = auto()
    NUMERIC = auto()
    BOOLEAN = auto()
    CATEGORICAL = auto()
    TEMPORAL = auto()


class SchemaAction(Enum):
    """Schema 提取动作。"""
    EXTRACT = auto()
    IGNORE = auto()
    DERIVE = auto()


class FeedbackSource(Enum):
    """反馈来源。"""
    USER_CORRECTION = auto()
    TASK_FAILURE = auto()
    CONSISTENCY_CHECK = auto()
    PERFORMANCE_DEGRADATION = auto()


class MigrationStatus(Enum):
    """迁移状态。"""
    PENDING = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    FAILED = auto()
    ROLLED_BACK = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class MMOS_EntityRecord:
    """MindMemOS 实体记录 (重命名: EntityRecord→MMOS_EntityRecord 避免冲突)。"""
    entity_id: str
    entity_type: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class AttributeRecord:
    """属性记录——属性级别的变更历史。"""
    attr_id: str
    entity_id: str
    attr_name: str
    old_value: Any
    new_value: Any
    attr_type: AttributeType = AttributeType.STRING
    timestamp: float = field(default_factory=time.time)


@dataclass
class TimeSliceRecord:
    """时间切片——某一时刻的实体快照。"""
    slice_id: str
    entity_id: str
    snapshot: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)


@dataclass
class EvolutionTrajectory:
    """演化轨迹——实体从创建到当前的所有变更。"""
    entity_id: str
    slices: List[TimeSliceRecord] = field(default_factory=list)
    attribute_history: List[AttributeRecord] = field(default_factory=list)
    version: int = 0


@dataclass
class ExtractionRule:
    """一条提取规则——MindSchema 的关键组件。"""
    rule_id: str
    target_entity_type: str
    attributes_to_extract: List[str] = field(default_factory=list)
    relationships_to_extract: List[str] = field(default_factory=list)
    action: SchemaAction = SchemaAction.EXTRACT
    priority: int = 0
    enabled: bool = True


@dataclass
class SchemaConfig:
    """MindSchema 可配置记忆提取规则。"""
    config_id: str
    task_name: str
    rules: List[ExtractionRule] = field(default_factory=list)
    entity_types: List[str] = field(default_factory=list)
    relationship_types: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


@dataclass
class MigrationManifest:
    """跨Agent迁移清单——迁移的完整元数据。"""
    manifest_id: str
    source_agent: str
    target_agent: str
    entity_ids: List[str] = field(default_factory=list)
    schema_config_id: str = ""
    status: MigrationStatus = MigrationStatus.PENDING
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None


# ---------------------------------------------------------------------------
# EntityAttributeTimeModel
# ---------------------------------------------------------------------------

class EntityAttributeTimeModel:
    """实体-属性-时间三维记忆结构。

    同时保存每个实体的最新状态与完整演化轨迹。

    Parameters
    ----------
    max_trajectory_length : int
        每个实体最大演化轨迹切片数。
    """

    def __init__(self, max_trajectory_length: int = 100) -> None:
        self.max_trajectory_length = max_trajectory_length
        self._entities: Dict[str, MMOS_EntityRecord] = {}
        self._trajectories: Dict[str, EvolutionTrajectory] = {}
        self._attr_history: Dict[str, List[AttributeRecord]] = {}
        self._lock = threading.RLock()
        self._entity_count: int = 0

    def create_entity(self, entity_type: str, initial_attrs: Optional[Dict[str, Any]] = None) -> MMOS_EntityRecord:
        """创建实体并初始化演化轨迹。"""
        with self._lock:
            self._entity_count += 1
            eid = f"e_{self._entity_count}_{int(time.time()*1e6)}"
            entity = MMOS_EntityRecord(
                entity_id=eid,
                entity_type=entity_type,
                attributes=initial_attrs or {},
            )
            self._entities[eid] = entity

            # 初始化演化轨迹
            trajectory = EvolutionTrajectory(entity_id=eid, version=1)
            if initial_attrs:
                ts = TimeSliceRecord(
                    slice_id=f"ts_{eid}_v1",
                    entity_id=eid,
                    snapshot=dict(initial_attrs),
                )
                trajectory.slices.append(ts)
            self._trajectories[eid] = trajectory
            self._attr_history[eid] = []
            return entity

    def update_attribute(self, entity_id: str, attr_name: str, new_value: Any) -> Optional[AttributeRecord]:
        """更新属性——记录变更历史与新切片。"""
        with self._lock:
            entity = self._entities.get(entity_id)
            if not entity:
                return None

            old_value = entity.attributes.get(attr_name)
            entity.attributes[attr_name] = new_value
            entity.updated_at = time.time()

            # 属性变更记录
            attr_rec = AttributeRecord(
                attr_id=f"attr_{entity_id}_{attr_name}_{int(time.time()*1e6)}",
                entity_id=entity_id,
                attr_name=attr_name,
                old_value=old_value,
                new_value=new_value,
                attr_type=_infer_attr_type(new_value),
            )
            self._attr_history[entity_id].append(attr_rec)

            # 时间切片
            traj = self._trajectories[entity_id]
            traj.version += 1
            traj.attribute_history.append(attr_rec)
            ts = TimeSliceRecord(
                slice_id=f"ts_{entity_id}_v{traj.version}",
                entity_id=entity_id,
                snapshot=dict(entity.attributes),
            )
            traj.slices.append(ts)
            if len(traj.slices) > self.max_trajectory_length:
                traj.slices.pop(0)

            return attr_rec

    def get_entity(self, entity_id: str) -> Optional[MMOS_EntityRecord]:
        """获取实体最新状态。"""
        return self._entities.get(entity_id)

    def get_trajectory(self, entity_id: str) -> Optional[EvolutionTrajectory]:
        """获取实体完整演化轨迹。"""
        return self._trajectories.get(entity_id)

    def query_by_type(self, entity_type: str) -> List[MMOS_EntityRecord]:
        """按类型检索所有实体。"""
        return [e for e in self._entities.values() if e.entity_type == entity_type]

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_entities": len(self._entities),
            "total_trajectories": len(self._trajectories),
        }


# ---------------------------------------------------------------------------
# MindSchema
# ---------------------------------------------------------------------------

class MindSchema:
    """可配置的记忆提取规则Schema。

    围绕给定任务定制实体/属性/关系提取。

    Parameters
    ----------
    config_capacity : int
        最大Schema配置数。
    """

    def __init__(self, config_capacity: int = 20) -> None:
        self.config_capacity = config_capacity
        self._configs: Dict[str, SchemaConfig] = {}
        self._lock = threading.RLock()
        self._config_count: int = 0

    def create_schema(self, task_name: str, rules: Optional[List[ExtractionRule]] = None) -> SchemaConfig:
        """为任务创建自定义提取Schema。"""
        with self._lock:
            if len(self._configs) >= self.config_capacity:
                oldest = min(self._configs.items(), key=lambda x: x[1].created_at)
                del self._configs[oldest[0]]

            self._config_count += 1
            config = SchemaConfig(
                config_id=f"schema_{self._config_count}_{int(time.time()*1e6)}",
                task_name=task_name,
                rules=rules or [],
            )
            self._configs[config.config_id] = config
            return config

    def add_rule(self, config_id: str, rule: ExtractionRule) -> bool:
        """向Schema添加规则。"""
        config = self._configs.get(config_id)
        if not config:
            return False
        config.rules.append(rule)
        return True

    def apply_schema(
        self, config_id: str, entities: List[MMOS_EntityRecord]
    ) -> Dict[str, Dict[str, Any]]:
        """应用Schema提取——返回实体→提取属性映射。"""
        config = self._configs.get(config_id)
        if not config:
            return {}

        result: Dict[str, Dict[str, Any]] = {}
        enabled_rules = [r for r in config.rules if r.enabled and r.action != SchemaAction.IGNORE]
        enabled_rules.sort(key=lambda r: r.priority, reverse=True)

        for entity in entities:
            if entity.entity_type not in {r.target_entity_type for r in enabled_rules}:
                continue
            extracted: Dict[str, Any] = {}
            for rule in enabled_rules:
                if rule.target_entity_type != entity.entity_type:
                    continue
                for attr in rule.attributes_to_extract:
                    if attr in entity.attributes:
                        extracted[attr] = entity.attributes[attr]
            if extracted:
                result[entity.entity_id] = extracted

        return result

    def get_schema(self, config_id: str) -> Optional[SchemaConfig]:
        return self._configs.get(config_id)

    def statistics(self) -> Dict[str, Any]:
        return {"total_schemas": len(self._configs)}


# ---------------------------------------------------------------------------
# FeedbackDrivenEvolution
# ---------------------------------------------------------------------------

class FeedbackDrivenEvolution:
    """从用户纠偏和真实使用中持续演进提取与检索策略。

    Parameters
    ----------
    learning_rate : float
        规则权重更新率。
    """

    def __init__(self, learning_rate: float = 0.05) -> None:
        self.learning_rate = learning_rate
        self._feedback_log: deque = deque(maxlen=500)
        self._rule_stats: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"hits": 0.0, "misses": 0.0, "accuracy": 0.5}
        )
        self._lock = threading.RLock()

    def record_feedback(
        self,
        source: FeedbackSource,
        rule_id: str,
        was_correct: bool,
        detail: str = "",
    ) -> None:
        """记录反馈信号。"""
        with self._lock:
            self._feedback_log.append({
                "timestamp": time.time(),
                "source": source,
                "rule_id": rule_id,
                "was_correct": was_correct,
                "detail": detail,
            })
            stats = self._rule_stats[rule_id]
            if was_correct:
                stats["hits"] += 1.0
            else:
                stats["misses"] += 1.0
            total = stats["hits"] + stats["misses"]
            if total > 0:
                stats["accuracy"] = stats["hits"] / total

    def evolve_rules(
        self, schemas: Dict[str, SchemaConfig]
    ) -> Dict[str, float]:
        """基于累积反馈演进提取规则——返回规则置信度更新。

        Returns
        -------
        Dict[str, float]
            规则ID → 新置信度。
        """
        updates: Dict[str, float] = {}
        with self._lock:
            for rule_id, stats in self._rule_stats.items():
                new_conf = stats["accuracy"]
                old_conf = 0.5
                updated_conf = old_conf + self.learning_rate * (new_conf - old_conf)
                updates[rule_id] = updated_conf

            # 应用更新到Schema规则
            for config in schemas.values():
                for rule in config.rules:
                    if rule.rule_id in updates:
                        # 低准确率规则禁用
                        if updates[rule.rule_id] < 0.3:
                            rule.enabled = False
                            logger.info("Rule %s disabled (accuracy=%.2f)", rule.rule_id, updates[rule.rule_id])

            return updates

    def get_rule_accuracy(self, rule_id: str) -> float:
        return self._rule_stats.get(rule_id, {}).get("accuracy", 0.5)

    def statistics(self) -> Dict[str, Any]:
        return {
            "feedback_entries": len(self._feedback_log),
            "tracked_rules": len(self._rule_stats),
        }


# ---------------------------------------------------------------------------
# CrossAgentMigration
# ---------------------------------------------------------------------------

class CrossAgentMigration:
    """记忆从单个Agent解耦，跨框架跨应用迁移复用。

    Parameters
    ----------
    supported_formats : List[str]
        支持的迁移格式 (如 ["json", "pickle", "parquet"])。
    """

    def __init__(self, supported_formats: Optional[List[str]] = None) -> None:
        self.supported_formats = supported_formats or ["json", "pickle"]
        self._manifests: Dict[str, MigrationManifest] = {}
        self._lock = threading.RLock()
        self._migration_count: int = 0

    def create_migration_manifest(
        self,
        source_agent: str,
        target_agent: str,
        entity_ids: List[str],
        schema_config_id: str = "",
    ) -> MigrationManifest:
        """创建跨Agent迁移清单。"""
        with self._lock:
            self._migration_count += 1
            manifest = MigrationManifest(
                manifest_id=f"mig_{self._migration_count}_{int(time.time()*1e6)}",
                source_agent=source_agent,
                target_agent=target_agent,
                entity_ids=entity_ids,
                schema_config_id=schema_config_id,
            )
            self._manifests[manifest.manifest_id] = manifest
            return manifest

    def export_entities(
        self,
        manifest_id: str,
        entity_model: EntityAttributeTimeModel,
        output_format: str = "json",
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """导出实体及其轨迹——Agent解耦格式。

        Returns
        -------
        Tuple[Optional[Dict], str]
            (导出数据, 消息)。
        """
        manifest = self._manifests.get(manifest_id)
        if not manifest:
            return None, "Manifest not found"

        if output_format not in self.supported_formats:
            return None, f"Unsupported format: {output_format}"

        export_data: Dict[str, Any] = {
            "manifest_id": manifest_id,
            "source_agent": manifest.source_agent,
            "format": output_format,
            "entities": {},
        }

        for eid in manifest.entity_ids:
            entity = entity_model.get_entity(eid)
            trajectory = entity_model.get_trajectory(eid)
            if entity:
                export_data["entities"][eid] = {
                    "type": entity.entity_type,
                    "attributes": entity.attributes,
                    "created_at": entity.created_at,
                    "version": trajectory.version if trajectory else 1,
                    "snapshots": [
                        {"time": s.timestamp, "data": s.snapshot}
                        for s in (trajectory.slices if trajectory else [])
                    ],
                }

        manifest.status = MigrationStatus.COMPLETED
        manifest.completed_at = time.time()

        logger.info("Migration exported: %d entities from %s", len(export_data["entities"]), manifest.source_agent)
        return export_data, "OK"

    def import_entities(
        self,
        data: Dict[str, Any],
        target_agent: str,
        entity_model: EntityAttributeTimeModel,
    ) -> Tuple[int, str]:
        """导入跨Agent数据——创建实体并重建轨迹。

        Returns
        -------
        Tuple[int, str]
            (导入实体数, 消息)。
        """
        count = 0
        try:
            entities_data = data.get("entities", {})
            for eid, edata in entities_data.items():
                entity = entity_model.create_entity(
                    entity_type=edata["type"],
                    initial_attrs=edata.get("attributes", {}),
                )
                # 重建轨迹快照
                for snap in edata.get("snapshots", []):
                    entity_model.update_attribute(entity.entity_id, "__snapshot__", snap.get("data", {}))
                count += 1

            logger.info("Migration imported: %d entities to %s", count, target_agent)
            return count, "OK"
        except Exception as e:
            return count, f"Import failed: {e}"

    def statistics(self) -> Dict[str, Any]:
        return {"total_migrations": self._migration_count}


# ---------------------------------------------------------------------------
# TransferableMemoryOS
# ---------------------------------------------------------------------------

class TransferableMemoryOS:
    """MindMemOS 可迁移记忆操作系统。

    Parameters
    ----------
    max_trajectory_length : int
        每个实体最大演化轨迹切片数。
    schema_capacity : int
        最大Schema配置数。
    feedback_lr : float
        反馈驱动演化的学习率。
    """

    def __init__(
        self,
        max_trajectory_length: int = 100,
        schema_capacity: int = 20,
        feedback_lr: float = 0.05,
    ) -> None:
        self.entity_attribute_time_model = EntityAttributeTimeModel(
            max_trajectory_length=max_trajectory_length,
        )
        self.mind_schema = MindSchema(config_capacity=schema_capacity)
        self.feedback_driven_evolution = FeedbackDrivenEvolution(
            learning_rate=feedback_lr,
        )
        self.cross_agent_migration = CrossAgentMigration()
        self._lock = threading.RLock()

        logger.info(
            "TransferableMemoryOS initialized [traj=%d schema=%d lr=%.3f]",
            max_trajectory_length, schema_capacity, feedback_lr,
        )

    def create_entity_with_schema(
        self,
        entity_type: str,
        initial_attrs: Dict[str, Any],
        schema_config_id: str,
    ) -> Optional[MMOS_EntityRecord]:
        """创建实体并根据Schema验证/补充属性。"""
        entity = self.entity_attribute_time_model.create_entity(entity_type, initial_attrs)
        extracted = self.mind_schema.apply_schema(schema_config_id, [entity])
        if extracted:
            for attr_name, attr_val in extracted.get(entity.entity_id, {}).items():
                if attr_name not in entity.attributes:
                    self.entity_attribute_time_model.update_attribute(entity.entity_id, attr_name, attr_val)
        return entity

    def evolve_from_feedback(
        self, source: FeedbackSource, rule_id: str, was_correct: bool, detail: str = ""
    ) -> None:
        """从反馈中演进。"""
        self.feedback_driven_evolution.record_feedback(source, rule_id, was_correct, detail)

    def migrate_to_agent(
        self, source_agent: str, target_agent: str, entity_ids: List[str]
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """执行跨Agent迁移——导出数据。"""
        manifest = self.cross_agent_migration.create_migration_manifest(
            source_agent, target_agent, entity_ids,
        )
        return self.cross_agent_migration.export_entities(
            manifest.manifest_id, self.entity_attribute_time_model,
        )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "entities": self.entity_attribute_time_model.statistics()["total_entities"],
                "schemas": self.mind_schema.statistics()["total_schemas"],
                "feedback_entries": self.feedback_driven_evolution.statistics()["feedback_entries"],
                "migrations": self.cross_agent_migration.statistics()["total_migrations"],
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_attr_type(value: Any) -> AttributeType:
    if isinstance(value, bool):
        return AttributeType.BOOLEAN
    if isinstance(value, (int, float)):
        return AttributeType.NUMERIC
    if isinstance(value, str):
        return AttributeType.STRING
    return AttributeType.CATEGORICAL
