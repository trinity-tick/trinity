"""
Hybrid Index - combines multiple vector indexes for multi-stage retrieval.

Architecture:
  - Stage 1: Fast approximate (ANN) - filters to broad candidate set
  - Stage 2: Exact re-ranking (brute force on candidates) - refines scores

This matches Trinity's ProgressiveCascade L2->L5 pattern but with real vectors.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from trinity.vector_index.index import (
    VectorIndex,
    SearchResult,
    create_index,
    NumpyBruteForceIndex,
)


class HybridIndex(VectorIndex):
    """Two-stage hybrid index: approximate search + exact re-ranking.

    Stage 1: Fast approximate nearest neighbor (FAISS IVF/Annoy)
    Stage 2: Brute force exact re-ranking on top-K candidates from stage 1
    """

    def __init__(
        self,
        dim: int,
        metric: str = "cosine",
        approx_backend: str = "auto",
        approx_top_k: int = 100,
        final_top_k: int = 10,
        **approx_kwargs,
    ):
        super().__init__(dim, metric)
        self._approx = create_index(approx_backend, dim, metric, **approx_kwargs)
        self._exact = NumpyBruteForceIndex(dim, metric)
        self._approx_top_k = approx_top_k
        self._final_top_k = final_top_k
        self._stage1_stats = {"runs": 0, "avg_candidates": 0}
        self._stage2_stats = {"runs": 0, "avg_reranked": 0}

    def _add_vector(self, entry):
        self._approx.add(entry.id, entry.vector, entry.metadata)
        self._exact.add(entry.id, entry.vector, entry.metadata)

    def _search_vectors(self, query: np.ndarray, top_k: int) -> List:
        # Stage 1: Fast approximate
        self._stage1_stats["runs"] += 1
        stage1_k = min(self._approx_top_k, len(self._entries))
        approx_results = self._approx._search_vectors(query, stage1_k)
        self._stage1_stats["avg_candidates"] = (
            (self._stage1_stats["avg_candidates"] * (self._stage1_stats["runs"] - 1) + len(approx_results))
            / self._stage1_stats["runs"]
        )

        # Stage 2: Exact re-rank on candidates
        self._stage2_stats["runs"] += 1
        if approx_results:
            candidate_ids = {id_ for id_, _ in approx_results}
            # Re-score using exact (brute force)
            exact_results = self._exact._search_vectors(query, len(candidate_ids))
            filtered = [(id_, s) for id_, s in exact_results if id_ in candidate_ids]
        else:
            filtered = []

        self._stage2_stats["avg_reranked"] = (
            (self._stage2_stats["avg_reranked"] * (self._stage2_stats["runs"] - 1) + len(filtered))
            / self._stage2_stats["runs"]
        )

        final_k = min(top_k, len(filtered))
        return filtered[:final_k]

    def _rebuild(self):
        self._approx._rebuild()
        self._exact._rebuild()

    def statistics(self) -> Dict[str, Any]:
        stats = super().statistics()
        stats.update({
            "approx_top_k": self._approx_top_k,
            "final_top_k": self._final_top_k,
            "stage1_avg_candidates": round(self._stage1_stats["avg_candidates"], 1),
            "stage2_avg_reranked": round(self._stage2_stats["avg_reranked"], 1),
            "approx_backend": self._approx.__class__.__name__,
        })
        return stats


def create_hybrid_index(
    dim: int = 1024,
    metric: str = "cosine",
    approx_top_k: int = 100,
    final_top_k: int = 10,
) -> HybridIndex:
    """Create a hybrid vector index with sensible defaults."""
    return HybridIndex(
        dim=dim,
        metric=metric,
        approx_top_k=approx_top_k,
        final_top_k=final_top_k,
    )
