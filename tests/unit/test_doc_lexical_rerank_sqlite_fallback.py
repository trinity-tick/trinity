# -*- coding: utf-8 -*-
"""`doc_lexical_rerank` 的 SQLite 回退路径测试（2026-10-06）。

## 背景：这个测试防的是什么

本模块原先 **PG-only**。本部署按 D28 保持 SQLite 且 PG 口令不可用，于是它
**每次搜索都 `empty_index` 并静默 fail-open** —— 一个已接线、已插桩、已标定
（引擎候选+重排 R@1 0.850）的机制就这样死了好几周，而响应里看不出原因。

## 钉住的失败形态

1. **回退必须真的建出索引**（`backend="sqlite"` 且 `n > 0`），不是换个名字继续空转。
2. **两条后端必须共用同一个装配函数**（§1050）：章节标题 ×3、中文 bigram、k1=1.2
   —— 少任何一维都复现不出标定过的分数（本模块 2026-09-21 已因此错两次）。
3. **后端必须写进元数据**：`backend` 是这次问题的全部教训 —— 失效不可见才致命。
4. **两条都失败时必须如实说**（`backend="none"` + `backend_error`），不许静默空转。
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def _load():
    path = os.path.join(ROOT, "trinity", "retrieval", "doc_lexical_rerank.py")
    spec = importlib.util.spec_from_file_location("dlr_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dlr_test"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


# ---- 装配口径（两条后端共用）--------------------------------------------------

def test_assemble_applies_section_3x_weight():
    """章节标题必须 ×3 —— 这是标定过的三处口径之一。

    注意不能断言"绝对计数 == 3"：`_tokenize` 本身就会为 2 字词补 bigram
    （'库存' 的 bigram 还是 '库存'），所以同样的词元天然出现 2 次，×3 后是 6。
    **要断言的是那个 ×3 的倍数关系**，不是某个猜出来的绝对值。
    """
    section = "库存对账"
    base = mod._tokenize(section)
    idx = mod._assemble([("C:/d/A.md", section, "")])
    cnt = dict((d, c) for d, c, _ in idx["docs"])["A.md"]
    for tok in set(base):
        assert cnt.get(tok, 0) == 3 * base.count(tok), \
            "词元 %s 必须是 _tokenize 基数的 3 倍（实测 %d，基数 %d）" % (
                tok, cnt.get(tok, 0), base.count(tok))


def test_assemble_adds_chinese_bigram():
    """中文 bigram 补齐 —— 抗分词差异（第二处口径）。"""
    idx = mod._assemble([("C:/d/A.md", "", "库存对账")])
    docs = dict((d, c) for d, c, _ in idx["docs"])
    assert "库存" in docs["A.md"], "bigram 必须补齐"


def test_assemble_skips_rows_without_basename():
    idx = mod._assemble([("", "标题", "正文"), ("C:/d/B.md", "s", "b")])
    assert idx["n"] == 1


def test_assemble_empty_is_safe():
    idx = mod._assemble([])
    assert idx["n"] == 0 and idx["docs"] == [] and idx["avgdl"] == 1.0


def test_assemble_counts_missing_body():
    idx = mod._assemble([("C:/d/A.md", "s", ""), ("C:/d/B.md", "s", "x")])
    assert idx["no_tsv"] == 1, "空正文必须计数（§13.2 不合并）"


# ---- 后端选择 -----------------------------------------------------------------

def test_build_falls_back_to_sqlite_when_pg_fails(monkeypatch):
    """**核心回归**：PG 抛错时必须回退 SQLite，且标明 backend。"""
    def _boom(persona):
        raise RuntimeError("fe_sendauth: no password supplied")
    monkeypatch.setattr(mod, "_build_pg", _boom)

    def _fake_sqlite(persona):
        return {"docs": [("A.md", {"x": 1}, 1)], "idf": {"x": 1.0}, "avgdl": 1.0,
                "n": 1, "rows": 1, "no_tsv": 0, "built_at": 0.0, "backend": "sqlite"}
    monkeypatch.setattr(mod, "_build_sqlite", _fake_sqlite)
    idx = mod._build("trinity-docs")
    assert idx["backend"] == "sqlite" and idx["n"] == 1
    assert "fe_sendauth" in idx["pg_error"], "PG 失败原因必须留痕，不许静默"


def test_build_reports_none_when_both_fail(monkeypatch):
    """两条都失败 ⇒ 必须如实说，不许静默空转（这正是本次问题的教训）。"""
    monkeypatch.setattr(mod, "_build_pg", lambda p: (_ for _ in ()).throw(RuntimeError("pg down")))
    monkeypatch.setattr(mod, "_build_sqlite", lambda p: (_ for _ in ()).throw(RuntimeError("no db")))
    idx = mod._build("trinity-docs")
    assert idx["backend"] == "none" and idx["n"] == 0
    assert "pg down" in idx["backend_error"] and "no db" in idx["backend_error"]


def test_build_prefers_pg_when_available(monkeypatch):
    monkeypatch.setattr(mod, "_build_pg", lambda p: {"n": 5, "backend": "pg", "docs": [],
                                                     "idf": {}, "avgdl": 1.0,
                                                     "built_at": 0.0, "rows": 5})
    idx = mod._build("trinity-docs")
    assert idx["backend"] == "pg"


# ---- _sqlite_db 解析 -----------------------------------------------------------

def test_sqlite_db_resolves_in_this_environment():
    """本机应能解析出真实库（TRINITY_STORE 或 ~/.trinity 下的两个目录之一）。"""
    got = mod._sqlite_db()
    assert got, "解析不出 SQLite 库 —— 回退路径会退化成 backend=none"
    assert os.path.isfile(got)


def test_sqlite_db_accepts_directory_form(monkeypatch, tmp_path):
    """`TRINITY_STORE` 是**目录**，不是文件（本会话早先就在这上面栽过）。"""
    d = tmp_path / "store-restored"
    d.mkdir()
    (d / "trinity_store.db").write_bytes(b"")
    monkeypatch.setenv("TRINITY_STORE", str(d))
    assert mod._sqlite_db() == str(d / "trinity_store.db")


# ---- 元数据必须报后端 ---------------------------------------------------------

def test_rerank_meta_reports_backend(monkeypatch):
    """**这次问题的全部教训**：响应里必须看得出索引来自哪个存储。"""
    monkeypatch.setattr(mod, "_index", lambda p: {
        "docs": [("A.md", {"x": 1}, 1)], "idf": {"x": 1.0}, "avgdl": 1.0,
        "n": 1, "rows": 1, "built_at": 0.0, "backend": "sqlite", "no_tsv": 0})
    out = mod.rerank_hits("x", "trinity-docs", [{"metadata": {"source_file": "A.md"}}], 5)
    assert out["meta"]["backend"] == "sqlite"


def test_non_pg_backend_carries_calibration_warning(monkeypatch):
    """**非 PG 后端必须带标定警告** —— 两条后端语料不同（实测 255/4246 vs 275/3569），
    静默换后端就是拿同一套分数去比两把刻度不同的尺。"""
    monkeypatch.setattr(mod, "_index", lambda p: {
        "docs": [("A.md", {"x": 1}, 1)], "idf": {"x": 1.0}, "avgdl": 1.0,
        "n": 1, "rows": 1, "built_at": 0.0, "backend": "sqlite", "no_tsv": 0})
    out = mod.rerank_hits("x", "trinity-docs", [{"metadata": {"source_file": "A.md"}}], 5)
    assert "calibration_warning" in out["meta"], "非 PG 后端必须警告标定不可比"


def test_pg_backend_has_no_calibration_warning(monkeypatch):
    """PG 是标定后端 ⇒ 不该出现该警告（否则警告会被无视）。"""
    monkeypatch.setattr(mod, "_index", lambda p: {
        "docs": [("A.md", {"x": 1}, 1)], "idf": {"x": 1.0}, "avgdl": 1.0,
        "n": 1, "rows": 1, "built_at": 0.0, "backend": "pg", "no_tsv": 0})
    out = mod.rerank_hits("x", "trinity-docs", [{"metadata": {"source_file": "A.md"}}], 5)
    assert "calibration_warning" not in out["meta"]


def test_rerank_meta_reports_backend_error(monkeypatch):
    monkeypatch.setattr(mod, "_index", lambda p: {
        "docs": [], "idf": {}, "avgdl": 1.0, "n": 0, "rows": 0, "built_at": 0.0,
        "backend": "none", "backend_error": "pg=x | sqlite=y", "no_tsv": 0})
    out = mod.rerank_hits("x", "p", [], 5)
    assert out["meta"]["backend"] == "none"
    assert out["meta"]["backend_error"] == "pg=x | sqlite=y"
    assert out["meta"]["reason"] == "empty_index"


# ---- 真实库上的端到端（存在才跑）-----------------------------------------------

def test_build_sqlite_on_real_store_builds_docs():
    """真实库上 `trinity-docs` 必须建出 >0 个文档（否则"回退"等于没回退）。"""
    if not mod._sqlite_db():
        pytest.skip("no sqlite store")
    idx = mod._build_sqlite("trinity-docs")
    assert idx["backend"] == "sqlite"
    assert idx["n"] > 0, "回退路径必须真的建出索引"
    assert idx["rows"] > 0
    # 口径差异（**先存的，不是我引入的**）：评测脚本按 `rel` 去重得到 279 篇；
    # 本模块沿用原有 PG 代码的 `if t` 过滤，会丢掉 **4 篇一个词元都提不出来的**文档 ⇒ 275。
    # 这里只钉住量级，并把这个差记下来，免得以后误当回归。
    assert 270 <= idx["n"] <= 285, "文档数应在评测口径(279)附近，实测 %d" % idx["n"]
    assert idx["n"] < idx["rows"], "文档数应远小于 chunk 行数（按 source 聚合）"
