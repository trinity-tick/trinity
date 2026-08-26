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

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  Semantic result cache (env-gated, default: memory backend)
#
#  TRINITY_CACHE_BACKEND  memory | redis | off   (default: memory, since
#                          2026-08-24 COMPARISON_VS_2026_SOTA_R7 P0-2 —
#                          semantic cache is industry-standard latency/cost
#                          reduction; memory backend is dependency-free)
#  TRINITY_REDIS_URL      redis://host:port/db   (default: redis://127.0.0.1:6379/0)
#  TRINITY_CACHE_TTL      seconds                (default: 300)
#
#  The wrapper is deliberately additive: setting TRINITY_CACHE_BACKEND=off
#  restores the pre-cache retrieval pipeline behaviour exactly.
# ═══════════════════════════════════════════════════════════════════════

_cache_instance: Optional[Any] = None
_cache_instance_config: Optional[Tuple[str, str, float]] = None
_cache_instance_lock = threading.Lock()


def _get_configured_cache() -> Optional[Any]:
    """Return the env-configured SemanticCache, or None when caching is off.

    Reads ``TRINITY_CACHE_BACKEND`` / ``TRINITY_REDIS_URL`` /
    ``TRINITY_CACHE_TTL`` on every call and lazily (re)builds the shared
    ``SemanticCache`` instance whenever the effective config changes, so
    callers/tests can flip the environment and see it immediately.
    """
    global _cache_instance, _cache_instance_config

    backend = os.environ.get("TRINITY_CACHE_BACKEND", "memory").strip().lower()
    if backend not in ("memory", "redis"):
        return None

    redis_url = os.environ.get(
        "TRINITY_REDIS_URL", "redis://127.0.0.1:6379/0"
    ).strip()
    try:
        ttl = float(os.environ.get("TRINITY_CACHE_TTL", "300"))
    except ValueError:
        ttl = 300.0

    config = (backend, redis_url, ttl)
    with _cache_instance_lock:
        if _cache_instance is not None and _cache_instance_config == config:
            return _cache_instance

        from trinity.core.cache import SemanticCache

        _cache_instance = SemanticCache(
            backend=backend,
            default_ttl=ttl,
            redis_url=redis_url,
        )
        _cache_instance_config = config
        return _cache_instance


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
        ppr_fn: Optional[Callable] = None,
        pagetree_fn: Optional[Callable] = None,
        pagetree_weight: float = 0.15,
    ):
        self._bm25 = bm25_index
        self._graph = graph_retriever
        self._search_fn = search_fn
        self._procedural_store = procedural_store
        self._aggregator_fn = aggregator_fn
        # 2026-08-24（R8 P1-4）：PPR 图谱通道（对齐 HippoRAG/Graphiti 共识）——
        # 传入后，图谱通道先用实体种子做 PPR 多跳扩散，再与 1-hop 扩展融合；
        # env TRINITY_GRAPH_PPR=off 可关闭（默认 on，失败静默降级 1-hop）。
        self._ppr_fn = ppr_fn
        # 2026-08-26（PageIndex 借鉴 Phase 1）：页树通道（可选）——
        # 传入 pagetree_fn 后参与融合（fusion/rrf/cascade）；未传入=通道关闭。
        self._pagetree_fn = pagetree_fn
        self.pagetree_weight = pagetree_weight

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
        cache_scope: str = "",
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
        cache_scope : str
            可选隔离维度（如 agent/persona/tenant 过滤的规范化串），折入缓存
            key——防止带不同过滤的查询共享同一缓存项（多租户隔离）。

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
            strategy = "rrf"  # 2026-08-17 标定: rrf 远优于 fusion

        # ── semantic result cache (default: memory backend, TTL 300s) ──
        cache = _get_configured_cache()
        cache_key = None
        if cache is not None:
            cache_key = cache.make_text_key(
                query, top_k=top_k, strategy=strategy, extra=cache_scope,
            )
            try:
                cached = cache.get(cache_key)
            except Exception:
                cached = None
            if cached is not None:
                return cached

        # ── 2026-08-25（结构进化：查询扩展 v2）────────────────────
        # TRINITY_QUERY_EXPANSION=on → 短查询 PRF 扩展（仅 BM25 通道用扩展查询，
        # vector/graph 保持原 query 精确匹配——v1 教训：扩展污染所有通道降质）。
        _bm25_query = query
        if os.environ.get("TRINITY_QUERY_EXPANSION", "off").strip().lower() in ("1", "on", "true", "yes"):
            _eq = self._expand_query(query, top_k=3)
            if _eq != query:
                _bm25_query = _eq
        # ── collect raw results from each source ───────────────────
        vector_results = self._get_vector_results(query, top_k)
        bm25_results = self._get_bm25_results(_bm25_query, top_k)
        graph_results = self._get_graph_results(query, top_k)
        # 1-hop neighbour expansion on graph results
        graph_results = self._expand_graph_neighbors(graph_results, top_k)
        proc_results = self._search_procedural(query, top_k)
        aggr_results = self._get_aggregator_results(query, top_k)
        pt_results = self._get_pagetree_results(query, top_k)

        # ── normalise scores ───────────────────────────────────────
        vector_norm = _minmax_normalise(vector_results, "score")
        bm25_norm = _minmax_normalise(bm25_results, "score", raw_key="bm25_score")
        graph_norm = _minmax_normalise(graph_results, "graph_score", raw_key="graph_score")
        proc_norm = _minmax_normalise(proc_results, "procedural_score", raw_key="procedural_score")
        aggr_norm = _minmax_normalise(aggr_results, "score", raw_key="aggregator_score")
        pt_norm = _minmax_normalise(pt_results, "score", raw_key="pagetree_score")

        # ── fuse ────────────────────────────────────────────────────
        if strategy == "fusion":
            fused = self._fusion_fuse(vector_norm, bm25_norm, graph_norm, aggr_norm, proc_norm, top_k, pt_norm)
        elif strategy == "rrf":
            fused = self._rrf_fuse(vector_norm, bm25_norm, graph_norm, aggr_norm, proc_norm, top_k, pt_norm)
        else:  # cascade
            fused = self._cascade_fuse(vector_norm, bm25_norm, graph_norm, aggr_norm, proc_norm, top_k, pt_norm)

        # ── 2026-08-17 评分校准层（引擎路径, 对标 MEMTIER/AgentPrizm）──
        # env 门控（默认 off，不改既有 96.8% R@5 基线）:
        #   TRINITY_CONFIDENCE_SCORER=on  四维置信度校准（来源权威/时效/语义…）
        #   TRINITY_IMPORTANCE_BOOST=on   importance 动态微调（存储 importance 加权）
        fused = self._apply_engine_calibration(fused, query)

        result: Dict[str, Any] = {
            "results": fused,
            "strategy": strategy,
            "query": query,
            "breakdown": {
                "vector": len(vector_results),
                "bm25": len(bm25_results),
                "graph": len(graph_results),
                "aggregator": len(aggr_results),
                "procedural": len(proc_results),
                "pagetree": len(pt_results),
                "unique_fused": len(fused),
            },
        }

        # Store the serialised result for later identical queries.
        if cache is not None and cache_key is not None:
            try:
                cache.set(cache_key, result)
            except Exception:
                logger.debug("semantic cache write failed", exc_info=True)

        return result

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

    # ── 2026-08-25（结构进化：查询扩展通道）──────────────────────────
    # PRF 式查询扩展：首轮 BM25 检索 top-k → 从结果记忆提取高频词项
    # （排除停用词/query 原词）→ 与 query 合并成扩展查询 → 后续所有
    # 通道用扩展查询检索。扩大召回覆盖面（模糊/短查询尤其有效）。
    # env TRINITY_QUERY_EXPANSION=on 启用（默认 off，向后兼容）。
    _STOPWORDS = frozenset({
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "i", "me", "my", "you", "your", "he", "she", "it", "we", "they",
        "and", "or", "but", "if", "then", "else", "when", "where", "what",
        "how", "do", "does", "did", "to", "of", "in", "on", "at", "for",
        "with", "about", "from", "by", "as", "that", "this", "these", "those",
        "have", "has", "had", "not", "no", "yes", "there", "here", "so",
    })

    def _expand_query(self, query: str, top_k: int = 3, max_terms: int = 2) -> str:
        """PRF 查询扩展 v2（2026-08-25）：仅短查询（≤3 词）启用。

        返回扩展查询（原 query + 1-2 个高频共现词）；长查询/失败原样返回。
        v1 教训：对所有查询扩展 4 词污染精确匹配（n=20 降质 -0.051）。
        v2 改进：①仅短查询（缺上下文最需扩展）；②最多 2 个词（少即是多）；
        ③扩展词只用于 BM25 召回通道（vector/graph 保持原 query）。
        """
        try:
            import re as _re
            _nq = len(_re.findall(r"[a-z0-9]+", query.lower()))
            if _nq > 3:
                return query  # 长查询已有区分度，不扩展
            q_terms = set(_re.findall(r"[a-z0-9]+", query.lower()))
            term_counts: dict = {}
            for h in self._search_fn(query, top_k)[:top_k]:
                content = (h.get("content") or "")[:2000]
                for t in _re.findall(r"[a-z0-9]+", content.lower()):
                    if t in self._STOPWORDS or t in q_terms or len(t) < 3:
                        continue
                    term_counts[t] = term_counts.get(t, 0) + 1
            if not term_counts:
                return query
            top_terms = [t for t, _ in sorted(term_counts.items(), key=lambda x: -x[1])
                         [:max_terms]]
            return query + " " + " ".join(top_terms)
        except Exception:
            return query

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
        base: List[Dict[str, Any]] = []
        try:
            base = self._graph.search_by_entity(query, top_k=top_k)
        except Exception:
            base = []
        # 2026-08-24（R8 P1-4）：PPR 图谱增强——实体种子 PPR 多跳扩散后
        # 补进图谱结果（对齐 HippoRAG/Graphiti；失败静默降级为纯 1-hop）。
        if self._ppr_fn is not None and os.environ.get("TRINITY_GRAPH_PPR", "on").lower() not in ("off", "0", "false"):
            try:
                ppr_hits = self._ppr_fn(query, top_k=top_k * 2)
                if ppr_hits:
                    seen = {r.get("memory_id") or r.get("id") for r in base}
                    for h in ppr_hits:
                        mid = h.get("memory_id") or h.get("id")
                        if mid and mid not in seen:
                            seen.add(mid)
                            base.append({
                                "memory_id": mid,
                                "content": h.get("content", ""),
                                "graph_score": float(h.get("score", 0.0)),
                                "source": "graph_ppr",
                            })
            except Exception as exc:
                logger.debug("Graph PPR channel skipped: %s", exc)
        return base

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

    def _get_pagetree_results(
        self, query: str, top_k: int,
    ) -> List[Dict[str, Any]]:
        """页树通道（2026-08-26 Phase 1）：PageIndex 式先定位页再读页内。

        pagetree_fn 签名: (query, top_k) -> [ {memory_id, score, ...} ]。
        未配置/失败 → 空列表（通道优雅降级）。
        """
        if self._pagetree_fn is None:
            return []
        try:
            raw = self._pagetree_fn(query, top_k)
            result = []
            for item in raw or []:
                mid = item.get("memory_id") or item.get("id")
                if not mid:
                    continue
                result.append({
                    "memory_id": mid,
                    "content": item.get("content", ""),
                    "pagetree_score": float(item.get("score") or 0.0),
                    "source": "pagetree",
                })
            return result
        except Exception as exc:
            logger.debug("Pagetree channel skipped: %s", exc)
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

    # ── 2026-08-17: 评分校准层（引擎路径）────────────────────────

    def _apply_engine_calibration(
        self,
        fused: List[Dict[str, Any]],
        query: str,
    ) -> List[Dict[str, Any]]:
        """RRF/融合后的评分校准（env 门控；除 STRENGTH 默认 on 外默认 off；不改变既有基线）。

        1) TRINITY_CONFIDENCE_SCORER=on → 四维置信度校准：
           hybrid_score × (0.6 + 0.4 × confidence.overall)，旧记忆/低权威降权。
        2) TRINITY_IMPORTANCE_BOOST=on → importance 动态微调：
           hybrid_score += (importance - 0.5) × 0.2（±0.1 有界）。
        3) TRINITY_STRENGTH_BOOST=on（opt-in，默认 off）→ 双强度因子（Bjork 双强度模型）：
           提取强度 = 0.5×最近访问度 + 0.5×访问频率；hybrid_score += (strength-0.5)×0.15（±0.075 有界）。
           刚被检索过/高频使用的记忆排名微升，对应"测试效应/检索强化"。
        最后按 hybrid_score 重排。
        """
        conf_on = os.environ.get("TRINITY_CONFIDENCE_SCORER", "off").strip().lower() == "on"
        imp_on = os.environ.get("TRINITY_IMPORTANCE_BOOST", "off").strip().lower() == "on"
        # 2026-08-20 评估：拟人化因子对机器检索价值有限且可能引入流行度偏差 ->
        # 默认改 off（opt-in，保持基线纯净）；需要时设 TRINITY_STRENGTH_BOOST=on 启用。
        strength_on = os.environ.get("TRINITY_STRENGTH_BOOST", "off").strip().lower() == "on"
        if (not conf_on and not imp_on and not strength_on) or not fused:
            return fused

        # 跨结果 min-max 归一化（置信度语义匹配维度用）
        scores = [float(f.get("hybrid_score") or 0.0) for f in fused]
        lo, hi = min(scores), max(scores)
        span = (hi - lo) or 1.0

        if conf_on:
            try:
                from trinity.modules.second_brain.confidence_scored_retrieval import (
                    ConfidenceScorer,
                    SourceType,
                    ValidityCategory,
                )

                scorer = ConfidenceScorer()

                def _vc(cat: str):
                    c = (cat or "").lower()
                    if "pref" in c:
                        return ValidityCategory.PERSONAL_PREFERENCE
                    if "news" in c:
                        return ValidityCategory.NEWS
                    if "financ" in c or "market" in c:
                        return ValidityCategory.FINANCIAL
                    if "regul" in c or "policy" in c:
                        return ValidityCategory.REGULATORY
                    return ValidityCategory.GENERAL_KNOWLEDGE

                for f in fused:
                    created = f.get("created_at") or 0.0
                    if isinstance(created, str):
                        try:
                            from datetime import datetime
                            created = datetime.fromisoformat(
                                created.replace("Z", "+00:00")
                            ).timestamp()
                        except Exception:
                            created = 0.0
                    acc = int(f.get("access_count") or 0)
                    conf = scorer.score(
                        source_type=(
                            SourceType.USER_CONFIRMED if acc > 0
                            else SourceType.LLM_GENERATED
                        ),
                        citation_count=acc,
                        citation_agreement=min(1.0, 0.5 + 0.05 * acc),
                        created_at=float(created or time.time()),
                        validity_category=_vc(str(f.get("category") or "")),
                        semantic_similarity=(float(f.get("hybrid_score") or 0.0) - lo) / span,
                    )
                    f["hybrid_score"] = float(f.get("hybrid_score") or 0.0) * (0.6 + 0.4 * conf.overall)
            except Exception as exc:
                logger.debug("Engine confidence calibration skipped: %s", exc)

        if imp_on:
            try:
                for f in fused:
                    imp = float(f.get("importance") or 0.5)
                    f["hybrid_score"] = min(
                        1.0, max(0.0, float(f.get("hybrid_score") or 0.0) + (imp - 0.5) * 0.2)
                    )
            except Exception as exc:
                logger.debug("Engine importance boost skipped: %s", exc)

        if strength_on:
            try:
                from datetime import datetime as _dt, timezone as _tz
                _now = _dt.now(_tz.utc)
                for f in fused:
                    acc_raw = f.get("access_count")
                    acc = int(acc_raw or 0)
                    last = f.get("last_accessed_at")
                    if not last and not acc_raw:
                        strength = 0.5  # 无访问数据 -> 中性，不改变基线
                    else:
                        recency = 0.5
                        if last:
                            try:
                                if isinstance(last, str):
                                    last = _dt.fromisoformat(str(last).replace("Z", "+00:00"))
                                if last.tzinfo is None:
                                    last = last.replace(tzinfo=_tz.utc)
                                days = max(0.0, (_now - last).total_seconds() / 86400.0)
                                recency = max(0.0, min(1.0, 1.0 - days / 30.0))
                            except Exception:
                                recency = 0.5
                        freq = max(0.0, min(1.0, acc / 20.0))
                        strength = 0.5 * recency + 0.5 * freq  # 提取强度 [0,1]
                    f["hybrid_score"] = min(
                        1.0, max(0.0, float(f.get("hybrid_score") or 0.0) + (strength - 0.5) * 0.15)
                    )
            except Exception as exc:
                logger.debug("Engine strength calibration skipped: %s", exc)

        fused.sort(key=lambda x: float(x.get("hybrid_score") or 0.0), reverse=True)
        return fused

    # ── fusion strategies ───────────────────────────────────────────

    def _fusion_fuse(
        self,
        vector: List[Dict],
        bm25: List[Dict],
        graph: List[Dict],
        aggregator: List[Dict],
        procedural: List[Dict],
        top_k: int,
        pagetree: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        wv, wb, wg, wa, wp = (
            self.vector_weight,
            self.bm25_weight,
            self.graph_weight,
            self.aggregator_weight if self._aggregator_fn is not None else 0.0,
            self.procedural_weight if self._procedural_store is not None else 0.0,
        )
        wpt = self.pagetree_weight if self._pagetree_fn is not None else 0.0
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

        for item in pagetree or []:
            mid = item["memory_id"]
            pts = item.get("pagetree_score", 0)
            if mid in merged:
                merged[mid]["pagetree_score"] = pts
                merged[mid]["hybrid_score"] += pts * wpt
            else:
                entry = _init_entry(item, "pagetree", pts * wpt)
                entry["pagetree_score"] = pts
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
        pagetree: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        def _rank(source_list, score_key):
            ranked = sorted(source_list, key=lambda x: x.get(score_key, 0), reverse=True)
            return {item["memory_id"]: r for r, item in enumerate(ranked)}

        v_rank = _rank(vector, "score")
        b_rank = _rank(bm25, "score")
        g_rank = _rank(graph, "graph_score")
        a_rank = _rank(aggregator, "aggregator_score")
        p_rank = _rank(procedural, "procedural_score")
        pt_rank = _rank(pagetree or [], "pagetree_score")

        all_ids = set(v_rank) | set(b_rank) | set(g_rank) | set(a_rank) | set(p_rank) | set(pt_rank)
        merged: Dict[str, Dict] = {}

        wa = 1.0 if self._aggregator_fn is not None else 0.0
        wp = 1.0 if self._procedural_store is not None else 0.0
        wpt = 1.0 if self._pagetree_fn is not None else 0.0

        for mid in all_ids:
            rrf = (
                1.0 / (self.rrf_k + v_rank.get(mid, len(v_rank)))
                + 1.0 / (self.rrf_k + b_rank.get(mid, len(b_rank)))
                + 1.0 / (self.rrf_k + g_rank.get(mid, len(g_rank)))
                + wa * 1.0 / (self.rrf_k + a_rank.get(mid, len(a_rank)))
                + wp * 1.0 / (self.rrf_k + p_rank.get(mid, len(p_rank)))
                + wpt * 1.0 / (self.rrf_k + pt_rank.get(mid, len(pt_rank)))
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
            if mid in pt_rank:
                entry["pagetree_score"] = next(
                    (it.get("pagetree_score", 0) for it in (pagetree or []) if it["memory_id"] == mid), 0)
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
        pagetree: Optional[List[Dict]] = None,
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
        for item in pagetree or []:
            mid = item["memory_id"]
            pts = item.get("pagetree_score", 0)
            if mid in merged:
                merged[mid]["pagetree_score"] = pts
                merged[mid]["hybrid_score"] += pts * 0.15
            elif mid in stage1_ids:
                merged[mid] = {
                    "memory_id": mid,
                    "pagetree_score": pts,
                    "hybrid_score": pts * 0.15,
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
    # 2026-08-15（压测修复）：score 可能为 None（并发错位/通道缺分），
    # 统一兜底为 0，避免 min()/max() 抛 TypeError。
    scores = [it.get(score_key) or 0 for it in items]
    mn, mx = min(scores), max(scores)
    rng = mx - mn
    out_key = raw_key or score_key
    for it in items:
        raw = it.get(score_key) or 0
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
    elif source == "pagetree":
        entry["pagetree_score"] = item.get("pagetree_score", 0)
        for key in item:
            if key not in entry:
                entry[key] = item[key]
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

