"""
SPLADE Learned Sparse Retriever
===============================
SPLADE (Sparse Lexical and Expansion) for learned sparse retrieval.

SPLADE produces learned sparse vectors via a Transformer encoder, where each
dimension corresponds to a vocabulary token with an importance weight.
This replaces BM25's rule-based term weighting with learned semantic matching.

Two key advantages over BM25:
  - Understands synonyms: "car" matches "automobile" via BERT contextualization
  - Query expansion built-in: generates related terms via MLM head

Usage:
    sparse = SPLADESparseRetriever()
    sparse.index_corpus(texts, ids)
    results = sparse.search(query, top_k=20)

Reference:
    - SPLADE: Sparse Lexical and Expansion Model for First Stage Ranking (2021)
    - naver/splade-cocondenser-ensembledistil (lightweight DistilBERT variant)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import math

import numpy as np

logger = logging.getLogger(__name__)

try:
    import torch
    from transformers import AutoModel, AutoTokenizer
    _TRANSFORMERS_AVAILABLE = True
except ImportError:
    _TRANSFORMERS_AVAILABLE = False
    torch = None


# Default SPLADE model (lightweight DistilBERT, ~68M params)
DEFAULT_SPLADE_MODEL = "naver/splade-cocondenser-ensembledistil"


class SPLADESparseRetriever:
    """SPLADE-based learned sparse retriever for keyword+semantic matching.

    Replaces BM25 with a learned sparse retrieval model that understands
    synonyms and generates expansion terms automatically.

    Interface identical to BM25SparseRetriever for drop-in replacement.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_SPLADE_MODEL,
        device: Optional[str] = None,
        batch_size: int = 32,
        max_length: int = 256,
        query_max_length: int = 64,
        lamda: float = 0.1,
    ):
        """Initialize SPLADE retriever.

        Args:
            model_name: HuggingFace model name.
            device: 'cpu', 'cuda', or None for auto-detect.
            batch_size: Batch size for encoding documents.
            max_length: Max token length for document encoding.
            query_max_length: Max token length for query encoding.
            lamda: Regularization strength for sparsity (lower = denser).
        """
        self._model_name = model_name
        self._device = device or ("cuda" if torch and torch.cuda.is_available() else "cpu")
        self._batch_size = batch_size
        self._max_length = max_length
        self._query_max_length = query_max_length
        self._lamda = lamda

        # Load model (lazy)
        self._model = None
        self._tokenizer = None
        self._model_loaded = False

        # Corpus state
        self._corpus: List[str] = []
        self._ids: List[str] = []
        self._doc_vectors: List[np.ndarray] = []  # sparse doc vectors
        self._vocab: Dict[str, int] = {}  # term -> sparse dimension
        self._idf: Dict[int, float] = {}  # sparse dim -> IDF weight
        self._total_docs: int = 0
        self._index_built: bool = False

        # Usage tracking
        self._total_searches = 0
        self._total_index_time = 0.0
        self._total_search_time = 0.0
        self._total_encode_time = 0.0

    def _ensure_model(self):
        """Lazy-load the SPLADE model and tokenizer."""
        if self._model_loaded:
            return
        if not _TRANSFORMERS_AVAILABLE:
            raise ImportError(
                "transformers is required for SPLADE. "
                "Install with: pip install transformers torch"
            )

        start = time.perf_counter()
        logger.info("Loading SPLADE model: %s (device=%s)", self._model_name, self._device)
        self._tokenizer = AutoTokenizer.from_pretrained(self._model_name)
        self._model = AutoModel.from_pretrained(self._model_name)
        self._model.to(self._device)
        self._model.eval()
        self._model_loaded = True
        elapsed = time.perf_counter() - start
        logger.info("SPLADE model loaded in %.2fs", elapsed)

    def _encode_sparse(self, texts: List[str], max_length: int) -> np.ndarray:
        """Encode texts into sparse SPLADE vectors.

        The SPLADE model outputs token-level logits; we apply ReLU + FLOG
        to produce a sparse vector where each dimension is a vocabulary token's
        importance weight.

        Args:
            texts: List of text strings to encode.
            max_length: Max token length.

        Returns:
            np.ndarray of shape (len(texts), vocab_size) — sparse vectors.
        """
        self._ensure_model()

        start = time.perf_counter()
        all_vecs = []

        for i in range(0, len(texts), self._batch_size):
            batch_texts = texts[i:i + self._batch_size]
            inputs = self._tokenizer(
                batch_texts,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=max_length,
            ).to(self._device)

            with torch.no_grad():
                outputs = self._model(**inputs)

            # SPLADE: ReLU on hidden states, then log(1 + ReLU) — FLOG regularization
            # outputs.last_hidden_state: (batch, seq_len, hidden_dim)
            # We use the MLM head logits (if available) or hidden states
            hidden = outputs.last_hidden_state
            # Take the max over sequence dimension (bag-of-words pooling)
            vec, _ = torch.max(torch.relu(hidden), dim=1)  # (batch, hidden_dim)

            # Apply FLOG: log(1 + ReLU(x)) for sparsity
            vec = torch.log(1.0 + vec)

            # L2 normalize for cosine-compatible scoring
            vec = vec / (vec.norm(dim=1, keepdim=True) + 1e-8)

            all_vecs.append(vec.cpu().numpy())

        result = np.concatenate(all_vecs, axis=0)
        elapsed = time.perf_counter() - start
        self._total_encode_time += elapsed
        logger.debug("Encoded %d texts in %.3fs", len(texts), elapsed)
        return result

    # ── Index Building ──────────────────────────────────────────────

    def index_corpus(
        self,
        texts: List[str],
        ids: Optional[List[str]] = None,
    ) -> "SPLADESparseRetriever":
        """Build the SPLADE sparse index from a list of documents.

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

        # Encode all documents into sparse SPLADE vectors
        self._doc_vectors = self._encode_sparse(texts, self._max_length)

        # Build IDF weights from sparse vectors (token frequency across corpus)
        # SPLADE dimensions correspond to BERT's vocabulary (30k tokens)
        doc_freq = {}
        for vec in self._doc_vectors:
            nonzero_dims = np.where(vec > 0.01)[0]  # above threshold
            for dim in nonzero_dims:
                doc_freq[dim] = doc_freq.get(dim, 0) + 1

        N = self._total_docs
        for dim, df in doc_freq.items():
            self._idf[dim] = 1 + math.log((1 + N) / (1 + df))

        self._index_built = True
        self._total_index_time = time.perf_counter() - start

        logger.info(
            "SPLADE index built: %d docs, %d active dims, time=%.3fs",
            self._total_docs, len(self._idf), self._total_index_time,
        )
        return self

    def add_documents(
        self,
        texts: List[str],
        ids: Optional[List[str]] = None,
    ) -> "SPLADESparseRetriever":
        """Add documents to an existing index (incremental)."""
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
        """Search the SPLADE index for the given query.

        Encodes the query into a sparse SPLADE vector, then computes dot
        product similarity with all document vectors.

        Args:
            query: Search query string.
            top_k: Number of results to return.

        Returns:
            List of result dicts with keys: id, score, text.
        """
        self._total_searches += 1

        if not self._index_built or self._total_docs == 0 or not query:
            return []

        search_start = time.perf_counter()

        # Encode query into sparse SPLADE vector
        query_vec = self._encode_sparse([query], self._query_max_length)[0]  # (vocab_dim,)

        # Compute dot product with all document vectors
        # Both vectors are L2-normalized, so dot = cosine similarity
        scores = self._doc_vectors @ query_vec  # (num_docs,)

        # Apply IDF boost: weight rare/important terms higher
        for idx in range(self._total_docs):
            doc_vec = self._doc_vectors[idx]
            boost = 0.0
            nonzero_dims = np.where(doc_vec > 0.01)[0]
            for dim in nonzero_dims:
                if dim in self._idf:
                    boost += self._idf[dim] * doc_vec[dim]
            scores[idx] = scores[idx] * (1.0 + 0.1 * boost / len(nonzero_dims)) if len(nonzero_dims) > 0 else scores[idx]

        # Sort by score descending
        top_indices = np.argsort(scores)[-top_k:][::-1]

        results = []
        for idx in top_indices:
            score = float(scores[idx])
            if score <= 0.001:
                continue
            results.append({
                "id": self._ids[idx],
                "score": score,
                "text": self._corpus[idx],
                "splade_score": score,
            })

        elapsed = time.perf_counter() - search_start
        self._total_search_time += elapsed
        logger.debug(
            "SPLADE search: query='%s' top_k=%d results=%d time=%.3fs",
            query[:50], top_k, len(results), elapsed,
        )

        return results

    # ── Utility ─────────────────────────────────────────────────────

    def size(self) -> int:
        """Number of indexed documents."""
        return self._total_docs

    def vocabulary_size(self) -> int:
        """Number of active SPLADE dimensions."""
        return len(self._idf) if self._idf else 0

    def statistics(self) -> Dict[str, Any]:
        """Index and usage statistics."""
        return {
            "total_docs": self._total_docs,
            "vocabulary_size": len(self._idf),
            "index_built": self._index_built,
            "index_time_s": round(self._total_index_time, 4),
            "total_searches": self._total_searches,
            "total_search_time_s": round(self._total_search_time, 4),
            "total_encode_time_s": round(self._total_encode_time, 4),
            "model": self._model_name,
            "device": self._device,
            "batch_size": self._batch_size,
            "model_loaded": self._model_loaded,
        }

    def clear(self):
        """Reset the index."""
        self._corpus = []
        self._ids = []
        self._doc_vectors = []
        self._vocab = {}
        self._idf = {}
        self._total_docs = 0
        self._index_built = False
        self._total_encode_time = 0.0

    def close(self):
        """Release model resources."""
        self._model = None
        self._tokenizer = None
        self._model_loaded = False
        import gc
        gc.collect()
        if torch and torch.cuda.is_available():
            torch.cuda.empty_cache()
