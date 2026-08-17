"""MemoryAggregator - insights / statistics mixin (split from aggregator.py).
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


class _StatsMixin:

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
