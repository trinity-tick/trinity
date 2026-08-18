"""
# status: orphan (2026-08-15 audit, not in runtime path)
P16-7: Memory Visualization
===========================

对标实时记忆可视化（LinkedIn 2026）— 交互式记忆视图与流式推送。

设计要点：
  - 力导向记忆图谱渲染：按网络类型着色 + 边权重厚度映射
  - 置信度实时仪表盘：红黄绿三色渐变，区间 [0,1]
  - AgentPrism 风格 Gantt 时间线：按时间轴排布记忆操作
  - WebSocket/SSE 流式推送：低延迟实时更新
  - 交互式推理树：可折叠子树 + 自动摘要节点

核心组件：
  - ForceGraphRenderer:    力导向图谱布局与渲染数据
  - ConfidenceDashboard:   置信度仪表盘（红黄绿渐变）
  - GanttTimelineBuilder:  记忆操作 Gantt 时间线构建
  - StreamPublisher:       WebSocket/SSE 流式推送接口
  - InferenceTreeBuilder:  可折叠推理树 + 自动摘要
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

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


# ============================================================================
# Enums
# ============================================================================

class NetworkType(Enum):
    """记忆网络类型（用于着色）。"""
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    WORKING = "working"
    LONG_TERM = "long_term"
    SHORT_TERM = "short_term"


class ConfidenceLevel(Enum):
    """置信度等级。"""
    HIGH = "high"       # 绿灯
    MEDIUM = "medium"   # 黄灯
    LOW = "low"         # 红灯


class StreamProtocol(Enum):
    """流式协议。"""
    WEBSOCKET = "websocket"
    SSE = "sse"
    POLLING = "polling"


class MemoryOperation(Enum):
    """记忆操作类型。"""
    ENCODE = "encode"
    RETRIEVE = "retrieve"
    CONSOLIDATE = "consolidate"
    PRUNE = "prune"
    REHEARSE = "rehearse"
    LINK = "link"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class GraphNode:
    """图谱节点。"""
    node_id: str
    label: str
    network_type: NetworkType
    confidence: float = 0.5
    x: float = 0.0
    y: float = 0.0
    size: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdge:
    """图谱边。"""
    edge_id: str
    source_id: str
    target_id: str
    weight: float = 0.5
    relation: str = "related_to"
    confidence: float = 0.5


@dataclass
class ForceGraphData:
    """力导向图谱数据。"""
    nodes: List[GraphNode] = field(default_factory=list)
    edges: List[GraphEdge] = field(default_factory=list)
    iteration: int = 0
    energy: float = 0.0


@dataclass
class ConfidenceSnapshot:
    """置信度快照。"""
    category: str
    value: float
    level: ConfidenceLevel
    timestamp: float = field(default_factory=time.time)


@dataclass
class GanttEvent:
    """Gantt 时间线事件。"""
    event_id: str
    operation: MemoryOperation
    start_ms: float
    end_ms: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InferenceTreeNode:
    """推理树节点。"""
    node_id: str
    content: str
    summary: str = ""
    children: List["InferenceTreeNode"] = field(default_factory=list)
    collapsed: bool = False
    confidence: float = 0.5


@dataclass
class StreamEvent:
    """流式推送事件。"""
    event_type: str
    payload: Any
    timestamp: float = field(default_factory=time.time)
    sequence: int = 0


# ============================================================================
# Core Components
# ============================================================================

class ForceGraphRenderer:
    """力导向图谱渲染器。

    使用简化力导向算法布局节点，按网络类型着色，边宽度映射权重。
    """

    NETWORK_COLORS: Dict[NetworkType, str] = {
        NetworkType.EPISODIC: "#2196F3",
        NetworkType.SEMANTIC: "#4CAF50",
        NetworkType.PROCEDURAL: "#FF9800",
        NetworkType.WORKING: "#9C27B0",
        NetworkType.LONG_TERM: "#607D8B",
        NetworkType.SHORT_TERM: "#00BCD4",
    }

    def __init__(self, width: int = 800, height: int = 600):
        self._lock = threading.RLock()
        self.width = width
        self.height = height
        self.repulsion = 5000.0
        self.attraction = 0.01
        self.damping = 0.9

    def layout(self, data: ForceGraphData, iterations: int = 100) -> ForceGraphData:
        with self._lock:
            for _ in range(iterations):
                forces: Dict[str, Tuple[float, float]] = {n.node_id: (0.0, 0.0) for n in data.nodes}

                # Repulsion
                for i, a in enumerate(data.nodes):
                    for b in data.nodes[i + 1:]:
                        dx = a.x - b.x
                        dy = a.y - b.y
                        dist = math.sqrt(dx * dx + dy * dy) + 1.0
                        fx = self.repulsion / (dist * dist) * (dx / dist)
                        fy = self.repulsion / (dist * dist) * (dy / dist)
                        fa = forces[a.node_id]
                        fb = forces[b.node_id]
                        forces[a.node_id] = (fa[0] + fx, fa[1] + fy)
                        forces[b.node_id] = (fb[0] - fx, fb[1] - fy)

                # Attraction
                for e in data.edges:
                    src = next((n for n in data.nodes if n.node_id == e.source_id), None)
                    tgt = next((n for n in data.nodes if n.node_id == e.target_id), None)
                    if src and tgt:
                        dx = tgt.x - src.x
                        dy = tgt.y - src.y
                        dist = math.sqrt(dx * dx + dy * dy) + 1.0
                        fx = self.attraction * dist * e.weight * dx / dist
                        fy = self.attraction * dist * e.weight * dy / dist
                        fa = forces[src.node_id]
                        fb = forces[tgt.node_id]
                        forces[src.node_id] = (fa[0] + fx, fa[1] + fy)
                        forces[tgt.node_id] = (fb[0] - fx, fb[1] - fy)

                # Center gravity
                cx = self.width / 2
                cy = self.height / 2
                for node in data.nodes:
                    gx = (cx - node.x) * 0.01
                    gy = (cy - node.y) * 0.01
                    f = forces[node.node_id]
                    forces[node.node_id] = (f[0] + gx, f[1] + gy)

                # Apply with damping
                total_energy = 0.0
                for node in data.nodes:
                    fx, fy = forces[node.node_id]
                    node.x += fx * self.damping
                    node.y += fy * self.damping
                    total_energy += math.sqrt(fx * fx + fy * fy)

                data.iteration += 1
                data.energy = total_energy

            return data

    def to_render_data(self, data: ForceGraphData) -> Dict[str, Any]:
        with self._lock:
            return {
                "nodes": [{"id": n.node_id, "label": n.label, "color": self.NETWORK_COLORS.get(n.network_type, "#999"),
                           "x": n.x, "y": n.y, "size": max(2, n.confidence * 10)} for n in data.nodes],
                "edges": [{"id": e.edge_id, "source": e.source_id, "target": e.target_id,
                           "thickness": max(0.5, e.weight * 5), "confidence": e.confidence} for e in data.edges],
                "iteration": data.iteration,
                "energy": data.energy,
            }


class ConfidenceDashboard:
    """置信度实时仪表盘。

    红黄绿三色渐变：绿 [0.7,1.0]、黄 [0.3,0.7)、红 [0,0.3)
    """

    THRESHOLD_HIGH = 0.7
    THRESHOLD_MEDIUM = 0.3

    def __init__(self):
        self._lock = threading.RLock()
        self.snapshots: List[ConfidenceSnapshot] = []

    def update(self, category: str, value: float):
        with self._lock:
            if value >= self.THRESHOLD_HIGH:
                level = ConfidenceLevel.HIGH
            elif value >= self.THRESHOLD_MEDIUM:
                level = ConfidenceLevel.MEDIUM
            else:
                level = ConfidenceLevel.LOW
            self.snapshots.append(ConfidenceSnapshot(category=category, value=value, level=level))

    def get_dashboard(self) -> Dict[str, Any]:
        with self._lock:
            latest: Dict[str, ConfidenceSnapshot] = {}
            for s in self.snapshots:
                latest[s.category] = s
            return {
                "categories": {
                    k: {"value": v.value, "level": v.level.value, "color": self._color(v.level)}
                    for k, v in latest.items()
                },
                "overall_confidence": sum(s.value for s in latest.values()) / max(len(latest), 1),
            }

    @staticmethod
    def _color(level: ConfidenceLevel) -> str:
        return {"high": "#4CAF50", "medium": "#FFC107", "low": "#F44336"}.get(level.value, "#999")


class GanttTimelineBuilder:
    """AgentPrism 风格 Gantt 时间线构建器。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.events: List[GanttEvent] = []

    def add_event(self, operation: MemoryOperation, duration_ms: float, metadata: Optional[Dict[str, Any]] = None):
        with self._lock:
            last_end = self.events[-1].end_ms if self.events else 0.0
            event = GanttEvent(
                event_id=str(uuid.uuid4())[:8],
                operation=operation,
                start_ms=last_end,
                end_ms=last_end + duration_ms,
                metadata=metadata or {},
            )
            self.events.append(event)

    def build_timeline(self) -> Dict[str, Any]:
        with self._lock:
            max_end = max((e.end_ms for e in self.events), default=1.0)
            return {
                "total_duration_ms": max_end,
                "event_count": len(self.events),
                "events": [{
                    "id": e.event_id,
                    "operation": e.operation.value,
                    "start_pct": (e.start_ms / max_end * 100) if max_end > 0 else 0,
                    "duration_pct": ((e.end_ms - e.start_ms) / max_end * 100) if max_end > 0 else 0,
                    "metadata": e.metadata,
                } for e in self.events],
            }


class StreamPublisher:
    """WebSocket/SSE 流式推送接口。"""

    def __init__(self, protocol: StreamProtocol = StreamProtocol.WEBSOCKET):
        self._lock = threading.RLock()
        self.protocol = protocol
        self.subscribers: Dict[str, deque] = {}
        self.sequence: int = 0

    def subscribe(self, channel: str) -> str:
        with self._lock:
            sub_id = str(uuid.uuid4())[:8]
            self.subscribers[sub_id] = deque(maxlen=1000)
            return sub_id

    def publish(self, channel: str, event_type: str, payload: Any):
        with self._lock:
            self.sequence += 1
            event = StreamEvent(event_type=event_type, payload=payload, sequence=self.sequence)
            for q in self.subscribers.values():
                q.append(event)

    def consume(self, subscriber_id: str, max_events: int = 10) -> List[StreamEvent]:
        with self._lock:
            q = self.subscribers.get(subscriber_id)
            if not q:
                return []
            events = []
            for _ in range(min(max_events, len(q))):
                events.append(q.popleft())
            return events

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "protocol": self.protocol.value,
                "subscribers": len(self.subscribers),
                "total_events": self.sequence,
            }


class InferenceTreeBuilder:
    """交互式推理树构建器。

    可折叠子树 + 自动摘要节点。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.roots: List[InferenceTreeNode] = []

    def add_root(self, content: str) -> InferenceTreeNode:
        with self._lock:
            node = InferenceTreeNode(node_id=str(uuid.uuid4())[:8], content=content, summary=self._summarize(content))
            self.roots.append(node)
            return node

    def add_child(self, parent: InferenceTreeNode, content: str) -> InferenceTreeNode:
        with self._lock:
            node = InferenceTreeNode(node_id=str(uuid.uuid4())[:8], content=content, summary=self._summarize(content))
            parent.children.append(node)
            return node

    def toggle_collapse(self, node: InferenceTreeNode):
        with self._lock:
            node.collapsed = not node.collapsed

    def to_tree_data(self, node: InferenceTreeNode) -> Dict[str, Any]:
        children_data = []
        if not node.collapsed:
            children_data = [self.to_tree_data(c) for c in node.children]
        return {
            "id": node.node_id,
            "content": node.content if not node.collapsed else node.summary,
            "summary": node.summary,
            "collapsed": node.collapsed,
            "confidence": node.confidence,
            "children": children_data,
        }

    @staticmethod
    def _summarize(content: str) -> str:
        words = content.split()
        if len(words) <= 8:
            return content
        return " ".join(words[:8]) + " ..."

    def statistics(self) -> Dict[str, Any]:
        def count_nodes(node: InferenceTreeNode) -> int:
            return 1 + sum(count_nodes(c) for c in node.children)
        with self._lock:
            total = sum(count_nodes(r) for r in self.roots)
            return {"roots": len(self.roots), "total_nodes": total}


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P16-7 Memory Visualization",
        "benchmark": "实时记忆可视化 (LinkedIn 2026)",
        "classes": 5,
        "enums": 5,
        "dataclasses": 8,
        "key_pattern": "Force-Directed Graph + Confidence Dashboard + Gantt Timeline + WS/SSE Streaming + Collapsible Inference Tree",
        "key_metric": "Interactive Real-Time Memory Views",
        "thread_safe": True,
    }
