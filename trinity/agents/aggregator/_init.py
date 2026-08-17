"""MemoryAggregator - init/pool construction mixin (split from aggregator.py, 2026-08-17).
Part of the MemoryAggregator package decomposition. Behavior identical to the pre-split single-file implementation.
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

from ._constants import logger, MAX_POOL_SIZE, _SENTINEL
from ._kgraph_adapter import _AggregatorKGraphAdapter


class _InitMixin:

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
        # ── 2026-08-17（RL 闭环喂食源）: 隐式使用反馈去重集合 ──
        # 检索命中即视为"使用"（IMPLICIT_USE, reward 0.05），每记忆每进程
        # 只奖励一次防 Q 值通胀；强信号仍走显式 rl_feedback（TASK_SUCCESS）。
        self._rl_implicit_rewarded: set = set()

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

        # ── 2026-08-15 (压测优化)：ANN 索引预热线程 ──────────────────
        # embedding 就绪后自动 rebuild 向量索引（懒调 _rebuild_index 只被
        # demo/测试触发，生产路径索引可能一直空 → 首次检索冷启动 2.4s 尾巴）。
        # 启动预热让首次检索索引就绪，收敛 p99 尾部延迟。
        threading.Thread(target=self._prewarm_ann_index, daemon=True,
                         name="agg-ann-prewarm").start()

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
