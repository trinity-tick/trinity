"""
Trinity Vector Index Module
============================
True vector similarity search to replace hash-based pseudo-semantic matching.

Backends:
  - FaissIndex: Facebook AI Similarity Search (GPU/CPU)
  - AnnoyIndex: Spotify's Approximate Nearest Neighbors
  - NumpyBruteForce: Simple brute force (no extra deps)
  - ChromaDBIndex: ChromaDB integration

Usage:
    from trinity.vector_index import create_index

    index = create_index(backend="faiss", dim=1024)
    index.add("mem_1", embedding_vector)
    results = index.search(query_vector, top_k=5)
"""

from trinity.vector_index.index import (
    VectorIndex,
    FaissIndex,
    AnnoyIndex,
    NumpyBruteForceIndex,
    ChromaDBIndex,
    create_index,
)
from trinity.vector_index.mixed import (
    HybridIndex,
    create_hybrid_index,
)

__all__ = [
    "VectorIndex",
    "FaissIndex",
    "AnnoyIndex",
    "NumpyBruteForceIndex",
    "ChromaDBIndex",
    "create_index",
    "HybridIndex",
    "create_hybrid_index",
]
