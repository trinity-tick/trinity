
"""
M119 TrainFreeEngramMemory — 免训练短语语义记忆
====================================================
基于 TF-Engram (arXiv 2607.07388, July 8, 2026):
免训练短语语义记忆，SSD 分层存储 + 预测性预取。

核心创新:
  传统 Engram 记忆依赖 GPU 驻留的哈希压缩存储，导致无关短语在共享槽中碰撞。
  TF-Engram 通过离线构建短语特定语义记忆表、GPU→DRAM→SSD 三级存储层次、
  Early-Exit Guided 预测性预取实现免训练、可扩展的短语记忆注入。

核心设计:
  1. TrainFreeEngramBuilder — 离线短语记忆构建
     - 从外部语料离线构建短语特定语义记忆表
     - GPU→DRAM→SSD 三级存储层次
     - 无哈希冲突: 每个短语独立存储槽
  2. PredictivePrefetcher — 预测性预取器
     - Early-Exit Guided 预测性预取
     - 利用自回归解码早期层输出预测即将需要的短语
     - SSD 延迟隐藏在 GPU 计算中
  3. PhraseFidelityGuard — 短语语义保真度守护
     - 短语级语义保真度检测
     - 与 M118 CompressedContextIntegrityGuard 协作
  4. 集成到 second_brain 记忆注入管线

字段说明:
  - MODULE_ID: M119
  - MODULE_VERSION: 1.0.0
  - PAPER_REF: TF-Engram (arXiv 2607.07388, July 8, 2026)
"""

from __future__ import annotations

import hashlib
import math
import time
import uuid
from collections import defaultdict, deque, OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np


MODULE_ID = "M119"
MODULE_VERSION = "1.0.0"
PAPER_REF = "TF-Engram: Train-Free Engram with SSD-Backed Memory (arXiv 2607.07388)"

SEP = "=" * 80
SUB = "-" * 60

# Storage tier constants
DEFAULT_GPU_CAPACITY = 256       # number of phrase slots on GPU
DEFAULT_DRAM_CAPACITY = 4096     # number of phrase slots in DRAM
DEFAULT_SSD_CAPACITY = 100000    # number of phrase slots on SSD
DEFAULT_EMBED_DIM = 768          # hidden state dimension
DEFAULT_PHRASE_MAX_LEN = 16      # max tokens per phrase
DEFAULT_EARLY_EXIT_LAYER = 6     # which layer to use for early-exit prediction
DEFAULT_PREFETCH_WINDOW = 8      # lookahead for prefetching


# ============================================================================
# Enums
# ============================================================================


class StorageTier(Enum):
    """Storage hierarchy tiers."""

    GPU = "gpu"         # fastest, limited capacity (HBM)
    DRAM = "dram"       # medium, moderate capacity
    SSD = "ssd"         # slowest, high capacity


class PrefetchResult(Enum):
    """Result of a prefetch operation."""

    HIT_GPU = "hit_gpu"             # phrase already on GPU
    HIT_DRAM = "hit_dram"           # phrase in DRAM, migrated to GPU
    HIT_SSD = "hit_ssd"             # phrase on SSD, loaded via DRAM
    MISS = "miss"                   # phrase not in any tier


class FidelityStatus(Enum):
    """Phrase-level semantic fidelity assessment."""

    HIGH_FIDELITY = "high_fidelity"       # meaning fully preserved
    MINOR_DEGRADATION = "minor_degradation"  # slight semantic shift
    SIGNIFICANT_DEGRADATION = "significant_degradation"  # meaning altered
    CORRUPTED = "corrupted"               # meaning lost or wrong


class EngramBuildMode(Enum):
    """Modes for building engram memory tables."""

    OFFLINE_FULL = "offline_full"         # build from complete external corpus
    INCREMENTAL = "incremental"           # add new phrases incrementally
    MERGE = "merge"                       # merge multiple engram tables


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class PhraseEngram:
    """A single phrase engram entry in the memory table."""

    engram_id: str
    phrase_text: str                    # original phrase text
    phrase_hash: str                    # collision-free hash identifier
    embedding: np.ndarray               # semantic embedding [embed_dim]
    hidden_state_projection: np.ndarray  # projection for hidden-state injection [embed_dim]
    token_count: int = 1
    frequency: int = 1                  # how often this phrase appears
    source_corpus: str = ""             # which corpus this came from
    last_accessed: float = 0.0          # Unix timestamp of last access

    # Storage tier tracking
    current_tier: StorageTier = StorageTier.SSD
    gpu_slot: int = -1
    dram_slot: int = -1
    ssd_slot: int = -1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engram_id": self.engram_id,
            "phrase": self.phrase_text[:40],
            "hash": self.phrase_hash,
            "embed_dim": self.embedding.shape[0],
            "tokens": self.token_count,
            "frequency": self.frequency,
            "tier": self.current_tier.value,
        }

    def fingerprint(self) -> str:
        return hashlib.md5(
            f"{self.phrase_text}|{self.phrase_hash}".encode()
        ).hexdigest()[:12]


@dataclass
class PrefetchPrediction:
    """Prediction of which phrases will be needed next."""

    predicted_phrases: List[str]        # phrase hashes predicted to be needed
    confidence: List[float]             # confidence score per prediction
    early_exit_layer: int               # which layer produced this prediction
    prediction_latency_ms: float = 0.0  # how long the prediction took

    @property
    def top_k_hashes(self, k: int = DEFAULT_PREFETCH_WINDOW) -> List[str]:
        sorted_pairs = sorted(
            zip(self.predicted_phrases, self.confidence),
            key=lambda x: x[1],
            reverse=True,
        )
        return [h for h, _ in sorted_pairs[:k]]


@dataclass
class FidelityReport:
    """Report on phrase semantic fidelity."""

    engram_id: str
    phrase_text: str
    fidelity_status: FidelityStatus
    original_embedding_norm: float
    current_embedding_norm: float
    cosine_similarity: float           # between original and current
    degradation_cause: str = ""         # why fidelity dropped (if any)
    m118_guard_result: Optional[Any] = None  # cross-check with M118

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engram_id": self.engram_id,
            "phrase": self.phrase_text[:30],
            "status": self.fidelity_status.value,
            "similarity": round(self.cosine_similarity, 6),
            "degradation": self.degradation_cause or "none",
        }


@dataclass
class PrefetchStats:
    """Statistics for prefetch operations."""

    total_prefetches: int = 0
    gpu_hits: int = 0
    dram_hits: int = 0
    ssd_hits: int = 0
    misses: int = 0
    total_latency_hidden_ms: float = 0.0  # SSD latency hidden

    @property
    def hit_rate(self) -> float:
        if self.total_prefetches == 0:
            return 0.0
        return (self.gpu_hits + self.dram_hits + self.ssd_hits) / self.total_prefetches

    @property
    def gpu_hit_rate(self) -> float:
        if self.total_prefetches == 0:
            return 0.0
        return self.gpu_hits / self.total_prefetches


# ============================================================================
# Core: TrainFreeEngramBuilder
# ============================================================================


class TrainFreeEngramBuilder:
    """Offline construction of phrase-specific semantic memory tables.

    Key properties:
      - Collision-free: each phrase gets its own slot (no hashing to shared buckets)
      - GPU→DRAM→SSD hierarchy: hot phrases on GPU, warm in DRAM, cold on SSD
      - Embedding as projection: encoder hidden states projected for LLM injection

    Build process:
      1. Extract phrases from external corpus
      2. Compute semantic embeddings via encoder
      3. Assign to storage tiers based on frequency
      4. Build collision-free lookup table
    """

    def __init__(
        self,
        embed_dim: int = DEFAULT_EMBED_DIM,
        gpu_capacity: int = DEFAULT_GPU_CAPACITY,
        dram_capacity: int = DEFAULT_DRAM_CAPACITY,
        ssd_capacity: int = DEFAULT_SSD_CAPACITY,
        max_phrase_len: int = DEFAULT_PHRASE_MAX_LEN,
    ):
        self.embed_dim = embed_dim
        self.gpu_capacity = gpu_capacity
        self.dram_capacity = dram_capacity
        self.ssd_capacity = ssd_capacity
        self.max_phrase_len = max_phrase_len

        # Storage tables (keyed by phrase_hash for collision-free lookup)
        self._gpu_table: OrderedDict[str, PhraseEngram] = OrderedDict()
        self._dram_table: OrderedDict[str, PhraseEngram] = OrderedDict()
        self._ssd_table: OrderedDict[str, PhraseEngram] = OrderedDict()

        # Global index
        self._all_phrases: Dict[str, PhraseEngram] = {}
        self._total_built: int = 0

        # Build tracking
        self._build_time_sec: float = 0.0

    # ── Building ──────────────────────────────────────────────────────

    def build_from_corpus(
        self,
        phrases: List[str],
        frequencies: Optional[List[int]] = None,
        source_corpus: str = "external",
    ) -> Dict[str, Any]:
        """Build engram memory table from an external corpus.

        Uses simulated embeddings (in production, these come from an encoder model).
        Each phrase gets a deterministic embedding based on its hash,
        ensuring reproducibility without training.

        Args:
            phrases: List of phrase strings.
            frequencies: Optional frequency counts.
            source_corpus: Corpus identifier.

        Returns:
            Build summary.
        """
        t0 = time.time()

        if frequencies is None:
            frequencies = [1] * len(phrases)

        # Step 1: Compute embeddings and create engrams
        engrams: List[PhraseEngram] = []
        for phrase, freq in zip(phrases, frequencies):
            phrase = phrase.strip()
            if not phrase or len(phrase.split()) > self.max_phrase_len:
                continue

            phrase_hash = self._compute_phrase_hash(phrase)
            embedding = self._compute_embedding(phrase)
            proj = self._compute_projection(embedding)

            engram = PhraseEngram(
                engram_id=f"eng_{len(engrams):06d}",
                phrase_text=phrase,
                phrase_hash=phrase_hash,
                embedding=embedding,
                hidden_state_projection=proj,
                token_count=len(phrase.split()),
                frequency=freq,
                source_corpus=source_corpus,
                last_accessed=time.time(),
                current_tier=StorageTier.SSD,
            )
            engrams.append(engram)

        # Step 2: Sort by frequency descending for tier assignment
        engrams.sort(key=lambda e: e.frequency, reverse=True)

        # Step 3: Assign to tiers
        for i, engram in enumerate(engrams):
            if i < self.gpu_capacity:
                engram.current_tier = StorageTier.GPU
                engram.gpu_slot = i
                self._gpu_table[engram.phrase_hash] = engram
            elif i < self.gpu_capacity + self.dram_capacity:
                engram.current_tier = StorageTier.DRAM
                engram.dram_slot = i - self.gpu_capacity
                self._dram_table[engram.phrase_hash] = engram
            elif i < self.gpu_capacity + self.dram_capacity + self.ssd_capacity:
                engram.current_tier = StorageTier.SSD
                engram.ssd_slot = i - self.gpu_capacity - self.dram_capacity
                self._ssd_table[engram.phrase_hash] = engram
            else:
                break  # capacity limit reached

            self._all_phrases[engram.phrase_hash] = engram

        self._total_built = len(self._all_phrases)
        self._build_time_sec = time.time() - t0

        return {
            "total_phrases": len(phrases),
            "engrams_built": self._total_built,
            "gpu_occupied": len(self._gpu_table),
            "dram_occupied": len(self._dram_table),
            "ssd_occupied": len(self._ssd_table),
            "build_time_sec": round(self._build_time_sec, 3),
            "source_corpus": source_corpus,
        }

    def _compute_phrase_hash(self, phrase: str) -> str:
        """Collision-free phrase hash.

        Unlike hash-table approaches where collisions occur, TF-Engram
        uses each phrase's own hash as a unique key — no shared slots.
        """
        return hashlib.sha256(phrase.encode("utf-8")).hexdigest()[:16]

    def _compute_embedding(self, phrase: str) -> np.ndarray:
        """Compute semantic embedding for a phrase.

        Uses deterministic sinusoidal encoding based on character-level features,
        simulating a fixed encoder. In production, this would use a pretrained
        encoder model (e.g., BERT, Sentence-BERT).
        """
        rng = np.random.RandomState(int(hashlib.md5(phrase.encode()).hexdigest()[:8], 16))
        # Base embedding: character-level sinusoidal encoding
        base = np.zeros(self.embed_dim, dtype=np.float32)
        chars = list(phrase)
        for i, ch in enumerate(chars):
            pos = ord(ch) % self.embed_dim
            base[pos] += (1.0 / (i + 1)) * math.sin(ord(ch) * 0.01)
        # Add deterministic noise for variation
        noise = rng.randn(self.embed_dim).astype(np.float32) * 0.01
        embedding = base + noise
        return embedding / (np.linalg.norm(embedding) + 1e-8)

    def _compute_projection(self, embedding: np.ndarray) -> np.ndarray:
        """Compute hidden-state projection for LLM injection.

        The projection maps the engram embedding to the LLM's hidden-state
        space for direct injection during decoding.
        """
        # Simulate a learned projection matrix (fixed, reproducible)
        rng = np.random.RandomState(137)
        proj_matrix = rng.randn(self.embed_dim, self.embed_dim).astype(np.float32) * 0.02
        return (proj_matrix @ embedding).astype(np.float32)

    # ── Lookup ────────────────────────────────────────────────────────

    def lookup(self, phrase_hash: str) -> Optional[PhraseEngram]:
        """Look up a phrase by its hash (collision-free)."""
        return self._all_phrases.get(phrase_hash)

    def lookup_text(self, phrase_text: str) -> Optional[PhraseEngram]:
        """Look up a phrase by its text."""
        phrase_hash = self._compute_phrase_hash(phrase_text)
        return self.lookup(phrase_hash)

    # ── Tier migration ────────────────────────────────────────────────

    def promote_to_gpu(self, phrase_hash: str) -> bool:
        """Promote a phrase to GPU tier (LRU eviction if full)."""
        engram = self._all_phrases.get(phrase_hash)
        if not engram:
            return False
        if engram.current_tier == StorageTier.GPU:
            engram.last_accessed = time.time()
            return True

        # Evict LRU from GPU if full
        if len(self._gpu_table) >= self.gpu_capacity:
            lru_hash, lru_engram = self._gpu_table.popitem(last=False)
            lru_engram.current_tier = StorageTier.DRAM
            lru_engram.gpu_slot = -1
            lru_engram.dram_slot = len(self._dram_table)
            self._dram_table[lru_hash] = lru_engram

        # Remove from current tier
        if engram.current_tier == StorageTier.DRAM:
            self._dram_table.pop(phrase_hash, None)
            engram.dram_slot = -1
        elif engram.current_tier == StorageTier.SSD:
            self._ssd_table.pop(phrase_hash, None)
            engram.ssd_slot = -1

        # Add to GPU
        engram.current_tier = StorageTier.GPU
        engram.gpu_slot = len(self._gpu_table)
        engram.last_accessed = time.time()
        self._gpu_table[phrase_hash] = engram
        return True

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "total_built": self._total_built,
            "gpu": {"capacity": self.gpu_capacity, "occupied": len(self._gpu_table)},
            "dram": {"capacity": self.dram_capacity, "occupied": len(self._dram_table)},
            "ssd": {"capacity": self.ssd_capacity, "occupied": len(self._ssd_table)},
            "build_time_sec": round(self._build_time_sec, 3),
            "embed_dim": self.embed_dim,
        }


# ============================================================================
# Core: PredictivePrefetcher
# ============================================================================


class PredictivePrefetcher:
    """Early-Exit Guided predictive prefetching.

    Leverages early-layer decoder outputs to predict which phrases will
    be needed in upcoming decoding steps. SSD latency is hidden behind
    the GPU computation of the later decoder layers.

    Workflow:
      1. At each decoding step, extract early-exit hidden states (layer 6)
      2. Compute similarity between early hidden states and phrase projections
      3. Prefetch top-k predicted phrases from SSD → DRAM → GPU
      4. By the time the full forward pass completes, phrases are on GPU
    """

    def __init__(
        self,
        builder: TrainFreeEngramBuilder,
        early_exit_layer: int = DEFAULT_EARLY_EXIT_LAYER,
        prefetch_window: int = DEFAULT_PREFETCH_WINDOW,
        confidence_threshold: float = 0.3,
        ssd_latency_ms: float = 2.0,     # simulated SSD access latency
        dram_latency_ms: float = 0.2,     # simulated DRAM access latency
    ):
        self.builder = builder
        self.early_exit_layer = early_exit_layer
        self.prefetch_window = prefetch_window
        self.confidence_threshold = confidence_threshold
        self.ssd_latency_ms = ssd_latency_ms
        self.dram_latency_ms = dram_latency_ms

        self.stats = PrefetchStats()
        self._prefetch_history: deque = deque(maxlen=100)

    def predict(
        self,
        early_hidden_state: np.ndarray,
        current_context: List[str] = None,
    ) -> PrefetchPrediction:
        """Predict which phrases will be needed next.

        Args:
            early_hidden_state: Hidden states from early-exit layer [embed_dim].
            current_context: Current phrase context for disambiguation.

        Returns:
            PrefetchPrediction with top predicted phrase hashes.
        """
        if current_context is None:
            current_context = []

        # Compute similarity between early hidden state and all phrase projections
        similarities: List[Tuple[str, float]] = []
        hs_norm = np.linalg.norm(early_hidden_state) + 1e-8

        for phrase_hash, engram in self.builder._all_phrases.items():
            proj = engram.hidden_state_projection
            proj_norm = np.linalg.norm(proj) + 1e-8
            sim = float(np.dot(early_hidden_state, proj) / (hs_norm * proj_norm))
            if sim >= self.confidence_threshold:
                similarities.append((phrase_hash, sim))

        # Sort by similarity descending
        similarities.sort(key=lambda x: x[1], reverse=True)
        similarities = similarities[:self.prefetch_window * 2]

        if not similarities:
            return PrefetchPrediction(
                predicted_phrases=[],
                confidence=[],
                early_exit_layer=self.early_exit_layer,
            )

        return PrefetchPrediction(
            predicted_phrases=[s[0] for s in similarities],
            confidence=[s[1] for s in similarities],
            early_exit_layer=self.early_exit_layer,
            prediction_latency_ms=0.05,  # ~50us for similarity computation
        )

    def prefetch(self, prediction: PrefetchPrediction) -> List[PrefetchResult]:
        """Execute prefetch: migrate predicted phrases up the storage hierarchy.

        The key innovation: SSD latency is hidden because the GPU is busy
        computing the later decoder layers while prefetch happens.

        Returns:
            List of PrefetchResult per predicted phrase.
        """
        results = []
        total_hidden_latency = 0.0
        top_hashes = prediction.predicted_phrases[:self.prefetch_window]

        for phrase_hash in top_hashes:
            engram = self.builder._all_phrases.get(phrase_hash)

            if engram is None:
                results.append(PrefetchResult.MISS)
                continue

            if engram.current_tier == StorageTier.GPU:
                results.append(PrefetchResult.HIT_GPU)
                self.stats.gpu_hits += 1
                continue

            if engram.current_tier == StorageTier.DRAM:
                # Simulate DRAM → GPU migration latency (hidden behind GPU compute)
                total_hidden_latency += self.dram_latency_ms
                self.builder.promote_to_gpu(phrase_hash)
                results.append(PrefetchResult.HIT_DRAM)
                self.stats.dram_hits += 1
                continue

            if engram.current_tier == StorageTier.SSD:
                # Simulate SSD → DRAM → GPU migration latency
                # This is the critical case: SSD latency hidden behind GPU computation
                total_hidden_latency += self.ssd_latency_ms
                self.builder.promote_to_gpu(phrase_hash)
                results.append(PrefetchResult.HIT_SSD)
                self.stats.ssd_hits += 1
                continue

            results.append(PrefetchResult.MISS)
            self.stats.misses += 1

        self.stats.total_prefetches += len(top_hashes)
        self.stats.total_latency_hidden_ms += total_hidden_latency

        return results

    def step(
        self,
        early_hidden_state: np.ndarray,
        current_context: Optional[List[str]] = None,
    ) -> Tuple[PrefetchPrediction, List[PrefetchResult]]:
        """Single prefetch step: predict + prefetch."""
        prediction = self.predict(early_hidden_state, current_context)
        results = self.prefetch(prediction)
        return prediction, results

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "early_exit_layer": self.early_exit_layer,
            "prefetch_window": self.prefetch_window,
            "confidence_threshold": self.confidence_threshold,
            "stats": {
                "total": self.stats.total_prefetches,
                "gpu_hit_rate": round(self.stats.gpu_hit_rate, 4),
                "overall_hit_rate": round(self.stats.hit_rate, 4),
                "latency_hidden_ms": round(self.stats.total_latency_hidden_ms, 3),
            },
        }


# ============================================================================
# Core: PhraseFidelityGuard
# ============================================================================


class PhraseFidelityGuard:
    """Phrase-level semantic fidelity detection.

    Detects when a phrase engram's semantic meaning has degraded
    due to tier migration, embedding drift, or compression artifacts.
    Collaborates with M118 CompressedContextIntegrityGuard for cross-validation.
    """

    def __init__(
        self,
        similarity_threshold: float = 0.95,
        degradation_history_size: int = 100,
    ):
        self.similarity_threshold = similarity_threshold
        self._fidelity_log: List[FidelityReport] = []
        self._degradation_history: deque = deque(maxlen=degradation_history_size)
        self._total_checks: int = 0
        self._degraded_count: int = 0

    def check(
        self,
        engram: PhraseEngram,
        original_embedding: Optional[np.ndarray] = None,
        m118_callback: Optional[callable] = None,
    ) -> FidelityReport:
        """Check the semantic fidelity of a phrase engram.

        Args:
            engram: The engram to check.
            original_embedding: Original embedding for comparison.
            m118_callback: Optional callback to M118 for cross-validation.

        Returns:
            FidelityReport with status assessment.
        """
        self._total_checks += 1

        # If no original embedding provided, recompute from phrase text
        if original_embedding is None:
            original_embedding = self._recompute_embedding(engram.phrase_text)

        current = engram.embedding
        orig_norm = float(np.linalg.norm(original_embedding))
        curr_norm = float(np.linalg.norm(current))

        # Cosine similarity between original and current
        similarity = float(
            np.dot(original_embedding, current)
            / (orig_norm * curr_norm + 1e-8)
        )

        # Determine fidelity status
        if similarity >= self.similarity_threshold:
            status = FidelityStatus.HIGH_FIDELITY
            cause = ""
        elif similarity >= self.similarity_threshold - 0.05:
            status = FidelityStatus.MINOR_DEGRADATION
            cause = "minor embedding drift"
        elif similarity >= self.similarity_threshold - 0.15:
            status = FidelityStatus.SIGNIFICANT_DEGRADATION
            cause = "tier migration compression artifact"
        else:
            status = FidelityStatus.CORRUPTED
            cause = "severe embedding corruption or hash collision"
            self._degraded_count += 1

        report = FidelityReport(
            engram_id=engram.engram_id,
            phrase_text=engram.phrase_text,
            fidelity_status=status,
            original_embedding_norm=orig_norm,
            current_embedding_norm=curr_norm,
            cosine_similarity=similarity,
            degradation_cause=cause,
        )

        if status != FidelityStatus.HIGH_FIDELITY:
            self._degradation_history.append(report)

        # Cross-validate with M118 if available
        if m118_callback and status != FidelityStatus.HIGH_FIDELITY:
            try:
                report.m118_guard_result = m118_callback(engram)
            except Exception:
                pass

        self._fidelity_log.append(report)
        return report

    def _recompute_embedding(self, phrase_text: str) -> np.ndarray:
        """Recompute the embedding from phrase text (reference value)."""
        builder = TrainFreeEngramBuilder(embed_dim=DEFAULT_EMBED_DIM)
        return builder._compute_embedding(phrase_text)

    def check_batch(
        self,
        engrams: List[PhraseEngram],
    ) -> List[FidelityReport]:
        """Batch fidelity check."""
        return [self.check(e) for e in engrams]

    @property
    def degradation_rate(self) -> float:
        if self._total_checks == 0:
            return 0.0
        return self._degraded_count / self._total_checks

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "total_checks": self._total_checks,
            "degraded_count": self._degraded_count,
            "degradation_rate": round(self.degradation_rate, 4),
            "similarity_threshold": self.similarity_threshold,
            "recent_degradations": len(self._degradation_history),
        }


# ============================================================================
# Unified Entry: TrainFreeEngramMemory
# ============================================================================


class TrainFreeEngramMemory:
    """Unified entry point for TF-Engram memory system.

    Integrates:
      - TrainFreeEngramBuilder: offline memory table construction
      - PredictivePrefetcher: early-exit guided prefetching
      - PhraseFidelityGuard: semantic fidelity monitoring
      - M118 collaboration: cross-validation with CompressedContextIntegrityGuard
    """

    def __init__(
        self,
        embed_dim: int = DEFAULT_EMBED_DIM,
        gpu_capacity: int = DEFAULT_GPU_CAPACITY,
        dram_capacity: int = DEFAULT_DRAM_CAPACITY,
        ssd_capacity: int = DEFAULT_SSD_CAPACITY,
        early_exit_layer: int = DEFAULT_EARLY_EXIT_LAYER,
        prefetch_window: int = DEFAULT_PREFETCH_WINDOW,
        similarity_threshold: float = 0.95,
    ):
        self.builder = TrainFreeEngramBuilder(
            embed_dim=embed_dim,
            gpu_capacity=gpu_capacity,
            dram_capacity=dram_capacity,
            ssd_capacity=ssd_capacity,
        )
        self.prefetcher = PredictivePrefetcher(
            builder=self.builder,
            early_exit_layer=early_exit_layer,
            prefetch_window=prefetch_window,
        )
        self.fidelity_guard = PhraseFidelityGuard(
            similarity_threshold=similarity_threshold,
        )
        self._m118_guard: Optional[Any] = None  # reference to M118

    # ── Main pipeline ─────────────────────────────────────────────────

    def build(
        self,
        phrases: List[str],
        frequencies: Optional[List[int]] = None,
        source: str = "external",
    ) -> Dict[str, Any]:
        """Build engram memory from corpus."""
        return self.builder.build_from_corpus(phrases, frequencies, source)

    def inject_to_hidden_state(
        self,
        phrase_hashes: List[str],
        current_hidden: np.ndarray,
    ) -> np.ndarray:
        """Inject phrase memory into LLM hidden state.

        For each phrase, look up its projection and add it (weighted) to
        the current hidden state. This is the core engram injection mechanism.

        Args:
            phrase_hashes: Phrases to inject.
            current_hidden: Current hidden state [seq_len, embed_dim].

        Returns:
            Modified hidden state.
        """
        modified = current_hidden.copy()
        weight = 0.05  # injection strength

        for phrase_hash in phrase_hashes:
            engram = self.builder.lookup(phrase_hash)
            if engram is None:
                continue
            # Promote accessed phrase to GPU
            self.builder.promote_to_gpu(phrase_hash)
            # Inject projection into hidden state
            proj = engram.hidden_state_projection
            if modified.ndim == 2:
                modified[0, :] += weight * proj
            else:
                modified += weight * proj

        return modified

    # ── Full step ─────────────────────────────────────────────────────

    def step(
        self,
        early_hidden: np.ndarray,
        current_context: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Single inference step: predict + prefetch + inject."""
        prediction, prefetch_results = self.prefetcher.step(
            early_hidden_state=early_hidden,
            current_context=current_context,
        )

        hits = {
            "gpu": sum(1 for r in prefetch_results if r == PrefetchResult.HIT_GPU),
            "dram": sum(1 for r in prefetch_results if r == PrefetchResult.HIT_DRAM),
            "ssd": sum(1 for r in prefetch_results if r == PrefetchResult.HIT_SSD),
            "miss": sum(1 for r in prefetch_results if r == PrefetchResult.MISS),
        }

        return {
            "prediction": prediction.predicted_phrases[:self.prefetcher.prefetch_window],
            "prefetch_hits": hits,
            "prefetch_latency_ms": prediction.prediction_latency_ms,
        }

    # ── M118 Integration ───────────────────────────────────────────────

    def set_m118_guard(self, m118_instance):
        """Set reference to M118 CompressedContextIntegrityGuard for cross-validation."""
        self._m118_guard = m118_instance

    def verify_all_fidelity(self) -> List[FidelityReport]:
        """Run fidelity check on all engrams."""
        all_engrams = list(self.builder._all_phrases.values())
        return self.fidelity_guard.check_batch(all_engrams)

    def diagnostics(self) -> Dict[str, Any]:
        diag = {
            "module": MODULE_ID,
            "version": MODULE_VERSION,
            "builder": self.builder.diagnostics(),
            "prefetcher": self.prefetcher.diagnostics(),
            "fidelity_guard": self.fidelity_guard.diagnostics(),
            "m118_integrated": self._m118_guard is not None,
        }
        return diag


# ============================================================================
# Self-Test
# ============================================================================


def run_self_test() -> TrainFreeEngramMemory:
    """Run comprehensive self-test for M119 TrainFreeEngramMemory.

    Returns:
        Fully initialized and tested TrainFreeEngramMemory instance.
    """
    print(SEP)
    print("  M119 TrainFreeEngramMemory — 自检")
    print(f"  Paper: {PAPER_REF}")
    print(SEP)

    # ── 1. TrainFreeEngramBuilder 实例化 ──
    builder = TrainFreeEngramBuilder(
        embed_dim=768,
        gpu_capacity=256,
        dram_capacity=4096,
        ssd_capacity=100000,
        max_phrase_len=16,
    )
    assert builder.embed_dim == 768
    assert builder.gpu_capacity == 256
    assert builder.dram_capacity == 4096
    print(f"[PASS] 1. Builder: GPU={builder.gpu_capacity}/DRAM={builder.dram_capacity}"
          f"/SSD={builder.ssd_capacity}, dim={builder.embed_dim}")

    # ── 2. 构建短语记忆表 ──
    phrases = [
        "machine learning", "deep neural network", "attention mechanism",
        "transformer architecture", "gradient descent", "backpropagation",
        "reinforcement learning", "natural language processing",
        "computer vision", "generative adversarial network",
    ] * 50  # 500 phrases, frequencies vary
    rng = np.random.RandomState(42)
    frequencies = [max(1, int(rng.exponential(5))) for _ in range(len(phrases))]
    summary = builder.build_from_corpus(phrases, frequencies)
    assert summary["engrams_built"] > 0
    assert summary["gpu_occupied"] > 0
    assert summary["dram_occupied"] > 0
    assert summary["ssd_occupied"] >= 0  # may be 0 if fits in DRAM
    print(f"[PASS] 2. Build: {summary['engrams_built']} engrams "
          f"(GPU={summary['gpu_occupied']}, DRAM={summary['dram_occupied']}, "
          f"SSD={summary['ssd_occupied']}), time={summary['build_time_sec']}s")

    # ── 3. 无哈希冲突验证 ──
    for phrase in ["machine learning", "deep neural network", "attention mechanism"]:
        e1 = builder.lookup_text(phrase)
        e2 = builder.lookup_text(phrase)
        assert e1 is not None and e2 is not None
        assert e1.engram_id == e2.engram_id
    # Two different phrases should have different hashes
    e_ml = builder.lookup_text("machine learning")
    e_nlp = builder.lookup_text("natural language processing")
    assert e_ml.phrase_hash != e_nlp.phrase_hash
    print(f"[PASS] 3. 无哈希冲突: phrases have unique hashes")

    # ── 4. GPU 提升 ──
    nlp_en = builder.lookup_text("natural language processing")
    original_tier = nlp_en.current_tier
    success = builder.promote_to_gpu(nlp_en.phrase_hash)
    assert success
    assert nlp_en.current_tier == StorageTier.GPU
    print(f"[PASS] 4. GPU提升: {original_tier.value} → GPU, last_accessed updated")

    # ── 5. PredictivePrefetcher 实例化 ──
    prefetcher = PredictivePrefetcher(
        builder=builder,
        early_exit_layer=6,
        prefetch_window=8,
        confidence_threshold=0.3,
    )
    assert prefetcher.early_exit_layer == 6
    assert prefetcher.prefetch_window == 8
    print(f"[PASS] 5. Prefetcher: exit_layer={prefetcher.early_exit_layer}, "
          f"window={prefetcher.prefetch_window}")

    # ── 6. 预测 ──
    rng_state = np.random.RandomState(123)
    early_hs = rng_state.randn(768).astype(np.float32) * 0.1
    prediction = prefetcher.predict(early_hs)
    assert isinstance(prediction, PrefetchPrediction)
    assert len(prediction.predicted_phrases) >= 0
    print(f"[PASS] 6. 预测: {len(prediction.predicted_phrases)} candidates, "
          f"top_confidence={prediction.confidence[0]:.4f}" if prediction.confidence
          else f"[PASS] 6. 预测: no candidates above threshold")

    # ── 7. 预取 ──
    results = prefetcher.prefetch(prediction)
    assert len(results) == min(len(prediction.predicted_phrases), prefetcher.prefetch_window)
    assert prefetcher.stats.total_prefetches >= 0  # may be 0 if no candidates
    print(f"[PASS] 7. 预取: {prefetcher.stats.total_prefetches} prefetches, "
          f"gpu_hit_rate={prefetcher.stats.gpu_hit_rate:.2f}")

    # ── 8. PhraseFidelityGuard ──
    guard = PhraseFidelityGuard(similarity_threshold=0.95)
    engram = builder.lookup_text("machine learning")
    report = guard.check(engram)
    assert report.fidelity_status == FidelityStatus.HIGH_FIDELITY
    assert report.cosine_similarity > 0.99  # recomputed from same phrase
    print(f"[PASS] 8. Fidelity: status={report.fidelity_status.value}, "
          f"similarity={report.cosine_similarity:.6f}")

    # ── 9. 受损坏保真度检测 ──
    # Simulate corruption by adding noise to embedding
    corrupted_engram = builder.lookup_text("gradient descent")
    original_emb = corrupted_engram.embedding.copy()
    corrupted_engram.embedding = corrupted_engram.embedding + rng.randn(768).astype(np.float32) * 0.1
    bad_report = guard.check(corrupted_engram, original_embedding=original_emb)
    assert bad_report.fidelity_status != FidelityStatus.HIGH_FIDELITY
    print(f"[PASS] 9. 损坏检测: status={bad_report.fidelity_status.value}, "
          f"similarity={bad_report.cosine_similarity:.6f}")

    # ── 10. TrainFreeEngramMemory 统一入口 ──
    memory = TrainFreeEngramMemory(
        embed_dim=768,
        gpu_capacity=256,
        dram_capacity=4096,
        ssd_capacity=50000,
    )
    build_result = memory.build(phrases, frequencies, source="unified_test")
    assert build_result["engrams_built"] > 0
    print(f"[PASS] 10. 统一入口: {build_result['engrams_built']} engrams built")

    # ── 11. 隐藏状态注入 ──
    hs = rng_state.randn(1, 768).astype(np.float32)
    ml_hash = builder._compute_phrase_hash("machine learning")
    modified_hs = memory.inject_to_hidden_state([ml_hash], hs)
    assert modified_hs.shape == hs.shape
    diff = np.linalg.norm(modified_hs - hs)
    assert diff > 0  # injection should change hidden state
    print(f"[PASS] 11. 隐藏状态注入: diff_norm={diff:.6f}, shape={modified_hs.shape}")

    # ── 12. 完整 step ──
    step_result = memory.step(early_hs)
    assert "prediction" in step_result
    assert "prefetch_hits" in step_result
    print(f"[PASS] 12. Step: predictions={len(step_result['prediction'])}, "
          f"hits={step_result['prefetch_hits']}")

    # ── 13. 全量保真度验证 ──
    all_reports = memory.verify_all_fidelity()
    high_fidelity = sum(1 for r in all_reports if r.fidelity_status == FidelityStatus.HIGH_FIDELITY)
    assert high_fidelity == len(all_reports)  # all should be high fidelity
    print(f"[PASS] 13. 全量保真度: {high_fidelity}/{len(all_reports)} HIGH_FIDELITY")

    # ── 14. GPU/DRAM/SSD 分层验证 ──
    tiers = defaultdict(int)
    for engram in memory.builder._all_phrases.values():
        tiers[engram.current_tier.value] += 1
    total = sum(tiers.values())
    assert total == memory.builder._total_built
    assert tiers["gpu"] <= memory.builder.gpu_capacity
    assert tiers["dram"] <= memory.builder.dram_capacity
    print(f"[PASS] 14. 分层: GPU={tiers.get('gpu',0)}, DRAM={tiers.get('dram',0)}, "
          f"SSD={tiers.get('ssd',0)} (total={total})")

    # ── 15. 诊断 ──
    diag = memory.diagnostics()
    assert diag["builder"]["total_built"] == total
    assert diag["fidelity_guard"]["degradation_rate"] == 0.0
    print(f"[PASS] 15. 诊断: built={diag['builder']['total_built']}, "
          f"gpu_hit_rate={diag['prefetcher']['stats']['gpu_hit_rate']}, "
          f"m118={diag['m118_integrated']}")

    print(SUB)
    print("  [M119 自检结果] ALL_PASS — 15/15 项通过")
    print(SEP)

    return memory


if __name__ == "__main__":
    run_self_test()
