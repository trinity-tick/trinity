"""MemoryAggregator - ingest / merge-on-similarity mixin (split from aggregator.py).
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

from ._constants import logger, SIMILARITY_MERGE_THRESHOLD


class _IngestMixin:

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
