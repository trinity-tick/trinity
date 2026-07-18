"""
Second Brain Retrieval Pipeline
================================
Multi-stage retrieval: BM25 sparse → FAISS dense → Cross-Encoder reranking.

This pipeline replaces the original hash-based pseudo-semantic retrieval
with true multi-stage hybrid search.

Pipeline:
  Stage 1: Query Understanding (expansion, intent)
  Stage 2: BM25 Sparse (keyword matching)
  Stage 3: FAISS HNSW Dense (semantic similarity)
  Stage 4: RRF Fusion (sparse + dense combined)
  Stage 5: Cross-Encoder Reranking (precision refinement)
  Stage 6: Temporal + Importance Boost

Reference:
  - Weaviate hybrid_search
  - Pinecone sparse-dense
  - Cohere Rerank
  - NDR (Neural Document Retrieval)
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from trinity.vector_index.sparse import BM25SparseRetriever, fuse_scores_sparse_dense
from trinity.vector_index.reranker import CrossEncoderReranker
from trinity.vector_index.index import VectorIndex, create_index

logger = logging.getLogger(__name__)


class TrinityRetrievalPipeline:
    """Complete multi-stage retrieval pipeline for Second Brain.

    Usage:
        pipeline = TrinityRetrievalPipeline()
        pipeline.index_corpus(docs, ids, vectors)
        results = pipeline.search(query, query_vec, top_k=10)
    """

    def __init__(
        self,
        dim: int = 1024,
        enable_sparse: bool = True,
        enable_reranker: bool = True,
        enable_query_expansion: bool = True,
        approx_top_k: int = 100,
        final_top_k: int = 10,
        reranker_model: str = "fast",
        fusion_alpha: float = 0.3,
    ):
        self._dim = dim
        self._approx_top_k = approx_top_k
        self._final_top_k = final_top_k
        self._fusion_alpha = fusion_alpha
        self._enable_query_expansion = enable_query_expansion

        # Dense vector index (FAISS HNSW by default)
        self._dense = create_index(
            backend="auto",
            dim=dim,
            metric="cosine",
            index_type="hnsw",
        )

        # Sparse BM25 retriever
        self._sparse: Optional[BM25SparseRetriever] = None
        if enable_sparse:
            self._sparse = BM25SparseRetriever()

        # Cross-Encoder reranker
        self._reranker: Optional[CrossEncoderReranker] = None
        if enable_reranker:
            self._reranker = CrossEncoderReranker(model_name=reranker_model)

        # Semantic cache (Stage 0)
        from trinity.core.cache import SemanticCache
        self._cache = SemanticCache(max_size=5000, default_ttl=300)

        # Stats
        self._stats = {
            "total_searches": 0,
            "avg_results": 0,
            "stage_sparse": 0,
            "stage_dense": 0,
            "stage_fusion": 0,
            "stage_reranker": 0,
            "stage_cache_hit": 0,
            "stage_cache_miss": 0,
        }

        self._metadata_store: Dict[str, Dict[str, Any]] = {}
        self._initialized = False

    # ── Index Management ───────────────────────────────────────────

    def index_corpus(
        self,
        texts: List[str],
        ids: List[str],
        vectors: Optional[np.ndarray] = None,
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> "TrinityRetrievalPipeline":
        """Index a corpus of documents.

        Args:
            texts: Document text strings.
            ids: Document IDs.
            vectors: Optional pre-computed vectors (if None, must be computed later).
            metadatas: Optional per-document metadata.

        Returns:
            Self for chaining.
        """
        start = time.perf_counter()

        # Add to dense index
        if vectors is not None:
            for i, (doc_id, vec) in enumerate(zip(ids, vectors)):
                meta = metadatas[i] if metadatas else {}
                self._dense.add(doc_id, vec, meta)

        # Add to sparse index
        if self._sparse:
            self._sparse.index_corpus(texts, ids)

        # Store metadata
        for i, doc_id in enumerate(ids):
            meta = metadatas[i] if metadatas else {}
            meta["text"] = texts[i]
            self._metadata_store[doc_id] = meta

        elapsed = time.perf_counter() - start
        logger.info(
            "Indexed %d documents (dense=%s, sparse=%s) in %.3fs",
            len(ids),
            vectors is not None,
            self._sparse is not None,
            elapsed,
        )

        self._initialized = True
        return self

    def add(
        self,
        doc_id: str,
        text: str,
        vector: np.ndarray,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Add a single document to the index."""
        if self._dense:
            self._dense.add(doc_id, vector, metadata or {})
        if self._sparse:
            self._sparse.add_documents([text], [doc_id])
        meta = dict(metadata or {})
        meta["text"] = text
        self._metadata_store[doc_id] = meta

    # ── Query Expansion ───────────────────────────────────────────

    def _expand_query(self, query: str) -> List[str]:
        """Generate query variants for better recall.

        Uses simple heuristics instead of LLM for speed:
        - Original query
        - Query without stopwords
        - Query with key terms duplicated (emphasized)
        """
        queries = [query]

        # Remove stopwords variant
        stopwords = {
            "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都",
            "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你",
            "会", "着", "没有", "看", "好", "自己", "the", "a", "an", "is",
            "are", "was", "were", "in", "on", "at", "of", "for", "to", "and",
        }
        tokens = re.findall(r'\w+', query.lower())
        filtered = [t for t in tokens if t not in stopwords]
        if len(filtered) >= 2 and len(filtered) != len(tokens):
            queries.append(" ".join(filtered))

        # Emphasize key terms (repeat them)
        if len(filtered) >= 2:
            key_terms = [t for t in filtered if len(t) > 2]
            if key_terms:
                emphasized = " ".join(key_terms) + " " + " ".join(key_terms)
                queries.append(emphasized)

        return list(set(queries))

    # ── Search ───────────────────────────────────────────────��──────

    def search(
        self,
        query: str,
        query_vector: Optional[np.ndarray] = None,
        top_k: int = 10,
        use_reranker: bool = True,
    ) -> List[Dict[str, Any]]:
        """Full multi-stage retrieval pipeline.

        Args:
            query: The text query.
            query_vector: Optional pre-computed query vector.
            top_k: Number of results to return.
            use_reranker: Whether to use Cross-Encoder reranking (slower).

        Returns:
            Ranked list of result dicts with keys: id, score, text, metadata.
        """
        self._stats["total_searches"] += 1
        start = time.perf_counter()

        # ── Stage 0: Semantic Cache Lookup ──
        cache_key = self._cache.make_key(query_vector, query, top_k)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._stats["stage_cache_hit"] += 1
            elapsed = time.perf_counter() - start
            logger.debug(
                "Cache HIT for '%s' (%d results, %.3fms)",
                query[:40], len(cached), elapsed * 1000,
            )
            return cached

        self._stats["stage_cache_miss"] += 1

        # ── Stage 1: Query Expansion ──
        if self._enable_query_expansion:
            queries = self._expand_query(query)
        else:
            queries = [query]

        all_fused: Dict[str, Dict[str, Any]] = {}

        for expanded_query in queries:
            # ── Stage 2: Sparse BM25 ──
            sparse_results: List[Dict[str, Any]] = []
            if self._sparse:
                self._stats["stage_sparse"] += 1
                sparse_results = self._sparse.search(
                    expanded_query,
                    top_k=self._approx_top_k,
                )
                for r in sparse_results:
                    r["sparse_query"] = expanded_query

            # ── Stage 3: Dense FAISS ──
            dense_results: List[Dict[str, Any]] = []
            if query_vector is not None and self._dense:
                self._stats["stage_dense"] += 1
                try:
                    dense_raw = self._dense.search(
                        query_vector,
                        top_k=self._approx_top_k,
                    )
                    for sr in dense_raw:
                        meta = sr.metadata or {}
                        dense_results.append({
                            "id": sr.id,
                            "score": float(sr.score),
                            "text": meta.get("text", meta.get("content", "")),
                            "dense_query": expanded_query,
                            **meta,
                        })
                except Exception as e:
                    logger.warning("Dense search failed: %s", e)

            # ── Stage 4: RRF Fusion ──
            if sparse_results and dense_results:
                self._stats["stage_fusion"] += 1
                fused = fuse_scores_sparse_dense(
                    sparse_results, dense_results,
                    alpha=self._fusion_alpha,
                    top_k=self._approx_top_k,
                )
            elif sparse_results:
                fused = sparse_results
            elif dense_results:
                fused = dense_results
            else:
                continue

            # Merge across query expansions
            for r in fused:
                rid = r["id"]
                if rid not in all_fused or r.get("rrf_score", 0) > all_fused[rid].get("rrf_score", 0):
                    all_fused[rid] = r

        # Sort fused results by RRF score
        results = sorted(all_fused.values(), key=lambda x: x.get("rrf_score", x.get("score", 0)), reverse=True)

        # ── Stage 5: Cross-Encoder Reranking ──
        if use_reranker and self._reranker and len(results) > 0:
            try:
                self._stats["stage_reranker"] += 1
                results = self._reranker.rerank(
                    query, results,
                    top_k=max(top_k, self._final_top_k),
                    text_key="text",
                )
            except Exception as e:
                logger.warning("Reranker failed, using fusion results: %s", e)

        # ── Stage 6: Temporal + Importance Boost ──
        results = self._apply_boosts(query, results)

        # ── Stage 7: Cache the result ──
        final = results[:top_k]
        self._cache.set(cache_key, final, ttl=300)

        elapsed = time.perf_counter() - start
        self._stats["avg_results"] = (
            (self._stats["avg_results"] * (self._stats["total_searches"] - 1) + len(final))
            / self._stats["total_searches"]
        )

        logger.info(
            "Search '%s': %d results in %.3fs (sparse=%d, dense=%d, rerank=%s)",
            query[:50], len(final), elapsed,
            len(sparse_results) if sparse_results else 0,
            len(dense_results) if dense_results else 0,
            use_reranker and self._reranker is not None,
        )

        return final

    def _apply_boosts(
        self,
        query: str,
        results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Apply temporal decay and importance boosting to results.

        Modified score = base_score * (1 + importance_boost) * temporal_decay
        """
        import time as time_module

        now = time_module.time()

        for r in results:
            # Importance boost (0.0-1.0)
            importance = r.get("importance", 0.5)
            if isinstance(importance, str):
                try:
                    importance = float(importance)
                except (ValueError, TypeError):
                    importance = 0.5

            # Temporal decay (newer = higher)
            created_at = r.get("created_at", r.get("timestamp", 0))
            if isinstance(created_at, str):
                try:
                    import dateutil.parser
                    created_at = dateutil.parser.parse(created_at).timestamp()
                except Exception:
                    try:
                        created_at = float(created_at)
                    except (ValueError, TypeError):
                        created_at = 0

            age_hours = (now - float(created_at)) / 3600 if float(created_at) > 0 else 24 * 365
            temporal_decay = max(0.1, 1.0 - (age_hours / (24 * 30)))  # 30-day half-life

            # Combine: base_score * (1 + 0.3*importance) * temporal_decay
            base_score = r.get("score", r.get("rrf_score", 0.5))
            r["boosted_score"] = base_score * (1 + 0.3 * importance) * temporal_decay
            r["temporal_decay"] = temporal_decay
            r["importance_boost"] = importance

        # Re-sort by boosted score
        results.sort(key=lambda x: x.get("boosted_score", 0), reverse=True)
        return results

    # ── Statistics ──

    def statistics(self) -> Dict[str, Any]:
        """Pipeline usage statistics."""
        dense_stats = self._dense.statistics() if self._dense else {}
        sparse_stats = self._sparse.statistics() if self._sparse else {}
        reranker_stats = self._reranker.statistics() if self._reranker else {}

        return {
            "searches": self._stats["total_searches"],
            "avg_results": round(self._stats["avg_results"], 1),
            "stage_sparse_runs": self._stats["stage_sparse"],
            "stage_dense_runs": self._stats["stage_dense"],
            "stage_fusion_runs": self._stats["stage_fusion"],
            "stage_reranker_runs": self._stats["stage_reranker"],
            "cache_hits": self._stats["stage_cache_hit"],
            "cache_misses": self._stats["stage_cache_miss"],
            "cache_hit_rate_pct": round(
                self._stats["stage_cache_hit"] / max(
                    self._stats["stage_cache_hit"] + self._stats["stage_cache_miss"], 1
                ) * 100, 1
            ),
            "dense_index": dense_stats,
            "sparse_index": sparse_stats,
            "reranker": reranker_stats,
            "cache": self._cache.statistics(),
            "config": {
                "dim": self._dim,
                "approx_top_k": self._approx_top_k,
                "final_top_k": self._final_top_k,
                "fusion_alpha": self._fusion_alpha,
                "query_expansion": self._enable_query_expansion,
            },
        }


# ── Legacy compat layer ───────────────────────────────────────────────

class ThreePhaseTriSignalRetrieval:
    """Backward-compatible wrapper for the new retrieval pipeline.

    The original ThreePhaseTriSignalRetrieval was Trinity's three-phase
    tri-signal retrieval system. This wraps the new multi-stage pipeline
    in the same interface.
    """

    def __init__(self, dim: int = 1024, **kwargs):
        self._pipeline = TrinityRetrievalPipeline(dim=dim, **kwargs)

    def search(
        self, query: str, query_vector: np.ndarray,
        top_k: int = 10, **kwargs,
    ) -> List[Dict[str, Any]]:
        return self._pipeline.search(query, query_vector, top_k=top_k)

    @property
    def pipeline(self) -> TrinityRetrievalPipeline:
        return self._pipeline
