#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`registry_freshness_gate` 判据测试（2026-10-02，事故后建立）。

核心是**反事实**：同一个 evaluate()，
  · 完整外部包 ⇒ 无硬违规
  · 漏登 1 条 ⇒ 硬违规
  · 凭空条目 ⇒ 硬违规
  · must_include 与 gates 不一致 ⇒ 硬违规
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from scripts import registry_freshness_gate as g  # noqa: E402


def _tree(tmp_path, *, scores_ids, ext_rows, must=None, gates=None, ci_wired=()):
    d = tmp_path
    (d / "docs").mkdir(parents=True, exist_ok=True)
    (d / g.SCORES).write_text(
        json.dumps({"entries": [{"id": i} for i in scores_ids]}), encoding="utf-8")
    must = must if must is not None else ["x"]
    gates = gates if gates is not None else [{"id": "x"}]
    (d / g.GATE_SET).write_text(
        json.dumps({"must_include": must, "gates": gates}), encoding="utf-8")
    (d / g.GATE_WIRING).write_text(
        json.dumps({"ci_wired": list(ci_wired)}), encoding="utf-8")
    (d / g.EXT_VERIFY).write_text("".join("| `%s` | v |\n" % r for r in ext_rows),
                                  encoding="utf-8")
    return str(d)


def test_complete_package_passes(tmp_path):
    root = _tree(tmp_path, scores_ids=["a", "b"], ext_rows=["a", "b"])
    out = g.evaluate(g.collect(root), root)
    assert out["hard"] == [], out["hard"]


def test_missing_row_is_hard(tmp_path):
    root = _tree(tmp_path, scores_ids=["a", "b"], ext_rows=["a"])
    out = g.evaluate(g.collect(root), root)
    assert any("漏登" in h for h in out["hard"]), out["hard"]


def test_ghost_row_is_hard(tmp_path):
    root = _tree(tmp_path, scores_ids=["a"], ext_rows=["a", "zzz"])
    out = g.evaluate(g.collect(root), root)
    assert any("不存在" in h for h in out["hard"]), out["hard"]


def test_must_include_mismatch_is_hard(tmp_path):
    root = _tree(tmp_path, scores_ids=["a"], ext_rows=["a"],
                 must=["x", "y"], gates=[{"id": "x"}])
    out = g.evaluate(g.collect(root), root)
    assert any("must_include 有而 gates 缺" in h for h in out["hard"]), out["hard"]


def test_gates_extra_is_hard(tmp_path):
    root = _tree(tmp_path, scores_ids=["a"], ext_rows=["a"],
                 must=["x"], gates=[{"id": "x"}, {"id": "z"}])
    out = g.evaluate(g.collect(root), root)
    assert any("gates 有而 must_include 缺" in h for h in out["hard"]), out["hard"]


def test_ci_wired_ghost_script_is_hard(tmp_path):
    root = _tree(tmp_path, scores_ids=["a"], ext_rows=["a"],
                 ci_wired=["definitely_not_a_real_script_xyz.py"])
    out = g.evaluate(g.collect(root), root)
    assert any("僵尸登记" in h for h in out["hard"]), out["hard"]


def test_missing_ext_verify_is_fail_closed(tmp_path):
    root = _tree(tmp_path, scores_ids=["a"], ext_rows=["a"])
    os.remove(os.path.join(root, g.EXT_VERIFY))
    out = g.evaluate(g.collect(root), root)
    assert any("不存在" in h for h in out["hard"]), out["hard"]


def test_selftest_passes():
    assert g.selftest() == 0


# ── 真实仓库：本门必须真的能抓到"漏登"（本次实测 9 vs 25）──────────────
def test_real_repo_is_consistent_now():
    c = g.collect()
    out = g.evaluate(c)
    assert out["hard"] == [], out["hard"]
    assert out["counts"]["scores"] == out["counts"]["ext_verify"], out["counts"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
