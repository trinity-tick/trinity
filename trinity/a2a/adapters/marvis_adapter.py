"""
MarvisAdapter — Marvis-to-Trinity A2A Protocol Bridge.

Connects the Marvis orchestration layer to Trinity's A2A protocol,
enabling unified memory / identity / task-dispatch / audit operations
through a single adapter facade.

Key capabilities:
  - Memory:   remember / recall / forget / summarize_session
  - Identity: register_sub_agent / get_sub_agent_profile / detect_sub_agent_drift
  - Dispatch: dispatch_to_sub_agent / query_sub_agent_task / cancel_sub_agent_task
  - Audit:    audit_sub_agent_action / get_sub_agent_audit_trail / get_sub_agent_trust_score
  - Marvis:   register_marvis_agent_card / sync_sub_agent_context / get_global_memory_snapshot
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Try to import optional dependencies ──────────────────────────────
try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


# ═══════════════════════════════════════════════════════════════════════════
# Data Models
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class MemoryResult:
    """Structured recall result from Trinity memory."""
    memory_id: str
    content: str
    modality: str = "text"
    relevance_score: float = 0.0
    created_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskResult:
    """A2A task dispatch result."""
    task_id: str
    status: str
    from_agent: str
    to_agent: str
    created_at: str = ""
    result: Optional[Dict[str, Any]] = None


@dataclass
class TrustScore:
    """DCSA-EJP trust score for a sub-agent."""
    agent_name: str
    overall_score: float = 1.0       # 0.0 – 1.0
    aedy: float = 0.0                 # Audit Execution Density
    jpc: float = 0.0                  # Justification Packet Completeness
    mcr: float = 0.0                  # Manual Conflict Resolution rate
    tsad: float = 0.0                 # Time on Audit (seconds avg)
    edq: float = 0.0                  # Ethical Decision Quality
    violation_count: int = 0
    last_audited: str = ""


@dataclass
class GlobalSnapshot:
    """Cross-agent memory and identity snapshot for Marvis decision-making."""
    agent_count: int = 0
    total_memories: int = 0
    sub_agent_profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    recent_tasks: List[Dict[str, Any]] = field(default_factory=list)
    trust_scores: Dict[str, TrustScore] = field(default_factory=dict)
    timestamp: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# MarvisAdapter
# ═══════════════════════════════════════════════════════════════════════════

class MarvisAdapter:
    """Bridge between Marvis orchestration layer and Trinity A2A protocol.

    Provides a unified API surface covering all four Trinity v8.0 core layers:
    memory operations, identity management, A2A task dispatch, and DCSA-EJP audit.

    Usage::

        adapter = MarvisAdapter(trinity_base_url="http://localhost:8001")
        adapter.register_marvis_agent_card()

        # Memory
        mid = adapter.remember("marvis-main", "user prefers dark mode")
        results = adapter.recall("marvis-main", "dark mode")

        # Sub-agent dispatch
        task = adapter.dispatch_to_sub_agent(
            "marvis-main", "search-agent",
            "Find recent papers on RAG", {"topic": "RAG"}
        )

        # Audit
        score = adapter.get_sub_agent_trust_score("search-agent")
    """

    def __init__(
        self,
        trinity_base_url: str = "http://localhost:8001",
        agent_id: str = "marvis-main",
    ):
        if not _HAS_REQUESTS:
            raise RuntimeError("'requests' package is required. Install with: pip install requests")

        self.base_url = trinity_base_url.rstrip("/")
        self.agent_id = agent_id
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._lock = threading.RLock()
        self._sub_agent_cache: Dict[str, Dict[str, Any]] = {}

        logger.info("MarvisAdapter initialized — agent=%s, trinity=%s", agent_id, trinity_base_url)

    # ── Internal HTTP Helpers ─────────────────────────────────────────

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            resp = self._session.get(f"{self.base_url}{path}", params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("GET %s failed: %s", path, e)
            return {"error": str(e)}

    def _post(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            resp = self._session.post(f"{self.base_url}{path}", json=data, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("POST %s failed: %s", path, e)
            return {"error": str(e)}

    def _delete(self, path: str) -> Dict[str, Any]:
        try:
            resp = self._session.delete(f"{self.base_url}{path}", timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("DELETE %s failed: %s", path, e)
            return {"error": str(e)}

    # ═══════════════════════════════════════════════════════════════════
    # Memory Operations (§1 — Trinity Core Memory API)
    # ═══════════════════════════════════════════════════════════════════

    def remember(
        self,
        agent_id: str,
        content: str,
        modality: str = "text",
        ttl_seconds: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Write a memory entry into Trinity.

        Args:
            agent_id:  Owning agent identifier.
            content:   Memory content string.
            modality:  Content modality ('text', 'image_ref', 'structured').
            ttl_seconds: Time-to-live in seconds (None for permanent).
            metadata:  Arbitrary key-value metadata.

        Returns:
            Dict with memory_id, status, and created_at.
        """
        payload = {
            "agent_id": agent_id,
            "content": content,
            "modality": modality,
            "metadata": metadata or {},
        }
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds

        result = self._post("/agents/memory/write", payload)
        logger.debug("Memory written: %s → %s", agent_id, result.get("memory_id", "?"))
        return result

    def recall(
        self,
        agent_id: str,
        query: str,
        limit: int = 5,
        modality: Optional[str] = None,
    ) -> List[MemoryResult]:
        """Semantic search over Trinity memories.

        Args:
            agent_id: Agent whose memories to search.
            query:    Search query string.
            limit:    Max results to return.
            modality: Optional filter by modality.

        Returns:
            List of MemoryResult objects sorted by relevance.
        """
        params: Dict[str, Any] = {
            "agent_id": agent_id,
            "query": query,
            "limit": limit,
        }
        if modality:
            params["modality"] = modality

        raw = self._get("/agents/memory/search", params)
        results = raw.get("results", raw.get("memories", []))
        return [
            MemoryResult(
                memory_id=r.get("memory_id", r.get("id", "")),
                content=r.get("content", ""),
                modality=r.get("modality", "text"),
                relevance_score=float(r.get("relevance_score", r.get("score", 0.0))),
                created_at=r.get("created_at", ""),
                metadata=r.get("metadata", {}),
            )
            for r in results
        ]

    def recall_hybrid(
        self,
        agent_id: str,
        query: str,
        limit: int = 10,
        strategy: str = "fusion",
    ) -> Dict[str, Any]:
        """Hybrid retrieval (vector + BM25 + graph).

        Args:
            agent_id: Agent whose memories to search.
            query:    Search query string.
            limit:    Max results to return.
            strategy: ``fusion`` / ``rrf`` / ``cascade``.

        Returns:
            Dict with ``results`` (list of MemoryResult-compatible dicts
            with per-source scores), ``strategy``, ``query``, ``breakdown``.
        """
        payload = {
            "query": query,
            "top_k": limit,
            "strategy": strategy,
            "agent_id": agent_id,
        }
        raw = self._post("/memory/search/hybrid", payload)
        # Normalise result format for Marvis client consumption
        results = raw.get("results", [])
        for r in results:
            r.setdefault("relevance_score", r.get("hybrid_score", 0))
        return raw

    # ═══════════════════════════════════════════════════════════════════
    # Cross-Modal Memory Retrieval (v8.1.0) — Text ↔ Image
    # ═══════════════════════════════════════════════════════════════════

    def recall_by_image(
        self,
        image_path: str,
        top_k: int = 10,
    ) -> Dict[str, Any]:
        """图搜文 — 用图片检索相关文字记忆。

        Parameters
        ----------
        image_path : str
            查询图片的绝对路径。
        top_k : int
            返回结果数量。

        Returns
        -------
        dict with results / query_type='image_to_text' / query_path.
        """
        return self._post("/memory/search/text-by-image", {
            "image_path": image_path,
            "top_k": top_k,
        })

    def recall_image_by_text(
        self,
        text: str,
        top_k: int = 10,
    ) -> Dict[str, Any]:
        """文搜图 — 用文字检索相关图片记忆。

        Parameters
        ----------
        text : str
            自然语言描述要找的图片。
        top_k : int
            返回结果数量。

        Returns
        -------
        dict with results / query_type='text_to_image' / query.
        """
        return self._post("/memory/search/image-by-text", {
            "text": text,
            "top_k": top_k,
        })

    def forget(self, memory_id: str) -> Dict[str, Any]:
        """Soft-delete a memory by ID."""
        return self._delete(f"/memories/{memory_id}")

    def summarize_session(self, agent_id: str) -> Dict[str, Any]:
        """Compress an agent's recent session into a memory summary.

        Returns:
            Dict with summary text and related memory_ids covered.
        """
        return self._post("/agents/memory/summarize", {"agent_id": agent_id})

    # ═══════════════════════════════════════════════════════════════════
    # Identity Management (§2 — Multi-Anchor Identity)
    # ═══════════════════════════════════════════════════════════════════

    def register_sub_agent(
        self,
        agent_name: str,
        capabilities: List[str],
    ) -> Dict[str, Any]:
        """Register a sub-agent with identity anchors + A2A AgentCard.

        Creates identity anchors of all four types (identity_files,
        procedural_patterns, episodic_keys, value_specifications)
        and an A2A AgentCard with the given capabilities.
        """
        identity_result = self._post("/identity/register", {
            "agent_id": agent_name,
            "agent_name": agent_name,
            "capabilities": capabilities,
        })

        # Build and register an A2A AgentCard
        card_result = self._post("/a2a/agents/register", {
            "agent_id": agent_name,
            "name": agent_name,
            "description": f"Marvis sub-agent: {agent_name}",
            "url": f"{self.base_url}/a2a/{agent_name}",
            "version": "8.0.0",
            "capabilities": capabilities,
            "provider": {"organization": "Marvis", "url": ""},
            "skills": [
                {"id": f"{agent_name}-default", "name": cap, "description": cap}
                for cap in capabilities
            ],
        })

        with self._lock:
            self._sub_agent_cache[agent_name] = {
                "registered_at": datetime.now(timezone.utc).isoformat(),
                "capabilities": capabilities,
            }

        return {
            "status": "registered",
            "agent_name": agent_name,
            "identity": identity_result,
            "agent_card": card_result,
        }

    def get_sub_agent_profile(self, agent_name: str) -> Dict[str, Any]:
        """Retrieve a sub-agent's full identity profile."""
        return self._get(f"/identity/profiles/{agent_name}")

    def detect_sub_agent_drift(self, agent_name: str) -> Dict[str, Any]:
        """Detect behavioral drift in a sub-agent's identity anchors.

        Returns:
            Dict with drift_score, drifted_anchors, and recommendations.
        """
        return self._post("/identity/drift", {
            "agent_id": agent_name,
        })

    # ═══════════════════════════════════════════════════════════════════
    # RLM Route methods
    # ═══════════════════════════════════════════════════════════════════

    def route_identity_query(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Route an identity query through the RLMRouter for optimal strategy selection.

        Calls POST /identity/route with the query and optional context.

        Args:
            query:   Natural language identity query.
            context: Optional routing context dict.

        Returns:
            Dict with strategy, confidence, top_k alternatives.
        """
        payload: Dict[str, Any] = {"query": query, "top_k": 3}
        if context:
            payload["context"] = context
        return self._post("/identity/route", payload)

    def report_route_feedback(
        self,
        query: str,
        strategy: str,
        success: bool,
    ) -> Dict[str, Any]:
        """Report feedback about a routing decision for weight adjustment.

        Calls POST /identity/route/feedback with the outcome.

        Args:
            query:    The original query that was routed.
            strategy: The strategy that was selected.
            success:  Whether the routing was successful.

        Returns:
            Dict with old_weight, new_weight, and stats.
        """
        return self._post("/identity/route/feedback", {
            "query": query,
            "strategy": strategy,
            "success": success,
        })

    # ═══════════════════════════════════════════════════════════════════
    # Task Dispatch (§3 — A2A Task Protocol)
    # ═══════════════════════════════════════════════════════════════════

    def dispatch_to_sub_agent(
        self,
        from_agent: str,
        to_agent: str,
        task_description: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> TaskResult:
        """Create an A2A task dispatched to a sub-agent.

        Args:
            from_agent:       Originating agent ID (usually 'marvis-main').
            to_agent:         Target sub-agent ID.
            task_description: Human-readable task summary.
            payload:          Structured task payload.

        Returns:
            TaskResult with task_id and initial status.
        """
        a2a_payload = {
            "from_agent": from_agent,
            "to_agent": to_agent,
            "task_description": task_description,
            "payload": payload or {},
        }
        raw = self._post("/a2a/marvis/dispatch", a2a_payload)
        return TaskResult(
            task_id=raw.get("task_id", ""),
            status=raw.get("status", "pending"),
            from_agent=from_agent,
            to_agent=to_agent,
            created_at=raw.get("created_at", ""),
        )

    def query_sub_agent_task(self, task_id: str) -> TaskResult:
        """Query the status and result of an A2A task."""
        raw = self._get(f"/a2a/tasks/{task_id}")
        return TaskResult(
            task_id=task_id,
            status=raw.get("status", "unknown"),
            from_agent=raw.get("from_agent", ""),
            to_agent=raw.get("to_agent", ""),
            result=raw.get("result"),
            created_at=raw.get("created_at", ""),
        )

    def cancel_sub_agent_task(self, task_id: str) -> Dict[str, Any]:
        """Cancel a pending or in-progress A2A task."""
        return self._post("/a2a/tasks/cancel", {"task_id": task_id})

    def list_sub_agent_tasks(
        self,
        agent_name: str,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List all tasks associated with a sub-agent, optionally filtered by status."""
        params = {"agent_id": agent_name}
        if status:
            params["status"] = status
        raw = self._get("/a2a/tasks", params)
        return raw.get("tasks", [])

    # ═══════════════════════════════════════════════════════════════════
    # Audit & Trust (§4 — DCSA-EJP Double-Loop Audit)
    # ═══════════════════════════════════════════════════════════════════

    def audit_sub_agent_action(
        self,
        agent_name: str,
        action_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute a DCSA-EJP audit on a sub-agent's action.

        Args:
            agent_name:     Target sub-agent.
            action_context: Dict describing the action (type, input, output, source, sink).

        Returns:
            Audit result with overall pass/fail + violations + justification.
        """
        return self._post("/audit/run", {
            "agent_id": agent_name,
            "action": action_context,
            "source": self.agent_id,
        })

    def get_sub_agent_audit_trail(
        self,
        agent_name: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Retrieve the DCSA-EJP audit history for a sub-agent."""
        raw = self._get(f"/audit/runs", {"agent_id": agent_name, "limit": limit})
        return raw.get("runs", [])

    def get_sub_agent_trust_score(self, agent_name: str) -> TrustScore:
        """Calculate a composite trust score using DCSA-EJP metrics.

        Based on AEDY, JPC, MCR, TSAD, and EDQ from the audit trail.
        Falls back to GET /a2a/marvis/agents/{name}/trust if available.
        """
        # Try the dedicated Marvis trust endpoint first
        raw = self._get(f"/a2a/marvis/agents/{agent_name}/trust")
        if "error" not in raw:
            return TrustScore(
                agent_name=agent_name,
                overall_score=raw.get("overall_score", 1.0),
                aedy=raw.get("aedy", 0.0),
                jpc=raw.get("jpc", 0.0),
                mcr=raw.get("mcr", 0.0),
                tsad=raw.get("tsad", 0.0),
                edq=raw.get("edq", 0.0),
                violation_count=raw.get("violation_count", 0),
                last_audited=raw.get("last_audited", ""),
            )

        # Fallback: compute from audit metrics
        metrics_raw = self._get("/audit/metrics", {"agent_id": agent_name})
        metrics = metrics_raw.get("metrics", {})
        violation_count = metrics.get("violation_count", 0)
        overall = max(0.0, 1.0 - 0.05 * violation_count)
        return TrustScore(
            agent_name=agent_name,
            overall_score=round(overall, 4),
            aedy=metrics.get("aedy", 0.0),
            jpc=metrics.get("jpc", 0.0),
            mcr=metrics.get("mcr", 0.0),
            tsad=metrics.get("tsad", 0.0),
            edq=metrics.get("edq", 0.0),
            violation_count=violation_count,
            last_audited=metrics.get("last_audited", ""),
        )

    # ═══════════════════════════════════════════════════════════════════
    # Marvis-Specific Operations
    # ═══════════════════════════════════════════════════════════════════

    def register_marvis_agent_card(self) -> Dict[str, Any]:
        """Register Marvis main agent's A2A AgentCard with full capabilities.

        This should be called once during Marvis initialization. The card
        declares Marvis as the orchestrator agent with dispatch + audit + memory
        capabilities, enabling other agents to discover and route to it.
        """
        return self._post("/a2a/marvis/agents/register", {
            "agent_id": self.agent_id,
            "agent_name": "Marvis Main Orchestrator",
            "capabilities": [
                "agent_orchestration",
                "task_dispatch",
                "memory_management",
                "identity_management",
                "constitutional_audit",
                "context_synchronization",
            ],
            "metadata": {
                "role": "orchestrator",
                "version": "8.0.0",
                "protocol": "A2A v0.3",
            },
        })

    def sync_sub_agent_context(
        self,
        from_agent: str,
        to_agent: str,
        context_dict: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Synchronize context from one agent to another via Trinity memory.

        Writes the context_dict as a structured memory under to_agent's
        namespace, ensuring sub-agents have access to Marvis's current
        understanding of the task state.

        Args:
            from_agent:   Source agent (usually 'marvis-main').
            to_agent:     Target agent to receive context.
            context_dict: Key-value context to synchronize.

        Returns:
            Dict with synced memory_ids.
        """
        content = json.dumps(context_dict, ensure_ascii=False)
        return self.remember(
            agent_id=to_agent,
            content=f"[MARVIS_CONTEXT from {from_agent}] {content}",
            modality="structured",
            metadata={
                "source": from_agent,
                "type": "context_sync",
                "synced_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def get_global_memory_snapshot(self) -> GlobalSnapshot:
        """Build a cross-agent memory and identity snapshot for global decision-making.

        Queries all registered agents, their memory pools, recent tasks,
        and trust scores to provide a unified view for Marvis orchestration.

        Returns:
            GlobalSnapshot with agent profiles, memories, tasks, and trust data.
        """
        # Use the dedicated snapshot endpoint if available
        raw = self._get("/a2a/marvis/snapshot")
        if "error" not in raw and raw:
            trust_map = {}
            for name, ts in raw.get("trust_scores", {}).items():
                trust_map[name] = TrustScore(
                    agent_name=name,
                    overall_score=ts.get("overall_score", 1.0),
                    aedy=ts.get("aedy", 0.0),
                    jpc=ts.get("jpc", 0.0),
                    mcr=ts.get("mcr", 0.0),
                    tsad=ts.get("tsad", 0.0),
                    edq=ts.get("edq", 0.0),
                    violation_count=ts.get("violation_count", 0),
                    last_audited=ts.get("last_audited", ""),
                )
            return GlobalSnapshot(
                agent_count=raw.get("agent_count", 0),
                total_memories=raw.get("total_memories", 0),
                sub_agent_profiles=raw.get("sub_agent_profiles", {}),
                recent_tasks=raw.get("recent_tasks", []),
                trust_scores=trust_map,
                timestamp=raw.get("timestamp", datetime.now(timezone.utc).isoformat()),
            )

        # Fallback: manually aggregate
        profiles = self._get("/identity/profiles")
        mem_stats = self._get("/agents/memory/pool")
        tasks_raw = self._get("/a2a/tasks", {"limit": 10})

        agent_profiles = profiles.get("profiles", profiles if isinstance(profiles, dict) else {})
        trust_scores: Dict[str, TrustScore] = {}
        for name in agent_profiles:
            trust_scores[name] = self.get_sub_agent_trust_score(name)

        return GlobalSnapshot(
            agent_count=len(agent_profiles),
            total_memories=mem_stats.get("total_memories", 0),
            sub_agent_profiles=agent_profiles,
            recent_tasks=tasks_raw.get("tasks", []),
            trust_scores=trust_scores,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ═══════════════════════════════════════════════════════════════════
    # Memory Compression (Letta-style virtual context management)
    # ═══════════════════════════════════════════════════════════════════

    def compress_context(
        self,
        agent_id: str,
        max_tokens: int = 4096,
        no_compress: bool = False,
    ) -> Dict[str, Any]:
        """Compress the agent's context window using Trinity memory compression.

        Delegates to the Trinity `/memory/compress` endpoint which runs
        the three-stage pipeline: dedup → importance sort → summarise.

        Parameters
        ----------
        agent_id : str
            Target agent for context compression.
        max_tokens : int
            Token budget ceiling (default 4096).
        no_compress : bool
            Pass True to skip compression entirely.

        Returns
        -------
        dict with active_count / trimmed_count / summary / budget_usage.
        """
        return self._post("/memory/compress", {
            "agent_id": agent_id,
            "max_tokens": max_tokens,
            "no_compress": no_compress,
        })

    def restore_context(
        self,
        agent_id: str,
        trimmed_ids: List[str],
    ) -> Dict[str, Any]:
        """Restore previously-trimmed memories back into the agent's context.

        Calls the Trinity `/memory/compress/restore` endpoint with the
        list of memory IDs to restore.

        Parameters
        ----------
        agent_id : str
            Target agent for restoration.
        trimmed_ids : List[str]
            List of memory IDs to restore (from previous compress result).

        Returns
        -------
        dict with restored / restored_count / failed / failed_count.
        """
        return self._post("/memory/compress/restore", {
            "agent_id": agent_id,
            "trimmed_ids": trimmed_ids,
        })
