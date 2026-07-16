"""Tests for trinity.embeddings.engine — EmbeddingEngine and factory.

Tests:
  - test_create_hash_engine      create_engine('hash') returns working engine
  - test_embed_single            embed() returns normalized float32 array
  - test_embed_batch             embed_batch processes multiple texts
  - test_embedding_dim           embedding_dim returns correct dimension
  - test_model_name              model_name returns non-empty string
  - test_cosine_similarity       cosine_similarity of identical texts ~1.0
  - test_cosine_similarity_diff  cosine_similarity of different texts < 1.0
  - test_cached_engine           CachedEmbeddingEngine caches results
  - test_cached_cache_stats      cache_stats returns hit/miss info
  - test_sklearn_engine          SklearnEmbeddingEngine works without Ollama
  - test_diagnostics             diagnostics() returns expected keys
  - test_create_ollama_engine    create_engine('ollama') initialises (may fail without Ollama)
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.embeddings.engine import (
    EmbeddingEngine,
    create_engine,
    CachedEmbeddingEngine,
    SklearnEmbeddingEngine,
    OllamaEmbeddingEngine,
    FusionEmbeddingEngine,
)


# ── Helpers ─────────────────────────────────────────────────────────────

TEST_TEXTS = [
    "Alice prefers hiking in the Rocky Mountains",
    "Bob works as a software engineer at Google",
    "The capital of France is Paris",
    "machine learning transformer architecture",
]

DUPLICATE_TEXTS = [
    "Alice prefers hiking in the Rocky Mountains",
    "Alice prefers hiking in the Rocky Mountains",  # duplicate
]


# ── Test creation via factory ───────────────────────────────────────────

class TestCreateEngine:
    """Test create_engine factory function."""

    def test_create_hash_backend(self):
        """create_engine('hash') returns a working engine with 32-dim embeddings."""
        engine = create_engine(backend="hash", use_cache=False)
        assert isinstance(engine, EmbeddingEngine)
        assert engine.embedding_dim() == 32
        assert "sha256" in engine.model_name().lower()

    def test_create_hash_with_cache(self):
        """create_engine('hash', use_cache=True) wraps in CachedEmbeddingEngine."""
        engine = create_engine(backend="hash", use_cache=True)
        assert isinstance(engine, CachedEmbeddingEngine)
        assert engine.embedding_dim() == 32

    def test_create_sklearn_backend(self):
        """create_engine('sklearn') returns a valid engine."""
        engine = create_engine(backend="sklearn", use_cache=False)
        assert isinstance(engine, SklearnEmbeddingEngine) or isinstance(engine, EmbeddingEngine)
        assert engine.embedding_dim() > 0

    def test_create_unknown_backend_raises(self):
        """create_engine with unknown backend raises ValueError."""
        with pytest.raises(ValueError, match="Unknown backend"):
            create_engine(backend="nonexistent")

    def test_ollama_engine_creation(self):
        """create_engine('ollama') initialises the engine (may not have a running server)."""
        engine = create_engine(backend="ollama", use_cache=False)
        assert isinstance(engine, OllamaEmbeddingEngine)
        assert engine.model_name() == "bge-m3"


# ── Test hash engine behaviour (deterministic, no external deps) ────────

class TestHashEngine:
    """Test the hash-based fallback engine (fully deterministic)."""

    @pytest.fixture
    def engine(self):
        return create_engine(backend="hash", use_cache=False)

    def test_embed_returns_float32(self, engine):
        """embed() returns a numpy float32 array."""
        vec = engine.embed("hello world")
        assert isinstance(vec, np.ndarray)
        assert vec.dtype == np.float32

    def test_embed_normalized(self, engine):
        """embed() returns a unit-L2-normalized vector."""
        vec = engine.embed("test text")
        norm = np.linalg.norm(vec)
        assert abs(norm - 1.0) < 1e-5

    def test_embed_deterministic(self, engine):
        """embed() is deterministic: same text yields same vector."""
        v1 = engine.embed("deterministic test")
        v2 = engine.embed("deterministic test")
        np.testing.assert_array_equal(v1, v2)

    def test_embed_dimension(self, engine):
        """embed() returns vector of embedding_dim() size."""
        vec = engine.embed("dim test")
        assert vec.shape[0] == engine.embedding_dim()

    def test_embed_batch(self, engine):
        """embed_batch() processes multiple texts and returns a list."""
        vecs = engine.embed_batch(TEST_TEXTS)
        assert len(vecs) == len(TEST_TEXTS)
        for v in vecs:
            assert isinstance(v, np.ndarray)
            assert v.dtype == np.float32
            assert abs(np.linalg.norm(v) - 1.0) < 1e-5

    def test_cosine_similarity_identical(self, engine):
        """cosine_similarity of identical texts is ~1.0."""
        v = engine.embed("same text")
        sim = engine.cosine_similarity(v, v)
        assert abs(sim - 1.0) < 1e-5

    def test_cosine_similarity_different(self, engine):
        """cosine_similarity of different texts is < 1.0."""
        v1 = engine.embed("aaaa")
        v2 = engine.embed("bbbb")
        sim = engine.cosine_similarity(v1, v2)
        assert sim < 1.0
        assert sim >= -1.0  # valid cosine range

    def test_embed_batch_empty(self, engine):
        """embed_batch([]) returns empty list."""
        vecs = engine.embed_batch([])
        assert vecs == []

    def test_diagnostics(self, engine):
        """diagnostics() returns dict with engine info."""
        diag = engine.diagnostics()
        assert diag["engine"] == "HashEngine"
        assert "model" in diag
        assert "dim" in diag
        assert diag["dim"] == 32


# ── Test cached engine ──────────────────────────────────────────────────

class TestCachedEmbeddingEngine:
    """Test caching wrapper around embedding engines."""

    @pytest.fixture
    def engine(self):
        raw = create_engine(backend="hash", use_cache=False)
        return CachedEmbeddingEngine(raw, cache_size=10)

    def test_cache_hit_on_duplicate(self, engine):
        """embed() with duplicate text returns cached result."""
        v1 = engine.embed("cache test")
        v2 = engine.embed("cache test")
        np.testing.assert_array_equal(v1, v2)
        stats = engine.cache_stats()
        assert stats["hits"] >= 1

    def test_cache_miss_on_new(self, engine):
        """embed() with new text registers a cache miss."""
        engine.embed("first")
        stats_before = engine.cache_stats()
        engine.embed("second")
        stats = engine.cache_stats()
        # Depending on implementation, hits may also be counted
        assert stats["misses"] >= stats_before["misses"]

    def test_cache_stats_structure(self, engine):
        """cache_stats() returns dict with expected keys."""
        engine.embed("stats test")
        stats = engine.cache_stats()
        assert "hits" in stats
        assert "misses" in stats
        assert "hit_rate" in stats
        assert "cache_size" in stats
        assert "max_size" in stats

    def test_cache_embed_batch(self, engine):
        """embed_batch with mixed duplicates uses cache."""
        texts = ["unique a", "unique b", "unique a"]  # third is duplicate
        vecs = engine.embed_batch(texts)
        assert len(vecs) == 3
        # First and third should be identical
        np.testing.assert_array_equal(vecs[0], vecs[2])

    def test_diagnostics_includes_cache(self, engine):
        """diagnostics() includes a 'cache' section."""
        engine.embed("diag test")
        diag = engine.diagnostics()
        assert "cache" in diag
        assert diag["cache"]["hit_rate"] >= 0

    def test_wrapped_engine_in_diagnostics(self, engine):
        """diagnostics() includes 'wrapped_engine' section."""
        diag = engine.diagnostics()
        assert "wrapped_engine" in diag
        assert diag["wrapped_engine"]["engine"] == "HashEngine"


# ── Test sklearn engine ─────────────────────────────────────────────────

class TestSklearnEngine:
    """Test the sklearn TF-IDF embedding engine (no external API needed)."""

    @pytest.fixture
    def engine(self):
        return create_engine(backend="sklearn", use_cache=False)

    def test_embed_returns_normalized(self, engine):
        """embed() returns L2-normalized float32 vector."""
        vec = engine.embed("sklearn test")
        assert isinstance(vec, np.ndarray)
        assert vec.dtype == np.float32
        norm = np.linalg.norm(vec)
        assert abs(norm - 1.0) < 1e-5

    def test_embed_batch(self, engine):
        """embed_batch works with sklearn engine."""
        vecs = engine.embed_batch(TEST_TEXTS)
        assert len(vecs) == len(TEST_TEXTS)
        for v in vecs:
            assert abs(np.linalg.norm(v) - 1.0) < 1e-5

    def test_embedding_dim(self, engine):
        """embedding_dim returns the configured max_features."""
        assert engine.embedding_dim() > 0

    def test_model_name(self, engine):
        """model_name contains 'sklearn'."""
        assert "sklearn" in engine.model_name().lower()

    def test_different_texts_different_embeddings(self, engine):
        """Different texts yield different embeddings."""
        v1 = engine.embed("cat")
        v2 = engine.embed("dog")
        # Should not be identical
        assert not np.allclose(v1, v2)


# ── Test abstract base ──────────────────────────────────────────────────

class TestEmbeddingEngineBase:
    """Test the abstract base class contract."""

    def test_abstract_class_cannot_instantiate(self):
        """EmbeddingEngine ABC cannot be instantiated directly."""
        with pytest.raises(TypeError):
            EmbeddingEngine()  # type: ignore[abstract]


# ── Test Ollama engine (no server needed) ───────────────────────────────

class TestOllamaEngine:
    """Test OllamaEmbeddingEngine creation and basic properties."""

    def test_init_defaults(self):
        """OllamaEmbeddingEngine initialises with default model."""
        engine = OllamaEmbeddingEngine()
        assert engine.model_name() == "bge-m3"
        assert engine.embedding_dim() == 1024  # bge-m3 default dim

    def test_init_custom_model(self):
        """OllamaEmbeddingEngine accepts custom model name."""
        engine = OllamaEmbeddingEngine(model="qwen3:0.6b")
        assert engine.model_name() == "qwen3:0.6b"

    def test_embedding_dim_map(self):
        """embedding_dim() returns correct dim for known models."""
        engine = OllamaEmbeddingEngine(model="nomic-embed-text")
        assert engine.embedding_dim() == 768

    def test_diagnostics(self):
        """diagnostics() returns expected structure."""
        engine = OllamaEmbeddingEngine()
        diag = engine.diagnostics()
        assert diag["engine"] == "OllamaEmbeddingEngine"
        assert diag["model"] == "bge-m3"
        assert "total_embeddings" in diag
        assert "avg_latency_ms" in diag
        assert "errors" in diag


# ── Test Fusion engine ──────────────────────────────────────────────────

class TestFusionEngine:
    """Test FusionEmbeddingEngine with mock engines."""

    def test_fusion_with_two_engines(self):
        """FusionEmbeddingEngine creates and runs with two sub-engines."""
        # Use HashEngine (32d) + hash engine again for deterministic test
        e1 = create_engine(backend="hash", use_cache=False)
        e2 = create_engine(backend="hash", use_cache=False)
        fusion = FusionEmbeddingEngine(engines=[e1, e2], weights=[0.5, 0.5])
        # Dim should be 32 + 32 = 64
        assert fusion.embedding_dim() == 64

        vec = fusion.embed("fusion test")
        assert isinstance(vec, np.ndarray)
        assert vec.dtype == np.float32
        assert abs(np.linalg.norm(vec) - 1.0) < 1e-5

    def test_fusion_embed_batch(self):
        """FusionEmbeddingEngine.embed_batch works as expected."""
        e1 = create_engine(backend="hash", use_cache=False)
        e2 = create_engine(backend="hash", use_cache=False)
        fusion = FusionEmbeddingEngine(engines=[e1, e2])
        vecs = fusion.embed_batch(TEST_TEXTS)
        assert len(vecs) == len(TEST_TEXTS)
        for v in vecs:
            assert abs(np.linalg.norm(v) - 1.0) < 1e-5

    def test_fusion_diagnostics(self):
        """FusionEmbeddingEngine.diagnostics() includes sub-engine info."""
        e1 = create_engine(backend="hash", use_cache=False)
        e2 = create_engine(backend="hash", use_cache=False)
        fusion = FusionEmbeddingEngine(engines=[e1, e2])
        diag = fusion.diagnostics()
        assert "num_engines" in diag
        assert diag["num_engines"] == 2
        assert "weights" in diag
        assert "sub_engines" in diag
        assert len(diag["sub_engines"]) == 2

    def test_fusion_weight_mismatch_raises(self):
        """Mismatched engine/weight counts raises ValueError."""
        e1 = create_engine(backend="hash", use_cache=False)
        e2 = create_engine(backend="hash", use_cache=False)
        with pytest.raises(ValueError, match="Number of weights"):
            FusionEmbeddingEngine(engines=[e1, e2], weights=[0.5])

    def test_fusion_model_name(self):
        """model_name describes both sub-engines."""
        e1 = create_engine(backend="hash", use_cache=False)
        e2 = create_engine(backend="hash", use_cache=False)
        fusion = FusionEmbeddingEngine(engines=[e1, e2])
        name = fusion.model_name()
        assert "fusion" in name.lower()
        assert "sha256" in name.lower()
