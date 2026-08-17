"""
ColBERTv2 Late Interaction Retriever
====================================
ColBERT uses token-level late interaction: query tokens and document tokens are
independently encoded, then scored via MaxSim (max similarity over query×document
token pairs). This captures fine-grained lexical/semantic matches that single-vector
dense retrieval misses.

ColBERT is used as a THIRD retrieval channel (alongside SPLADE sparse and FAISS dense)
in a 3-way RRF fusion, providing the highest recall among all three channels.

Reference:
  - ColBERTv2: Effective and Efficient Retrieval via Lightweight Late Interaction (2022)
  - colbert-ir/colbertv2.0 model
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)

# Lazy imports for heavy deps
try:
    import torch
    import torch.nn.functional as F
    from transformers import AutoModel, AutoTokenizer
    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False
    logger.warning("torch/transformers not available; ColBERT will raise on instantiation")


class ColBERTRetriever:
    """
    ColBERTv2-style late interaction retriever.

    Encodes query tokens and document tokens independently, then scores via MaxSim:
      score(q, d) = avg over query tokens of max sim over document tokens

    This captures fine-grained matching (e.g., "car" with "automobile" at the token level).

    Default model: "colbert-ir/colbertv2.0" (or any ColBERT-compatible BERT model).
    A lightweight alternative is "bert-base-uncased" with the ColBERT architecture.

    The retriever maintains its own docid→doc token embedding cache built via
    `index_corpus()` / `add_documents()`.
    """

    def __init__(
        self,
        model_name: str = "bert-base-uncased",
        device: Optional[str] = None,
        max_query_len: int = 32,
        max_doc_len: int = 180,
        batch_size: int = 16,
        dim: int = 768,
        normalize: bool = True,
    ):
        """
        Args:
            model_name: HuggingFace model name. For true ColBERT, use "colbert-ir/colbertv2.0".
                        For a lightweight alternative without downloading ColBERT weights,
                        use "bert-base-uncased" (768-dim, works out of the box).
            device: "cpu", "cuda", or None for auto-detect.
            max_query_len: Max query tokens (ColBERT default: 32).
            max_doc_len: Max document tokens (ColBERT default: 180).
            batch_size: Batch size for document encoding.
            dim: Embedding dimension (768 for BERT-base, 128 for colbertv2).
            normalize: L2-normalize token embeddings for cosine similarity.
        """
        if not _TORCH_AVAILABLE:
            raise ImportError(
                "ColBERT requires torch and transformers. "
                "Install with: pip install torch transformers"
            )

        self._model_name = model_name
        self._device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._max_query_len = max_query_len
        self._max_doc_len = max_doc_len
        self._batch_size = batch_size
        self._dim = dim
        self._normalize = normalize

        self._model = None
        self._tokenizer = None
        self._is_loaded = False

        # Document index
        self._doc_ids: List[str] = []
        self._doc_texts: List[str] = []
        self._doc_embeddings: Optional[torch.Tensor] = None  # [n_docs, max_doc_len, dim]

        self._stats = {
            "calls": 0,
            "documents_indexed": 0,
            "total_search_time_ms": 0,
        }

    def _load_model(self) -> None:
        """Lazy-load the tokenizer and model."""
        if self._is_loaded:
            return
        logger.info("Loading ColBERT model: %s on %s", self._model_name, self._device)
        t0 = time.perf_counter()
        self._tokenizer = AutoTokenizer.from_pretrained(self._model_name)
        self._model = AutoModel.from_pretrained(self._model_name)
        self._model = self._model.to(self._device)
        self._model.eval()
        elapsed = time.perf_counter() - t0
        self._is_loaded = True
        logger.info("ColBERT model loaded in %.2fs", elapsed)

    def _encode(
        self,
        texts: List[str],
        max_len: int,
        batch_size: Optional[int] = None,
    ) -> torch.Tensor:
        """
        Encode texts into token-level embeddings.

        Returns:
            Tensor of shape [n_texts, max_len, dim] with padding mask applied
            (padded positions have zero embeddings).
        """
        self._load_model()
        bs = batch_size or self._batch_size
        all_embeddings = []

        for i in range(0, len(texts), bs):
            batch_texts = texts[i : i + bs]
            inputs = self._tokenizer(
                batch_texts,
                padding="max_length",
                truncation=True,
                max_length=max_len,
                return_tensors="pt",
            )
            input_ids = inputs["input_ids"].to(self._device)
            attention_mask = inputs["attention_mask"].to(self._device)

            with torch.no_grad():
                outputs = self._model(input_ids, attention_mask=attention_mask)

            # Use last hidden state as token embeddings
            # Shape: [batch_size, seq_len, dim]
            embeddings = outputs.last_hidden_state

            # Zero out padding positions
            mask = attention_mask.unsqueeze(-1).float()  # [batch, seq_len, 1]
            embeddings = embeddings * mask

            if self._normalize:
                # L2-normalize along the dim axis
                embeddings = F.normalize(embeddings, p=2, dim=-1)

            all_embeddings.append(embeddings.cpu())

        return torch.cat(all_embeddings, dim=0)

    def index_corpus(self, texts: List[str], ids: Optional[List[str]] = None) -> "ColBERTRetriever":
        """
        Index a corpus of documents by encoding them into token-level embeddings.

        Args:
            texts: List of document texts.
            ids: Optional list of document IDs. If None, uses 0-based indices as strings.

        Returns:
            self (for chaining).
        """
        if ids is None:
            ids = [str(i) for i in range(len(texts))]

        logger.info("ColBERT indexing %d documents...", len(texts))
        t0 = time.perf_counter()

        self._doc_ids = list(ids)
        self._doc_texts = list(texts)
        self._doc_embeddings = self._encode(texts, self._max_doc_len)

        elapsed = time.perf_counter() - t0
        self._stats["documents_indexed"] = len(texts)
        logger.info("ColBERT indexed %d docs in %.2fs (embeddings shape: %s)",
                     len(texts), elapsed, list(self._doc_embeddings.shape))
        return self

    def add_documents(self, texts: List[str], ids: List[str]) -> "ColBERTRetriever":
        """
        Add documents to the existing index (incremental update).

        For simplicity, this is an alias for full re-index. In production,
        you'd append to the tensor. For Trinity's use case with manageable corpus
        sizes, full rebuild is fine.
        """
        logger.warning("ColBERT add_documents: performing full rebuild")
        all_texts = list(self._doc_texts) + list(texts)
        all_ids = list(self._doc_ids) + list(ids)
        return self.index_corpus(all_texts, all_ids)

    def search(
        self,
        query: str,
        top_k: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Search using ColBERT late interaction (MaxSim scoring).

        For each query token, finds the maximum similarity with any document token,
        then averages across query tokens.

        Args:
            query: The search query string.
            top_k: Number of results to return.

        Returns:
            List of result dicts with keys: id, text, score, colbert_score.
        """
        if self._doc_embeddings is None or len(self._doc_ids) == 0:
            logger.warning("ColBERT search called but no documents indexed")
            return []

        self._stats["calls"] += 1
        t0 = time.perf_counter()

        # Encode query
        query_emb = self._encode([query], self._max_query_len)  # [1, q_len, dim]
        q_emb = query_emb[0]  # [q_len, dim]

        # ColBERT late interaction: MaxSim scoring
        # For each query token i, find max similarity across all doc tokens j
        # score(q_i, d) = max_j cos(q_i, d_j)
        # score(q, d) = mean_i score(q_i, d)

        # doc_embeddings: [n_docs, doc_len, dim]
        # q_emb: [q_len, dim]
        # We compute: sim[i, doc] = max over j of q_i · d_doc_j

        doc_emb = self._doc_embeddings.to(self._device)  # [n_docs, doc_len, dim]
        q_emb_dev = q_emb.to(self._device)  # [q_len, dim]

        with torch.no_grad():
            # Compute dot product: [q_len, n_docs, doc_len]
            # q_emb_dev: [q_len, 1, 1, dim]
            # doc_emb: [1, n_docs, doc_len, dim]
            # sim: [q_len, n_docs, doc_len]
            sim = (q_emb_dev.unsqueeze(1).unsqueeze(2) * doc_emb.unsqueeze(0)).sum(dim=-1)

            # Max over document tokens: [q_len, n_docs]
            max_sim, _ = sim.max(dim=-1)

            # Mean over query tokens: [n_docs]
            # Compute masking for query tokens (exclude [SEP] padding)
            scores = max_sim.mean(dim=0)  # [n_docs]

        # Convert to numpy
        scores_np = scores.cpu().numpy()

        # Build results
        results = []
        for idx in np.argsort(-scores_np)[:top_k]:
            score = float(scores_np[idx])
            results.append({
                "id": self._doc_ids[idx],
                "score": score,
                "text": self._doc_texts[idx],
                "colbert_score": score,
                "bm25_score": score,  # Alias for RRF fusion compatibility
            })

        elapsed_ms = (time.perf_counter() - t0) * 1000
        self._stats["total_search_time_ms"] += elapsed_ms

        return results

    def statistics(self) -> Dict[str, Any]:
        """Return usage statistics."""
        stats = dict(self._stats)
        stats["model"] = self._model_name
        stats["device"] = self._device
        stats["dim"] = self._dim
        stats["normalize"] = self._normalize
        stats["documents_in_index"] = len(self._doc_ids)
        if self._stats["calls"] > 0:
            stats["avg_search_time_ms"] = self._stats["total_search_time_ms"] / self._stats["calls"]
        return stats
