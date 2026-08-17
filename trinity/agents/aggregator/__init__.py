"""Memory Aggregator - shared memory pool with dimension indexing (package decomposition, 2026-08-17).
The former monolith aggregator.py was split into domain mixins (_init/_persist/_ingest/_search/_vector/_rl/_graph/_stats/_maintenance/_similarity/_diagnostics). Public API unchanged: MemoryAggregator, create_aggregator, self_test and _AggregatorKGraphAdapter are re-exported here; module constants are re-exported from ._constants.
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

from ._constants import (SIMILARITY_MERGE_THRESHOLD, MAX_POOL_SIZE, PERSIST_FILENAME, PERSIST_DEBOUNCE_SECONDS, PERSIST_MAX_DIRTY, VECTOR_PERSIST_FILENAME, CLEANUP_INTERVAL_SECONDS, _HAS_FAISS, _SENTINEL, logger)


class _PersistMixin:
    """Persistence / lifecycle mixin.

    Defined in the package namespace (__init__.py) on purpose: _save/_load/_mark_dirty
    read PERSIST_* / _HAS_FAISS as module globals at call time, and external
    code monkey-patches them through trinity.agents.aggregator (e.g.
    benchmark/sync_pool_from_db_v2.py sets agg_mod.PERSIST_MAX_DIRTY = 10**9;
    tests/unit/test_aggregator_index_selfheal.py does setattr(agg_mod,
    '_HAS_FAISS', True)).
    """

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

            # ── P0-2: RL 记忆决策状态持久化（2026-08-17）──────────
            # EpisodicRLScorer 奖励跨重启累积（此前只存内存，进程重启清零）。
            if self._rl_scorer is not None:
                try:
                    self._rl_scorer.save(os.path.join(persist_dir, "rl_state.json"))
                except Exception:
                    pass

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
                    # 2026-08-17（记忆周期优化 P1-3）：VECTOR_PERSIST_FILENAME 曾被
                    # 不同 faiss 可用性的进程写成两种格式——有 faiss 时 faiss.write_index
                    # （原生二进制），无 faiss 时 pickle.dump（magic 0x80 开头）。
                    # 之前按 _HAS_FAISS 固定选一种读法，读到另一种格式即抛
                    # "Index type ... not recognized"→ 删文件 → 每次启动全量重建
                    # （数分钟 GIL 饥饿）。改为读文件头 8 字节探测格式，两种都兼容。
                    with open(vec_path, "rb") as _probe:
                        _magic = _probe.read(8)
                    _is_pickle = len(_magic) > 0 and _magic[0] == 0x80  # pickle protocol magic
                    if _HAS_FAISS and not _is_pickle:
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
                            if _HAS_FAISS:
                                import faiss
                                _faiss_idx = faiss.IndexFlatIP(self._vector_dim)
                                _faiss_idx.add(np.ascontiguousarray(np.array(vectors, dtype=np.float32)))
                                self._faiss_index = _faiss_idx
                            else:
                                self._faiss_index = np.array(vectors, dtype=np.float32)
                except Exception as exc:
                    # 双格式探测后仍失败（文件真正损坏/截断）：删除损坏文件让
                    # _prewarm_ann_index 重建正确格式并 _save 落盘，避免每次启动
                    # "load failed → 全量重建"（数分钟 GIL 饥饿）。
                    logger.warning(
                        "Vector index load failed (%s): %s — removing stale file to rebuild",
                        "faiss" if _HAS_FAISS else "pickle", exc,
                    )
                    try:
                        os.remove(vec_path)
                    except Exception:
                        pass

            # ── P0-2: RL 记忆决策状态恢复（2026-08-17）────────────
            # 与 _save 对称：进程重启后恢复 Q 值/命中统计，避免学完即忘。
            try:
                rl_path = os.path.join(persist_dir, "rl_state.json")
                if os.path.exists(rl_path):
                    from trinity.modules.second_brain.episodic_rl import EpisodicRLScorer
                    self._rl_scorer = EpisodicRLScorer.load(rl_path)
                # 2026-08-17（P2）：无论是否从文件恢复，启动即落盘一次，
                # 确保 rl_state.json 存在（空状态也可追溯），
                # 避免"无 RL 反馈就一直不落盘"。
                if self._rl_scorer is not None:
                    self._rl_scorer.save(rl_path)
            except Exception:
                pass

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


from ._init import _InitMixin
from ._ingest import _IngestMixin
from ._search import _SearchMixin
from ._vector import _VectorMixin
from ._rl import _RLMixin
from ._graph import _GraphMixin
from ._stats import _StatsMixin
from ._maintenance import _MaintenanceMixin
from ._similarity import _SimilarityMixin
from ._diagnostics import _DiagnosticsMixin
from ._kgraph_adapter import _AggregatorKGraphAdapter


class MemoryAggregator(_InitMixin, _PersistMixin, _IngestMixin, _SearchMixin, _VectorMixin, _RLMixin, _GraphMixin, _StatsMixin, _MaintenanceMixin, _SimilarityMixin, _DiagnosticsMixin):
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
    pass


from ._factory import create_aggregator, self_test

__all__ = ["MemoryAggregator", "create_aggregator", "self_test", "_AggregatorKGraphAdapter"]
