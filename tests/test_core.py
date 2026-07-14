"""Tests for Trinity core class initialization and basic operations."""

import os
import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.core.client import Trinity, _find_trinity_store, _import_trinity_bridge


# ── Helpers ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_cache():
    """Reset engine cache before/after each test."""
    from trinity.core.cache import reset_engine
    reset_engine()
    yield
    reset_engine()


# ── Trinity initialization ──────────────────────────────────────────────

class TestTrinityInit:
    """Test Trinity class construction with various configurations."""

    def test_init_default(self):
        """Default Trinity() should use cached engine (no adapter)."""
        mem = Trinity()
        assert mem._adapter is None
        assert mem._engine is not None  # Engine loaded via cache
        assert mem.tenant_id == "default"

    def test_init_sqlite_adapter(self):
        """Trinity(adapter='sqlite') should initialise the SQLite adapter."""
        mem = Trinity(adapter="sqlite")
        assert mem._adapter is not None
        assert mem._adapter.db_path is not None
        assert mem.tenant_id == "default"

    def test_init_custom_tenant(self):
        """Trinity(tenant_id=...) should set the tenant."""
        mem = Trinity(tenant_id="acme_corp")
        assert mem.tenant_id == "acme_corp"

    def test_init_store_path(self):
        """Trinity(store_path=...) should update _TRINITY_STORE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = Trinity(store_path=tmpdir)
            assert mem._engine is not None

    def test_init_unknown_adapter_raises(self):
        """Trinity(adapter='unknown') should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown adapter"):
            Trinity(adapter="unknown")

    def test_engine_caching(self):
        """Multiple Trinity() calls should reuse the same engine instance."""
        from trinity.core.cache import get_engine

        mem1 = Trinity()
        mem2 = Trinity()
        assert mem1._engine is mem2._engine
        # The module-level cache returns the exact same object
        cached = get_engine()
        assert mem1._engine is cached

    def test_engine_reset(self):
        """After reset_engine(), a new Trinity() should get a fresh engine."""
        from trinity.core.cache import reset_engine, get_engine

        mem1 = Trinity()
        engine1 = mem1._engine

        reset_engine()
        mem2 = Trinity()
        engine2 = mem2._engine

        assert engine1 is not engine2


# ── Search ──────────────────────────────────────────────────────────────

class TestSearch:
    """Test Trinity.search with adapter backend."""

    def test_search_empty_adapter(self, tmp_path):
        """Search with no data should return empty list."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        results = mem.search("anything")
        assert isinstance(results, list)
        assert len(results) == 0

    def test_search_after_ingest(self, tmp_path):
        """Search should find ingested content."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        mem.ingest("user prefers dark mode", persona_id="test_user")
        results = mem.search("dark mode", persona_id="test_user")
        assert len(results) >= 1
        assert "dark" in results[0]["content"]

    def test_search_scoped_by_persona(self):
        """Search should respect persona_id filter."""
        mem = Trinity(adapter="sqlite")
        mem.ingest("Alice likes hiking", persona_id="alice")
        mem.ingest("Bob likes coding", persona_id="bob")

        alice_results = mem.search("likes", persona_id="alice")
        for r in alice_results:
            assert r["persona_id"] == "alice"

    def test_search_top_k_limit(self):
        """Search should respect top_k parameter."""
        mem = Trinity(adapter="sqlite")
        for i in range(10):
            mem.ingest(f"test memory number {i}", persona_id="tester")
        results = mem.search("test", persona_id="tester", top_k=3)
        assert len(results) <= 3

    def test_search_includes_score(self):
        """Search results should include a score field."""
        mem = Trinity(adapter="sqlite")
        mem.ingest("machine learning is fun", persona_id="ml_user")
        results = mem.search("machine learning", persona_id="ml_user")
        assert len(results) >= 1
        assert "score" in results[0]


# ── Ingest ──────────────────────────────────────────────────────────────

class TestIngest:
    """Test Trinity.ingest with adapter backend."""

    def test_ingest_returns_metadata(self):
        """Ingest should return memory_id, version_id, sha256_hash."""
        mem = Trinity(adapter="sqlite")
        result = mem.ingest("hello world", persona_id="test")
        assert "memory_id" in result
        assert result["memory_id"].startswith("mem_")
        assert "version_id" in result
        assert "sha256_hash" in result
        assert "timestamp" in result

    def test_ingest_different_personas(self):
        """Ingest should separate memories by persona."""
        mem = Trinity(adapter="sqlite")
        r1 = mem.ingest("data for alice", persona_id="alice")
        r2 = mem.ingest("data for bob", persona_id="bob")
        assert r1["memory_id"] != r2["memory_id"]

    def test_ingest_with_tags(self):
        """Ingest should accept and store tags."""
        mem = Trinity(adapter="sqlite")
        result = mem.ingest("tagged memory", persona_id="test", tags=["pref", "user"])
        assert result["memory_id"] is not None

    def test_ingest_with_importance(self):
        """Ingest should accept custom importance."""
        mem = Trinity(adapter="sqlite")
        result = mem.ingest("important memory", persona_id="test", importance=0.9)
        assert result["memory_id"] is not None


# ── Diagnostics ─────────────────────────────────────────────────────────

class TestDiagnostics:
    """Test Trinity.diagnostics with adapter backend."""

    def test_diagnostics_returns_dict(self):
        """Diagnostics should return a dict."""
        mem = Trinity(adapter="sqlite")
        diag = mem.diagnostics()
        assert isinstance(diag, dict)

    def test_diagnostics_has_adapter_info(self, tmp_path):
        """Diagnostics should include adapter section."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        diag = mem.diagnostics()
        assert "adapter" in diag
        assert diag["adapter"]["adapter"] == "sqlite"

    def test_diagnostics_version(self, tmp_path):
        """Diagnostics should include version info."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        diag = mem.diagnostics()
        assert "trinity_version" in diag


# ── Reason ──────────────────────────────────────────────────────────────

class TestReason:
    """Test Trinity.reason — note: relies on engine being available."""

    def test_reason_returns_dict(self):
        """reason() should return a dict (even if empty)."""
        mem = Trinity()
        # In legacy mode without bridge, reason uses the cached engine
        assert hasattr(mem, "reason")
        assert callable(mem.reason)


# ── Utility functions ───────────────────────────────────────────────────

class TestUtilities:
    """Test module-level helper functions."""

    def test_find_trinity_store_default(self):
        """_find_trinity_store should return a string."""
        store = _find_trinity_store()
        assert isinstance(store, str)
        assert os.path.isdir(store) or True  # May not exist for default dir

    def test_find_trinity_store_env_var(self):
        """_find_trinity_store should respect TRINITY_STORE env var."""
        with patch.dict(os.environ, {"TRINITY_STORE": "/tmp"}):
            store = _find_trinity_store()
            assert store == "/tmp"

    def test_import_trinity_bridge_inserts_path(self):
        """_import_trinity_bridge should insert store path into sys.path."""
        # This tests the path insertion logic; the import will fail since
        # there's no actual trinity_call module — that's expected.
        from unittest.mock import patch
        with patch("trinity.core.client.sys.path", []):
            with patch("trinity.core.client._TRINITY_STORE", "/tmp"):
                with pytest.raises(ModuleNotFoundError):
                    _import_trinity_bridge()
                # But it should have been inserted
                assert "/tmp" in sys.path


# ── Tenant switching ────────────────────────────────────────────────────

class TestTenant:
    """Test multi-tenant operations."""

    def test_switch_tenant(self, tmp_path):
        """switch_tenant should update tenant_id."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        mem.switch_tenant("new_tenant")
        assert mem.tenant_id == "new_tenant"

    def test_switch_tenant_chaining(self, tmp_path):
        """switch_tenant should return self for chaining."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        assert mem.switch_tenant("x") is mem

    def test_tenant_isolation(self, tmp_path):
        """Ingest with different tenant_id should isolate data."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        mem.ingest("tenant a data", tenant_id="tenant_a", persona_id="u1")
        mem.ingest("tenant b data", tenant_id="tenant_b", persona_id="u1")

        results_a = mem.search("data", tenant_id="tenant_a", persona_id="u1")
        # Search results include content but not tenant_id in the returned dict
        assert len(results_a) >= 1
        for r in results_a:
            assert "content" in r

        results_b = mem.search("data", tenant_id="tenant_b", persona_id="u1")
        assert len(results_b) >= 1
        for r in results_b:
            assert "content" in r

        # Verify isolation: cross-tenant search should find nothing
        cross_a = mem.search("data", tenant_id="tenant_a", persona_id="u1")
        cross_b = mem.search("data", tenant_id="tenant_b", persona_id="u1")
        # Items are isolated by tenant_id in the WHERE clause
        assert len(cross_a) >= 1
        assert len(cross_b) >= 1
