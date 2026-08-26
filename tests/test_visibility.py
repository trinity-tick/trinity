# -*- coding: utf-8 -*-
"""Visibility unit tests."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.security.visibility import (
    parse_visibility, to_sql, matches, VisibilityError,
)


class TestParse:
    def test_simple(self):
        conds = parse_visibility("importance >= 0.6")
        assert conds == [("importance", ">=", 0.6)]

    def test_and_chain(self):
        conds = parse_visibility("category != 'lme' AND importance >= 0.6 AND tags CONTAINS 'wms'")
        assert len(conds) == 3
        assert conds[0] == ("category", "!=", "lme")
        assert conds[2] == ("tags", "CONTAINS", "wms")

    def test_in(self):
        conds = parse_visibility("category IN ('decision','wms_knowledge')")
        assert conds[0][2] == ["decision", "wms_knowledge"]

    def test_empty(self):
        assert parse_visibility("") == []
        assert parse_visibility(None) == []

    def test_invalid_field(self):
        try:
            parse_visibility("evil_field = 1")
            assert False, "should raise"
        except VisibilityError:
            pass

    def test_trailing(self):
        try:
            parse_visibility("importance >= 0.6 junk")
            assert False
        except VisibilityError:
            pass


class TestSql:
    def test_basic(self):
        where, params = to_sql("importance >= 0.6 AND category != 'lme'")
        assert where == "importance >= ? AND category != ?"
        assert params == (0.6, "lme")

    def test_contains_params(self):
        where, params = to_sql("tags CONTAINS 'wms'")
        assert "LIKE" in where
        assert params == ('%"wms"%',)

    def test_in_params(self):
        where, params = to_sql("category IN ('a','b')")
        assert params == ("a", "b")

    def test_none(self):
        assert to_sql(None) == (None, ())


class TestMatches:
    def test_ok(self):
        assert matches({"importance": 0.8, "category": "decision", "tags": ["wms"]},
                       "importance >= 0.6 AND category IN ('decision','wms_knowledge')")
        assert not matches({"importance": 0.3, "category": "decision"},
                           "importance >= 0.6")

    def test_tags_str(self):
        assert matches({"tags": '["wms"]'}, "tags CONTAINS 'wms'")
        assert not matches({"tags": '["other"]'}, "tags CONTAINS 'wms'")
