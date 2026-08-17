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
  - ScalarQuantizer + ProductQuantizer (PQ) for 4x-16x storage reduction
  - QuantizedVectorIndex: transparent quantized search
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

from .quantization import (
    QuantizationConfig,
    QuantizationMethod,
    QuantizedVectors,
    ScalarQuantizer,
    ProductQuantizer,
    QuantizedVectorIndex,
    create_quantized_index,
    self_test as quantization_self_test,
    QUANTIZATION_METHOD,
    QUANTIZATION_PQ_M,
    QUANTIZATION_PQ_K,
    QUANTIZATION_RERANK_K,
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
    # Quantization
    "QuantizationConfig",
    "QuantizationMethod",
    "QuantizedVectors",
    "ScalarQuantizer",
    "ProductQuantizer",
    "QuantizedVectorIndex",
    "create_quantized_index",
    "quantization_self_test",
    "QUANTIZATION_METHOD",
    "QUANTIZATION_PQ_M",
    "QUANTIZATION_PQ_K",
    "QUANTIZATION_RERANK_K",
]

__version__ = "1.1.0"
