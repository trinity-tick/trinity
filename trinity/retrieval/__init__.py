"""
Trinity Retrieval Package — Hybrid Retrieval Engine (v8.1.0)
===========================================================
Three-tier retrieval combining:
  - BM25 sparse vector (keyword match)
  - Vector / FTS dense retrieval (semantic)
  - Knowledge graph traversal (entity / relation)
  - Cross-modal retrieval (text ↔ image)  [NEW v8.1.0]

Exports:
    BM25Index          — Pure-Python BM25 keyword inverted index
    GraphRetriever     — Entity / relation / subgraph retrieval
    HybridRetriever    — Fusion / RRF / cascade multi-source retrieval
    ANNIndex           — HNSW approximate nearest neighbor index
    CrossModalRetriever — Text ↔ Image cross-modal memory search
"""

__all__ = [
    "BM25Index",
    "GraphRetriever",
    "HybridRetriever",
    "ANNIndex",
    "CrossModalRetriever",
]

from trinity.retrieval.bm25_index import BM25Index
from trinity.retrieval.graph_retriever import GraphRetriever
from trinity.retrieval.hybrid_retriever import HybridRetriever
from trinity.retrieval.ann_index import ANNIndex
from trinity.retrieval.cross_modal import CrossModalRetriever
