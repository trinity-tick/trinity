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

# EXECUTION 520 (C2p-III): dead numpy import removed

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
from ._base import _AggregatorMixinBase
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:  # 兜底：退回原静默行为
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(site, exc, detail=detail)
        except Exception:
            return None


def _vector_persist_snapshot() -> Dict[str, Any]:
    """§1270：索引持久化计数快照（**惰性导入**包级计数器，避免与包 `__init__` 形成环）。

    取不到时返回 `{}` 而**不是**编一个 0 —— 「取不到 ≠ 是 0」（§13.2）：
    `/metrics` 那边据此**不发**该行，而不是发一条恒 0 的假读数。
    """
    try:
        from . import VECTOR_PERSIST_STATS
        return dict(VECTOR_PERSIST_STATS)
    except Exception:  # noqa: BLE001
        return {}


def _pool_persist_snapshot() -> Dict[str, Any]:
    """2026-10-02（外部复评 §3.3）：**池文件**落盘计数快照。

    与 `_vector_persist_snapshot` 同款纪律：取不到返回 `{}`（`/metrics` 据此不发假 0）。
    为什么需要：池落盘失败此前只有一行 `logger.warning`，而 `_pool_write_guard.py`
    记的同族事故（`Aggregator persist failed: [WinError 5]` → API 停摆）正缺这个读数。
    """
    try:
        from . import POOL_PERSIST_STATS
        return dict(POOL_PERSIST_STATS)
    except Exception:  # noqa: BLE001
        return {}


# ── 2026-09-28：检索通道声明的**唯一诚实来源** ────────────────────────────────
# 本文件有两处回答"哪些检索通道是活的？"（cross_agent_insights 与 statistics），原来都用
# `self._x is not None` —— 即"对象存在"。而 `_init.py:89-99` 是无条件构造这些对象的，
# 所以那五个布尔**恒真**。实测后果：/dashboard 与 /agents/memory/insights 把 6 条通道
# 全报 true，而**同一进程**的 /health 明说 beamlight "has NO search() method at all"、
# retrieval_v47 的 search() 恒 `return []` —— 两个 live 面互相矛盾，而且都是绿的。
#
# 存在 ≠ 有贡献。仓库里本来就有这件事的权威分类：degradation.py 的
# FUSION_CHANNELS_WIRED（每条带调用点、门控条件与实测 breakdown 数字）、
# NON_CAPABILITY_NAMES（每条带理由）、以及判定成员资格的 ROSTER_CRITERION。
# 从它推导 = 一处维护，而不是再手写第三份名单；并且它**带出**旧映射漏掉的两条真通道
# （graph_ppr、serendipity），同时把四条不可能贡献的名字标成 False。
# 取不到分类表时 **fail-closed**：一条通道都不声明（绝不退回恒真）。
def _honest_channel_claims() -> Tuple[Dict[str, bool], Dict[str, str], str]:
    try:
        from trinity.agents.degradation import DegradationManager as _DM
        wired = dict(getattr(_DM, "FUSION_CHANNELS_WIRED", {}) or {})
        noncap = dict(getattr(_DM, "NON_CAPABILITY_NAMES", {}) or {})
        criterion = str(getattr(_DM, "ROSTER_CRITERION", "") or "")
    except Exception as _e:  # noqa: BLE001 — 统计接口不得因分类表移动而挂掉
        logger.warning(
            "_stats: channel classification table unavailable (%s); reporting NO "
            "channel claims rather than the previous tautological `is not None` values", _e)
        return {}, {}, ""
    claims: Dict[str, bool] = {name: True for name in wired}
    for name in noncap:
        if name == "aggregator":
            continue          # 聚合器自身，不属于通道名册
        claims[name] = False
    registry_only = {n: w for n, w in noncap.items() if n != "aggregator"}
    return claims, registry_only, criterion


_CHANNEL_CLAIMS, _CHANNEL_REGISTRY_ONLY, _CHANNEL_CRITERION = _honest_channel_claims()


class _StatsMixin(_AggregatorMixinBase):

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
                    # 2026-09-28: was the literal `True`, contradicted one line above by its own
                    # sibling field -- the live payload returned
                    #   {"episode_count": "N/A", "observer_active": true}
                    # i.e. it advertised an active observer while reporting that the episode
                    # counter does not exist. That is the fake-green shape: the flag recorded
                    # "a class imported and an object constructed", not "the observer observed".
                    _ec = sb_insights.get("episode_count")
                    sb_insights["observer_active"] = isinstance(_ec, int) and _ec > 0
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
            # retrieval_channels：从 degradation.py 的权威分类**推导**（2026-09-28）。
            # 见文件上部 _honest_channel_claims 的说明：这里原来是 1 个字面量 + 5 个
            # `is not None`（对象存在即报 true），与同进程 /health 的结论直接矛盾。
            try:
                insights["retrieval_channels"] = dict(_CHANNEL_CLAIMS)
                insights["retrieval_channels_registry_only"] = dict(_CHANNEL_REGISTRY_ONLY)
                insights["retrieval_channels_criterion"] = _CHANNEL_CRITERION
            except Exception as _e:
                swallow(__name__, _e)

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

        §1032：**锁内只取快照，O(池) 聚合与外部调用全部移到锁外**。
        依据 §1027（py-spy 现场栈）：原实现把三段 O(池) 聚合（外加 _engine.statistics() 与
        _observability.dashboard()）全放在 with self._lock 里，于是 /metrics、检索与 /health
        在这把锁后排成队 ⇒ API 运行约 15 分钟后出现一次「不响应」。
        判据：tests/unit/test_statistics_lockscope.py（正确性用例必须不变；锁范围用例由 xfail 转 pass）。
        """
        # ── 锁内：只做快照（浅拷贝；不做任何聚合/外部调用）──
        with self._lock:
            _pool_vals = list(self._pool.values())
            _pool_keys = set(self._pool.keys())
            _agent_index = {a: list(ids) for a, ids in self._agent_index.items()}
            _graph_lens = [len(adj) for adj in self._relations_graph.values()]
            _topic_index = self._topic_index
            _engine = self._engine
            _base_stats = dict(self._stats)
            # 通道声明从权威分类推导；**锁内不做 import**（表在模块加载时已算好一次）
            _chan = dict(_CHANNEL_CLAIMS)
            _obs = self._observability if hasattr(self, "_observability") else None

        # ── 锁外：聚合与外部调用 ──
        source_dist: Dict[str, int] = {}
        for agent, ids in _agent_index.items():
            valid = sum(1 for mid in ids if mid in _pool_keys)
            source_dist[agent] = valid

        category_dist: Dict[str, int] = Counter(dv.category for dv in _pool_vals)

        topic_dist_raw: Counter = Counter()
        for dv in _pool_vals:
            for topic in dv.topics:
                topic_dist_raw[topic] += 1
        topic_dist = dict(topic_dist_raw.most_common(20))

        total = len(_pool_vals)
        avg_conf = sum(dv.confidence for dv in _pool_vals) / max(total, 1)
        avg_pri = sum(dv.priority for dv in _pool_vals) / max(total, 1)
        graph_edges = sum(_graph_lens)

        return {
            "total_memories": total,
            "total_relations": graph_edges,
            "avg_confidence": round(avg_conf, 3),
            "avg_priority": round(avg_pri, 4),
            "source_distribution": source_dist,
            "category_distribution": dict(category_dist),
            "topic_distribution_top20": topic_dist,
            "distinct_topics": len(_topic_index),
            "engine_stats": _engine.statistics(),
            "retrieval_channels": {
                "keyword": True,
                **_chan,
            },
            "observability": (_obs.dashboard() if _obs is not None else {}),
            # §1270：索引持久化的成色（written / skipped_no_index / skipped_empty_id_map）。
            # 加它的理由：本轮实测「池写了、索引静默没写」⇒ 索引文件丢失后无人知晓，
            # 而代价是每次冷启动一次全量重建（实测 0.15–0.30 s/行）。读数出到 /metrics。
            "vector_persist": _vector_persist_snapshot(),
            # 2026-10-02（外部复评 §3.3）：**池文件**落盘的成色（ok / failed / disabled / last_error）。
            # 加它的理由：池落盘失败此前只有一行 warning，而同族事故（[WinError 5] → API 停摆）
            # 正需要"最后一次落盘成功没有"这个读数。读数出到 /metrics。
            "pool_persist": _pool_persist_snapshot(),
            **_base_stats,
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
