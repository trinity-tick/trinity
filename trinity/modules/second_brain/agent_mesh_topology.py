"""
# status: orphan (2026-08-15 audit, not in runtime path)
P26-2: Agent Mesh Topology — 对标 Agent Mesh 2026.07
三元语: Mesh → Elect → Route → Monitor
设计要点:
  - MeshNode 为 meshed agent 节点 dataclass，含 role/health_score
  - LeaderElection 基于 Bully 算法的简单 leader 选举
  - FaultTolerantRouter 主节点故障时自动 fallback 到下一个健康节点
  - HeartbeatMonitor 定期 ping 邻居，health_score 衰减/恢复
  - AgentMesh 封装节点管理 + 路由一体化入口
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class MeshRole(str, Enum):
    LEADER = "leader"
    FOLLOWER = "follower"
    CANDIDATE = "candidate"


@dataclass
class MeshNode:
    """Agent Mesh 节点 — 含角色、邻居列表与健康评分。"""

    node_id: str
    role: MeshRole = MeshRole.FOLLOWER
    neighbors: list[str] = field(default_factory=list)
    health_score: float = 1.0
    last_seen: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


class LeaderElection:
    """Bully 算法实现的简单 leader 选举。

    elect() 从 peer_nodes 中选择 node_id 最大（字典序）的健康节点为 leader。
    """

    def __init__(self) -> None:
        self._current_leader: Optional[str] = None
        self._lock = threading.RLock()
        self._election_count = 0

    def elect(self, peer_nodes: list[MeshNode]) -> Optional[str]:
        """执行一轮 Bully 选举 — 健康节点中 node_id 最大者胜出。"""
        with self._lock:
            healthy = [n for n in peer_nodes if n.health_score > 0.5]
            if not healthy:
                self._current_leader = None
                return None
            healthy.sort(key=lambda n: n.node_id, reverse=True)
            self._current_leader = healthy[0].node_id
            self._election_count += 1
            return self._current_leader

    @property
    def leader_id(self) -> Optional[str]:
        return self._current_leader

    def statistics(self) -> dict:
        return {"leader_id": self._current_leader, "election_count": self._election_count}


class FaultTolerantRouter:
    """容错路由器 — primary 故障时自动 fallback 到下一个健康节点。

    route() 按 health_score 降序尝试，首个成功即返回。
    """

    def __init__(self, mesh: dict[str, MeshNode]) -> None:
        self._mesh = mesh
        self._lock = threading.RLock()
        self._route_count = 0
        self._fallback_count = 0

    def route(self, request: dict, mesh_topology: Optional[dict[str, MeshNode]] = None) -> dict:
        """路由请求 — 优先 leader，失败则 fallback。"""
        nodes = mesh_topology or self._mesh
        with self._lock:
            sorted_nodes = sorted(
                nodes.values(),
                key=lambda n: (1 if n.role == MeshRole.LEADER else 0, n.health_score),
                reverse=True,
            )
            self._route_count += 1
            for i, node in enumerate(sorted_nodes):
                if node.health_score <= 0.3:
                    continue
                if i > 0:
                    self._fallback_count += 1
                return {
                    "routed_to": node.node_id,
                    "role": node.role.value,
                    "fallback_used": i > 0,
                }
            return {"routed_to": None, "error": "no healthy node available"}

    def statistics(self) -> dict:
        return {"route_count": self._route_count, "fallback_count": self._fallback_count}


class HeartbeatMonitor:
    """心跳监控器 — 定期 ping 邻居，health_score 衰减/恢复。

    check() 遍历节点，超过 timeout 未 seen 则指数衰减 health_score。
    """

    def __init__(self, timeout_sec: float = 30.0, decay_rate: float = 0.9) -> None:
        self._timeout = timeout_sec
        self._decay = decay_rate
        self._lock = threading.RLock()
        self._monitor_rounds = 0

    def check(self, nodes: dict[str, MeshNode]) -> dict[str, MeshNode]:
        """执行一轮心跳检查，更新各节点 health_score。"""
        now = time.time()
        with self._lock:
            for node in nodes.values():
                elapsed = now - node.last_seen
                if elapsed > self._timeout:
                    node.health_score *= self._decay
                else:
                    node.health_score = min(1.0, node.health_score + 0.05)
            self._monitor_rounds += 1
            return nodes

    def heartbeat(self, node: MeshNode) -> None:
        """收到节点心跳，刷新 last_seen 并恢复 health_score。"""
        with self._lock:
            node.last_seen = time.time()
            node.health_score = min(1.0, node.health_score + 0.1)

    def statistics(self) -> dict:
        return {"monitor_rounds": self._monitor_rounds}


class AgentMesh:
    """Agent Mesh 拓扑管理 — 节点管理 + 选举 + 路由一体化入口。

    用法:
        mesh = AgentMesh([MeshNode(...), ...])
        result = mesh.route(task)
    """

    def __init__(self, nodes: Optional[list[MeshNode]] = None) -> None:
        self._nodes: dict[str, MeshNode] = {}
        self._election = LeaderElection()
        self._monitor = HeartbeatMonitor()
        self._lock = threading.RLock()
        if nodes:
            for n in nodes:
                self._nodes[n.node_id] = n
        self._router: Optional[FaultTolerantRouter] = None

    def add_node(self, node: MeshNode) -> None:
        with self._lock:
            self._nodes[node.node_id] = node

    def remove_node(self, node_id: str) -> bool:
        with self._lock:
            return self._nodes.pop(node_id, None) is not None

    @property
    def leader(self) -> Optional[str]:
        return self._election.leader_id

    def elect(self) -> Optional[str]:
        nodes = list(self._nodes.values())
        return self._election.elect(nodes)

    def route(self, task: dict) -> dict:
        """路由任务 — 先确保 router 同步 mesh，然后调用 FaultTolerantRouter。"""
        with self._lock:
            self._monitor.check(self._nodes)
            if self._router is None:
                self._router = FaultTolerantRouter(self._nodes)
        return self._router.route(task, self._nodes)

    def statistics(self) -> dict:
        with self._lock:
            return {
                "node_count": len(self._nodes),
                "leader": self._election.leader_id,
                "monitor": self._monitor.statistics(),
                "health_scores": {
                    nid: round(n.health_score, 3) for nid, n in self._nodes.items()
                },
            }
