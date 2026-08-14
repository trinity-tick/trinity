"""
Hybrid Retriever — Fuses BM25, Vector/FTS, and Graph retrieval.

Three fusion strategies:
  - fusion    Weighted linear combination (configurable weights)
  - rrf       Reciprocal Rank Fusion (robust rank-based)
  - cascade   Coarse-to-fine pipeline (vector → BM25 re-rank → graph expand)

All scores are min-max normalised to [0, 1] and deduplicated (highest
per-source score kept for each memory_id).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


class HybridRetriever:
    """Combine BM25 + Vector/FTS + Graph + Aggregator + Procedural into a single ranked result list.

    Four primary retrieval channels:
      - vector      Semantic / FTS vector search (weight 0.35)
      - bm25        Keyword inverted index (weight 0.25)
      - graph       Entity-relation graph traversal + 1-hop expansion (weight 0.25)
      - aggregator  MemoryAggregator pooled recall (weight 0.15)

    Additional channel:
      - procedural  Keyword + template matching for skill/action memories (weight 0.10)

    Parameters
    ----------
    bm25_index : BM25Index
        Keyword inverted index.
    graph_retriever : GraphRetriever
        Entity / relation / subgraph retriever.
    search_fn : callable
        Signature ``(query, top_k) -> list of memory dicts``.
        Must return dicts with at least ``memory_id`` and ``score``.
    procedural_store : callable or list, optional
        Either a list of procedural memory dicts, or a callable
        ``() -> list of procedural memory dicts``. Each dict must
        contain ``memory_id``, ``content``, and optionally ``tags`` /
        ``action_verbs``.
    aggregator_fn : callable, optional
        Signature ``(query, top_k) -> list of memory dicts``.
        MemoryAggregator pooled recall. Results must contain
        ``memory_id`` and ``score``.
    vector_weight : float
        Default fusion weight for vector/FTS source.
    bm25_weight : float
        Default fusion weight for BM25.
    graph_weight : float
        Default fusion weight for graph source.
    aggregator_weight : float
        Default fusion weight for aggregator source.
    procedural_weight : float
        Default fusion weight for procedural source.  Ignored when
        procedural_store is None.
    rrf_k : int
        RRF constant (default 60).
    cascade_top_n : int
        Number of candidates for cascade first stage.
    """

    def __init__(
        self,
        bm25_index,
        graph_retriever,
        search_fn: Callable,
        procedural_store=None,
        *,
        aggregator_fn=None,
        vector_weight: float = 0.35,
        bm25_weight: float = 0.25,
        graph_weight: float = 0.25,
        aggregator_weight: float = 0.15,
        procedural_weight: float = 0.10,
        rrf_k: int = 60,
        cascade_top_n: int = 50,
    ):
        self._bm25 = bm25_index
        self._graph = graph_retriever
        self._search_fn = search_fn
        self._procedural_store = procedural_store
        self._aggregator_fn = aggregator_fn

        self.vector_weight = vector_weight
        self.bm25_weight = bm25_weight
        self.graph_weight = graph_weight
        self.aggregator_weight = aggregator_weight
        self.procedural_weight = procedural_weight
        self.rrf_k = rrf_k
        self.cascade_top_n = cascade_top_n

    # ── Public API ──────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 10,
        strategy: str = "fusion",
    ) -> Dict[str, Any]:
        """Hybrid search entry point.

        Parameters
        ----------
        query : str
            Search query.
        top_k : int
            Max results.
        strategy : str
            ``fusion`` | ``rrf`` | ``cascade``.

        Returns
        -------
        dict with keys:
            results : list of memory dicts with ``hybrid_score`` and
                      per-source scores (vector_score / bm25_score /
                      graph_score).
            strategy : str
            query : str
            breakdown : dict of per-source stat counts
        """
        strategy = strategy.lower()
        if strategy not in ("fusion", "rrf", "cascade"):
            strategy = "fusion"

        # ── collect raw results from each source ───────────────────
        vector_results = self._get_vector_results(query, top_k)
        bm25_results = self._get_bm25_results(query, top_k)
        graph_results = self._get_graph_results(query, top_k)
        # 1-hop neighbour expansion on graph results
        graph_results = self._expand_graph_neighbors(graph_results, top_k)
        proc_results = self._search_procedural(query, top_k)
        aggr_results = self._get_aggregator_results(query, top_k)

        # ── normalise scores ───────────────────────────────────────
        vector_norm = _minmax_normalise(vector_results, "score")
        bm25_norm = _minmax_normalise(bm25_results, "score", raw_key="bm25_score")
        graph_norm = _minmax_normalise(graph_results, "graph_score", raw_key="graph_score")
        proc_norm = _minmax_normalise(proc_results, "procedural_score", raw_key="procedural_score")
        aggr_norm = _minmax_normalise(aggr_results, "score", raw_key="aggregator_score")

        # ── fuse ────────────────────────────────────────────────────
        if strategy == "fusion":
            fused = self._fusion_fuse(vector_norm, bm25_norm, graph_norm, aggr_norm, proc_norm, top_k)
        elif strategy == "rrf":
            fused = self._rrf_fuse(vector_norm, bm25_norm, graph_norm, aggr_norm, proc_norm, top_k)
        else:  # cascade
            fused = self._cascade_fuse(vector_norm, bm25_norm, graph_norm, aggr_norm, proc_norm, top_k)

        return {
            "results": fused,
            "strategy": strategy,
            "query": query,
            "breakdown": {
                "vector": len(vector_results),
                "bm25": len(bm25_results),
                "graph": len(graph_results),
                "aggregator": len(aggr_results),
                "procedural": len(proc_results),
                "unique_fused": len(fused),
            },
        }

    def search_cross_modal(
        self,
        query: str,
        query_type: str = "auto",
        top_k: int = 10,
    ) -> Dict[str, Any]:
        """Cross-modal search: text ↔ image memory retrieval.

        Routes to the CrossModalRetriever (fourth retrieval source,
        weight 0.1 in fusion strategies). Supports:
          - text → image_description memories
          - image → text memories
          - auto-detect query type

        Parameters
        ----------
        query : str
            Text query or image file path.
        query_type : str
            ``auto`` | ``text`` | ``image`` | ``combined``.
        top_k : int
            Max results.

        Returns
        -------
        dict with results / query_type / total.
        """
        # Lazy-init cross-modal retriever
        if not hasattr(self, "_cross_modal") or self._cross_modal is None:
            from trinity.retrieval.cross_modal import CrossModalRetriever
            self._cross_modal = CrossModalRetriever(
                trinity_instance=getattr(self, "_trinity", None),
            )
        return self._cross_modal.search_cross_modal(
            query=query, query_type=query_type, top_k=top_k,
        )

    # ── source feeders ──────────────────────────────────────────────

    def _get_vector_results(
        self, query: str, top_k: int,
    ) -> List[Dict[str, Any]]:
        try:
            return self._search_fn(query, top_k)
        except Exception:
            return []

    def _get_bm25_results(
        self, query: str, top_k: int,
    ) -> List[Dict[str, Any]]:
        try:
            hits = self._bm25.search(query, top_k=top_k)
            return [{"memory_id": doc_id, "score": score} for doc_id, score in hits]
        except Exception:
            return []

    def _get_graph_results(
        self, query: str, top_k: int,
    ) -> List[Dict[str, Any]]:
        try:
            return self._graph.search_by_entity(query, top_k=top_k)
        except Exception:
            return []

    def _get_aggregator_results(
        self, query: str, top_k: int,
    ) -> List[Dict[str, Any]]:
        """Recall from MemoryAggregator pooled memory."""
        if self._aggregator_fn is None:
            return []
        try:
            raw = self._aggregator_fn(query, top_k)
            if not raw:
                return []
            # Normalise to unified dict format
            result = []
            for item in raw:
                mid = item.get("memory_id") or item.get("id")
                if not mid:
                    continue
                result.append({
                    "memory_id": mid,
                    "content": item.get("content", ""),
                    "aggregator_score": item.get("score", 0),
                    "source": "aggregator",
                })
            return result
        except Exception:
            return []

    def _expand_graph_neighbors(
        self, graph_results: List[Dict], top_k: int,
    ) -> List[Dict]:
        """1-hop neighbour expansion: supplement graph hits with adjacent entities.

        For each graph result, query 1-hop neighbours and inject them into
        the result list with a discounted score (0.7 × original).
        """
        if not graph_results or self._graph is None:
            return graph_results

        try:
            seen = {r["memory_id"] for r in graph_results}
            expanded = list(graph_results)
            for item in graph_results:
                mid = item.get("memory_id", "")
                if not mid:
                    continue
                neighbours = self._graph.get_neighbors(mid) or []
                for nb in neighbours[:3]:  # limit neighbours per entity
                    nid = nb.get("neighbor_id") or nb.get("memory_id") or nb.get("id")
                    if not nid or nid in seen:
                        continue
                    seen.add(nid)
                    expanded.append({
                        "memory_id": nid,
                        "content": nb.get("content", ""),
                        "graph_score": round(item.get("graph_score", 0) * 0.7, 6),
                        "source": "graph_1hop",
                    })
            return expanded[: max(len(expanded), top_k)]
        except Exception:
            return graph_results

    def _search_procedural(self, query: str, top_k: int,
                            ) -> List[Dict[str, Any]]:
        """Keyword + template matching for procedural (skill/action) memories.

        Query is tokenised and matched against:
          - ``content`` (case-insensitive substring or partial-word overlap)
          - ``action_verbs`` / ``tags`` fields when present
        """
        store = self._procedural_store
        if store is None:
            return []

        # Resolve callable → list
        if callable(store):
            try:
                templates = store()
            except Exception:
                return []
        else:
            templates = store

        if not templates:
            return []

        # Tokenise query into lowercase words
        q_words = set(_tokenise(query))

        scored: List[Dict[str, Any]] = []
        for tmpl in templates:
            content = (tmpl.get("content") or "").lower()
            tags = [t.lower() for t in (tmpl.get("tags") or [])]
            action_verbs = [v.lower() for v in (tmpl.get("action_verbs") or [])]

            # Score = Jaccard-like overlap of query words against
            # content words + tags + action_verbs
            t_words = set(_tokenise(content)) | set(tags) | set(action_verbs)
            if not t_words:
                continue
            overlap = len(q_words & t_words)
            union = len(q_words | t_words)
            score = round(overlap / union, 6) if union > 0 else 0.0
            if score > 0:
                scored.append({
                    "memory_id": tmpl["memory_id"],
                    "content": tmpl.get("content", ""),
                    "procedural_score": score,
                })

        scored.sort(key=lambda x: x["procedural_score"], reverse=True)
        return scored[:top_k]

    # ── fusion strategies ───────────────────────────────────────────

    def _fusion_fuse(
        self,
        vector: List[Dict],
        bm25: List[Dict],
        graph: List[Dict],
        aggregator: List[Dict],
        procedural: List[Dict],
        top_k: int,
    ) -> List[Dict]:
        wv, wb, wg, wa, wp = (
            self.vector_weight,
            self.bm25_weight,
            self.graph_weight,
            self.aggregator_weight if self._aggregator_fn is not None else 0.0,
            self.procedural_weight if self._procedural_store is not None else 0.0,
        )
        merged: Dict[str, Dict] = {}

        for item in vector:
            mid = item["memory_id"]
            merged[mid] = _init_entry(item, "vector", item.get("score", 0) * wv)

        for item in bm25:
            mid = item["memory_id"]
            bs = item.get("score", 0)
            if mid in merged:
                merged[mid]["bm25_score"] = bs
                merged[mid]["hybrid_score"] += bs * wb
            else:
                merged[mid] = _init_entry(item, "bm25", bs * wb)

        for item in graph:
            mid = item["memory_id"]
            gs = item.get("graph_score", item.get("score", 0))
            if mid in merged:
                merged[mid]["graph_score"] = gs
                merged[mid]["hybrid_score"] += gs * wg
            else:
                merged[mid] = _init_entry(item, "graph", gs * wg)

        for item in aggregator:
            mid = item["memory_id"]
            ags = item.get("aggregator_score", item.get("score", 0))
            if mid in merged:
                merged[mid]["aggregator_score"] = ags
                merged[mid]["hybrid_score"] += ags * wa
            else:
                entry = _init_entry(item, "aggregator", ags * wa)
                entry["aggregator_score"] = ags
                merged[mid] = entry

        for item in procedural:
            mid = item["memory_id"]
            ps = item.get("procedural_score", 0)
            if mid in merged:
                merged[mid]["procedural_score"] = ps
                merged[mid]["hybrid_score"] += ps * wp
            else:
                entry = _init_entry(item, "procedural", ps * wp)
                entry["procedural_score"] = ps
                merged[mid] = entry

        return _sort_and_trim(merged, top_k)

    def _rrf_fuse(
        self,
        vector: List[Dict],
        bm25: List[Dict],
        graph: List[Dict],
        aggregator: List[Dict],
        procedural: List[Dict],
        top_k: int,
    ) -> List[Dict]:
        def _rank(source_list, score_key):
            ranked = sorted(source_list, key=lambda x: x.get(score_key, 0), reverse=True)
            return {item["memory_id"]: r for r, item in enumerate(ranked)}

        v_rank = _rank(vector, "score")
        b_rank = _rank(bm25, "score")
        g_rank = _rank(graph, "graph_score")
        a_rank = _rank(aggregator, "aggregator_score")
        p_rank = _rank(procedural, "procedural_score")

        all_ids = set(v_rank) | set(b_rank) | set(g_rank) | set(a_rank) | set(p_rank)
        merged: Dict[str, Dict] = {}

        wa = 1.0 if self._aggregator_fn is not None else 0.0
        wp = 1.0 if self._procedural_store is not None else 0.0

        for mid in all_ids:
            rrf = (
                1.0 / (self.rrf_k + v_rank.get(mid, len(v_rank)))
                + 1.0 / (self.rrf_k + b_rank.get(mid, len(b_rank)))
                + 1.0 / (self.rrf_k + g_rank.get(mid, len(g_rank)))
                + wa * 1.0 / (self.rrf_k + a_rank.get(mid, len(a_rank)))
                + wp * 1.0 / (self.rrf_k + p_rank.get(mid, len(p_rank)))
            )
            entry: Dict[str, Any] = {"memory_id": mid, "hybrid_score": round(rrf, 6)}
            if mid in v_rank:
                entry["vector_score"] = next(
                    (it.get("score", 0) for it in vector if it["memory_id"] == mid), 0)
            if mid in b_rank:
                entry["bm25_score"] = next(
                    (it.get("score", 0) for it in bm25 if it["memory_id"] == mid), 0)
            if mid in g_rank:
                entry["graph_score"] = next(
                    (it.get("graph_score", 0) for it in graph if it["memory_id"] == mid), 0)
            if mid in a_rank:
                entry["aggregator_score"] = next(
                    (it.get("aggregator_score", 0) for it in aggregator if it["memory_id"] == mid), 0)
            if mid in p_rank:
                entry["procedural_score"] = next(
                    (it.get("procedural_score", 0) for it in procedural if it["memory_id"] == mid), 0)
            merged[mid] = entry

        return _sort_and_trim(merged, top_k)

    def _cascade_fuse(
        self,
        vector: List[Dict],
        bm25: List[Dict],
        graph: List[Dict],
        aggregator: List[Dict],
        procedural: List[Dict],
        top_k: int,
    ) -> List[Dict]:
        # Stage 1: vector coarse rank
        stage1 = sorted(vector, key=lambda x: x.get("score", 0), reverse=True)[:self.cascade_top_n]

        # Stage 2: BM25 re-rank (re-score candidates against query)
        stage1_ids = {m["memory_id"] for m in stage1}
        bm25_candidates = [
            it for it in bm25 if it["memory_id"] in stage1_ids
        ]
        bm25_candidates.sort(key=lambda x: x.get("score", 0), reverse=True)

        # Stage 3: graph + aggregator + procedural expansion
        merged: Dict[str, Dict] = {}
        for item in bm25_candidates[:top_k * 2]:
            mid = item["memory_id"]
            bs = item.get("score", 0)
            merged[mid] = {
                "memory_id": mid,
                "bm25_score": bs,
                "hybrid_score": bs,
            }
        for item in graph:
            mid = item["memory_id"]
            gs = item.get("graph_score", 0)
            if mid in merged:
                merged[mid]["graph_score"] = gs
                merged[mid]["hybrid_score"] += gs * 0.25
            elif mid in stage1_ids:
                merged[mid] = {
                    "memory_id": mid,
                    "graph_score": gs,
                    "hybrid_score": gs * 0.25,
                }
        for item in aggregator:
            mid = item["memory_id"]
            ags = item.get("aggregator_score", 0)
            if mid in merged:
                merged[mid]["aggregator_score"] = ags
                merged[mid]["hybrid_score"] += ags * 0.15
            elif mid in stage1_ids:
                merged[mid] = {
                    "memory_id": mid,
                    "aggregator_score": ags,
                    "hybrid_score": ags * 0.15,
                }
        for item in procedural:
            mid = item["memory_id"]
            ps = item.get("procedural_score", 0)
            if mid in merged:
                merged[mid]["procedural_score"] = ps
                merged[mid]["hybrid_score"] += ps * 0.10
            elif mid in stage1_ids:
                merged[mid] = {
                    "memory_id": mid,
                    "procedural_score": ps,
                    "hybrid_score": ps * 0.10,
                }

        return _sort_and_trim(merged, top_k)


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _minmax_normalise(
    items: List[Dict],
    score_key: str,
    raw_key: Optional[str] = None,
) -> List[Dict]:
    """Normalise scores to [0, 1] in-place; return original list."""
    if not items:
        return items
    scores = [it.get(score_key, 0) for it in items]
    mn, mx = min(scores), max(scores)
    rng = mx - mn
    out_key = raw_key or score_key
    for it in items:
        raw = it.get(score_key, 0)
        it[out_key] = round((raw - mn) / rng, 6) if rng > 1e-9 else 1.0
    return items


def _init_entry(item: Dict, source: str, init_score: float) -> Dict:
    entry = {
        "memory_id": item["memory_id"],
        "content": item.get("content", ""),
        "hybrid_score": init_score,
        "vector_score": 0,
        "bm25_score": 0,
        "graph_score": 0,
        "aggregator_score": 0,
        "procedural_score": 0,
    }
    if source == "vector":
        entry["vector_score"] = item.get("score", 0)
        for key in item:
            if key not in entry:
                entry[key] = item[key]
    elif source == "bm25":
        entry["bm25_score"] = item.get("score", 0)
    elif source == "graph":
        entry["graph_score"] = item.get("graph_score", 0)
        for key in item:
            if key not in entry:
                entry[key] = item[key]
    elif source == "procedural":
        entry["procedural_score"] = item.get("procedural_score", 0)
    return entry


def _sort_and_trim(merged: Dict, top_k: int) -> List[Dict]:
    results = sorted(merged.values(), key=lambda x: x["hybrid_score"], reverse=True)
    return results[:top_k]


def _tokenise(text: str) -> List[str]:
    """Simple word tokeniser: lowercase, split on non-alpha chars, drop empty."""
    import re
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def self_test() -> Dict[str, Any]:
    """Self-contained verification of HybridRetriever (P0.5).

    Uses lightweight mock sources to validate:
      1. Default fusion weights match the P0.5 spec
         (vector 0.35 / bm25 0.25 / graph 0.25 / aggregator 0.15 /
          procedural 0.10).
      2. ``fusion`` / ``rrf`` / ``cascade`` all return ranked results.
      3. 1-hop graph neighbour expansion is exercised.
    """
    class _BM25:
        def search(self, query, top_k=10):
            return [("m_1", 0.9), ("m_4", 0.5)]

    class _Graph:
        def search_by_entity(self, query, top_k=10):
            return [
                {"memory_id": "m_2", "content": "entity hit", "graph_score": 0.8},
            ]

        def get_neighbors(self, memory_id):
            return [{"neighbor_id": "m_5", "content": "neighbour"}]

    def _vector_fn(query, top_k=10):
        return [
            {"memory_id": "m_1", "content": "vector hit", "score": 0.95},
            {"memory_id": "m_2", "content": "vector hit 2", "score": 0.6},
            {"memory_id": "m_3", "content": "vector hit 3", "score": 0.3},
        ]

    def _aggr_fn(query, top_k=10):
        return [{"memory_id": "m_1", "content": "aggregated", "score": 0.7}]

    procedural_store = [
        {"memory_id": "m_6", "content": "run backup script", "tags": ["backup"]},
    ]

    retriever = HybridRetriever(
        bm25_index=_BM25(),
        graph_retriever=_Graph(),
        search_fn=_vector_fn,
        procedural_store=procedural_store,
        aggregator_fn=_aggr_fn,
    )

    passed = 0
    failed = 0
    details = []

    # Test 1: default fusion weights
    weights = (
        retriever.vector_weight,
        retriever.bm25_weight,
        retriever.graph_weight,
        retriever.aggregator_weight,
        retriever.procedural_weight,
    )
    if weights == (0.35, 0.25, 0.25, 0.15, 0.10):
        passed += 1
    else:
        failed += 1
        details.append(f"fusion weights mismatch: {weights}")

    # Test 2: fusion strategy returns ranked results with hybrid_score
    res = retriever.search("backup entity", top_k=10, strategy="fusion")
    if res["strategy"] == "fusion" and res["results"] and all(
        "hybrid_score" in r for r in res["results"]
    ):
        passed += 1
    else:
        failed += 1
        details.append(f"fusion results malformed: {res.get('breakdown')}")

    # Test 3: rrf strategy works
    res_rrf = retriever.search("backup entity", top_k=10, strategy="rrf")
    if res_rrf["strategy"] == "rrf" and res_rrf["results"]:
        passed += 1
    else:
        failed += 1
        details.append("rrf returned no results")

    # Test 4: cascade strategy works
    res_cas = retriever.search("backup entity", top_k=10, strategy="cascade")
    if res_cas["strategy"] == "cascade" and res_cas["results"]:
        passed += 1
    else:
        failed += 1
        details.append("cascade returned no results")

    # Test 5: 1-hop expansion injected neighbour
    graph_ids = {r["memory_id"] for r in res["results"]}
    if "m_5" in graph_ids:
        passed += 1
    else:
        failed += 1
        details.append("1-hop neighbour not present in fused results")

    # Test 6: breakdown stats populated
    bd = res["breakdown"]
    if bd["vector"] >= 1 and bd["bm25"] >= 1 and bd["graph"] >= 1:
        passed += 1
    else:
        failed += 1
        details.append(f"breakdown incomplete: {bd}")

    return {
        "module": "trinity.retrieval.hybrid_retriever",
        "result": "PASS" if failed == 0 else "FAIL",
        "passed": passed,
        "failed": failed,
        "total": passed + failed,
        "details": details,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(self_test(), ensure_ascii=False, indent=2))

