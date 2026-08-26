"""MemoryAggregator - retrieval / search mixin (split from aggregator.py).
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


class _SearchMixin:

    @property
    def second_brain_available(self) -> bool:
        """P0-3: Whether SecondBrain bridge is active."""
        return self._sb_engine is not None

    def query(
        self,
        filters: Dict[str, Any],
        limit: int = 50,
        mode: str = "keyword",
        query_text: str = "",
        source: str = "",
        include_archived: bool = False,
    ) -> List[DimensionVector]:
        """Multi-dimension combined retrieval with optional semantic search.

        Args:
            filters: dimension query dict (see DimensionEngine.query)
            limit: max results
            mode: "keyword" / "vector" / "hybrid" (default "keyword" for compat)
            query_text: natural-language query for vector/hybrid modes
            source: 调用来源标识（2026-08-17 P1-2：聚合池利用率归因，
                    记录到 stats.queries_by_source 与 last_query_at）
            include_archived: 是否包含源库已归档（source_status=archived）的记忆。
                默认 False——2026-08-24（R8 P0-1）聚合池检索面与引擎库 active
                口径统一（引擎库只检索 active），修复"归档记忆仍可被 API/MCP
                侧命中"的口径分裂。

        Returns:
            List of matching DimensionVectors
        """
        def _active_only(dvs: List[DimensionVector]) -> List[DimensionVector]:
            if include_archived:
                return dvs
            return [
                dv for dv in dvs
                if dv.source_status not in ("archived", "deleted")
            ]

        with self._lock:
            # ── v7.1.0: Tracing ──
            if self._tracer:
                self._tracer.start_span("query", query_text=query_text, mode=mode)
            self._stats["total_queries"] += 1
            # 2026-08-17（P1-2 最小优化）：查询来源归因 + 最近使用时间戳，
            # 让"聚合池 11k 条但 total_queries 仅 61"的利用率可监控、可归因。
            if source:
                src_stats = self._stats.setdefault("queries_by_source", {})
                src_stats[source] = src_stats.get(source, 0) + 1
            self._stats["last_query_at"] = time.time()

            # ── Keyword results (always computed for hybrid) ──
            kw_results = _active_only(self._engine.query(filters))

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
            vec_dvs = _active_only(vec_dvs)

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
                        ranked_lists.append(_active_only(v47_dvs))
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
                        ranked_lists.append(_active_only(exa_dvs))
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
                        ranked_lists.append(_active_only(graph_dvs))
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
                        and dv.source_status not in ("archived", "deleted")
                    ][:50]
                    if explore_pool:
                        # 用 WanderRetriever 温度采样（relevance 取 importance 近似）
                        # 2026-08-17 修复: DimensionVector 无 importance 字段,
                        # 原 float(dv.importance) 恒抛 AttributeError → 通道静默空转。
                        class _Hit:
                            def __init__(self, dv):
                                self.dv = dv
                                self.relevance = self._imp(dv)
                                self.mode = None
                                self.serendipity_score = 0.0

                            @staticmethod
                            def _imp(dv):
                                try:
                                    return float(dv.importance)
                                except (AttributeError, TypeError):
                                    return 0.5
                        hits = [_Hit(dv) for dv in explore_pool]
                        wandered = self._serendipity.wander(hits)
                        ser_dvs = [h.dv for h in wandered if h.dv.memory_id in self._pool]
                        if ser_dvs:
                            ranked_lists.append(ser_dvs)
                except Exception as exc:
                    logger.debug("Serendipity channel skipped: %s", exc)

            # RRF Fusion across all active channels
            merged = self._rrf_fusion(ranked_lists, top_k=limit)

            # ── 2026-08-17 评分校准层（对标 MEMTIER/AgentPrizm/动态记忆评分）──
            # env 门控（默认 off，不改变既有 96.8% R@5 基线；开启需 A/B 验证）:
            #   TRINITY_CONFIDENCE_SCORER=on  四维置信度校准（来源权威/引用一致/时效/语义）
            #   TRINITY_IMPORTANCE_BOOST=on   importance 动态微调（写时定值 → 查询时加权）
            #   TRINITY_RERANK=on             Cross-Encoder 重排（模型本地缓存，失败静默降级）
            merged = self._apply_scoring_calibration(merged, query_text)

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

                # ── 2026-08-17（RL 闭环自动喂食）: 检索命中即隐式使用 ──
                # top 结果打 IMPLICIT_USE 小奖励（0.05，每记忆每进程一次），
                # 使"检索→使用→反馈→Q 值"闭环无需人工/agent 显式调用。
                try:
                    self.rl_implicit_use([r.memory_id for r in merged], limit=3)
                except Exception as exc:
                    logger.debug("RL implicit feedback skipped: %s", exc)

            logger.debug("query hybrid → %d results (RRF, limiting to %d)", len(merged), limit)
            # ── v7.1.0: Tracing end ──
            if self._tracer:
                self._tracer.end_span("query")
            self._observability.record_memory_op("query")
            return merged[:limit]

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

    def _add_to_topic_index(self, memory_id: str, topics: List[str]) -> None:
        for topic in topics:
            t = topic.lower()
            self._topic_index.setdefault(t, set()).add(memory_id)

    def _add_to_agent_index(self, memory_id: str, agent_name: str) -> None:
        self._agent_index.setdefault(agent_name, set()).add(memory_id)

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
