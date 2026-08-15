"""
# status: orphan (2026-08-15 audit, not in runtime path)
P7-4: Enterprise-Level Memory Permission Hierarchy (对标 Tencent Enterprise Memory)
======================================================================================

四级架构：
  1. 团队记忆池（Team Memory Pool）：跨 Agent 共享的企业级记忆池
  2. 分级权限管控（Permission Control）：企业-部门-个人三级权限体系
  3. 场景化记忆聚类（Scenario Clustering）：按部门/业务自动归类
  4. 记忆资产复用引擎（Asset Reuse Engine）：一键调用团队经验

Reference: 腾讯云数据库, "2026 AI Agent 记忆解决方案",
           https://cloud.tencent.com.cn/developer/article/2681636, 2026.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections import defaultdict, deque, OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举 ──────────────────────────────────────────────────────────────

class PermissionLevel(Enum):
    """权限层级。"""
    ENTERPRISE = "enterprise"
    DEPARTMENT = "department"
    PERSONAL = "personal"


class AccessAction(Enum):
    """访问操作类型。"""
    VIEW = "view"
    EDIT = "edit"
    REUSE = "reuse"
    MANAGE = "manage"
    EXPORT = "export"


class MemoryCategory(Enum):
    """记忆资产分类。"""
    TECHNICAL = "technical"
    BUSINESS = "business"
    CUSTOMER = "customer"
    OPERATIONAL = "operational"
    PRODUCT = "product"
    COMPLIANCE = "compliance"
    TRAINING = "training"
    GENERAL = "general"


class ClusterMethod(Enum):
    """聚类方式。"""
    BY_DEPARTMENT = "by_department"
    BY_BUSINESS_LINE = "by_business_line"
    BY_SCENARIO = "by_scenario"
    BY_PROJECT = "by_project"
    HYBRID = "hybrid"


class AuditEventType(Enum):
    """审计事件类型。"""
    MEMORY_CREATE = "memory_create"
    MEMORY_ACCESS = "memory_access"
    MEMORY_EDIT = "memory_edit"
    MEMORY_DELETE = "memory_delete"
    MEMORY_REUSE = "memory_reuse"
    PERMISSION_CHANGE = "permission_change"
    POOL_SYNC = "pool_sync"
    CLUSTER_REORGANIZE = "cluster_reorganize"


# ── 数据结构 ──────────────────────────────────────────────────────────

@dataclass
class PermissionEntry:
    entry_id: str = field(default_factory=lambda: f"perm_{uuid.uuid4().hex[:12]}")
    level: PermissionLevel = PermissionLevel.PERSONAL
    allowed_actions: List[AccessAction] = field(default_factory=list)
    grantee_id: str = ""
    resource_id: str = ""
    granted_by: str = ""
    granted_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None


@dataclass
class MemoryAsset:
    asset_id: str = field(default_factory=lambda: f"mast_{uuid.uuid4().hex[:12]}")
    title: str = ""
    content: str = ""
    category: MemoryCategory = MemoryCategory.GENERAL
    level: PermissionLevel = PermissionLevel.PERSONAL
    department: str = ""
    owner_agent: str = ""
    tags: List[str] = field(default_factory=list)
    usage_count: int = 0
    quality_score: float = 0.5
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class MemoryCluster:
    cluster_id: str = field(default_factory=lambda: f"mcl_{uuid.uuid4().hex[:12]}")
    name: str = ""
    assets: List[str] = field(default_factory=list)
    method: ClusterMethod = ClusterMethod.BY_DEPARTMENT
    department: str = ""
    business_line: str = ""
    description: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class AuditRecord:
    record_id: str = field(default_factory=lambda: f"aud_{uuid.uuid4().hex[:12]}")
    event_type: AuditEventType = AuditEventType.MEMORY_ACCESS
    agent_id: str = ""
    resource_id: str = ""
    action: str = ""
    details: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class TeamMemoryPool:
    pool_id: str = ""
    name: str = ""
    level: PermissionLevel = PermissionLevel.ENTERPRISE
    asset_count: int = 0
    department_count: int = 0
    last_synced: Optional[float] = None


@dataclass
class EnterpriseMemoryStats:
    total_assets: int = 0
    enterprise_assets: int = 0
    department_assets: int = 0
    personal_assets: int = 0
    total_clusters: int = 0
    total_permissions: int = 0
    total_reuses: int = 0
    total_audit_events: int = 0
    pools_count: int = 0


# ── _OrgKnowledgeGraph ────────────────────────────────────────────────

class _OrgKnowledgeGraph:
    """组织知识图谱：部门管理 + 资产CRUD + 聚类 + 记忆池 + 复用引擎。"""

    def __init__(self, parent: "EnterpriseMemoryEngine") -> None:
        self._p = parent

    # ── 部门管理 ──

    def register_department(self, department_name: str,
                            member_agents: Optional[List[str]] = None) -> None:
        with self._p._lock:
            if member_agents:
                self._p._department_members[department_name].update(member_agents)

    def get_department_members(self, department_name: str) -> List[str]:
        return list(self._p._department_members.get(department_name, set()))

    # ── 资产 CRUD ──

    def create_asset(self, title: str, content: str, owner_agent: str,
                     level: PermissionLevel = PermissionLevel.PERSONAL,
                     category: MemoryCategory = MemoryCategory.GENERAL,
                     department: str = "", tags: Optional[List[str]] = None) -> MemoryAsset:
        dept = department or self._p.default_department
        asset = MemoryAsset(title=title, content=content, category=category,
                            level=level, department=dept, owner_agent=owner_agent,
                            tags=tags or [])
        with self._p._lock:
            self._p._assets[asset.asset_id] = asset
            self._p._level_index[level].add(asset.asset_id)
            self._p._department_index[dept].add(asset.asset_id)
            self._p._category_index[category].add(asset.asset_id)
            for tag in asset.tags:
                self._p._tag_index[tag].add(asset.asset_id)
            if self._p.auto_cluster:
                self._auto_assign_cluster(asset)
        self._p._policy._log_audit(AuditEventType.MEMORY_CREATE, owner_agent,
                                    asset.asset_id, f"create:{level.value}",
                                    f"Created asset '{title}'")
        logger.debug("Asset created: %s (level=%s, dept=%s, owner=%s)",
                     asset.asset_id, level.value, dept, owner_agent)
        return asset

    def update_asset(self, asset_id: str, agent_id: str, **updates: Any) -> Optional[MemoryAsset]:
        if not self._p._policy.can_edit(agent_id, asset_id):
            logger.warning("Edit denied: agent=%s asset=%s", agent_id, asset_id)
            return None
        with self._p._lock:
            asset = self._p._assets.get(asset_id)
            if asset is None:
                return None
            for key, value in updates.items():
                if hasattr(asset, key):
                    setattr(asset, key, value)
            asset.updated_at = time.time()
        self._p._policy._log_audit(AuditEventType.MEMORY_EDIT, agent_id,
                                    asset_id, "update", str(updates))
        return asset

    def get_asset(self, asset_id: str) -> Optional[MemoryAsset]:
        return self._p._assets.get(asset_id)

    def get_assets_by_ids(self, asset_ids: List[str]) -> List[MemoryAsset]:
        return [a for aid in asset_ids if (a := self._p._assets.get(aid)) is not None]

    # ── 聚类 ──

    def _auto_assign_cluster(self, asset: MemoryAsset) -> None:
        dept = asset.department
        cluster_key = f"{dept}:{asset.category.value}"
        for cluster in self._p._clusters.values():
            if cluster.name == cluster_key:
                cluster.assets.append(asset.asset_id)
                return
        cluster = MemoryCluster(
            name=cluster_key, assets=[asset.asset_id],
            method=ClusterMethod.BY_BUSINESS_LINE if asset.category in (
                MemoryCategory.BUSINESS, MemoryCategory.PRODUCT
            ) else ClusterMethod.BY_DEPARTMENT,
            department=dept,
            description=f"Auto-cluster for {asset.category.value} in {dept}",
        )
        self._p._clusters[cluster.cluster_id] = cluster

    def create_cluster(self, name: str, asset_ids: Optional[List[str]] = None,
                       method: ClusterMethod = ClusterMethod.BY_SCENARIO,
                       department: str = "", business_line: str = "",
                       description: str = "") -> MemoryCluster:
        cluster = MemoryCluster(name=name, assets=asset_ids or [],
                                method=method, department=department,
                                business_line=business_line, description=description)
        with self._p._lock:
            self._p._clusters[cluster.cluster_id] = cluster
        return cluster

    def get_clusters(self, department: Optional[str] = None,
                     method: Optional[ClusterMethod] = None) -> List[MemoryCluster]:
        with self._p._lock:
            clusters = list(self._p._clusters.values())
            if department:
                clusters = [c for c in clusters if c.department == department]
            if method:
                clusters = [c for c in clusters if c.method == method]
            return clusters

    def reorganize_clusters(self, method: ClusterMethod) -> int:
        with self._p._lock:
            old_count = len(self._p._clusters)
            self._p._clusters.clear()
            for asset in self._p._assets.values():
                if method == ClusterMethod.BY_DEPARTMENT:
                    cluster_key = f"dept:{asset.department}"
                elif method == ClusterMethod.BY_BUSINESS_LINE:
                    cluster_key = f"biz:{asset.category.value}"
                elif method == ClusterMethod.BY_SCENARIO:
                    cluster_key = f"scn:{asset.category.value}:{asset.department}"
                else:
                    cluster_key = f"proj:{asset.department}:{asset.category.value}"
                existing = next((c for c in self._p._clusters.values() if c.name == cluster_key), None)
                if existing:
                    existing.assets.append(asset.asset_id)
                else:
                    cluster = MemoryCluster(name=cluster_key, assets=[asset.asset_id],
                                            method=method, department=asset.department)
                    self._p._clusters[cluster.cluster_id] = cluster
            new_count = len(self._p._clusters)
            self._p._policy._log_audit(AuditEventType.CLUSTER_REORGANIZE, "system", "*",
                                        f"reorganize:{method.value}",
                                        f"Clusters: {old_count} -> {new_count}")
            logger.info("Clusters reorganized: %d -> %d (method=%s)", old_count, new_count, method.value)
            return new_count

    # ── 复用引擎 ──

    def reuse_asset(self, asset_id: str, agent_id: str) -> Optional[MemoryAsset]:
        if not self._p._policy.can_reuse(agent_id, asset_id):
            logger.warning("Reuse denied: agent=%s asset=%s", agent_id, asset_id)
            return None
        with self._p._lock:
            asset = self._p._assets.get(asset_id)
            if asset is None:
                return None
            asset.usage_count += 1
            self._p._total_reuses += 1
        self._p._policy._log_audit(AuditEventType.MEMORY_REUSE, agent_id,
                                    asset_id, "reuse", f"Agent {agent_id} reused '{asset.title}'")
        return asset

    def search_and_reuse(self, agent_id: str, query: str,
                         category: Optional[MemoryCategory] = None,
                         top_k: int = 5) -> List[MemoryAsset]:
        accessible = self._p._policy.get_accessible_assets(agent_id, AccessAction.REUSE, category=category)
        query_lower = query.lower()
        scored: List[Tuple[MemoryAsset, float]] = []
        for asset in accessible:
            score = 0.0
            query_words = set(query_lower.split())
            title_words = set(asset.title.lower().split())
            overlap = query_words & title_words
            if overlap:
                score += len(overlap) / max(len(query_words), 1) * 0.6
            tag_match = sum(1 for tag in asset.tags if tag.lower() in query_lower)
            score += tag_match * 0.2 + asset.quality_score * 0.2
            scored.append((asset, score))
        scored.sort(key=lambda x: -x[1])
        return [asset for asset, _ in scored[:top_k]]

    # ── 记忆池 ──

    def build_team_pool(self, level: PermissionLevel = PermissionLevel.ENTERPRISE,
                        department: Optional[str] = None) -> TeamMemoryPool:
        pool_id = f"pool_{level.value}_{department or 'enterprise'}"
        with self._p._lock:
            assets = list(self._p._assets.values())
            if department:
                assets = [a for a in assets if a.department == department]
            else:
                assets = [a for a in assets if a.level == level]
            departments = {a.department for a in assets}
            pool = TeamMemoryPool(pool_id=pool_id, name=f"{level.value.capitalize()} Memory Pool",
                                  level=level, asset_count=len(assets),
                                  department_count=len(departments), last_synced=time.time())
            self._p._pools[pool_id] = pool
        self._p._policy._log_audit(AuditEventType.POOL_SYNC, "system", pool_id, "build",
                                    f"Pool built: {len(assets)} assets, {len(departments)} departments")
        return pool

    def get_pools(self) -> List[TeamMemoryPool]:
        return list(self._p._pools.values())


# ── _PolicyEnforcer ───────────────────────────────────────────────────


    def snapshot(self) -> "EnterpriseMemoryStats":
        with self._p._lock:
            return EnterpriseMemoryStats(
                total_assets=len(self._p._assets),
                enterprise_assets=len(self._p._level_index.get(PermissionLevel.ENTERPRISE, set())),
                department_assets=len(self._p._level_index.get(PermissionLevel.DEPARTMENT, set())),
                personal_assets=len(self._p._level_index.get(PermissionLevel.PERSONAL, set())),
                total_clusters=len(self._p._clusters), total_permissions=len(self._p._permissions),
                total_reuses=self._p._total_reuses, total_audit_events=len(self._p._audit_log),
                pools_count=len(self._p._pools),
            )

    def statistics(self) -> Dict[str, Any]:
        snap = self.snapshot()
        return {"assets_total": snap.total_assets, "assets_enterprise": snap.enterprise_assets,
                "assets_department": snap.department_assets, "assets_personal": snap.personal_assets,
                "clusters_total": snap.total_clusters, "permissions_total": snap.total_permissions,
                "total_reuses": snap.total_reuses, "audit_events": snap.total_audit_events,
                "pools_count": snap.pools_count, "enterprise_name": self._p.enterprise_name,
                "departments": list(self._p._department_members.keys()),
                "department_count": len(self._p._department_members),
                "category_distribution": {cat.value: len(self._p._category_index.get(cat, set()))
                                          for cat in MemoryCategory},
                "auto_cluster": self._p.auto_cluster, "max_assets": self._p.max_assets,
                "max_audit_records": self._p.max_audit_records}

class _PolicyEnforcer:
    """权限管控 + 审计日志 + 质量评分。"""

    def __init__(self, parent: "EnterpriseMemoryEngine") -> None:
        self._p = parent

    def grant_permission(self, grantee_id: str, resource_id: str, level: PermissionLevel,
                         actions: List[AccessAction], granter_id: str,
                         expires_at: Optional[float] = None) -> PermissionEntry:
        entry = PermissionEntry(level=level, allowed_actions=actions,
                                grantee_id=grantee_id, resource_id=resource_id,
                                granted_by=granter_id, expires_at=expires_at)
        with self._p._lock:
            self._p._permissions[entry.entry_id] = entry
        self._log_audit(AuditEventType.PERMISSION_CHANGE, granter_id, resource_id,
                        f"grant:{level.value}", f"To {grantee_id}: {[a.value for a in actions]}")
        return entry

    def check_access(self, agent_id: str, resource_id: str,
                     action: AccessAction) -> bool:
        with self._p._lock:
            asset = self._p._assets.get(resource_id)
            if asset and asset.level == PermissionLevel.PERSONAL:
                return asset.owner_agent == agent_id
            if asset and asset.level == PermissionLevel.DEPARTMENT:
                members = self._p._department_members.get(asset.department, set())
                if agent_id not in members and agent_id != asset.owner_agent:
                    return False
            for entry in self._p._permissions.values():
                if entry.grantee_id != agent_id:
                    continue
                if entry.resource_id != resource_id and entry.resource_id != "*":
                    continue
                if entry.expires_at and time.time() > entry.expires_at:
                    continue
                if action in entry.allowed_actions:
                    return True
            if asset and asset.level == PermissionLevel.ENTERPRISE:
                return action in (AccessAction.VIEW, AccessAction.REUSE)
            return False

    def can_view(self, agent_id: str, resource_id: str) -> bool:
        return self.check_access(agent_id, resource_id, AccessAction.VIEW)

    def can_edit(self, agent_id: str, resource_id: str) -> bool:
        return self.check_access(agent_id, resource_id, AccessAction.EDIT)

    def can_reuse(self, agent_id: str, resource_id: str) -> bool:
        return self.check_access(agent_id, resource_id, AccessAction.REUSE)

    def get_accessible_assets(self, agent_id: str,
                              action: AccessAction = AccessAction.VIEW,
                              category: Optional[MemoryCategory] = None,
                              department: Optional[str] = None,
                              limit: int = 50) -> List[MemoryAsset]:
        results: List[MemoryAsset] = []
        with self._p._lock:
            for asset in self._p._assets.values():
                if category and asset.category != category:
                    continue
                if department and asset.department != department:
                    continue
                if self.check_access(agent_id, asset.asset_id, action):
                    results.append(asset)
                    if len(results) >= limit:
                        break
        results.sort(key=lambda a: a.quality_score, reverse=True)
        return results

    def _log_audit(self, event_type: AuditEventType, agent_id: str,
                   resource_id: str, action: str, details: str = "") -> AuditRecord:
        record = AuditRecord(event_type=event_type, agent_id=agent_id,
                             resource_id=resource_id, action=action, details=details)
        self._p._audit_log.append(record)
        return record

    def get_audit_log(self, event_type: Optional[AuditEventType] = None,
                      agent_id: Optional[str] = None, limit: int = 100) -> List[AuditRecord]:
        records = list(self._p._audit_log)
        if event_type:
            records = [r for r in records if r.event_type == event_type]
        if agent_id:
            records = [r for r in records if r.agent_id == agent_id]
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records[:limit]

    def rate_asset(self, asset_id: str, score: float, agent_id: str) -> bool:
        with self._p._lock:
            asset = self._p._assets.get(asset_id)
            if asset is None:
                return False
            alpha = 0.3
            asset.quality_score = round(alpha * score + (1 - alpha) * asset.quality_score, 4)
            asset.updated_at = time.time()
        return True


# ── EnterpriseMemoryEngine (Facade) ────────────────────────────────────

class EnterpriseMemoryEngine:
    """企业级记忆权限分层引擎。四级架构：团队记忆池 + 权限管控 + 场景化聚类 + 资产复用。"""

    def __init__(self, enterprise_name: str = "", max_assets: int = 10000,
                 max_audit_records: int = 5000, auto_cluster: bool = True,
                 default_department: str = "default"):
        self.enterprise_name = enterprise_name or f"enterprise_{uuid.uuid4().hex[:8]}"
        self.max_assets = max_assets; self.max_audit_records = max_audit_records
        self.auto_cluster = auto_cluster; self.default_department = default_department
        self._assets: Dict[str, MemoryAsset] = {}; self._permissions: Dict[str, PermissionEntry] = {}
        self._clusters: Dict[str, MemoryCluster] = {}
        self._audit_log: deque[AuditRecord] = deque(maxlen=max_audit_records)
        self._pools: Dict[str, TeamMemoryPool] = {}
        self._level_index: Dict[PermissionLevel, Set[str]] = defaultdict(set)
        self._department_index: Dict[str, Set[str]] = defaultdict(set)
        self._category_index: Dict[MemoryCategory, Set[str]] = defaultdict(set)
        self._tag_index: Dict[str, Set[str]] = defaultdict(set)
        self._department_members: Dict[str, Set[str]] = defaultdict(set)
        self._total_reuses: int = 0; self._lock = threading.RLock()
        self._org = _OrgKnowledgeGraph(self); self._policy = _PolicyEnforcer(self)
        logger.info("EnterpriseMemoryEngine initialized (enterprise=%s, max_assets=%d)",
                    self.enterprise_name, max_assets)

    # ── 委托给 _OrgKnowledgeGraph ──
    def register_department(self, department_name: str, member_agents: Optional[List[str]] = None) -> None:
        self._org.register_department(department_name, member_agents)
    def get_department_members(self, department_name: str) -> List[str]:
        return self._org.get_department_members(department_name)
    def create_asset(self, title: str, content: str, owner_agent: str,
                     level: PermissionLevel = PermissionLevel.PERSONAL,
                     category: MemoryCategory = MemoryCategory.GENERAL,
                     department: str = "", tags: Optional[List[str]] = None) -> MemoryAsset:
        return self._org.create_asset(title, content, owner_agent, level, category, department, tags)
    def update_asset(self, asset_id: str, agent_id: str, **updates: Any) -> Optional[MemoryAsset]:
        return self._org.update_asset(asset_id, agent_id, **updates)
    def get_asset(self, asset_id: str) -> Optional[MemoryAsset]:
        return self._org.get_asset(asset_id)
    def get_assets_by_ids(self, asset_ids: List[str]) -> List[MemoryAsset]:
        return self._org.get_assets_by_ids(asset_ids)
    def create_cluster(self, name: str, asset_ids: Optional[List[str]] = None,
                       method: ClusterMethod = ClusterMethod.BY_SCENARIO,
                       department: str = "", business_line: str = "",
                       description: str = "") -> MemoryCluster:
        return self._org.create_cluster(name, asset_ids, method, department, business_line, description)
    def get_clusters(self, department: Optional[str] = None,
                     method: Optional[ClusterMethod] = None) -> List[MemoryCluster]:
        return self._org.get_clusters(department, method)
    def reorganize_clusters(self, method: ClusterMethod) -> int:
        return self._org.reorganize_clusters(method)
    def reuse_asset(self, asset_id: str, agent_id: str) -> Optional[MemoryAsset]:
        return self._org.reuse_asset(asset_id, agent_id)
    def search_and_reuse(self, agent_id: str, query: str,
                         category: Optional[MemoryCategory] = None, top_k: int = 5) -> List[MemoryAsset]:
        return self._org.search_and_reuse(agent_id, query, category, top_k)
    def build_team_pool(self, level: PermissionLevel = PermissionLevel.ENTERPRISE,
                        department: Optional[str] = None) -> TeamMemoryPool:
        return self._org.build_team_pool(level, department)
    def get_pools(self) -> List[TeamMemoryPool]:
        return self._org.get_pools()
    def snapshot(self) -> EnterpriseMemoryStats:
        return self._org.snapshot()
    def statistics(self) -> Dict[str, Any]:
        return self._org.statistics()

    # ── 委托给 _PolicyEnforcer ──
    def grant_permission(self, grantee_id: str, resource_id: str, level: PermissionLevel,
                         actions: List[AccessAction], granter_id: str,
                         expires_at: Optional[float] = None) -> PermissionEntry:
        return self._policy.grant_permission(grantee_id, resource_id, level, actions, granter_id, expires_at)
    def check_access(self, agent_id: str, resource_id: str, action: AccessAction) -> bool:
        return self._policy.check_access(agent_id, resource_id, action)
    def can_view(self, agent_id: str, resource_id: str) -> bool:
        return self._policy.can_view(agent_id, resource_id)
    def can_edit(self, agent_id: str, resource_id: str) -> bool:
        return self._policy.can_edit(agent_id, resource_id)
    def can_reuse(self, agent_id: str, resource_id: str) -> bool:
        return self._policy.can_reuse(agent_id, resource_id)
    def get_accessible_assets(self, agent_id: str, action: AccessAction = AccessAction.VIEW,
                              category: Optional[MemoryCategory] = None,
                              department: Optional[str] = None, limit: int = 50) -> List[MemoryAsset]:
        return self._policy.get_accessible_assets(agent_id, action, category, department, limit)
    def get_audit_log(self, event_type: Optional[AuditEventType] = None,
                      agent_id: Optional[str] = None, limit: int = 100) -> List[AuditRecord]:
        return self._policy.get_audit_log(event_type, agent_id, limit)
    def rate_asset(self, asset_id: str, score: float, agent_id: str) -> bool:
        return self._policy.rate_asset(asset_id, score, agent_id)

    # ── 重置 ──
    def reset(self) -> None:
        with self._lock:
            self._assets.clear(); self._permissions.clear(); self._clusters.clear()
            self._audit_log.clear(); self._pools.clear(); self._level_index.clear()
            self._department_index.clear(); self._category_index.clear()
            self._tag_index.clear(); self._department_members.clear(); self._total_reuses = 0
        logger.info("EnterpriseMemoryEngine reset")
