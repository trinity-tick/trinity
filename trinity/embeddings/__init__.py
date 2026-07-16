"""
Trinity Semantic Embedding Engine
==================================
Replaces SHA-256 hash-based pseudo-embeddings with real semantic embeddings
from local Ollama models (bge-m3, qwen3-embedding) or scikit-learn TF-IDF fallback.

Provides:
  - EmbeddingEngine (abstract base)
  - OllamaEmbeddingEngine (via Ollama REST API)
  - SklearnEmbeddingEngine (TF-IDF fallback)
  - CachedEmbeddingEngine (LRU cache decorator)
  - create_engine() factory function
  - self_test() function
"""

from .engine import (
    EmbeddingEngine,
    OllamaEmbeddingEngine,
    SklearnEmbeddingEngine,
    CachedEmbeddingEngine,
    FusionEmbeddingEngine,
    create_engine,
    self_test,
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_EMBED_MODEL,
    EMBEDDING_CACHE_SIZE,
    DEFAULT_EMBED_DIM,
)

__all__ = [
    "EmbeddingEngine",
    "OllamaEmbeddingEngine",
    "SklearnEmbeddingEngine",
    "CachedEmbeddingEngine",
    "FusionEmbeddingEngine",
    "create_engine",
    "self_test",
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_EMBED_MODEL",
    "EMBEDDING_CACHE_SIZE",
    "DEFAULT_EMBED_DIM",
]

__version__ = "1.0.0"
