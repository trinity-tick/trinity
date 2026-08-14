"""
BM25 Keyword Index — Pure-Python Implementation.

No external dependencies (avoids rank-bm25). Features:
  - Inverted index with term-frequency tracking
  - IDF with smoothing (Robertson-Sparck Jones)
  - Document-length normalisation (k1=1.5, b=0.75)
  - Full add / remove / search lifecycle
  - Batch add_documents for bulk indexing

Reference: Manning, Raghavan, Schütze §11.4.3
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


class BM25Index:
    """BM25 inverted index for keyword search over text documents.

    Thread-unsafe by design — callers must serialise writes.

    Parameters
    ----------
    k1 : float
        Term-frequency saturation (default 1.5).
    b  : float
        Document-length normalisation (default 0.75).
    """

    _WORD_RE = re.compile(r"\w+", re.UNICODE)

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b

        # inverted index: term → {doc_id: tf}
        self._index: Dict[str, Dict[str, int]] = defaultdict(dict)

        # document lengths for average-length computation
        self._doc_lengths: Dict[str, int] = {}

        # cached IDF + average doc length
        self._dirty = True
        self._idf_cache: Dict[str, float] = {}
        self._avgdl: float = 0.0

    # ── public API ──────────────────────────────────────────────────

    @property
    def doc_count(self) -> int:
        """Number of indexed documents."""
        return len(self._doc_lengths)

    def add_document(self, doc_id: str, text: str) -> None:
        """Index a single document.

        Parameters
        ----------
        doc_id : str
            Unique document identifier.
        text : str
            Document body (will be tokenised in-place).
        """
        tokens = self._tokenize(text)
        term_freqs = self._term_counts(tokens)
        self._doc_lengths[doc_id] = len(tokens)
        for term, tf in term_freqs.items():
            self._index[term][doc_id] = tf
        self._dirty = True

    def add_documents(self, items: List[Tuple[str, str]]) -> None:
        """Batch-add documents (avoids per-doc IDF rebuild).

        Parameters
        ----------
        items : list of (doc_id, text)
        """
        for doc_id, text in items:
            tokens = self._tokenize(text)
            term_freqs = self._term_counts(tokens)
            self._doc_lengths[doc_id] = len(tokens)
            for term, tf in term_freqs.items():
                self._index[term][doc_id] = tf
        self._dirty = True

    def remove_document(self, doc_id: str) -> None:
        """Remove a document from the index.

        Parameters
        ----------
        doc_id : str
            Document identifier to drop.
        """
        # Remove from doc-lengths
        self._doc_lengths.pop(doc_id, None)

        # Remove from inverted index
        empty_terms: List[str] = []
        for term, postings in self._index.items():
            postings.pop(doc_id, None)
            if not postings:
                empty_terms.append(term)
        for term in empty_terms:
            del self._index[term]

        self._dirty = True

    def search(
        self,
        query: str,
        top_k: int = 10,
    ) -> List[Tuple[str, float]]:
        """Keyword search — returns (doc_id, score) sorted descending.

        Parameters
        ----------
        query : str
            Natural-language query (tokenised same as docs).
        top_k : int
            Max results to return.

        Returns
        -------
        list of (doc_id, bm25_score)
        """
        if self._dirty:
            self._rebuild_idf()

        query_tokens = self._tokenize(query)
        scores: Dict[str, float] = defaultdict(float)

        for token in query_tokens:
            idf = self._idf_cache.get(token, 0.0)
            if idf == 0.0:
                continue
            for doc_id, tf in self._index.get(token, {}).items():
                dl = self._doc_lengths.get(doc_id, 1)
                numerator = tf * (self.k1 + 1.0)
                denominator = tf + self.k1 * (1.0 - self.b + self.b * dl / max(self._avgdl, 1e-9))
                scores[doc_id] += idf * numerator / denominator

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]

    # ── internals ───────────────────────────────────────────────────

    def _tokenize(self, text: str) -> List[str]:
        """Lower-case word tokenization."""
        return self._WORD_RE.findall(text.lower())

    @staticmethod
    def _term_counts(tokens: List[str]) -> Dict[str, int]:
        tf: Dict[str, int] = defaultdict(int)
        for t in tokens:
            tf[t] += 1
        return tf

    def _rebuild_idf(self) -> None:
        N = max(self.doc_count, 1)
        self._avgdl = sum(self._doc_lengths.values()) / max(N, 1)
        self._idf_cache.clear()
        for term, postings in self._index.items():
            df = len(postings)
            self._idf_cache[term] = math.log(1.0 + (N - df + 0.5) / (df + 0.5))
        self._dirty = False
