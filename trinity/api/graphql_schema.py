"""
P2-3: Trinity GraphQL Interface (Strawberry)
==============================================

基于 Strawberry 的 GraphQL 查询接口，补充 REST API，
为 Trinity 提供声明式数据查询能力。

支持: Memory CRUD, Agent 管理, 向量搜索, Persona 查询,
      诊断信息, 批量操作, Subscription (WebSocket)。

Usage:
    from trinity.api.graphql_schema import schema
    strawberry server schema
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional

import strawberry
from strawberry.types import Info

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────────

@strawberry.enum
class MemoryStatus(Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    SOFT_DELETED = "soft_deleted"
    PENDING = "pending"


@strawberry.enum
class RiskLevel(Enum):
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@strawberry.enum
class SearchStrategy(Enum):
    SEMANTIC = "semantic"
    KEYWORD = "keyword"
    HYBRID = "hybrid"
    CAUSAL = "causal"


# ── Types ────────────────────────────────────────────────────────────────

@strawberry.type
class HealthStatus:
    status: str
    version: str
    uptime_seconds: float
    memory_count: int
    agent_count: int
    component_status: str

    @strawberry.field
    def is_healthy(self) -> bool:
        return self.status == "ok" and "unhealthy" not in self.component_status.lower()


@strawberry.type
class Memory:
    memory_id: str
    content: str
    persona_id: str = ""
    agent_id: str = ""
    status: MemoryStatus = MemoryStatus.ACTIVE
    tags: List[str] = field(default_factory=list)
    confidence: float = 1.0
    created_at: str = ""
    updated_at: str = ""

    @strawberry.field
    def summary(self, max_length: int = 120) -> str:
        return self.content[:max_length] + ("..." if len(self.content) > max_length else "")


@strawberry.type
class MemoryEdge:
    source_id: str
    target_id: str
    relation: str
    weight: float = 1.0
    evidence: str = ""


@strawberry.type
class Agent:
    agent_id: str
    name: str
    role: str
    status: str = "active"
    registered_at: str = ""
    memory_pool_size: int = 0

    @strawberry.field
    def label(self) -> str:
        return f"{self.name} ({self.role})"


@strawberry.type
class Persona:
    persona_id: str
    name: str
    description: str = ""
    memory_count: int = 0
    created_at: str = ""


@strawberry.type
class SearchResult:
    score: float
    memory: Memory
    matched_segments: List[str] = field(default_factory=list)


@strawberry.type
class VectorSearchHit:
    vector_id: str
    similarity: float
    memory_id: str = ""
    metadata: str = "{}"

    @strawberry.field
    def meta_dict(self) -> str:
        return self.metadata


@strawberry.type
class BatchResult:
    success_count: int
    failure_count: int
    errors: List[str] = field(default_factory=list)


@strawberry.type
class Diagnostics:
    component: str
    health: str
    latency_ms: float
    error_rate: float
    details: str = ""

    @strawberry.field
    def is_healthy(self) -> bool:
        return self.health in ("ok", "healthy", "degraded")


@strawberry.type
class Insight:
    insight_id: str
    title: str
    description: str
    confidence: float
    related_agents: List[str] = field(default_factory=list)
    generated_at: str = ""


@strawberry.type
class TimelinePoint:
    time: str
    event: str
    actor: str = ""
    detail: str = ""


# ── Inputs ───────────────────────────────────────────────────────────────

@strawberry.input
class MemoryInput:
    content: str
    persona_id: str = ""
    agent_id: str = ""
    tags: List[str] = field(default_factory=list)
    confidence: float = 1.0


@strawberry.input
class MemoryFilter:
    status: Optional[MemoryStatus] = None
    persona_id: Optional[str] = None
    agent_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    min_confidence: Optional[float] = None
    created_after: Optional[str] = None
    created_before: Optional[str] = None


@strawberry.input
class MemoryBulkInput:
    memories: List[MemoryInput]


@strawberry.input
class AgentInput:
    name: str
    role: str
    metadata: str = "{}"


@strawberry.input
class VectorSearchInput:
    query: str
    top_k: int = 10
    strategy: SearchStrategy = SearchStrategy.SEMANTIC
    namespace: str = "default"


@strawberry.input
class InsightQuery:
    agent_ids: List[str] = field(default_factory=list)
    time_range_hours: int = 24
    min_confidence: float = 0.5


# ── Resolver Backend (Mock Trinity Bridge) ───────────────────────────────

class _TrinityResolver:
    """轻量解析器后端：模拟 Trinity 核心的查询/写入能力。
    生产环境中替换为真实 Trinity / REST 客户端调用。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._memories: Dict[str, Dict[str, Any]] = {}
        self._agents: Dict[str, Dict[str, Any]] = {}
        self._personas: Dict[str, Dict[str, Any]] = {}
        self._edges: Dict[str, Dict[str, Any]] = {}
        self._vectors: Dict[str, Dict[str, Any]] = {}
        self._insights: List[Dict[str, Any]] = []
        self._start_time = time.time()
        self._version = "9.0.0-p2.3"
        self._seed_data()

    def _seed_data(self):
        pid = f"p_{uuid.uuid4().hex[:8]}"
        self._personas[pid] = {"persona_id": pid, "name": "Default",
                                "description": "Auto-created persona", "memory_count": 0,
                                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")}

    # ── Health ──
    def health(self) -> HealthStatus:
        return HealthStatus(
            status="ok", version=self._version,
            uptime_seconds=round(time.time() - self._start_time, 1),
            memory_count=len(self._memories), agent_count=len(self._agents),
            component_status="all_green",
        )

    # ── Memory CRUD ──
    def create_memory(self, input_data: MemoryInput) -> Memory:
        with self._lock:
            mid = f"mem_{uuid.uuid4().hex[:12]}"
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
            record = {"memory_id": mid, "content": input_data.content,
                      "persona_id": input_data.persona_id,
                      "agent_id": input_data.agent_id,
                      "status": "active", "tags": input_data.tags,
                      "confidence": input_data.confidence,
                      "created_at": now, "updated_at": now}
            self._memories[mid] = record
            if input_data.persona_id in self._personas:
                self._personas[input_data.persona_id]["memory_count"] += 1
            return self._to_memory(record)

    def get_memory(self, memory_id: str) -> Optional[Memory]:
        with self._lock:
            r = self._memories.get(memory_id)
            return self._to_memory(r) if r else None

    def search_memories(self, query: str, filter_data: Optional[MemoryFilter] = None,
                        top_k: int = 10, strategy: SearchStrategy = SearchStrategy.KEYWORD) -> List[SearchResult]:
        with self._lock:
            results: List[SearchResult] = []
            query_lower = query.lower()
            for m in self._memories.values():
                if filter_data:
                    if filter_data.status and m["status"] != filter_data.status.value:
                        continue
                    if filter_data.persona_id and m["persona_id"] != filter_data.persona_id:
                        continue
                    if filter_data.agent_id and m["agent_id"] != filter_data.agent_id:
                        continue
                    if filter_data.min_confidence and m["confidence"] < filter_data.min_confidence:
                        continue
                    if filter_data.tags and not set(filter_data.tags).intersection(m.get("tags", [])):
                        continue
                score = 0.0
                if strategy in (SearchStrategy.KEYWORD, SearchStrategy.HYBRID):
                    content_lower = m["content"].lower()
                    if query_lower in content_lower:
                        score = len(query_lower) / max(len(content_lower), 1) * 0.9
                if strategy in (SearchStrategy.SEMANTIC, SearchStrategy.HYBRID):
                    qt = set(query_lower.split())
                    ct = set(m["content"].lower().split())
                    overlap = len(qt & ct) / max(len(qt), 1)
                    score = max(score, overlap * 0.7)
                if score > 0:
                    results.append(SearchResult(score=round(score, 4),
                        memory=self._to_memory(m),
                        matched_segments=[m["content"][:200]]))
            results.sort(key=lambda x: x.score, reverse=True)
            return results[:top_k]

    def delete_memory(self, memory_id: str) -> bool:
        with self._lock:
            if memory_id in self._memories:
                self._memories[memory_id]["status"] = "soft_deleted"
                return True
            return False

    # ── Bulk ──
    def bulk_create_memories(self, inputs: List[MemoryInput]) -> BatchResult:
        ok, fail = 0, 0
        errors: List[str] = []
        for inp in inputs:
            try:
                self.create_memory(inp)
                ok += 1
            except Exception as e:
                fail += 1
                errors.append(str(e))
        return BatchResult(success_count=ok, failure_count=fail, errors=errors)

    # ── Agents ──
    def list_agents(self) -> List[Agent]:
        with self._lock:
            return [Agent(agent_id=a["agent_id"], name=a["name"],
                          role=a["role"], status=a.get("status", "active"),
                          registered_at=a.get("registered_at", ""),
                          memory_pool_size=a.get("memory_pool_size", 0))
                    for a in self._agents.values()]

    def register_agent(self, input_data: AgentInput) -> Agent:
        with self._lock:
            aid = f"agent_{uuid.uuid4().hex[:8]}"
            record = {"agent_id": aid, "name": input_data.name,
                      "role": input_data.role, "status": "active",
                      "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "metadata": input_data.metadata, "memory_pool_size": 0}
            self._agents[aid] = record
            return Agent(agent_id=aid, name=record["name"],
                         role=record["role"], status="active",
                         registered_at=record["registered_at"])

    # ── Personas ──
    def list_personas(self) -> List[Persona]:
        with self._lock:
            return [Persona(**p) for p in self._personas.values()]

    # ── Vector Search ──
    def vector_search(self, input_data: VectorSearchInput) -> List[VectorSearchHit]:
        with self._lock:
            hits: List[VectorSearchHit] = []
            qt = set(input_data.query.lower().split())
            for vid, vec in self._vectors.items():
                vt = set(vec.get("text", "").lower().split())
                overlap = len(qt & vt) / max(len(qt), 1)
                if overlap > 0:
                    hits.append(VectorSearchHit(
                        vector_id=vid, similarity=round(overlap * 0.85, 4),
                        memory_id=vec.get("memory_id", ""),
                        metadata=json.dumps(vec.get("metadata", {}))))
            hits.sort(key=lambda h: h.similarity, reverse=True)
            return hits[:input_data.top_k]

    def index_vector(self, memory_id: str, text: str, namespace: str = "default") -> str:
        with self._lock:
            vid = f"vec_{uuid.uuid4().hex[:8]}"
            self._vectors[vid] = {"text": text, "memory_id": memory_id,
                                  "namespace": namespace, "metadata": {}}
            return vid

    # ── Diagnostics ──
    def diagnostics(self, component: Optional[str] = None) -> List[Diagnostics]:
        diag = [
            Diagnostics(component="api", health="ok", latency_ms=12.3, error_rate=0.001,
                        details="REST + GraphQL serving normally"),
            Diagnostics(component="memory_store", health="ok", latency_ms=5.1, error_rate=0.0,
                        details=f"{len(self._memories)} memories stored"),
            Diagnostics(component="vector_index", health="ok", latency_ms=8.7, error_rate=0.002,
                        details=f"{len(self._vectors)} vectors indexed"),
            Diagnostics(component="agent_pool", health="ok", latency_ms=3.4, error_rate=0.0,
                        details=f"{len(self._agents)} agents registered"),
        ]
        if component:
            return [d for d in diag if d.component == component]
        return diag

    # ── Insights ──
    def cross_agent_insights(self, query_data: InsightQuery) -> List[Insight]:
        with self._lock:
            insights = [
                Insight(insight_id=f"ins_{uuid.uuid4().hex[:8]}",
                        title="Cross-agent memory overlap detected",
                        description="Two or more agents share semantically similar memory fragments.",
                        confidence=0.82, related_agents=query_data.agent_ids[:2],
                        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ")),
            ]
            return [i for i in insights if i.confidence >= query_data.min_confidence]

    # ── Timeline ──
    def memory_timeline(self, persona_id: str, limit: int = 20) -> List[TimelinePoint]:
        with self._lock:
            points: List[TimelinePoint] = []
            for m in sorted(self._memories.values(), key=lambda x: x["created_at"]):
                if m["persona_id"] == persona_id:
                    points.append(TimelinePoint(time=m["created_at"],
                        event=f"Memory stored: {m['content'][:80]}",
                        actor=m.get("agent_id", "system")))
            return points[-limit:]

    @staticmethod
    def _to_memory(record: Dict[str, Any]) -> Memory:
        return Memory(
            memory_id=record["memory_id"], content=record["content"],
            persona_id=record.get("persona_id", ""),
            agent_id=record.get("agent_id", ""),
            status=MemoryStatus(record.get("status", "active")),
            tags=record.get("tags", []), confidence=record.get("confidence", 1.0),
            created_at=record.get("created_at", ""),
            updated_at=record.get("updated_at", ""),
        )


# ── Resolver Singleton ───────────────────────────────────────────────────

_resolver = _TrinityResolver()


# ── Queries ──────────────────────────────────────────────────────────────

# ── DSH 结构层类型（结构融合：Trinity 承载 DSH 会话事件流/goal/schedule）─

@strawberry.type
class StructureStats:
    sessions: int
    events: int
    goals: int
    todos: int
    headers: int
    schedules: int
    event_types: str


@strawberry.type
class StructureSession:
    session_id: str
    agent_id: str
    persona_id: str = ""
    parent_session: Optional[str] = None
    created_at: float
    updated_at: float
    status: str = "active"
    title: Optional[str] = None


@strawberry.type
class StructureEvent:
    session_id: str
    seq: int
    type: str
    turn: Optional[int] = None
    step: Optional[int] = None
    time: float
    data: str


@strawberry.type
class StructureGoal:
    goal_id: str
    objective: str
    status: str = "active"
    phase: Optional[str] = None
    round: int = 0
    max_rounds: Optional[int] = None
    created_at: float
    updated_at: float


@strawberry.type
class StructureSchedule:
    schedule_id: str
    prompt: str
    target: Optional[str] = None
    status: str = "active"
    created_at: float
    updated_at: float


from trinity.structure_store import (  # noqa: E402
    structure_stats as _store_stats,
    structure_sessions as _store_sessions,
    structure_query as _store_query,
    goal_list as _store_goals,
    schedule_list as _store_schedules,
)


def _structure_stats() -> Optional[StructureStats]:
    try:
        s = _store_stats()
        return StructureStats(
            sessions=s.get("sessions", 0), events=s.get("events", 0),
            goals=s.get("goals", 0), todos=s.get("todos", 0),
            headers=s.get("headers", 0), schedules=s.get("schedules", 0),
            event_types=json.dumps(s.get("event_types", {}), ensure_ascii=False),
        )
    except Exception as exc:
        logger.warning("structure_stats failed: %s", exc)
        return None


def _structure_sessions(limit: int = 200) -> List[StructureSession]:
    try:
        data = _store_sessions()
        return [StructureSession(**s) for s in data.get("sessions", [])[: min(limit, 1000)]]
    except Exception as exc:
        logger.warning("structure_sessions failed: %s", exc)
        return []


def _structure_events(session_id: Optional[str], event_type: Optional[str],
                      agent_id: Optional[str], limit: int = 200) -> List[StructureEvent]:
    try:
        data = _store_query({
            "session_id": session_id, "type": event_type,
            "agent_id": agent_id, "limit": limit,
        })
        return [
            StructureEvent(
                session_id=e["session_id"], seq=e["seq"], type=e["type"],
                turn=e.get("turn"), step=e.get("step"), time=e["time"],
                data=json.dumps(e.get("data", {}), ensure_ascii=False),
            ) for e in data.get("events", [])
        ]
    except Exception as exc:
        logger.warning("structure_events failed: %s", exc)
        return []


def _structure_goals(limit: int = 100) -> List[StructureGoal]:
    try:
        data = _store_goals()
        return [StructureGoal(**g) for g in data.get("goals", [])[: min(limit, 500)]]
    except Exception as exc:
        logger.warning("structure_goals failed: %s", exc)
        return []


def _structure_schedules(limit: int = 100) -> List[StructureSchedule]:
    try:
        data = _store_schedules()
        return [StructureSchedule(**s) for s in data.get("schedules", [])[: min(limit, 500)]]
    except Exception as exc:
        logger.warning("structure_schedules failed: %s", exc)
        return []


@strawberry.type
class Query:

    @strawberry.field
    def health(self) -> HealthStatus:
        return _resolver.health()

    @strawberry.field
    def memory(self, memory_id: str) -> Optional[Memory]:
        return _resolver.get_memory(memory_id)

    @strawberry.field
    def search_memories(self, query: str, filter: Optional[MemoryFilter] = None,
                        top_k: int = 10,
                        strategy: SearchStrategy = SearchStrategy.KEYWORD) -> List[SearchResult]:
        return _resolver.search_memories(query, filter, top_k, strategy)

    @strawberry.field
    def agents(self) -> List[Agent]:
        return _resolver.list_agents()

    @strawberry.field
    def agent(self, agent_id: str) -> Optional[Agent]:
        for a in _resolver.list_agents():
            if a.agent_id == agent_id:
                return a
        return None

    @strawberry.field
    def personas(self) -> List[Persona]:
        return _resolver.list_personas()

    @strawberry.field
    def vector_search(self, input: VectorSearchInput) -> List[VectorSearchHit]:
        return _resolver.vector_search(input)

    @strawberry.field
    def diagnostics(self, component: Optional[str] = None) -> List[Diagnostics]:
        return _resolver.diagnostics(component)

    @strawberry.field
    def cross_agent_insights(self, input: InsightQuery) -> List[Insight]:
        return _resolver.cross_agent_insights(input)

    @strawberry.field
    def memory_timeline(self, persona_id: str, limit: int = 20) -> List[TimelinePoint]:
        return _resolver.memory_timeline(persona_id, limit)

    # ── DSH 结构层（结构融合：Trinity 承载 DSH 会话事件流/goal/schedule）──
    @strawberry.field
    def structure_stats(self) -> Optional[StructureStats]:
        return _structure_stats()

    @strawberry.field
    def structure_sessions(self, limit: int = 200) -> List[StructureSession]:
        return _structure_sessions(limit)

    @strawberry.field
    def structure_events(self, session_id: Optional[str] = None,
                         event_type: Optional[str] = None,
                         agent_id: Optional[str] = None,
                         limit: int = 200) -> List[StructureEvent]:
        return _structure_events(session_id, event_type, agent_id, limit)

    @strawberry.field
    def structure_goals(self, limit: int = 100) -> List[StructureGoal]:
        return _structure_goals(limit)

    @strawberry.field
    def structure_schedules(self, limit: int = 100) -> List[StructureSchedule]:
        return _structure_schedules(limit)


# ── Mutations ────────────────────────────────────────────────────────────

@strawberry.type
class Mutation:

    @strawberry.mutation
    def create_memory(self, input: MemoryInput) -> Memory:
        return _resolver.create_memory(input)

    @strawberry.mutation
    def delete_memory(self, memory_id: str) -> bool:
        return _resolver.delete_memory(memory_id)

    @strawberry.mutation
    def bulk_create_memories(self, input: MemoryBulkInput) -> BatchResult:
        return _resolver.bulk_create_memories(input.memories)

    @strawberry.mutation
    def register_agent(self, input: AgentInput) -> Agent:
        return _resolver.register_agent(input)

    @strawberry.mutation
    def index_vector(self, memory_id: str, text: str,
                     namespace: str = "default") -> str:
        return _resolver.index_vector(memory_id, text, namespace)

    @strawberry.mutation
    def create_persona(self, name: str, description: str = "") -> Persona:
        pid = f"p_{uuid.uuid4().hex[:8]}"
        record = {"persona_id": pid, "name": name,
                  "description": description, "memory_count": 0,
                  "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
        _resolver._personas[pid] = record
        return Persona(**record)


# ── Subscriptions ────────────────────────────────────────────────────────

@strawberry.type
class Subscription:

    @strawberry.subscription
    async def memory_created(self, info: Info,
                             persona_id: Optional[str] = None) -> AsyncIterator[Memory]:
        """新记忆创建时推送。生产环境替换为 Redis Pub/Sub 或 Kafka。"""
        seen: set = set()
        while True:
            await _async_sleep(2.0)
            with _resolver._lock:
                for m in list(_resolver._memories.values()):
                    mid = m["memory_id"]
                    if mid not in seen:
                        if persona_id is None or m.get("persona_id") == persona_id:
                            seen.add(mid)
                            yield _resolver._to_memory(m)

    @strawberry.subscription
    async def health_watch(self, info: Info, interval_seconds: int = 10) -> AsyncIterator[HealthStatus]:
        """定期健康状态推送。"""
        while True:
            await _async_sleep(float(interval_seconds))
            yield _resolver.health()


async def _async_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


# ── Schema ───────────────────────────────────────────────────────────────

schema = strawberry.Schema(query=Query, mutation=Mutation, subscription=Subscription)


# ── Self-Test ────────────────────────────────────────────────────────────

def self_test() -> Dict[str, Any]:
    """GraphQL schema 自检。

    验证所有 Query / Mutation / Subscription 类型可正确解析，
    并执行集成级 smoke test。
    """
    results: Dict[str, Any] = {"module": "P2-3_graphql_schema", "passed": 0, "failed": 0, "details": []}

    def _pass(test: str):
        results["passed"] += 1
        results["details"].append({"test": test, "status": "PASS"})

    def _fail(test: str, reason: str):
        results["failed"] += 1
        results["details"].append({"test": test, "status": "FAIL", "reason": reason})

    # Test 1: Schema introspection
    try:
        assert schema.query is not None, "Query type missing"
        assert schema.mutation is not None, "Mutation type missing"
        assert schema.subscription is not None, "Subscription type missing"
        _pass("Schema structure (Query/Mutation/Subscription)")
    except Exception as e:
        _fail("Schema structure", str(e))

    # Test 2: Health query
    try:
        h = _resolver.health()
        assert h.status == "ok", f"Expected ok, got {h.status}"
        assert h.is_healthy(), "Expected healthy"
        _pass("Health query")
    except Exception as e:
        _fail("Health query", str(e))

    # Test 3: Memory CRUD
    try:
        m = _resolver.create_memory(MemoryInput(content="Test memory via GraphQL",
                                                 persona_id="p_test",
                                                 tags=["graphql", "test"]))
        assert m.memory_id.startswith("mem_"), f"Bad id: {m.memory_id}"
        assert "Test memory" in m.content, f"Bad content: {m.content}"

        got = _resolver.get_memory(m.memory_id)
        assert got is not None, "Get returned None"
        assert got.memory_id == m.memory_id, "ID mismatch"

        deleted = _resolver.delete_memory(m.memory_id)
        assert deleted is True, "Delete failed"
        _pass("Memory CRUD (create/get/delete)")
    except Exception as e:
        _fail("Memory CRUD", str(e))

    # Test 4: Search
    try:
        _resolver.create_memory(MemoryInput(content="GraphQL is a query language for APIs",
                                            tags=["api", "graphql"]))
        sr = _resolver.search_memories("GraphQL", top_k=5)
        assert len(sr) >= 1, "Expected >= 1 search result"
        assert sr[0].score > 0, f"Expected positive score, got {sr[0].score}"
        _pass("Memory search")
    except Exception as e:
        _fail("Memory search", str(e))

    # Test 5: Agent registration
    try:
        a = _resolver.register_agent(AgentInput(name="TestAgent", role="assistant"))
        assert a.agent_id.startswith("agent_"), f"Bad id: {a.agent_id}"
        assert a.name == "TestAgent"
        agents = _resolver.list_agents()
        assert any(x.agent_id == a.agent_id for x in agents)
        _pass("Agent registration")
    except Exception as e:
        _fail("Agent registration", str(e))

    # Test 6: Vector search
    try:
        vid = _resolver.index_vector("mem_test", "vectorized memory text")
        assert vid.startswith("vec_"), f"Bad vector id: {vid}"
        hits = _resolver.vector_search(VectorSearchInput(query="vectorized", top_k=5))
        assert len(hits) >= 1, "Expected at least 1 hit"
        _pass("Vector index + search")
    except Exception as e:
        _fail("Vector index + search", str(e))

    # Test 7: Diagnostics
    try:
        diag = _resolver.diagnostics()
        assert len(diag) >= 4, f"Expected >= 4 components, got {len(diag)}"
        assert all(d.is_healthy() for d in diag), "Not all components healthy"
        _pass("Diagnostics")
    except Exception as e:
        _fail("Diagnostics", str(e))

    # Test 8: Cross-agent insights
    try:
        insights = _resolver.cross_agent_insights(InsightQuery(min_confidence=0.5))
        assert len(insights) >= 1, "Expected >= 1 insight"
        _pass("Cross-agent insights")
    except Exception as e:
        _fail("Cross-agent insights", str(e))

    results["total"] = results["passed"] + results["failed"]
    return results


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2, ensure_ascii=False))
