"""MemoryAggregator - cleanup / maintenance mixin (split from aggregator.py).
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

from ._constants import logger, CLEANUP_INTERVAL_SECONDS, MAX_POOL_SIZE, _HAS_FAISS


class _MaintenanceMixin:

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

    def shutdown(self) -> None:
        """Graceful shutdown: stop cleanup daemon and persist."""
        self._stop_cleanup.set()
        self._cleanup_thread.join(timeout=5)
        self._save()
        logger.info("MemoryAggregator shut down")
