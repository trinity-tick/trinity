"""
AdaMMAnalyticMemory — AdaMM Analytic Memory for Multimodal Agents
==================================================================
arXiv 2607.29440v2, Jul 31 2026 · P44-1

实现分析型记忆: 超越纯检索, 支持过滤/聚合/排序/时序比较的分析查询。
从对话/图片/元数据中提取溯源链属性-值观察, 自动发现重复字段结构并物化。
Memory-aware Planner 将查询分解为检索+分析操作并路由。MemEye/MemGallery 验证。

设计要点:
  - AttributeValueObservation: 溯源链属性-值观察
  - FieldStructureDiscovery: 自动发现重复字段结构
  - MaterializedFieldStore: 物化字段存储
  - MemoryAwarePlanner: 查询分解+操作路由
  - AnalyticQueryEngine: 过滤/聚合/排序/时序比较
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque
import re
import json

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AnalyticQueryType(Enum):
    """分析查询类型。"""
    FILTER = auto()
    AGGREGATE = auto()
    SORT = auto()
    TEMPORAL_COMPARE = auto()
    COMPOSITE = auto()


class AnalyticOperation(Enum):
    """分析操作原子。"""
    COUNT = auto()
    SUM = auto()
    AVG = auto()
    MAX = auto()
    MIN = auto()
    GROUP_BY = auto()
    ORDER_BY = auto()
    FILTER_BY = auto()
    TIME_RANGE = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ObservationProvenance:
    """溯源追踪——记录观察的来源。"""
    source_type: str = ""  # dialogue / image / metadata
    source_id: str = ""
    timestamp: float = field(default_factory=time.time)
    extraction_method: str = ""
    confidence: float = 1.0


@dataclass
class AttributeValueObservation:
    """属性-值观察——从交互中提取的结构化观察。"""
    observation_id: str
    attribute: str
    value: Any
    provenance: ObservationProvenance = field(default_factory=ObservationProvenance)
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# FieldStructureDiscovery
# ---------------------------------------------------------------------------

class FieldStructureDiscovery:
    """自动发现重复字段结构并物化。

    Parameters
    ----------
    min_occurrence : int
        最小出现次数才触发发现。
    """

    def __init__(self, min_occurrence: int = 3) -> None:
        self.min_occurrence = min_occurrence
        self._field_frequencies: Dict[str, int] = defaultdict(int)
        self._discovered_schemas: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def observe(self, observations: List[AttributeValueObservation]) -> Dict[str, Any]:
        """观察新数据并发现结构。"""
        with self._lock:
            for obs in observations:
                self._field_frequencies[obs.attribute] += 1

            discovered: Dict[str, Any] = {}
            for attr, freq in self._field_frequencies.items():
                if freq >= self.min_occurrence and attr not in self._discovered_schemas:
                    schema = {
                        "field_name": attr,
                        "occurrence_count": freq,
                        "discovered_at": time.time(),
                        "value_type": self._infer_type(attr),
                    }
                    self._discovered_schemas[attr] = schema
                    discovered[attr] = schema
                    logger.info("Discovered field structure: '%s' (%d occurrences)", attr, freq)

            return {"newly_discovered": len(discovered), "total_schemas": len(self._discovered_schemas)}

    def _infer_type(self, attr: str) -> str:
        """推断字段类型。"""
        lower = attr.lower()
        if any(kw in lower for kw in ("count", "num", "age", "id", "price", "amount")):
            return "numeric"
        if any(kw in lower for kw in ("date", "time", "timestamp", "when")):
            return "temporal"
        if any(kw in lower for kw in ("is_", "has_", "can_", "bool")):
            return "boolean"
        return "string"

    def statistics(self) -> Dict[str, Any]:
        return {
            "discovered_schemas": len(self._discovered_schemas),
            "field_frequencies": dict(self._field_frequencies),
        }


# ---------------------------------------------------------------------------
# MaterializedFieldStore
# ---------------------------------------------------------------------------

class MaterializedFieldStore:
    """物化字段存储——将重复字段物化为可查询结构。"""

    def __init__(self) -> None:
        self._materialized: Dict[str, List[Any]] = defaultdict(list)
        self._indexes: Dict[str, Dict[str, List[int]]] = defaultdict(dict)
        self._lock = threading.RLock()

    def materialize(self, field_name: str, values: List[Any]) -> int:
        """物化字段值。"""
        with self._lock:
            self._materialized[field_name].extend(values)
            return len(self._materialized[field_name])

    def query(self, field_name: str, operation: AnalyticOperation, **kwargs: Any) -> Any:
        """对物化字段执行分析操作。"""
        values = self._materialized.get(field_name, [])
        if not values:
            return None

        numeric_vals = [v for v in values if isinstance(v, (int, float))]

        if operation == AnalyticOperation.COUNT:
            return len(values)
        if operation == AnalyticOperation.SUM and numeric_vals:
            return sum(numeric_vals)
        if operation == AnalyticOperation.AVG and numeric_vals:
            return sum(numeric_vals) / len(numeric_vals)
        if operation == AnalyticOperation.MAX and numeric_vals:
            return max(numeric_vals)
        if operation == AnalyticOperation.MIN and numeric_vals:
            return min(numeric_vals)
        if operation == AnalyticOperation.FILTER_BY:
            threshold = kwargs.get("threshold")
            if threshold is not None and numeric_vals:
                return [v for v in numeric_vals if v >= threshold]

        return values

    def statistics(self) -> Dict[str, Any]:
        return {"materialized_fields": len(self._materialized)}


# ---------------------------------------------------------------------------
# QueryDecomposer
# ---------------------------------------------------------------------------

class QueryDecomposer:
    """查询分解器——将自然语言查询分解为检索+分析操作。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._op_keyword_map = {
            "count": AnalyticOperation.COUNT,
            "how many": AnalyticOperation.COUNT,
            "sum": AnalyticOperation.SUM,
            "total": AnalyticOperation.SUM,
            "average": AnalyticOperation.AVG,
            "avg": AnalyticOperation.AVG,
            "maximum": AnalyticOperation.MAX,
            "max": AnalyticOperation.MAX,
            "minimum": AnalyticOperation.MIN,
            "min": AnalyticOperation.MIN,
            "filter": AnalyticOperation.FILTER_BY,
            "where": AnalyticOperation.FILTER_BY,
            "sort": AnalyticOperation.ORDER_BY,
            "order": AnalyticOperation.ORDER_BY,
            "group": AnalyticOperation.GROUP_BY,
            "time range": AnalyticOperation.TIME_RANGE,
        }

    def decompose(self, query: str) -> Dict[str, Any]:
        """分解查询为操作序列。"""
        with self._lock:
            query_lower = query.lower()
            operations: List[Dict[str, Any]] = []

            for keyword, op in self._op_keyword_map.items():
                if keyword in query_lower:
                    operations.append({"operation": op.name, "trigger": keyword})

            # 提取属性名
            attributes = self._extract_attributes(query)

            return {
                "original_query": query,
                "operations": operations,
                "attributes": attributes,
                "query_type": self._classify(operations),
            }

    def _extract_attributes(self, query: str) -> List[str]:
        """从查询中提取属性名。"""
        patterns = [
            r'of\s+(\w+)', r'for\s+(\w+)', r'by\s+(\w+)',
            r'(\w+)\s+(?:greater|less|above|below)',
        ]
        attrs: Set[str] = set()
        for pat in patterns:
            matches = re.findall(pat, query.lower())
            attrs.update(m for m in matches if len(m) > 2)
        return list(attrs)[:5]

    def _classify(self, operations: List[Dict[str, Any]]) -> str:
        if not operations:
            return AnalyticQueryType.COMPOSITE.name
        op_names = {o["operation"] for o in operations}
        if "FILTER_BY" in op_names and len(op_names) == 1:
            return AnalyticQueryType.FILTER.name
        if op_names & {"COUNT", "SUM", "AVG", "MAX", "MIN"}:
            return AnalyticQueryType.AGGREGATE.name
        if "ORDER_BY" in op_names:
            return AnalyticQueryType.SORT.name
        if "TIME_RANGE" in op_names:
            return AnalyticQueryType.TEMPORAL_COMPARE.name
        return AnalyticQueryType.COMPOSITE.name

    def statistics(self) -> Dict[str, Any]:
        return {"status": "ready"}


# ---------------------------------------------------------------------------
# MemoryAwarePlanner
# ---------------------------------------------------------------------------

class MemoryAwarePlanner:
    """Memory-aware Planner——将查询分解为检索+分析操作并路由。"""

    def __init__(self) -> None:
        self.query_decomposer = QueryDecomposer()
        self._lock = threading.RLock()

    def plan(self, query: str, field_store: MaterializedFieldStore, observations: List[AttributeValueObservation]) -> Dict[str, Any]:
        """规划查询执行——分解并路由。

        Returns
        -------
        Dict[str, Any]
            规划结果: {retrieval_ops, analytic_ops, routed_to}
        """
        with self._lock:
            decomposed = self.query_decomposer.decompose(query)

            retrieval_ops: List[Dict[str, Any]] = []
            analytic_ops: List[Dict[str, Any]] = []

            for attr in decomposed.get("attributes", []):
                if field_store.query(attr, AnalyticOperation.COUNT):
                    # 有物化数据 → 分析操作
                    for op_info in decomposed.get("operations", []):
                        analytic_ops.append({"field": attr, "operation": op_info["operation"]})
                else:
                    # 无物化数据 → 检索操作
                    for obs in observations:
                        if attr.lower() in obs.attribute.lower():
                            retrieval_ops.append({
                                "observation_id": obs.observation_id,
                                "attribute": obs.attribute,
                                "value": obs.value,
                            })

            return {
                "query": query,
                "query_type": decomposed.get("query_type"),
                "retrieval_ops": retrieval_ops,
                "analytic_ops": analytic_ops,
                "routed_to": "analytic" if analytic_ops else "retrieval",
            }

    def statistics(self) -> Dict[str, Any]:
        return {"status": "ready"}


# ---------------------------------------------------------------------------
# AnalyticQueryEngine
# ---------------------------------------------------------------------------

class AnalyticQueryEngine:
    """分析查询引擎——执行过滤/聚合/排序/时序比较。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def filter(
        self, observations: List[AttributeValueObservation], attribute: str, condition: Callable[[Any], bool]
    ) -> List[AttributeValueObservation]:
        """过滤观察。"""
        return [o for o in observations if o.attribute == attribute and condition(o.value)]

    def aggregate(
        self, observations: List[AttributeValueObservation], attribute: str, operation: AnalyticOperation
    ) -> Any:
        """聚合观察。"""
        values = [o.value for o in observations if o.attribute == attribute and isinstance(o.value, (int, float))]
        if not values:
            return 0
        if operation == AnalyticOperation.COUNT:
            return len(values)
        if operation == AnalyticOperation.SUM:
            return sum(values)
        if operation == AnalyticOperation.AVG:
            return sum(values) / len(values)
        if operation == AnalyticOperation.MAX:
            return max(values)
        if operation == AnalyticOperation.MIN:
            return min(values)
        return values

    def sort(
        self, observations: List[AttributeValueObservation], attribute: str, ascending: bool = True
    ) -> List[AttributeValueObservation]:
        """排序观察。"""
        relevant = [o for o in observations if o.attribute == attribute]
        relevant.sort(key=lambda o: str(o.value))
        if not ascending:
            relevant.reverse()
        return relevant

    def temporal_compare(
        self, observations: List[AttributeValueObservation], attribute: str
    ) -> Dict[str, Any]:
        """时序比较。"""
        relevant = sorted(
            [o for o in observations if o.attribute == attribute],
            key=lambda o: o.timestamp,
        )
        if len(relevant) < 2:
            return {"trend": "insufficient_data"}

        first_val = relevant[0].value
        last_val = relevant[-1].value

        if isinstance(first_val, (int, float)) and isinstance(last_val, (int, float)):
            if last_val > first_val:
                return {"trend": "increasing", "delta": last_val - first_val}
            if last_val < first_val:
                return {"trend": "decreasing", "delta": first_val - last_val}
            return {"trend": "stable", "delta": 0}

        return {"trend": "changed", "from": first_val, "to": last_val}

    def statistics(self) -> Dict[str, Any]:
        return {"status": "ready"}


# ---------------------------------------------------------------------------
# AdaMMAnalyticMemory
# ---------------------------------------------------------------------------

class AdaMMAnalyticMemory:
    """AdaMM 分析型记忆系统——检索+分析双模式。

    Parameters
    ----------
    min_field_occurrence : int
        字段结构发现的最小出现次数。
    """

    def __init__(self, min_field_occurrence: int = 3) -> None:
        self.field_structure_discovery = FieldStructureDiscovery(min_occurrence=min_field_occurrence)
        self.materialized_field_store = MaterializedFieldStore()
        self.memory_aware_planner = MemoryAwarePlanner()
        self.analytic_query_engine = AnalyticQueryEngine()
        self._observations: List[AttributeValueObservation] = []
        self._lock = threading.RLock()
        self._obs_count: int = 0

        logger.info("AdaMMAnalyticMemory initialized [min_field=%d]", min_field_occurrence)

    def observe(
        self, attribute: str, value: Any, source_type: str = "dialogue", source_id: str = ""
    ) -> AttributeValueObservation:
        """记录属性-值观察。"""
        with self._lock:
            self._obs_count += 1
            obs = AttributeValueObservation(
                observation_id=f"obs_{self._obs_count}_{int(time.time()*1e6)}",
                attribute=attribute,
                value=value,
                provenance=ObservationProvenance(source_type=source_type, source_id=source_id),
            )
            self._observations.append(obs)

            # 自动物化
            self.materialized_field_store.materialize(attribute, [value])

            # 定期结构发现 (每5条)
            if self._obs_count % 5 == 0:
                self.field_structure_discovery.observe(self._observations[-10:])

            return obs

    def query(self, query_text: str) -> Dict[str, Any]:
        """分析查询——MemoryAwarePlanner 分解并路由。"""
        plan = self.memory_aware_planner.plan(
            query_text, self.materialized_field_store, self._observations,
        )

        results: Dict[str, Any] = {"plan": plan}

        for op in plan.get("analytic_ops", []):
            field = op["field"]
            op_name = op["operation"]
            try:
                op_enum = AnalyticOperation[op_name]
                result = self.analytic_query_engine.aggregate(self._observations, field, op_enum)
                results.setdefault("analytic_results", {})[f"{field}.{op_name}"] = result
            except (KeyError, ValueError):
                pass

        return results

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_observations": len(self._observations),
                "schemas": self.field_structure_discovery.statistics()["discovered_schemas"],
                "materialized_fields": self.materialized_field_store.statistics()["materialized_fields"],
            }
