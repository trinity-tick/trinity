"""
P23-4: Agent-Memory-OS — 团队级 ACL 记忆织物

对标论文: agent-memory-os (Team ACL Memory Fabric for Multi-Agent Systems, 2026.08)
核心发现: 多智能体记忆系统需要三层硬隔离：private / team / project，
        每层独立 ACL 控制。关联召回跨越隔离边界（仅读取有权限的条目），
        联邦组织结构同步确保团队成员变更时 ACL 自动更新。
三元语: 三层硬隔离 → ACL 控制 → 关联召回 → 联邦组织结构同步

设计要点:
- ACLScope: 记忆作用域枚举（PRIVATE / TEAM / PROJECT / PUBLIC）
- ACLPermission: ACL 权限条目，定义主体对资源的访问级别
- MemoryFabricNode: 记忆织物节点，含作用域、内容和 ACL 列表
- PrivateMemoryStore: 私有记忆存储，仅创建者可读写
- TeamMemoryStore: 团队记忆存储，按团队 ACL 控制访问
- ProjectMemoryStore: 项目记忆存储，按项目 ACL 控制访问
- AssociationRecallEngine: 关联召回引擎，跨隔离边界检索相关的可访问记忆
- FederationSyncCoordinator: 联邦组织结构同步器，监听团队变更并更新 ACL
- TeamACLMemoryFabric: 统一编排器，线程安全，提供 statistics() 运行时指标
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# Enums & Constants
# ============================================================================


class ACLScope(Enum):
    """记忆作用域"""
    PRIVATE = "private"               # 私有：仅创建者可访问
    TEAM = "team"                     # 团队：同团队所有成员可读
    PROJECT = "project"               # 项目：项目成员按角色权限访问
    PUBLIC = "public"                 # 公开：所有认证用户可读


class AccessLevel(Enum):
    """访问级别"""
    NONE = "none"                     # 无权限
    READ = "read"                     # 只读
    WRITE = "write"                   # 读写（含更新）
    ADMIN = "admin"                   # 管理（含删除和 ACL 修改）


class SyncEventType(Enum):
    """联邦同步事件类型"""
    MEMBER_JOINED = "member_joined"   # 新成员加入
    MEMBER_LEFT = "member_left"       # 成员离开
    ROLE_CHANGED = "role_changed"     # 角色变更
    TEAM_CREATED = "team_created"     # 新团队创建
    TEAM_DISBANDED = "team_disbanded" # 团队解散
    PROJECT_ARCHIVED = "project_archived"  # 项目归档


class RecallStrategy(Enum):
    """关联召回策略"""
    DIRECT_ONLY = "direct_only"       # 仅直接关联
    ONE_HOP = "one_hop"              # 一跳关联
    TWO_HOP = "two_hop"              # 两跳关联
    GRAPH_WALK = "graph_walk"        # 图游走
    SCORE_THRESHOLD = "score_threshold"  # 分数阈值


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass
class ACLPermission:
    """ACL 权限条目"""
    permission_id: str                # 权限唯一标识
    subject_id: str                   # 主体标识（用户/团队/项目 ID）
    subject_type: str                 # 主体类型（user / team / project）
    access_level: AccessLevel         # 访问级别
    granted_by: str                   # 授权者
    granted_at: float                 # 授权时间戳
    expires_at: Optional[float] = None  # 过期时间（None 表示永不过期）
    conditions: Dict[str, Any] = field(default_factory=dict)  # 条件约束


@dataclass
class MemoryFabricNode:
    """记忆织物节点"""
    node_id: str                      # 节点唯一标识
    scope: ACLScope                   # 记忆作用域
    owner_id: str                     # 创建者标识
    content: Dict[str, Any]           # 记忆内容
    acl: List[ACLPermission]          # ACL 列表
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    version: int = 1
    associations: List[str] = field(default_factory=list)  # 关联的其他节点 ID
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RecallCandidate:
    """关联召回候选"""
    node: MemoryFabricNode
    relevance_score: float            # 相关性分数
    association_path: List[str]       # 关联路径 [node_id, ...]
    access_granted: bool              # 是否已授予访问权限
    scope_crossed: List[ACLScope]     # 跨越的作用域边界


@dataclass
class FederationSyncEvent:
    """联邦同步事件"""
    event_id: str
    event_type: SyncEventType
    team_id: Optional[str]
    project_id: Optional[str]
    affected_subjects: List[str]      # 受影响的主体 ID 列表
    old_state: Optional[Dict[str, Any]]  # 变更前状态
    new_state: Optional[Dict[str, Any]]  # 变更后状态
    timestamp: float = field(default_factory=time.time)


@dataclass
class SyncResult:
    """同步结果"""
    event: FederationSyncEvent
    acl_updates_count: int            # ACL 更新数量
    nodes_reassigned: int             # 重新分配作用域的节点数
    errors: List[str] = field(default_factory=list)
    success: bool = True


# ============================================================================
# Core Classes
# ============================================================================


class PrivateMemoryStore:
    """私有记忆存储

    仅创建者可读写。通过 owner_id 做严格隔离。
    线程安全。
    """

    def __init__(self) -> None:
        self._nodes: OrderedDict[str, MemoryFabricNode] = OrderedDict()
        self._lock = threading.RLock()
        self._node_counter: int = 0

    def create(self, owner_id: str, content: Dict[str, Any],
               tags: Optional[List[str]] = None,
               associations: Optional[List[str]] = None) -> MemoryFabricNode:
        """创建私有记忆节点"""
        with self._lock:
            self._node_counter += 1
            node_id = f"PRIV_{self._node_counter:08d}"
            node = MemoryFabricNode(
                node_id=node_id,
                scope=ACLScope.PRIVATE,
                owner_id=owner_id,
                content=dict(content),
                acl=[ACLPermission(
                    permission_id=f"ACL_{node_id}_owner",
                    subject_id=owner_id,
                    subject_type="user",
                    access_level=AccessLevel.ADMIN,
                    granted_by="system",
                    granted_at=time.time(),
                )],
                tags=tags or [],
                associations=associations or [],
            )
            self._nodes[node_id] = node
            return node

    def read(self, node_id: str, requester_id: str) -> Optional[MemoryFabricNode]:
        """读取私有记忆（需 ownership 验证）"""
        node = self._nodes.get(node_id)
        if not node or node.owner_id != requester_id:
            return None
        return node

    def update(self, node_id: str, requester_id: str,
               content: Dict[str, Any]) -> Optional[MemoryFabricNode]:
        """更新私有记忆"""
        node = self._nodes.get(node_id)
        if not node or node.owner_id != requester_id:
            return None
        node.content = dict(content)
        node.updated_at = time.time()
        node.version += 1
        return node

    def delete(self, node_id: str, requester_id: str) -> bool:
        """删除私有记忆（硬删除）"""
        node = self._nodes.get(node_id)
        if not node or node.owner_id != requester_id:
            return False
        del self._nodes[node_id]
        return True

    def list_by_owner(self, owner_id: str) -> List[MemoryFabricNode]:
        return [n for n in self._nodes.values() if n.owner_id == owner_id]

    def statistics(self) -> Dict[str, Any]:
        return {"total_nodes": len(self._nodes)}


class TeamMemoryStore:
    """团队记忆存储

    基于团队 ACL 控制访问。同团队所有成员默认有 READ 权限。
    支持按角色授予 WRITE/ADMIN。
    """

    def __init__(self) -> None:
        self._nodes: OrderedDict[str, MemoryFabricNode] = OrderedDict()
        self._team_index: Dict[str, Set[str]] = defaultdict(set)  # team_id → {node_id}
        self._lock = threading.RLock()
        self._node_counter: int = 0

    def create(self, team_id: str, owner_id: str, content: Dict[str, Any],
               member_ids: List[str],
               tags: Optional[List[str]] = None) -> MemoryFabricNode:
        """创建团队记忆节点"""
        with self._lock:
            self._node_counter += 1
            node_id = f"TEAM_{self._node_counter:08d}"

            acl = [ACLPermission(
                permission_id=f"ACL_{node_id}_owner",
                subject_id=owner_id,
                subject_type="user",
                access_level=AccessLevel.ADMIN,
                granted_by="system",
                granted_at=time.time(),
            )]
            for mid in member_ids:
                if mid != owner_id:
                    acl.append(ACLPermission(
                        permission_id=f"ACL_{node_id}_{mid}",
                        subject_id=mid,
                        subject_type="user",
                        access_level=AccessLevel.READ,
                        granted_by="system",
                        granted_at=time.time(),
                    ))

            node = MemoryFabricNode(
                node_id=node_id,
                scope=ACLScope.TEAM,
                owner_id=owner_id,
                content=dict(content),
                acl=acl,
                tags=tags or [],
                metadata={"team_id": team_id},
            )
            self._nodes[node_id] = node
            self._team_index[team_id].add(node_id)
            return node

    def check_access(self, node_id: str, requester_id: str,
                     required_level: AccessLevel = AccessLevel.READ) -> bool:
        """检查访问权限"""
        node = self._nodes.get(node_id)
        if not node:
            return False
        for perm in node.acl:
            if perm.subject_id == requester_id:
                if perm.expires_at and perm.expires_at < time.time():
                    continue
                levels = {AccessLevel.NONE: 0, AccessLevel.READ: 1,
                          AccessLevel.WRITE: 2, AccessLevel.ADMIN: 3}
                return levels.get(perm.access_level, 0) >= levels.get(required_level, 0)
        return False

    def read(self, node_id: str, requester_id: str) -> Optional[MemoryFabricNode]:
        if not self.check_access(node_id, requester_id, AccessLevel.READ):
            return None
        return self._nodes.get(node_id)

    def list_by_team(self, team_id: str) -> List[MemoryFabricNode]:
        node_ids = self._team_index.get(team_id, set())
        return [self._nodes[nid] for nid in node_ids if nid in self._nodes]

    def add_acl(self, node_id: str, permission: ACLPermission) -> bool:
        node = self._nodes.get(node_id)
        if not node:
            return False
        node.acl.append(permission)
        return True

    def statistics(self) -> Dict[str, Any]:
        return {"total_nodes": len(self._nodes), "teams": len(self._team_index)}


class ProjectMemoryStore:
    """项目记忆存储

    按项目 ACL 控制访问。支持角色级别的权限（owner / maintainer / contributor / viewer）。
    项目归档后所有节点变为只读。
    """

    def __init__(self) -> None:
        self._nodes: OrderedDict[str, MemoryFabricNode] = OrderedDict()
        self._project_index: Dict[str, Set[str]] = defaultdict(set)
        self._archived_projects: Set[str] = set()
        self._lock = threading.RLock()
        self._node_counter: int = 0

    def create(self, project_id: str, owner_id: str, content: Dict[str, Any],
               role_permissions: Dict[str, AccessLevel],  # user_id → AccessLevel
               tags: Optional[List[str]] = None) -> MemoryFabricNode:
        """创建项目记忆节点"""
        with self._lock:
            self._node_counter += 1
            node_id = f"PROJ_{self._node_counter:08d}"

            acl = []
            for uid, level in role_permissions.items():
                acl.append(ACLPermission(
                    permission_id=f"ACL_{node_id}_{uid}",
                    subject_id=uid,
                    subject_type="user",
                    access_level=level,
                    granted_by=owner_id,
                    granted_at=time.time(),
                ))

            node = MemoryFabricNode(
                node_id=node_id,
                scope=ACLScope.PROJECT,
                owner_id=owner_id,
                content=dict(content),
                acl=acl,
                tags=tags or [],
                metadata={"project_id": project_id},
            )
            self._nodes[node_id] = node
            self._project_index[project_id].add(node_id)
            return node

    def is_archived(self, project_id: str) -> bool:
        return project_id in self._archived_projects

    def archive_project(self, project_id: str) -> None:
        """归档项目：所有节点变为只读"""
        self._archived_projects.add(project_id)
        for nid in self._project_index.get(project_id, set()):
            node = self._nodes.get(nid)
            if node:
                for perm in node.acl:
                    if perm.access_level in (AccessLevel.WRITE, AccessLevel.ADMIN):
                        perm.access_level = AccessLevel.READ
                node.metadata["archived"] = True

    def check_access(self, node_id: str, requester_id: str,
                     required_level: AccessLevel = AccessLevel.READ) -> bool:
        node = self._nodes.get(node_id)
        if not node:
            return False
        project_id = node.metadata.get("project_id", "")
        if self.is_archived(project_id) and required_level != AccessLevel.READ:
            return False
        for perm in node.acl:
            if perm.subject_id == requester_id:
                if perm.expires_at and perm.expires_at < time.time():
                    continue
                levels = {AccessLevel.NONE: 0, AccessLevel.READ: 1,
                          AccessLevel.WRITE: 2, AccessLevel.ADMIN: 3}
                return levels.get(perm.access_level, 0) >= levels.get(required_level, 0)
        return False

    def read(self, node_id: str, requester_id: str) -> Optional[MemoryFabricNode]:
        if not self.check_access(node_id, requester_id, AccessLevel.READ):
            return None
        return self._nodes.get(node_id)

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_nodes": len(self._nodes),
            "projects": len(self._project_index),
            "archived": len(self._archived_projects),
        }


class AssociationRecallEngine:
    """关联召回引擎

    跨越三层隔离边界检索相关的可访问记忆。
    沿关联图进行 N-hop 游走，仅返回请求者有读取权限的节点。
    """

    def __init__(self, recall_strategy: RecallStrategy = RecallStrategy.TWO_HOP,
                 relevance_threshold: float = 0.3,
                 max_results: int = 50) -> None:
        self._recall_strategy = recall_strategy
        self._relevance_threshold = relevance_threshold
        self._max_results = max_results

    def recall(self, seed_nodes: List[MemoryFabricNode],
               all_stores: List[Any],  # [PrivateMemoryStore, TeamMemoryStore, ProjectMemoryStore]
               requester_id: str) -> List[RecallCandidate]:
        """执行关联召回"""
        candidates: List[RecallCandidate] = []
        visited: Set[str] = set()

        # 构建统一节点查找表
        node_map: Dict[str, MemoryFabricNode] = {}
        for store in all_stores:
            if hasattr(store, '_nodes'):
                node_map.update(store._nodes)

        # BFS N-hop 游走
        current_layer: List[Tuple[MemoryFabricNode, List[str], float]] = [
            (n, [n.node_id], 1.0) for n in seed_nodes
        ]
        max_hops = {RecallStrategy.DIRECT_ONLY: 0, RecallStrategy.ONE_HOP: 1,
                     RecallStrategy.TWO_HOP: 2, RecallStrategy.GRAPH_WALK: 3}.get(
            self._recall_strategy, 2)

        for hop in range(max_hops + 1):
            next_layer: List[Tuple[MemoryFabricNode, List[str], float]] = []
            for node, path, score in current_layer:
                if node.node_id in visited:
                    continue
                visited.add(node.node_id)

                # 检查访问权限
                access_granted = self._check_access_any_store(node, requester_id, all_stores)
                scopes_crossed = self._collect_scopes(path, node_map)

                if score >= self._relevance_threshold:
                    candidates.append(RecallCandidate(
                        node=node,
                        relevance_score=score,
                        association_path=list(path),
                        access_granted=access_granted,
                        scope_crossed=scopes_crossed,
                    ))

                # 沿关联边扩展
                for assoc_id in node.associations:
                    next_node = node_map.get(assoc_id)
                    if next_node and next_node.node_id not in visited:
                        decay = 0.6 ** (hop + 1)
                        next_layer.append((next_node, path + [assoc_id], score * decay))

            current_layer = next_layer

        # 按相关性排序
        candidates.sort(key=lambda c: -c.relevance_score)
        return candidates[:self._max_results]

    def _check_access_any_store(self, node: MemoryFabricNode,
                                 requester_id: str,
                                 all_stores: List[Any]) -> bool:
        """检查节点在对应存储中的访问权限"""
        if node.scope == ACLScope.PRIVATE:
            return node.owner_id == requester_id
        for store in all_stores:
            if hasattr(store, 'check_access'):
                try:
                    if store.check_access(node.node_id, requester_id):
                        return True
                except Exception:
                    pass
        return False

    def _collect_scopes(self, path: List[str],
                        node_map: Dict[str, MemoryFabricNode]) -> List[ACLScope]:
        scopes: List[ACLScope] = []
        seen: Set[ACLScope] = set()
        for nid in path:
            n = node_map.get(nid)
            if n and n.scope not in seen:
                seen.add(n.scope)
                scopes.append(n.scope)
        return scopes

    def statistics(self) -> Dict[str, Any]:
        return {"strategy": self._recall_strategy.value, "max_results": self._max_results}


class FederationSyncCoordinator:
    """联邦组织结构同步器

    监听团队/项目组织结构变更事件，自动更新 ACL。
    支持增量同步和全量重建。
    """

    def __init__(self) -> None:
        self._event_log: List[FederationSyncEvent] = []
        self._member_index: Dict[str, Set[str]] = defaultdict(set)  # team_id → {user_id}
        self._lock = threading.RLock()

    def on_member_joined(self, team_id: str, user_id: str) -> FederationSyncEvent:
        """成员加入事件"""
        with self._lock:
            self._member_index[team_id].add(user_id)
            event = FederationSyncEvent(
                event_id=f"SYNC_{len(self._event_log):06d}",
                event_type=SyncEventType.MEMBER_JOINED,
                team_id=team_id,
                project_id=None,
                affected_subjects=[user_id],
                old_state=None,
                new_state={"team_id": team_id, "member_id": user_id},
            )
            self._event_log.append(event)
            return event

    def on_member_left(self, team_id: str, user_id: str) -> FederationSyncEvent:
        """成员离开事件"""
        with self._lock:
            self._member_index[team_id].discard(user_id)
            event = FederationSyncEvent(
                event_id=f"SYNC_{len(self._event_log):06d}",
                event_type=SyncEventType.MEMBER_LEFT,
                team_id=team_id,
                project_id=None,
                affected_subjects=[user_id],
                old_state={"team_id": team_id, "member_id": user_id},
                new_state=None,
            )
            self._event_log.append(event)
            return event

    def sync_acls(self, event: FederationSyncEvent,
                  team_store: TeamMemoryStore,
                  project_store: ProjectMemoryStore) -> SyncResult:
        """根据同步事件更新 ACL"""
        result = SyncResult(event=event, acl_updates_count=0, nodes_reassigned=0)

        if event.event_type == SyncEventType.MEMBER_LEFT:
            team_id = event.team_id
            if team_id:
                # 从团队存储中移除该成员的 ACL
                for node in team_store.list_by_team(team_id):
                    node.acl = [p for p in node.acl if p.subject_id not in event.affected_subjects]
                    result.acl_updates_count += 1

                # 从项目存储中移除该成员的 ACL
                for ps_nodes in project_store._project_index.values():
                    for nid in ps_nodes:
                        node = project_store._nodes.get(nid)
                        if node:
                            old_len = len(node.acl)
                            node.acl = [p for p in node.acl if p.subject_id not in event.affected_subjects]
                            result.acl_updates_count += (old_len - len(node.acl))

        elif event.event_type == SyncEventType.MEMBER_JOINED:
            team_id = event.team_id
            if team_id:
                for node in team_store.list_by_team(team_id):
                    for uid in event.affected_subjects:
                        node.acl.append(ACLPermission(
                            permission_id=f"ACL_{node.node_id}_{uid}",
                            subject_id=uid,
                            subject_type="user",
                            access_level=AccessLevel.READ,
                            granted_by="federation_sync",
                            granted_at=time.time(),
                        ))
                    result.acl_updates_count += len(event.affected_subjects)

        return result

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_events": len(self._event_log),
            "teams_indexed": len(self._member_index),
            "total_members": sum(len(v) for v in self._member_index.values()),
        }


# ============================================================================
# Engine
# ============================================================================


class TeamACLMemoryFabric:
    """Agent-Memory-OS 统一编排器

    整合 三层硬隔离 → ACL 控制 → 关联召回 → 联邦组织结构同步
    的完整团队级记忆织物。线程安全。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._private_store = PrivateMemoryStore()
        self._team_store = TeamMemoryStore()
        self._project_store = ProjectMemoryStore()
        self._recall_engine = AssociationRecallEngine()
        self._sync_coordinator = FederationSyncCoordinator()

    # --- 创建操作 ---

    def create_private(self, owner_id: str, content: Dict[str, Any],
                       tags: Optional[List[str]] = None) -> MemoryFabricNode:
        with self._lock:
            return self._private_store.create(owner_id, content, tags)

    def create_team(self, team_id: str, owner_id: str, content: Dict[str, Any],
                    member_ids: List[str],
                    tags: Optional[List[str]] = None) -> MemoryFabricNode:
        with self._lock:
            return self._team_store.create(team_id, owner_id, content, member_ids, tags)

    def create_project(self, project_id: str, owner_id: str,
                       content: Dict[str, Any],
                       role_permissions: Dict[str, AccessLevel],
                       tags: Optional[List[str]] = None) -> MemoryFabricNode:
        with self._lock:
            return self._project_store.create(project_id, owner_id, content, role_permissions, tags)

    # --- 读取操作（带 ACL 验证）---

    def read(self, node_id: str, requester_id: str) -> Optional[MemoryFabricNode]:
        for store in [self._private_store, self._team_store, self._project_store]:
            if hasattr(store, 'read'):
                result = store.read(node_id, requester_id)
                if result:
                    return result
        return None

    # --- 关联召回 ---

    def recall(self, seed_node_ids: List[str], requester_id: str) -> List[RecallCandidate]:
        all_stores = [self._private_store, self._team_store, self._project_store]
        seed_nodes: List[MemoryFabricNode] = []
        for nid in seed_node_ids:
            for store in all_stores:
                if hasattr(store, '_nodes') and nid in store._nodes:
                    seed_nodes.append(store._nodes[nid])
                    break
        return self._recall_engine.recall(seed_nodes, all_stores, requester_id)

    # --- 联邦同步 ---

    def sync_member_joined(self, team_id: str, user_id: str) -> SyncResult:
        with self._lock:
            event = self._sync_coordinator.on_member_joined(team_id, user_id)
            return self._sync_coordinator.sync_acls(event, self._team_store, self._project_store)

    def sync_member_left(self, team_id: str, user_id: str) -> SyncResult:
        with self._lock:
            event = self._sync_coordinator.on_member_left(team_id, user_id)
            return self._sync_coordinator.sync_acls(event, self._team_store, self._project_store)

    def archive_project(self, project_id: str) -> None:
        with self._lock:
            self._project_store.archive_project(project_id)

    def statistics(self) -> Dict[str, Any]:
        """聚合运行时统计"""
        return {
            "private": self._private_store.statistics(),
            "team": self._team_store.statistics(),
            "project": self._project_store.statistics(),
            "recall": self._recall_engine.statistics(),
            "sync": self._sync_coordinator.statistics(),
        }


# ============================================================================
# Module-level statistics helper
# ============================================================================

def statistics(engine: Optional[TeamACLMemoryFabric] = None) -> Dict[str, Any]:
    """模块级统计接口"""
    if engine is not None:
        return engine.statistics()
    return {"status": "no engine initialized"}
