"""P0-1: reranker default-ON gate (COMPARISON_VS_2026_SOTA_R7).

Verifies:
  - enable_reranker=None → env TRINITY_RERANKER (default "on" → True)
  - explicit True/False always wins over env
  - sticky failure: reranker degrades to no-op after failed model load
    instead of retrying the import on every search
"""

import pytest

from trinity.vector_index.mixed import HybridIndex, create_hybrid_index
from trinity.vector_index.reranker import CrossEncoderReranker


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("TRINITY_RERANKER", raising=False)


def test_default_on_when_env_unset():
    idx = HybridIndex(dim=8)
    assert idx._enable_reranker is True


@pytest.mark.parametrize("val", ["on", "1", "true", "yes", "ON", "True"])
def test_env_on_values(monkeypatch, val):
    monkeypatch.setenv("TRINITY_RERANKER", val)
    idx = HybridIndex(dim=8)
    assert idx._enable_reranker is True


@pytest.mark.parametrize("val", ["off", "0", "false", "no", "OFF"])
def test_env_off_values(monkeypatch, val):
    monkeypatch.setenv("TRINITY_RERANKER", val)
    idx = HybridIndex(dim=8)
    assert idx._enable_reranker is False


def test_explicit_true_wins_over_env_off(monkeypatch):
    monkeypatch.setenv("TRINITY_RERANKER", "off")
    idx = HybridIndex(dim=8, enable_reranker=True)
    assert idx._enable_reranker is True


def test_explicit_false_wins_over_env_on():
    idx = HybridIndex(dim=8, enable_reranker=False)
    assert idx._enable_reranker is False


def test_create_hybrid_index_default_on():
    idx = create_hybrid_index(dim=8)
    assert idx._enable_reranker is True


def test_create_hybrid_index_explicit_off():
    idx = create_hybrid_index(dim=8, enable_reranker=False)
    assert idx._enable_reranker is False


def test_statistics_exposes_flag():
    idx = HybridIndex(dim=8)
    stats = idx.statistics()
    assert stats["enable_reranker"] is True
    assert stats["reranker"]["loaded"] is False


def test_reranker_sticky_failure(monkeypatch):
    """Failed load → no-op permanently; no repeated import attempts."""
    import sys

    rk = CrossEncoderReranker(model_name="fast")

    # Block the sentence_transformers import → CrossEncoder load fails
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    candidates = [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}]
    r1 = rk.rerank("q", candidates, top_k=2)
    assert r1 == candidates  # identity no-op
    assert rk._model_failed is True

    # second call must not retry the import (still fine, no exception)
    r2 = rk.rerank("q", candidates, top_k=2)
    assert r2 == candidates
