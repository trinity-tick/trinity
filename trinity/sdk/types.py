"""
Trinity SDK Data Types (dataclasses).

Provides type-safe wrappers around Trinity API responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Memory:
    """A single memory entry in the Trinity memory pool."""

    id: str
    content: str
    modality: str = "text"
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_uri: Optional[str] = None
    agent_id: str = "default"
    persona_id: str = "default"
    created_at: str = ""
    ttl_seconds: Optional[int] = None
    last_accessed_at: str = ""
    access_count: int = 0
    importance_score: float = 0.0
    content_hash: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Memory:
        return cls(
            id=data.get("memory_id", ""),
            content=data.get("content", ""),
            modality=data.get("modality", "text"),
            metadata=data.get("metadata", {}),
            source_uri=data.get("source_uri"),
            agent_id=data.get("agent_id", "default"),
            persona_id=data.get("persona_id", "default"),
            created_at=data.get("created_at", ""),
            ttl_seconds=data.get("ttl_seconds"),
            last_accessed_at=data.get("last_accessed_at", ""),
            access_count=data.get("access_count", 0),
            importance_score=data.get("importance_score", 0.0),
            content_hash=data.get("content_hash"),
        )


@dataclass
class SearchResult:
    """A ranked search result with multi-stage scoring details."""

    memory: Memory
    score: float = 0.0
    layer_scores: Dict[str, float] = field(default_factory=dict)
    pushed: List[Memory] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SearchResult:
        pushed = [Memory.from_dict(m) for m in data.get("pushed_memories", [])]
        return cls(
            memory=Memory.from_dict(data),
            score=data.get("final_score", data.get("score", 0.0)),
            layer_scores=data.get("layer_scores", {}),
            pushed=pushed,
        )


@dataclass
class Entity:
    """A named entity in the Trinity knowledge graph."""

    id: str
    name: str
    type: str = "concept"
    properties: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Entity:
        return cls(
            id=data.get("entity_id", data.get("id", "")),
            name=data.get("name", ""),
            type=data.get("type", "concept"),
            properties=data.get("properties", {}),
            created_at=data.get("created_at", ""),
        )


@dataclass
class Relation:
    """A relationship between two entities in the Trinity knowledge graph."""

    id: str
    subject_id: str
    predicate: str
    object_id: str
    properties: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Relation:
        return cls(
            id=data.get("relation_id", data.get("id", "")),
            subject_id=data.get("subject_id", ""),
            predicate=data.get("predicate", ""),
            object_id=data.get("object_id", ""),
            properties=data.get("properties", {}),
        )


@dataclass
class Stats:
    """Memory pool statistics."""

    total_memories: int = 0
    expired_count: int = 0
    agent_distribution: Dict[str, int] = field(default_factory=dict)
    avg_access_frequency: float = 0.0
    modality_distribution: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Stats:
        return cls(
            total_memories=data.get("total_memories", 0),
            expired_count=data.get("expired_memories", 0),
            agent_distribution=data.get("agent_distribution", {}),
            avg_access_frequency=data.get("avg_access_count", 0.0),
            modality_distribution=data.get("modalities", {}),
        )


@dataclass
class Health:
    """Trinity server health status."""

    status: str = "unknown"
    version: str = ""
    uptime: str = ""
    memory_count: int = 0
    vector_index_size: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Health:
        return cls(
            status=data.get("status", "unknown"),
            version=data.get("version", ""),
            uptime=data.get("uptime", ""),
            memory_count=data.get("memory_count", 0),
            vector_index_size=data.get("vector_index_size", 0),
        )
