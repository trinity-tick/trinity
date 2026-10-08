# -*- coding: utf-8 -*-
"""留出问题题集构造（Step B 扩样本）判据测试。

钉住的失败形态：
1. **口径对齐**：只取指定 persona（doc 域评测的语料是 `trinity-docs`）——
   取错 persona 会让题集 target **不在评测语料内**，两把尺子混用 ⇒ 结论无效。
2. **§13.2**：解析不出 target 的行**按原因分档计数**，不许合并成 `skipped`。
3. **可失败**：题集为空、或出现非 `.md` target ⇒ FAILED。
4. `basename_of` 必须同时吃 Windows 反斜杠与正斜杠路径。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    path = os.path.join(ROOT, "scripts", "build_heldout_golden.py")
    spec = importlib.util.spec_from_file_location("build_heldout_golden", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_heldout_golden"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


@pytest.mark.parametrize("src,expect", [
    (r"C:\Users\a\.trinity\kb_harvest\X.md", "X.md"),
    ("C:/Users/a/docs/Y.md", "Y.md"),
    ("/tmp/Z.md", "Z.md"),
    (None, ""),
    ("", ""),
])
def test_basename_of(src, expect):
    assert mod.basename_of(src) == expect


def test_resolve_target_prefers_source_uri():
    t, why = mod.resolve_target("C:/x/A.md", None)
    assert t == "A.md" and why == "ok:source_uri"


def test_resolve_target_falls_back_to_metadata():
    t, why = mod.resolve_target(None, json.dumps({"source_file": "C:/x/B.md"}))
    assert t == "B.md" and why == "ok:metadata"


@pytest.mark.parametrize("meta,why", [
    (None, "no_metadata"),
    ("{broken", "metadata_unparsable"),
    ("[]", "metadata_not_dict"),
    (json.dumps({"other": 1}), "no_source_file_in_metadata"),
])
def test_resolve_target_reasons_are_distinct(meta, why):
    """§13.2：原因必须分档，不许合并成一个 skipped。"""
    t, got = mod.resolve_target(None, meta)
    assert t == "" and got == why


def _mk(tmp_path, rows):
    """rows = (memory_id, persona, source_uri, metadata)"""
    p = os.path.join(str(tmp_path), "s.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE memories (memory_id TEXT, persona_id TEXT, status TEXT,"
                " source_uri TEXT, metadata TEXT)")
    con.execute("CREATE TABLE memories_doc2query_holdout (memory_id TEXT, question TEXT)")
    for mid, persona, uri, meta in rows:
        con.execute("INSERT INTO memories VALUES (?,?,'active',?,?)", (mid, persona, uri, meta))
        con.execute("INSERT INTO memories_doc2query_holdout VALUES (?,?)",
                    (mid, "问题-%s" % mid))
    con.commit()
    con.close()
    return p


def test_only_requested_persona_is_taken(tmp_path):
    """口径：取错 persona ⇒ target 不在评测语料内 ⇒ 结论无效。"""
    p = _mk(tmp_path, [
        ("a", "trinity-docs", "C:/docs/A.md", None),
        ("b", "default", "C:/kb/B.md", None),
    ])
    r = mod.build(p, "trinity-docs", 50)
    assert r["verdict"] == "OK", r
    assert r["items"] == 1 and r["_items"][0]["target"] == "A.md"


def test_unresolved_reasons_are_counted(tmp_path):
    p = _mk(tmp_path, [
        ("a", "trinity-docs", "C:/docs/A.md", None),
        ("b", "trinity-docs", None, None),                       # no_metadata
        ("c", "trinity-docs", None, "{broken"),                  # metadata_unparsable
        ("d", "trinity-docs", "C:/docs/D.txt", None),            # not_markdown
    ])
    r = mod.build(p, "trinity-docs", 50)
    assert r["items"] == 1
    u = r["unresolved_by_reason"]
    assert u.get("no_metadata") == 1 and u.get("metadata_unparsable") == 1
    assert u.get("not_markdown") == 1, "非 .md 必须单独成档，不得与上面合并"


def test_duplicate_queries_are_deduped_and_counted(tmp_path):
    p = os.path.join(str(tmp_path), "d.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE memories (memory_id TEXT, persona_id TEXT, status TEXT,"
                " source_uri TEXT, metadata TEXT)")
    con.execute("CREATE TABLE memories_doc2query_holdout (memory_id TEXT, question TEXT)")
    for mid in ("a", "b"):
        con.execute("INSERT INTO memories VALUES (?,'trinity-docs','active','C:/docs/A.md',NULL)",
                    (mid,))
        con.execute("INSERT INTO memories_doc2query_holdout VALUES (?,'同一个问题')", (mid,))
    con.commit()
    con.close()
    r = mod.build(p, "trinity-docs", 50)
    assert r["items"] == 1 and r["unresolved_by_reason"].get("duplicate_query") == 1


def test_empty_set_is_failed_not_ok(tmp_path):
    p = _mk(tmp_path, [("b", "default", "C:/kb/B.md", None)])
    r = mod.build(p, "trinity-docs", 50)
    assert r["verdict"] == "FAILED", "题集为空不得判 OK"


def test_missing_store_inconclusive(tmp_path):
    r = mod.build(os.path.join(str(tmp_path), "no.db"), "trinity-docs", 10)
    assert r["verdict"] == "INCONCLUSIVE" and "not found" in r["error"]
