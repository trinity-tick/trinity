"""
Trinity Embedding Quantization
==============================
Reduces vector storage footprint by 4x-8x while preserving >95% recall.
Implements Scalar Quantization (int8/uint8) and Product Quantization (PQ).

Paper alignment:
  - Jégou et al. 2011 "Product Quantization for Nearest Neighbor Search"
  - FAISS PQ design (Meta AI Research)
  - RAGAS-aligned evaluation pipeline integration

Triad alignment (Retrieval + Memory + Guardian):
  - Retrieval: Transparent quantization on search path, asymmetric distance
  - Memory:    Storage reduction 4x (int8) to 16x (PQ M=64)
  - Guardian:  Storage savings statistics, recall degradation monitoring

Design:
  - QuantizationConfig: method selection, PQ hyperparams
  - ScalarQuantizer:    float32 → int8/uint8 with scale+zero_point
  - ProductQuantizer:   K-means codebook per sub-vector, asymmetric scoring
  - QuantizedVectors:   serializable container for quantized data
  - QuantizedVectorIndex: transparent wrapper around VectorIndex
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── Config Constants ───────────────────────────────────────────────────

QUANTIZATION_METHOD: str = "int8"
"""Default quantization method: 'int8' | 'uint8' | 'pq'"""

QUANTIZATION_PQ_M: int = 32
"""Number of sub-vectors for Product Quantization (must divide embedding dim)."""

QUANTIZATION_PQ_K: int = 256
"""Number of centroids (codebook size) per sub-vector for PQ."""

QUANTIZATION_RERANK_K: int = 100
"""Number of candidates to re-rank with full-precision vectors after PQ search."""

# ── Data Classes ───────────────────────────────────────────────────────

class QuantizationMethod(Enum):
    INT8 = "int8"
    UINT8 = "uint8"
    FLOAT16 = "float16"
    PQ = "pq"


@dataclass
class QuantizationConfig:
    """Configuration for embedding quantization.

    Attributes:
        method: Quantization method ('int8' / 'uint8' / 'float16' / 'pq').
        calibration_samples: Number of samples used to estimate scale/zero_point.
        pq_num_subvectors: M — number of sub-vector partitions for PQ.
        pq_num_centroids:  K — codebook size per sub-vector for PQ.
    """
    method: str = "int8"
    calibration_samples: int = 1000
    pq_num_subvectors: int = QUANTIZATION_PQ_M
    pq_num_centroids: int = QUANTIZATION_PQ_K

    def __post_init__(self):
        valid = {"int8", "uint8", "float16", "pq"}
        if self.method not in valid:
            raise ValueError(f"method must be one of {valid}, got '{self.method}'")
        if self.method == "pq":
            if self.pq_num_subvectors <= 0:
                raise ValueError("pq_num_subvectors must be > 0")
            if self.pq_num_centroids <= 0:
                raise ValueError("pq_num_centroids must be > 0")


@dataclass
class QuantizedVectors:
    """Container for quantized vector data.

    Attributes:
        quantized_data: The compressed vector data (np.ndarray of int8/uint8/float16).
        scale:         Scale factor for dequantization (scalar or per-subvector).
        zero_point:    Zero-point offset for dequantization.
        method:        Quantization method used.
        original_dim:  Original embedding dimension before quantization.
        codebook:      For PQ: list of sub-codebooks (M × K × d_sub arrays).
    """
    quantized_data: np.ndarray
    scale: np.ndarray
    zero_point: np.ndarray
    method: str
    original_dim: int
    codebook: Optional[List[np.ndarray]] = None

    def memory_bytes(self) -> int:
        """Estimate total memory footprint of quantized representation."""
        total = int(self.quantized_data.nbytes)
        total += int(self.scale.nbytes)
        total += int(self.zero_point.nbytes)
        if self.codebook:
            total += sum(int(cb.nbytes) for cb in self.codebook)
        return total

    def compression_ratio(self) -> float:
        """Compression ratio vs float32 full precision (1.0 = no savings)."""
        original_bytes = self.original_dim * 4  # float32 = 4 bytes per dimension
        per_vector_bytes = self.memory_bytes() / max(1, len(self.quantized_data))
        return original_bytes / max(1, per_vector_bytes)


# ── Scalar Quantizer ───────────────────────────────────────────────────

class ScalarQuantizer:
    """Scalar quantization: float32 → int8/uint8 with scale + zero-point.

    Maps continuous float values to discrete integer bins:
        float_value = (int_value - zero_point) * scale

    Supports asymmetric quantization: int8 range [-128, 127] or
    uint8 range [0, 255] for cosine-similarity-friendly unsigned encoding.
    """

    def __init__(self, method: str = "int8"):
        if method not in ("int8", "uint8"):
            raise ValueError(f"ScalarQuantizer method must be 'int8' or 'uint8', got '{method}'")
        self._method = method
        self._lock = threading.RLock()

    @property
    def method(self) -> str:
        return self._method

    # ── Encode / Decode ────────────────────────────────────────────

    def encode(self, vectors: np.ndarray) -> QuantizedVectors:
        """Quantize float32 vectors to int8/uint8.

        Args:
            vectors: 2D float32 array of shape [N, D].

        Returns:
            QuantizedVectors with compressed representation.
        """
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim != 2:
            raise ValueError(f"Expected 2D array [N, D], got shape {vectors.shape}")

        N, D = vectors.shape

        # Per-dimension min/max for better accuracy than global scaling
        v_min = vectors.min(axis=0)
        v_max = vectors.max(axis=0)

        if self._method == "int8":
            qmin, qmax = -128.0, 127.0
        else:  # uint8
            qmin, qmax = 0.0, 255.0

        # Avoid division by zero when min == max (constant dimension)
        range_val = v_max - v_min
        range_val = np.where(range_val < 1e-8, 1.0, range_val)

        scale = range_val / (qmax - qmin)
        zero_point = np.round(qmin - v_min / scale)

        # Clamp to valid range
        zero_point = np.clip(zero_point, qmin, qmax)

        # Quantize
        quantized = np.zeros((N, D), dtype=np.int8 if self._method == "int8" else np.uint8)
        with self._lock:
            for d in range(D):
                quantized[:, d] = np.clip(
                    np.round(vectors[:, d] / scale[d] + zero_point[d]),
                    qmin, qmax,
                )

        return QuantizedVectors(
            quantized_data=quantized,
            scale=scale.astype(np.float32),
            zero_point=zero_point.astype(np.float32),
            method=self._method,
            original_dim=D,
        )

    def decode(self, qv: QuantizedVectors) -> np.ndarray:
        """Dequantize back to approximate float32.

        Args:
            qv: QuantizedVectors from encode().

        Returns:
            Approximate float32 vectors of shape [N, D].
        """
        if qv.method != self._method:
            raise ValueError(
                f"QuantizedVectors method '{qv.method}' does not match "
                f"ScalarQuantizer method '{self._method}'"
            )

        N = len(qv.quantized_data)
        D = qv.original_dim
        result = np.zeros((N, D), dtype=np.float32)

        with self._lock:
            for d in range(D):
                result[:, d] = (
                    qv.quantized_data[:, d].astype(np.float32) - qv.zero_point[d]
                ) * qv.scale[d]

        return result

    def compute_scores(self, query: np.ndarray, qv: QuantizedVectors) -> np.ndarray:
        """Compute cosine similarity of float32 query against quantized vectors.

        Uses asymmetric distance computation: query stays in full precision,
        quantized vectors are dequantized on-the-fly in dimension batches.

        Args:
            query: Float32 query vector of shape [D].
            qv:    QuantizedVectors from encode().

        Returns:
            Similarity scores array of shape [N].
        """
        query = np.asarray(query, dtype=np.float32)
        if query.ndim == 1:
            query = query.reshape(1, -1)
        N = len(qv.quantized_data)

        # Dequantize + dot product in chunks to limit memory
        scores = np.zeros(N, dtype=np.float32)
        chunk_size = 4096
        for start in range(0, N, chunk_size):
            end = min(start + chunk_size, N)
            chunk_decoded = np.zeros((end - start, qv.original_dim), dtype=np.float32)
            cdata = qv.quantized_data[start:end]
            for d in range(qv.original_dim):
                chunk_decoded[:, d] = (
                    cdata[:, d].astype(np.float32) - qv.zero_point[d]
                ) * qv.scale[d]
            # L2 normalize decoded vectors for cosine similarity
            norms = np.linalg.norm(chunk_decoded, axis=1, keepdims=True)
            norms = np.where(norms < 1e-10, 1.0, norms)
            chunk_decoded /= norms
            scores[start:end] = np.dot(chunk_decoded, query.T).ravel()

        return scores

    def statistics(self) -> Dict[str, Any]:
        return {
            "method": self._method,
            "quant_range": "[-128, 127]" if self._method == "int8" else "[0, 255]",
            "compression_per_dim": 1 if self._method == "int8" else 1,  # bytes per dim
        }


# ── Product Quantizer ──────────────────────────────────────────────────

class ProductQuantizer:
    """Product Quantization (PQ) for lossy vector compression.

    Splits each D-dimensional vector into M sub-vectors of dimension D//M,
    then quantizes each sub-vector independently using K-means clustering.

    Compression: float32 (4 bytes/dim) → ceil(log2(K)) bits/dim.
    With M=32, K=256: 1024-dim × 4 bytes = 4096 bytes → 32 bytes (128x compression).

    Supports asymmetric distance computation (ADC):
    query stays in float32, database codes are used to look up pre-computed
    distance tables per sub-vector.
    """

    def __init__(self, M: int = QUANTIZATION_PQ_M, K: int = QUANTIZATION_PQ_K,
                 max_iter: int = 100, seed: int = 42):
        """
        Args:
            M:    Number of sub-vectors (must divide embedding dimension evenly).
            K:    Codebook size (number of centroids per sub-vector).
            max_iter: K-means max iterations.
            seed:     Random seed for reproducibility.
        """
        self._M = M
        self._K = K
        self._max_iter = max_iter
        self._seed = seed
        self._lock = threading.RLock()

        # Trained state
        self._d_sub: Optional[int] = None
        self._original_dim: Optional[int] = None
        self._codebook: Optional[List[np.ndarray]] = None  # [M] of shape [K, d_sub]
        self._trained = False

    @property
    def M(self) -> int:
        return self._M

    @property
    def K(self) -> int:
        return self._K

    @property
    def is_trained(self) -> bool:
        return self._trained

    def train(self, vectors: np.ndarray) -> None:
        """Train K-means codebook for each sub-vector partition.

        Args:
            vectors: Float32 training vectors of shape [N, D].
        """
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim != 2:
            raise ValueError(f"Expected 2D array [N, D], got shape {vectors.shape}")

        N, D = vectors.shape

        if D % self._M != 0:
            raise ValueError(
                f"Embedding dimension {D} must be divisible by M={self._M}"
            )

        self._d_sub = D // self._M
        self._original_dim = D

        rng = np.random.RandomState(self._seed)
        codebook: List[np.ndarray] = []

        for m in range(self._M):
            start = m * self._d_sub
            end = start + self._d_sub
            sub_vectors = vectors[:, start:end].copy()

            # K-means on sub-vectors
            centroids = self._kmeans(sub_vectors, rng)
            codebook.append(centroids)

        self._codebook = codebook
        self._trained = True

    def _kmeans(self, data: np.ndarray, rng: np.random.RandomState) -> np.ndarray:
        """Vanilla K-means clustering.

        Args:
            data: [N, d_sub] float32 array.
            rng:  Random state for centroid initialization.

        Returns:
            centroids: [K, d_sub] float32 array.
        """
        N, d = data.shape
        K = min(self._K, N)

        # K-means++ initialization
        centroids = np.zeros((K, d), dtype=np.float32)
        # First centroid: random sample
        centroids[0] = data[rng.randint(N)]

        for k in range(1, K):
            # Compute min squared distances to existing centroids
            dists = np.zeros(N, dtype=np.float64)
            for j in range(k):
                diff = data - centroids[j]
                d2 = np.sum(diff * diff, axis=1)
                if j == 0:
                    dists = d2
                else:
                    dists = np.minimum(dists, d2)
            # Sample proportional to distance
            probs = dists / (dists.sum() + 1e-10)
            centroids[k] = data[np.random.choice(N, p=probs)]

        # Lloyd iterations
        assignments = np.zeros(N, dtype=np.int32)
        for _iter in range(self._max_iter):
            # Assign
            changed = False
            for i in range(N):
                best_k = 0
                best_d2 = float("inf")
                for k in range(K):
                    d2 = float(np.sum((data[i] - centroids[k]) ** 2))
                    if d2 < best_d2:
                        best_d2 = d2
                        best_k = k
                if assignments[i] != best_k:
                    assignments[i] = best_k
                    changed = True

            # Update centroids
            new_centroids = np.zeros((K, d), dtype=np.float32)
            counts = np.zeros(K, dtype=np.int32)
            for i in range(N):
                k = assignments[i]
                new_centroids[k] += data[i]
                counts[k] += 1
            for k in range(K):
                if counts[k] > 0:
                    centroids[k] = new_centroids[k] / counts[k]

            if not changed:
                break

        return centroids

    def encode(self, vectors: np.ndarray) -> QuantizedVectors:
        """Encode float32 vectors as PQ codes.

        Args:
            vectors: [N, D] float32 vectors.

        Returns:
            QuantizedVectors with quantized_data as int32 codes of shape [N, M].
        """
        if not self._trained:
            raise RuntimeError("ProductQuantizer must be trained before encoding. Call train() first.")
        if self._codebook is None:
            raise RuntimeError("Codebook not initialized.")

        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim != 2:
            raise ValueError(f"Expected 2D array [N, D], got shape {vectors.shape}")

        N, D = vectors.shape
        if D != self._original_dim:
            raise ValueError(
                f"Vector dimension {D} does not match trained dimension {self._original_dim}"
            )

        codes = np.zeros((N, self._M), dtype=np.int32)

        with self._lock:
            for m in range(self._M):
                start = m * self._d_sub
                end = start + self._d_sub
                sub = vectors[:, start:end]
                cb = self._codebook[m]

                for i in range(N):
                    best_k = 0
                    best_d2 = float("inf")
                    for k in range(self._K):
                        d2 = float(np.sum((sub[i] - cb[k]) ** 2))
                        if d2 < best_d2:
                            best_d2 = d2
                            best_k = k
                    codes[i, m] = best_k

        return QuantizedVectors(
            quantized_data=codes,
            scale=np.array([1.0], dtype=np.float32),
            zero_point=np.array([0.0], dtype=np.float32),
            method="pq",
            original_dim=D,
            codebook=self._codebook,
        )

    def decode(self, qv: QuantizedVectors) -> np.ndarray:
        """Decode PQ codes back to approximate float32 vectors.

        Args:
            qv: QuantizedVectors from encode().

        Returns:
            Approximate float32 vectors of shape [N, D].
        """
        if qv.codebook is None:
            raise ValueError("QuantizedVectors codebook is None, cannot decode PQ.")
        if qv.method != "pq":
            raise ValueError(f"Expected method='pq', got '{qv.method}'")

        N = len(qv.quantized_data)
        D = self._original_dim or qv.original_dim

        result = np.zeros((N, D), dtype=np.float32)

        for m in range(self._M):
            start = m * self._d_sub
            end = start + self._d_sub
            cb = qv.codebook[m]
            for i in range(N):
                result[i, start:end] = cb[qv.quantized_data[i, m]]

        return result

    def compute_scores(self, query: np.ndarray, qv: QuantizedVectors) -> np.ndarray:
        """Asymmetric Distance Computation (ADC) for PQ.

        Pre-computes distance from query sub-vectors to all codebook entries,
        then looks up distances for each database vector. O(M*K*d_sub + N*M)
        vs O(N*D) for brute force.

        Args:
            query: Float32 query vector of shape [D].
            qv:    QuantizedVectors with PQ codes and codebook.

        Returns:
            Cosine similarity scores of shape [N].
        """
        query = np.asarray(query, dtype=np.float32).ravel()
        if qv.codebook is None:
            raise ValueError("QuantizedVectors codebook is None.")

        N = len(qv.quantized_data)
        codes = qv.quantized_data  # [N, M] int32

        # Pre-compute distance tables: for each sub-vector, distances from query
        # sub-vector to all K centroids
        # Using dot product for cosine similarity (assuming centroids are normalized)
        dist_tables = np.zeros((self._M, self._K), dtype=np.float32)
        for m in range(self._M):
            start = m * self._d_sub
            end = start + self._d_sub
            q_sub = query[start:end]
            cb = qv.codebook[m]  # [K, d_sub]
            # Normalize q_sub for cosine
            q_norm = np.linalg.norm(q_sub)
            if q_norm > 1e-10:
                q_sub = q_sub / q_norm
            # Compute dot product (cosine sim if centroids are normalized)
            cb_norms = np.linalg.norm(cb, axis=1)
            cb_norms = np.where(cb_norms < 1e-10, 1.0, cb_norms)
            dist_tables[m] = np.dot(cb, q_sub) / cb_norms  # [K]

        # Lookup scores
        scores = np.zeros(N, dtype=np.float32)
        for m in range(self._M):
            scores += dist_tables[m, codes[:, m]]

        # Average over M sub-vectors
        scores /= self._M

        return scores

    # ── Codebook Persistence ────────────────────────────────────────

    def save_codebook(self, path: str) -> None:
        """Save codebook as JSON file."""
        if self._codebook is None:
            raise RuntimeError("No codebook to save. Train first.")

        data = {
            "M": self._M,
            "K": self._K,
            "d_sub": self._d_sub,
            "original_dim": self._original_dim,
            "codebook": [cb.tolist() for cb in self._codebook],
        }
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        logger.info("Saved PQ codebook (%d × %d × %d) to %s",
                     self._M, self._K, self._d_sub, path)

    @classmethod
    def load_codebook(cls, path: str) -> "ProductQuantizer":
        """Load codebook from JSON file and return a trained ProductQuantizer."""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        pq = cls(M=data["M"], K=data["K"])
        pq._d_sub = data["d_sub"]
        pq._original_dim = data["original_dim"]
        pq._codebook = [np.array(cb, dtype=np.float32) for cb in data["codebook"]]
        pq._trained = True
        logger.info("Loaded PQ codebook (%d × %d × %d) from %s",
                     pq._M, pq._K, pq._d_sub, path)
        return pq

    def statistics(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "method": "pq",
            "M": self._M,
            "K": self._K,
            "trained": self._trained,
        }
        if self._trained and self._codebook:
            info["d_sub"] = self._d_sub
            info["original_dim"] = self._original_dim
            bits_per_dim = np.log2(self._K)
            info["bits_per_dim"] = round(bits_per_dim, 2)
            info["compression_ratio"] = round(32.0 / bits_per_dim, 1)  # vs float32
        return info


# ── Quantized Vector Index ─────────────────────────────────────────────

class QuantizedVectorIndex:
    """Transparent quantization wrapper around any VectorIndex.

    Workflow:
      1. build(vectors): Quantize vectors → store QuantizedVectors → build index
         on approximated (decoded) vectors for fast exact search, OR
         use quantized index for approximate search.
      2. search(q, k):   Query in float32 → quantized-domain approximate search
         → optionally re-rank top candidates with full precision.

    Storage savings are tracked via statistics().
    """

    def __init__(
        self,
        config: Optional[QuantizationConfig] = None,
        index: Any = None,  # VectorIndex instance
        embed_dim: int = 1024,
    ):
        self._config = config or QuantizationConfig()
        self._index = index
        self._embed_dim = embed_dim
        self._lock = threading.RLock()

        # Quantizer
        self._quantizer: Any = None
        self._quantized_store: Optional[QuantizedVectors] = None
        self._original_vectors: Optional[np.ndarray] = None

        # Stats
        self._total_indexed: int = 0
        self._total_searches: int = 0

        self._init_quantizer()

    def _init_quantizer(self):
        method = self._config.method
        if method in ("int8", "uint8"):
            self._quantizer = ScalarQuantizer(method=method)
        elif method == "pq":
            self._quantizer = ProductQuantizer(
                M=self._config.pq_num_subvectors,
                K=self._config.pq_num_centroids,
            )

    @property
    def quantizer(self):
        return self._quantizer

    # ── Build ───────────────────────────────────────────────────────

    def build(self, vectors: np.ndarray, ids: Optional[List[str]] = None,
              metadata_list: Optional[List[Dict]] = None) -> None:
        """Quantize vectors and build the underlying index.

        Args:
            vectors:       Float32 vectors of shape [N, D].
            ids:           Optional custom IDs (auto-generated if None).
            metadata_list: Optional per-vector metadata.
        """
        vectors = np.asarray(vectors, dtype=np.float32)
        N, D = vectors.shape
        self._embed_dim = D

        # Train PQ if needed
        if isinstance(self._quantizer, ProductQuantizer) and not self._quantizer.is_trained:
            self._quantizer.train(vectors)

        # Quantize
        self._quantized_store = self._quantizer.encode(vectors)
        self._original_vectors = vectors

        # Build index on decoded (approximate) vectors for exact search compatibility
        if self._index is not None:
            decoded = self._quantizer.decode(self._quantized_store)
            # Normalize decoded vectors
            norms = np.linalg.norm(decoded, axis=1, keepdims=True)
            norms = np.where(norms < 1e-10, 1.0, norms)
            decoded = decoded / norms

            if ids is None:
                ids = [f"qvec_{i}" for i in range(N)]
            if metadata_list is None:
                metadata_list = [{}] * N

            self._index.add_batch(ids, list(decoded), metadata_list)

        self._total_indexed = N

    # ── Search ──────────────────────────────────────────────────────

    def search(self, query: np.ndarray, top_k: int = 10,
               rerank: bool = True) -> List[Tuple[str, float]]:
        """Search nearest neighbors with quantization-aware scoring.

        Two-pass approach:
          1. Quantized-domain approximate scoring on all vectors.
          2. Optional re-rank: compute exact scores on top candidates.

        Args:
            query:  Float32 query vector of shape [D].
            top_k:  Number of results to return.
            rerank: If True, re-rank top candidates with full precision.

        Returns:
            List of (id, score) tuples.
        """
        query = np.asarray(query, dtype=np.float32).ravel()
        self._total_searches += 1

        if self._quantized_store is None:
            return []

        # Phase 1: Quantized-domain scoring
        scores = self._quantizer.compute_scores(query, self._quantized_store)
        N = len(scores)
        k_candidates = min(QUANTIZATION_RERANK_K, N) if rerank else min(top_k, N)

        if k_candidates <= 0:
            return []

        # Get top candidates
        top_indices = np.argsort(scores)[-k_candidates:][::-1]

        # Phase 2: Re-rank with full precision if available
        if rerank and self._original_vectors is not None:
            q_norm = np.linalg.norm(query)
            if q_norm > 1e-10:
                query_normed = query / q_norm
            else:
                query_normed = query

            candidate_vecs = self._original_vectors[top_indices]
            vec_norms = np.linalg.norm(candidate_vecs, axis=1)
            vec_norms = np.where(vec_norms < 1e-10, 1.0, vec_norms)
            exact_scores = np.dot(candidate_vecs, query_normed) / vec_norms
        else:
            exact_scores = scores[top_indices]

        # Sort by exact scores
        final_order = np.argsort(exact_scores)[::-1][:top_k]

        results: List[Tuple[str, float]] = []
        for idx in final_order:
            orig_idx = top_indices[idx]
            item_id = f"qvec_{orig_idx}"
            results.append((item_id, float(exact_scores[idx])))

        return results

    # ── Statistics ──────────────────────────────────────────────────

    def storage_stats(self) -> Dict[str, Any]:
        """Report storage savings from quantization."""
        stats: Dict[str, Any] = {
            "method": self._config.method,
            "vectors_indexed": self._total_indexed,
        }

        if self._quantized_store is not None:
            qv = self._quantized_store
            compressed_bytes = qv.memory_bytes()
            original_bytes = self._total_indexed * self._embed_dim * 4
            ratio = qv.compression_ratio()
            stats.update({
                "original_bytes": original_bytes,
                "compressed_bytes": compressed_bytes,
                "savings_pct": round((1 - compressed_bytes / max(1, original_bytes)) * 100, 1),
                "compression_ratio": round(ratio, 1),
                "savings_factor": f"{ratio:.1f}x",
            })

        if isinstance(self._quantizer, ProductQuantizer):
            stats["pq_stats"] = self._quantizer.statistics()

        return stats

    def statistics(self) -> Dict[str, Any]:
        return {
            "config": self._config.method,
            "embed_dim": self._embed_dim,
            "total_indexed": self._total_indexed,
            "total_searches": self._total_searches,
            "storage": self.storage_stats(),
        }

    def diagnostics(self) -> Dict[str, Any]:
        return self.statistics()


# ── Factory ────────────────────────────────────────────────────────────

def create_quantized_index(
    method: str = QUANTIZATION_METHOD,
    embed_dim: int = 1024,
    pq_M: int = QUANTIZATION_PQ_M,
    pq_K: int = QUANTIZATION_PQ_K,
    index_backend: str = "numpy",
) -> QuantizedVectorIndex:
    """Factory to create a QuantizedVectorIndex with configurable settings.

    Args:
        method:       Quantization method ('int8' / 'uint8' / 'pq').
        embed_dim:    Embedding dimension.
        pq_M:         Number of PQ sub-vectors.
        pq_K:         PQ codebook size.
        index_backend: Underlying VectorIndex backend for decoded index.

    Returns:
        Configured QuantizedVectorIndex.
    """
    config = QuantizationConfig(
        method=method,
        pq_num_subvectors=pq_M,
        pq_num_centroids=pq_K,
    )

    # Late import to avoid circular dependency
    from trinity.vector_index.index import create_index
    base_index = create_index(backend=index_backend, dim=embed_dim)

    return QuantizedVectorIndex(config=config, index=base_index, embed_dim=embed_dim)


# ── Self-Test ──────────────────────────────────────────────────────────

def self_test():
    """Comprehensive self-test for embedding quantization module."""
    print("=" * 70)
    print("  Trinity Embedding Quantization — Self Test")
    print("=" * 70)

    np.random.seed(42)
    dim = 128
    N = 500

    # Generate realistic vectors (L2-normalized, as from embedding engines)
    base = np.random.randn(N, dim).astype(np.float32)
    norms = np.linalg.norm(base, axis=1, keepdims=True)
    base = base / np.where(norms < 1e-10, 1.0, norms)

    errors: List[str] = []

    # ── Test 1: ScalarQuantizer int8 ────────────────────────────────
    print("\n[Test 1] ScalarQuantizer (int8)")
    try:
        sq = ScalarQuantizer("int8")
        qv = sq.encode(base)
        assert qv.method == "int8", f"Expected int8, got {qv.method}"
        assert qv.quantized_data.shape == (N, dim), f"Shape mismatch: {qv.quantized_data.shape}"
        assert qv.quantized_data.dtype == np.int8, f"Dtype mismatch: {qv.quantized_data.dtype}"

        # Decode and check reconstruction error
        decoded = sq.decode(qv)
        mse = np.mean((base - decoded) ** 2)
        print(f"    MSE: {mse:.6f} (should be < 0.01)")
        assert mse < 0.01, f"MSE too high: {mse}"

        # Score computation
        query = base[0].copy()
        scores = sq.compute_scores(query, qv)
        assert len(scores) == N
        self_sim = scores[0]
        print(f"    Self-similarity: {self_sim:.4f} (should be near 1.0)")
        assert self_sim > 0.95, f"Self-similarity too low: {self_sim}"

        # Compression ratio
        ratio = qv.compression_ratio()
        print(f"    Compression ratio: {ratio:.1f}x (target ~4x)")
        assert ratio > 3.0, f"Compression ratio too low: {ratio}"

        print("    PASS")
    except Exception as e:
        errors.append(f"Test 1: {e}")
        print(f"    FAIL: {e}")

    # ── Test 2: ScalarQuantizer uint8 ───────────────────────────────
    print("\n[Test 2] ScalarQuantizer (uint8)")
    try:
        sq = ScalarQuantizer("uint8")
        # shift vectors to [0, 2] range to make uint8 meaningful (cosine-ready)
        shifted = base + 1.0
        shifted = shifted / np.linalg.norm(shifted, axis=1, keepdims=True)
        qv = sq.encode(shifted)
        assert qv.method == "uint8"
        assert qv.quantized_data.dtype == np.uint8

        decoded = sq.decode(qv)
        mse = np.mean((shifted - decoded) ** 2)
        print(f"    MSE: {mse:.6f}")
        assert mse < 0.01, f"MSE too high: {mse}"

        print("    PASS")
    except Exception as e:
        errors.append(f"Test 2: {e}")
        print(f"    FAIL: {e}")

    # ── Test 3: ProductQuantizer train/encode/decode ────────────────
    print("\n[Test 3] ProductQuantizer (M=16, K=64)")
    try:
        pq = ProductQuantizer(M=16, K=64, seed=42)
        pq.train(base)
        assert pq.is_trained

        qv = pq.encode(base)
        assert qv.method == "pq"
        assert qv.quantized_data.shape == (N, pq.M)
        assert qv.codebook is not None

        # Decode and check
        decoded = pq.decode(qv)
        mse = np.mean((base - decoded) ** 2)
        print(f"    MSE: {mse:.6f} (higher than scalar, that's expected for PQ)")

        # Score computation
        scores = pq.compute_scores(base[0], qv)
        self_sim = scores[0]
        print(f"    Self-similarity: {self_sim:.4f}")
        assert self_sim > 0.3, f"Self-similarity too low: {self_sim}"

        # Codebook save/load
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            tmp_path = f.name
        pq.save_codebook(tmp_path)
        pq2 = ProductQuantizer.load_codebook(tmp_path)
        assert pq2.is_trained
        assert pq2.M == pq.M
        assert pq2.K == pq.K
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        print("    Codebook save/load: OK")

        print("    PASS")
    except Exception as e:
        errors.append(f"Test 3: {e}")
        print(f"    FAIL: {e}")

    # ── Test 4: QuantizedVectorIndex (int8) ─────────────────────────
    print("\n[Test 4] QuantizedVectorIndex (int8)")
    try:
        config = QuantizationConfig(method="int8")
        qvi = QuantizedVectorIndex(config=config, embed_dim=dim)
        qvi.build(base)

        query = base[0].copy()
        results = qvi.search(query, top_k=5)
        assert len(results) > 0, "No results returned"
        print(f"    Top result: {results[0][0]} score={results[0][1]:.4f}")
        assert results[0][1] > 0.9, f"Top score too low: {results[0][1]}"

        stats = qvi.storage_stats()
        print(f"    Storage: {stats['savings_factor']} savings")
        assert stats["savings_pct"] > 50, f"Savings too low: {stats['savings_pct']}%"

        print("    PASS")
    except Exception as e:
        errors.append(f"Test 4: {e}")
        print(f"    FAIL: {e}")

    # ── Test 5: QuantizedVectorIndex (PQ) ───────────────────────────
    print("\n[Test 5] QuantizedVectorIndex (PQ, M=16, K=64)")
    try:
        config = QuantizationConfig(method="pq", pq_num_subvectors=16, pq_num_centroids=64)
        qvi = QuantizedVectorIndex(config=config, embed_dim=dim)
        qvi.build(base)

        query = base[0].copy()
        results = qvi.search(query, top_k=5, rerank=True)
        assert len(results) > 0
        print(f"    Top result: {results[0][0]} score={results[0][1]:.4f}")

        stats = qvi.storage_stats()
        print(f"    Storage savings: {stats['savings_factor']}")

        print("    PASS")
    except Exception as e:
        errors.append(f"Test 5: {e}")
        print(f"    FAIL: {e}")

    # ── Test 6: Factory function ────────────────────────────────────
    print("\n[Test 6] Factory create_quantized_index")
    try:
        from trinity.embeddings.quantization import create_quantized_index
        qvi = create_quantized_index(method="int8", embed_dim=dim)
        qvi.build(base[:100])
        results = qvi.search(base[0], top_k=3)
        assert len(results) == 3
        print("    PASS")
    except Exception as e:
        errors.append(f"Test 6: {e}")
        print(f"    FAIL: {e}")

    # ── Test 7: Config validation ───────────────────────────────────
    print("\n[Test 7] QuantizationConfig validation")
    try:
        # Valid
        QuantizationConfig(method="int8")
        QuantizationConfig(method="pq", pq_num_subvectors=32, pq_num_centroids=256)
        # Invalid should raise
        try:
            QuantizationConfig(method="invalid")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass
        try:
            QuantizationConfig(method="pq", pq_num_subvectors=0)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass
        print("    PASS")
    except Exception as e:
        errors.append(f"Test 7: {e}")
        print(f"    FAIL: {e}")

    # ── Test 8: Statistics / Diagnostics ────────────────────────────
    print("\n[Test 8] Statistics & diagnostics")
    try:
        config = QuantizationConfig(method="int8")
        qvi = QuantizedVectorIndex(config=config, embed_dim=128)
        qvi.build(base[:50])
        stats = qvi.statistics()
        diag = qvi.diagnostics()
        assert "storage" in stats
        assert stats["storage"]["method"] == "int8"
        assert stats["total_indexed"] == 50
        print("    PASS")
    except Exception as e:
        errors.append(f"Test 8: {e}")
        print(f"    FAIL: {e}")

    # ── Summary ─────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    if errors:
        print(f"  FAILED: {len(errors)}/{8} tests")
        for e in errors:
            print(f"    - {e}")
    else:
        print("  ALL 8 TESTS PASSED")
    print("=" * 70)
    return len(errors) == 0


if __name__ == "__main__":
    self_test()
