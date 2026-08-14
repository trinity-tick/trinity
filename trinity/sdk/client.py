"""
TrinitySDK — Standardized HTTP Client for Trinity Memory System.

A thin wrapper over the Trinity REST API (default: http://localhost:8001).
All methods return Python native dicts and handle HTTP exceptions internally.

Usage::

    from trinity.sdk import TrinitySDK

    with TrinitySDK() as trinity:
        trinity.write("Hello world")
        results = trinity.search("Hello", limit=5)
        print(results)

    # Or without context manager:
    sdk = TrinitySDK(base_url="http://localhost:8001")
    sdk.write("A new memory", modality="text")
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from trinity.sdk.exceptions import (
    AuthenticationError,
    ConflictError,
    ConnectionError,
    DuplicateMemory,
    MemoryNotFound,
    TrinityError,
    ValidationError,
)

# Optional requests dependency; deferred import with friendly error
try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


_STATUS_ERROR_MAP = {
    400: ValidationError,
    401: AuthenticationError,
    404: MemoryNotFound,
    409: ConflictError,
    422: ValidationError,
}


class TrinitySDK:
    """Standardized Trinity Memory System SDK.

    All operations map to REST API endpoints served by ``trinity-api``.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8001",
        persona_id: str = "default",
        agent_id: str = "default",
    ):
        if requests is None:
            raise RuntimeError(
                "The 'requests' package is required. Install with: "
                "pip install trinity-memory[sdk]"
            )

        self.base_url = base_url.rstrip("/")
        self.persona_id = persona_id
        self.agent_id = agent_id
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # ── Context Manager ──────────────────────────────────────────────

    def __enter__(self) -> TrinitySDK:
        return self

    def __exit__(self, *args: Any) -> None:
        self._session.close()

    def close(self) -> None:
        """Explicitly close the underlying HTTP session."""
        self._session.close()

    # ── Internal HTTP helpers ─────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute an HTTP request and return parsed JSON.

        Raises TrinityError subclasses for non-2xx responses.
        """
        try:
            resp = self._session.request(
                method=method,
                url=self._url(path),
                json=json,
                params=params,
                timeout=30,
            )
        except requests.exceptions.ConnectionError as exc:
            raise ConnectionError(
                f"Cannot reach Trinity server at {self.base_url}",
                status_code=0,
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise ConnectionError(
                f"Trinity server at {self.base_url} timed out",
                status_code=0,
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise TrinityError(
                str(exc), status_code=0, response_body=""
            ) from exc

        if resp.ok:
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

        # Map status codes to typed exceptions
        error_body = resp.text
        error_cls = _STATUS_ERROR_MAP.get(resp.status_code, TrinityError)
        raise error_cls(
            f"HTTP {resp.status_code}: {error_body[:500]}",
            status_code=resp.status_code,
            response_body=error_body,
        )

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request("GET", path, params=params)

    def _post(self, path: str, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request("POST", path, json=json)

    def _delete(self, path: str) -> Dict[str, Any]:
        return self._request("DELETE", path)

    # ── Core Memory Operations ────────────────────────────────────────

    def write(
        self,
        content: str,
        modality: str = "text",
        ttl_seconds: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        source_uri: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Write (ingest) a memory.

        Args:
            content: Memory text content.
            modality: One of 'text' / 'code' / 'trace' / 'image_description'.
            ttl_seconds: Time-to-live in seconds (None = never expire).
            metadata: Arbitrary key-value metadata.
            source_uri: Original source file path or URL.

        Returns:
            Dict with memory_id, version_id, timestamp, etc.
        """
        body: Dict[str, Any] = {
            "content": content,
            "persona_id": self.persona_id,
            "agent_id": self.agent_id,
            "modality": modality,
        }
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        if metadata is not None:
            body["metadata"] = metadata
        if source_uri is not None:
            body["source_uri"] = source_uri

        return self._post("/memories", json=body)

    def search(
        self,
        query: str,
        limit: int = 10,
        modality: Optional[str] = None,
        agent_id: Optional[str] = None,
        ranked: bool = True,
    ) -> Dict[str, Any]:
        """Semantic memory search.

        Args:
            query: Search query string.
            limit: Maximum number of results.
            modality: Filter by modality.
            agent_id: Filter by agent namespace.
            ranked: Enable multi-stage ranking.

        Returns:
            Dict with 'results' list and 'pushed_memories' list.
        """
        params: Dict[str, Any] = {
            "query": query,
            "limit": limit,
            "persona_id": self.persona_id,
            "agent_id": agent_id or self.agent_id,
            "ranked": str(ranked).lower(),
        }
        if modality:
            params["modality"] = modality
        return self._get("/memories", params=params)

    def read(self, memory_id: str) -> Dict[str, Any]:
        """Read a single memory by ID.

        Args:
            memory_id: The memory's unique identifier.

        Returns:
            Memory dict.
        """
        return self._get(f"/memories/{memory_id}")

    def list_all(
        self,
        persona_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """List memories with optional filters.

        Args:
            persona_id: Filter by persona (defaults to instance setting).
            agent_id: Filter by agent (defaults to instance setting).
            limit: Max results.

        Returns:
            Dict with 'memories' list.
        """
        params: Dict[str, Any] = {
            "limit": limit,
            "persona_id": persona_id or self.persona_id,
            "agent_id": agent_id or self.agent_id,
        }
        return self._get("/memories", params=params)

    def age(self) -> Dict[str, Any]:
        """Trigger expiry cleanup (TTL-based aging).

        Returns:
            Dict with aged_count.
        """
        return self._post("/memories/age")

    def stats(self) -> Dict[str, Any]:
        """Retrieve memory pool statistics.

        Returns:
            Stats dict with total_memories, expired_count, etc.
        """
        return self._get("/memories/stats")

    # ── Knowledge Graph ───────────────────────────────────────────────

    def entities(
        self,
        name: Optional[str] = None,
        type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search knowledge graph entities.

        Args:
            name: Entity name (fuzzy match).
            type: Entity type filter.

        Returns:
            Dict with 'entities' list.
        """
        params: Dict[str, Any] = {}
        if name:
            params["name"] = name
        if type:
            params["type"] = type
        return self._get("/graph/entities", params=params)

    def relations(
        self,
        subject_id: Optional[str] = None,
        predicate: Optional[str] = None,
        object_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Query knowledge graph relations.

        Args:
            subject_id: Source entity ID.
            predicate: Relation predicate name.
            object_id: Target entity ID.

        Returns:
            Dict with 'relations' list.
        """
        params: Dict[str, Any] = {}
        if subject_id:
            params["subject_id"] = subject_id
        if predicate:
            params["predicate"] = predicate
        if object_id:
            params["object_id"] = object_id
        return self._get("/graph/relations", params=params)

    def traverse(self, start_id: str, max_hops: int = 3) -> Dict[str, Any]:
        """Multi-hop graph traversal from a starting entity.

        Args:
            start_id: Starting entity ID.
            max_hops: Maximum traversal depth.

        Returns:
            Dict with 'nodes' and 'edges'.
        """
        return self._get(
            "/graph/traverse",
            params={"start_id": start_id, "max_hops": max_hops},
        )

    def explore(self, topic: str) -> Dict[str, Any]:
        """Topic-based knowledge exploration.

        Searches entities matching *topic*, expands one-hop relations,
        aggregates related memories, and returns a structured knowledge card.

        Args:
            topic: Topic name to explore.

        Returns:
            Dict with entities, relations, related_memories, summary.
        """
        return self._post("/memories/explore", json={"topic": topic})

    # ── Agent Weights ─────────────────────────────────────────────────

    def weights(self) -> Dict[str, Any]:
        """Get all agent weight configurations.

        Returns:
            Dict mapping agent_id → weight.
        """
        return self._get("/agents/weights")

    def set_weight(self, agent_id: str, weight: float) -> Dict[str, Any]:
        """Set or update an agent's retrieval weight.

        Args:
            agent_id: Agent identifier.
            weight: Weight value (suggested 0.1–2.0).

        Returns:
            Operation result dict.
        """
        return self._post(
            "/agents/weights",
            json={"agent_id": agent_id, "weight": weight},
        )

    # ── Memory Links ──────────────────────────────────────────────────

    def links(self, memory_id: str) -> Dict[str, Any]:
        """Get all links for a memory (outgoing + incoming).

        Args:
            memory_id: Memory identifier.

        Returns:
            Dict with 'outgoing' and 'incoming' link lists.
        """
        return self._get(f"/memories/{memory_id}/links")

    def link(
        self,
        source_id: str,
        target_id: str,
        link_type: str = "semantic",
    ) -> Dict[str, Any]:
        """Create a link between two memories.

        Args:
            source_id: Source memory ID.
            target_id: Target memory ID.
            link_type: Link category.

        Returns:
            Dict with link_id.
        """
        return self._post(
            f"/memories/{source_id}/links",
            json={"target_id": target_id, "link_type": link_type},
        )

    # ── Conflict Management ───────────────────────────────────────────

    def conflicts(self, memory_id: str) -> Dict[str, Any]:
        """View conflict chain for a memory.

        Args:
            memory_id: Memory with potential conflicts.

        Returns:
            Dict with conflict_group_id and conflicting versions.
        """
        return self._get(f"/memories/{memory_id}/conflicts")

    def resolve(self, memory_id: str, keep_id: str) -> Dict[str, Any]:
        """Resolve a conflict group, keeping the chosen version.

        Args:
            memory_id: Any memory ID in the conflict group.
            keep_id: The memory version to keep.

        Returns:
            Dict with resolved_count and discarded_ids.
        """
        return self._post(
            "/memories/conflicts/resolve",
            json={"memory_id": memory_id, "keep_id": keep_id},
        )

    # ── Audit & Replay ────────────────────────────────────────────────

    def audit_trail(self, memory_id: str) -> Dict[str, Any]:
        """Retrieve full audit trail for a specific memory.

        Args:
            memory_id: Target memory ID.

        Returns:
            Dict with audit_trail list and total_entries.
        """
        return self._get(f"/audit/memories/{memory_id}")

    def replay(self, agent_id: str,
               start_time: str = None,
               end_time: str = None) -> Dict[str, Any]:
        """Replay an agent's session within a time range.

        Args:
            agent_id: Target agent identifier.
            start_time: ISO format start time.
            end_time: ISO format end time.

        Returns:
            Dict with operations list and total_operations.
        """
        params: Dict[str, Any] = {}
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        return self._get(f"/audit/agents/{agent_id}/replay", params=params)

    def verify(self) -> Dict[str, Any]:
        """Verify audit chain integrity.

        Returns:
            Dict with integrity_ok, total_entries, tampered list.
        """
        return self._get("/audit/integrity")

    def audit_summary(self, start_time: str = None,
                       end_time: str = None) -> Dict[str, Any]:
        """Get audit summary statistics.

        Args:
            start_time: ISO format start time.
            end_time: ISO format end time.

        Returns:
            Dict with total_entries, action_counts, active_agents, etc.
        """
        params: Dict[str, Any] = {}
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        return self._get("/audit/summary", params=params)

    # ── Multi-Anchor Identity ────────────────────────────────────────

    def register_anchor(self, agent_id: str, anchor_type: str,
                        content: str) -> Dict[str, Any]:
        """Register or update an identity anchor.

        Args:
            agent_id: Agent identifier.
            anchor_type: One of identity_files/procedural_patterns/episodic_keys/value_specifications.
            content: JSON-encoded anchor content.

        Returns:
            Dict with id, agent_id, anchor_type, version, checksum.
        """
        return self._post("/identity/anchors", json={
            "agent_id": agent_id,
            "anchor_type": anchor_type,
            "content": content,
        })

    def get_anchors(self, agent_id: str,
                    anchor_type: str = None) -> Dict[str, Any]:
        """Get all anchors for an agent, optionally filtered by type.

        Args:
            agent_id: Agent identifier.
            anchor_type: Optional anchor type filter.

        Returns:
            Dict with anchors list and total count.
        """
        params: Dict[str, Any] = {}
        if anchor_type:
            params["anchor_type"] = anchor_type
        return self._get(f"/identity/agents/{agent_id}/anchors", params=params)

    def get_profile(self, agent_id: str) -> Dict[str, Any]:
        """Get full identity profile with consistency score.

        Args:
            agent_id: Agent identifier.

        Returns:
            Dict with agent_id, anchors, consistency_score, etc.
        """
        return self._get(f"/identity/agents/{agent_id}/profile")

    def reconstruct_identity(self, agent_id: str,
                              available_anchors: List[str] = None) -> Dict[str, Any]:
        """Reconstruct identity from anchors (full or partial).

        Args:
            agent_id: Agent identifier.
            available_anchors: Optional list of anchor types for partial reconstruction.

        Returns:
            Dict with reconstructed identity profile.
        """
        body: Dict[str, Any] = {}
        if available_anchors:
            body["available_anchors"] = available_anchors
        return self._post(f"/identity/agents/{agent_id}/reconstruct", json=body)

    def detect_drift(self, agent_id: str) -> Dict[str, Any]:
        """Detect identity drift against baseline anchors.

        Args:
            agent_id: Agent identifier.

        Returns:
            Dict with drift_score, drift_detected, anchor_comparisons, etc.
        """
        return self._post(f"/identity/agents/{agent_id}/drift-check")

    def export_identity(self, agent_id: str) -> Dict[str, Any]:
        """Export full identity bundle for agent migration.

        Args:
            agent_id: Agent identifier.

        Returns:
            Dict with bundle containing all anchors.
        """
        return self._post("/identity/bundles/export", json={"agent_id": agent_id})

    def import_identity(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        """Import an identity bundle.

        Args:
            bundle: The identity bundle dict from export_identity.

        Returns:
            Dict with import result.
        """
        return self._post("/identity/bundles/import", json={"bundle": bundle})

    # ── DCSA-EJP 双循环宪法自审计 ─────────────────────────────────

    def audit_run(self, agent_id: str, task: str = "",
                  executor_result: str = "{}") -> Dict[str, Any]:
        """Execute a dual-loop constitutional audit.

        Args:
            agent_id: Target agent identifier.
            task: Task description.
            executor_result: Executor output JSON.

        Returns:
            Dict with run_id, pass/fail/flag, violation_count, justification_packet.
        """
        return self._post("/audit/run", json={
            "agent_id": agent_id,
            "task": task,
            "executor_result": executor_result,
        })

    def audit_runs(self, agent_id: str = None,
                   limit: int = 50) -> Dict[str, Any]:
        """List audit run history.

        Args:
            agent_id: Optional agent filter.
            limit: Max results.

        Returns:
            Dict with runs list.
        """
        params: Dict[str, Any] = {"limit": limit}
        if agent_id:
            params["agent_id"] = agent_id
        return self._get("/audit/runs", params=params)

    def audit_run_detail(self, run_id: str) -> Dict[str, Any]:
        """Get a single audit run detail with justification packet.

        Args:
            run_id: Audit run identifier.

        Returns:
            Full audit run with violations list.
        """
        return self._get(f"/audit/runs/{run_id}")

    def violation_trends(self, agent_id: str = None,
                          limit: int = 100) -> Dict[str, Any]:
        """Get constitutional violation trends.

        Args:
            agent_id: Optional agent filter.
            limit: Max results.

        Returns:
            Dict with violations list and total.
        """
        params: Dict[str, Any] = {"limit": limit}
        if agent_id:
            params["agent_id"] = agent_id
        return self._get("/audit/violations", params=params)

    def get_constitution(self) -> Dict[str, Any]:
        """View current constitutional invariants.

        Returns:
            Dict with invariants list.
        """
        return self._get("/audit/constitution")

    def update_constitution(self, name: str, rule: str,
                            severity: str = "medium",
                            enabled: bool = True) -> Dict[str, Any]:
        """Add or update a constitutional invariant.

        Args:
            name: Invariant name.
            rule: Rule description.
            severity: low / medium / high / critical.
            enabled: Whether the invariant is active.

        Returns:
            Dict with status and total invariants.
        """
        return self._request("PUT", "/audit/constitution", json={
            "name": name,
            "rule": rule,
            "severity": severity,
            "enabled": enabled,
        })

    def dcsa_metrics(self) -> Dict[str, Any]:
        """Get real-time DCSA-EJP metrics.

        Returns:
            Dict with AEDY, JPC, MCR, FBB, TSAD, EDQ.
        """
        return self._get("/audit/metrics")

    # ── A2A Protocol ─────────────────────────────────────────────────

    def register_agent_card(self, agent_id: str, name: str,
                             description: str = "", version: str = "1.0.0",
                             capabilities: List[str] = None,
                             endpoints: Dict[str, str] = None,
                             skills: List[Dict[str, Any]] = None,
                             input_modes: List[str] = None,
                             output_modes: List[str] = None,
                             security_level: str = "low") -> Dict[str, Any]:
        """注册 Agent 到 A2A 联邦能力目录。"""
        return self._post("/a2a/agents/register", json={
            "agent_id": agent_id,
            "name": name,
            "description": description,
            "version": version,
            "capabilities": capabilities or [],
            "endpoints": endpoints or {},
            "skills": skills or [],
            "input_modes": input_modes or ["text"],
            "output_modes": output_modes or ["text"],
            "security_level": security_level,
        })

    def get_agent_card(self, agent_id: str) -> Dict[str, Any]:
        """获取 Agent 能力卡片。"""
        return self._get(f"/a2a/agents/{agent_id}/card")

    def list_a2a_agents(self) -> Dict[str, Any]:
        """列出所有注册的 Agent。"""
        return self._get("/a2a/agents")

    def unregister_agent(self, agent_id: str) -> Dict[str, Any]:
        """注销 Agent。"""
        return self._delete(f"/a2a/agents/{agent_id}")

    def create_a2a_task(self, task_id: str, from_agent: str,
                         to_agent: str, payload: str = "{}") -> Dict[str, Any]:
        """创建跨 Agent 任务。"""
        return self._post("/a2a/tasks", json={
            "task_id": task_id,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "payload": payload,
        })

    def query_a2a_task(self, task_id: str) -> Dict[str, Any]:
        """查询跨 Agent 任务。"""
        return self._get(f"/a2a/tasks/{task_id}")

    def update_a2a_task(self, task_id: str, status: str,
                         result: str = None) -> Dict[str, Any]:
        """更新跨 Agent 任务状态。"""
        payload = {"status": status}
        if result is not None:
            payload["result"] = result
        return self._request("PUT", f"/a2a/tasks/{task_id}", json=payload)

    def list_a2a_tasks(self, agent_id: str = None,
                        status: str = None) -> Dict[str, Any]:
        """列出跨 Agent 任务。"""
        params = {}
        if agent_id:
            params["agent_id"] = agent_id
        if status:
            params["status"] = status
        return self._get("/a2a/tasks", params=params if params else None)

    def send_a2a_message(self, from_agent: str, method: str,
                          params: Dict[str, Any] = None,
                          to_agent: str = None,
                          req_id: str = None) -> Dict[str, Any]:
        """发送 A2A 消息（JSON-RPC 2.0）。"""
        payload = {
            "from_agent": from_agent,
            "method": method,
            "params": params or {},
        }
        if to_agent:
            payload["to_agent"] = to_agent
        if req_id:
            payload["id"] = req_id
        return self._post("/a2a/message", json=payload)

    def match_agent_by_capability(self, capability: str) -> Dict[str, Any]:
        """按能力匹配最佳 Agent。"""
        return self._get(f"/a2a/match?capability={capability}")

    # ── Marvis Adapter (v8.0.0) ───────────────────────────────────────

    def marvis_adapter(self, agent_id: str = "marvis-main") -> "MarvisAdapter":
        """创建 MarvisAdapter 快捷实例。

        Args:
            agent_id: Marvis orchestrator agent ID.

        Returns:
            MarvisAdapter 实例，已绑定当前 Trinity 实例。
        """
        from trinity.a2a.adapters.marvis_adapter import MarvisAdapter
        return MarvisAdapter(trinity_base_url=self.base_url, agent_id=agent_id)

    # ── Health ────────────────────────────────────────────────────────

    def health(self) -> Dict[str, Any]:
        """Server health check.

        Returns:
            Dict with status, version, uptime, memory_count, etc.
        """
        return self._get("/health")
