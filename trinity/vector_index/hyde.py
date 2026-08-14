"""
HyDE (Hypothetical Document Embeddings) Retriever
================================================
Generates a hypothetical document from the user's query using a small LLM (via Ollama),
embeds both the query and the hypothetical document, then merges the retrieval results.

This bridges the vocabulary gap between query and document, significantly improving
zero-shot recall, especially for rare/non-obvious queries.

Reference: "Precise Zero-Shot Dense Retrieval without Relevance Labels" (Gao et al., 2023)
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

import numpy as np
import requests

logger = logging.getLogger(__name__)

# Default prompt templates for generating hypothetical documents
HYPOTHETICAL_PROMPTS = {
    "default": (
        "Please write a short passage that provides relevant information to the following question or query. "
        "Write as if you are answering the question in a factual, informative tone.\n\n"
        "Query: {query}\n\n"
        "Relevant passage:"
    ),
    "factoid": (
        "Generate a factual paragraph that contains the answer to this question. "
        "Include specific details, dates, names, and figures where appropriate.\n\n"
        "Question: {query}\n\n"
        "Informative paragraph:"
    ),
    "howto": (
        "Write a step-by-step explanation that addresses this request. "
        "Be practical and include specific instructions.\n\n"
        "Request: {query}\n\n"
        "Explanation:"
    ),
}

# Simple query type detection (extendable)
_QUERY_PATTERNS = {
    "factoid": [r"^what\s+is", r"^who\s+is", r"^when\s+was", r"^where\s+is", r"^define", r"^explain"],
    "howto": [r"^how\s+(to|do|can)", r"^steps?\s+(to|for)", r"^guide\s+(to|for)"],
}


def _detect_query_type(query: str) -> str:
    """Detect query type for choosing the best HyDE prompt template."""
    lower = query.lower().strip()
    for qtype, patterns in _QUERY_PATTERNS.items():
        for pat in patterns:
            import re
            if re.match(pat, lower):
                return qtype
    return "default"


class HydeRetriever:
    """
    HyDE (Hypothetical Document Embeddings) Retriever.

    For a given query:
      1. Generate a hypothetical document using Ollama.
      2. Embed both the original query and the hypothetical doc via the FAISS index's encoder.
      3. Retrieve with both embeddings, merge results via weighted score averaging.

    This is designed as an add-on to the existing FAISS dense retriever (Stage 3),
    not a replacement — it enriches the dense retrieval channel with HyDE-augmented results.
    """

    def __init__(
        self,
        faiss_index: Any,
        embed_fn,
        ollama_base_url: str = "http://localhost:11434",
        llm_model: str = "qwen3:0.6b",
        hyde_top_k: int = 100,
        merge_alpha: float = 0.4,
        max_hypothetical_length: int = 200,
        timeout: int = 15,
    ):
        """
        Args:
            faiss_index: The FAISS index object with a `.search(vector, k)` method.
            embed_fn: Callable that takes a string and returns a numpy ndarray embedding.
            ollama_base_url: Ollama REST API base URL.
            llm_model: Ollama model name for generation (use smallest capable model).
            hyde_top_k: Number of results to retrieve per embedding.
            merge_alpha: Weight of HyDE results vs original query results (0 = only original, 1 = only HyDE).
            max_hypothetical_length: Maximum tokens for the generated hypothetical doc.
            timeout: Timeout in seconds for the Ollama API call.
        """
        self._faiss_index = faiss_index
        self._embed_fn = embed_fn
        self._ollama_base_url = ollama_base_url.rstrip("/")
        self._llm_model = llm_model
        self._hyde_top_k = hyde_top_k
        self._merge_alpha = max(0.0, min(1.0, merge_alpha))
        self._max_hypothetical_length = max_hypothetical_length
        self._timeout = timeout
        self._stats = {
            "calls": 0,
            "hyde_generated": 0,
            "hyde_failures": 0,
            "total_generation_time_ms": 0,
        }

    def _generate_hypothetical_document(self, query: str) -> Optional[str]:
        """
        Generate a hypothetical document using Ollama.

        Returns None if generation fails or times out.
        """
        query_type = _detect_query_type(query)
        prompt_template = HYPOTHETICAL_PROMPTS.get(query_type, HYPOTHETICAL_PROMPTS["default"])
        prompt = prompt_template.format(query=query)

        payload = {
            "model": self._llm_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": self._max_hypothetical_length,
                "temperature": 0.7,
            },
        }

        try:
            t0 = time.perf_counter()
            resp = requests.post(
                f"{self._ollama_base_url}/api/generate",
                json=payload,
                timeout=self._timeout,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            self._stats["total_generation_time_ms"] += elapsed_ms

            if resp.status_code != 200:
                logger.warning("HyDE Ollama generation returned %d: %s", resp.status_code, resp.text[:200])
                self._stats["hyde_failures"] += 1
                return None

            result = resp.json()
            hypothetical = result.get("response", "").strip()
            if not hypothetical:
                logger.warning("HyDE generated empty response for query: %.80s", query)
                self._stats["hyde_failures"] += 1
                return None

            self._stats["hyde_generated"] += 1
            logger.debug("HyDE generated %d chars in %.0fms for query: %.60s", len(hypothetical), elapsed_ms, query)
            return hypothetical

        except requests.exceptions.Timeout:
            logger.warning("HyDE Ollama generation timed out after %ds for query: %.80s", self._timeout, query)
            self._stats["hyde_failures"] += 1
            return None
        except requests.exceptions.ConnectionError as e:
            logger.error("HyDE cannot connect to Ollama at %s: %s", self._ollama_base_url, e)
            self._stats["hyde_failures"] += 1
            return None
        except Exception as e:
            logger.error("HyDE generation error: %s", e)
            self._stats["hyde_failures"] += 1
            return None

    def search(
        self,
        query: str,
        query_vector: np.ndarray,
        doc_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Perform HyDE-augmented search.

        Args:
            query: The original text query.
            query_vector: The original query embedding vector.
            doc_ids: Optional list of document IDs to restrict results to.

        Returns:
            List of result dicts with keys: id, score, text, hyde_score, original_score
        """
        self._stats["calls"] += 1

        # Step 1: Original query retrieval
        orig_results = self._faiss_index.search(query_vector, self._hyde_top_k)
        if doc_ids:
            orig_results = [r for r in orig_results if r.get("id") in doc_ids]

        # Step 2: Generate hypothetical document
        hypothetical = self._generate_hypothetical_document(query)
        if hypothetical is None:
            # Fall back to original results if HyDE fails
            for r in orig_results:
                r["hyde_score"] = 0.0
                r["original_score"] = r.get("score", 0.0)
            return orig_results

        # Step 3: Embed the hypothetical document and retrieve with it
        try:
            hyde_vector = self._embed_fn(hypothetical)
        except Exception as e:
            logger.error("HyDE embedding error: %s", e)
            for r in orig_results:
                r["hyde_score"] = 0.0
                r["original_score"] = r.get("score", 0.0)
            return orig_results

        hyde_results = self._faiss_index.search(hyde_vector, self._hyde_top_k)
        if doc_ids:
            hyde_results = [r for r in hyde_results if r.get("id") in doc_ids]

        # Step 4: Merge results with weighted scoring
        return self._merge_results(orig_results, hyde_results, self._hyde_top_k)

    def _merge_results(
        self,
        orig_results: List[Dict[str, Any]],
        hyde_results: List[Dict[str, Any]],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """
        Merge original and HyDE results using score averaging.

        Each result gets a final score = (1 - alpha) * original_score + alpha * hyde_score
        where scores are normalized to [0, 1] within each result set.
        """
        if not orig_results:
            return hyde_results[:top_k]
        if not hyde_results:
            return orig_results[:top_k]

        alpha = self._merge_alpha

        # Build lookup for original scores (normalized)
        orig_scores = {r.get("id", ""): self._safe_score(r.get("score", 0.0)) for r in orig_results}
        if orig_scores:
            max_os = max(orig_scores.values()) or 1.0
            orig_scores = {k: v / max_os for k, v in orig_scores.items()}

        # Build lookup for HyDE scores (normalized)
        hyde_scores = {r.get("id", ""): self._safe_score(r.get("score", 0.0)) for r in hyde_results}
        if hyde_scores:
            max_hs = max(hyde_scores.values()) or 1.0
            hyde_scores = {k: v / max_hs for k, v in hyde_scores.items()}

        # Merge — collect all unique IDs
        all_ids = set(orig_scores.keys()) | set(hyde_scores.keys())
        merged_texts = {}
        for r in orig_results:
            merged_texts[r.get("id", "")] = r.get("text", "")
        for r in hyde_results:
            merged_texts[r.get("id", "")] = r.get("text", "")

        merged = []
        for doc_id in all_ids:
            os_score = orig_scores.get(doc_id, 0.0)
            hs_score = hyde_scores.get(doc_id, 0.0)
            final_score = (1.0 - alpha) * os_score + alpha * hs_score
            merged.append({
                "id": doc_id,
                "score": final_score,
                "text": merged_texts.get(doc_id, ""),
                "original_score": os_score,
                "hyde_score": hs_score,
            })

        # Sort by descending final score
        merged.sort(key=lambda x: x["score"], reverse=True)
        return merged[:top_k]

    @staticmethod
    def _safe_score(s: Any) -> float:
        """Convert a score to a positive float."""
        try:
            return max(float(s), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def statistics(self) -> Dict[str, Any]:
        """Return usage statistics for monitoring."""
        stats = dict(self._stats)
        avg_gen_ms = 0.0
        if stats["hyde_generated"] > 0:
            avg_gen_ms = stats["total_generation_time_ms"] / stats["hyde_generated"]
        stats["avg_generation_time_ms"] = avg_gen_ms
        stats["merge_alpha"] = self._merge_alpha
        stats["llm_model"] = self._llm_model
        return stats
