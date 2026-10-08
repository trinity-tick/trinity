# -*- coding: utf-8 -*-
"""T73/I13 判据：`scripts/service_freshness_probe.py` 的两个真缺陷（去掉跳过之后露出的）。

| # | 判据 | 反向/牙齿 |
|---|---|---|
| C1 | **失败路径也输出合法 JSON**（读库失败）⇒ `json.loads(stdout)` 成立且 `ok is False` / `rc=2` / `reason` 非空 | 这是缺陷 1 的正面判据 |
| C2 | **成功路径 JSON 结构不变**（反事实：与修前 PG 实测的键集逐一对齐，且**没有**多出 `ok` 之类的改造痕迹） | 防"为修失败路径而改坏成功路径" |
| C3 | **三种失败路径都走同一 JSON 契约**（proc_start 异常 / 无服务进程 / 读库失败） | 覆盖 main 的三处 early-return |
| C4 | **不依赖私有 `_get_conn`**：源码里不再出现该属性名，且用只有 `_conn` 的**SQLite 形状**适配器也能取到行 | 缺陷 2 的正面判据 |
| C5 | 未知后端 ⇒ **响亮失败且带 JSON**（不静默、不环境性跳过） | 反"环境性跳过" |
| C6 | ⭐ **牙齿**：把失败路径改回"只打印 `[UNTESTABLE]`、不输出 JSON" ⇒ **C1 必须红** | 证明 C1 真的在测契约 |
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import sqlite3
import sys

import pytest

ROOT = r"D:\trinity-code"
PROBE_PATH = os.path.join(ROOT, "scripts", "service_freshness_probe.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "scripts"))


def _load():
    spec = importlib.util.spec_from_file_location("service_freshness_probe_t73", PROBE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def probe():
    return _load()


class _Args:
    def __init__(self, **kw):
        self.port = 8001
        self.n = 3
        self.top_k = 10
        self.base = "http://127.0.0.1:1"      # 故意不可达：失败路径不需要网络
        self.json = True
        for k, v in kw.items():
            setattr(self, k, v)


def _run_main(mod, argv, monkeypatch, capsys, rows=None, start=1_700_000_000.0):
    """在进程内跑 main()：fake `pending_activation` + 可控的取行函数。"""
    fake = type(sys)("pending_activation")
    fake._proc_start_epoch = lambda port: start
    monkeypatch.setitem(sys.modules, "pending_activation", fake)

    def _rows(after_epoch, n, adapter=None):
        if isinstance(rows, Exception):
            raise rows
        if rows is None:
            raise RuntimeError("no rows configured")
        return rows
    monkeypatch.setattr(mod, "_fresh_rows", _rows)
    monkeypatch.setattr(sys, "argv", ["service_freshness_probe.py"] + list(argv))
    rc = mod.main()
    out = capsys.readouterr()
    return rc, out


# ── C1 失败路径也输出合法 JSON ────────────────────────────────────────────
def test_c1_failure_path_still_emits_json(probe, monkeypatch, capsys):
    rc, out = _run_main(probe, ["--json"], monkeypatch, capsys,
                        rows=RuntimeError("'SQLiteAdapter' object has no attribute '_get_conn'"))
    assert rc == 2, "失败仍须 fail-closed（rc=2）"
    obj = json.loads(out.out)                       # ① 必须能解析
    assert obj["ok"] is False and obj["verdict"] == "UNTESTABLE"
    assert obj["rc"] == 2 and obj["reason"]
    assert "读库失败" in obj["reason"] and obj["stage"] == "read_rows"
    assert obj["error_type"] == "RuntimeError"
    assert "[UNTESTABLE]" in out.err, "人类可读行必须保留（走 stderr，不污染 stdout 的 JSON）"


# ── C2 成功路径 JSON 结构不变（键集与修前实测对齐）────────────────────────
BASELINE_KEYS = {"verdict", "n", "visible", "skipped", "detail", "why",
                 "service", "started_at", "start_epoch"}
#: **t77/I17 有意新增的 5 个诊断键**（归因前提检查：探针读的库 vs 服务服务的库）。
#: 属**加法**、已在 T77 报告披露：判据由"键集相等"改成"基线键**全在** + 只允许这 5 个新增"——
#: 仍钉住"成功路径不得被失败路径的字段改造（无 ok/reason）"，且没有基线键被删/改名。
T77_ADDED_KEYS = {"service_adapter", "probe_backend", "probe_backend_source",
                  "cross_store", "adapter_check"}


def test_c2_success_path_keys_unchanged(probe, monkeypatch, capsys):
    rows = [("mem_a", "内容" * 40, "episodic", "2026-10-07T11:00:00+00:00"),
            ("mem_b", "内容" * 40, "procedural", "2026-10-07T11:01:00+00:00")]

    class _FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", lambda req, timeout=None: _FakeResp(
        json.dumps({"results": [{"memory_id": "mem_a"}]}).encode()))
    rc, out = _run_main(probe, ["--json"], monkeypatch, capsys, rows=rows)
    obj = json.loads(out.out)
    assert BASELINE_KEYS <= set(obj), "基线键被删/改名：%s" % (BASELINE_KEYS - set(obj))
    assert set(obj) - BASELINE_KEYS <= T77_ADDED_KEYS, \
        "出现了未披露的新键：%s" % (set(obj) - BASELINE_KEYS - T77_ADDED_KEYS)
    assert obj["n"] == 2 and obj["verdict"] in ("FRESH_OK", "NOT_RANKED", "STALE_VIEW",
                                                "CROSS_STORE_MISSING")
    assert "ok" not in obj and "reason" not in obj, "成功路径不得被失败路径的字段改造"
    assert rc in (0, 1, 3)


# ── C3 三条失败路径都带 JSON ─────────────────────────────────────────────
def test_c3_all_early_returns_emit_json(probe, monkeypatch, capsys):
    # (a) 取启动时刻异常
    fake = type(sys)("pending_activation")

    def _boom(port):
        raise RuntimeError("proc scan failed")
    fake._proc_start_epoch = _boom
    monkeypatch.setitem(sys.modules, "pending_activation", fake)
    monkeypatch.setattr(sys, "argv", ["p", "--json"])
    rc = probe.main()
    o = capsys.readouterr()
    assert rc == 2 and json.loads(o.out)["stage"] == "proc_start"
    # (b) 没有服务进程
    fake._proc_start_epoch = lambda port: 0
    monkeypatch.setattr(sys, "argv", ["p", "--json"])
    rc = probe.main()
    o = capsys.readouterr()
    assert rc == 2 and "没有可识别的服务进程" in json.loads(o.out)["reason"]
    # (c) 读库失败（同 C1，但这里核对字段齐全）
    rc, out = _run_main(probe, ["--json"], monkeypatch, capsys, rows=ValueError("boom"))
    assert json.loads(out.out)["error_type"] == "ValueError"


# ── C4 不用私有 `_get_conn`：SQLite 形状适配器也能取行 ────────────────────
def test_c4_no_private_get_conn_and_sqlite_shaped_adapter_works(probe, tmp_path):
    # 2026-10-07（t92）：改 `utf-8-sig`（剥 BOM）—— 满足本仓"同族写法棘轮"
    # （structure_gate.same_family_utf8_reads：读源码再 ast.parse 必须剥 BOM）。
    src = io.open(PROBE_PATH, encoding="utf-8-sig").read()
    tree = ast.parse(src)
    #: 源码里不得再有 `._get_conn(` **调用**（AST 口径：注释/文档字符串里记录旧缺陷不算 ——
    #: 这条也修正过我自己的一个误判：首版用裸字符串匹配 `"ad._get_conn()" not in src`，
    #: 结果被模块 docstring 里的**说明文字**误伤；AST 才是权威口径）。
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "_get_conn"]
    assert not calls, "仍在调用适配器私有 _get_conn"
    #: 也不得用字符串反射去摸它（`getattr(x, "_get_conn")`）
    reflected = [n for n in ast.walk(tree) if isinstance(n, ast.Constant)
                 and n.value == "_get_conn"]
    assert not reflected, "用字符串反射摸私有属性 `_get_conn`"

    class SQLiteShapedAdapter:                 # 只有 `_conn`，故意没有 `_get_conn`
        def __init__(self, conn):
            self._conn = conn

    db = tmp_path / "store.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        "CREATE TABLE memories (memory_id TEXT PRIMARY KEY, content TEXT, category TEXT,"
        " status TEXT, created_at TEXT);")
    long = "内容" * 40
    con.execute("INSERT INTO memories VALUES ('mem_x', ?, 'episodic', 'active',"
                " '2026-10-07T11:02:43.548716+00:00')", (long,))
    con.commit()
    rows = probe._fresh_rows(1_700_000_000.0, 3, adapter=SQLiteShapedAdapter(con))
    assert len(rows) == 1 and rows[0][0] == "mem_x"
    con.close()


# ── C5 未知后端 ⇒ 响亮失败 + JSON（不是环境性跳过）──────────────────────
def test_c5_unknown_backend_fails_loudly_with_json(probe, monkeypatch, capsys):
    class WeirdAdapter:
        pass
    with pytest.raises(RuntimeError) as ei:
        probe._fresh_rows(1_700_000_000.0, 3, adapter=WeirdAdapter())
    assert "未知存储后端" in str(ei.value)
    rc, out = _run_main(probe, ["--json"], monkeypatch, capsys, rows=RuntimeError("未知存储后端（WeirdAdapter）"))
    obj = json.loads(out.out)
    assert rc == 2 and obj["ok"] is False and "未知存储后端" in obj["reason"]
    assert "skip" not in json.dumps(obj).lower(), "不得以跳过/环境性跳过形式收场"


# ── C6 牙齿：改回旧行为 ⇒ C1 必红 ────────────────────────────────────────
def test_c6_teeth_old_behaviour_kills_c1(probe, monkeypatch, capsys):
    """把 `_fail` 换成"只打印 [UNTESTABLE]、不输出 JSON"（= 修前行为）⇒ C1 的断言必须失败。"""
    def _old_fail(args, why, rc=2, **extra):
        print("[UNTESTABLE] %s" % why)
        return rc
    monkeypatch.setattr(probe, "_fail", _old_fail)
    rc, out = _run_main(probe, ["--json"], monkeypatch, capsys, rows=RuntimeError("boom"))
    assert rc == 2
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.out)          # ← 修前行为下 stdout 不是 JSON ⇒ C1 会红
