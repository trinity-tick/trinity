"""
BM25 Sparse Retriever
=====================
BM25-based sparse retrieval for keyword-style matching.

BM25 (Best Matching 25) is a bag-of-words retrieval function that ranks
documents by their term frequency, inverse document frequency, and
document length normalization.

Combined with dense vector retrieval (FAISS), this forms a hybrid search
pipeline that captures both semantic meaning (dense) and exact keyword
matches (sparse).

Industry reference:
  - Elasticsearch BM25 (default scoring)
  - Weaviate hybrid_search (BM25 + vector)
  - rank_bm25 Python library

Usage:
    sparse = BM25SparseRetriever()
    sparse.index_corpus(texts, ids)
    results = sparse.search(query, top_k=20)
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class BM25SparseRetriever:
    """BM25-based sparse retriever for keyword matching.

    This provides the complement to dense vector search:
    - Dense: captures semantic similarity, synonyms, paraphrases
    - Sparse: captures exact keyword matches, rare terms, domain jargon

    The retriever maintains its own in-memory index and can be combined
    with a dense vector index via score fusion.

    Supports BM25+ (with term frequency saturation) for improved results.
    """

    def __init__(
        self,
        k1: float = 1.5,
        b: float = 0.75,
        delta: float = 0.5,
        analyzer: str = "standard",
        language: str = "zh",
    ):
        """Initialize BM25 retriever.

        Args:
            k1: Term frequency saturation parameter (1.2-2.0).
            b: Length normalization parameter (0.0-1.0).
            delta: BM25+ delta parameter for term frequency normalization.
            analyzer: Tokenization strategy ("standard", "jieba", "whitespace").
            language: Language hint for tokenizer ("zh" for Chinese, "en" for English).
        """
        self._k1 = k1
        self._b = b
        self._delta = delta
        self._analyzer = analyzer
        self._language = language

        # Corpus state
        self._corpus: List[str] = []
        self._ids: List[str] = []
        self._tokenized: List[List[str]] = []

        # Precomputed statistics
        self._avg_doc_length: float = 0.0
        self._doc_lengths: List[int] = []
        self._idf: Dict[str, float] = {}
        self._term_freqs: List[Dict[str, int]] = []
        self._total_docs: int = 0

        # Usage tracking
        self._total_searches = 0
        self._total_index_time = 0.0
        self._index_built = False

        # Jieba (Chinese tokenization) availability
        self._jieba_available = False
        try:
            import jieba
            self._jieba = jieba
            self._jieba_available = True
        except ImportError:
            pass

    # ── Tokenization ────────────────────────────────────────────────

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize text based on configured analyzer."""
        if not text or not isinstance(text, str):
            return []

        text = text.lower().strip()

        if self._analyzer == "whitespace":
            return text.split()

        if self._analyzer == "jieba" and self._jieba_available:
            return list(self._jieba.cut(text))

        # Standard tokenizer: split on non-alphanumeric, filter short tokens
        import re
        tokens = re.findall(r'\w+', text)
        # Filter out very short tokens (single chars) unless Chinese
        if self._language == "zh":
            # Keep single Chinese characters
            tokens = [t for t in tokens if len(t) >= 1]
        else:
            tokens = [t for t in tokens if len(t) >= 2]
        return tokens

    # ── Index Building ──────────────────────────────────────────────

    def index_corpus(
        self,
        texts: List[str],
        ids: Optional[List[str]] = None,
    ) -> "BM25SparseRetriever":
        """Build the BM25 index from a list of documents.

        Args:
            texts: List of document texts.
            ids: Optional list of IDs. Auto-generated if not provided.

        Returns:
            Self for chaining.
        """
        start = time.perf_counter()

        if ids is None:
            ids = [f"doc_{i}" for i in range(len(texts))]

        self._corpus = list(texts)
        self._ids = list(ids)
        self._total_docs = len(texts)

        # Tokenize all documents
        import hashlib
        self._tokenized = [self._tokenize(t) for t in texts]
        self._doc_lengths = [len(tokens) for tokens in self._tokenized]
        self._avg_doc_length = (
            sum(self._doc_lengths) / len(self._doc_lengths)
            if self._doc_lengths else 0.0
        )

        # Compute term frequencies per document
        self._term_freqs = []
        doc_freq: Dict[str, int] = {}
        for tokens in self._tokenized:
            tf: Dict[str, int] = {}
            seen_in_doc: set = set()
            for token in tokens:
                tf[token] = tf.get(token, 0) + 1
                if token not in seen_in_doc:
                    seen_in_doc.add(token)
                    doc_freq[token] = doc_freq.get(token, 0) + 1
            self._term_freqs.append(tf)

        # Compute IDF for all terms
        N = self._total_docs
        for term, df in doc_freq.items():
            # BM25 IDF formula
            self._idf[term] = math.log(1 + (N - df + 0.5) / (df + 0.5))

        self._index_built = True
        self._total_index_time = time.perf_counter() - start

        logger.info(
            "BM25 index built: %d docs, %d unique terms, avg_len=%.1f, time=%.3fs",
            self._total_docs, len(self._idf), self._avg_doc_length,
            self._total_index_time
        )
        return self

    def add_documents(
        self,
        texts: List[str],
        ids: Optional[List[str]] = None,
    ) -> "BM25SparseRetriever":
        """Add documents to an existing index (incremental).

        For simplicity, this rebuilds the full index after adding.
        For very large indexes, consider incremental BM25 variants.
        """
        if not self._index_built:
            return self.index_corpus(texts, ids)

        if ids is None:
            ids = [f"doc_{len(self._ids) + i}" for i in range(len(texts))]

        self._corpus.extend(texts)
        self._ids.extend(ids)

        # Rebuild the full index
        return self.index_corpus(self._corpus, self._ids)

    # ── Search ──────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search the BM25 index for the given query.

        Args:
            query: Search query string.
            top_k: Number of results to return.

        Returns:
            List of result dicts with keys: id, score, text.
        """
        self._total_searches += 1

        if not self._index_built or self._total_docs == 0:
            return []

        start = time.perf_counter()
        query_tokens = self._tokenize(query)

        if not query_tokens:
            return []

        # Compute BM25 scores for each document
        scores = np.zeros(self._total_docs, dtype=np.float32)
        for term in query_tokens:
            if term not in self._idf:
                continue
            idf = self._idf[term]

            for doc_idx in range(self._total_docs):
                tf = self._term_freqs[doc_idx].get(term, 0)
                if tf == 0:
                    continue

                # BM25+ scoring with term frequency saturation
                doc_len = self._doc_lengths[doc_idx]
                numerator = tf * (self._k1 + 1)
                denominator = tf + self._k1 * (
                    1 - self._b + self._b * doc_len / self._avg_doc_length
                )
                # BM25+ delta component for better term frequency normalization
                scores[doc_idx] += idf * (numerator / denominator + self._delta)

        # Sort by score descending
        top_indices = np.argsort(scores)[-top_k:][::-1]

        results = []
        for idx in top_indices:
            score = float(scores[idx])
            if score <= 0:
                continue
            results.append({
                "id": self._ids[idx],
                "score": score,
                "text": self._corpus[idx],
                "bm25_score": score,
            })

        elapsed = time.perf_counter() - start
        logger.debug(
            "BM25 search: query='%s' top_k=%d results=%d time=%.3fs",
            query[:50], top_k, len(results), elapsed
        )

        return results

    # ── Utility ─────────────────────────────────────────────────────

    def size(self) -> int:
        """Number of indexed documents."""
        return self._total_docs

    def vocabulary_size(self) -> int:
        """Number of unique terms in the index."""
        return len(self._idf)

    def statistics(self) -> Dict[str, Any]:
        """Index statistics."""
        return {
            "total_docs": self._total_docs,
            "vocabulary_size": len(self._idf),
            "avg_doc_length": round(self._avg_doc_length, 2),
            "index_built": self._index_built,
            "index_time_s": round(self._total_index_time, 4),
            "total_searches": self._total_searches,
            "k1": self._k1,
            "b": self._b,
            "delta": self._delta,
            "analyzer": self._analyzer,
            "language": self._language,
        }

    def clear(self):
        """Reset the index."""
        self._corpus = []
        self._ids = []
        self._tokenized = []
        self._doc_lengths = []
        self._term_freqs = []
        self._idf = {}
        self._total_docs = 0
        self._index_built = False


# ── Score Fusion Utilities ──────────────────────────────────────────────

def fuse_scores_sparse_dense(
    sparse_results: List[Dict[str, Any]],
    dense_results: List[Dict[str, Any]],
    alpha: float = 0.3,
    top_k: int = 10,
    id_key: str = "id",
) -> List[Dict[str, Any]]:
    """Fuse BM25 sparse scores with FAISS dense scores using reciprocal rank fusion.

    This implements Reciprocal Rank Fusion (RRF), which is a simple yet
    effective method for combining multiple ranking lists.

    Args:
        sparse_results: Results from BM25SparseRetriever.search().
        dense_results: Results from VectorIndex.search().
        alpha: Weight for sparse scores in linear combination (0-1).
               If alpha=0, only dense. If alpha=1, only sparse.
               For RRF mode, this controls the constant k.
        top_k: Maximum results to return.
        id_key: Key for result ID in result dicts.

    Returns:
        Fused and ranked result list.
    """
    import math

    # Build score maps
    sparse_scores: Dict[str, float] = {}
    dense_scores: Dict[str, float] = {}

    # Normalize scores to [0, 1]
    def normalize(results, key="score"):
        scores = [r.get(key, 0) for r in results]
        if not scores:
            return {}
        max_s = max(scores) if max(scores) > 0 else 1.0
        min_s = min(scores)
        range_s = max_s - min_s if max_s > min_s else 1.0
        return {r.get(id_key, ""): (r.get(key, 0) - min_s) / range_s for r in results}

    # Support both BM25 and SPLADE sparse score keys
    sparse_score_key = "splade_score" if any("splade_score" in r for r in sparse_results) else "bm25_score"
    sparse_scores = normalize(sparse_results, sparse_score_key)
    dense_scores = normalize(dense_results)

    # Reciprocal Rank Fusion
    all_ids = set(sparse_scores.keys()) | set(dense_scores.keys())
    k = 60  # RRF constant

    fused = []
    for doc_id in all_ids:
        rank_sparse = 0
        rank_dense = 0
        for rank, r in enumerate(sparse_results):
            if r.get(id_key) == doc_id:
                rank_sparse = rank + 1
                break
        for rank, r in enumerate(dense_results):
            if r.get(id_key) == doc_id:
                rank_dense = rank + 1
                break

        # RRF score
        rrf_score = (1 / (k + rank_sparse if rank_sparse > 0 else float('inf'))) + \
                    (1 / (k + rank_dense if rank_dense > 0 else float('inf')))

        # Linear combination as secondary signal
        linear_score = (
            alpha * sparse_scores.get(doc_id, 0) +
            (1 - alpha) * dense_scores.get(doc_id, 0)
        )

        fused.append({
            "id": doc_id,
            "score": rrf_score,
            "rrf_score": rrf_score,
            "linear_score": linear_score,
            "sparse_score": sparse_scores.get(doc_id, 0),
            "dense_score": dense_scores.get(doc_id, 0),
        })

    # Sort by RRF score
    fused.sort(key=lambda x: x["rrf_score"], reverse=True)
    return fused[:top_k]


# ── Self-test ──────────────────────────────────────────────────────────

def self_test():
    """Quick self-test for BM25 sparse retriever."""
    print("=" * 60)
    print("  BM25 Sparse Retriever - Self Test")
    print("=" * 60)

    bm25 = BM25SparseRetriever()

    docs = [
        "Machine learning is a subset of artificial intelligence.",
        "Deep neural networks are a key machine learning technique.",
        "The weather today is sunny with a chance of rain.",
        "I like to cook pasta with tomato sauce.",
        "Supervised learning uses labeled training data.",
        "Natural language processing helps computers understand text.",
    ]
    ids = [f"doc_{i}" for i in range(len(docs))]

    bm25.index_corpus(docs, ids)
    print(f"\n  Index: {bm25.size()} docs, {bm25.vocabulary_size()} terms")

    query = "machine learning neural networks"
    results = bm25.search(query, top_k=3)
    print(f"\n  Query: '{query}'")
    print(f"  Results: {len(results)}")
    for r in results:
        print(f"    {r['id']}: score={r['score']:.4f}  text={r['text'][:60]}")

    print(f"\n  Statistics: {bm25.statistics()}")

    print("\n" + "=" * 60)
    print("  Self-test complete")
    print("=" * 60)


if __name__ == "__main__":
    self_test()
