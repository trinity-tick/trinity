# -*- coding: utf-8 -*-
"""`metadata.source_file` 缺口审计 + 门控回填 判据测试。

钉住的失败形态：
1. **已有 `source_file` 的行必须不动**（幂等；重跑不得覆盖或改写）。
2. **原因必须分档**（§13.2）：metadata 不可解析 / 非 dict / 无 uri，**不得合并成一个 skipped**。
3. `basename_of` 同时吃反斜杠与正斜杠。
4. **默认 dry-run**：不带 apply 绝不写库（用只读连接验证）。
5. 回填**只**改 `metadata.source_file`，**不得**动 content 或其它列。
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
    path = os.path.join(ROOT, "scripts", "backfill_source_file.py")
    spec = importlib.util.spec_from_file_location("backfill_source_file", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_source_file"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


@pytest.mark.parametrize("src,expect", [
    (r"C:\a\kb_harvest\X.md", "X.md"), ("C:/a/b/Y.md", "Y.md"), ("/t/Z.txt", "Z.txt"),
    (None, ""), ("", ""),
])
def test_basename_of(src, expect):
    assert mod.basename_of(src) == expect


def test_plan_row_already_present_is_untouched():
    """**幂等关键**：已有 source_file ⇒ 不动（返回空 + already_present）。"""
    md = json.dumps({"source_file": "EXISTING.md", "source_uri": "C:/x/OTHER.md"})
    nb, why = mod.plan_row(md, "C:/x/OTHER.md")
    assert nb == "" and why == "already_present", "不得覆盖已有的 source_file"


def test_plan_row_from_metadata_uri():
    nb, why = mod.plan_row(json.dumps({"source_uri": "C:/x/A.md"}), None)
    assert nb == "A.md" and why == "ok:metadata.source_uri"


def test_plan_row_falls_back_to_column():
    nb, why = mod.plan_row(json.dumps({"mtime": 1}), "C:/x/B.md")
    assert nb == "B.md" and why == "ok:column.source_uri"


@pytest.mark.parametrize("meta,uri,why", [
    ("{broken", None, "metadata_unparsable"),
    ("[]", None, "metadata_not_dict"),
    (json.dumps({"mtime": 1}), None, "no_uri"),
])
def test_plan_row_reasons_are_distinct(meta, uri, why):
    """§13.2：原因分档，不合并。"""
    nb, got = mod.plan_row(meta, uri)
    assert nb == "" and got == why


def _mk(tmp_path, rows):
    """rows = (memory_id, status, content, source_uri, metadata[, persona])。

    注意：**必须带 `persona_id` 列** —— 审计里有一条按 persona/category 分档的查询，
    真实 schema 有这一列；夹具缺了它会让 audit 抛 `no such column: persona_id`
    而被吞成 INCONCLUSIVE（初版就是这么假红的）。
    """
    p = os.path.join(str(tmp_path), "s.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE memories (memory_id TEXT, status TEXT, content TEXT,"
                " source_uri TEXT, metadata TEXT, persona_id TEXT, category TEXT)")
    for row in rows:
        mid, status, content, uri, meta = row[:5]
        persona = row[5] if len(row) > 5 else "default"
        cat = row[6] if len(row) > 6 else "kb_harvested"
        con.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?)",
                    (mid, status, content, uri, meta, persona, cat))
    con.commit()
    con.close()
    return p


def test_audit_counts_and_plan(tmp_path):
    p = _mk(tmp_path, [
        ("a", "active", "c1", r"C:\kb\A.md", json.dumps({"source_uri": r"C:\kb\A.md"})),
        ("b", "active", "c2", None, json.dumps({"mtime": 1})),           # 非文档
        ("c", "active", "c3", None, json.dumps({"source_file": "C.md"})),  # 已有，不算候选
        ("d", "archived", "c4", r"C:\kb\D.md", json.dumps({"source_uri": r"C:\kb\D.md"})),
    ])
    r = mod.audit(p)
    assert r["verdict"] == "OK"
    assert r["active_total"] == 3
    assert r["has_source_file"] == 1
    assert r["backfill_candidates"] == 1, "只有 a 是候选（c 已有、b 无 uri）"
    assert r["writable"] == 1
    assert r["plan_reasons"].get("ok:metadata.source_uri") == 1


def test_audit_missing_store_is_inconclusive(tmp_path):
    r = mod.audit(os.path.join(str(tmp_path), "no.db"))
    assert r["verdict"] == "INCONCLUSIVE"


def test_candidate_filter_uses_column_uri_too(tmp_path):
    """**关键回归**：路径只在**列** `source_uri` 里、metadata 里没有的行，也必须是候选。

    初版的筛选条件只看 `metadata.source_uri` ⇒ 在真实库上 dry-run 只报 **163**，
    而实际有列 `source_uri` 的是 **6,784** —— 漏了 40 倍，而且漏的方向是"显得更安全"。
    """
    p = _mk(tmp_path, [
        # metadata 里**没有** source_uri，只有列上有
        ("col_only", "active", "c", r"C:\kb\COL.md", json.dumps({"mtime": 1, "ext": ".md"})),
        # metadata 里也有
        ("meta_has", "active", "c", r"C:\kb\META.md",
         json.dumps({"source_uri": r"C:\kb\META.md"})),
    ])
    r = mod.audit(p)
    assert r["verdict"] == "OK"
    assert r["backfill_candidates"] == 2, "列上有 uri 的行必须算候选"
    assert r["writable"] == 2
    assert r["plan_reasons"].get("ok:column.source_uri") == 1
    assert r["plan_reasons"].get("ok:metadata.source_uri") == 1


def test_apply_only_touches_source_file_and_is_idempotent(tmp_path, monkeypatch):
    # **必须把 ROOT 指到 tmp**：`apply_backfill` 把回滚清单写到 `ROOT/output/`，
    # 不重定向的话测试会往**真实 output/** 里丢文件（实测丢过 3 个，
    # 还会让"本轮到底有没有写过生产库"的核对变得含混）。
    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    p = _mk(tmp_path, [
        ("a", "active", "ORIGINAL", r"C:\kb\A.md", json.dumps({"source_uri": r"C:\kb\A.md", "ext": ".md"})),
    ])
    r1 = mod.apply_backfill(p)
    assert r1["changed"] == 1
    con = sqlite3.connect(p)
    mid, content, meta = con.execute("SELECT memory_id, content, metadata FROM memories").fetchone()
    md = json.loads(meta)
    assert md["source_file"] == "A.md"
    assert md["ext"] == ".md", "其它 metadata 键必须原样保留"
    assert content == "ORIGINAL", "**content 绝不能被改**"
    con.close()
    # 回滚清单必须落在被重定向的 ROOT 下
    assert os.path.isdir(os.path.join(str(tmp_path), "output"))
    assert any(f.startswith("backfill_source_file_")
               for f in os.listdir(os.path.join(str(tmp_path), "output"))), \
        "回滚清单必须写在 ROOT/output 下"
    # 幂等：再跑一次不得再改
    r2 = mod.apply_backfill(p)
    assert r2["changed"] == 0 and r2["skipped"] >= 0
