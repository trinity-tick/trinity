# -*- coding: utf-8 -*-
"""Eval runner unit tests (DSH 借鉴 Phase 2)."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.eval.runner import check_assertion, run_task, run_all, DEFAULT_TASKS


class TestAssertions:
    def test_contains(self):
        ok, _ = check_assertion({"type": "contains", "value": "wms"}, "wms 上架规则")
        assert ok
        ok2, _ = check_assertion({"type": "contains", "value": "xyz"}, "abc")
        assert not ok2

    def test_not_contains(self):
        ok, _ = check_assertion({"type": "not_contains", "value": "lme"}, "wms 规则")
        assert ok

    def test_regex(self):
        ok, _ = check_assertion({"type": "regex", "value": r"^mem_[a-f0-9]+$"}, "mem_abc123")
        assert ok
        ok2, _ = check_assertion({"type": "regex", "value": r"^mem_"}, "other")
        assert not ok2

    def test_json_eq(self):
        ok, _ = check_assertion({"type": "json", "path": "stats.records",
                                 "op": "gt", "value": 0},
                                {"stats": {"records": 5}})
        assert ok
        ok2, _ = check_assertion({"type": "json", "path": "stats.records",
                                  "op": "gt", "value": 10},
                                 {"stats": {"records": 5}})
        assert not ok2

    def test_json_truthy_and_in_list(self):
        assert check_assertion({"type": "json", "path": "ok", "op": "truthy"},
                               {"ok": 1})[0]
        assert check_assertion({"type": "json", "path": "mode", "op": "in_list",
                                "value": ["llm", "fallback"]},
                               {"mode": "fallback"})[0]

    def test_unknown(self):
        ok, _ = check_assertion({"type": "nope", "value": 1}, "x")
        assert not ok


class TestTasks:
    def test_default_tasks_listed(self):
        names = [t["name"] for t in DEFAULT_TASKS]
        for expect in ("pagetree-built", "search-schema", "reason-available",
                       "automation-healthy", "views-loadable",
                       "visibility-parses", "goals-healthy",
                       "automation-rollout-healthy", "pagetree-summary-coverage",
                       "goals-no-stall"):
            assert expect in names

    def test_run_task_missing_run(self):
        r = run_task({"name": "x", "assertions": []})
        assert r["ok"] is False

    def test_run_task_failure_is_fail(self):
        def boom():
            raise RuntimeError("boom")
        r = run_task({"name": "y", "run": boom, "assertions": []})
        assert r["ok"] is False
        assert "boom" in r["detail"]

    def test_visibility_parses_task(self):
        task = next(t for t in DEFAULT_TASKS if t["name"] == "visibility-parses")
        r = run_task(task)
        assert r["ok"] is True

    def test_run_all_shape(self):
        s = run_all()
        assert s["total"] == len(DEFAULT_TASKS)
        assert s["passed"] + s["failed"] == s["total"]
