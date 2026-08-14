"""
Graph + Vector Hybrid Retrieval Pipeline
=========================================

Three-stage hybrid retrieval combining vector semantic search
with knowledge-graph Personalized PageRank (PPR) expansion,
fused via Reciprocal Rank Fusion (RRF) or weighted scoring.

Usage:
    from trinity.vector_index.graph_vector_hybrid import GraphVectorHybridRetriever

    retriever = GraphVectorHybridRetriever(
        vector_index=my_vector_index,
        kgraph=my_knowledge_graph,
        embed_func=my_embedding_fn,
    )
    results = retriever.search("deep learning papers", top_k=10)
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Union

import numpy as np


class GraphVectorHybridRetriever:
    """Three-stage hybrid retrieval: vector recall → graph PPR expansion → RRF fusion.

    Parameters
    ----------
    vector_index:
        A vector index instance with a ``search(query_vec, top_k) → List[SearchResult]``
        method.  Each result must expose ``.id`` (str) and ``.score`` (float).
    kgraph:
        A knowledge graph instance.  Must expose at minimum:
        * ``query_relations(entity_id, max_depth) → List[dict]``       (BFS fallback)
        * ``get_entity(entity_id) → dict | None``
        Optionally, if ``ppr_search(query_entities, top_k=...) → List[dict]`` exists
        it will be preferred over the BFS fallback.
    embed_func:
        Optional callable ``embed_func(text: str) → np.ndarray``.
        Required when ``search(query, ...)`` receives a string query.
    rrf_k:
        Reciprocal Rank Fusion constant (default 60, standard in IR literature).
    fusion_mode:
        ``"rrf"`` (default) — Reciprocal Rank Fusion.
        ``"weighted"``  — weighted linear combination of vector + graph scores.

    Examples
    --------
    >>> import numpy as np
    >>> from trinity.vector_index.index import NumpyBruteForceIndex
    >>> from trinity.kgraph.graph import KnowledgeGraph
    >>> vi = NumpyBruteForceIndex(dim=4)
    >>> vi.add("e1", np.array([0.1, 0.2, 0.3, 0.4]))
    >>> kg = KnowledgeGraph()
    >>> kg.add_entity("e1", "paper", {"title": "Deep Learning"})
    >>> kg.add_entity("e2", "author", {"name": "Hinton"})
    >>> kg.add_relation("e1", "authored_by", "e2")
    >>> retriever = GraphVectorHybridRetriever(vi, kg)
    >>> results = retriever.search(np.array([0.1, 0.2, 0.3, 0.4]), top_k=5)
    """

    def __init__(
        self,
        vector_index: Any,
        kgraph: Any,
        embed_func: Optional[Callable[[str], np.ndarray]] = None,
        rrf_k: int = 60,
        fusion_mode: str = "rrf",
    ):
        self.vector_index = vector_index
        self.kgraph = kgraph
        self.embed_func = embed_func
        self.rrf_k = rrf_k
        self.fusion_mode = fusion_mode

        # Probe whether the kgraph supports PPR search
        self._has_ppr = hasattr(kgraph, "ppr_search") and callable(
            getattr(kgraph, "ppr_search", None)
        )

    # ── Public API ────────────────────────────────────────────────────

    def search(
        self,
        query: Union[str, np.ndarray],
        top_k: int = 10,
        ppr_alpha: float = 0.85,
        ppr_max_iter: int = 100,
        vector_weight: float = 0.7,
    ) -> List[Dict[str, Any]]:
        """Execute the three-stage hybrid retrieval pipeline.

        Parameters
        ----------
        query:
            Either a raw text string (requires ``embed_func``) or a pre-computed
            embedding vector (``np.ndarray``, shape ``(dim,)``).
        top_k:
            Number of final fused results to return.
        ppr_alpha:
            Damping factor for PPR (only used when ``ppr_search`` is available).
        ppr_max_iter:
            Maximum PPR iterations (only used when ``ppr_search`` is available).
        vector_weight:
            Weight for vector scores in ``"weighted"`` fusion mode (0–1).
            Graph score weight is ``1 - vector_weight``.

        Returns
        -------
        list[dict]
            Each dict contains:
            - ``entity_id`` (str)
            - ``entity`` (dict | None) — full entity from kgraph
            - ``vector_score`` (float | None)
            - ``graph_score`` (float | None)
            - ``fused_score`` (float)
            - ``source`` (str) — ``"vector"``, ``"graph"``, or ``"both"``
        """
        # ── Stage 1: Vector semantic recall ───────────────────────────
        query_vec = self._resolve_query_vector(query)
        vector_candidates = self.vector_index.search(query_vec, top_k=top_k * 2)

        # Build vector rank map: entity_id → (rank, score)
        vector_rank: Dict[str, tuple] = {}
        for rank, result in enumerate(vector_candidates, start=1):
            vector_rank[result.id] = (rank, result.score)

        # ── Stage 2: Graph expansion from candidate entities ──────────
        candidate_ids = list(vector_rank.keys())
        graph_results: List[Dict[str, Any]] = self._graph_expand(
            candidate_ids,
            top_k=top_k,
            ppr_alpha=ppr_alpha,
            ppr_max_iter=ppr_max_iter,
        )

        # Build graph rank map: entity_id → (rank, score)
        graph_rank: Dict[str, tuple] = {}
        for rank, g in enumerate(graph_results, start=1):
            graph_rank[g["entity_id"]] = (rank, g["ppr_score"])

        # ── Stage 3: Fusion ───────────────────────────────────────────
        fused = self._fuse(vector_rank, graph_rank, top_k, vector_weight)

        # Enrich with entity info
        for item in fused:
            item["entity"] = self.kgraph.get_entity(item["entity_id"])

        return fused

    # ── Internal helpers ──────────────────────────────────────────────

    def _resolve_query_vector(self, query: Union[str, np.ndarray]) -> np.ndarray:
        """Convert a string query to an embedding vector if needed."""
        if isinstance(query, np.ndarray):
            return query.astype(np.float32)
        if isinstance(query, str):
            if self.embed_func is None:
                raise ValueError(
                    "embed_func is required when search() receives a string query. "
                    "Pass embed_func to GraphVectorHybridRetriever, or provide a "
                    "pre-computed numpy vector."
                )
            return self.embed_func(query).astype(np.float32)
        raise TypeError(f"query must be str or np.ndarray, got {type(query).__name__}")

    def _graph_expand(
        self,
        candidate_ids: List[str],
        top_k: int,
        ppr_alpha: float,
        ppr_max_iter: int,
    ) -> List[Dict[str, Any]]:
        """Expand from candidate entities via PPR (preferred) or BFS (fallback)."""
        if self._has_ppr:
            return self.kgraph.ppr_search(
                query_entities=candidate_ids,
                alpha=ppr_alpha,
                max_iter=ppr_max_iter,
                top_k=top_k,
            )
        else:
            return self._bfs_expand(candidate_ids, top_k)

    def _bfs_expand(
        self,
        entity_ids: List[str],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """Breadth-first graph expansion fallback when PPR is unavailable.

        For each candidate entity, traverse one hop and collect neighbor
        entities with scores proportional to relation weights.
        """
        if not hasattr(self.kgraph, "query_relations"):
            return []

        scored: Dict[str, float] = {}
        for eid in entity_ids:
            rels = self.kgraph.query_relations(eid, max_depth=1)
            for rel in rels:
                neighbor = (
                    rel["object"] if rel["subject"] == eid else rel["subject"]
                )
                # Skip the original candidate entities themselves
                if neighbor in entity_ids:
                    continue
                weight = float(rel.get("weight", 1.0))
                scored[neighbor] = scored.get(neighbor, 0.0) + weight

        # Sort by accumulated weight descending
        sorted_items = sorted(scored.items(), key=lambda x: -x[1])[:top_k]
        return [
            {
                "entity_id": eid,
                "entity": self.kgraph.get_entity(eid) if hasattr(self.kgraph, "get_entity") else None,
                "ppr_score": round(score, 6),
            }
            for eid, score in sorted_items
        ]

    def _fuse(
        self,
        vector_rank: Dict[str, tuple],
        graph_rank: Dict[str, tuple],
        top_k: int,
        vector_weight: float,
    ) -> List[Dict[str, Any]]:
        """Fuse vector and graph results via RRF or weighted scoring."""
        if self.fusion_mode == "weighted":
            return self._weighted_fusion(vector_rank, graph_rank, top_k, vector_weight)
        return self._rrf_fusion(vector_rank, graph_rank, top_k)

    def _rrf_fusion(
        self,
        vector_rank: Dict[str, tuple],
        graph_rank: Dict[str, tuple],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """Reciprocal Rank Fusion (RRF).

        RRF score = Σ 1 / (k + rank_r)
        where k = self.rrf_k and rank_r is the position of the document in ranker r.
        """
        all_ids: set = set(vector_rank) | set(graph_rank)
        fused_scores: Dict[str, Dict[str, Any]] = {}

        k = self.rrf_k
        for eid in all_ids:
            rrf = 0.0
            vs = None
            gs = None

            if eid in vector_rank:
                rank_v, vs = vector_rank[eid]
                rrf += 1.0 / (k + rank_v)
            if eid in graph_rank:
                rank_g, gs = graph_rank[eid]
                rrf += 1.0 / (k + rank_g)

            source = "both"
            if eid in vector_rank and eid not in graph_rank:
                source = "vector"
            elif eid in graph_rank and eid not in vector_rank:
                source = "graph"

            fused_scores[eid] = {
                "entity_id": eid,
                "vector_score": vs,
                "graph_score": gs,
                "fused_score": round(rrf, 6),
                "source": source,
                "entity": None,  # populated by caller
            }

        # Sort by fused score descending
        sorted_results = sorted(
            fused_scores.values(), key=lambda x: -x["fused_score"]
        )
        return sorted_results[:top_k]

    def _weighted_fusion(
        self,
        vector_rank: Dict[str, tuple],
        graph_rank: Dict[str, tuple],
        top_k: int,
        vector_weight: float,
    ) -> List[Dict[str, Any]]:
        """Weighted linear combination fusion.

        final = vector_weight * normalized_vector_score
              + (1 - vector_weight) * normalized_graph_score
        """
        all_ids: set = set(vector_rank) | set(graph_rank)
        gw = 1.0 - vector_weight

        # Normalize scores to [0, 1] within each ranker
        v_scores = {eid: s for eid, (_, s) in vector_rank.items()}
        g_scores = {eid: s for eid, (_, s) in graph_rank.items()}

        v_max = max(v_scores.values()) if v_scores else 1.0
        g_max = max(g_scores.values()) if g_scores else 1.0

        def _norm(v, m):
            return v / m if m > 0 else 0.0

        fused_scores: Dict[str, Dict[str, Any]] = {}
        for eid in all_ids:
            vs = v_scores.get(eid)
            gs = g_scores.get(eid)

            nvs = _norm(vs, v_max) if vs is not None else 0.0
            ngs = _norm(gs, g_max) if gs is not None else 0.0

            final = vector_weight * nvs + gw * ngs

            source = "both"
            if vs is not None and gs is None:
                source = "vector"
            elif gs is not None and vs is None:
                source = "graph"

            fused_scores[eid] = {
                "entity_id": eid,
                "vector_score": vs,
                "graph_score": gs,
                "fused_score": round(final, 6),
                "source": source,
                "entity": None,
            }

        sorted_results = sorted(
            fused_scores.values(), key=lambda x: -x["fused_score"]
        )
        return sorted_results[:top_k]


__all__ = ["GraphVectorHybridRetriever"]
