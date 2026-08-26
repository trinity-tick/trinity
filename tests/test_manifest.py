# -*- coding: utf-8 -*-
"""Manifest unit tests (Claude Science 借鉴 Phase 1)."""
import os
import sys
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.benchmark.manifest import (
    build_manifest, validate_manifest, compute_code_hash, list_eval_sets,
)


class TestManifest:
    def test_build_and_validate(self):
        with tempfile.TemporaryDirectory() as td:
            result = os.path.join(td, "exp.json")
            with open(result, "w", encoding="utf-8") as f:
                f.write('{"R@5": 0.9}')
            ds = os.path.join(td, "data.json")
            with open(ds, "w", encoding="utf-8") as f:
                f.write('{"q": 1}')
            build_manifest(result, params={"top_k": 10}, dataset_paths=[ds])
            ok, report = validate_manifest(result)
            assert ok is True
            assert report["code_changed"] is False
            assert report["dataset_changed"] is False
            assert report["params"] == {"top_k": 10}

    def test_dataset_change_detected(self):
        with tempfile.TemporaryDirectory() as td:
            result = os.path.join(td, "exp.json")
            with open(result, "w", encoding="utf-8") as f:
                f.write("{}")
            ds = os.path.join(td, "data.json")
            with open(ds, "w", encoding="utf-8") as f:
                f.write("v1")
            build_manifest(result, dataset_paths=[ds])
            with open(ds, "w", encoding="utf-8") as f:
                f.write("v2-changed")
            ok, report = validate_manifest(result)
            assert ok is False
            assert report["dataset_changed"] is True

    def test_missing_manifest(self):
        ok, report = validate_manifest("no-such-file.json")
        assert ok is False
        assert report["exists"] is False

    def test_code_hash_stable(self):
        h1 = compute_code_hash()
        h2 = compute_code_hash()
        assert h1 == h2
        assert len(h1) == 16


class TestEvalSets:
    def test_list(self):
        sets = list_eval_sets()
        assert "mock500q" in sets
        assert "holdout" in sets
        assert sets["mock500q"]["dataset_ready"] is True
