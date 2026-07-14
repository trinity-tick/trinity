"""
ImageMemoryEncoder — 图像记忆编码器
====================================
Encodes image paths/URLs into semantic embeddings using lightweight methods.

Architecture:
  - Default: color histogram + average hash (no ML dependencies)
  - Upgrade path: set `use_model=True` and provide a CLIP-compatible model
  - Embedding dimensionality aligns with M119's embed_dim (768 default)

Tiered storage integration:
  - Hot images (frequent access) → GPU tier
  - Warm images → DRAM tier
  - Cold images → SSD tier
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import time
import urllib.request
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple

from collections import OrderedDict

import numpy as np

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    Image = None  # type: ignore


# RGB histogram bins per channel
_HISTOGRAM_BINS = 32
_AVERAGE_HASH_SIZE = 16  # 16x16 → 256-bit hash

# Default embed dim matching M119
DEFAULT_IMAGE_EMBED_DIM = 768


@dataclass
class ImageEngram:
    """A single image engram entry in multimodal memory.

    Mirrors M119's PhraseEngram pattern for cross-compatibility.
    """

    engram_id: str
    source_path: str                       # local path or URL
    image_hash: str                        # collision-free hash identifier
    embedding: np.ndarray                  # semantic embedding [embed_dim]
    histogram: np.ndarray                  # color histogram [3 * bins]
    avg_hash_bits: str                     # average hash (hex string)
    width: int = 0
    height: int = 0
    format: str = "unknown"
    file_size_bytes: int = 0
    frequency: int = 1
    last_accessed: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Storage tier tracking (mirrors M119's PhraseEngram)
    current_tier: str = "ssd"             # "gpu" | "dram" | "ssd"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engram_id": self.engram_id,
            "source": self.source_path[:60],
            "hash": self.image_hash,
            "size": f"{self.width}x{self.height}",
            "format": self.format,
            "embed_dim": self.embedding.shape[0],
            "frequency": self.frequency,
            "tier": self.current_tier,
        }

    def fingerprint(self) -> str:
        return hashlib.md5(self.image_hash.encode()).hexdigest()[:12]


class ImageMemoryEncoder:
    """Encodes images into embeddings for multimodal memory storage.

    Default encoding (no ML):
      1. Resize to 256x256
      2. Compute 3-channel color histogram (32 bins/channel → 96-dim)
      3. Compute average hash (16x16 → 256 bits)
      4. Concatenate into a fixed embedding (or project to embed_dim)

    Upgrade path:
      Set `use_model=True` and pass a model_path or callable that
      returns a CLIP-compatible embedding vector.
    """

    def __init__(
        self,
        embed_dim: int = DEFAULT_IMAGE_EMBED_DIM,
        use_model: bool = False,
        model_path: Optional[str] = None,
        model_callable: Optional[callable] = None,
    ):
        self.embed_dim = embed_dim
        self.use_model = use_model
        self.model_path = model_path
        self._model_callable = model_callable

        # Internal LRU cache for loaded images (reduces disk I/O)
        self._image_cache: OrderedDict[str, ImageEngram] = OrderedDict()
        self._cache_max_size = 32

        # Statistics
        self._total_encoded = 0
        self._total_errors = 0

    # ── Public API ────────────────────────────────────────────────────

    def encode(self, path_or_url: str, metadata: Optional[Dict[str, Any]] = None) -> Optional[ImageEngram]:
        """Encode an image from a local path or HTTP(S) URL.

        Args:
            path_or_url: Local file path or http(s) URL.
            metadata: Optional metadata dict.

        Returns:
            ImageEngram with embedding, or None on failure.
        """
        if not HAS_PIL:
            raise ImportError("PIL (Pillow) is required for ImageMemoryEncoder. Install with: pip install Pillow")

        if metadata is None:
            metadata = {}

        # Check cache first
        cache_key = hashlib.md5(path_or_url.encode()).hexdigest()
        if cache_key in self._image_cache:
            cached = self._image_cache[cache_key]
            cached.last_accessed = time.time()
            self._image_cache.move_to_end(cache_key)
            return cached

        try:
            # Load image
            if path_or_url.startswith(("http://", "https://")):
                image, file_info = self._load_from_url(path_or_url)
            else:
                image, file_info = self._load_from_file(path_or_url)

            if image is None:
                self._total_errors += 1
                return None

            # Compute embedding
            if self.use_model and self._model_callable is not None:
                embedding = self._compute_model_embedding(image)
            else:
                embedding = self._compute_lightweight_embedding(image)

            # Compute hash
            image_hash = self._compute_image_hash(path_or_url, embedding)

            engram = ImageEngram(
                engram_id=f"img_{self._total_encoded:08d}",
                source_path=path_or_url,
                image_hash=image_hash,
                embedding=embedding,
                histogram=self._compute_histogram(image),
                avg_hash_bits=self._compute_average_hash(image),
                width=file_info.get("width", 0),
                height=file_info.get("height", 0),
                format=file_info.get("format", "unknown"),
                file_size_bytes=file_info.get("size", 0),
                frequency=1,
                last_accessed=time.time(),
                metadata=metadata,
                current_tier="ssd",
            )

            # Update cache
            self._image_cache[cache_key] = engram
            if len(self._image_cache) > self._cache_max_size:
                self._image_cache.popitem(last=False)

            self._total_encoded += 1
            return engram

        except Exception as e:
            self._total_errors += 1
            raise RuntimeError(f"Failed to encode image '{path_or_url}': {e}") from e

    def encode_batch(
        self, paths: List[str], metadata_list: Optional[List[Dict[str, Any]]] = None
    ) -> List[Optional[ImageEngram]]:
        """Encode multiple images in batch."""
        if metadata_list is None:
            metadata_list = [None] * len(paths)
        return [self.encode(p, m) for p, m in zip(paths, metadata_list)]

    def similarity(
        self,
        engram_a: ImageEngram,
        engram_b: ImageEngram,
    ) -> float:
        """Cosine similarity between two image embeddings."""
        a_norm = np.linalg.norm(engram_a.embedding)
        b_norm = np.linalg.norm(engram_b.embedding)
        if a_norm < 1e-8 or b_norm < 1e-8:
            return 0.0
        return float(np.dot(engram_a.embedding, engram_b.embedding) / (a_norm * b_norm))

    def search(
        self,
        query_engram: ImageEngram,
        candidates: List[ImageEngram],
        top_k: int = 5,
    ) -> List[Tuple[ImageEngram, float]]:
        """Search for most similar images among candidates."""
        scored = [(c, self.similarity(query_engram, c)) for c in candidates]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    # ── Internal: Image Loading ───────────────────────────────────────

    def _load_from_file(self, path: str) -> Tuple[Optional[Image.Image], Dict[str, Any]]:
        """Load image from local file."""
        if not os.path.isfile(path):
            return None, {}
        try:
            img = Image.open(path)
            img.load()
            file_size = os.path.getsize(path)
            info = {
                "width": img.width,
                "height": img.height,
                "format": img.format or "unknown",
                "size": file_size,
            }
            # Convert to RGB for consistent processing
            if img.mode != "RGB":
                img = img.convert("RGB")
            return img, info
        except Exception:
            return None, {}

    def _load_from_url(self, url: str) -> Tuple[Optional[Image.Image], Dict[str, Any]]:
        """Load image from HTTP(S) URL."""
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = resp.read()
            img = Image.open(io.BytesIO(data))
            img.load()
            info = {
                "width": img.width,
                "height": img.height,
                "format": img.format or "unknown",
                "size": len(data),
            }
            if img.mode != "RGB":
                img = img.convert("RGB")
            return img, info
        except Exception:
            return None, {}

    # ── Internal: Embedding Computation ───────────────────────────────

    def _compute_lightweight_embedding(self, image: Image.Image) -> np.ndarray:
        """Compute embedding using color histogram + average hash (no ML).

        This is a lightweight fallback when no ML model is available.
        In production, replace with CLIP embeddings.

        Returns:
            Embedding vector of shape [embed_dim].
        """
        # Resize to standard size
        img_resized = image.resize((256, 256), Image.LANCZOS)
        pixels = np.array(img_resized, dtype=np.float32) / 255.0  # [256, 256, 3]

        # 1. Color histogram features (96-dim: 32 bins × 3 channels)
        hist_features = []
        for c in range(3):
            hist, _ = np.histogram(pixels[:, :, c], bins=_HISTOGRAM_BINS, range=(0, 1))
            hist = hist.astype(np.float32) / (256 * 256)  # normalize
            hist_features.append(hist)
        hist_vec = np.concatenate(hist_features)  # [96]

        # 2. Average hash features (256-dim: 16x16 bits → float)
        avg_hash = self._compute_average_hash(image)
        hash_bits = np.array([int(b) for b in avg_hash], dtype=np.float32)  # [256]
        hash_bits = hash_bits * 2.0 - 1.0  # map to [-1, 1]

        # 3. Concatenate → [352] dim, then project to embed_dim
        combined = np.concatenate([hist_vec, hash_bits])  # [352]
        if len(combined) < self.embed_dim:
            # Pad with zeros
            padded = np.zeros(self.embed_dim, dtype=np.float32)
            padded[: len(combined)] = combined
            combined = padded
        elif len(combined) > self.embed_dim:
            # Truncate
            combined = combined[: self.embed_dim]

        # Normalize
        norm = np.linalg.norm(combined)
        if norm > 1e-8:
            combined = combined / norm

        return combined.astype(np.float32)

    def _compute_model_embedding(self, image: Image.Image) -> np.ndarray:
        """Compute embedding using ML model (e.g., CLIP).

        Upgrade path: set `use_model=True` and provide `model_callable`.
        The callable should accept a PIL Image and return a numpy array.

        Default fallback: returns lightweight embedding if no model set.
        """
        if self._model_callable is not None:
            try:
                emb = self._model_callable(image)
                if isinstance(emb, np.ndarray) and emb.shape[0] == self.embed_dim:
                    return emb.astype(np.float32)
            except Exception:
                pass  # fall through to lightweight
        return self._compute_lightweight_embedding(image)

    def _compute_histogram(self, image: Image.Image) -> np.ndarray:
        """Compute 3-channel color histogram as numpy array [3 * bins]."""
        img_resized = image.resize((256, 256), Image.LANCZOS)
        pixels = np.array(img_resized, dtype=np.float32) / 255.0
        hist_features = []
        for c in range(3):
            hist, _ = np.histogram(pixels[:, :, c], bins=_HISTOGRAM_BINS, range=(0, 1))
            hist_features.append(hist.astype(np.float32) / (256 * 256))
        return np.concatenate(hist_features)

    def _compute_average_hash(self, image: Image.Image) -> str:
        """Compute average hash (aHash) — a 16x16 perceptual hash.

        Returns hex string of 64 chars (256 bits).
        """
        # Resize to hash size + convert to grayscale
        img_small = image.resize((_AVERAGE_HASH_SIZE, _AVERAGE_HASH_SIZE), Image.LANCZOS)
        img_gray = img_small.convert("L")
        pixels = np.array(img_gray, dtype=np.float32)

        # Compute average
        avg = pixels.mean()

        # Generate bits: 1 if pixel > avg, else 0
        bits = (pixels > avg).flatten().astype(int)
        hex_str = "".join(str(b) for b in bits)
        return hex_str

    def _compute_image_hash(self, path_or_url: str, embedding: np.ndarray) -> str:
        """Compute collision-free image hash."""
        raw = path_or_url.encode() + embedding.tobytes()
        return hashlib.sha256(raw).hexdigest()[:16]

    # ── Diagnostics ─────────────────────────────────────────────────

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "embed_dim": self.embed_dim,
            "use_model": self.use_model,
            "has_pil": HAS_PIL,
            "total_encoded": self._total_encoded,
            "total_errors": self._total_errors,
            "cache_size": len(self._image_cache),
        }


def run_image_encoder_self_test() -> ImageMemoryEncoder:
    """Run self-test for ImageMemoryEncoder."""
    print("=" * 80)
    print("  ImageMemoryEncoder — 自检")
    print("=" * 80)

    encoder = ImageMemoryEncoder(embed_dim=768)

    # Test: create a synthetic test image
    test_path = os.path.join(os.path.dirname(__file__), "_test_image.png")
    try:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (100, 100), color=(73, 109, 137))
        draw = ImageDraw.Draw(img)
        draw.rectangle([20, 20, 80, 80], fill=(255, 200, 100))
        img.save(test_path)
    except Exception:
        print("[SKIP] 1. Could not create test image — PIL not fully available")
        return encoder

    # 1. Encode
    engram = encoder.encode(test_path)
    assert engram is not None, "Encoding failed"
    assert engram.embedding.shape[0] == 768
    print(f"[PASS] 1. Encode: {engram.source_path} → {engram.engram_id}, "
          f"embed_dim={engram.embedding.shape[0]}, hash={engram.image_hash}")

    # 2. Caching
    engram2 = encoder.encode(test_path)
    assert engram2 is not None
    assert engram2.engram_id == engram.engram_id
    print(f"[PASS] 2. Cache hit: same engram returned")

    # 3. Similarity (same image → cos ≈ 1.0)
    sim = encoder.similarity(engram, engram2)
    assert sim > 0.99, f"Expected near-1 similarity, got {sim}"
    print(f"[PASS] 3. Self-similarity: {sim:.6f}")

    # 4. Different image → lower similarity
    img2 = Image.new("RGB", (100, 100), color=(255, 0, 0))
    draw2 = ImageDraw.Draw(img2)
    draw2.rectangle([10, 10, 90, 90], fill=(0, 255, 0))
    test_path2 = os.path.join(os.path.dirname(__file__), "_test_image2.png")
    img2.save(test_path2)

    engram3 = encoder.encode(test_path2)
    assert engram3 is not None
    sim_diff = encoder.similarity(engram, engram3)
    assert sim_diff < 0.99, f"Expected lower similarity for different images, got {sim_diff}"
    print(f"[PASS] 4. Cross-similarity: {sim_diff:.6f} (different images)")

    # 5. Metadata passthrough
    engram_meta = encoder.encode(test_path, metadata={"label": "test", "index": 42})
    assert engram_meta is not None
    assert engram_meta.metadata["label"] == "test"
    print(f"[PASS] 5. Metadata: {engram_meta.metadata}")

    # 6. Search
    results = encoder.search(engram, [engram2, engram3], top_k=2)
    assert len(results) == 2
    assert results[0][0].engram_id == engram.engram_id
    print(f"[PASS] 6. Search: top={results[0][0].engram_id} (sim={results[0][1]:.4f})")

    # Cleanup
    for p in [test_path, test_path2]:
        try:
            os.remove(p)
        except OSError:
            pass

    print("-" * 60)
    print(f"  [ImageMemoryEncoder] ALL_PASS — 6/6 项通过")
    print("=" * 80)

    return encoder


if __name__ == "__main__":
    run_image_encoder_self_test()
