"""
P0-6: Memory Deduplication Engine (内容哈希 + 语义相似度)

Prevents redundant storage of identical or semantically equivalent
memories across the Trinity ecosystem. Uses a two-tier approach:

  Tier 1 — Content Hash Dedup (exact match):
    SHA-256 hashing of normalized memory content. O(1) lookup.
    Catches exact duplicates immediately.

  Tier 2 — Semantic Similarity Dedup (near-duplicate):
    Cosine similarity over embedding vectors. Configurable threshold.
    Catches paraphrased / semantically equivalent memories.

Design:
  - Pluggable: Hash backend + similarity backend are replaceable
  - Batch-friendly: bulk_dedup() for processing large memory sets
  - Thread-safe: RLocks on all mutable state
  - Configurable: thresholds, max hash cache size, similarity engine
  - Audit-ready: every dedup decision is logged with reasoning

Aligned with:
  - Trinity XMemory deduplication infrastructure
  - GDPR data minimization principle
  - ICLR 2026 scalable dedup benchmarks
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ── Enums ──────────────────────────────────────────────────────────

class DedupDecision(Enum):
    """Decision for a single dedup check."""
    UNIQUE = auto()              # No duplicate found — store as new
    EXACT_DUPLICATE = auto()     # Identical content hash — skip storage
    NEAR_DUPLICATE = auto()      # Semantic similarity above threshold — flag
    MERGED = auto()              # Merged into existing record
    ERROR = auto()               # Error during dedup check


class DedupStrategy(Enum):
    """Overall deduplication strategy."""
    STRICT = "strict"            # Reject any duplicate (exact + near)
    EXACT_ONLY = "exact_only"    # Only reject exact duplicates
    FLAG_ONLY = "flag_only"      # Flag duplicates but still store
    MERGE = "merge"              # Merge duplicates into existing records


# ── Data Structures ────────────────────────────────────────────────

@dataclass
class ContentFingerprint:
    """Fingerprint of a memory content chunk.

    Fields:
        fingerprint_id: Unique fingerprint identifier
        content_hash: SHA-256 hash of normalized content
        normalized_text: Content after normalization (whitespace, lowercasing)
        embedding: Optional embedding vector for semantic comparison
        memory_id: Original memory ID this fingerprint represents
        created_at: Unix timestamp of fingerprint creation
        metadata: Arbitrary key-value extensions
    """
    fingerprint_id: str = ""
    content_hash: str = ""
    normalized_text: str = ""
    embedding: Optional[np.ndarray] = None
    memory_id: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(self.fingerprint_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ContentFingerprint):
            return NotImplemented
        return self.fingerprint_id == other.fingerprint_id


@dataclass
class DedupResult:
    """Result of a single deduplication check.

    Fields:
        memory_id: Input memory ID
        decision: Dedup decision
        matched_fingerprint_id: If duplicate, which existing fingerprint matched
        similarity_score: Cosine similarity score (for near-duplicates)
        reason: Human-readable explanation
        suggested_action: What the caller should do (store / skip / merge / review)
    """
    memory_id: str
    decision: DedupDecision = DedupDecision.UNIQUE
    matched_fingerprint_id: str = ""
    similarity_score: float = 0.0
    reason: str = ""
    suggested_action: str = "store"


@dataclass
class DedupStats:
    """Aggregated dedup statistics for a batch or session."""
    total_checked: int = 0
    unique: int = 0
    exact_duplicates: int = 0
    near_duplicates: int = 0
    merged: int = 0
    errors: int = 0
    total_hash_lookups_ns: float = 0.0
    total_similarity_compute_ms: float = 0.0


# ── Content Normalizer ─────────────────────────────────────────────

def normalize_content(text: str) -> str:
    """Normalize text for consistent hashing.

    Steps:
      1. Strip leading/trailing whitespace
      2. Collapse multiple whitespace chars to single space
      3. Lowercase

    This ensures that formatting differences don't cause false negatives
    in exact duplicate detection.
    """
    if not text:
        return ""
    return " ".join(text.strip().lower().split())


def compute_content_hash(text: str) -> str:
    """Compute SHA-256 hash of normalized content."""
    normalized = normalize_content(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ── Semantic Similarity Engine ─────────────────────────────────────

class SemanticSimilarityEngine:
    """Computes cosine similarity between embedding vectors.

    Supports pluggable embedding backends. Default uses numpy for
    in-process computation. Can be swapped for FAISS or Annoy.

    Config:
      threshold: Cosine similarity threshold (0.0-1.0). Default 0.92.
      min_dim: Minimum embedding dimension (filters invalid vectors).
    """

    def __init__(
        self,
        threshold: float = 0.92,
        min_dim: int = 64,
        embed_fn: Optional[Callable[[str], np.ndarray]] = None,
    ):
        self.threshold = threshold
        self.min_dim = min_dim
        self.embed_fn = embed_fn

    def compute_similarity(self, vec_a: np.ndarray, vec_b: np.ndarray) -> float:
        """Compute cosine similarity between two vectors.

        Returns 0.0 if either vector is invalid (None, wrong dim, zero norm).
        """
        if vec_a is None or vec_b is None:
            return 0.0
        if vec_a.shape[0] < self.min_dim or vec_b.shape[0] < self.min_dim:
            return 0.0

        norm_a = np.linalg.norm(vec_a)
        norm_b = np.linalg.norm(vec_b)
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0

        return float(np.dot(vec_a, vec_b) / (norm_a * norm_b))

    def embed(self, text: str) -> Optional[np.ndarray]:
        """Generate embedding for text if embed_fn is set."""
        if self.embed_fn is None:
            return None
        try:
            vec = self.embed_fn(text)
            if isinstance(vec, np.ndarray) and vec.shape[0] >= self.min_dim:
                return vec
        except Exception as e:
            logger.warning("Embedding generation failed: %s", e)
        return None

    def find_nearest(
        self,
        query_vec: np.ndarray,
        candidates: List[ContentFingerprint],
        top_k: int = 5,
    ) -> List[Tuple[ContentFingerprint, float]]:
        """Find top-k nearest fingerprints by cosine similarity."""
        if query_vec is None or not candidates:
            return []

        scored = []
        for fp in candidates:
            if fp.embedding is None:
                continue
            sim = self.compute_similarity(query_vec, fp.embedding)
            if sim >= self.threshold:
                scored.append((fp, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


# ── Memory Dedup Engine ────────────────────────────────────────────

class MemoryDedupEngine:
    """Two-tier memory deduplication engine.

    Tier 1: Content hash (O(1) lookup via hash → fingerprint index)
    Tier 2: Semantic similarity (cosine over embedding vectors)

    Usage:
        engine = MemoryDedupEngine(strategy=DedupStrategy.STRICT)
        engine.set_embedding_fn(my_embed_fn)  # Optional

        # Single check
        result = engine.check("mem_001", "The cat sat on the mat")
        if result.decision == DedupDecision.UNIQUE:
            engine.register("mem_001", "The cat sat on the mat")

        # Batch check
        items = [("mem_001", "text 1"), ("mem_002", "text 2")]
        results = engine.check_batch(items)
    """

    def __init__(
        self,
        strategy: DedupStrategy = DedupStrategy.STRICT,
        similarity_threshold: float = 0.92,
        max_fingerprints: int = 100000,
    ):
        self._lock = threading.RLock()
        self.strategy = strategy
        self.max_fingerprints = max_fingerprints

        # Hash → Fingerprint ID index (Tier 1)
        self._hash_index: Dict[str, str] = {}

        # Fingerprint ID → ContentFingerprint
        self._fingerprints: OrderedDict[str, ContentFingerprint] = OrderedDict()

        # Semantic engine (Tier 2)
        self._sim_engine = SemanticSimilarityEngine(threshold=similarity_threshold)

        # Statistics
        self._stats = DedupStats()

        # Dedup decision log
        self._decision_log: List[DedupResult] = []
        self._log_max_size = 50000

    # ── Embedding Function ──────────────────────────────────────────

    def set_embedding_fn(self, fn: Callable[[str], np.ndarray]) -> None:
        """Set the embedding function for semantic similarity."""
        self._sim_engine.embed_fn = fn
        logger.info("Embedding function registered for dedup engine")

    # ── Tier 1: Content Hash Check ──────────────────────────────────

    def _hash_check(self, memory_id: str, content: str) -> Optional[DedupResult]:
        """Check for exact content hash match. Returns result if duplicate found."""
        ch = compute_content_hash(content)
        if ch in self._hash_index:
            fp_id = self._hash_index[ch]
            fp = self._fingerprints.get(fp_id)
            matched_id = fp.memory_id if fp else fp_id
            return DedupResult(
                memory_id=memory_id,
                decision=DedupDecision.EXACT_DUPLICATE,
                matched_fingerprint_id=fp_id,
                similarity_score=1.0,
                reason=f"Exact content hash match with {matched_id}",
                suggested_action="skip" if self.strategy != DedupStrategy.MERGE else "merge",
            )
        return None

    # ── Tier 2: Semantic Similarity Check ───────────────────────────

    def _similarity_check(
        self,
        memory_id: str,
        content: str,
        embedding: Optional[np.ndarray] = None,
    ) -> Optional[DedupResult]:
        """Check for near-duplicate via semantic similarity.

        Only runs if Tier 1 found no exact match.
        """
        if self._sim_engine.embed_fn is None and embedding is None:
            return None  # No embedding capability — skip semantic check

        if embedding is None:
            embedding = self._sim_engine.embed(content)

        if embedding is None:
            return None

        candidates = list(self._fingerprints.values())
        nearest = self._sim_engine.find_nearest(embedding, candidates, top_k=1)

        if nearest:
            fp, sim = nearest[0]
            return DedupResult(
                memory_id=memory_id,
                decision=DedupDecision.NEAR_DUPLICATE,
                matched_fingerprint_id=fp.fingerprint_id,
                similarity_score=round(sim, 6),
                reason=f"Semantic near-duplicate: similarity={sim:.4f} with {fp.memory_id}",
                suggested_action="review" if self.strategy == DedupStrategy.FLAG_ONLY else "skip",
            )

        return None

    # ── Core Check ──────────────────────────────────────────────────

    def check(
        self,
        memory_id: str,
        content: str,
        embedding: Optional[np.ndarray] = None,
    ) -> DedupResult:
        """Check if content is a duplicate of an existing fingerprint.

        Returns DedupResult with decision and suggested action.

        Args:
            memory_id: Unique ID of the memory being checked
            content: Raw text content
            embedding: Pre-computed embedding vector (optional)
        """
        with self._lock:
            self._stats.total_checked += 1
            t_hash_start = time.perf_counter_ns()

            # Tier 1: Exact hash
            result = self._hash_check(memory_id, content)
            self._stats.total_hash_lookups_ns += (time.perf_counter_ns() - t_hash_start)

            if result:
                self._record_result(result)
                self._stats.exact_duplicates += 1
                return result

            # Tier 2: Semantic similarity (only if not exact match)
            if self.strategy in (DedupStrategy.STRICT, DedupStrategy.FLAG_ONLY):
                t_sim_start = time.perf_counter()
                result = self._similarity_check(memory_id, content, embedding)
                self._stats.total_similarity_compute_ms += (
                    (time.perf_counter() - t_sim_start) * 1000
                )

                if result:
                    self._record_result(result)
                    self._stats.near_duplicates += 1
                    return result

            # No duplicate
            result = DedupResult(
                memory_id=memory_id,
                decision=DedupDecision.UNIQUE,
                reason="No duplicate found (hash + semantic)",
                suggested_action="store",
            )
            self._record_result(result)
            self._stats.unique += 1
        return result

    # ── Registration ────────────────────────────────────────────────

    def register(
        self,
        memory_id: str,
        content: str,
        embedding: Optional[np.ndarray] = None,
    ) -> ContentFingerprint:
        """Register a new content fingerprint after confirming uniqueness.

        Returns the created fingerprint. Auto-evicts oldest entries
        when max_fingerprints is reached.
        """
        with self._lock:
            ch = compute_content_hash(content)
            fp = ContentFingerprint(
                fingerprint_id=hashlib.sha256(
                    f"{memory_id}|{ch}|{time.time()}".encode()
                ).hexdigest()[:20],
                content_hash=ch,
                normalized_text=normalize_content(content),
                embedding=embedding,
                memory_id=memory_id,
            )
            self._hash_index[ch] = fp.fingerprint_id
            self._fingerprints[fp.fingerprint_id] = fp

            # Evict oldest if over limit
            while len(self._fingerprints) > self.max_fingerprints:
                oldest_id, oldest_fp = self._fingerprints.popitem(last=False)
                if oldest_fp.content_hash in self._hash_index:
                    del self._hash_index[oldest_fp.content_hash]
                logger.debug("Evicted fingerprint: %s", oldest_id)

            logger.debug("Registered fingerprint: %s (hash=%s)", fp.fingerprint_id, ch[:12])
        return fp

    # ── Bulk Operations ─────────────────────────────────────────────

    def check_batch(
        self,
        items: List[Tuple[str, str]],
        embeddings: Optional[List[Optional[np.ndarray]]] = None,
    ) -> List[DedupResult]:
        """Check multiple (memory_id, content) pairs in a batch.

        Args:
            items: List of (memory_id, content) tuples
            embeddings: Optional list of pre-computed embeddings

        Returns:
            List of DedupResult, one per input item, in order.
        """
        results = []
        for i, (mid, content) in enumerate(items):
            emb = embeddings[i] if embeddings and i < len(embeddings) else None
            results.append(self.check(mid, content, embedding=emb))
        return results

    def register_batch(
        self,
        items: List[Tuple[str, str]],
        embeddings: Optional[List[Optional[np.ndarray]]] = None,
    ) -> Tuple[List[ContentFingerprint], DedupStats]:
        """Check AND register a batch of items.

        For each item: check for duplicate → register if unique.
        Returns (fingerprints, stats).
        """
        registered = []
        for i, (mid, content) in enumerate(items):
            emb = embeddings[i] if embeddings and i < len(embeddings) else None
            result = self.check(mid, content, embedding=emb)
            if result.decision == DedupDecision.UNIQUE:
                fp = self.register(mid, content, embedding=emb)
                registered.append(fp)
        return registered, self.get_stats()

    # ── Query ───────────────────────────────────────────────────────

    def get_fingerprint(self, fingerprint_id: str) -> Optional[ContentFingerprint]:
        """Get a fingerprint by ID."""
        return self._fingerprints.get(fingerprint_id)

    def get_fingerprint_count(self) -> int:
        """Return current number of stored fingerprints."""
        return len(self._fingerprints)

    def get_stats(self) -> DedupStats:
        """Return aggregated dedup statistics (shallow copy)."""
        with self._lock:
            return DedupStats(
                total_checked=self._stats.total_checked,
                unique=self._stats.unique,
                exact_duplicates=self._stats.exact_duplicates,
                near_duplicates=self._stats.near_duplicates,
                merged=self._stats.merged,
                errors=self._stats.errors,
                total_hash_lookups_ns=self._stats.total_hash_lookups_ns,
                total_similarity_compute_ms=self._stats.total_similarity_compute_ms,
            )

    def get_decision_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent dedup decisions as dicts."""
        log = self._decision_log[-limit:]
        return [
            {
                "memory_id": r.memory_id,
                "decision": r.decision.name,
                "matched_fingerprint_id": r.matched_fingerprint_id,
                "similarity_score": r.similarity_score,
                "reason": r.reason,
            }
            for r in log
        ]

    # ── Bulk Cleanup ────────────────────────────────────────────────

    def clear_fingerprints(self) -> None:
        """Clear all fingerprints (for testing / reset)."""
        with self._lock:
            self._hash_index.clear()
            self._fingerprints.clear()
            logger.info("Cleared all dedup fingerprints")

    # ── Internal ────────────────────────────────────────────────────

    def _record_result(self, result: DedupResult) -> None:
        """Record a decision in the log."""
        self._decision_log.append(result)
        if len(self._decision_log) > self._log_max_size:
            self._decision_log = self._decision_log[-self._log_max_size:]

    def statistics(self) -> Dict[str, Any]:
        """Return runtime statistics."""
        with self._lock:
            return {
                "fingerprint_count": len(self._fingerprints),
                "hash_index_size": len(self._hash_index),
                "similarity_threshold": self._sim_engine.threshold,
                "strategy": self.strategy.value,
                "max_fingerprints": self.max_fingerprints,
                "has_embedding_fn": self._sim_engine.embed_fn is not None,
                "stats": {
                    "total_checked": self._stats.total_checked,
                    "unique": self._stats.unique,
                    "exact_duplicates": self._stats.exact_duplicates,
                    "near_duplicates": self._stats.near_duplicates,
                    "merged": self._stats.merged,
                },
            }


# ── Module Self-Test ───────────────────────────────────────────────

def self_test() -> Dict[str, Any]:
    """Run module self-test."""
    results = []

    # 1. Content normalization
    norm = normalize_content("  Hello   World  ")
    results.append(("normalization", norm == "hello world"))

    # 2. Content hashing consistency
    h1 = compute_content_hash("same text")
    h2 = compute_content_hash("same text")
    results.append(("hash_consistency", h1 == h2))

    # 3. Hash sensitivity
    h3 = compute_content_hash("different text")
    results.append(("hash_sensitivity", h1 != h3))

    # 4. Engine - exact duplicate detection
    engine = MemoryDedupEngine(strategy=DedupStrategy.EXACT_ONLY)
    engine.register("mem_001", "The quick brown fox jumps over the lazy dog")
    r = engine.check("mem_002", "The quick brown fox jumps over the lazy dog")
    results.append(("exact_duplicate", r.decision == DedupDecision.EXACT_DUPLICATE))

    # 5. Engine - unique detection
    r2 = engine.check("mem_003", "A completely different sentence here")
    results.append(("unique_detection", r2.decision == DedupDecision.UNIQUE))

    # 6. Semantic similarity (use vectors above min_dim=64)
    engine2 = MemoryDedupEngine(strategy=DedupStrategy.STRICT, similarity_threshold=0.85)
    rng = np.random.RandomState(42)
    vec_a_sem = rng.randn(128).astype(np.float64)
    vec_b_sem = vec_a_sem + rng.randn(128).astype(np.float64) * 0.02  # tiny noise
    engine2.register("sem_001", "original content", embedding=vec_a_sem)
    r3 = engine2.check("sem_002", "similar content", embedding=vec_b_sem)
    results.append(("semantic_similarity", r3.decision == DedupDecision.NEAR_DUPLICATE))

    # 7. Batch check (register between checks to catch duplicates)
    engine3 = MemoryDedupEngine(strategy=DedupStrategy.EXACT_ONLY)
    items = [("b1", "alpha"), ("b2", "beta"), ("b3", "alpha")]
    results_batch: List[DedupResult] = []
    for mid, content in items:
        r_check = engine3.check(mid, content)
        if r_check.decision == DedupDecision.UNIQUE:
            engine3.register(mid, content)
        results_batch.append(r_check)
    decisions = [r.decision for r in results_batch]
    results.append(("batch_check", decisions == [
        DedupDecision.UNIQUE,
        DedupDecision.UNIQUE,
        DedupDecision.EXACT_DUPLICATE,
    ]))

    # 8. Statistics (engine has 1 fingerprint; engine3 from batch has 2)
    stats = engine3.statistics()
    results.append(("statistics", stats["fingerprint_count"] >= 2 and stats["stats"]["exact_duplicates"] >= 1))

    passed = all(r[1] for r in results)
    return {
        "PASS": passed,
        "details": {name: "PASS" if ok else "FAIL" for name, ok in results},
        "total": len(results),
        "passed_count": sum(1 for _, ok in results if ok),
    }


if __name__ == "__main__":
    import sys
    result = self_test()
    print(f"SELFTEST_RESULT: {json.dumps(result, indent=2)}")
    sys.exit(0 if result["PASS"] else 1)
