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
    HNSWConfig,
    create_index,
)
from trinity.vector_index.mixed import (
    HybridIndex,
    create_hybrid_index,
)
from trinity.vector_index.sparse import (
    BM25SparseRetriever,
    fuse_scores_sparse_dense,
)
from trinity.vector_index.reranker import (
    CrossEncoderReranker,
)

__all__ = [
    "VectorIndex",
    "FaissIndex",
    "AnnoyIndex",
    "NumpyBruteForceIndex",
    "ChromaDBIndex",
    "HNSWConfig",
    "create_index",
    "HybridIndex",
    "create_hybrid_index",
    "BM25SparseRetriever",
    "fuse_scores_sparse_dense",
    "CrossEncoderReranker",
]
