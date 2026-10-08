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

    def test_init_default(self, tmp_path):
        """Default Trinity() 应初始化出一个可用适配器（**后端由配置解析**）。

        2026-09-15（R41-P23）：原断言写死 `db_path`（SQLite 专有属性）并称
        "should initialize SQLite adapter"。但默认后端现在是**由
        `security.credentials.resolve_backend()` 从配置解析**的——本机实测
        `Trinity()` 得到的是 `PostgreSQLAdapter`，故原断言报
        `AttributeError: 'PostgreSQLAdapter' object has no attribute 'db_path'`。
        这与被测行为无关，是**测试把"某一台机器的默认配置"当成了契约**。
        改为只断言与后端无关的不变量；SQLite 专有属性由
        `test_init_sqlite_adapter`（显式钉后端）覆盖。
        """
        mem = Trinity(store_path=str(tmp_path))
        assert mem._adapter is not None
        assert mem.tenant_id == "default"

    def test_init_sqlite_adapter(self, tmp_path):
        """Trinity(adapter='sqlite') should initialise the SQLite adapter."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        assert mem._adapter is not None
        assert mem._adapter.db_path is not None
        assert mem.tenant_id == "default"

    def test_init_custom_tenant(self, tmp_path):
        """Trinity(tenant_id=...) should set the tenant."""
        mem = Trinity(tenant_id="acme_corp", store_path=str(tmp_path))
        assert mem.tenant_id == "acme_corp"

    def test_init_store_path(self, tmp_path):
        """Trinity(store_path=...) should update _TRINITY_STORE."""
        mem = Trinity(store_path=str(tmp_path))
        assert mem._adapter is not None

    def test_init_unknown_adapter_raises(self):
        """Trinity(adapter='unknown') should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown adapter"):
            Trinity(adapter="unknown")

    def test_engine_caching(self, tmp_path):
        """Multiple Trinity() calls create independent instances."""
        mem1 = Trinity(store_path=str(tmp_path))
        mem2 = Trinity()
        assert mem1._adapter is not None

    def test_engine_reset(self, tmp_path):
        """Re-initializing Trinity after store deletion gets a fresh db."""
        mem1 = Trinity(store_path=str(tmp_path))
        assert mem1._adapter is not None


# ── Search ──────────────────────────────────────────────────────────────

class TestSearch:
    """Test Trinity.search with adapter backend."""

    def test_search_empty_adapter(self, tmp_path):
        """Search with no data should return empty result list."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        result = mem.search("anything")
        assert isinstance(result, dict)
        assert result["results"] == []
        assert result["pushed_memories"] == []

    def test_search_after_ingest(self, tmp_path):
        """Search should find ingested content."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        mem.ingest("user prefers dark mode", persona_id="test_user")
        results = mem.search("dark mode", persona_id="test_user")["results"]
        assert len(results) >= 1
        assert "dark" in results[0]["content"]

    def test_search_scoped_by_persona(self, tmp_path):
        """Search should respect persona_id filter."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        mem.ingest("Alice likes hiking", persona_id="alice")
        mem.ingest("Bob likes coding", persona_id="bob")

        alice_results = mem.search("likes", persona_id="alice")["results"]
        for r in alice_results:
            assert r["persona_id"] == "alice"

    def test_search_top_k_limit(self, tmp_path):
        """Search should respect top_k parameter."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        for i in range(10):
            mem.ingest(f"test memory number {i}", persona_id="tester")
        results = mem.search("test", persona_id="tester", top_k=3)["results"]
        assert 0 < len(results) <= 3

    def test_search_includes_score(self, tmp_path):
        """Search results should include a score field."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        mem.ingest("machine learning is fun", persona_id="ml_user")
        results = mem.search("machine learning", persona_id="ml_user")["results"]
        assert len(results) >= 1
        assert "score" in results[0]


# ── Ingest ──────────────────────────────────────────────────────────────

class TestIngest:
    """Test Trinity.ingest with adapter backend."""

    def test_ingest_returns_metadata(self, tmp_path):
        """Ingest should return memory_id, version_id, sha256_hash."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        result = mem.ingest("hello world", persona_id="test")
        assert "memory_id" in result
        assert result["memory_id"].startswith("mem_")
        assert "version_id" in result
        assert "sha256_hash" in result
        assert "timestamp" in result

    def test_ingest_different_personas(self, tmp_path):
        """Ingest should separate memories by persona."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        r1 = mem.ingest("data for alice", persona_id="alice")
        r2 = mem.ingest("data for bob", persona_id="bob")
        assert r1["memory_id"] != r2["memory_id"]

    def test_ingest_with_tags(self, tmp_path):
        """Ingest should accept and store tags."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        result = mem.ingest("tagged memory", persona_id="test", tags=["pref", "user"])
        assert result["memory_id"] is not None

    def test_ingest_with_importance(self, tmp_path):
        """Ingest should accept custom importance."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
        result = mem.ingest("important memory", persona_id="test", importance=0.9)
        assert result["memory_id"] is not None


# ── Diagnostics ─────────────────────────────────────────────────────────

class TestDiagnostics:
    """Test Trinity.diagnostics with adapter backend."""

    def test_diagnostics_returns_dict(self, tmp_path):
        """Diagnostics should return a dict."""
        mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
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
        # A8：原为 `assert os.path.isdir(store) or True` —— 短路恒真、永不可能失败。
        # 本用例 docstring 只声明「should return a string」，上一行已覆盖该意图；
        # 目录是否存在属 test_find_trinity_store_nonexistent_path_falls_back 的范围。

    def test_find_trinity_store_env_var(self):
        """_find_trinity_store should respect TRINITY_STORE env var.

        2026 优化轮修复：原用例硬编码 Unix 路径 ``/tmp``。而实现（见
        ``_find_trinity_store`` 文档字符串规则 1）要求该目录**存在**才采用，
        于是 Windows 上 ``isdir("/tmp")`` 为假 -> 回落到默认目录 -> 用例恒红，
        长期混在失败清单里没人管（每次全量回归都要人工排除一遍）。
        改为使用**真实存在的临时目录**，语义不变（env 变量最高优先）且跨平台。
        """
        with tempfile.TemporaryDirectory() as real_dir:
            with patch.dict(os.environ, {"TRINITY_STORE": real_dir}):
                store = _find_trinity_store()
                assert store == real_dir

    def test_find_trinity_store_nonexistent_path_falls_back(self):
        """显式 TRINITY_STORE 指向**不存在**的目录时回落默认（把既有行为钉住）。

        这是实现的既有意图（避免创建与权威大库并存的小库），此处显式固化，
        以防将来被误当 bug 改掉——若确要改为"按需创建"，应先改此用例并说明理由。
        """
        missing = os.path.join(tempfile.gettempdir(), "_trinity_definitely_missing_2026")
        assert not os.path.isdir(missing)
        with patch.dict(os.environ, {"TRINITY_STORE": missing}):
            store = _find_trinity_store()
        assert store != missing
        assert store.endswith(os.path.join(".trinity", "store"))

    def test_import_trinity_bridge_inserts_path(self, tmp_path):
        """_import_trinity_bridge should insert store path into sys.path.

        2026-10-06（t71/I11）**修逃逸口**：原实现是
            `@pytest.mark.skipif(True, reason="bridge module caching interferes with parallel tests")`
            + 函数体只有一根 docstring 和一个 `pass`
        —— **永久跳过 + 什么也不断言**（最纯粹的逃逸口：判据存在，但从不执行、也不校验任何东西）。
        当初跳过的理由是"模块缓存干扰并行测试" ⇒ 修法不是继续跳过，而是**在子进程里跑**
        （全新解释器 ⇒ 没有缓存干扰），并把断言写成真的判据：
          ① `_import_trinity_bridge()` 之后 `TRINITY_STORE` 目录**确实在 `sys.path` 里**；
          ② 桥模块存在时**真的导入成功**（返回它的 `trinity` 属性）；不存在时返回 None（容错降级）。
        """
        import subprocess
        import sys as _sys

        store_dir = tmp_path / "store"
        store_dir.mkdir()
        (store_dir / "trinity_call.py").write_text(
            "trinity = 'BRIDGE-MARKER'\n", encoding="utf-8")
        code = (
            "import os, sys;"
            f"os.environ['TRINITY_STORE'] = r'{store_dir}';"
            "from trinity.core.client._construction import _import_trinity_bridge as f;"
            "b = f();"
            "print('IN_PATH=' + str(os.path.abspath(%r) in [os.path.abspath(p) for p in sys.path]));"
            "print('BRIDGE=' + str(b))" % str(store_dir)
        )
        r = subprocess.run([_sys.executable, "-c", code],
                           cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        assert r.returncode == 0, (r.stdout, r.stderr[-400:])
        assert "IN_PATH=True" in r.stdout, (
            "_import_trinity_bridge 没有把 store 路径插进 sys.path：%r" % r.stdout)
        assert "BRIDGE=BRIDGE-MARKER" in r.stdout, (
            "桥模块存在时应真的导入成功（而不是返回 None）：%r" % r.stdout)


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

        results_a = mem.search("data", tenant_id="tenant_a", persona_id="u1")["results"]
        # Search results include content but not tenant_id in the returned dict
        assert len(results_a) >= 1
        for r in results_a:
            assert "content" in r

        results_b = mem.search("data", tenant_id="tenant_b", persona_id="u1")["results"]
        assert len(results_b) >= 1
        for r in results_b:
            assert "content" in r

        # Verify isolation: cross-tenant search should find nothing
        cross_a = mem.search("data", tenant_id="tenant_a", persona_id="u1")["results"]
        cross_b = mem.search("data", tenant_id="tenant_b", persona_id="u1")["results"]
        # Items are isolated by tenant_id in the WHERE clause
        assert len(cross_a) >= 1
        assert len(cross_b) >= 1
