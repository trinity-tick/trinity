"""Abstract storage adapter interface."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class StorageAdapter(ABC):
    """Abstract base class for storage backends.

    Supports multi-tenant, multi-persona, multi-session memory storage.
    """

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to the storage backend."""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection."""
        ...

    @abstractmethod
    def store_memory(
        self,
        content: str,
        persona_id: str = "default",
        session_id: Optional[str] = None,
        tenant_id: str = "default",
        agent_id: str = "default",
        app_id: Optional[str] = None,
        role: str = "user",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        category: str = "general",
    ) -> Dict[str, Any]:
        """Store a memory entry.

        Args:
            content: Memory text content.
            persona_id: User/profile identifier.
            session_id: Session identifier.
            tenant_id: Tenant/organization identifier.
            agent_id: Agent identifier (namespace isolation).
            role: user/assistant/system.
            importance: 0-1 importance score.
            tags: List of tags.
            category: Memory category.

        Returns:
            Dict with memory_id, version_id, sha256_hash, timestamp.
        """
        ...

    @abstractmethod
    def search_memories(
        self,
        query: str,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        app_id: Optional[str] = None,
        session_id: Optional[str] = None,
        category: Optional[str] = None,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search memories with optional composite scope filtering.

        Args:
            query: Search query.
            persona_id: Filter by persona (None = all).
            tenant_id: Filter by tenant (None = all).
            agent_id: Filter by agent (None = all).
            app_id: Filter by application (None = all).
            session_id: Filter by session (None = all).
            category: Filter by memory category (None = all).
            top_k: Max results.

        Returns:
            List of matching memory dicts.
        """
        ...

    @abstractmethod
    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Get a single memory by ID."""
        ...

    @abstractmethod
    def get_persona_memories(
        self, persona_id: str, agent_id: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get all memories for a persona, optionally filtered by agent_id."""
        ...

    @abstractmethod
    def delete_memory(self, memory_id: str) -> bool:
        """Soft-delete a memory."""
        ...

    @abstractmethod
    def get_version_chain(self, memory_id: str) -> List[Dict[str, Any]]:
        """Get the full version/audit chain for a memory."""
        ...

    def get_all_memories(self, agent_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        """Get all active memories across all personas.

        Args:
            limit: Max memories to return.

        Returns:
            List of memory dicts.
        """
        # Default implementation uses get_persona_memories with empty persona_id
        # Subclasses should override for better performance
        return []

    @abstractmethod
    def touch_memory(self, memory_id: str) -> bool:
        """更新指定记忆的 last_accessed_at 和 access_count。

        Args:
            memory_id: 要触达的记忆 ID。

        Returns:
            是否成功更新。
        """
        ...

    @abstractmethod
    def age_memories(self) -> Dict[str, Any]:
        """手动触发老化扫描，清理 TTL 过期的记忆（软删除）。

        Returns:
            Dict with aged_count and details.
        """
        ...

    @abstractmethod
    def get_memory_stats(self) -> Dict[str, Any]:
        """返回记忆统计信息（总数、过期数、Agent 分布、平均访问频率）。

        Returns:
            Stats dict.
        """
        ...

    @abstractmethod
    def get_modality_stats(self) -> Dict[str, Any]:
        """返回各模态记忆数量、存储占比统计。

        Returns:
            Dict with total_active, modalities, percentages.
        """
        ...

    @abstractmethod
    def get_conflicts(self, memory_id: str) -> Dict[str, Any]:
        """查看指定记忆的冲突链（同一 conflict_group_id 的所有版本）。

        Args:
            memory_id: 记忆 ID。

        Returns:
            冲突链信息。
        """
        ...

    @abstractmethod
    def resolve_conflict(
        self, conflict_group_id: str, keep_memory_id: str
    ) -> Dict[str, Any]:
        """解决冲突：保留选定版本，软删除同一冲突组的其他版本。

        Args:
            conflict_group_id: 冲突组 ID。
            keep_memory_id: 保留的记忆 ID。

        Returns:
            操作结果，含 resolved_count 与 discarded_ids。
        """
        ...

    @abstractmethod
    def dedup_stats(self) -> Dict[str, Any]:
        """返回去重统计信息（冲突组数、已解决数等）。

        Returns:
            Dedup stats dict.
        """
        ...

    @abstractmethod
    def set_agent_weight(self, agent_id: str, weight: float) -> Dict[str, Any]:
        """设置 Agent 的检索权重。

        Args:
            agent_id: Agent 标识。
            weight: 权重值。

        Returns:
            操作结果。
        """
        ...

    @abstractmethod
    def get_agent_weights(self) -> Dict[str, float]:
        """获取所有 Agent 权重配置。

        Returns:
            Dict[agent_id, weight]
        """
        ...

    @abstractmethod
    def delete_agent_weight(self, agent_id: str) -> bool:
        """删除 Agent 权重配置。

        Args:
            agent_id: Agent 标识。

        Returns:
            是否删除成功。
        """
        ...

    @abstractmethod
    def create_memory_link(self, source_id: str, target_id: str,
                           link_type: str = "semantic",
                           strength: float = 0.5) -> Dict[str, Any]:
        """创建记忆关联链接。

        Args:
            source_id: 源记忆 ID。
            target_id: 目标记忆 ID。
            link_type: 链接类型（co_occurrence/semantic/causal/same_task）。
            strength: 关联强度 0-1。

        Returns:
            创建结果 dict，包含 id / source_id / target_id / link_type / strength。
        """
        ...

    @abstractmethod
    def get_linked_memories(self, memory_id: str,
                            min_strength: float = 0.0) -> List[Dict[str, Any]]:
        """获取与指定记忆关联的所有链接（按强度降序）。

        Args:
            memory_id: 记忆 ID。
            min_strength: 最低关联强度阈值。

        Returns:
            链接列表。
        """
        ...

    @abstractmethod
    def strengthen_link(self, link_id: str,
                        increment: float = 0.1) -> Dict[str, Any]:
        """增强链接强度（上限 1.0）。

        Args:
            link_id: 链接 ID。
            increment: 增量值。

        Returns:
            操作结果。
        """
        ...

    @abstractmethod
    def weaken_link(self, link_id: str,
                    decrement: float = 0.1) -> Dict[str, Any]:
        """削弱链接强度（下限 0.0）。

        Args:
            link_id: 链接 ID。
            decrement: 减量值。

        Returns:
            操作结果。
        """
        ...

    @abstractmethod
    def delete_memory_link(self, link_id: str) -> bool:
        """删除指定链接。

        Args:
            link_id: 链接 ID。

        Returns:
            是否删除成功。
        """
        ...

    @abstractmethod
    def get_all_links(self, memory_id: str) -> Dict[str, Any]:
        """获取某记忆的所有关联链接和反向链接。

        Args:
            memory_id: 记忆 ID。

        Returns:
            Dict with outgoing/incoming lists.
        """
        ...

    # ── 记忆图谱（entities + relations）───────────────────────────

    @abstractmethod
    def upsert_entity(self, name: str, etype: str = "concept",
                      properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建或更新实体（幂等：按 name + type 去重）。"""
        ...

    @abstractmethod
    def get_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """查询实体详情（含关联关系）。"""
        ...

    @abstractmethod
    def search_entities(self, name: Optional[str] = None,
                        etype: Optional[str] = None,
                        limit: int = 20) -> List[Dict[str, Any]]:
        """搜索实体。"""
        ...

    @abstractmethod
    def create_relation(self, subject_id: str, predicate: str,
                        object_id: str,
                        properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建关系（幂等去重）。"""
        ...

    @abstractmethod
    def query_relations(self, subject_id: Optional[str] = None,
                        predicate: Optional[str] = None,
                        object_id: Optional[str] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        """查询关系。"""
        ...

    @abstractmethod
    def traverse(self, start_id: str,
                 max_hops: int = 3) -> Dict[str, Any]:
        """多跳遍历子图。"""
        ...

    @abstractmethod
    def create_entity(self, name: str, etype: str = "concept",
                      properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建新实体（非幂等，实体已存在时返回错误）。"""
        ...

    @abstractmethod
    def get_entity_by_name(self, name: str,
                           etype: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """按名称精确匹配单个实体。"""
        ...

    @abstractmethod
    def get_neighbors(self, entity_id: str) -> Dict[str, Any]:
        """获取实体的 1-hop 邻居（直接关联的实体和关系）。"""
        ...

    @abstractmethod
    def query_graph(self, query: str,
                    limit: int = 20) -> Dict[str, Any]:
        """通过关键词搜索实体，返回以匹配实体为中心的子图。
        等价于 search_entities + 收集每个实体的 1-hop 邻居 + 聚合去重。"""
        ...

    # ── 审计日志（Memory Replay & Audit）─────────────────────────

    @abstractmethod
    def write_audit_log(self, memory_id: str = None, action: str = "",
                         agent_id: str = None, persona_id: str = None,
                         details: dict = None) -> None:
        """写入审计日志（链式 SHA-256 防篡改）。"""
        ...

    @abstractmethod
    def get_audit_trail(self, memory_id: str) -> List[Dict[str, Any]]:
        """查看某条记忆的完整变更历史。"""
        ...

    @abstractmethod
    def replay_agent_session(self, agent_id: str,
                              start_time: str = None,
                              end_time: str = None) -> List[Dict[str, Any]]:
        """回放某 Agent 在时间段内的所有操作。"""
        ...

    @abstractmethod
    def verify_audit_integrity(self) -> Dict[str, Any]:
        """验证审计链完整性。"""
        ...

    @abstractmethod
    def get_audit_summary(self, start_time: str = None,
                           end_time: str = None) -> Dict[str, Any]:
        """审计摘要：各操作计数、活跃 Agent、峰值时段。"""
        ...

    # ── DCSA-EJP 双循环宪法自审计 ─────────────────────────────────

    @abstractmethod
    def log_audit_run(self, run_id: str, agent_id: str, task: str,
                       executor_result: str, auditor_result: str,
                       disagreement_flag: bool = False,
                       packet_json: str = "{}") -> bool:
        """持久化一次双循环审计运行。"""
        ...

    @abstractmethod
    def log_constitutional_violation(self, run_id: str, invariant: str,
                                      severity: str, context: str = "{}") -> bool:
        """记录宪法违规。"""
        ...

    @abstractmethod
    def get_audit_history(self, agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """获取某 Agent 的审计运行历史。"""
        ...

    @abstractmethod
    def get_audit_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """获取单次审计运行详情（含违规记录）。"""
        ...

    @abstractmethod
    def get_violation_trends(self, agent_id: Optional[str] = None,
                              limit: int = 100) -> List[Dict[str, Any]]:
        """获取违规趋势数据。"""
        ...

    # ── A2A Protocol: Agent Registry & Task Management ────────────

    @abstractmethod
    def register_agent_card(self, agent_id: str, card_json: str) -> bool:
        """注册或更新 Agent Card 到全局注册中心。"""
        ...

    @abstractmethod
    def get_agent_card(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """获取 Agent 的注册卡片。"""
        ...

    @abstractmethod
    def create_a2a_task(self, task_id: str, from_agent: str, to_agent: str,
                         payload: str, status: str = "pending",
                         result: Optional[str] = None) -> bool:
        """创建跨 Agent 任务记录。"""
        ...

    @abstractmethod
    def update_a2a_task(self, task_id: str, status: str,
                         result: Optional[str] = None) -> bool:
        """更新跨 Agent 任务状态。"""
        ...

    @abstractmethod
    def list_a2a_tasks(self, task_id: Optional[str] = None,
                        agent_id: Optional[str] = None,
                        status: Optional[str] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        """列出跨 Agent 任务。"""
        ...

    @abstractmethod
    def diagnostics(self) -> Dict[str, Any]:
        """Return storage diagnostics."""
        ...
