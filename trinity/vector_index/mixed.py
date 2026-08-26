"""
Hybrid Index - combines multiple retrieval strategies for best results.

Architecture:
  - Stage 1: Sparse keyword (BM25) - captures exact term matches
  - Stage 2: Dense ANN (FAISS/Annoy) - captures semantic similarity
  - Stage 3: Exact re-ranking (brute force) - refines top candidates

Plus optional Stage 4: Cross-Encoder reranking for precision (from reranker.py)

This replaces Trinity's hash-based pseudo-semantic matching with true
multi-stage hybrid search, aligning with industry best practices
(Weaviate hybrid_search, Pinecone sparse-dense, Elasticsearch + vector).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from trinity.vector_index.index import (
    VectorIndex,
    SearchResult,
    create_index,
    NumpyBruteForceIndex,
)
from trinity.vector_index.sparse import (
    BM25SparseRetriever,
    fuse_scores_sparse_dense,
)
from trinity.vector_index.reranker import (
    CrossEncoderReranker,
)

logger = logging.getLogger(__name__)


class HybridIndex(VectorIndex):
    """Three-stage hybrid index: sparse + dense + exact rerank.

    Pipeline:
        Sparse (BM25) ─┐
                       ├── RRF Fusion ──→ Stage 3: Exact ──→ Final Results
        Dense (ANN)   ─┘                 (re-rank)

    The BM25 component captures exact keyword/synonym matches that
    dense vectors might miss, while the dense ANN captures semantic
    relationships that keyword search can't express.
    """

    def __init__(
        self,
        dim: int,
        metric: str = "cosine",
        approx_backend: str = "auto",
        approx_top_k: int = 100,
        final_top_k: int = 10,
        enable_sparse: bool = True,
        enable_reranker: Optional[bool] = None,
        fusion_alpha: float = 0.3,
        **approx_kwargs,
    ):
        """Initialize hybrid index.

        Args:
            dim: Embedding dimension.
            metric: Distance metric ("cosine" or "l2").
            approx_backend: ANN backend ("auto", "faiss", "annoy").
            approx_top_k: Candidates retrieved from ANN stage.
            final_top_k: Final results after reranking.
            enable_sparse: Enable BM25 sparse retrieval.
            enable_reranker: Enable Cross-Encoder reranking (slower).
                None → env-gated default ON (TRINITY_RERANKER, default "on");
                explicit True/False always wins. Reranker falls back to
                identity (no-op) when the model cannot be loaded.
            fusion_alpha: Weight for sparse scores in RRF fusion (0-1).
        """
        super().__init__(dim, metric)
        self._approx = create_index(approx_backend, dim, metric, **approx_kwargs)
        self._exact = NumpyBruteForceIndex(dim, metric)
        self._approx_top_k = approx_top_k
        self._final_top_k = final_top_k
        self._fusion_alpha = fusion_alpha
        self._enable_sparse = enable_sparse
        if enable_reranker is None:
            enable_reranker = os.environ.get(
                "TRINITY_RERANKER", "on"
            ).strip().lower() in ("1", "on", "true", "yes")
        self._enable_reranker = enable_reranker

        # BM25 sparse retriever
        self._sparse: Optional[BM25SparseRetriever] = None
        if enable_sparse:
            self._sparse = BM25SparseRetriever()

        # Cross-Encoder reranker
        self._reranker: Optional[CrossEncoderReranker] = None
        if enable_reranker:
            self._reranker = CrossEncoderReranker()

        # Stats
        self._stage_stats = {
            "sparse_runs": 0,
            "dense_runs": 0,
            "fusion_runs": 0,
            "reranker_runs": 0,
            "avg_sparse_results": 0,
            "avg_dense_results": 0,
        }

    # ── Index Management ───────────────────────────────────────────

    def _add_vector(self, entry):
        """Add vector to both approximate and exact indexes."""
        self._approx.add(entry.id, entry.vector, entry.metadata)
        self._exact.add(entry.id, entry.vector, entry.metadata)

        # Also add text to BM25 sparse index if enabled
        if self._sparse and entry.metadata:
            text = (
                entry.metadata.get("text")
                or entry.metadata.get("content")
                or entry.metadata.get("description", "")
            )
            if text:
                self._sparse.add_documents([text], [entry.id])

    def _rebuild(self):
        """Rebuild underlying indexes."""
        self._approx._rebuild()
        self._exact._rebuild()

    def get_entries(self) -> List[Any]:
        """Get all entries from the underlying exact index."""
        return self._exact._entries

    # ── Search ─────────────────────────────────────────────────────

    def _search_vectors(self, query: np.ndarray, top_k: int) -> List[Tuple[str, float]]:
        """Pure vector search (used internally, bypasses sparse/reranker)."""
        # Stage 1: Fast approximate
        self._stage_stats["dense_runs"] += 1
        stage1_k = min(self._approx_top_k, len(self._entries))
        approx_results = self._approx._search_vectors(query, stage1_k)

        # Stage 2: Exact re-rank on candidates
        if approx_results:
            candidate_ids = {id_ for id_, _ in approx_results}
            exact_results = self._exact._search_vectors(query, len(candidate_ids))
            filtered = [(id_, s) for id_, s in exact_results if id_ in candidate_ids]
        else:
            filtered = []

        final_k = min(top_k, len(filtered))
        return filtered[:final_k]

    def search(
        self,
        query_vector: np.ndarray,
        top_k: int = 10,
        query_text: Optional[str] = None,
    ) -> List[SearchResult]:
        """Full hybrid search with optional BM25 sparse + dense fusion.

        Args:
            query_vector: The query embedding vector.
            top_k: Number of results to return.
            query_text: Optional query text for BM25 sparse search.
                        If None, falls back to pure dense search.

        Returns:
            List of SearchResult objects.
        """
        # If no sparse available, use dense-only
        if not self._sparse or not query_text:
            dense_results = super().search(query_vector, top_k)
            return dense_results

        # ── Stage 1: Dense ANN (semantic) ──
        self._stage_stats["dense_runs"] += 1
        stage1_k = min(self._approx_top_k, len(self._entries))
        approx_raw = self._approx._search_vectors(query_vector, stage1_k)
        self._stage_stats["avg_dense_results"] = (
            (self._stage_stats["avg_dense_results"] *
             (self._stage_stats["dense_runs"] - 1) + len(approx_raw))
            / self._stage_stats["dense_runs"]
        )

        # Convert to dict format for fusion
        dense_dict = []
        for id_, score in approx_raw:
            entry = self._exact._entries.get(id_)
            text = ""
            metadata = {}
            if entry:
                metadata = entry.metadata or {}
                text = (metadata.get("text") or metadata.get("content") or "")
            dense_dict.append({
                "id": id_,
                "score": float(score),
                "text": text,
                **metadata,
            })

        # ── Stage 2: Sparse BM25 (keyword) ──
        self._stage_stats["sparse_runs"] += 1
        sparse_results = self._sparse.search(query_text, top_k=stage1_k)
        self._stage_stats["avg_sparse_results"] = (
            (self._stage_stats["avg_sparse_results"] *
             (self._stage_stats["sparse_runs"] - 1) + len(sparse_results))
            / self._stage_stats["sparse_runs"]
        )

        # ── Stage 3: RRF Fusion ──
        self._stage_stats["fusion_runs"] += 1
        fused = fuse_scores_sparse_dense(
            sparse_results, dense_dict,
            alpha=self._fusion_alpha,
            top_k=stage1_k,
        )

        # ── Stage 4: Exact re-rank on fused candidates ──
        if fused:
            fused_ids = {r["id"] for r in fused}
            exact_results = self._exact._search_vectors(query_vector, len(fused_ids))
            exact_map = {id_: s for id_, s in exact_results}

            for r in fused:
                r["exact_dense_score"] = float(exact_map.get(r["id"], 0.0))

            # Re-rank: 0.6 * RRF + 0.4 * exact_dense
            for r in fused:
                r["combined_score"] = (
                    0.6 * r["rrf_score"] +
                    0.4 * r.get("exact_dense_score", 0)
                )

            fused.sort(key=lambda x: x["combined_score"], reverse=True)

        final_results = fused[:max(top_k, self._final_top_k)]

        # ── Stage 5 (optional): Cross-Encoder reranking ──
        if self._reranker and self._enable_reranker and query_text:
            try:
                self._stage_stats["reranker_runs"] += 1
                final_results = self._reranker.rerank(
                    query_text, final_results,
                    top_k=top_k,
                    text_key="text",
                )
            except Exception as e:
                logger.warning("Reranker failed, using fusion results: %s", e)

        # ── Convert to SearchResult format ──
        search_results = []
        for r in final_results[:top_k]:
            entry = self._exact._entries.get(r["id"])
            if entry:
                search_results.append(SearchResult(
                    id=r["id"],
                    score=r.get("combined_score", r.get("score", 0)),
                    metadata=entry.metadata or {},
                ))

        return search_results

    # ── Statistics ─────────────────────────────────────────────────

    def statistics(self) -> Dict[str, Any]:
        stats = super().statistics()
        stats.update({
            "hybrid_mode": "sparse_dense_fusion" if self._sparse else "dense_only",
            "approx_top_k": self._approx_top_k,
            "final_top_k": self._final_top_k,
            "fusion_alpha": self._fusion_alpha,
            "enable_sparse": self._enable_sparse,
            "enable_reranker": self._enable_reranker,
            "approx_backend": self._approx.__class__.__name__,
            "stage_dense_runs": self._stage_stats["dense_runs"],
            "stage_sparse_runs": self._stage_stats["sparse_runs"],
            "stage_fusion_runs": self._stage_stats["fusion_runs"],
            "stage_reranker_runs": self._stage_stats["reranker_runs"],
            "avg_dense_results": round(self._stage_stats["avg_dense_results"], 1),
            "avg_sparse_results": round(self._stage_stats["avg_sparse_results"], 1),
        })

        # Add sparse stats if available
        if self._sparse:
            stats["sparse"] = self._sparse.statistics()
        if self._reranker:
            stats["reranker"] = self._reranker.statistics()

        return stats


def create_hybrid_index(
    dim: int = 1024,
    metric: str = "cosine",
    approx_backend: str = "auto",
    approx_top_k: int = 100,
    final_top_k: int = 10,
    enable_sparse: bool = True,
    enable_reranker: Optional[bool] = None,
) -> HybridIndex:
    """Create a hybrid multi-stage vector index with sensible defaults.

    Example:
        index = create_hybrid_index(dim=384, enable_sparse=True)
        index.add("doc1", vec1, {"text": "Machine learning ..."})
        results = index.search(query_vec, top_k=5, query_text="ML basics")
    """
    return HybridIndex(
        dim=dim,
        metric=metric,
        approx_backend=approx_backend,
        approx_top_k=approx_top_k,
        final_top_k=final_top_k,
        enable_sparse=enable_sparse,
        enable_reranker=enable_reranker,
    )
