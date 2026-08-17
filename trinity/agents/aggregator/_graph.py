"""MemoryAggregator - relationship graph mixin (split from aggregator.py).
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

from ._constants import logger


class _GraphMixin:

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
