"""Tests for trinity.core.utils."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.core.utils import (
    extract_keywords,
    encode_to_embedding,
    cosine_similarity,
    jaccard_similarity,
    normalize_and_tokenize,
    compute_signature,
    estimate_token_count,
)


class TestExtractKeywords:
    def test_empty(self):
        assert extract_keywords("") == []

    def test_stop_words_filtered(self):
        kw = extract_keywords("the quick brown fox")
        assert "the" not in kw
        assert "quick" in kw

    def test_chinese(self):
        kw = extract_keywords("用户喜欢暗色模式")
        # Chinese chars are kept as a single token; just verify we got tokens
        assert len(kw) > 0
        assert any(len(t) >= 2 for t in kw)


class TestEmbedding:
    def test_dimension(self):
        emb = encode_to_embedding("test", dim=64)
        assert len(emb) == 64

    def test_deterministic(self):
        e1 = encode_to_embedding("hello")
        e2 = encode_to_embedding("hello")
        assert e1 == e2

    def test_different_inputs_different(self):
        e1 = encode_to_embedding("hello")
        e2 = encode_to_embedding("world")
        assert e1 != e2

    def test_cosine_same(self):
        e = encode_to_embedding("same")
        assert abs(cosine_similarity(e, e) - 1.0) < 1e-9

    def test_cosine_orthogonal(self):
        assert cosine_similarity([1, 0], [0, 1]) == 0.0

    def test_cosine_zero_vector(self):
        assert cosine_similarity([0, 0], [1, 0]) == 0.0


class TestJaccard:
    def test_identical(self):
        assert jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint(self):
        assert jaccard_similarity({"a"}, {"b"}) == 0.0

    def test_both_empty(self):
        assert jaccard_similarity(set(), set()) == 1.0


class TestNormalize:
    def test_basic(self):
        assert normalize_and_tokenize("Hello, World!") == ["hello", "world"]

    def test_empty(self):
        assert normalize_and_tokenize("") == []


class TestSignature:
    def test_length(self):
        assert len(compute_signature("hello")) == 8

    def test_consistent(self):
        assert compute_signature("test") == compute_signature("test")


class TestTokenCount:
    def test_empty(self):
        assert estimate_token_count("") == 1

    def test_approximate(self):
        assert estimate_token_count("hello world") == 2  # 11 chars // 4
