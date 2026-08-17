"""
Vector Index implementations with multiple backends.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np


@dataclass
class IndexEntry:
    """A single entry in the vector index."""
    id: str
    vector: np.ndarray
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class SearchResult:
    """Result of a vector search."""
    id: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    distance: float = 0.0


@dataclass
class PQConfig:
    """Product Quantization configuration for memory-efficient FAISS indexing.

    PQ compresses high-dimensional vectors into compact codes by splitting
    each vector into M sub-vectors and quantizing each independently.

    Attributes:
        M: Number of sub-vectors (must divide dimension evenly).
        nbits: Number of bits per sub-vector centroid index (8-16).
        use_ivf: Whether to combine PQ with IVF (inverted file) for fast search.
        nlist: Number of IVF centroids (only used if use_ivf=True).
    """
    M: int = 32
    nbits: int = 8
    use_ivf: bool = True
    nlist: int = 100

    @property
    def compression_ratio(self) -> float:
        """Approximate memory savings ratio vs full-precision float32."""
        pq_bytes = self.M * self.nbits // 8
        return f"~{pq_bytes / (1024 * 4) * 100:.1f}% of original"

    def __repr__(self) -> str:
        return f"PQConfig(M={self.M}, nbits={self.nbits}, IVF={self.use_ivf})"


class VectorIndex(ABC):
    """Abstract base for vector indexes."""

    def __init__(self, dim: int, metric: str = "cosine"):
        self._dim = dim
        self._metric = metric
        self._entries: Dict[str, IndexEntry] = {}
        self._total_adds = 0
        self._total_searches = 0
        self._total_deletes = 0

    @abstractmethod
    def _add_vector(self, entry: IndexEntry):
        """Backend-specific add implementation."""
        ...

    @abstractmethod
    def _search_vectors(self, query: np.ndarray, top_k: int) -> List[Tuple[str, float]]:
        """Backend-specific search. Returns list of (id, score)."""
        ...

    @abstractmethod
    def _rebuild(self):
        """Rebuild the index from self._entries."""
        ...

    # ── Public API ────────────────────────────────────────────────────

    def add(self, id: str, vector: np.ndarray, metadata: Optional[Dict] = None) -> str:
        """Add a vector to the index."""
        entry = IndexEntry(
            id=id,
            vector=vector.astype(np.float32),
            metadata=metadata or {},
        )
        self._entries[id] = entry
        self._add_vector(entry)
        self._total_adds += 1
        return id

    def add_batch(self, ids: List[str], vectors: List[np.ndarray],
                   metadata_list: Optional[List[Dict]] = None) -> List[str]:
        """Add multiple vectors efficiently."""
        if metadata_list is None:
            metadata_list = [{}] * len(ids)
        for id_, vec, meta in zip(ids, vectors, metadata_list):
            self.add(id_, vec, meta)
        return ids

    def search(self, query: np.ndarray, top_k: int = 10) -> List[SearchResult]:
        """Search for nearest neighbors."""
        self._total_searches += 1
        results = self._search_vectors(query.astype(np.float32), top_k)
        return [
            SearchResult(
                id=id_,
                score=score,
                metadata=self._entries.get(id_).metadata if id_ in self._entries else {},
                distance=1.0 - score if self._metric == "cosine" else score,
            )
            for id_, score in results
        ]

    def delete(self, id: str) -> bool:
        """Delete an entry from the index."""
        if id in self._entries:
            del self._entries[id]
            self._rebuild()
            self._total_deletes += 1
            return True
        return False

    def get(self, id: str) -> Optional[IndexEntry]:
        """Get an entry by ID."""
        return self._entries.get(id)

    def size(self) -> int:
        return len(self._entries)

    @property
    def dim(self) -> int:
        return self._dim

    def statistics(self) -> Dict[str, Any]:
        return {
            "backend": self.__class__.__name__,
            "dim": self._dim,
            "metric": self._metric,
            "total_entries": len(self._entries),
            "total_adds": self._total_adds,
            "total_searches": self._total_searches,
            "total_deletes": self._total_deletes,
        }


# ── NumpyBruteForceIndex (no extra deps, always works) ────────────────

class NumpyBruteForceIndex(VectorIndex):
    """Simple brute-force nearest neighbor search using NumPy.

    O(n*d) per search. Best for <10K entries or as reference baseline.
    """

    def __init__(self, dim: int, metric: str = "cosine"):
        super().__init__(dim, metric)
        self._vectors: List[Tuple[str, np.ndarray]] = []

    def _add_vector(self, entry: IndexEntry):
        self._vectors.append((entry.id, entry.vector))

    def _search_vectors(self, query: np.ndarray, top_k: int) -> List[Tuple[str, float]]:
        if not self._vectors:
            return []

        matrix = np.stack([v for _, v in self._vectors])
        if self._metric == "cosine":
            # Cosine similarity
            q_norm = np.linalg.norm(query)
            norms = np.linalg.norm(matrix, axis=1)
            sims = np.dot(matrix, query) / (norms * q_norm + 1e-10)
        elif self._metric == "l2":
            dists = np.linalg.norm(matrix - query, axis=1)
            sims = 1.0 / (1.0 + dists)
        else:
            sims = np.dot(matrix, query)

        top_indices = np.argsort(sims)[-top_k:][::-1]
        return [(self._vectors[i][0], float(sims[i])) for i in top_indices]

    def _rebuild(self):
        self._vectors = [(e.id, e.vector) for e in self._entries.values()]


# ── FaissIndex (FAISS GPU/CPU) ─────────────────────────────────────────

@dataclass
class HNSWConfig:
    """HNSW index hyperparameters.

    M:              Number of neighbors per node (default 32, range 4-64).
    efConstruction: Build-time dynamic list size (default 200, range 40-400).
                    Higher = better recall, slower build.
    efSearch:       Query-time dynamic list size (default 64, range 1-1000).
                    Higher = better recall, slower query.
    """
    M: int = 32
    efConstruction: int = 200
    efSearch: int = 64

    def to_dict(self) -> Dict[str, int]:
        return {"M": self.M, "efConstruction": self.efConstruction}


class FaissIndex(VectorIndex):
    """Facebook AI Similarity Search (FAISS) based index.

    Requires: pip install faiss-cpu (or faiss-gpu)
    Supports IVF, HNSW (default), and Flat indexes.

    HNSW is the recommended default for most use cases:
    - O(log n) search time
    - High recall (95%+ at efSearch=128)
    - No training required (unlike IVF)
    """

    def __init__(self, dim: int, metric: str = "cosine",
                 index_type: str = "hnsw", nlist: int = 100,
                 hnsw_config: Optional[HNSWConfig] = None):
        super().__init__(dim, metric)
        self._index_type = index_type
        self._nlist = nlist
        self._hnsw_config = hnsw_config or HNSWConfig()
        self._pq_config: Optional[PQConfig] = None
        self._index = None
        self._id_map: Dict[int, str] = {}  # faiss_id -> our_id
        self._next_faiss_id = 0
        self._faiss_available = False

        try:
            import faiss
            self._faiss = faiss
            self._faiss_available = True
            self._build_index()
        except ImportError:
            import warnings
            warnings.warn("faiss not installed. Use pip install faiss-cpu")

    def _build_index(self):
        if not self._faiss_available:
            return
        metric = (self._faiss.METRIC_INNER_PRODUCT if self._metric == "cosine"
                  else self._faiss.METRIC_L2)

        if self._index_type == "pq":
            pq = self._pq_config or PQConfig()
            self._index = self._faiss.IndexPQ(self._dim, pq.M, pq.nbits)
            logger.info("Built IndexPQ (M=%d, nbits=%d, compression=%s)",
                        pq.M, pq.nbits, pq.compression_ratio)
        elif self._index_type == "ivfpq":
            pq = self._pq_config or PQConfig()
            quantizer = self._faiss.IndexFlatIP(self._dim)
            self._index = self._faiss.IndexIVFPQ(quantizer, self._dim, pq.nlist, pq.M, pq.nbits)
            self._index.nprobe = min(pq.nlist, 10)
            logger.info("Built IndexIVFPQ (M=%d, nbits=%d, nlist=%d, nprobe=%d, compression=%s)",
                        pq.M, pq.nbits, pq.nlist, self._index.nprobe, pq.compression_ratio)
        if self._index_type == "flat":
            self._index = self._faiss.IndexFlatIP(self._dim)
        elif self._index_type == "ivf":
            quantizer = self._faiss.IndexFlatIP(self._dim)
            self._index = self._faiss.IndexIVFFlat(
                quantizer, self._dim, self._nlist, metric
            )
            self._index.train(self._faiss.random(self._faiss.float32, (self._nlist, self._dim)))
        elif self._index_type == "hnsw":
            self._index = self._faiss.IndexHNSWFlat(self._dim, self._hnsw_config.M)
            self._index.hnsw.efConstruction = self._hnsw_config.efConstruction
            self._index.hnsw.efSearch = self._hnsw_config.efSearch
        else:
            self._index = self._faiss.IndexFlatIP(self._dim)

    def train(self, vectors: np.ndarray) -> None:
        """Train the index (required for PQ/IVF indexes)."""
        if self._index is not None and not self._index.is_trained:
            if isinstance(self._index, (self._faiss.IndexPQ, self._faiss.IndexIVFPQ, self._faiss.IndexIVFFlat)):
                logger.info("Training PQ index with %d vectors...", len(vectors))
                self._index.train(vectors)
                logger.info("PQ index trained successfully")

    def _add_vector(self, entry: IndexEntry):
        if not self._faiss_available:
            return
        # Auto-train PQ/IVF indexes on first add
        if self._index is not None and not self._index.is_trained:
            self.train(entry.vector.reshape(1, -1).astype(np.float32))
        vec = entry.vector.reshape(1, -1).astype(np.float32)
        if self._metric == "cosine":
            self._faiss.normalize_L2(vec)
        faiss_id = self._next_faiss_id
        self._next_faiss_id += 1
        self._id_map[faiss_id] = entry.id
        if hasattr(self._index, 'add_with_ids'):
            self._index.add_with_ids(vec, np.array([faiss_id]))
        else:
            self._index.add(vec)

    def _search_vectors(self, query: np.ndarray, top_k: int) -> List[Tuple[str, float]]:
        if not self._faiss_available or not self._entries:
            return self._brute_fallback(query, top_k)

        q = query.reshape(1, -1).astype(np.float32)
        if self._metric == "cosine":
            self._faiss.normalize_L2(q)

        scores, indices = self._index.search(q, min(top_k, len(self._entries)))
        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue
            our_id = self._id_map.get(int(idx), f"faiss_{idx}")
            results.append((our_id, float(score)))
        return results

    def _brute_fallback(self, query: np.ndarray, top_k: int) -> List[Tuple[str, float]]:
        """Fallback to brute force if FAISS unavailable."""
        bf = NumpyBruteForceIndex(self._dim, self._metric)
        for e in self._entries.values():
            bf.add(e.id, e.vector, e.metadata)
        return bf._search_vectors(query, top_k)

    def _rebuild(self):
        if not self._faiss_available:
            return
        self._build_index()
        for entry in self._entries.values():
            self._add_vector(entry)


# ── AnnoyIndex (Spotify ANNOY) ─────────────────────────────────────────

class AnnoyIndex(VectorIndex):
    """Spotify's Approximate Nearest Neighbors (ANNOY).

    Requires: pip install annoy
    Memory-mapped, good for large indexes.
    """

    def __init__(self, dim: int, metric: str = "angular",
                 n_trees: int = 10):
        super().__init__(dim, metric)
        self._n_trees = n_trees
        self._index = None
        self._annoy_available = False
        self._annoy_id_map: Dict[int, str] = {}
        self._next_annoy_id = 0

        try:
            from annoy import AnnoyIndex as AnnoyIdx
            self._AnnoyIdx = AnnoyIdx
            self._annoy_available = True
            self._create_annoy()
        except ImportError:
            import warnings
            warnings.warn("annoy not installed. Use pip install annoy")

    def _create_annoy(self):
        metric_map = {"cosine": "angular", "l2": "euclidean", "dot": "dot"}
        a_metric = metric_map.get(self._metric, "angular")
        self._index = self._AnnoyIdx(self._dim, a_metric)

    def _add_vector(self, entry: IndexEntry):
        if not self._annoy_available:
            return
        annoy_id = self._next_annoy_id
        self._next_annoy_id += 1
        self._annoy_id_map[annoy_id] = entry.id
        self._index.add_item(annoy_id, entry.vector.tolist())

    def _search_vectors(self, query: np.ndarray, top_k: int) -> List[Tuple[str, float]]:
        if not self._annoy_available or not self._entries:
            return self._brute_fallback(query, top_k)

        self._index.build(self._n_trees)
        indices, distances = self._index.get_nns_by_vector(
            query.tolist(), min(top_k, len(self._entries)),
            include_distances=True
        )
        results = []
        for idx, dist in zip(indices, distances):
            our_id = self._annoy_id_map.get(int(idx), f"annoy_{idx}")
            # Convert distance to similarity score
            if self._metric == "cosine":
                score = 1.0 - (dist / 2.0)  # angular -> cosine
            elif self._metric == "l2":
                score = 1.0 / (1.0 + dist)
            else:
                score = 1.0 - dist
            results.append((our_id, float(score)))
        return results

    def _brute_fallback(self, query: np.ndarray, top_k: int) -> List[Tuple[str, float]]:
        bf = NumpyBruteForceIndex(self._dim, self._metric)
        for e in self._entries.values():
            bf.add(e.id, e.vector, e.metadata)
        return bf._search_vectors(query, top_k)

    def _rebuild(self):
        if not self._annoy_available:
            return
        self._create_annoy()
        for entry in self._entries.values():
            self._add_vector(entry)


# ── ChromaDBIndex ──────────────────────────────────────────────────────

class ChromaDBIndex(VectorIndex):
    """ChromaDB-based vector index integration."""

    def __init__(self, dim: int, metric: str = "cosine",
                 collection_name: str = "trinity_vectors",
                 persist_dir: Optional[str] = None):
        super().__init__(dim, metric)
        self._collection_name = collection_name
        self._persist_dir = persist_dir
        self._chroma_available = False
        self._collection = None

        try:
            import chromadb
            self._chroma = chromadb
            self._chroma_available = True
            self._init_chromadb()
        except ImportError:
            import warnings
            warnings.warn("chromadb not installed. Use pip install chromadb")

    def _init_chromadb(self):
        if not self._chroma_available:
            return
        client_kwargs = {}
        if self._persist_dir:
            os.makedirs(self._persist_dir, exist_ok=True)
            client_kwargs["path"] = self._persist_dir
        client = self._chroma.PersistentClient(**client_kwargs) if self._persist_dir \
            else self._chroma.EphemeralClient()

        try:
            self._collection = client.get_collection(self._collection_name)
        except Exception:
            self._collection = client.create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
            )

    def _add_vector(self, entry: IndexEntry):
        if not self._chroma_available or not self._collection:
            return
        self._collection.add(
            ids=[entry.id],
            embeddings=[entry.vector.tolist()],
            metadatas=[{**entry.metadata, "_timestamp": entry.timestamp}],
        )

    def _search_vectors(self, query: np.ndarray, top_k: int) -> List[Tuple[str, float]]:
        if not self._chroma_available or not self._collection:
            return self._brute_fallback(query, top_k)
        try:
            results = self._collection.query(
                query_embeddings=[query.tolist()],
                n_results=min(top_k, max(1, len(self._entries))),
            )
            ids = results["ids"][0] if results["ids"] else []
            distances = results["distances"][0] if results["distances"] else []
            return [
                (id_, 1.0 - dist) for id_, dist in zip(ids, distances)
            ]
        except Exception:
            return self._brute_fallback(query, top_k)

    def _brute_fallback(self, query: np.ndarray, top_k: int) -> List[Tuple[str, float]]:
        bf = NumpyBruteForceIndex(self._dim, self._metric)
        for e in self._entries.values():
            bf.add(e.id, e.vector, e.metadata)
        return bf._search_vectors(query, top_k)

    def _rebuild(self):
        pass  # ChromaDB handles its own persistence


# ── Factory ────────────────────────────────────────────────────────────

def create_index(
    backend: str = "auto",
    dim: int = 1024,
    metric: str = "cosine",
    index_type: str = "hnsw",
    hnsw_config: Optional[HNSWConfig] = None,
    pq_config: Optional[PQConfig] = None,
    **kwargs,
) -> VectorIndex:
    """Create a vector index with the specified backend.

    Args:
        backend: "auto" (try faiss -> annoy -> numpy), "faiss", "annoy",
                 "numpy", "chromadb"
        dim: Embedding dimension.
        metric: "cosine" or "l2".
        index_type: For FAISS backend: "hnsw" (default), "flat", "ivf",
            "pq" (product quantization), "ivfpq" (IVF+PQ).
        hnsw_config: HNSW hyperparameters (M, efConstruction, efSearch).
        pq_config: Optional PQ configuration for pq/ivfpq index types.

    Returns:
        Configured VectorIndex instance.
    """
    if backend == "numpy":
        return NumpyBruteForceIndex(dim, metric, **kwargs)

    if backend == "faiss":
        idx = FaissIndex(dim, metric, index_type=index_type,
                         hnsw_config=hnsw_config, **kwargs)
        if pq_config:
            idx._pq_config = pq_config
        return idx

    if backend == "annoy":
        return AnnoyIndex(dim, metric, **kwargs)

    if backend == "chromadb":
        return ChromaDBIndex(dim, metric, **kwargs)

    if backend == "auto":
        try:
            import faiss
            idx = FaissIndex(dim, metric, index_type=index_type,
                             hnsw_config=hnsw_config, **kwargs)
            if pq_config:
                idx._pq_config = pq_config
            return idx
        except ImportError:
            pass
        try:
            from annoy import AnnoyIndex as _  # noqa
            return AnnoyIndex(dim, metric, **kwargs)
        except ImportError:
            pass
        return NumpyBruteForceIndex(dim, metric, **kwargs)

    raise ValueError(f"Unknown backend: {backend}")


# ── Self-test ──────────────────────────────────────────────────────────

def self_test():
    print("=" * 60)
    print("  Trinity Vector Index - Self Test")
    print("=" * 60)

    dim = 128
    np.random.seed(42)

    for backend in ["numpy", "auto"]:
        print(f"\n  Backend: {backend}")
        try:
            index = create_index(backend=backend, dim=dim, metric="cosine")
            print(f"    Created: {index.__class__.__name__}, dim={dim}")

            # Generate test vectors
            vectors = []
            for i in range(100):
                vec = np.random.randn(dim).astype(np.float32)
                vec = vec / np.linalg.norm(vec)
                vectors.append(vec)
                index.add(f"mem_{i}", vec, {"content": f"memory_{i}"})
            print(f"    Added: {index.size()} vectors")

            # Search
            query = vectors[0]
            results = index.search(query, top_k=5)
            print(f"    Search results: {len(results)}")
            for r in results[:3]:
                print(f"      {r.id}: score={r.score:.4f}")

            # Statistics
            stats = index.statistics()
            print(f"    Stats: {stats}")

        except Exception as e:
            print(f"    ERROR: {e}")

    print("\n" + "=" * 60)
    print("  Self-test complete")
    print("=" * 60)


if __name__ == "__main__":
    self_test()
