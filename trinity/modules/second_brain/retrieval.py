"""
# status: frozen (2026-09 EXECUTION 163)
Second Brain Retrieval Pipeline
================================
Multi-stage retrieval: BM25 sparse → FAISS dense → Cross-Encoder reranking.

This pipeline replaces the original hash-based pseudo-semantic retrieval
with true multi-stage hybrid search.

Pipeline:
  Stage 1: Query Understanding (expansion, intent)
  Stage 2: SPLADE Sparse (learned sparse retrieval)
  Stage 2b: ColBERTv2 Late Interaction (token-level 3rd channel)
  Stage 2c: HyDE Hypothesis Generation (LLM-augmented query)
  Stage 2d: ContextExpander query decomposition
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
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from trinity.vector_index.sparse import BM25SparseRetriever, fuse_scores_sparse_dense
from trinity.vector_index.splade import SPLADESparseRetriever
from trinity.vector_index.reranker import CrossEncoderReranker
from trinity.vector_index.index import VectorIndex, create_index
from trinity.vector_index.hyde import HydeRetriever
from trinity.vector_index.colbert import ColBERTRetriever
from trinity.core.crag import CorrectiveRAG
from trinity.modules.open_domain.reasoner import ContextExpander
from trinity.modules.multimodal.multimodal_memory import MultiModalMemory, ModalityType

logger = logging.getLogger(__name__)


class TrinityRetrievalPipeline:
    """Multi-stage retrieval: BM25/SPLADE sparse → FAISS dense → RRF fusion → Cross-Encoder reranking."""

    def __init__(self, dim=1024, enable_sparse=True, enable_splade=True,
                 enable_colbert=False, enable_hyde=False, enable_crag=False,
                 enable_multimodal=False, enable_reranker=True,
                 enable_query_expansion=True, approx_top_k=100, final_top_k=10,
                 reranker_model="fast", fusion_alpha=0.3, multimodal_modality="auto"):
        self._dim = dim; self._approx_top_k = approx_top_k
        self._final_top_k = final_top_k; self._fusion_alpha = fusion_alpha
        self._enable_query_expansion = enable_query_expansion
        self._dense = create_index(backend="auto", dim=dim, metric="cosine", index_type="hnsw")
        self._sparse = (
            SPLADESparseRetriever() if (enable_sparse and enable_splade)
            else BM25SparseRetriever() if enable_sparse else None)
        self._colbert = None
        if enable_colbert:
            logger.info("Initializing ColBERTv2 retriever"); self._colbert = ColBERTRetriever()
        self._multimodal = None; self._multimodal_modality = multimodal_modality
        if enable_multimodal:
            logger.info("Initializing MultiModalMemory"); self._multimodal = MultiModalMemory()
        self._hyde = None
        if enable_hyde:
            logger.info("Initializing HyDE retriever")
        self._crag = None
        if enable_crag:
            logger.info("Initializing Corrective RAG")
            self._crag = CorrectiveRAG(
                re_retrieve_fn=lambda q, p: self._aggregator._crag_re_retrieve(q, p))
        self._reranker = (
            CrossEncoderReranker(model_name=reranker_model) if enable_reranker else None)
        # Lazy cache init — defer import to break static cycle with core.cache
        self._cache = None
        self._cache_configured = False
        if self._reranker:
            threading.Thread(target=self._reranker._load_model, daemon=True).start()
        self._stats = {"total_searches": 0, "avg_results": 0, "stage_sparse": 0,
                       "stage_dense": 0, "stage_fusion": 0, "stage_reranker": 0,
                       "stage_multimodal": 0, "stage_cache_hit": 0, "stage_cache_miss": 0}
        self._metadata_store: Dict[str, Dict[str, Any]] = {}
        self._initialized = False
        self._router = _QueryRouter(self)
        self._aggregator = _FusionAggregator(self)
        self._indexer = _IndexManager(self)

    def index_corpus(self, texts, ids, vectors=None, metadatas=None):
        """Index a corpus of documents. Returns self for chaining."""
        return self._indexer.index_corpus(texts, ids, vectors, metadatas)

    def add(self, doc_id, text, vector, metadata=None):
        """Add a single document to the index."""
        self._indexer.add(doc_id, text, vector, metadata)

    def index_multimodal(self, file_paths, texts=None, metadatas=None):
        """Index multi-modal content into MultiModalMemory. Returns self for chaining."""
        return self._indexer.index_multimodal(file_paths, texts, metadatas)

    def _init_cache(self):
        """Lazily initialize the semantic cache (breaks static cycle with core.cache)."""
        if not self._cache_configured:
            from trinity.core.cache import configure_cache, get_cache
            configure_cache(backend="redis", redis_url="redis://localhost:6379/0",
                            max_size=5000, default_ttl=300)
            self._cache = get_cache()
            self._cache_configured = True

    @property
    def cache(self):
        """Lazy accessor for the semantic cache singleton."""
        self._init_cache()
        return self._cache

    def search(self, query, query_vector=None, top_k=10, use_reranker=True):
        """Full multi-stage retrieval pipeline."""
        self._stats["total_searches"] += 1; start = time.perf_counter()
        cached, cache_key, queries = self._router._preprocess_query(query, query_vector, top_k)
        if cached is not None:
            return cached
        all_fused: Dict[str, Dict[str, Any]] = {}
        self._router._multi_channel_search(queries, query_vector, all_fused)
        results = self._aggregator._merge_and_rerank(query, all_fused, top_k, use_reranker)
        return self._aggregator._postprocess_results(query, results, top_k, cache_key, start)

    def statistics(self):
        """Pipeline usage statistics."""
        s = self._stats; h = s["stage_cache_hit"]; m = s["stage_cache_miss"]
        return {
            "searches": s["total_searches"], "avg_results": round(s["avg_results"], 1),
            "stage_sparse_runs": s["stage_sparse"], "stage_dense_runs": s["stage_dense"],
            "stage_fusion_runs": s["stage_fusion"], "stage_reranker_runs": s["stage_reranker"],
            "stage_multimodal_runs": s["stage_multimodal"],
            "cache_hits": h, "cache_misses": m,
            "cache_hit_rate_pct": round(h / max(h + m, 1) * 100, 1),
            "dense_index": self._dense.statistics() if self._dense else {},
            "sparse_index": self._sparse.statistics() if self._sparse else {},
            "reranker": self._reranker.statistics() if self._reranker else {},
            "cache": self.cache.statistics(),
            "config": {"dim": self._dim, "approx_top_k": self._approx_top_k,
                       "final_top_k": self._final_top_k,
                       "multimodal_enabled": self._multimodal is not None,
                       "multimodal_modality": self._multimodal_modality,
                       "fusion_alpha": self._fusion_alpha,
                       "query_expansion": self._enable_query_expansion},
        }


class _IndexManager:
    """Corpus indexing: dense, sparse, and multimodal index management."""

    def __init__(self, parent: TrinityRetrievalPipeline):
        self._p = parent

    def index_corpus(self, texts, ids, vectors=None, metadatas=None):
        """Index a corpus of documents. Returns self._p for chaining."""
        p = self._p; start = time.perf_counter()
        if vectors is not None:
            for i, (doc_id, vec) in enumerate(zip(ids, vectors)):
                p._dense.add(doc_id, vec, metadatas[i] if metadatas else {})
        if p._sparse:
            p._sparse.index_corpus(texts, ids)
        for i, doc_id in enumerate(ids):
            meta = dict(metadatas[i]) if metadatas else {}
            meta["text"] = texts[i]; p._metadata_store[doc_id] = meta
        elapsed = time.perf_counter() - start
        logger.info("Indexed %d documents (dense=%s, sparse=%s) in %.3fs",
                    len(ids), vectors is not None, p._sparse is not None, elapsed)
        p._initialized = True; return p

    def add(self, doc_id, text, vector, metadata=None):
        """Add a single document to the index."""
        p = self._p
        if p._dense:
            p._dense.add(doc_id, vector, metadata or {})
        if p._sparse:
            p._sparse.add_documents([text], [doc_id])
        meta = dict(metadata or {}); meta["text"] = text
        p._metadata_store[doc_id] = meta

    def index_multimodal(self, file_paths, texts=None, metadatas=None):
        """Index multi-modal content into MultiModalMemory. Returns self._p for chaining."""
        p = self._p
        if not p._multimodal:
            logger.warning("MultiModalMemory not enabled, skipping index"); return p
        try:
            for i, fp in enumerate(file_paths):
                text = texts[i] if texts and i < len(texts) else fp
                meta = metadatas[i] if metadatas and i < len(metadatas) else {}
                p._multimodal.store(text=text, file_path=fp, metadata=meta)
            logger.info("Indexed %d multi-modal items", len(file_paths))
        except Exception as e:
            logger.warning("Multi-modal indexing failed: %s", e)
        return p


class _QueryRouter:
    """Query routing: expansion, preprocessing, and multi-channel search."""

    def __init__(self, parent: TrinityRetrievalPipeline):
        self._p = parent

    def _expand_query(self, query: str) -> List[str]:
        """Generate query variants for better recall."""
        queries = [query]
        try:
            expander = ContextExpander()
            decomposed = expander.expand_query(query)
            if isinstance(decomposed, list):
                for dq in decomposed:
                    if isinstance(dq, str) and len(dq) > 5 and dq not in queries:
                        queries.append(dq)
        except Exception as e:
            logger.debug("ContextExpander unavailable: %s", e)
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
        if len(filtered) >= 2:
            key_terms = [t for t in filtered if len(t) > 2]
            if key_terms:
                queries.append(" ".join(key_terms) + " " + " ".join(key_terms))
        return list(set(queries))

    def _preprocess_query(self, query: str, query_vector, top_k: int):
        """Check semantic cache and expand queries. Returns (cached, cache_key, queries)."""
        p = self._p
        p._stats["total_searches"] += 1
        start = time.perf_counter()
        cache_key = p.cache.make_key(query_vector, query, top_k)
        cached = p.cache.get(cache_key)
        if cached is not None:
            p._stats["stage_cache_hit"] += 1
            elapsed = time.perf_counter() - start
            logger.debug("Cache HIT for '%s' (%d results, %.3fms)",
                         query[:40], len(cached), elapsed * 1000)
            return cached, cache_key, None
        p._stats["stage_cache_miss"] += 1
        if p._enable_query_expansion:
            queries = self._expand_query(query)
        else:
            queries = [query]
        return None, cache_key, queries

    def _search_all_channels(self, expanded_query: str, query_vector) -> tuple:
        """Run sparse/ColBERT/MultiModal/Dense/HyDE for one expanded query."""
        p = self._p

        sparse_results: List[Dict[str, Any]] = []
        if p._sparse:
            p._stats["stage_sparse"] += 1
            sparse_results = p._sparse.search(expanded_query, top_k=p._approx_top_k)
            for r in sparse_results:
                r["sparse_query"] = expanded_query

        colbert_results: List[Dict[str, Any]] = []
        if p._colbert and sparse_results:
            try:
                colbert_results = p._colbert.search(expanded_query, top_k=p._approx_top_k)
                for r in colbert_results:
                    r["colbert_query"] = expanded_query
            except Exception as e:
                logger.warning("ColBERT search failed: %s", e)

        multimodal_results: List[Dict[str, Any]] = []
        if p._multimodal:
            try:
                p._stats["stage_multimodal"] += 1
                mm_raw = p._multimodal.search(
                    query=expanded_query, modality=p._multimodal_modality,
                    top_k=p._approx_top_k // 2)
                for mr in mm_raw:
                    multimodal_results.append({
                        "id": mr.get("id", f'mm_{hash(mr.get("text", ""))}'),
                        "score": float(mr.get("score", 0.5)),
                        "text": mr.get("text", ""),
                        "multimodal_source": mr.get("modality", "unknown"),
                        "file_path": mr.get("file_path", ""),
                        "multimodal_query": expanded_query,
                    })
            except Exception as e:
                logger.warning("MultiModal search failed: %s", e)

        dense_results: List[Dict[str, Any]] = []
        if query_vector is not None and p._dense:
            p._stats["stage_dense"] += 1
            try:
                dense_raw = p._dense.search(query_vector, top_k=p._approx_top_k)
                for sr in dense_raw:
                    meta = sr.metadata or {}
                    dense_results.append({
                        "id": sr.id, "score": float(sr.score),
                        "text": meta.get("text", meta.get("content", "")),
                        "dense_query": expanded_query, **meta,
                    })
            except Exception as e:
                logger.warning("Dense search failed: %s", e)

        if p._hyde and dense_results and len(dense_results) > 5:
            try:
                hyde_extra = p._hyde.search(
                    query=expanded_query, query_vector=query_vector,
                    doc_ids=[r["id"] for r in dense_results[:50]])
                hyde_map = {r["id"]: r.get("score", 0.0) for r in hyde_extra}
                for r in dense_results:
                    hs = hyde_map.get(r["id"], 0.0)
                    if hs > 0:
                        r["score"] = 0.7 * r.get("score", 0.0) + 0.3 * hs
                        r["hyde_boost"] = hs
            except Exception as e:
                logger.warning("HyDE augmentation failed: %s", e)

        return sparse_results, colbert_results, multimodal_results, dense_results

    def _multi_channel_search(
        self, queries: list, query_vector,
        all_fused: Dict[str, Dict[str, Any]],
    ) -> None:
        """Iterate expanded queries, run all channels, fuse and merge."""
        p = self._p
        for expanded_query in queries:
            sparse, colbert, multimodal, dense = self._search_all_channels(
                expanded_query, query_vector)
            p._aggregator._fuse_and_merge(sparse, colbert, multimodal, dense, all_fused)


class _FusionAggregator:
    """Fusion, reranking, boosting, KG augmentation, and post-processing."""

    def __init__(self, parent: TrinityRetrievalPipeline):
        self._p = parent

    def _crag_re_retrieve(self, query: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Re-retrieval helper for CRAG with adjustable parameters."""
        p = self._p
        alpha = params.get("alpha", p._fusion_alpha)
        top_k_val = params.get("top_k", p._approx_top_k)
        old_alpha = p._fusion_alpha
        p._fusion_alpha = alpha
        try:
            result = p.search(query, top_k=top_k_val)
        finally:
            p._fusion_alpha = old_alpha
        return result

    def _fuse_and_merge(
        self, sparse_results: list, colbert_results: list,
        multimodal_results: list, dense_results: list,
        all_fused: Dict[str, Dict[str, Any]],
    ) -> None:
        """4-way RRF fusion of channel results and merge into all_fused."""
        p = self._p
        if sparse_results and dense_results:
            p._stats["stage_fusion"] += 1
            fused = fuse_scores_sparse_dense(
                sparse_results, dense_results, alpha=p._fusion_alpha, top_k=p._approx_top_k)
            if colbert_results:
                fused = fuse_scores_sparse_dense(
                    fused, colbert_results, alpha=0.5, top_k=p._approx_top_k)
            if multimodal_results:
                fused = fuse_scores_sparse_dense(
                    fused, multimodal_results, alpha=0.3, top_k=p._approx_top_k)
        elif sparse_results:
            fused = sparse_results
            if colbert_results:
                fused = fuse_scores_sparse_dense(
                    fused, colbert_results, alpha=0.5, top_k=p._approx_top_k)
            if multimodal_results:
                fused = fuse_scores_sparse_dense(
                    fused, multimodal_results, alpha=0.3, top_k=p._approx_top_k)
        elif dense_results:
            fused = dense_results
            if colbert_results:
                fused = fuse_scores_sparse_dense(
                    fused, colbert_results, alpha=0.5, top_k=p._approx_top_k)
            if multimodal_results:
                fused = fuse_scores_sparse_dense(
                    fused, multimodal_results, alpha=0.3, top_k=p._approx_top_k)
        elif colbert_results:
            fused = colbert_results
            if multimodal_results:
                fused = fuse_scores_sparse_dense(
                    fused, multimodal_results, alpha=0.3, top_k=p._approx_top_k)
        elif multimodal_results:
            fused = multimodal_results
        else:
            return

        for r in fused:
            rid = r["id"]
            if rid not in all_fused or r.get("rrf_score", 0) > all_fused[rid].get("rrf_score", 0):
                all_fused[rid] = r

    def _merge_and_rerank(
        self, query: str, all_fused: dict, top_k: int, use_reranker: bool,
    ):
        """Sort fused results, apply cross-encoder reranker and CRAG correction."""
        p = self._p
        results = sorted(all_fused.values(),
                         key=lambda x: x.get("rrf_score", x.get("score", 0)),
                         reverse=True)

        if use_reranker and p._reranker and len(results) > 0:
            try:
                p._stats["stage_reranker"] += 1
                results = p._reranker.rerank(
                    query, results, top_k=max(top_k, p._final_top_k), text_key="text")
            except Exception as e:
                logger.warning("Reranker failed, using fusion results: %s", e)

        if p._crag and results:
            try:
                results, confidence = p._crag.evaluate(results)
                if confidence in ("low", "medium"):
                    logger.info("CRAG: %s confidence, applying correction", confidence)
                    results = p._crag.correct(
                        query, results,
                        previous_params={"alpha": p._fusion_alpha, "top_k": p._approx_top_k})
            except Exception as e:
                logger.warning("CRAG evaluation failed: %s", e)

        return results

    def _postprocess_results(
        self, query: str, results: list, top_k: int,
        cache_key, start: float,
    ):
        """Apply boosts, KG augmentation, cache, stats and logging."""
        p = self._p
        results = self._apply_boosts(query, results)
        try:
            results = self._kg_augment(query, results, top_k=top_k)
        except Exception as e:
            logger.debug("KG stage failed: %s", e)
        final = results[:top_k]
        p.cache.set(cache_key, final, ttl=300)
        elapsed = time.perf_counter() - start
        p._stats["avg_results"] = (
            (p._stats["avg_results"] * (p._stats["total_searches"] - 1) + len(final))
            / p._stats["total_searches"]
        )
        logger.info("Search '%s': %d results in %.3fs", query[:50], len(final), elapsed)
        return final

    def _apply_boosts(
        self, query: str, results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Apply temporal decay and importance boosting to results."""
        import time as time_module

        now = time_module.time()
        for r in results:
            importance = r.get("importance", 0.5)
            if isinstance(importance, str):
                try:
                    importance = float(importance)
                except (ValueError, TypeError):
                    importance = 0.5
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
            temporal_decay = max(0.1, 1.0 - (age_hours / (24 * 30)))
            base_score = r.get("score", r.get("rrf_score", 0.5))
            r["boosted_score"] = base_score * (1 + 0.3 * importance) * temporal_decay
            r["temporal_decay"] = temporal_decay
            r["importance_boost"] = importance
        results.sort(key=lambda x: x.get("boosted_score", 0), reverse=True)
        return results

    def _kg_augment(
        self, query: str, results: List[Dict[str, Any]], top_k: int = 20,
    ) -> List[Dict[str, Any]]:
        """Augment results with Knowledge Graph triples."""
        try:
            from trinity.kgraph.graph import KnowledgeGraph
            kg = KnowledgeGraph()
            kg_hits = kg.search(query, top_k=5)
            if not kg_hits:
                for word in query.split():
                    if len(word) > 1:
                        word_hits = kg.search(word, top_k=3)
                        kg_hits.extend(word_hits)
            if kg_hits:
                seen = set()
                for hit in kg_hits:
                    eid = hit.get("entity", {}).get("id", "")
                    if eid and eid not in seen:
                        seen.add(eid)
                        relations = kg.query_relations(eid, max_depth=2)
                        if relations:
                            triple_text = " | ".join(
                                f"{r['subject']} --{r['predicate']}--> {r['object']}"
                                for r in relations[:10])
                            results.append({
                                "id": f"kg_{eid}",
                                "score": hit.get("score", 0.5),
                                "text": triple_text,
                                "source": "knowledge_graph",
                                "kg_entity": eid,
                            })
        except Exception as e:
            logger.debug("KG augmentation failed: %s", e)
        return results


# ── Legacy compat layer ───────────────────────────────────────────────

class ThreePhaseTriSignalRetrieval:
    """Backward-compatible wrapper for the new retrieval pipeline."""

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
