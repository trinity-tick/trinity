"""
Tests for Trinity core layer modules:
  - trinity.core.bridge   — bridge() function dispatch & error handling
  - trinity.core.cache    — engine singleton cache get/set/clear
  - trinity.core.utils    — shared utility functions
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, MagicMock, PropertyMock


# (bridge tests moved to tests/test_bridge.py)


# ============================================================================
# trinity.core.cache
# ============================================================================

class TestCache:
    """Test the engine singleton cache."""

    def setup_method(self):
        from trinity.core import cache
        cache.reset_engine()
        self.cache = cache

    def test_get_engine_returns_instance(self):
        engine = self.cache.get_engine()
        assert engine is not None

    def test_get_engine_caches(self):
        e1 = self.cache.get_engine()
        e2 = self.cache.get_engine()
        assert e1 is e2

    def test_reset_engine_clears_cache(self):
        e1 = self.cache.get_engine()
        self.cache.reset_engine()
        e2 = self.cache.get_engine()
        assert e1 is not e2

    def test_get_engine_status_cached(self):
        self.cache.get_engine()
        assert self.cache.get_engine() is not None

    def test_get_engine_status_not_cached(self):
        self.cache.reset_engine()
        assert self.cache.get_engine() is not None

    def test_reset_engine_no_engine_loaded(self):
        self.cache.reset_engine()
        self.cache.reset_engine()  # should not raise

    def test_double_checked_locking(self):
        engines = []
        import threading
        def get():
            engines.append(self.cache.get_engine())
        threads = [threading.Thread(target=get) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert all(e is engines[0] for e in engines)


# ============================================================================
# trinity.core.utils
# ============================================================================

class TestExtractKeywords:
    def test_empty_string(self):
        from trinity.core.utils import extract_keywords
        assert extract_keywords("") == []

    def test_only_stop_words(self):
        from trinity.core.utils import extract_keywords
        assert extract_keywords("the a an is") == []

    def test_filters_stop_words(self):
        from trinity.core.utils import extract_keywords
        kw = extract_keywords("the quick brown fox")
        assert "the" not in kw
        assert "quick" in kw

    def test_single_char_filtered(self):
        from trinity.core.utils import extract_keywords
        kw = extract_keywords("a b c d word")
        assert "word" in kw

    def test_chinese_text(self):
        from trinity.core.utils import extract_keywords
        kw = extract_keywords("用户喜欢暗色模式")
        assert len(kw) > 0
        assert any(len(t) >= 2 for t in kw)

    def test_punctuation_removed(self):
        from trinity.core.utils import extract_keywords
        kw = extract_keywords("hello, world! test...")
        assert "hello" in kw
        assert "world" in kw


class TestEncodeToEmbedding:
    def test_default_dimension(self):
        from trinity.core.utils import encode_to_embedding
        emb = encode_to_embedding("test")
        assert len(emb) == 64

    def test_custom_dimension(self):
        from trinity.core.utils import encode_to_embedding
        emb = encode_to_embedding("test", dim=128)
        assert len(emb) == 128

    def test_deterministic(self):
        from trinity.core.utils import encode_to_embedding
        assert encode_to_embedding("hello") == encode_to_embedding("hello")

    def test_different_inputs_differ(self):
        from trinity.core.utils import encode_to_embedding
        assert encode_to_embedding("hello") != encode_to_embedding("world")

    def test_min_dimension(self):
        from trinity.core.utils import encode_to_embedding
        emb = encode_to_embedding("x", dim=1)
        assert len(emb) == 1


class TestCosineSimilarity:
    def test_identical_vectors(self):
        from trinity.core.utils import cosine_similarity
        assert abs(cosine_similarity([1, 0], [1, 0]) - 1.0) < 1e-9

    def test_orthogonal_vectors(self):
        from trinity.core.utils import cosine_similarity
        assert cosine_similarity([1, 0], [0, 1]) == 0.0

    def test_opposite_vectors(self):
        from trinity.core.utils import cosine_similarity
        assert abs(cosine_similarity([1, 0], [-1, 0]) - (-1.0)) < 1e-9

    def test_zero_vector_first(self):
        from trinity.core.utils import cosine_similarity
        assert cosine_similarity([0, 0], [1, 0]) == 0.0

    def test_zero_vector_second(self):
        from trinity.core.utils import cosine_similarity
        assert cosine_similarity([1, 0], [0, 0]) == 0.0

    def test_both_zero_vectors(self):
        from trinity.core.utils import cosine_similarity
        assert cosine_similarity([0, 0], [0, 0]) == 0.0

    def test_single_element(self):
        from trinity.core.utils import cosine_similarity
        assert abs(cosine_similarity([2.0], [4.0]) - 1.0) < 1e-9


class TestJaccardSimilarity:
    def test_identical_sets(self):
        from trinity.core.utils import jaccard_similarity
        assert jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint_sets(self):
        from trinity.core.utils import jaccard_similarity
        assert jaccard_similarity({"a"}, {"b"}) == 0.0

    def test_partial_overlap(self):
        from trinity.core.utils import jaccard_similarity
        assert jaccard_similarity({"a", "b"}, {"b", "c"}) == 1/3

    def test_both_empty(self):
        from trinity.core.utils import jaccard_similarity
        assert jaccard_similarity(set(), set()) == 1.0

    def test_one_empty(self):
        from trinity.core.utils import jaccard_similarity
        assert jaccard_similarity({"a"}, set()) == 0.0

    def test_string_sets(self):
        from trinity.core.utils import jaccard_similarity
        assert jaccard_similarity({"hello"}, {"hello"}) == 1.0


class TestNormalizeAndTokenize:
    def test_basic(self):
        from trinity.core.utils import normalize_and_tokenize
        assert normalize_and_tokenize("Hello, World!") == ["hello", "world"]

    def test_empty_string(self):
        from trinity.core.utils import normalize_and_tokenize
        assert normalize_and_tokenize("") == []

    def test_only_punctuation(self):
        from trinity.core.utils import normalize_and_tokenize
        assert normalize_and_tokenize("!!! ???") == []

    def test_mixed_case(self):
        from trinity.core.utils import normalize_and_tokenize
        result = normalize_and_tokenize("Foo BAR baz")
        assert "foo" in result and "bar" in result

    def test_extra_whitespace(self):
        from trinity.core.utils import normalize_and_tokenize
        assert normalize_and_tokenize("   hello    world   ") == ["hello", "world"]

    def test_numbers_preserved(self):
        from trinity.core.utils import normalize_and_tokenize
        assert "123" in normalize_and_tokenize("test 123")


class TestComputeSignature:
    def test_default_length(self):
        from trinity.core.utils import compute_signature
        assert len(compute_signature("hello")) == 8

    def test_custom_length(self):
        from trinity.core.utils import compute_signature
        assert len(compute_signature("hello", length=4)) == 4

    def test_deterministic(self):
        from trinity.core.utils import compute_signature
        assert compute_signature("test") == compute_signature("test")

    def test_different_inputs_differ(self):
        from trinity.core.utils import compute_signature
        assert compute_signature("a") != compute_signature("b")

    def test_empty_string(self):
        from trinity.core.utils import compute_signature
        assert len(compute_signature("")) == 8

    def test_min_length(self):
        from trinity.core.utils import compute_signature
        assert len(compute_signature("x", length=1)) == 1


class TestEstimateTokenCount:
    def test_empty_string(self):
        from trinity.core.utils import estimate_token_count
        assert estimate_token_count("") == 1

    def test_short_string(self):
        from trinity.core.utils import estimate_token_count
        assert estimate_token_count("hello") == 1  # 5 // 4 = 1

    def test_exact_division(self):
        from trinity.core.utils import estimate_token_count
        assert estimate_token_count("abcd") == 1

    def test_medium_text(self):
        from trinity.core.utils import estimate_token_count
        assert estimate_token_count("hello world") == 2  # 11 // 4 = 2

    def test_long_text(self):
        from trinity.core.utils import estimate_token_count
        assert estimate_token_count("a" * 100) == 25  # 100 // 4 = 25

    def test_unicode_text(self):
        from trinity.core.utils import estimate_token_count
        assert estimate_token_count("你好世界") >= 1


class TestDiagnosticsCollect:
    def test_collects_attributes(self):
        from trinity.core.utils import diagnostics_collect
        class Obj:
            def __init__(self):
                self.a = True
                self.b = False
        obj = Obj()
        result = diagnostics_collect(obj, [("a", "check_a"), ("b", "check_b")])
        assert result["check_a"] is True
        assert result["check_b"] is False

    def test_collects_callable(self):
        from trinity.core.utils import diagnostics_collect
        class Obj:
            def is_ok(self):
                return True
        obj = Obj()
        result = diagnostics_collect(obj, [("is_ok", "healthy")])
        assert result["healthy"] is True

    def test_missing_attribute(self):
        from trinity.core.utils import diagnostics_collect
        class Obj:
            pass
        obj = Obj()
        result = diagnostics_collect(obj, [("nonexistent", "missing")])
        assert result["missing"] is False

    def test_empty_checks(self):
        from trinity.core.utils import diagnostics_collect
        assert diagnostics_collect(object(), []) == {}

    def test_mixed_attrs_and_callables(self):
        from trinity.core.utils import diagnostics_collect
        class Obj:
            flag = True
            def check(self):
                return 42
        obj = Obj()
        result = diagnostics_collect(obj, [("flag", "f"), ("check", "c")])
        assert result["f"] is True
        assert result["c"] is True  # non-None return
