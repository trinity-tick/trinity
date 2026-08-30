"""
Cross-Encoder Reranker
======================
Reranks retrieval candidates using a Cross-Encoder model for higher precision.

Industry reference:
  - Cohere Rerank v3
  - BAAI BGE-Reranker-v2-m3
  - Cross-Encoder/ms-marco-MiniLM-L-6-v2

Usage:
    reranker = CrossEncoderReranker()
    results = reranker.rerank(query, candidates, top_k=10)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Default ranking model mapping (quality vs speed)
MODEL_REGISTRY = {
    "fast": "cross-encoder/ms-marco-MiniLM-L-6-v2",       # fastest, good enough
    "balanced": "cross-encoder/ms-marco-MiniLM-L-12-v2",  # good balance
    "accurate": "BAAI/bge-reranker-v2-m3",                 # best quality, larger
    # 2026-09: chinese 改指 ms-marco-MiniLM-L-6-v2——v2-m3(~2.2GB)受限网络
    # 下载不可靠（incomplete 分片重试），且 bge-small-zh 是 embedding 非 CE
    # （分类头 MISSING 随机初始化）。ms-marco 是真正 CrossEncoder、88MB 缓存完整。
    "chinese": "cross-encoder/ms-marco-MiniLM-L-6-v2",    # CE reranker (cached)
}
DEFAULT_MODEL = "balanced"


class CrossEncoderReranker:
    """Cross-Encoder based reranker for improving retrieval precision.

    Unlike bi-encoders (which encode query & doc separately), Cross-Encoders
    jointly encode the (query, document) pair, producing much more accurate
    relevance scores at the cost of slower speed.

    Strategy: Use bi-encoder for initial top-K retrieval (~50-100),
    then rerank with Cross-Encoder for final top-k (~5-10).
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        device: Optional[str] = None,
        batch_size: int = 32,
        max_length: int = 512,
        use_fp16: bool = False,
    ):
        self._model_name = MODEL_REGISTRY.get(model_name) if model_name else MODEL_REGISTRY[DEFAULT_MODEL]
        self._device = device
        self._batch_size = batch_size
        self._max_length = max_length
        self._use_fp16 = use_fp16
        self._model = None
        self._model_loaded = False
        self._model_failed = False
        self._total_reranks = 0
        self._total_rerank_time = 0.0

    def _load_model(self):
        """Lazy-load the Cross-Encoder model.

        Failures are sticky: after a failed load attempt the reranker
        permanently degrades to identity (no-op) instead of retrying the
        import on every search (avoids repeated import failures + log spam
        when sentence-transformers is not installed).
        """
        if self._model_loaded or self._model_failed:
            return
        try:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder(
                self._model_name,
                device=self._device,
                max_length=self._max_length,
            )
            self._model_loaded = True
            logger.info(
                "Loaded Cross-Encoder: %s (device=%s)",
                self._model_name, self._device or "auto"
            )
        except Exception as e:
            self._model_failed = True
            logger.warning(
                "Failed to load Cross-Encoder '%s': %s. "
                "Falling back to identity reranking (no-op).",
                self._model_name, e
            )

    def _ollama_rerank(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        top_k: int,
        score_key: str,
        text_key: str,
    ) -> List[Dict[str, Any]]:
        """Ollama bi-encoder 降级重排（CrossEncoder 不可用时）。

        用本地嵌入模型（nomic-embed-text 768d 或 bge-m3 1024d）对
        (query, candidate) 分别编码，余弦相似度排序。质量低于真 CE 但
        优于 no-op；模型可用性由 ollama /api/embed 决定。
        """
        import requests as _req
        texts = [cand.get(text_key) or cand.get("content") or cand.get(id_key, "")
                 for cand in candidates]
        if not texts:
            return candidates[:top_k]
        model = "nomic-embed-text:v1.5"
        resp = _req.post("http://127.0.0.1:11434/api/embed",
                         json={"model": model, "input": [query] + texts},
                         timeout=120)
        resp.raise_for_status()
        vecs = resp.json().get("embeddings")
        if not vecs or len(vecs) != 1 + len(texts):
            raise RuntimeError("ollama embed count mismatch")
        import numpy as _np
        qv = _np.asarray(vecs[0], dtype=_np.float32)
        def _norm(v):
            v = _np.asarray(v, dtype=_np.float32)
            n = _np.linalg.norm(v)
            return v / n if n > 1e-8 else v
        qv = _norm(qv)
        scored = []
        for cand, vec in zip(candidates, vecs[1:]):
            sim = float(_np.dot(qv, _norm(vec)))
            cand[score_key] = sim
            scored.append(cand)
        scored.sort(key=lambda x: x[score_key], reverse=True)
        self._total_reranks += 1
        return scored[:top_k]

    def rerank(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        top_k: int = 10,
        score_key: str = "score",
        text_key: str = "text",
        id_key: str = "id",
    ) -> List[Dict[str, Any]]:
        """Rerank a list of candidate documents by relevance to the query.

        Args:
            query: The search query string.
            candidates: List of candidate dicts, each must contain text_key.
            top_k: Number of results to return after reranking.
            score_key: Key to store the reranker score in the result dict.
            text_key: Key for the document text in each candidate dict.
            id_key: Key for the document ID.

        Returns:
            Reranked list of candidate dicts (top_k items).
        """
        self._load_model()
        if not candidates:
            return []
        if not self._model_loaded:
            # 2026-09 降级：CrossEncoder 不可用（网络/版本兼容）时用
            # Ollama bi-encoder 余弦相似度重排（nomic-embed-text/bge-m3 本地，
            # 零下载零编译）。仍不可用才返回原序（no-op）。
            try:
                return self._ollama_rerank(query, candidates, top_k, score_key, text_key)
            except Exception:
                return candidates[:top_k]

        start = time.perf_counter()

        # Prepare (query, document) pairs
        texts = [
            cand.get(text_key) or cand.get("content") or cand.get(id_key, "")
            for cand in candidates
        ]
        pairs = [(query, text) for text in texts]

        # Score in batches using the Cross-Encoder
        scores = self._model.predict(
            pairs,
            batch_size=self._batch_size,
            show_progress_bar=False,
        )

        elapsed = time.perf_counter() - start
        self._total_reranks += 1
        self._total_rerank_time += elapsed

        # Update candidates with reranker scores
        reranked = []
        for cand, score in zip(candidates, scores):
            score_val = float(score) if hasattr(score, 'item') else float(score)
            cand[score_key] = score_val
            reranked.append(cand)

        # Sort by reranker score descending
        reranked.sort(key=lambda x: x[score_key], reverse=True)

        logger.debug(
            "Reranked %d candidates -> top-%d in %.3fs",
            len(candidates), top_k, elapsed
        )

        return reranked[:top_k]

    def batch_rerank(
        self,
        queries: List[str],
        candidates_list: List[List[Dict[str, Any]]],
        top_k: int = 10,
    ) -> List[List[Dict[str, Any]]]:
        """Rerank multiple query results in batch (maximizes GPU utilization).

        Args:
            queries: List of query strings.
            candidates_list: List of candidate lists (one per query).
            top_k: Number of results to return per query after reranking.

        Returns:
            List of reranked candidate lists.
        """
        self._load_model()
        if not self._model_loaded:
            return [c[:top_k] for c in candidates_list]

        start = time.perf_counter()

        # Build all pairs across all queries
        all_pairs: List[Tuple[str, str]] = []
        offsets: List[int] = [0]
        for query, candidates in zip(queries, candidates_list):
            for cand in candidates:
                text = cand.get("text") or cand.get("content") or cand.get("id", "")
                all_pairs.append((query, text))
            offsets.append(offsets[-1] + len(candidates))

        # Score all pairs at once
        scores = self._model.predict(
            all_pairs,
            batch_size=self._batch_size,
            show_progress_bar=False,
        )

        # Split scores back per query
        results = []
        for i, (query, candidates) in enumerate(zip(queries, candidates_list)):
            start_idx = offsets[i]
            end_idx = offsets[i + 1]
            query_scores = scores[start_idx:end_idx]

            reranked = []
            for cand, score in zip(candidates, query_scores):
                score_val = float(score) if hasattr(score, 'item') else float(score)
                cand["rerank_score"] = score_val
                reranked.append(cand)

            reranked.sort(key=lambda x: x["rerank_score"], reverse=True)
            results.append(reranked[:top_k])

        elapsed = time.perf_counter() - start
        self._total_reranks += len(queries)
        self._total_rerank_time += elapsed

        return results

    def statistics(self) -> Dict[str, Any]:
        """Return reranker usage statistics."""
        avg_time = (
            self._total_rerank_time / self._total_reranks
            if self._total_reranks > 0 else 0.0
        )
        return {
            "model": self._model_name,
            "loaded": self._model_loaded,
            "total_reranks": self._total_reranks,
            "total_rerank_time_s": round(self._total_rerank_time, 4),
            "avg_rerank_time_s": round(avg_time, 6),
            "batch_size": self._batch_size,
        }


# Convenience alias
Reranker = CrossEncoderReranker


# ─── Self-test ──────────────────────────────────────────────────────────

def self_test():
    """Quick self-test for the reranker."""
    import numpy as np

    print("=" * 60)
    print("  Cross-Encoder Reranker - Self Test")
    print("=" * 60)

    reranker = CrossEncoderReranker(model_name="fast")

    query = "How does machine learning work?"
    candidates = [
        {"id": "doc1", "text": "Machine learning is a subset of artificial intelligence."},
        {"id": "doc2", "text": "The weather today is sunny with a chance of rain."},
        {"id": "doc3", "text": "Deep neural networks are a key ML technique."},
        {"id": "doc4", "text": "I like to cook pasta with tomato sauce."},
        {"id": "doc5", "text": "Supervised learning uses labeled training data."},
    ]

    print(f"\n  Query: {query}")
    print(f"  Candidates: {len(candidates)}")

    results = reranker.rerank(query, candidates, top_k=3)

    print(f"\n  Top-3 after reranking:")
    for r in results:
        print(f"    {r['id']}: score={r['score']:.4f}  text={r['text'][:50]}...")

    stats = reranker.statistics()
    print(f"\n  Stats: {stats}")

    print("\n" + "=" * 60)
    print("  Self-test complete")
    print("=" * 60)


if __name__ == "__main__":
    self_test()
