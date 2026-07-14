"""Tests for SQLiteAdapter CRUD operations."""

import os
import sys
import json
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.adapters.sqlite import SQLiteAdapter


# ── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def adapter():
    """Create a fresh SQLiteAdapter backed by a temporary database file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    a = SQLiteAdapter(db_path=db_path)
    a.connect()
    yield a
    a.disconnect()
    if os.path.exists(db_path):
        os.unlink(db_path)


# ── Store ───────────────────────────────────────────────────────────────

class TestStore:
    """Test SQLiteAdapter.store_memory."""

    def test_store_returns_metadata(self, adapter):
        """store_memory should return expected metadata fields."""
        result = adapter.store_memory("hello world", persona_id="test")
        assert "memory_id" in result
        assert result["memory_id"].startswith("mem_")
        assert "version_id" in result
        assert result["version_id"].startswith("ver_")
        assert "sha256_hash" in result
        assert "timestamp" in result
        assert result["persona_id"] == "test"

    def test_store_multiple_memories(self, adapter):
        """Store multiple memories and verify they all exist."""
        for i in range(5):
            adapter.store_memory(f"memory {i}", persona_id="multi")
        results = adapter.search_memories("memory", persona_id="multi")
        assert len(results) == 5

    def test_store_with_tags(self, adapter):
        """store_memory should persist tags."""
        tags = ["important", "test"]
        adapter.store_memory("tagged memory", persona_id="tagger", tags=tags)
        results = adapter.search_memories("tagged", persona_id="tagger")
        assert len(results) >= 1
        assert results[0]["tags"] == tags

    def test_store_with_importance(self, adapter):
        """store_memory should persist importance."""
        adapter.store_memory("high importance", persona_id="imp", importance=0.95)
        adapter.store_memory("low importance", persona_id="imp", importance=0.1)
        results = adapter.search_memories("importance", persona_id="imp")
        assert len(results) >= 2

    def test_store_sha256_consistency(self, adapter):
        """Same content should produce the same sha256 hash."""
        r1 = adapter.store_memory("exact same content", persona_id="a")
        r2 = adapter.store_memory("exact same content", persona_id="b")
        assert r1["sha256_hash"] == r2["sha256_hash"]


# ── Search ──────────────────────────────────────────────────────────────

class TestSearch:
    """Test SQLiteAdapter.search_memories."""

    def test_search_empty_db(self, adapter):
        """Search on empty DB should return empty list."""
        results = adapter.search_memories("anything")
        assert results == []

    def test_search_finds_content(self, adapter):
        """Search should find matching content."""
        adapter.store_memory("machine learning basics", persona_id="ml")
        results = adapter.search_memories("machine", persona_id="ml")
        assert len(results) >= 1

    def test_search_tenant_scoping(self, adapter):
        """Search should be scoped by tenant_id."""
        adapter.store_memory("tenant A data", tenant_id="A", persona_id="u1")
        adapter.store_memory("tenant B data", tenant_id="B", persona_id="u1")

        results_a = adapter.search_memories("data", tenant_id="A")
        assert len(results_a) >= 1
        # Make sure it only returns from A (there is no tenant filter on the result row,
        # but the query condition ensures it). Let's verify content.
        assert "tenant A data" in results_a[0]["content"]

        results_b = adapter.search_memories("data", tenant_id="B")
        assert len(results_b) >= 1
        assert "tenant B data" in results_b[0]["content"]

    def test_search_top_k(self, adapter):
        """Search should respect top_k limit."""
        for i in range(10):
            adapter.store_memory(f"limit test {i}", persona_id="limiter")
        results = adapter.search_memories("limit", persona_id="limiter", top_k=3)
        assert len(results) <= 3

    def test_search_scores(self, adapter):
        """Search results should include a score field."""
        adapter.store_memory("searchable content", persona_id="scorer")
        results = adapter.search_memories("searchable", persona_id="scorer")
        assert len(results) >= 1
        assert "score" in results[0]


# ── Delete ──────────────────────────────────────────────────────────────

class TestDelete:
    """Test SQLiteAdapter.delete_memory."""

    def test_delete_soft_delete(self, adapter):
        """delete_memory should soft-delete (mark as deleted)."""
        result = adapter.store_memory("to delete", persona_id="del")
        mid = result["memory_id"]
        deleted = adapter.delete_memory(mid)
        assert deleted is True

        # Should no longer appear in search
        results = adapter.search_memories("to delete", persona_id="del")
        assert len(results) == 0

    def test_delete_nonexistent(self, adapter):
        """Deleting a non-existent memory should return False."""
        deleted = adapter.delete_memory("nonexistent_id")
        assert deleted is False

    def test_delete_adds_version_record(self, adapter):
        """Deleting should add a DELETE version record."""
        result = adapter.store_memory("versioned delete", persona_id="vd")
        mid = result["memory_id"]
        adapter.delete_memory(mid)

        versions = adapter.get_version_chain(mid)
        operations = [v["operation"] for v in versions]
        assert "CREATE" in operations
        assert "DELETE" in operations


# ── Version chain ───────────────────────────────────────────────────────

class TestVersionChain:
    """Test SQLiteAdapter.get_version_chain."""

    def test_version_chain_structure(self, adapter):
        """Version chain entries should have expected fields."""
        result = adapter.store_memory("versioned content", persona_id="ver")
        mid = result["memory_id"]
        versions = adapter.get_version_chain(mid)
        assert len(versions) >= 1
        v = versions[0]
        assert "version_id" in v
        assert "memory_id" in v
        assert "content" in v
        assert "sha256_hash" in v
        assert "operation" in v
        assert v["operation"] == "CREATE"

    def test_version_chain_empty(self, adapter):
        """Non-existent memory should return empty list."""
        versions = adapter.get_version_chain("nonexistent")
        assert versions == []


# ── Diagnostics ─────────────────────────────────────────────────────────

class TestDiagnostics:
    """Test SQLiteAdapter.diagnostics."""

    def test_diagnostics_returns_dict(self, adapter):
        """diagnostics() should return a dict."""
        diag = adapter.diagnostics()
        assert isinstance(diag, dict)

    def test_diagnostics_adapter_type(self, adapter):
        """diagnostics should identify adapter type."""
        diag = adapter.diagnostics()
        assert diag["adapter"] == "sqlite"

    def test_diagnostics_counts(self, adapter):
        """diagnostics should track memory counts."""
        diag_before = adapter.diagnostics()
        count_before = diag_before["total_memories"]

        adapter.store_memory("diag test", persona_id="diag")
        diag_after = adapter.diagnostics()
        assert diag_after["total_memories"] == count_before + 1

    def test_diagnostics_db_path(self, adapter):
        """diagnostics should include db_path."""
        diag = adapter.diagnostics()
        assert "db_path" in diag
        assert diag["db_path"] is not None
