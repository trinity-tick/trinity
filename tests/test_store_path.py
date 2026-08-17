"""Regression: Trinity(store_path=<db file>) must initialize its adapter.

曾发现：store_path 传 .db 文件路径时被当作目录处理（拼接 trinity_store.db），
adapter 初始化静默失败（_adapter=None），导致 Trinity.search() 恒返回 0 结果
—— 曾被误报为 "FTS5 多词 + 过滤 bug"（见 DSH 基准核查报告）。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity import Trinity


class TestStorePathFile:
    def _clear_env(self, monkeypatch) -> None:
        # 回归修复(2026-08-14): 隔离 TRINITY_DB_PATH，避免被其它测试污染（全量套件偶发失败根因）
        monkeypatch.delenv("TRINITY_DB_PATH", raising=False)
        monkeypatch.delenv("TRINITY_STORE", raising=False)

    def test_file_path_initializes_adapter(self, monkeypatch):
        self._clear_env(monkeypatch)
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "store_path_test.db")
        t = Trinity(store_path=db)
        try:
            assert t._adapter is not None, "adapter must initialize when store_path is a file"
            # 写一条再搜，验证链路真实可用
            t.ingest("Alice likes Sichuan food.", persona_id="default")
            s = t.search("What does Alice like?", top_k=5, mode="keyword")
            results = s.get("results", []) if isinstance(s, dict) else []
            assert len(results) >= 1, "search should return results after ingest"
        finally:
            t._adapter.disconnect() if t._adapter else None

    def test_directory_path_still_works(self, monkeypatch):
        self._clear_env(monkeypatch)
        tmp = tempfile.mkdtemp()
        t = Trinity(store_path=tmp)
        try:
            assert t._adapter is not None
            assert t._adapter.db_path == os.path.join(tmp, "trinity_store.db")
        finally:
            t._adapter.disconnect() if t._adapter else None

    def test_no_store_path_defaults_to_cwd_db(self, monkeypatch):
        self._clear_env(monkeypatch)
        t = Trinity()
        try:
            # 默认路径：相对 trinity_store.db（或 TRINITY_DB_PATH）
            assert t._adapter is not None
        finally:
            t._adapter.disconnect() if t._adapter else None
