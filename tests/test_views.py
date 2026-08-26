# -*- coding: utf-8 -*-
"""Views unit tests."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.views import apply_view, _parse_tags


class TestParseTags:
    def test_list(self):
        assert _parse_tags(["wms", "上架"]) == ["wms", "上架"]

    def test_json_str(self):
        assert _parse_tags('["wms", "上架"]') == ["wms", "上架"]

    def test_garbage(self):
        assert _parse_tags(123) == []
        assert _parse_tags("not json") == []


class TestApplyView:
    def _results(self):
        return [
            {"memory_id": "a", "category": "decision", "tags": ["wms"], "importance": 0.9, "created_at": "2026-08-01T00:00:00"},
            {"memory_id": "b", "category": "wms_knowledge", "tags": ["wms", "上架"], "importance": 0.5, "created_at": "2026-08-02T00:00:00"},
            {"memory_id": "c", "category": "general", "tags": [], "importance": 0.8, "created_at": "2026-08-03T00:00:00"},
        ]

    def test_category_filter(self):
        out = apply_view(self._results(), {"categories": ["decision"]})
        assert [r["memory_id"] for r in out] == ["a"]

    def test_tag_filter(self):
        out = apply_view(self._results(), {"tags": ["上架"]})
        assert [r["memory_id"] for r in out] == ["b"]

    def test_min_importance(self):
        out = apply_view(self._results(), {"min_importance": 0.7})
        assert {r["memory_id"] for r in out} == {"a", "c"}

    def test_sort_importance(self):
        out = apply_view(self._results(), {"sort": "importance"})
        assert [r["memory_id"] for r in out] == ["a", "c", "b"]

    def test_top_k(self):
        out = apply_view(self._results(), {"top_k": 2})
        assert len(out) == 2

    def test_combo(self):
        out = apply_view(self._results(), {"categories": ["decision", "wms_knowledge"],
                                           "tags": ["wms"], "sort": "importance"})
        assert [r["memory_id"] for r in out] == ["a", "b"]
