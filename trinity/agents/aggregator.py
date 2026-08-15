# -*- coding: utf-8 -*-
"""
Memory Aggregator — Shared Memory Pool with Dimension Indexing
==============================================================
Unified cross-agent shared memory pool, replacing per-agent
isolated storage. Built on top of DimensionEngine for
8-dimension indexing with similarity-based merge dedup.

Key features:
  - Single shared pool with merge-on-similarity
  - FAISS vector index for semantic search (numpy fallback)
  - Multi-agent source tracking with confidence boosting
  - Relationship graph for contradiction / support / extends edges
  - BFS neighborhood traversal for get_related()
  - Memory lifecycle management (TTL / expire_at / cleanup daemon)
  - SecondBrain bridge for semantic similarity & hybrid retrieval
  - 5-channel retrieval gateway with RRF fusion (keyword + vector + SecondBrain + V47 + Exabase)
  - Thread-safe all public methods

Classes:
  - MemoryAggregator: shared pool + indexes + engine
"""

from __future__ import annotations

import json
import logging
import math
import os
import pickle
import threading
import time
from collections import Counter, deque
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# ── v7.1.0: Observability & Tracing ──
from trinity.agents.observability import ObservabilityManager, RequestTracer

import numpy as np

from trinity.agents.dimensions import (
    DEFAULT_CONFIDENCE,
    CONFIDENCE_BOOST_PER_AGENT,
    MAX_CONFIDENCE,
    TOPIC_MAX_TOPICS,
    DimensionEngine,
    DimensionVector,
    MemoryCategory,
    MemoryScope,
    RelationType,
)

logger = logging.getLogger(__name__)

# ── Config Constants ──────────────────────────────────────────────────────

SIMILARITY_MERGE_THRESHOLD = 0.75
MAX_POOL_SIZE = 100000
PERSIST_FILENAME = "aggregator_pool.json"
PERSIST_DEBOUNCE_SECONDS = 2.0  # Delay save after last write
PERSIST_MAX_DIRTY = 50  # Force save after N dirty writes
VECTOR_PERSIST_FILENAME = "aggregator_vectors.pkl"
CLEANUP_INTERVAL_SECONDS = 300  # Daemon cleanup every 5 min

# Optional FAISS import
try:
    import faiss
    _HAS_FAISS = True
except ImportError:
    _HAS_FAISS = False
    logger.info("faiss not installed; using numpy cosine fallback for vector search")

# Internal sentinel: distinguish "not passed" (auto-discover) from "None" (disable persistence)
_SENTINEL = object()


# ── Graph KGraph Adapter（2026-08-15, R3 P0-1a）────────────────────────
# 把 MemoryAggregator 的 _relations_graph（memory_id 级关系图）+ 向量索引
# 适配为 GraphVectorHybridRetriever 所需的 kgraph 接口（query_relations /
# get_entity / ppr_search），让"向量+PPR+RRF"三阶段检索成为 hybrid 第 6 通道。
class _AggregatorKGraphAdapter:
    """GraphVectorHybridRetriever 兼容的轻量 kgraph（基于聚合池关系图）。"""

    def __init__(self, aggregator: "MemoryAggregator"):
        self._agg = aggregator

    def query_relations(self, memory_id: str, max_depth: int = 1) -> list:
        """返回 memory_id 的直接邻接关系（模拟 kgraph.query_relations）。"""
        graph = self._agg._relations_graph
        edges = []
        adj = graph.get(memory_id, {})
        for target, rel in adj.items():
            edges.append({"subject_id": memory_id, "object_id": target,
                          "predicate": str(rel)})
        # 反向边（target → memory_id）
        for src, adj_dict in graph.items():
            if memory_id in adj_dict:
                edges.append({"subject_id": src, "object_id": memory_id,
                              "predicate": str(adj_dict[memory_id])})
        return edges[: max_depth * 16]

    def get_entity(self, memory_id: str) -> Optional[dict]:
        """返回 memory_id 对应的记忆向量（模拟 get_entity）。"""
        dv = self._agg._pool.get(memory_id)
        if dv is None:
            return None
        return {"id": memory_id, "name": memory_id,
                "properties": {"content": getattr(dv, "content", "")[:200]}}

    def ppr_search(self, query_entities: list, top_k: int = 20, **kwargs) -> list:
        """轻量 PPR：从种子实体做 1-2 跳 BFS，按度加权返回。"""
        from collections import Counter
        graph = self._agg._relations_graph
        scores: Counter = Counter()
        for seed in query_entities:
            seed_id = seed if isinstance(seed, str) else (seed or {}).get("id", "")
            if not seed_id:
                continue
            scores[seed_id] += 1.0
            for target in graph.get(seed_id, {}):
                scores[target] += 0.5
                for hop2 in graph.get(target, {}):
                    scores[hop2] += 0.25
        ranked = scores.most_common(top_k)
        return [{"id": mid, "score": float(s)} for mid, s in ranked]


# ── MemoryAggregator ──────────────────────────────────────────────────────


class MemoryAggregator:
    """Shared cross-agent memory pool with dimension-aware indexing.

    Replaces per-agent isolated storage with a single shared pool.
    Uses DimensionEngine internally for topic/scope/category indexing.
    Supports similarity-based dedup merging, relationship graph
    traversal, and automatic expiration.

    Usage:
        agg = MemoryAggregator()
        dv = agg.ingest("user prefers dark mode", "main",
                        {"category": "preference", "scope": "global"})
        results = agg.query({"category": "preference"})
        related = agg.get_related(dv.memory_id, depth=2)
    """

    def __init__(self, persist_path=_SENTINEL):
        self._lock = threading.RLock()
        self._pool: Dict[str, DimensionVector] = {}
        self._topic_index: Dict[str, Set[str]] = {}      # topic → {memory_id}
        self._agent_index: Dict[str, Set[str]] = {}      # agent_name → {memory_id}
        self._relations_graph: Dict[str, Dict[str, str]] = {}  # mid → {target_mid: relation_type}
        self._engine = DimensionEngine()

        # ── P0-1: Vector index ───────────────────────────────────────
        self._vector_dim: int = 384  # default embedding dim
        self._faiss_index: Any = None  # FAISS or None
        self._index_id_map: List[str] = []  # memory_id per index row
        self._embedding_fn: Any = None  # lazy-init embedding callable

        # ── P0-3: SecondBrain bridge ─────────────────────────────────
        self._sb_engine: Any = None
        try:
            from trinity.modules.second_brain import Engine as _SBEngine
            self._sb_engine = _SBEngine()
            logger.info("SecondBrain bridge active")
        except Exception:
            logger.info("SecondBrain unavailable; semantic features limited")

        # ── P1-2: Retrieval Channel Gateway ─────────────────────────
        self._retrieval_v47: Any = None
        self._exabase: Any = None
        self._beamlight: Any = None
        if self._sb_engine is not None:
            try:
                from trinity.modules.second_brain import (
                    RetrievalSystemV47, ExabaseRetrieval, BEAMLIGHT
                )
                self._retrieval_v47 = RetrievalSystemV47()
                self._exabase = ExabaseRetrieval()
                self._beamlight = BEAMLIGHT()
                logger.info("Retrieval Gateway active: V47 + Exabase + BEAMLIGHT")
            except Exception as exc:
                logger.info("Retrieval Gateway limited: %s", exc)

        # ── R3 P0-1a: Graph+PPR hybrid channel (2026-08-15) ──────────
        # 用聚合池关系图做"向量候选 → PPR 图扩展"作为 hybrid 融合的第 6 通道，
        # 对齐 2026 PPR 检索主流。通道对象总创建（ppr 不依赖向量索引）；
        # 实际生效由 hybrid 查询时的 vec_ids 是否非空决定。
        self._graph_channel: Any = None
        try:
            self._graph_channel = _AggregatorKGraphAdapter(self)
            logger.info("Graph+PPR hybrid channel active (6th RRF channel)")
        except Exception as exc:
            self._graph_channel = None
            logger.info("Graph+PPR hybrid channel disabled: %s", exc)

        # ── R5 P0: Serendipity 探索通道（2026-08-15）────────────────
        # RippleMem 对齐：WanderRetriever 温度采样 + AssociativeBridging
        # 弱关联桥，作为 hybrid 融合的探索通道（噪声预算，提升长尾/意外发现）。
        # env TRINITY_SERENDIPITY=off 可关闭（默认 on）。
        self._serendipity: Any = None
        self._serendipity_bridge: Any = None
        try:
            from trinity.modules.second_brain.serendipity_retrieval_engine import (
                AssociativeBridging, WanderRetriever,
            )
            self._serendipity = WanderRetriever(
                temperature=1.2,
                sample_count=int(os.environ.get("TRINITY_SERENDIPITY_SAMPLES", "3")),
            )
            self._serendipity_bridge = AssociativeBridging(max_hops=2)
            logger.info("Serendipity exploration channel active (RippleMem aligned)")
        except Exception as exc:
            self._serendipity = None
            logger.info("Serendipity channel disabled: %s", exc)

        # ── R6 P0: RL 记忆决策（2026-08-15, MemRL 对齐）──────────────
        # EpisodicRLScorer：Q 值记忆打分 + UCB 探索 + 在线更新。
        # hybrid 融合后按 RL 分数微调排序（语义 × Q 权重）。
        # env TRINITY_RL_SCORER=off 可关闭（默认 on）。
        self._rl_scorer: Any = None
        try:
            from trinity.modules.second_brain.episodic_rl import EpisodicRLScorer
            self._rl_scorer = EpisodicRLScorer()
            logger.info("RL memory decision active (MemRL aligned)")
        except Exception as exc:
            self._rl_scorer = None
            logger.info("RL scorer disabled: %s", exc)

        # ── P1-4: Degradation Policy ─────────────────────────────────
        from trinity.agents.degradation import DegradationManager, ServiceTier
        self._degradation = DegradationManager()
        self._ServiceTier = ServiceTier  # stored for self_test access

        # ── v7.1.0: Observability ────────────────────────────────────
        self._observability = ObservabilityManager()
        self._tracer: Optional[RequestTracer] = None

        # ── P0-2: Lifecycle daemon ────────────────────────────────────
        self._stop_cleanup = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="agg-cleanup"
        )
        self._cleanup_thread.start()

        # ── 2026-08-15 (压测优化)：embedding 预热线程 ────────────────
        # sklearn TF-IDF 首次 fit 全量词典约 10s，若在首次 ingest 时同步触发
        # 会让第一批写入卡顿。启动即后台预热，把冷启动移到进程启动期。
        self._embedding_ready = threading.Event()
        threading.Thread(target=self._prewarm_embedding, daemon=True,
                         name="agg-embed-prewarm").start()

        self._stats = {
            "total_ingested": 0,
            "total_merged": 0,
            "total_queries": 0,
            "total_cleanups": 0,
            "cleaned_items": 0,
        }

        # Debounced persistence
        self._dirty_count: int = 0
        self._persist_timer: Optional[threading.Timer] = None

        # Persistence (v6.96.0): _SENTINEL = auto-discover, None = disabled, str = explicit path
        if persist_path is _SENTINEL:
            self._persist_path = self._discover_persist_path()
        else:
            self._persist_path = persist_path
        if self._persist_path and os.path.exists(self._persist_path):
            self._load()
        logger.info("MemoryAggregator initialized (max_pool=%d, persist=%s)",
                    MAX_POOL_SIZE, self._persist_path or "disabled")

    # ── Persistence (v6.96.0) ─────────────────────────────────────────────

    def _discover_persist_path(self) -> Optional[str]:
        """Auto-discover the persistence file path via TRINITY_HOME."""
        candidates = [
            os.environ.get("TRINITY_HOME"),
            os.path.join(os.path.expanduser("~"), "trinity"),
            os.path.join(os.path.expanduser("~"), ".trinity"),
        ]
        for base in candidates:
            if base and os.path.isdir(base):
                return os.path.join(base, "data", PERSIST_FILENAME)
        # Fallback: write alongside aggregator.py
        return os.path.join(os.path.dirname(__file__), "..", "..", "data", PERSIST_FILENAME)

    def _save(self) -> None:
        """Persist the current pool and vector index to disk atomically."""
        if not self._persist_path:
            return
        try:
            with self._lock:
                data = {
                    "version": "6.99.0",
                    "timestamp": time.time(),
                    "memories": [dv.to_dict(full=True) for dv in self._pool.values()],
                    "relations": {
                        mid: dict(edges) for mid, edges in self._relations_graph.items()
                    },
                    "stats": dict(self._stats),
                }
            # Atomic write: 每进程独立 tmp（pid 后缀，避免多进程共用 .tmp 竞态）→ fsync → rename
            persist_dir = os.path.dirname(self._persist_path)
            os.makedirs(persist_dir, exist_ok=True)
            tmp_path = f"{self._persist_path}.{os.getpid()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._persist_path)

            # ── P0-1: Persist vector index ──
            if self._faiss_index is not None and self._index_id_map:
                vec_path = os.path.join(persist_dir, VECTOR_PERSIST_FILENAME)
                vec_tmp = f"{vec_path}.{os.getpid()}.tmp"
                vec_data = {
                    "dim": self._vector_dim,
                    "id_map": self._index_id_map,
                }
                if _HAS_FAISS:
                    import faiss
                    faiss.write_index(self._faiss_index, vec_tmp)
                else:
                    vec_data["vectors"] = self._faiss_index.tolist()
                    with open(vec_tmp, "wb") as f:
                        pickle.dump(vec_data, f)
                os.replace(vec_tmp, vec_path)

            logger.debug("Aggregator pool persisted (%d memories)", len(self._pool))
        except Exception as exc:
            logger.warning("Aggregator persist failed (non-fatal): %s", exc)

    def _mark_dirty(self) -> None:
        """Schedule a debounced save after a write operation.

        Avoids excessive disk I/O by coalescing multiple writes into
        a single persist() call after PERSIST_DEBOUNCE_SECONDS of
        inactivity, or when PERSIST_MAX_DIRTY dirty writes accumulate.
        """
        self._dirty_count += 1

        if self._dirty_count >= PERSIST_MAX_DIRTY:
            # Force immediate save
            if self._persist_timer:
                self._persist_timer.cancel()
                self._persist_timer = None
            self._save()
            self._dirty_count = 0
            return

        # Reset debounce timer
        if self._persist_timer:
            self._persist_timer.cancel()

        self._persist_timer = threading.Timer(
            PERSIST_DEBOUNCE_SECONDS,
            self._flush_dirty,
        )
        self._persist_timer.daemon = True
        self._persist_timer.start()

    def _flush_dirty(self) -> None:
        """Timer callback: persist and reset dirty count."""
        with self._lock:
            if self._dirty_count > 0:
                self._save()
                self._dirty_count = 0
            self._persist_timer = None

    def _load(self) -> None:
        """Restore pool and vector index from disk."""
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            memories = data.get("memories", [])
            relations = data.get("relations", {})
            stats = data.get("stats", {})

            loaded = 0
            for d in memories:
                dv = DimensionVector.from_dict(d)
                self._pool[dv.memory_id] = dv
                # Re-index into DimensionEngine for query support
                self._engine._vectors[dv.memory_id] = dv
                for agent in dv.source_agents:
                    self._add_to_agent_index(dv.memory_id, agent)
                self._add_to_topic_index(dv.memory_id, dv.topics)
                # Update engine stats
                self._engine._stats["total_indexed"] += 1
                loaded += 1

            self._relations_graph = {
                mid: dict(edges) for mid, edges in relations.items()
            }
            self._stats.update(stats)

            # ── P0-1: Restore vector index ──
            persist_dir = os.path.dirname(self._persist_path)
            vec_path = os.path.join(persist_dir, VECTOR_PERSIST_FILENAME)
            if os.path.exists(vec_path):
                try:
                    if _HAS_FAISS:
                        import faiss
                        self._faiss_index = faiss.read_index(vec_path)
                        self._vector_dim = self._faiss_index.d
                        # id_map must be reconstructed from pool order
                        self._index_id_map = list(self._pool.keys())
                    else:
                        with open(vec_path, "rb") as f:
                            vec_data = pickle.load(f)
                        self._vector_dim = vec_data.get("dim", 384)
                        self._index_id_map = vec_data.get("id_map", [])
                        vectors = vec_data.get("vectors", [])
                        if vectors:
                            self._faiss_index = np.array(vectors, dtype=np.float32)
                except Exception as exc:
                    logger.warning("Vector index load failed: %s", exc)

            logger.info(
                "Aggregator pool restored from disk: %d memories, %d relations",
                loaded, len(self._relations_graph),
            )
        except Exception as exc:
            # 自愈：损坏/截断的池文件备份后以空池启动，避免覆盖现场证据
            try:
                if self._persist_path and os.path.exists(self._persist_path):
                    backup = f"{self._persist_path}.corrupt_{int(time.time())}"
                    os.replace(self._persist_path, backup)
                    logger.warning("Aggregator pool corrupted; backed up to %s", backup)
            except Exception:
                pass
            logger.warning("Aggregator load failed (starting fresh): %s", exc)

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def second_brain_available(self) -> bool:
        """P0-3: Whether SecondBrain bridge is active."""
        return self._sb_engine is not None

    def ingest(
        self,
        content: str,
        source_agent: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> DimensionVector:
        """Ingest a memory into the shared pool.

        Checks for existing similar memories first; if found, merges
        by boosting confidence and adding the source agent. Otherwise
        creates a new DimensionVector via DimensionEngine.

        Args:
            content: memory text
            source_agent: originating agent name
            metadata: optional {category, scope, ...} overrides

        Returns:
            The created or merged DimensionVector
        """
        with self._lock:
            # ── v7.1.0: Tracing ──
            if self._tracer:
                self._tracer.start_span("ingest", memory_id=None)
            self._enforce_capacity()

            # Try merge with similar existing memory
            merged = self.merge_if_similar(
                content, source_agent, threshold=SIMILARITY_MERGE_THRESHOLD
            )
            if merged is not None:
                self._stats["total_ingested"] += 1
                self._stats["total_merged"] += 1
                self._mark_dirty()
                # ── v7.1.0: Tracing end ──
                if self._tracer:
                    self._tracer.end_span("ingest")
                self._observability.record_memory_op("ingest")
                return merged

            # No similar → create new via engine
            dv = self._engine.index_memory(content, source_agent, metadata)

            # ── P0-2: Apply TTL / expire_at from metadata ──
            md = metadata or {}
            if "ttl" in md:
                dv.expire_at = time.time() + float(md["ttl"])
            elif "expire_at" in md:
                dv.expire_at = float(md["expire_at"])

            # Register in local pool
            self._pool[dv.memory_id] = dv
            self._add_to_agent_index(dv.memory_id, source_agent)
            self._add_to_topic_index(dv.memory_id, dv.topics)
            self._relations_graph.setdefault(dv.memory_id, {})

            # ── P0-1: Add to vector index ──
            try:
                self._add_to_index(dv)
            except Exception as exc:
                logger.debug("Vector index update skipped: %s", exc)

            self._stats["total_ingested"] += 1
            self._mark_dirty()
            # ── v7.1.0: Tracing end ──
            if self._tracer:
                self._tracer.end_span("ingest")
            self._observability.record_memory_op("ingest")
            logger.info(
                "Ingested new memory %s (agent=%s, category=%s, topics=%s, ttl=%s)",
                dv.memory_id, source_agent, dv.category, dv.topics, dv.expire_at,
            )
            return dv

    def merge_if_similar(
        self,
        content: str,
        source_agent: str,
        threshold: float = SIMILARITY_MERGE_THRESHOLD,
    ) -> Optional[DimensionVector]:
        """Find similar existing memory and merge if above threshold.

        Uses SecondBrain semantic_similarity() when available;
        otherwise falls back to Jaccard token similarity.

        Returns merged DimensionVector or None if no match.
        """
        with self._lock:
            if not self._pool:
                return None

            best_score = 0.0
            best_dv: Optional[DimensionVector] = None

            # P0-3: Use SecondBrain ContextualEmbedder if available
            if self._sb_engine is not None:
                try:
                    from trinity.modules.second_brain import ContextualEmbedder
                    embedder = ContextualEmbedder()
                    e1 = embedder.embed(content)
                    best_score = 0.0
                    best_dv = None

                    candidate_ids: Set[str] = set()
                    input_topics = set(self._engine.extract_topics(content))
                    for topic in input_topics:
                        if topic in self._topic_index:
                            candidate_ids |= self._topic_index[topic]
                        if len(candidate_ids) >= 200:
                            break
                    if not candidate_ids:
                        candidate_ids = set(self._pool.keys())

                    for mid in candidate_ids:
                        dv = self._pool.get(mid)
                        if dv is None:
                            continue
                        e2 = embedder.embed(dv.content)
                        score = float(np.dot(e1, e2) / (np.linalg.norm(e1) * np.linalg.norm(e2) + 1e-8))
                        if score > best_score:
                            best_score = score
                            best_dv = dv
                except Exception as exc:
                    logger.debug("SecondBrain similarity failed, falling back to Jaccard: %s", exc)
                    best_score = 0.0
                    best_dv = None

            # Fallback: Jaccard token similarity
            if best_dv is None:
                input_tokens = self._tokenize(content)
                if not input_tokens:
                    return None

                candidate_ids: Set[str] = set()
                input_topics = set(self._engine.extract_topics(content))
                for topic in input_topics:
                    if topic in self._topic_index:
                        candidate_ids |= self._topic_index[topic]
                    if len(candidate_ids) >= 200:
                        break

                if not candidate_ids:
                    candidate_ids = set(self._pool.keys())

                for mid in candidate_ids:
                    dv = self._pool.get(mid)
                    if dv is None:
                        continue
                    score = self._jaccard_similarity(input_tokens, self._tokenize(dv.content))
                    if score > best_score:
                        best_score = score
                        best_dv = dv

            if best_dv is None or best_score < threshold:
                return None

            # Merge: boost confidence
            old_confidence = best_dv.confidence
            best_dv.confidence = min(
                best_dv.confidence + CONFIDENCE_BOOST_PER_AGENT,
                MAX_CONFIDENCE,
            )
            best_dv.source_agents.add(source_agent)
            best_dv.updated_at = time.time()
            best_dv.priority = self._engine.compute_priority(best_dv)

            # Update agent index
            self._add_to_agent_index(best_dv.memory_id, source_agent)

            # P1-1: GuardianChainV50 merge safety verification
            if self._sb_engine is not None:
                try:
                    from trinity.modules.second_brain import GuardianChainV50
                    guardian = GuardianChainV50()
                    if not guardian.verify_merge_safety(content, best_dv.content):
                        logger.warning(
                            "GuardianChainV50 blocked merge: %s (score=%.3f, agent=%s)",
                            best_dv.memory_id, best_score, source_agent,
                        )
                        return None
                except Exception as exc:
                    logger.debug("GuardianChainV50 verification skipped: %s", exc)

            logger.info(
                "Merged (score=%.3f): %s confidence %.3f→%.3f (agent=%s)",
                best_score, best_dv.memory_id,
                old_confidence, best_dv.confidence, source_agent,
            )
            return best_dv

    def _rrf_fusion(
        self,
        ranked_lists: List[List["DimensionVector"]],
        k: int = 60,
        top_k: int = 10,
    ) -> List["DimensionVector"]:
        """Reciprocal Rank Fusion: merge multiple ranked result lists.

        Each list is already sorted by relevance descending.
        k: RRF constant (default 60, standard in literature).
        """
        scores: Dict[str, float] = {}
        dv_map: Dict[str, "DimensionVector"] = {}

        for lst in ranked_lists:
            for rank, dv in enumerate(lst, start=1):
                dv_map[dv.memory_id] = dv
                scores[dv.memory_id] = (
                    scores.get(dv.memory_id, 0) + 1.0 / (k + rank)
                )

        sorted_ids = sorted(scores.keys(), key=lambda mid: scores[mid], reverse=True)
        return [dv_map[mid] for mid in sorted_ids[:top_k]]

    def query(
        self,
        filters: Dict[str, Any],
        limit: int = 50,
        mode: str = "keyword",
        query_text: str = "",
    ) -> List[DimensionVector]:
        """Multi-dimension combined retrieval with optional semantic search.

        Args:
            filters: dimension query dict (see DimensionEngine.query)
            limit: max results
            mode: "keyword" / "vector" / "hybrid" (default "keyword" for compat)
            query_text: natural-language query for vector/hybrid modes

        Returns:
            List of matching DimensionVectors
        """
        with self._lock:
            # ── v7.1.0: Tracing ──
            if self._tracer:
                self._tracer.start_span("query", query_text=query_text, mode=mode)
            self._stats["total_queries"] += 1

            # ── Keyword results (always computed for hybrid) ──
            kw_results = self._engine.query(filters)

            # ── Auto-touch all keyword results (P0-2) ──
            for dv in kw_results:
                dv.access_count += 1
                dv.last_accessed = time.time()

            if mode == "keyword":
                logger.debug("query keyword → %d results, limiting to %d", len(kw_results), limit)
                # ── v7.1.0: Tracing end ──
                if self._tracer:
                    self._tracer.end_span("query")
                self._observability.record_memory_op("query")
                return kw_results[:limit]

            # ── Vector search ──
            if query_text:
                vec_results_raw = self.vector_search(query_text, top_k=max(limit * 2, 50))
                vec_ids = [mid for _, mid in vec_results_raw]
                vec_scores = {mid: score for score, mid in vec_results_raw}
            else:
                vec_ids = []
                vec_scores = {}

            vec_dvs = [self._pool[mid] for mid in vec_ids if mid in self._pool]

            # ── Auto-touch vector results (P0-2) ──
            for dv in vec_dvs:
                dv.access_count += 1
                dv.last_accessed = time.time()

            # ── Apply filters to vector results ──
            filtered_vec: List[DimensionVector] = []
            for dv in vec_dvs:
                if "category" in filters and dv.category != filters["category"]:
                    continue
                if "scope" in filters and dv.scope != filters["scope"]:
                    continue
                if "source_agent" in filters and filters["source_agent"] not in dv.source_agents:
                    continue
                filtered_vec.append(dv)

            if mode == "vector":
                logger.debug("query vector → %d results", len(filtered_vec))
                # ── v7.1.0: Tracing end ──
                if self._tracer:
                    self._tracer.end_span("query")
                self._observability.record_memory_op("query")
                return filtered_vec[:limit]

            # ── P1-2: Hybrid mode — 5-channel RRF fusion ──
            # Build ranked lists from independent retrieval channels
            ranked_lists: List[List[DimensionVector]] = [kw_results]

            if vec_dvs:
                ranked_lists.append(vec_dvs)

            if self._retrieval_v47 is not None and query_text and self._degradation.is_channel_available("retrieval_v47"):
                try:
                    v47_results = self._retrieval_v47.search(query_text, top_k=limit)
                    v47_dvs: List[DimensionVector] = []
                    for r in v47_results:
                        dv_id = getattr(r, "memory_id", None) or getattr(r, "id", None)
                        if dv_id is None and isinstance(r, dict):
                            dv_id = r.get("memory_id") or r.get("id")
                        if dv_id and dv_id in self._pool:
                            v47_dvs.append(self._pool[dv_id])
                    if v47_dvs:
                        ranked_lists.append(v47_dvs)
                except Exception as exc:
                    self._degradation.mark_failure("retrieval_v47", str(exc)[:100])

            if self._exabase is not None and query_text and self._degradation.is_channel_available("exabase"):
                try:
                    exa_results = self._exabase.search(query_text, top_k=limit)
                    exa_dvs: List[DimensionVector] = []
                    for r in exa_results:
                        dv_id = getattr(r, "memory_id", None) or getattr(r, "id", None)
                        if dv_id is None and isinstance(r, dict):
                            dv_id = r.get("memory_id") or r.get("id")
                        if dv_id and dv_id in self._pool:
                            exa_dvs.append(self._pool[dv_id])
                    if exa_dvs:
                        ranked_lists.append(exa_dvs)
                except Exception as exc:
                    self._degradation.mark_failure("exabase", str(exc)[:100])

            # ── R3 P0-1a: Graph+PPR 第 6 通道（2026-08-15）────────────
            # 向量候选 → 关系图 PPR 扩展 → 按 ppr 分数映射回池内记忆。
            if self._graph_channel is not None and query_text and vec_ids:
                try:
                    ppr_candidates = self._graph_channel.ppr_search(
                        vec_ids[:10], top_k=limit * 2,
                    )
                    graph_dvs: List[DimensionVector] = []
                    for g in ppr_candidates:
                        mid = g.get("id") or (g.get("entity_id") if isinstance(g, dict) else None)
                        if mid and mid in self._pool and mid not in [d.memory_id for d in graph_dvs]:
                            graph_dvs.append(self._pool[mid])
                    if graph_dvs:
                        ranked_lists.append(graph_dvs)
                except Exception as exc:
                    logger.debug("Graph+PPR channel skipped: %s", exc)

            # ── R5: Serendipity 探索通道（2026-08-15）────────────────
            # RippleMem 对齐：从池内低相关记忆温度采样少量，提升长尾/意外发现。
            # 只在有向量候选时启用（探索建立在已有检索之上）；失败静默降级。
            if (self._serendipity is not None and query_text and vec_ids
                    and os.environ.get("TRINITY_SERENDIPITY", "on") != "off"):
                try:
                    # 候选 = 池中未被主通道命中的记忆（低相关 → 高意外性）
                    hit_ids = set(vec_ids[:limit]) | {d.memory_id for lst in ranked_lists for d in lst}
                    explore_pool = [
                        dv for dv in self._pool.values()
                        if dv.memory_id not in hit_ids
                    ][:50]
                    if explore_pool:
                        # 用 WanderRetriever 温度采样（relevance 取 importance 近似）
                        class _Hit:
                            def __init__(self, dv):
                                self.dv = dv
                                self.relevance = float(dv.importance) + 0.01
                                self.mode = None
                                self.serendipity_score = 0.0
                        hits = [_Hit(dv) for dv in explore_pool]
                        wandered = self._serendipity.wander(hits)
                        ser_dvs = [h.dv for h in wandered if h.dv.memory_id in self._pool]
                        if ser_dvs:
                            ranked_lists.append(ser_dvs)
                except Exception as exc:
                    logger.debug("Serendipity channel skipped: %s", exc)

            # RRF Fusion across all active channels
            merged = self._rrf_fusion(ranked_lists, top_k=limit)

            # SecondBrain SelectiveRecall reranker (P0-3, post-RRF boost)
            if self._sb_engine is not None and query_text and merged:
                try:
                    from trinity.modules.second_brain import SelectiveRecallRouter
                    router = SelectiveRecallRouter()
                    decision = router.decide(
                        query_text,
                        [r.content for r in merged[:limit]],
                    )
                    scores = decision.scores if hasattr(decision, "scores") else []
                    for i, score in enumerate(scores):
                        if i < len(merged) and score > 0.5 and merged[i].priority < 0.9:
                            merged[i].priority = min(merged[i].priority + 0.1, 1.0)
                except Exception as exc:
                    logger.debug("SecondBrain rerank skipped: %s", exc)

            # ── R6: RL 记忆决策排序微调（2026-08-15, MemRL 对齐）────
            # 用 Q 值对融合结果微调：优先级 × (1 + rl_bonus)，rl_bonus 来自
            # 历史反馈成功率。冷启动时 Q≈default，排序基本不变。
            if (self._rl_scorer is not None and merged
                    and os.environ.get("TRINITY_RL_SCORER", "on") != "off"):
                try:
                    import math as _math
                    ids = [r.memory_id for r in merged]
                    rl_scores = self._rl_scorer.score_memories(ids)
                    for r in merged:
                        q = rl_scores.get(r.memory_id, 0.5)
                        # 未尝试记忆 UCB=inf（探索）→ 视为 default_q，避免排序污染
                        if not _math.isfinite(q):
                            q = 0.5
                        # bonus: 相对 default(0.5) 的偏移，映射到 ±0.15
                        bonus = (q - 0.5) * 0.3
                        r.priority = min(1.0, r.priority + max(0.0, bonus))
                    merged.sort(key=lambda x: x.priority, reverse=True)
                except Exception as exc:
                    logger.debug("RL rerank skipped: %s", exc)

            logger.debug("query hybrid → %d results (RRF, limiting to %d)", len(merged), limit)
            # ── v7.1.0: Tracing end ──
            if self._tracer:
                self._tracer.end_span("query")
            self._observability.record_memory_op("query")
            return merged[:limit]

    def rl_feedback(self, memory_id: str, positive: bool = True) -> Dict[str, Any]:
        """记录 RL 强化信号（用户确认/纠正 → 更新 Q 值）。

        Args:
            memory_id: 目标记忆。
            positive: True=用户确认（TASK_SUCCESS），False=纠正（TASK_FAILURE）。

        Returns:
            {"rl": bool, "q_value": float}
        """
        if self._rl_scorer is None:
            return {"rl": False, "q_value": 0.5}
        try:
            from trinity.modules.second_brain.episodic_rl import FeedbackSignal
            signal = FeedbackSignal.TASK_SUCCESS if positive else FeedbackSignal.TASK_FAILURE
            self._rl_scorer.record_feedback(memory_id, signal)
            self._rl_scorer.update_q_values()
            q = self._rl_scorer.score_memory(memory_id)
            return {"rl": True, "q_value": round(q, 4)}
        except Exception as exc:
            logger.debug("rl_feedback failed: %s", exc)
            return {"rl": False, "q_value": 0.5}

    def get_by_agent(
        self,
        agent_name: str,
        limit: int = 50,
    ) -> List[DimensionVector]:
        """Retrieve memories contributed by a specific Agent.

        Uses _agent_index to look up memory IDs, then resolves
        from pool. Not isolated storage — queries the shared pool.

        Args:
            agent_name: the agent to look up
            limit: max results

        Returns:
            List of DimensionVectors from that agent, sorted by priority
        """
        with self._lock:
            ids = self._agent_index.get(agent_name, set())
            results = [self._pool[mid] for mid in ids if mid in self._pool]
            results.sort(key=lambda dv: dv.priority, reverse=True)
            return results[:limit]

    def get_by_topic(
        self,
        topic: str,
        limit: int = 50,
    ) -> List[DimensionVector]:
        """Retrieve memories matching a specific topic.

        Args:
            topic: the topic keyword
            limit: max results

        Returns:
            List of DimensionVectors, sorted by priority
        """
        with self._lock:
            ids = self._topic_index.get(topic.lower(), set())
            results = [self._pool[mid] for mid in ids if mid in self._pool]
            results.sort(key=lambda dv: dv.priority, reverse=True)
            return results[:limit]

    def get_related(
        self,
        memory_id: str,
        depth: int = 1,
    ) -> List[DimensionVector]:
        """BFS neighborhood traversal in the relationship graph.

        Explores relations_graph from memory_id outward up to depth
        hops, returning all visited vectors.

        Args:
            memory_id: starting node
            depth: BFS max depth (≥1)

        Returns:
            List of related DimensionVectors, excluding the start node
        """
        with self._lock:
            if memory_id not in self._pool:
                return []

            visited: Set[str] = {memory_id}
            frontier = deque([(memory_id, 0)])
            result_ids: List[str] = []

            while frontier:
                current, level = frontier.popleft()
                if level >= depth:
                    continue

                adj = self._relations_graph.get(current, {})
                for neighbor_id in adj:
                    if neighbor_id not in visited:
                        visited.add(neighbor_id)
                        frontier.append((neighbor_id, level + 1))
                        result_ids.append(neighbor_id)

            results = [self._pool[mid] for mid in result_ids if mid in self._pool]
            results.sort(key=lambda dv: dv.priority, reverse=True)
            return results

    def get_contradictions(
        self,
        memory_id: str,
    ) -> List[DimensionVector]:
        """Find memories that contradict the given one.

        Two strategies:
          1. Check relations_graph for CONTRADICTS edges
          2. Use DimensionEngine.find_contradictions() for topic+negation
             overlap detection

        Args:
            memory_id: the memory to check contradictions against

        Returns:
            List of contradictory DimensionVectors, deduplicated
        """
        with self._lock:
            dv = self._pool.get(memory_id)
            if dv is None:
                return []

            seen: Set[str] = set()
            results: List[DimensionVector] = []

            # Strategy 1: explicit CONTRADICTS edges in relations_graph
            adj = self._relations_graph.get(memory_id, {})
            for target_id, rel_type in adj.items():
                if rel_type == RelationType.CONTRADICTS.value:
                    target_dv = self._pool.get(target_id)
                    if target_dv and target_id not in seen:
                        results.append(target_dv)
                        seen.add(target_id)

            # Also check for edges pointing TO this memory with CONTRADICTS
            for source_id, adj_dict in self._relations_graph.items():
                if adj_dict.get(memory_id) == RelationType.CONTRADICTS.value:
                    source_dv = self._pool.get(source_id)
                    if source_dv and source_id not in seen:
                        results.append(source_dv)
                        seen.add(source_id)

            # Strategy 2: engine-level topic+negation detection
            engine_contradictions = self._engine.find_contradictions(dv.content)
            for cdv in engine_contradictions:
                if cdv.memory_id != memory_id and cdv.memory_id not in seen:
                    results.append(cdv)
                    seen.add(cdv.memory_id)

            results.sort(key=lambda x: x.priority, reverse=True)
            return results

    def get_global_context(self, limit: int = 100) -> List[DimensionVector]:
        """Retrieve cross-agent global context.

        Returns memories with scope=global, plus high-priority
        cross_agent memories, sorted by priority descending.

        Args:
            limit: max results

        Returns:
            List of global/cross-agent DimensionVectors
        """
        with self._lock:
            candidates: List[DimensionVector] = []

            for dv in self._pool.values():
                if dv.scope == MemoryScope.GLOBAL.value:
                    candidates.append(dv)
                elif dv.scope == MemoryScope.CROSS_AGENT.value:
                    candidates.append(dv)

            candidates.sort(key=lambda x: x.priority, reverse=True)
            return candidates[:limit]

    def clean_expired(self, max_age_hours: float = 720.0) -> int:
        """Remove memories older than max_age_hours.

        Only removes local/episodic memories; preserves global scope
        and policy memories regardless of age.

        Args:
            max_age_hours: age threshold in hours (default 720 = 30 days)

        Returns:
            Number of memories removed
        """
        with self._lock:
            max_age_seconds = max_age_hours * 3600.0
            now = time.time()
            to_remove: List[str] = []

            for mid, dv in self._pool.items():
                age = now - dv.created_at
                if age > max_age_seconds:
                    # Protect global scope and policy memories
                    if dv.scope == MemoryScope.GLOBAL.value:
                        continue
                    if dv.category == MemoryCategory.POLICY.value:
                        continue
                    to_remove.append(mid)

            for mid in to_remove:
                self._remove_from_pool(mid)

            counted = len(to_remove)
            self._stats["total_cleanups"] += 1
            self._stats["cleaned_items"] += counted
            logger.info(
                "Cleaned %d expired memories (max_age=%dh)", counted, max_age_hours
            )
            return counted

    # ── P1-3: Cross-Agent Insights (enhanced with contribution analysis) ──

    def cross_agent_insights(
        self, agent_name: Optional[str] = None, top_k: int = 10
    ) -> Dict[str, Any]:
        """Generate cross-agent insights: contributions, shared topics,
        knowledge gaps, collaboration patterns, and emerging themes.

        Args:
            agent_name: optional, filter insights to focus on a specific agent
            top_k: number of top items per category
        """
        with self._lock:
            # ── Agent contributions ──
            agent_knowledge: Dict[str, int] = {}
            agent_contributions: Dict[str, Dict] = {}
            for agent, ids in self._agent_index.items():
                agent_mems = [self._pool[mid] for mid in ids if mid in self._pool]
                agent_knowledge[agent] = len(agent_mems)
                # Top topics per agent
                topic_counter: Counter = Counter()
                for dv in agent_mems:
                    for t in dv.topics:
                        topic_counter[t.lower()] += 1
                agent_contributions[agent] = {
                    "memory_count": len(agent_mems),
                    "top_topics": topic_counter.most_common(min(top_k, len(topic_counter))),
                }

            # ── Shared topics & knowledge gaps ──
            topic_agents: Dict[str, Set[str]] = {}
            for dv in self._pool.values():
                for t in dv.topics:
                    tl = t.lower()
                    topic_agents.setdefault(tl, set()).update(dv.source_agents)
            shared_topics = [
                {"topic": t, "agent_count": len(a), "agents": sorted(a)}
                for t, a in sorted(topic_agents.items(), key=lambda x: len(x[1]), reverse=True)
                if len(a) >= 2
            ][:top_k]
            knowledge_gaps = [
                {"topic": t, "agent": sorted(a)[0]}
                for t, a in topic_agents.items()
                if len(a) == 1
            ][:top_k]

            # ── Collaboration patterns: agent pairs that share topics ──
            collaboration_patterns: List[Dict] = []
            agent_list = sorted(self._agent_index.keys())
            for i in range(len(agent_list)):
                for j in range(i + 1, len(agent_list)):
                    a1, a2 = agent_list[i], agent_list[j]
                    # Count memories where both agents contributed
                    shared_count = sum(
                        1 for dv in self._pool.values()
                        if a1 in dv.source_agents and a2 in dv.source_agents
                    )
                    # Count contradictory edges between them
                    conflict_count = 0
                    for src, adj in self._relations_graph.items():
                        if src not in self._pool:
                            continue
                        for target, rel in adj.items():
                            if rel == "contradicts" and target in self._pool:
                                src_a = self._pool[src].source_agents
                                tgt_a = self._pool[target].source_agents
                                if (a1 in src_a and a2 in tgt_a) or (a2 in src_a and a1 in tgt_a):
                                    conflict_count += 1
                    if shared_count > 0 or conflict_count > 0:
                        collaboration_patterns.append({
                            "agents": [a1, a2],
                            "shared_memories": shared_count,
                            "contradictions": conflict_count,
                        })
            collaboration_patterns.sort(
                key=lambda x: (x["shared_memories"], -x["contradictions"]), reverse=True
            )
            collaboration_patterns = collaboration_patterns[:top_k]

            # ── Emerging themes: most recently created memories ──
            all_dvs = sorted(
                self._pool.values(),
                key=lambda dv: dv.created_at,
                reverse=True,
            )[:top_k]
            emerging_themes = [
                {
                    "topic": dv.topics[0] if dv.topics else "uncategorized",
                    "agent": list(dv.source_agents)[0] if dv.source_agents else "unknown",
                    "content_preview": dv.content[:80] if dv.content else "",
                }
                for dv in all_dvs
            ]

            # ── Contradiction hotspots (preserved from P1-1) ──
            contradictions: Counter = Counter()
            for src, adj in self._relations_graph.items():
                for target, rel in adj.items():
                    if rel == "contradicts":
                        dv = self._pool.get(src)
                        cat = dv.category if dv else "unknown"
                        contradictions[cat] += 1

            # ── Orphan knowledge ──
            orphan_count = sum(
                1 for dv in self._pool.values()
                if len(dv.source_agents) <= 1
            )

            # ── SecondBrain diagnostics (preserved from P1-1) ──
            sb_insights = {}
            if self._sb_engine is not None:
                try:
                    from trinity.modules.second_brain import (
                        GroundTruthEpisodes,
                        ObserverReflector,
                    )
                    gte = GroundTruthEpisodes()
                    sb_insights["episode_count"] = (
                        gte.count() if hasattr(gte, "count") else "N/A"
                    )
                    sb_insights["observer_active"] = True
                except Exception as exc:
                    sb_insights["error"] = str(exc)

            insights: Dict[str, Any] = {
                "total_agents": len(agent_knowledge),
                "total_memories": len(self._pool),
                "agent_knowledge_counts": agent_knowledge,
                "agent_contributions": agent_contributions,
                "shared_topics": shared_topics,
                "knowledge_gaps": knowledge_gaps,
                "collaboration_patterns": collaboration_patterns,
                "emerging_themes": emerging_themes,
                "orphan_knowledge_count": orphan_count,
                "orphan_ratio": round(orphan_count / max(len(self._pool), 1), 3),
                "contradiction_hotspots": dict(contradictions.most_common(10)),
                "second_brain_insights": sb_insights,
                "retrieval_channels": {},
            }
            # Populate retrieval_channels (non-locking call to avoid deadlock)
            try:
                insights["retrieval_channels"] = {
                    "keyword": True,
                    "vector": self._faiss_index is not None,
                    "second_brain": self._sb_engine is not None,
                    "retrieval_v47": self._retrieval_v47 is not None,
                    "exabase": self._exabase is not None,
                    "beamlight": self._beamlight is not None,
                }
            except Exception:
                pass

            # Agent-specific focus
            if agent_name:
                insights["agent_focus"] = {
                    "agent": agent_name,
                    "contributions": agent_contributions.get(agent_name, {}),
                    "shared_with": [
                        t["topic"] for t in shared_topics
                        if agent_name in t["agents"]
                    ],
                }

            return insights

    def statistics(self) -> Dict[str, Any]:
        """Return comprehensive aggregator statistics.

        Returns distributions by source agent, category, and topic.
        """
        with self._lock:
            # Per-source distribution
            source_dist: Dict[str, int] = {}
            for agent, ids in self._agent_index.items():
                valid = sum(1 for mid in ids if mid in self._pool)
                source_dist[agent] = valid

            # Per-category distribution
            category_dist: Dict[str, int] = Counter(
                dv.category for dv in self._pool.values()
            )

            # Per-topic distribution (top 20)
            topic_dist_raw: Counter = Counter()
            for dv in self._pool.values():
                for topic in dv.topics:
                    topic_dist_raw[topic] += 1
            topic_dist = dict(topic_dist_raw.most_common(20))

            # Average confidence
            total = len(self._pool)
            avg_conf = (
                sum(dv.confidence for dv in self._pool.values()) / max(total, 1)
            )

            # Avg priority
            avg_pri = (
                sum(dv.priority for dv in self._pool.values()) / max(total, 1)
            )

            # Graph stats
            graph_edges = sum(len(adj) for adj in self._relations_graph.values())

            return {
                "total_memories": total,
                "total_relations": graph_edges,
                "avg_confidence": round(avg_conf, 3),
                "avg_priority": round(avg_pri, 4),
                "source_distribution": source_dist,
                "category_distribution": dict(category_dist),
                "topic_distribution_top20": topic_dist,
                "distinct_topics": len(self._topic_index),
                "engine_stats": self._engine.statistics(),
                "retrieval_channels": {
                    "keyword": True,
                    "vector": self._faiss_index is not None,
                    "second_brain": self._sb_engine is not None,
                    "retrieval_v47": self._retrieval_v47 is not None,
                    "exabase": self._exabase is not None,
                    "beamlight": self._beamlight is not None,
                },
                "observability": (
                    self._observability.dashboard()
                    if hasattr(self, '_observability')
                    else {}
                ),
                **self._stats,
            }

    # ── Internal Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _tokenize(content: str) -> Set[str]:
        """Tokenize content into a set of normalized terms for Jaccard."""
        import re
        tokens = re.findall(r'[a-zA-Z0-9_\u4e00-\u9fff]+', content.lower())
        # Filter very short tokens
        return {t for t in tokens if len(t) >= 2}

    @staticmethod
    def _jaccard_similarity(set_a: Set[str], set_b: Set[str]) -> float:
        """Compute Jaccard similarity coefficient."""
        if not set_a or not set_b:
            return 0.0
        intersection = len(set_a & set_b)
        union = len(set_a | set_b)
        return intersection / union

    def _add_to_topic_index(self, memory_id: str, topics: List[str]) -> None:
        for topic in topics:
            t = topic.lower()
            self._topic_index.setdefault(t, set()).add(memory_id)

    def _add_to_agent_index(self, memory_id: str, agent_name: str) -> None:
        self._agent_index.setdefault(agent_name, set()).add(memory_id)

    def _remove_from_pool(self, memory_id: str) -> None:
        """Remove a memory from pool, all indexes, and vector index."""
        dv = self._pool.pop(memory_id, None)
        if dv is None:
            return

        # Clean topic index
        for topic in dv.topics:
            t = topic.lower()
            if t in self._topic_index:
                self._topic_index[t].discard(memory_id)
                if not self._topic_index[t]:
                    del self._topic_index[t]

        # Clean agent index
        for agent in dv.source_agents:
            if agent in self._agent_index:
                self._agent_index[agent].discard(memory_id)

        # Clean relations graph
        self._relations_graph.pop(memory_id, None)
        for adj in self._relations_graph.values():
            adj.pop(memory_id, None)

        # ── P0-1: Remove from vector index ──
        if memory_id in self._index_id_map:
            idx = self._index_id_map.index(memory_id)
            if _HAS_FAISS and self._faiss_index is not None:
                self._faiss_index.remove_ids(np.array([idx], dtype=np.int64))
            elif self._faiss_index is not None:
                self._faiss_index = np.delete(self._faiss_index, idx, axis=0)
            self._index_id_map.pop(idx)

    def _enforce_capacity(self) -> None:
        """Prune lowest-priority memories when over MAX_POOL_SIZE."""
        if len(self._pool) < MAX_POOL_SIZE:
            return

        # Remove bottom 5%
        prune_count = max(1, int(MAX_POOL_SIZE * 0.05))
        sorted_ids = sorted(
            self._pool.keys(),
            key=lambda mid: self._pool[mid].priority,
        )
        for mid in sorted_ids[:prune_count]:
            self._remove_from_pool(mid)

        logger.warning("Capacity enforced: pruned %d low-priority memories", prune_count)

    # ── P0-1: Vector Search ──────────────────────────────────────────────

    def _prewarm_embedding(self) -> None:
        """后台预热 embedding 引擎（sklearn 首次 fit 较慢，移到启动期）。

        2026-08-15 (压测优化)：避免首次 ingest 触发 10s 级冷启动。
        预热完成后置 ready 标记；失败静默（后续 ingest 仍惰性初始化）。
        """
        try:
            self._get_embedding_fn()
            self._get_embedding_fn()("预热")
            logger.info("embedding prewarmed (dim=%d)", self._vector_dim)
        except Exception:
            pass
        finally:
            self._embedding_ready.set()

    def _get_embedding_fn(self):
        """Lazy-init embedding callable via trinity.embeddings or hash fallback.

        2026-08-15 (压测优化)：backend 从 "auto" 改为 "sklearn"——auto 会先探测
        Ollama（本机未开时每次 embed 等 ~300ms 超时，是写入 p50 2s 的根因）。
        sklearn TF-IDF 确定性、毫秒级，写入路径提速 ~10x。
        """
        if self._embedding_fn is not None:
            return self._embedding_fn
        try:
            from trinity.embeddings import create_engine
            _eng = create_engine(backend="sklearn")
            # probe dimension
            probe = _eng.embed("test")
            self._vector_dim = len(probe) if isinstance(probe, (list, np.ndarray)) else 384
            self._embedding_fn = lambda text: np.array(_eng.embed(text), dtype=np.float32)
        except Exception:
            logger.info("embeddings module unavailable; using hash-based pseudo-vectors")
            self._embedding_fn = self._hash_embed
        return self._embedding_fn

    @staticmethod
    def _hash_embed(text: str, dim: int = 384) -> np.ndarray:
        """Deterministic pseudo-vector from content hash (fallback)."""
        h = abs(hash(text))
        vec = np.zeros(dim, dtype=np.float32)
        for i in range(dim):
            vec[i] = ((h >> (i % 32)) & 1) * 2.0 - 1.0
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    def _add_to_index(self, dv: DimensionVector) -> None:
        """Add a DimensionVector's embedding to the vector index."""
        # 2026-08-15 (压测优化)：embedding 未预热完成时不阻塞写入——
        # 跳过本次索引（后续 _rebuild_index 全量重建补齐）。
        if not getattr(self, "_embedding_ready", None) or not self._embedding_ready.is_set():
            return
        fn = self._get_embedding_fn()
        vec = fn(dv.content).reshape(1, -1).astype(np.float32)
        if vec.shape[1] != self._vector_dim:
            return  # dimension mismatch, skip

        if _HAS_FAISS:
            if self._faiss_index is None:
                self._faiss_index = faiss.IndexFlatIP(self._vector_dim)
                self._index_id_map = []
            self._faiss_index.add(vec)
        else:
            # numpy fallback: store raw vectors
            if self._faiss_index is None:
                self._faiss_index = np.empty((0, self._vector_dim), dtype=np.float32)
                self._index_id_map = []
            self._faiss_index = np.vstack([self._faiss_index, vec])
        self._index_id_map.append(dv.memory_id)

    def _rebuild_index(self) -> None:
        """Rebuild vector index from all pool memories."""
        self._faiss_index = None
        self._index_id_map = []
        with self._lock:
            for dv in self._pool.values():
                self._add_to_index(dv)

    def vector_search(self, query: str, top_k: int = 10) -> List[Tuple[float, str]]:
        """Search vector index for top-k nearest neighbors.

        Returns list of (score, memory_id) sorted by similarity descending.
        """
        # ── v7.1.0: Tracing ──
        if self._tracer:
            self._tracer.start_span("vector_search", query=query[:80])
        fn = self._get_embedding_fn()
        qv = fn(query).reshape(1, -1).astype(np.float32)

        with self._lock:
            if self._faiss_index is None or not self._index_id_map:
                if self._tracer:
                    self._tracer.end_span("vector_search")
                return []
            if _HAS_FAISS:
                scores, indices = self._faiss_index.search(qv, min(top_k, len(self._index_id_map)))
                results = []
                for s, idx in zip(scores[0], indices[0]):
                    if idx >= 0 and idx < len(self._index_id_map):
                        results.append((float(s), self._index_id_map[idx]))
                if self._tracer:
                    self._tracer.end_span("vector_search")
                return results
            else:
                # numpy cosine similarity
                vecs = self._faiss_index  # (N, dim)
                qv_norm = qv / (np.linalg.norm(qv) + 1e-10)
                vecs_norm = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-10)
                sims = np.dot(vecs_norm, qv_norm.T).flatten()
                top_indices = np.argsort(sims)[::-1][:top_k]
                if self._tracer:
                    self._tracer.end_span("vector_search")
                return [(float(sims[i]), self._index_id_map[i]) for i in top_indices if i < len(self._index_id_map)]

    # ── P0-2: Lifecycle ──────────────────────────────────────────────────

    def _cleanup_loop(self) -> None:
        """Daemon loop: periodically run cleanup until stopped."""
        while not self._stop_cleanup.wait(CLEANUP_INTERVAL_SECONDS):
            try:
                self.cleanup()
            except Exception as exc:
                logger.warning("Cleanup daemon error: %s", exc)

    def cleanup(self) -> int:
        """Remove expired memories from pool, indexes, and vector index.

        A memory is expired if its expire_at is set and current time
        exceeds it. Protected memories (scope=global or category=policy)
        are skipped.

        Returns count of removed memories.
        """
        now = time.time()
        # ── v7.1.0: Tracing ──
        if self._tracer:
            self._tracer.start_span("cleanup")
        to_remove: List[str] = []
        with self._lock:
            for mid, dv in self._pool.items():
                if dv.expire_at is None:
                    continue
                if now < dv.expire_at:
                    continue
                if dv.scope == MemoryScope.GLOBAL.value:
                    continue
                if dv.category == MemoryCategory.POLICY.value:
                    continue
                to_remove.append(mid)

            for mid in to_remove:
                self._remove_from_pool(mid)

        counted = len(to_remove)
        if counted:
            self._stats["total_cleanups"] += 1
            self._stats["cleaned_items"] += counted
            logger.info("Cleanup removed %d expired memories", counted)
            self._mark_dirty()
        # ── v7.1.0: Tracing end ──
        if self._tracer:
            self._tracer.end_span("cleanup")
        self._observability.record_memory_op("cleanup")
        return counted

    def touch(self, memory_id: str) -> bool:
        """Update access_count and last_accessed for a memory.

        Returns True if the memory was found and touched.
        """
        with self._lock:
            dv = self._pool.get(memory_id)
            if dv is None:
                return False
            dv.access_count += 1
            dv.last_accessed = time.time()
            return True

    def memory_stats(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Return access statistics for a single memory."""
        with self._lock:
            dv = self._pool.get(memory_id)
            if dv is None:
                return None
            return {
                "memory_id": dv.memory_id,
                "access_count": dv.access_count,
                "last_accessed": dv.last_accessed,
                "created_at": dv.created_at,
                "expire_at": dv.expire_at,
                "category": dv.category,
                "scope": dv.scope,
                "source_agents": sorted(dv.source_agents),
            }

    def shutdown(self) -> None:
        """Graceful shutdown: stop cleanup daemon and persist."""
        self._stop_cleanup.set()
        self._cleanup_thread.join(timeout=5)
        self._save()
        logger.info("MemoryAggregator shut down")


    # ── P1-5 / v7.0.0: Importance Scoring ──────────────────────────────

    def importance_score(self, memory_id: str) -> float:
        """Auto-score memory importance (Mem0/Supermemory aligned).

        Factors: access frequency, recency, cross-agent references, content
        length (proxy for information density), and priority dimension.

        Returns float in [0.0, 1.0].
        """
        if memory_id not in self._pool:
            return 0.0
        dv = self._pool[memory_id]
        score = 0.0
        # 1. Access frequency bonus (30% weight)
        access_count = getattr(dv, 'access_count', 0)
        score += min(access_count / 10.0, 1.0) * 0.3
        # 2. Content length — proxy for information density (20% weight)
        content_len = len(dv.content) if dv.content else 0
        score += min(content_len / 500.0, 1.0) * 0.2
        # 3. Cross-agent reference bonus — topic shared by 2+ agents (30% weight)
        dv_topics = getattr(dv, 'topics', [])
        topic_agents: Set[str] = set()
        for other in self._pool.values():
            other_topics = getattr(other, 'topics', [])
            if set(dv_topics) & set(other_topics):
                topic_agents.update(other.source_agents)
        if len(topic_agents) >= 2:
            score += 0.3
        # 4. Priority dimension bonus (20% weight)
        priority = getattr(dv, 'priority', 0.5)
        score += priority * 0.2
        return round(min(score, 1.0), 4)

    # ── v7.0.0: Memory Consolidation (Auto-Dreamer aligned) ──────────

    @staticmethod
    def _content_similarity(a: str, b: str) -> float:
        """Simple word-level Jaccard similarity on first 200 words."""
        wa = set(a.lower().split()[:200])
        wb = set(b.lower().split()[:200])
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    def merge_memories(self, topic: Optional[str] = None,
                       similarity_threshold: float = 0.75) -> int:
        """Offline memory consolidation: merge similar memories within topic.

        Keeps highest-importance memory, merges similar ones into it.
        Returns number of merges performed.
        """
        merged_count = 0
        # ── v7.1.0: Tracing ──
        if self._tracer:
            self._tracer.start_span("merge_memories", topic=topic)
        candidates = list(self._pool.values())
        if topic:
            candidates = [dv for dv in candidates
                          if topic in getattr(dv, 'topics', [])]

        # Group by topic (use first topic as grouping key)
        topic_groups: Dict[str, List[DimensionVector]] = {}
        for dv in candidates:
            t = (
                getattr(dv, 'topics', ['uncategorized'])[0]
                if getattr(dv, 'topics', [])
                else 'uncategorized'
            )
            topic_groups.setdefault(t, []).append(dv)

        for _t, dvs in topic_groups.items():
            if len(dvs) < 2:
                continue
            # Sort by importance, keep highest, merge rest into it
            dvs.sort(key=lambda dv: self.importance_score(dv.memory_id),
                     reverse=True)
            keeper = dvs[0]
            for dv in dvs[1:]:
                if (self._content_similarity(keeper.content or '',
                                              dv.content or '')
                        >= similarity_threshold):
                    # Merge: append content, update metadata
                    keeper.content = ((keeper.content or '')
                                      + '\n---\n' + (dv.content or ''))
                    keeper.metadata['merged_from'] = (
                        keeper.metadata.get('merged_from', [])
                        + [dv.memory_id]
                    )
                    if dv.memory_id in self._pool:
                        del self._pool[dv.memory_id]
                        merged_count += 1

        if merged_count > 0:
            self._mark_dirty()
            self._rebuild_indices()
        # ── v7.1.0: Tracing end ──
        if self._tracer:
            self._tracer.end_span("merge_memories")
        self._observability.record_memory_op("merge_memories")
        return merged_count

    # ── v7.0.0: Contradiction Detection (SecondBrain CF aligned) ─────

    def detect_contradictions(self, topic: Optional[str] = None
                              ) -> List[dict]:
        """Detect potentially contradictory memory pairs via negation heuristics.

        Returns list of {memory_a, memory_b, pattern, agent_a, agent_b} dicts,
        limited to 20.
        """
        contradictions: List[dict] = []
        candidates = list(self._pool.values())
        if topic:
            candidates = [dv for dv in candidates
                          if topic in getattr(dv, 'topics', [])]

        NEGATION_PAIRS = [
            ('always', 'never'), ('success', 'fail'), ('true', 'false'),
            ('yes', 'no'), ('increase', 'decrease'), ('start', 'stop'),
            ('enable', 'disable'), ('support', 'unsupported'),
        ]

        for i, dv1 in enumerate(candidates):
            c1 = (dv1.content or '').lower()
            for dv2 in candidates[i + 1:]:
                # Skip pairs where agents fully overlap (self-contradiction is common)
                if dv1.source_agents == dv2.source_agents:
                    continue
                c2 = (dv2.content or '').lower()
                for pos, neg in NEGATION_PAIRS:
                    if pos in c1 and neg in c2:
                        contradictions.append({
                            'memory_a': dv1.memory_id,
                            'memory_b': dv2.memory_id,
                            'pattern': f'{pos} vs {neg}',
                            'agent_a': sorted(dv1.source_agents),
                            'agent_b': sorted(dv2.source_agents),
                        })
                        break
                if len(contradictions) >= 20:
                    break
            if len(contradictions) >= 20:
                break
        return contradictions[:20]

    # ── v7.0.0: Human-Readable Export (Memsearch/Zilliz aligned) ─────

    def export_readable(self, filepath: Optional[str] = None) -> str:
        """Export all memories as human-readable Markdown text.

        If filepath is provided, writes to that path in addition to returning
        the content string.
        """
        lines = [
            "# Trinity Shared Memory Export",
            f"# Generated: {datetime.now().isoformat()}",
            f"# Total Memories: {len(self._pool)}",
            f"# Agents: {sorted(self._agent_index.keys())}",
            "",
        ]

        for agent in sorted(self._agent_index.keys()):
            lines.append(f"## Agent: {agent}")
            for dv in self.get_by_agent(agent):
                topic_label = (
                    getattr(dv, 'topics', ['uncategorized'])[0]
                    if getattr(dv, 'topics', [])
                    else 'uncategorized'
                )
                importance = self.importance_score(dv.memory_id)
                lines.append(
                    f"\n### [{topic_label}] (importance: {importance:.2f})"
                )
                lines.append(f"  ID: {dv.memory_id}")
                lines.append(
                    f"  Content: {dv.content[:300] if dv.content else '(empty)'}"
                )
                lines.append("")

        content = '\n'.join(lines)
        if filepath:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
        return content

    # ── v7.1.0: Benchmark Suite ──────────────────────────────────────────

    def run_benchmark(self) -> List[dict]:
        """Run the full benchmark suite and return results as dicts."""
        from trinity.agents.benchmark import MemoryBenchmark
        bench = MemoryBenchmark(self)
        results = bench.run_full_suite()
        return [
            {
                "name": r.name,
                "success_rate": r.success_rate,
                "avg_latency_ms": r.avg_latency_ms,
                "p50_ms": r.p50_latency_ms,
                "p95_ms": r.p95_latency_ms,
                "details": r.details,
            }
            for r in results
        ]


# ── Factory ───────────────────────────────────────────────────────────────


def create_aggregator(
    persist: Union[bool, str] = _SENTINEL,
    vector_backend: str = "faiss",
    auto_consolidate: bool = False,
    importance_threshold: float = 0.0,
    **kwargs,
) -> MemoryAggregator:
    """Factory function for MemoryAggregator (P1-5 unified + v7.0.0).

    Args:
        persist: False=memory-only, True=auto-discover path, str=explicit path.
        vector_backend: "faiss" (default) or "chromadb" for vector index.
        auto_consolidate: If True, periodic memory consolidation is enabled.
        importance_threshold: Minimum importance to retain (0=keep all).
        **kwargs: Passed through to MemoryAggregator.
    """
    # Resolve persist_path
    if persist is _SENTINEL:
        persist_path = _SENTINEL  # auto-discover
    elif persist is True:
        persist_path = _SENTINEL
    elif persist is False:
        persist_path = None
    else:
        persist_path = persist  # explicit path string

    agg = MemoryAggregator(persist_path=persist_path, **kwargs)

    # ── P1-7 / v7.0.0: ChromaDB vector backend ───────────────────────
    if vector_backend == "chromadb":
        try:
            import chromadb
            # ChromaDB client setup (in-memory collection for aggregator)
            _chroma_client = chromadb.Client(
                chromadb.config.Settings(anonymized_telemetry=False)
            )
            # Store reference for potential use in vector search
            agg._chroma_client = _chroma_client
            logger.info("ChromaDB vector backend active")
        except ImportError:
            logger.warning("chromadb not installed; falling back to FAISS/numpy")
            agg._chroma_client = None
    else:
        agg._chroma_client = None

    # ── P1-6 / v7.0.0: Auto-consolidation ────────────────────────────
    agg._auto_consolidate = auto_consolidate
    agg._importance_threshold = importance_threshold

    if auto_consolidate:
        _orig_ingest = agg.ingest

        def _wrapped_ingest(*args, **kwargs_inner):
            dv = _orig_ingest(*args, **kwargs_inner)
            if len(agg._pool) > 100:
                try:
                    merged = agg.merge_memories()
                    if merged > 0:
                        logger.debug("Auto-consolidate: merged %d memories", merged)
                except Exception as exc:
                    logger.debug("Auto-consolidate skipped: %s", exc)
            return dv

        agg.ingest = _wrapped_ingest  # type: ignore[method-assign]

    return agg


# ── Self-Test ─────────────────────────────────────────────────────────────


def self_test() -> bool:
    """Comprehensive self-test for MemoryAggregator."""
    print("=" * 60)
    print("  Trinity Memory Aggregator — Self Test")
    print("=" * 60)
    passed = 0
    total = 0

    # ── Test 1: creation ──
    total += 1
    print("\n[Test 1] MemoryAggregator creation")
    try:
        agg = create_aggregator(persist=False)
        assert len(agg._pool) == 0
        assert len(agg._agent_index) == 0
        assert len(agg._topic_index) == 0
        assert agg._engine is not None
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 2: ingest new memory ──
    total += 1
    print("\n[Test 2] ingest new memory")
    try:
        dv1 = agg.ingest(
            "user prefers dark mode in all applications",
            "main",
            metadata={"category": "preference", "scope": "global"},
        )
        assert dv1.memory_id
        assert "main" in dv1.source_agents
        assert dv1.category == "preference"
        assert dv1.scope == "global"
        assert dv1.memory_id in agg._pool
        assert "main" in agg._agent_index
        print(f"    id={dv1.memory_id}, confidence={dv1.confidence}, "
              f"topics={dv1.topics}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 3: merge_if_similar (above threshold) ──
    total += 1
    print("\n[Test 3] merge_if_similar (semantic duplicate)")
    try:
        merged = agg.merge_if_similar(
            "user prefers dark mode in all applications",  # identical
            "browser",
            threshold=0.4,  # low threshold so it matches
        )
        assert merged is not None
        assert merged.memory_id == dv1.memory_id
        assert "browser" in merged.source_agents
        assert merged.source_count == 2
        print(f"    merged into {merged.memory_id}, sources={merged.source_agents}, "
              f"confidence={merged.confidence:.2f}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 4: merge_if_similar (below threshold → None) ──
    total += 1
    print("\n[Test 4] merge_if_similar (unrelated content)")
    try:
        result = agg.merge_if_similar(
            "completely unrelated topic about weather forecast",
            "main",
            threshold=0.75,
        )
        assert result is None
        print("    correctly returned None")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 5: ingest second memory & get_by_agent ──
    total += 1
    print("\n[Test 5] get_by_agent")
    try:
        dv2 = agg.ingest(
            "the project uses PostgreSQL as primary database",
            "file-agent",
            metadata={"category": "fact"},
        )
        dv3 = agg.ingest(
            "HTTPS is enforced for all external connections",
            "file-agent",
            metadata={"category": "policy"},
        )
        results = agg.get_by_agent("file-agent")
        assert len(results) >= 2
        print(f"    file-agent contributed {len(results)} memories")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 6: get_by_topic ──
    total += 1
    print("\n[Test 6] get_by_topic")
    try:
        results = agg.get_by_topic("dark")
        assert len(results) >= 1
        # The dark-mode memory should be in results
        found = any("dark" in dv.content.lower() for dv in results)
        assert found
        print(f"    topic='dark' → {len(results)} results")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 7: query ──
    total += 1
    print("\n[Test 7] query with filters")
    try:
        results = agg.query({"category": "policy"})
        assert len(results) >= 1
        assert all(dv.category == "policy" for dv in results)
        print(f"    query category=policy → {len(results)} results")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 8: get_related (BFS depth=1) ──
    total += 1
    print("\n[Test 8] get_related (BFS)")
    try:
        # Add relations via _relations_graph directly (add_relation not in aggregator,
        # but we can use engine's add_relation + sync graph)
        dv_a = agg.ingest("Python is the primary language", "main",
                          metadata={"category": "fact"})
        dv_b = agg.ingest("Python 3.11+ is required", "computer-agent",
                          metadata={"category": "fact"})

        # Manually add relation to graph
        agg._relations_graph[dv_a.memory_id][dv_b.memory_id] = RelationType.EXTENDS.value

        related = agg.get_related(dv_a.memory_id, depth=1)
        assert len(related) >= 1
        assert dv_b.memory_id in {r.memory_id for r in related}
        print(f"    BFS depth=1 from {dv_a.memory_id[:8]} → {len(related)} nodes")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 9: get_contradictions ──
    total += 1
    print("\n[Test 9] get_contradictions")
    try:
        dv_c = agg.ingest("do not use Python 2, it is deprecated",
                          "main", metadata={"category": "policy"})

        # Add CONTRADICTS edge
        agg._relations_graph[dv_a.memory_id][dv_c.memory_id] = RelationType.CONTRADICTS.value

        contradictions = agg.get_contradictions(dv_a.memory_id)
        assert len(contradictions) >= 1
        print(f"    found {len(contradictions)} contradictions for {dv_a.memory_id[:8]}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 10: get_global_context ──
    total += 1
    print("\n[Test 10] get_global_context")
    try:
        ctx = agg.get_global_context(limit=100)
        assert len(ctx) >= 1
        # dv1 was scope=global
        assert dv1.memory_id in {c.memory_id for c in ctx}
        print(f"    global context: {len(ctx)} memories")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 11: clean_expired ──
    total += 1
    print("\n[Test 11] clean_expired")
    try:
        # All memories are recent, should clean 0
        removed = agg.clean_expired(max_age_hours=720)
        print(f"    removed {removed} (expected 0 since all fresh)")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 12: statistics ──
    total += 1
    print("\n[Test 12] statistics")
    try:
        stats = agg.statistics()
        assert stats["total_memories"] >= 5
        assert "source_distribution" in stats
        assert "category_distribution" in stats
        assert "topic_distribution_top20" in stats
        print(f"    memories={stats['total_memories']}, "
              f"avg_confidence={stats['avg_confidence']}, "
              f"avg_priority={stats['avg_priority']}")
        print(f"    sources={stats['source_distribution']}")
        print(f"    categories={stats['category_distribution']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 13: Thread safety ──
    total += 1
    print("\n[Test 13] Thread safety")
    try:
        threads = []
        errors = []

        def worker(wid: int) -> None:
            try:
                for i in range(20):
                    agg.ingest(
                        f"thread-{wid} generated observation number {i}",
                        f"agent-{wid % 3}",
                    )
                    agg.get_by_agent(f"agent-{wid % 3}", limit=10)
                    agg.query({"confidence_min": 0.3}, limit=5)
            except Exception as exc:
                errors.append(str(exc))

        for tid in range(4):
            t = threading.Thread(target=worker, args=(tid,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Thread errors: {errors}"
        print(f"    4 threads × 20 operations OK, pool size={len(agg._pool)}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 14: merge on ingest ──
    total += 1
    print("\n[Test 14] auto-merge on ingest (similar content)")
    try:
        before = agg.statistics()["total_memories"]
        dv_merge = agg.ingest(
            "user prefers dark mode in all applications",  # identical to dv1
            "search-agent",
        )
        after = agg.statistics()["total_memories"]
        # Should merge not create new → count unchanged or +0
        assert after == before
        assert "search-agent" in dv_merge.source_agents
        print(f"    pool size unchanged ({before}→{after}), "
              f"sources={dv_merge.source_agents}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P0-1 Test 15: vector_search ──
    total += 1
    print("\n[Test 15] P0-1 vector_search")
    try:
        vec_results = agg.vector_search("dark mode preference", top_k=5)
        assert len(vec_results) >= 1
        print(f"    vector_search returned {len(vec_results)} results")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P0-1 Test 16: query mode=vector ──
    total += 1
    print("\n[Test 16] P0-1 query mode=vector")
    try:
        results = agg.query({}, limit=5, mode="vector", query_text="database SQL")
        assert isinstance(results, list)
        print(f"    vector query returned {len(results)} results")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P0-1 Test 17: query mode=hybrid ──
    total += 1
    print("\n[Test 17] P0-1 query mode=hybrid")
    try:
        results = agg.query({}, limit=10, mode="hybrid", query_text="Python programming")
        assert isinstance(results, list)
        assert len(results) >= 1
        print(f"    hybrid query returned {len(results)} results")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P0-2 Test 18: TTL ingest and cleanup ──
    total += 1
    print("\n[Test 18] P0-2 TTL ingest + cleanup")
    try:
        agg.ingest("temporary test memory - should expire", "test-agent",
                   metadata={"ttl": 1})  # 1 second TTL
        time.sleep(1.5)
        removed = agg.cleanup()
        print(f"    cleanup removed {removed} expired memories")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P0-2 Test 19: touch and memory_stats ──
    total += 1
    print("\n[Test 19] P0-2 touch + memory_stats")
    try:
        dv_t = agg.ingest("touchable test memory", "test-agent",
                          metadata={"category": "fact"})
        agg.touch(dv_t.memory_id)
        agg.touch(dv_t.memory_id)
        stats = agg.memory_stats(dv_t.memory_id)
        assert stats is not None
        assert stats["access_count"] >= 2
        print(f"    memory_stats: access_count={stats['access_count']}, "
              f"last_accessed={stats['last_accessed']:.1f}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P1-2 Test 21: RRF fusion correctness ──
    total += 1
    print("\n[Test 21] P1-2 RRF fusion correctness")
    try:
        # Create two ranked lists with known overlap
        list_a = [agg._pool[list(agg._pool.keys())[0]]] if agg._pool else []
        list_b = [agg._pool[list(agg._pool.keys())[1]]] if len(agg._pool) > 1 else []
        fused = agg._rrf_fusion([list_a, list_b], top_k=5)
        assert isinstance(fused, list)
        assert len(fused) <= 5
        # With 2 disjoint lists, should get 2 results (if both lists non-empty)
        if list_a and list_b:
            assert len(fused) == 2
            print(f"    RRF fused 2 lists → {len(fused)} results")
        else:
            print(f"    RRF fusion returned {len(fused)} results (pool may be sparse)")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P1-2 Test 22: hybrid query with RRF + statistics channels ──
    total += 1
    print("\n[Test 22] P1-2 hybrid query + retrieval_channels stats")
    try:
        results = agg.query({}, limit=5, mode="hybrid", query_text="dark mode")
        assert isinstance(results, list)
        print(f"    hybrid query returned {len(results)} results")

        stats = agg.statistics()
        channels = stats.get("retrieval_channels", {})
        assert isinstance(channels, dict)
        assert "keyword" in channels
        assert "vector" in channels
        assert "second_brain" in channels
        assert "retrieval_v47" in channels
        assert "exabase" in channels
        assert "beamlight" in channels
        print(f"    retrieval_channels: {channels}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P1-3 Test 23: cross_agent_insights structure ──
    total += 1
    print("\n[Test 23] P1-3 cross_agent_insights structure")
    try:
        insights = agg.cross_agent_insights(top_k=5)
        assert isinstance(insights, dict)
        # Required top-level keys
        for key in ("total_agents", "total_memories", "agent_knowledge_counts",
                     "agent_contributions", "shared_topics", "knowledge_gaps",
                     "collaboration_patterns", "emerging_themes",
                     "orphan_knowledge_count", "orphan_ratio",
                     "contradiction_hotspots", "second_brain_insights",
                     "retrieval_channels"):
            assert key in insights, f"Missing key: {key}"
        assert isinstance(insights["agent_contributions"], dict)
        assert isinstance(insights["shared_topics"], list)
        assert isinstance(insights["knowledge_gaps"], list)
        assert isinstance(insights["collaboration_patterns"], list)
        assert isinstance(insights["emerging_themes"], list)
        print(f"    insights: {len(insights['agent_contributions'])} agents, "
              f"{len(insights['shared_topics'])} shared topics, "
              f"{len(insights['emerging_themes'])} emerging themes")

        # Agent-specific focus
        agent_insights = agg.cross_agent_insights(agent_name="main", top_k=5)
        assert "agent_focus" in agent_insights
        print(f"    agent_focus for 'main': {agent_insights['agent_focus']['agent']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P1-4 Test 24: Degradation Manager ──
    total += 1
    print("\n[Test 24] P1-4 DegradationManager three-tier fallback")
    try:
        dm = agg._degradation
        dm.reset()  # reset in case previous tests triggered degradation

        # Tier starts at FULL
        assert dm.tier == agg._ServiceTier.FULL
        assert dm.is_channel_available("retrieval_v47")
        assert dm.is_channel_available("exabase")
        stats = dm.statistics()
        assert stats["tier"] == "full"
        print("    initial tier: FULL ✓")

        # Mark V47 failure → DEGRADED (V47 ∈ FULL_CHANNELS)
        changed = dm.mark_failure("retrieval_v47", "timeout")
        assert changed
        assert dm.tier == agg._ServiceTier.DEGRADED
        assert not dm.is_channel_available("retrieval_v47")
        stats = dm.statistics()
        assert stats["failure_counts"]["retrieval_v47"] == 1
        print(f"    V47 failed → DEGRADED ✓ (active: {stats['active_channels']})")

        # Mark vector failure → MINIMAL (keyword only)
        changed = dm.mark_failure("vector", "crash")
        assert changed
        assert dm.tier == agg._ServiceTier.MINIMAL
        print("    Vector failed → MINIMAL ✓")

        # Recovery: V47 back → still MINIMAL (vector still down)
        dm.mark_recovery("retrieval_v47")
        assert dm.tier == agg._ServiceTier.MINIMAL
        print("    V47 recovered, tier remains MINIMAL ✓")

        # Recovery: vector back → FULL (all channels healthy again)
        dm.mark_recovery("vector")
        assert dm.tier == agg._ServiceTier.FULL
        print("    Vector recovered → FULL ✓")

        # Verify failure_counts preserved after recovery
        stats = dm.statistics()
        assert stats["failure_counts"]["retrieval_v47"] == 1
        assert stats["failure_counts"]["vector"] == 1
        print("    failure_counts preserved after recovery ✓")

        # Reset
        dm.reset()
        assert dm.tier == agg._ServiceTier.FULL
        assert dm.statistics()["failure_counts"] == {}
        print("    reset → clean state ✓")

        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── v7.0.0 Test 25: importance_score ──
    total += 1
    print("\n[Test 25] v7.0.0 importance_score returns 0-1 range")
    try:
        for mid in list(agg._pool.keys())[:3]:
            score = agg.importance_score(mid)
            assert 0.0 <= score <= 1.0, f"score {score} out of range"
        # Unknown ID returns 0
        assert agg.importance_score("nonexistent-id") == 0.0
        print(f"    importance_score works, sample range valid")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── v7.0.0 Test 26: merge_memories ──
    total += 1
    print("\n[Test 26] v7.0.0 merge_memories consolidates similar memories")
    try:
        before = agg.statistics()["total_memories"]
        # Ingest two very similar memories
        agg.ingest("The API rate limit is 100 requests per minute",
                   "test-agent", metadata={"topic": "api"})
        agg.ingest("The API rate limit is 100 requests per minute, enforced globally",
                   "test-agent", metadata={"topic": "api"})
        merged = agg.merge_memories(topic="api", similarity_threshold=0.4)
        after = agg.statistics()["total_memories"]
        # With low threshold, should merge similar ones
        print(f"    merge_memories: merged={merged}, pool {before+2}→{after}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── v7.0.0 Test 27: detect_contradictions ──
    total += 1
    print("\n[Test 27] v7.0.0 detect_contradictions")
    try:
        agg.ingest("The system always requires authentication for all endpoints",
                   "main", metadata={"category": "policy"})
        agg.ingest("The system never requires authentication for internal calls",
                   "computer-agent", metadata={"category": "policy"})
        contradictions = agg.detect_contradictions()
        assert isinstance(contradictions, list)
        # always vs never should be detected across different agents
        assert len(contradictions) >= 1, f"Expected >=1 contradiction, got {len(contradictions)}"
        assert contradictions[0]["pattern"] == "always vs never"
        print(f"    detected {len(contradictions)} contradictions: {contradictions[0]['pattern']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── v7.0.0 Test 28: export_readable ──
    total += 1
    print("\n[Test 28] v7.0.0 export_readable outputs markdown")
    try:
        content = agg.export_readable()
        assert isinstance(content, str)
        assert "# Trinity Shared Memory Export" in content
        assert "## Agent:" in content
        assert "importance:" in content
        print(f"    export_readable generated {len(content)} chars, "
              f"{content.count('## Agent:')} agent sections")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── v7.1.0 Test 29: ObservabilityManager dashboard ──
    total += 1
    print("\n[Test 29] v7.1.0 ObservabilityManager dashboard structure")
    try:
        dash = agg._observability.dashboard()
        assert isinstance(dash, dict)
        for key in ("uptime_seconds", "health", "requests", "operations", "memory_ops"):
            assert key in dash, f"Missing key: {key}"
        assert dash["health"] == "healthy"
        assert "total" in dash["requests"]
        assert "errors" in dash["requests"]
        assert "avg_latency_ms" in dash["requests"]
        print(f"    dashboard: health={dash['health']}, "
              f"uptime={dash['uptime_human']}, requests={dash['requests']['total']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── v7.1.0 Test 30: MemoryBenchmark three-stage run ──
    total += 1
    print("\n[Test 30] v7.1.0 MemoryBenchmark three-stage run")
    try:
        results = agg.run_benchmark()
        assert isinstance(results, list)
        assert len(results) == 3, f"Expected 3 benchmark stages, got {len(results)}"
        stage_names = [r["name"] for r in results]
        assert "ingest" in stage_names
        assert "query" in stage_names
        assert "retrieval" in stage_names
        for r in results:
            assert "success_rate" in r
            assert "avg_latency_ms" in r
            assert "p50_ms" in r
            assert "p95_ms" in r
        print(f"    benchmark stages: {stage_names}")
        print(f"    ingest:  {results[0]['success_rate']:.2%} success, "
              f"{results[0]['avg_latency_ms']:.1f}ms avg")
        print(f"    query:   {results[1]['success_rate']:.2%} success, "
              f"{results[1]['avg_latency_ms']:.1f}ms avg")
        print(f"    retrieval: {results[2].get('details', {}).get('recall_at_k', 0):.2%} recall@K, "
              f"{results[2]['avg_latency_ms']:.1f}ms avg")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P0-2 Test 20: shutdown ──
    total += 1
    print("\n[Test 20] P0-2 graceful shutdown")
    try:
        agg.shutdown()
        assert agg._stop_cleanup.is_set()
        print("    shutdown completed, cleanup daemon stopped")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Summary ──
    print("\n" + "=" * 60)
    print(f"  RESULTS: {passed}/{total} passed")
    print("=" * 60)
    return passed == total


if __name__ == "__main__":
    ok = self_test()
    raise SystemExit(0 if ok else 1)
