"""
ANN Index — Approximate Nearest Neighbor index for Trinity retrieval.

Implements HNSW (Hierarchical Navigable Small World) graph-based ANN search.
Backend priority: hnswlib > FAISS HNSW > Numpy brute-force (cosine similarity).

Usage:
    from trinity.retrieval.ann_index import ANNIndex

    idx = ANNIndex(dim=384)
    idx.add_vector("mem_1", vec1)
    idx.add_vectors(["mem_2", "mem_3"], [vec2, vec3])
    results = idx.search(query_vec, k=10)
    idx.save("index.bin")
    idx.load("index.bin")
"""

from __future__ import annotations

import json
import os
import warnings
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# ── Backend detection ──────────────────────────────────────────────────

_HNSWLIB_AVAILABLE = False
_FAISS_AVAILABLE = False
_BACKEND_NAME = "numpy"

try:
    import hnswlib
    _HNSWLIB_AVAILABLE = True
    _BACKEND_NAME = "hnswlib"
except ImportError:
    pass

if not _HNSWLIB_AVAILABLE:
    try:
        import faiss
        _FAISS_AVAILABLE = True
        _BACKEND_NAME = "faiss"
    except ImportError:
        pass


# ── ANNIndex ────────────────────────────────────────────────────────────

class ANNIndex:
    """HNSW-based Approximate Nearest Neighbor index.

    Parameters
    ----------
    dim : int
        Embedding dimensionality (default 384).
    space : str
        Distance metric: ``cosine`` | ``l2`` | ``ip`` (default ``cosine``).
    max_elements : int
        Maximum number of elements the index can hold (default 100000).
    M : int
        Number of bidirectional links per node. Higher = better recall,
        more memory. Range 4-64 (default 16).
    ef_construction : int
        Build-time dynamic candidate list size. Higher = better recall,
        slower build. Range 40-400 (default 200).
    """

    def __init__(
        self,
        dim: int = 384,
        space: str = "cosine",
        max_elements: int = 100000,
        M: int = 16,
        ef_construction: int = 200,
    ):
        self._dim = dim
        self._space = space
        self._max_elements = max_elements
        self._M = M
        self._ef_construction = ef_construction

        # Internal state
        self._index = None          # hnswlib or FAISS index object
        self._id_map: Dict[int, str] = {}  # internal_id -> external memory_id
        self._rev_map: Dict[str, int] = {}  # external memory_id -> internal_id
        self._next_id = 0
        self._vectors: Dict[str, np.ndarray] = {}  # for numpy fallback
        self._built = False

        # Backend label (set once, used by __repr__ / statistics)
        self._backend = "numpy"  # may be upgraded on first add

        # ── Try hnswlib first ───────────────────────────────────────
        if _HNSWLIB_AVAILABLE:
            self._backend = "hnswlib"
            try:
                space_map = {"cosine": "cosine", "l2": "l2", "ip": "ip"}
                self._index = hnswlib.Index(
                    space=space_map.get(space, "cosine"), dim=dim
                )
                self._index.init_index(
                    max_elements=max_elements,
                    M=M,
                    ef_construction=ef_construction,
                )
            except Exception as e:
                warnings.warn(f"hnswlib init failed: {e}; falling back.")
                self._backend = "faiss" if _FAISS_AVAILABLE else "numpy"
                self._index = None

        # ── FAISS fallback ──────────────────────────────────────────
        if self._index is None and _FAISS_AVAILABLE:
            self._backend = "faiss"
            try:
                if space == "cosine":
                    self._index = faiss.IndexHNSWFlat(dim, M)
                    self._index.hnsw.efConstruction = ef_construction
                    self._index.hnsw.efSearch = 50
                elif space == "l2":
                    self._index = faiss.IndexHNSWFlat(dim, M, faiss.METRIC_L2)
                    self._index.hnsw.efConstruction = ef_construction
                    self._index.hnsw.efSearch = 50
                else:  # ip
                    self._index = faiss.IndexHNSWFlat(dim, M, faiss.METRIC_INNER_PRODUCT)
                    self._index.hnsw.efConstruction = ef_construction
                    self._index.hnsw.efSearch = 50
            except Exception as e:
                warnings.warn(f"FAISS HNSW init failed: {e}; falling back to numpy.")
                self._backend = "numpy"
                self._index = None

        # ── Numpy fallback ──────────────────────────────────────────
        if self._index is None:
            self._backend = "numpy"

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def size(self) -> int:
        return len(self._vectors)

    # ── Public API ─────────────────────────────────────────────────────

    def add_vector(self, memory_id: str, vector: np.ndarray) -> None:
        """Add a single vector to the index.

        Parameters
        ----------
        memory_id : str
            External identifier for the memory entry.
        vector : np.ndarray
            Float32 vector of shape (dim,).
        """
        self.add_vectors([memory_id], [vector])

    def add_vectors(self, ids: List[str], vectors: List[np.ndarray]) -> None:
        """Add multiple vectors in batch.

        Parameters
        ----------
        ids : list[str]
            External identifiers for each memory entry.
        vectors : list[np.ndarray]
            Corresponding float32 vectors, each of shape (dim,).
        """
        if len(ids) != len(vectors):
            raise ValueError(
                f"ids and vectors length mismatch: {len(ids)} vs {len(vectors)}"
            )

        for mem_id, vec in zip(ids, vectors):
            vec_arr = np.asarray(vec, dtype=np.float32).ravel()
            if len(vec_arr) != self._dim:
                raise ValueError(
                    f"Vector dim mismatch for {mem_id}: "
                    f"expected {self._dim}, got {len(vec_arr)}"
                )

            # Normalize for cosine
            if self._space == "cosine":
                norm = np.linalg.norm(vec_arr)
                if norm > 1e-10:
                    vec_arr = vec_arr / norm

            self._vectors[mem_id] = vec_arr

        # Rebuild index
        self._built = False
        self._rebuild()

    def remove_vector(self, memory_id: str) -> bool:
        """Remove a vector from the index.

        Returns True if the entry existed and was removed.
        """
        if memory_id not in self._vectors:
            return False
        del self._vectors[memory_id]
        self._built = False
        self._rebuild()
        return True

    def search(
        self,
        query_vector: np.ndarray,
        k: int = 10,
        ef: int = 50,
    ) -> List[Tuple[str, float]]:
        """Search for k nearest neighbors.

        Parameters
        ----------
        query_vector : np.ndarray
            Query vector of shape (dim,).
        k : int
            Number of results to return.
        ef : int
            Query-time dynamic candidate list size (hnswlib only).
            Higher = better recall, slower query.

        Returns
        -------
        list of (memory_id, score) tuples, sorted by descending similarity.
        """
        if not self._vectors:
            return []

        q = np.asarray(query_vector, dtype=np.float32).ravel()
        if self._space == "cosine":
            norm = np.linalg.norm(q)
            if norm > 1e-10:
                q = q / norm

        k_eff = min(k, len(self._vectors))

        if self._backend == "hnswlib" and self._index is not None:
            self._index.set_ef(ef)
            labels, distances = self._index.knn_query(q, k=k_eff)
            ids = [
                self._id_map.get(int(lbl), f"unk_{lbl}")
                for lbl in labels[0]
            ]
            return list(zip(ids, 1.0 - np.array(distances[0])))

        if self._backend == "faiss" and self._index is not None:
            self._index.hnsw.efSearch = ef
            q_reshaped = q.reshape(1, -1).astype(np.float32)
            scores, indices = self._index.search(q_reshaped, k_eff)
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0:
                    continue
                mem_id = self._id_map.get(int(idx), f"faiss_{idx}")
                results.append((mem_id, float(score)))
            return results

        # Numpy brute-force fallback
        return self._numpy_search(q, k_eff)

    def save(self, path: str) -> None:
        """Persist the index to disk.

        Always saves vectors + metadata via npz/json (portable).
        Additionally saves the native backend index file if available,
        which can be used as a fast-reload cache on next ``load()``.
        """
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)

        meta = {
            "dim": self._dim,
            "space": self._space,
            "max_elements": self._max_elements,
            "M": self._M,
            "ef_construction": self._ef_construction,
            "backend": self._backend,
            "size": len(self._vectors),
        }

        meta_path = path + ".meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        # Always save vectors + id maps as numpy arrays (primary persistence)
        vec_path = path + ".vec.npz"
        ids = list(self._vectors.keys())
        matrix = np.stack([self._vectors[mid] for mid in ids])
        np.savez_compressed(vec_path, ids=np.array(ids), vectors=matrix)

        # Save backend-specific native index as cache
        try:
            if self._backend == "hnswlib" and self._index is not None:
                self._index.save_index(path)
            elif self._backend == "faiss" and self._index is not None:
                faiss.write_index(self._index, path + ".faiss")
        except Exception as e:
            warnings.warn(f"Native index save failed (will rebuild on load): {e}")

    def load(self, path: str) -> None:
        """Load the index from disk.

        Primary: loads vectors from npz, then rebuilds native index.
        Cache: if native index file exists and matches, uses it for fast load.
        """
        meta_path = path + ".meta.json"
        if not os.path.exists(meta_path):
            raise FileNotFoundError(f"Index metadata not found: {meta_path}")

        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        self._dim = meta["dim"]
        self._space = meta["space"]
        self._max_elements = meta.get("max_elements", 100000)
        self._M = meta.get("M", 16)
        self._ef_construction = meta.get("ef_construction", 200)
        stored_backend = meta.get("backend", "numpy")

        # Reload vectors from npz (primary persistence)
        vec_path = path + ".vec.npz"
        if os.path.exists(vec_path):
            data = np.load(vec_path, allow_pickle=True)
            stored_ids = data["ids"]
            stored_vecs = data["vectors"]
            self._vectors = {}
            for i, mid in enumerate(stored_ids):
                self._vectors[str(mid)] = stored_vecs[i].astype(np.float32)

        # Try loading native index cache for fast startup
        native_loaded = False
        if stored_backend == "hnswlib" and _HNSWLIB_AVAILABLE and os.path.exists(path):
            try:
                space_map = {"cosine": "cosine", "l2": "l2", "ip": "ip"}
                self._index = hnswlib.Index(
                    space=space_map.get(self._space, "cosine"), dim=self._dim
                )
                self._index.load_index(path)
                self._backend = "hnswlib"
                native_loaded = True
            except Exception as e:
                warnings.warn(f"hnswlib index cache load failed, will rebuild: {e}")

        if not native_loaded and stored_backend == "faiss" and _FAISS_AVAILABLE:
            faiss_path = path + ".faiss"
            if os.path.exists(faiss_path):
                try:
                    self._index = faiss.read_index(faiss_path)
                    self._backend = "faiss"
                    native_loaded = True
                except Exception as e:
                    warnings.warn(f"FAISS index cache load failed, will rebuild: {e}")

        # Always rebuild id maps and ensure index is ready
        self._built = native_loaded
        if not native_loaded:
            self._rebuild()

    def statistics(self) -> Dict[str, Any]:
        """Return index statistics."""
        return {
            "backend": self._backend,
            "dim": self._dim,
            "space": self._space,
            "size": len(self._vectors),
            "M": self._M,
            "ef_construction": self._ef_construction,
            "built": self._built,
        }

    def __repr__(self) -> str:
        return (
            f"ANNIndex(backend={self._backend}, dim={self._dim}, "
            f"space={self._space}, size={len(self._vectors)})"
        )

    # ── Internal methods ────────────────────────────────────────────────

    def _rebuild(self) -> None:
        """Rebuild the underlying index from self._vectors."""
        if self._built:
            return
        if not self._vectors:
            return

        self._id_map = {}
        self._rev_map = {}
        self._next_id = 0

        ids = list(self._vectors.keys())
        vecs = [self._vectors[mid] for mid in ids]
        matrix = np.stack(vecs, axis=0).astype(np.float32)

        for mem_id in ids:
            internal_id = self._next_id
            self._id_map[internal_id] = mem_id
            self._rev_map[mem_id] = internal_id
            self._next_id += 1

        if self._backend == "hnswlib" and _HNSWLIB_AVAILABLE:
            try:
                space_map = {"cosine": "cosine", "l2": "l2", "ip": "ip"}
                self._index = hnswlib.Index(
                    space=space_map.get(self._space, "cosine"), dim=self._dim
                )
                self._index.init_index(
                    max_elements=max(self._max_elements, len(self._vectors)),
                    M=self._M,
                    ef_construction=self._ef_construction,
                )
                internal_ids = np.arange(len(ids))
                self._index.add_items(matrix, internal_ids)
                self._built = True
            except Exception as e:
                warnings.warn(f"hnswlib rebuild failed: {e}")
                self._backend = "faiss" if _FAISS_AVAILABLE else "numpy"
                self._index = None
                self._rebuild()  # retry with next backend
                return

        elif self._backend == "faiss" and _FAISS_AVAILABLE:
            try:
                if self._space == "cosine":
                    self._index = faiss.IndexHNSWFlat(self._dim, self._M)
                elif self._space == "l2":
                    self._index = faiss.IndexHNSWFlat(self._dim, self._M, faiss.METRIC_L2)
                else:
                    self._index = faiss.IndexHNSWFlat(self._dim, self._M, faiss.METRIC_INNER_PRODUCT)
                self._index.hnsw.efConstruction = self._ef_construction
                self._index.hnsw.efSearch = 50

                if hasattr(self._index, 'add_with_ids'):
                    self._index.add_with_ids(matrix, np.arange(len(ids), dtype=np.int64))
                else:
                    self._index.add(matrix)
                self._built = True
            except Exception as e:
                warnings.warn(f"FAISS rebuild failed: {e}")
                self._backend = "numpy"
                self._index = None
                self._built = True  # numpy is always "built"
        else:
            self._built = True  # numpy backend needs no building

    def _numpy_search(
        self, query: np.ndarray, k: int
    ) -> List[Tuple[str, float]]:
        """Brute-force cosine / L2 search using numpy."""
        ids = list(self._vectors.keys())
        matrix = np.stack([self._vectors[mid] for mid in ids])

        if self._space in ("cosine", "ip"):
            # Dot product (vectors are already L2-normalized for cosine)
            sims = np.dot(matrix, query)
        elif self._space == "l2":
            dists = np.linalg.norm(matrix - query, axis=1)
            sims = 1.0 / (1.0 + dists)
        else:
            sims = np.dot(matrix, query)

        top_indices = np.argsort(sims)[-k:][::-1]
        return [(ids[i], float(sims[i])) for i in top_indices]


# ── Self-test ──────────────────────────────────────────────────────────

def self_test():
    """Quick smoke test for ANNIndex."""
    print("=" * 60)
    print("  ANNIndex Self-Test")
    print("=" * 60)

    np.random.seed(42)
    dim = 128

    idx = ANNIndex(dim=dim, space="cosine", max_elements=100, M=16, ef_construction=100)
    print(f"\n  Backend: {idx.backend}")
    print(f"  Dim: {idx.dim}")

    # Generate vectors
    n = 50
    mem_ids = [f"mem_{i}" for i in range(n)]
    vectors = []
    for _ in range(n):
        v = np.random.randn(dim).astype(np.float32)
        v = v / np.linalg.norm(v)
        vectors.append(v)

    idx.add_vectors(mem_ids, vectors)
    print(f"  Added: {idx.size} vectors")
    assert idx.size == n, f"Expected {n}, got {idx.size}"

    # Search
    query = vectors[0] + np.random.randn(dim).astype(np.float32) * 0.01
    query = query / np.linalg.norm(query)
    results = idx.search(query, k=5, ef=50)
    print(f"  Search: top-5 results")
    for mid, score in results:
        print(f"    {mid}: {score:.6f}")
    assert len(results) == 5
    assert results[0][0] == "mem_0", f"Top result should be mem_0, got {results[0][0]}"

    # Remove
    assert idx.remove_vector("mem_0")
    assert not idx.remove_vector("nonexistent")
    assert idx.size == n - 1

    # Save / Load
    save_path = "test_ann_index.bin"
    idx.save(save_path)

    idx2 = ANNIndex(dim=dim)
    idx2.load(save_path)
    assert idx2.size == n - 1, f"Loaded size: {idx2.size}"
    assert idx2.backend == idx.backend

    results2 = idx2.search(query, k=5, ef=50)
    print(f"  After save/load: {len(results2)} results")

    # Statistics
    stats = idx.statistics()
    print(f"  Stats: {stats}")

    # Cleanup
    for ext in ("", ".meta.json", ".vec.npz"):
        p = save_path + ext
        if os.path.exists(p):
            os.remove(p)

    print(f"\n  All assertions passed (backend={idx.backend})")
    print("=" * 60)


if __name__ == "__main__":
    self_test()
