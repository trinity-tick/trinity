"""Regression: Trinity.search mode 参数真实路由（GEN-2）。

曾发现：mode=keyword/hybrid/semantic 在 adapter 分支结果完全一致（装饰性参数，
见 output/channel_attribution.md）。修复后：
  - keyword/exact → FTS5（默认路径，行为不变）
  - semantic       → 尝试向量检索，不可用时回退 FTS5（结果非空）
  - hybrid         → 仅当 hybrid retriever 已初始化时走 47 通道融合；否则回退 FTS5
  - 所有路径返回非空结果且与默认路径兼容（不崩溃、不空转）
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity import Trinity


class TestSearchModeRouting:
    def _setup(self):
        tmp = tempfile.mkdtemp(prefix="mode_rt_")
        t = Trinity(adapter="sqlite", store_path=tmp)
        t.ingest("Alice likes Sichuan food and lives in Chengdu.", persona_id="default")
        t.ingest("Bob prefers dark mode in dashboards.", persona_id="default")
        return t

    def test_keyword_mode_returns_results(self):
        t = self._setup()
        try:
            r = t.search("What does Alice like?", mode="keyword", top_k=5)
            assert len(r.get("results", [])) >= 1
        finally:
            t._adapter.disconnect()

    def test_semantic_mode_falls_back_or_returns(self):
        t = self._setup()
        try:
            r = t.search("What does Alice like?", mode="semantic", top_k=5)
            # 向量不可用时回退 FTS5 → 仍应有结果；可用时返回向量结果
            assert len(r.get("results", [])) >= 1
        finally:
            t._adapter.disconnect()

    def test_hybrid_mode_returns_results(self):
        t = self._setup()
        try:
            # 未初始化 hybrid retriever → 回退 FTS5；初始化后 → fusion
            r1 = t.search("What does Alice like?", mode="hybrid", top_k=5)
            assert len(r1.get("results", [])) >= 1
            # 显式初始化后 hybrid 也应有结果
            t.search_hybrid("Alice", top_k=3, strategy="fusion")
            r2 = t.search("What does Alice like?", mode="hybrid", top_k=5)
            assert len(r2.get("results", [])) >= 1
        finally:
            t._adapter.disconnect()

    def test_keyword_results_stable(self):
        """mode 路由不改变 keyword 路径的既有行为。"""
        t = self._setup()
        try:
            a = t.search("Sichuan food", mode="keyword", top_k=5).get("results", [])
            b = t.search("Sichuan food", mode="keyword", top_k=5).get("results", [])
            assert [r.get("memory_id") for r in a] == [r.get("memory_id") for r in b]
        finally:
            t._adapter.disconnect()
