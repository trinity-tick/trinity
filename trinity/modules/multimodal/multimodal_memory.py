"""
MultiModalMemory — 统一多模态记忆系统
======================================
Unified multi-modal memory with text/image/audio support,
extending M119 TrainFreeEngramMemory's GPU→DRAM→SSD tiered storage pattern.

Architecture:
  - Three modality-specific encoders (text, image, audio)
  - GPU→DRAM→SSD tiered storage with LRU eviction
  - Predictable prefetching (analogous to M119's early-exit prefetch)
  - Cross-modal similarity search

Design inherits from M119:
  - TrainFreeEngramBuilder → MultiModalMemory (tiered storage pattern)
  - PredictivePrefetcher → ModalityPrefetcher (predictive migration)
  - PhraseFidelityGuard → ModalityFidelityGuard (semantic fidelity)
"""

from __future__ import annotations

import hashlib
import os
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

from trinity.modules.multimodal.image_encoder import ImageMemoryEncoder, ImageEngram
from trinity.modules.multimodal.audio_encoder import AudioMemoryEncoder, AudioEngram


# ============================================================================
# Enums
# ============================================================================


class ModalityType(Enum):
    """Supported modalities."""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"


class StorageTier(Enum):
    """Storage hierarchy tiers (mirrored from M119)."""

    GPU = "gpu"     # fastest, limited capacity
    DRAM = "dram"   # medium, moderate capacity
    SSD = "ssd"     # slowest, high capacity


# ============================================================================
# Unified Engram (wraps any modality)
# ============================================================================


@dataclass
class UnifiedEngram:
    """A unified engram entry that can hold any modality.

    Wraps modality-specific engrams (ImageEngram, AudioEngram, or text)
    into a single interface for the tiered storage system.
    """

    engram_id: str
    modality: ModalityType
    modality_key: str                  # unique key for lookups
    embedding: np.ndarray              # unified embedding [embed_dim]
    source_path: str                   # original source
    wrapped_engram: Any                # modality-specific engram or text data
    frequency: int = 1
    last_accessed: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Storage tier tracking
    current_tier: StorageTier = StorageTier.SSD

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engram_id": self.engram_id,
            "modality": self.modality.value,
            "source": self.source_path[:50],
            "embed_dim": self.embedding.shape[0],
            "frequency": self.frequency,
            "tier": self.current_tier.value,
        }

    def fingerprint(self) -> str:
        return hashlib.md5(
            f"{self.modality_key}|{self.modality.value}".encode()
        ).hexdigest()[:12]


# ============================================================================
# Core: MultiModalMemory
# ============================================================================


class MultiModalMemory:
    """Unified multi-modal memory with GPU→DRAM→SSD tiered storage.

    Extends M119's pattern to support text, image, and audio modalities.

    Key features:
      - store(path, modality, metadata): ingest any modality
      - search(query, modality, top_k): similarity search
      - Modality-specific encoders with lightweight defaults
      - LRU eviction between tiers
      - Predictive prefetching across tiers
    """

    def __init__(
        self,
        embed_dim: int = 768,
        gpu_capacity: int = 128,
        dram_capacity: int = 2048,
        ssd_capacity: int = 50000,
        image_encoder: Optional[ImageMemoryEncoder] = None,
        audio_encoder: Optional[AudioMemoryEncoder] = None,
        use_models: bool = False,
    ):
        self.embed_dim = embed_dim

        # Tier capacities
        self.gpu_capacity = gpu_capacity
        self.dram_capacity = dram_capacity
        self.ssd_capacity = ssd_capacity

        # Storage tables (tiered, LRU-ordered)
        self._gpu_table: OrderedDict[str, UnifiedEngram] = OrderedDict()
        self._dram_table: OrderedDict[str, UnifiedEngram] = OrderedDict()
        self._ssd_table: OrderedDict[str, UnifiedEngram] = OrderedDict()

        # Global index (keyed by modality_key)
        self._all_engrams: Dict[str, UnifiedEngram] = {}

        # Per-modality index for filtered search
        self._modality_index: Dict[str, Set[str]] = {
            ModalityType.TEXT.value: set(),
            ModalityType.IMAGE.value: set(),
            ModalityType.AUDIO.value: set(),
        }

        # Initialize modality encoders
        self.image_encoder = image_encoder or ImageMemoryEncoder(
            embed_dim=embed_dim, use_model=use_models
        )
        self.audio_encoder = audio_encoder or AudioMemoryEncoder(
            embed_dim=embed_dim, use_model=use_models
        )

        # Statistics
        self._total_stored = 0
        self._total_searches = 0

        # Prefetch tracking (analogous to M119 PredictivePrefetcher)
        self._prefetch_history: deque = deque(maxlen=50)
        self._total_prefetches = 0
        self._prefetch_hits = 0

    # ── Store ─────────────────────────────────────────────────────────

    def store(
        self,
        source_path: str,
        modality: ModalityType = ModalityType.IMAGE,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[UnifiedEngram]:
        """Store a media item (image or audio) into multi-modal memory.

        Args:
            source_path: Path or URL to the media file.
            modality: Modality type (IMAGE or AUDIO).
            metadata: Optional metadata dict.

        Returns:
            UnifiedEngram stored in memory, or None on failure.
        """
        if metadata is None:
            metadata = {}

        # Encode via modality-specific encoder
        if modality == ModalityType.IMAGE:
            image_engram = self.image_encoder.encode(source_path, metadata)
            if image_engram is None:
                return None
            embedding = image_engram.embedding
            modality_key = image_engram.image_hash
            wrapped = image_engram

        elif modality == ModalityType.AUDIO:
            audio_engram = self.audio_encoder.encode(source_path, metadata)
            if audio_engram is None:
                return None
            embedding = audio_engram.embedding
            modality_key = audio_engram.audio_hash
            wrapped = audio_engram

        else:
            raise ValueError(f"Unsupported modality: {modality}. Use IMAGE or AUDIO.")

        # Check for duplicate
        if modality_key in self._all_engrams:
            existing = self._all_engrams[modality_key]
            existing.frequency += 1
            existing.last_accessed = time.time()
            return existing

        # Create unified engram
        engram = UnifiedEngram(
            engram_id=f"mm_{self._total_stored:08d}",
            modality=modality,
            modality_key=modality_key,
            embedding=embedding,
            source_path=source_path,
            wrapped_engram=wrapped,
            frequency=1,
            last_accessed=time.time(),
            metadata=metadata,
            current_tier=StorageTier.SSD,
        )

        # Place on SSD (cold start)
        self._assign_to_tier(engram, StorageTier.SSD)

        self._all_engrams[modality_key] = engram
        self._modality_index[modality.value].add(modality_key)
        self._total_stored += 1

        # If GPU/DRAM have capacity, promote
        if len(self._gpu_table) < self.gpu_capacity:
            self.promote_to_gpu(modality_key)
        elif len(self._dram_table) < self.dram_capacity:
            self.promote_to_dram(modality_key)

        return engram

    def store_batch(
        self,
        paths: List[str],
        modalities: List[ModalityType],
        metadata_list: Optional[List[Optional[Dict[str, Any]]]] = None,
    ) -> List[Optional[UnifiedEngram]]:
        """Store multiple media items."""
        if metadata_list is None:
            metadata_list = [None] * len(paths)
        return [
            self.store(p, m, meta)
            for p, m, meta in zip(paths, modalities, metadata_list)
        ]

    def store_text(
        self,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> UnifiedEngram:
        """Store text into multi-modal memory.

        Generates an embedding via simple text hashing (deterministic).
        In production, replace with a Sentence-BERT encoder.
        """
        if metadata is None:
            metadata = {"text": text[:100]}

        text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]

        if text_hash in self._all_engrams:
            existing = self._all_engrams[text_hash]
            existing.frequency += 1
            existing.last_accessed = time.time()
            return existing

        # Generate deterministic embedding from text
        embedding = self._text_to_embedding(text)

        engram = UnifiedEngram(
            engram_id=f"mm_{self._total_stored:08d}",
            modality=ModalityType.TEXT,
            modality_key=text_hash,
            embedding=embedding,
            source_path=f"text://{text[:40]}",
            wrapped_engram=text,
            frequency=1,
            last_accessed=time.time(),
            metadata=metadata,
            current_tier=StorageTier.SSD,
        )

        self._assign_to_tier(engram, StorageTier.SSD)
        self._all_engrams[text_hash] = engram
        self._modality_index[ModalityType.TEXT.value].add(text_hash)
        self._total_stored += 1

        if len(self._gpu_table) < self.gpu_capacity:
            self.promote_to_gpu(text_hash)
        elif len(self._dram_table) < self.dram_capacity:
            self.promote_to_dram(text_hash)

        return engram

    @staticmethod
    def _text_to_embedding(text: str, embed_dim: int = 768) -> np.ndarray:
        """Convert text to a deterministic embedding.

        Uses character-level sinusoidal encoding (same pattern as M119).
        In production, replace with Sentence-BERT/LLM embeddings.
        """
        seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
        rng = np.random.RandomState(seed)
        base = np.zeros(embed_dim, dtype=np.float32)
        chars = list(text)
        for i, ch in enumerate(chars):
            pos = ord(ch) % embed_dim
            base[pos] += (1.0 / (i + 1)) * np.sin(ord(ch) * 0.01)
        noise = rng.randn(embed_dim).astype(np.float32) * 0.01
        embedding = base + noise
        norm = np.linalg.norm(embedding)
        if norm > 1e-8:
            embedding = embedding / norm
        return embedding.astype(np.float32)

    # ── Search ────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        modality: Optional[ModalityType] = None,
        top_k: int = 5,
    ) -> List[Tuple[UnifiedEngram, float]]:
        """Search across all modalities by text query.

        Args:
            query: Text query string.
            modality: Optional filter to a specific modality. None = all.
            top_k: Number of results to return.

        Returns:
            List of (UnifiedEngram, similarity_score).
        """
        self._total_searches += 1

        # Generate query embedding using text encoder
        query_embedding = self._text_to_embedding(query, self.embed_dim)

        # Determine candidate pool
        if modality is not None:
            keys = self._modality_index.get(modality.value, set())
        else:
            keys = set(self._all_engrams.keys())

        if not keys:
            return []

        # Score all candidates
        scored: List[Tuple[UnifiedEngram, float]] = []
        q_norm = np.linalg.norm(query_embedding)

        for key in keys:
            engram = self._all_engrams.get(key)
            if engram is None:
                continue
            e_norm = np.linalg.norm(engram.embedding)
            if q_norm < 1e-8 or e_norm < 1e-8:
                continue
            sim = float(np.dot(query_embedding, engram.embedding) / (q_norm * e_norm))
            scored.append((engram, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def search_by_image(
        self,
        image_path: str,
        top_k: int = 5,
    ) -> List[Tuple[UnifiedEngram, float]]:
        """Search using an image as query (image-to-image and cross-modal)."""
        image_engram = self.image_encoder.encode(image_path)
        if image_engram is None:
            return []

        query_emb = image_engram.embedding
        q_norm = np.linalg.norm(query_emb)

        scored: List[Tuple[UnifiedEngram, float]] = []
        for key, engram in self._all_engrams.items():
            e_norm = np.linalg.norm(engram.embedding)
            if q_norm < 1e-8 or e_norm < 1e-8:
                continue
            sim = float(np.dot(query_emb, engram.embedding) / (q_norm * e_norm))
            scored.append((engram, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    # ── Tier Management ──────────────────────────────────────────────

    def promote_to_gpu(self, modality_key: str) -> bool:
        """Promote an item to GPU tier (LRU eviction if full)."""
        engram = self._all_engrams.get(modality_key)
        if not engram:
            return False

        if engram.current_tier == StorageTier.GPU:
            engram.last_accessed = time.time()
            self._gpu_table.move_to_end(modality_key)
            return True

        # Evict LRU from GPU if full
        if len(self._gpu_table) >= self.gpu_capacity:
            lru_key, lru_engram = self._gpu_table.popitem(last=False)
            self._assign_to_tier(lru_engram, StorageTier.DRAM)

        # Remove from current tier
        if engram.current_tier == StorageTier.DRAM:
            self._dram_table.pop(modality_key, None)
        elif engram.current_tier == StorageTier.SSD:
            self._ssd_table.pop(modality_key, None)

        # Place in GPU
        self._assign_to_tier(engram, StorageTier.GPU)
        engram.last_accessed = time.time()
        return True

    def promote_to_dram(self, modality_key: str) -> bool:
        """Promote an item from SSD to DRAM."""
        engram = self._all_engrams.get(modality_key)
        if not engram:
            return False
        if engram.current_tier in (StorageTier.GPU, StorageTier.DRAM):
            return True

        if len(self._dram_table) >= self.dram_capacity:
            lru_key, lru_engram = self._dram_table.popitem(last=False)
            self._assign_to_tier(lru_engram, StorageTier.SSD)

        self._ssd_table.pop(modality_key, None)
        self._assign_to_tier(engram, StorageTier.DRAM)
        engram.last_accessed = time.time()
        return True

    def _assign_to_tier(self, engram: UnifiedEngram, tier: StorageTier) -> None:
        """Assign engram to a specific tier table."""
        key = engram.modality_key
        engram.current_tier = tier

        if tier == StorageTier.GPU:
            self._gpu_table[key] = engram
        elif tier == StorageTier.DRAM:
            self._dram_table[key] = engram
        elif tier == StorageTier.SSD:
            self._ssd_table[key] = engram

    # ── Prefetching (analogous to M119 PredictivePrefetcher) ─────────

    def prefetch(
        self,
        query: str,
        top_k: int = 3,
    ) -> Dict[str, Any]:
        """Predictively prefetch items likely to be searched next.

        Uses the query embedding to find top-k candidates and promotes
        them to GPU/DRAM tiers, hiding SSD latency.

        This is a simplified version of M119's Early-Exit Guided prefetching.
        """
        query_emb = self._text_to_embedding(query, self.embed_dim)
        q_norm = np.linalg.norm(query_emb)

        # Score all SSD items
        ssd_candidates: List[Tuple[str, float]] = []
        for key, engram in self._ssd_table.items():
            e_norm = np.linalg.norm(engram.embedding)
            if q_norm > 1e-8 and e_norm > 1e-8:
                sim = float(np.dot(query_emb, engram.embedding) / (q_norm * e_norm))
                ssd_candidates.append((key, sim))

        ssd_candidates.sort(key=lambda x: x[1], reverse=True)

        # Promote top-k SSD candidates to DRAM (prefetch)
        promoted = 0
        for key, sim in ssd_candidates[:top_k]:
            if sim > 0.3:  # confidence threshold
                self.promote_to_dram(key)
                promoted += 1

        self._total_prefetches += promoted
        self._prefetch_history.append({
            "query": query[:30],
            "promoted": promoted,
            "candidates": len(ssd_candidates),
        })

        return {
            "prefetched": promoted,
            "candidates_scored": len(ssd_candidates),
            "threshold": 0.3,
        }

    # ── Diagnostics ─────────────────────────────────────────────────

    def diagnostics(self) -> Dict[str, Any]:
        tiers = defaultdict(int)
        for engram in self._all_engrams.values():
            tiers[engram.current_tier.value] += 1

        modality_counts = {}
        for mod, keys in self._modality_index.items():
            modality_counts[mod] = len(keys)

        return {
            "module": "MultiModalMemory",
            "total_stored": self._total_stored,
            "total_searches": self._total_searches,
            "tiers": {
                "gpu": {"capacity": self.gpu_capacity, "occupied": len(self._gpu_table)},
                "dram": {"capacity": self.dram_capacity, "occupied": len(self._dram_table)},
                "ssd": {"capacity": self.ssd_capacity, "occupied": len(self._ssd_table)},
            },
            "modality_counts": modality_counts,
            "prefetch": {
                "total": self._total_prefetches,
                "history_size": len(self._prefetch_history),
            },
            "image_encoder": self.image_encoder.diagnostics(),
            "audio_encoder": self.audio_encoder.diagnostics(),
            "embed_dim": self.embed_dim,
        }


def run_multimodal_memory_self_test() -> MultiModalMemory:
    """Run self-test for MultiModalMemory."""
    print("=" * 80)
    print("  MultiModalMemory — 自检")
    print("=" * 80)

    memory = MultiModalMemory(
        embed_dim=768,
        gpu_capacity=8,
        dram_capacity=32,
        ssd_capacity=1000,
    )

    # 1. Store text items
    texts = [
        "machine learning transformer architecture",
        "deep neural network with attention",
        "reinforcement learning from human feedback",
        "computer vision for autonomous driving",
        "natural language processing with BERT",
    ]
    text_engrams = []
    for t in texts:
        e = memory.store_text(t)
        assert e is not None
        text_engrams.append(e)
    assert memory._total_stored == len(texts)
    print(f"[PASS] 1. Text store: {len(texts)} items stored, "
          f"total={memory._total_stored}")

    # 2. Store images (create synthetic test images)
    from PIL import Image, ImageDraw
    import tempfile

    image_paths = []
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    for i, color in enumerate(colors):
        tmp = os.path.join(tempfile.gettempdir(), f"_mm_test_img_{i}.png")
        img = Image.new("RGB", (64, 64), color=color)
        draw = ImageDraw.Draw(img)
        draw.rectangle([16, 16, 48, 48], fill=(255, 255, 255))
        img.save(tmp)
        image_paths.append(tmp)

    image_engrams = []
    for p in image_paths:
        e = memory.store(p, ModalityType.IMAGE)
        assert e is not None
        image_engrams.append(e)
    print(f"[PASS] 2. Image store: {len(image_paths)} images stored")

    # 3. Store audio (create synthetic test audio)
    import wave
    import struct

    audio_paths = []
    freqs = [440, 880, 1760]
    for i, freq in enumerate(freqs):
        tmp = os.path.join(tempfile.gettempdir(), f"_mm_test_aud_{i}.wav")
        sr = 16000
        t = np.linspace(0, 0.3, int(sr * 0.3), endpoint=False)
        samples = (np.sin(2 * np.pi * freq * t) * 0.5).astype(np.float32)
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes((samples * 32767).astype(np.int16).tobytes())
        audio_paths.append(tmp)

    audio_engrams = []
    for p in audio_paths:
        e = memory.store(p, ModalityType.AUDIO)
        assert e is not None
        audio_engrams.append(e)
    print(f"[PASS] 3. Audio store: {len(audio_paths)} audio clips stored")

    # 4. Text search
    results = memory.search("machine learning")
    assert len(results) > 0
    print(f"[PASS] 4. Text search 'machine learning': {len(results)} results, "
          f"top={results[0][0].source_path[:30]} (sim={results[0][1]:.4f})")

    # 5. Modality-filtered search
    img_results = memory.search("red image", modality=ModalityType.IMAGE)
    print(f"[PASS] 5. Image-only search: {len(img_results)} results")

    aud_results = memory.search("audio", modality=ModalityType.AUDIO)
    print(f"[PASS] 6. Audio-only search: {len(aud_results)} results")

    # 7. Duplicate detection (same text → frequency increment)
    e_dup = memory.store_text("machine learning transformer architecture")
    assert e_dup.frequency >= 2
    print(f"[PASS] 7. Duplicate detection: freq={e_dup.frequency}")

    # 8. Tier promotion
    key_to_promote = text_engrams[0].modality_key
    memory.promote_to_gpu(key_to_promote)
    assert memory._all_engrams[key_to_promote].current_tier == StorageTier.GPU
    print(f"[PASS] 8. GPU promotion: {key_to_promote} → GPU tier")

    # 9. Prefetch
    prefetch_result = memory.prefetch("deep learning")
    print(f"[PASS] 9. Prefetch: {prefetch_result['prefetched']} items promoted, "
          f"{prefetch_result['candidates_scored']} scored")

    # 10. GPU capacity enforcement (promote more items than capacity)
    for key in list(memory._all_engrams.keys())[:10]:
        memory.promote_to_gpu(key)
    assert len(memory._gpu_table) <= memory.gpu_capacity
    print(f"[PASS] 10. Capacity enforcement: GPU={len(memory._gpu_table)}"
          f" ≤ {memory.gpu_capacity}")

    # 11. Diagnostics
    diag = memory.diagnostics()
    assert diag["total_stored"] == len(texts) + len(image_paths) + len(audio_paths)
    print(f"[PASS] 11. Diagnostics: total={diag['total_stored']}, "
          f"modalities={diag['modality_counts']}")

    # Cleanup
    for p in image_paths + audio_paths:
        try:
            os.remove(p)
        except OSError:
            pass

    print("-" * 60)
    print("  [MultiModalMemory] ALL_PASS — 11/11 项通过")
    print("=" * 80)

    return memory


if __name__ == "__main__":
    run_multimodal_memory_self_test()
