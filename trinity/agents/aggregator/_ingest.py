"""MemoryAggregator - ingest / merge-on-similarity mixin (split from aggregator.py).
"""

from __future__ import annotations

import json
import logging
import math
import os
import pickle
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
from ._base import _AggregatorMixinBase
# 2026-10-06（复评 G1）：**真正的**合并安全判据。此前这里曾调用一个全仓从未存在过的
# `GuardianChainV50.verify_merge_safety`（每次抛 AttributeError 被 debug 级吞掉，属假绿）；
# 第一轮修复删掉了那个调用并显式宣告"未实现"，本轮把判据补上。
from ._merge_safety import verify_merge_safety, verify_merge_postcondition

#: 后置不变量的 WARNING **一次性**哨兵（t13）：违反**每次都计数**，日志只印第一次。
#: 名字刻意与已废弃的 `_merge_safety_warning_emitted` 区分（后者不得回归，见
#: `tests/unit/test_merge_safety_absence_20261006.py::test_已废弃的宣告函数不得留作死代码`）。
_MERGE_POSTCOND_WARNED = False

# ── 历史沿革（保留以备追溯，勿再据此判断现状）────────────────────────────────
# 本文件原在 merge_if_similar() 里调用 `GuardianChainV50.verify_merge_safety`，而该方法
# **全仓从未实现**（`git log --all -S "def verify_merge_safety"` 无任何命中；活动类的公开
# 方法只有 __init__ / validate / enforcing_count / get_new_shields）。调用自 916be25
# （2026-08-14 初始导入）起就是坏的，b0f9f3a（2026-08-17 拆分）原样搬进本文件 ⇒ 每次合并
# 抛 AttributeError，被 `except Exception` 在 **logger.debug** 级别吞掉 ⇒ 校验从未生效且静默。
#
# 处置分两步（2026-10-06）：
#   ① 第一轮（F5）：删除那个不可能的调用，改为一次性 WARNING 的显式 no-op 宣告 —— 即
#      "不再假装有校验"。这一步消除了**假绿**。
#   ② 本轮（G1/G2）：把**真实判据**补上（`_merge_safety.py`），并把校验**前置**到任何
#      变更之前；不安全时**零变更** + 返回既有条目（返回 None 会被调用方读成"没有相似项"
#      而**新建重复记忆** —— 那正是旧的顺序缺陷"假绿退化成静默重复"的机制）。
# 原委见 trinity/audit_report_v6.96.0.md「Aggregator 桥接增强」一节。


class _IngestMixin(_AggregatorMixinBase):

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
        # 2026-09-15（R41-P23）：首次真正使用聚合器 → 启动预热（幂等、不阻塞）。
        # 聚合器构造期不再无条件预热，见 _init.py::_start_warmup 的说明。
        self._start_warmup()
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

            # ── 2026-10-06（复评 G1/G2）：合并安全校验 —— **必须在任何变更之前** ──
            # 旧实现的顺序缺陷（已实测确认）：校验发生在 confidence / source_agents /
            # updated_at / priority 与 agent 索引**全部写完之后**，且失败时 `return None`
            # **不回滚**；而调用方（ingest，见本文件 :113）把 None 读成"没有相似项"
            # ⇒ **新建一条重复记忆**。即「假绿会退化成静默重复」。
            # 现口径：校验**前置**；不安全 ⇒ **零变更** + 返回**既有条目**（不是 None），
            # 既不改写既有状态、也不制造重复。
            _verdict = verify_merge_safety(
                content,
                best_dv.content,
                existing_sources=set(best_dv.source_agents),
                new_source=source_agent,
            )
            if not _verdict.safe:
                self._stats["total_merge_refused"] = (
                    self._stats.get("total_merge_refused", 0) + 1
                )
                logger.warning(
                    "MERGE-SAFETY-REFUSED[%s]: 拒绝把来料并入 %s（agent=%s）；"
                    "**零变更**并返回既有条目以避免产生重复记忆。原因：%s",
                    _verdict.code, best_dv.memory_id, source_agent, _verdict.detail,
                )
                return best_dv

            # ── 2026-10-06（t13）：后置不变量的**变更前快照** ──
            # 前置判据（上面的 verify_merge_safety）回答"能不能开始改"；本段与下面的后置核对
            # 回答"改完之后有没有改坏"。此行只是**新增一条快照**，不移动任何既有语句。
            _before_sources = set(best_dv.source_agents)

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

            # ── 2026-10-06（t13）：R2 的**可失败后置不变量**（前置形态是恒真式，已退役）──
            # 核对"新来源已入集 **且** 原有来源一个不少"。它放在**全部变更之后** ⇒
            # 与 G2 契约（前置校验必须早于任何变更）无关，也不改变任何既有语句的顺序。
            # 纪律：**计数每次做、WARNING 至多一次**（后置不变量一旦被违反，每次合并都会违反，
            # 不做 one-shot 会把日志刷爆 —— 那会让真信号被淹没）。
            #   计数键：`self._stats["total_merge_postcondition_violations"]`
            _post = verify_merge_postcondition(
                _before_sources, best_dv.source_agents, new_source=source_agent,
            )
            if not _post.safe:
                self._stats["total_merge_postcondition_violations"] = (
                    self._stats.get("total_merge_postcondition_violations", 0) + 1
                )
                global _MERGE_POSTCOND_WARNED
                if not _MERGE_POSTCOND_WARNED:
                    _MERGE_POSTCOND_WARNED = True
                    logger.warning(
                        "MERGE-SAFETY-POSTCOND[%s]: %s（本条只告警一次；"
                        "累计次数见计数键 total_merge_postcondition_violations）",
                        _post.code, _post.detail,
                    )

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

        touched_keepers: List[str] = []
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
                    if keeper.memory_id not in touched_keepers:
                        touched_keepers.append(keeper.memory_id)
                    if dv.memory_id in self._pool:
                        # §1258：**不要**裸 `del self._pool[...]` —— 那只摘池、不摘索引，
                        # 会在向量索引里留下「池里已没有的 id」。改走本类的既有 API
                        # `_remove_from_pool`（池 + topic/agent 索引 + 关系图 + 向量索引一起摘）。
                        self._remove_from_pool(dv.memory_id)
                        merged_count += 1

        if merged_count > 0:
            # §1258：这里原本还有一行 `self._rebuild_indices()` —— **本仓从无此方法**
            # （全仓 grep 只有 memory_layers 的 `_rebuild_indices_for_entry`），所以只要真的
            # 合并成功就必抛 AttributeError：REST 的 merge 面直接 500，而池已经被改过 ⇒
            # 现场停在「池已删、索引未改」。删除点改走 `_remove_from_pool`（它自己就把各索引
            # 一起摘了）之后，此处不需要额外重建，故删掉那一行。
            #
            # §1267：keeper 的向量**过期**（正文被追加过）—— 上一轮把它登记成「已知未修」，本轮修掉：
            # 把 keeper 那一行从向量索引里**摘掉**（不动池），下一次重建的**增量**路径就会把它
            # 当新条目重新 embed 补回（成本与新增量成正比）。**不做全量重建**：19k 池上的全量
            # 正是 §1019/§1024 那条「重建期间健康探测超时被判死」的路径。
            self._mark_dirty()
            for _mid in touched_keepers:
                self._drop_vector_row(_mid)
        # ── v7.1.0: Tracing end ──
        if self._tracer:
            self._tracer.end_span("merge_memories")
        self._observability.record_memory_op("merge_memories")
        return merged_count
