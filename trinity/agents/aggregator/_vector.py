"""MemoryAggregator - embedding / FAISS vector index mixin (split from aggregator.py).
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

from ._constants import logger, _HAS_FAISS

# Optional FAISS import (must be bound in this module's namespace, like the
# pre-split monolith had it at module level)
try:
    import faiss  # noqa: F401
except ImportError:
    faiss = None


class _VectorMixin:

    def _prewarm_ann_index(self) -> None:
        """后台预热 ANN 向量索引（embedding 就绪后 rebuild）。

        2026-08-15 (压测优化)：生产路径索引靠 ingest 增量加，但预热期间
        ingest 跳过索引 → 首次检索可能索引空/冷启动。此线程在 embedding
        ready 后全量 rebuild（池空时短暂重试），让首次检索索引就绪。
        幂等、失败静默。
        """
        try:
            if not self._embedding_ready.wait(timeout=60):
                return
            # 池可能尚未填充：短暂等待后重试（最多 ~10s）
            for _ in range(10):
                if self._pool:
                    break
                time.sleep(1.0)
            if self._pool:
                self._rebuild_index()
                logger.info("ANN index prewarmed (%d vectors)", len(self._index_id_map))
        except Exception:
            pass

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
