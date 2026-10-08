# -*- coding: utf-8 -*-
"""T77/I17 判据：探针**不得**在"服务 adapter ≠ 探针 backend"时报 `STALE_VIEW`（误归因）。

背景（t75/I15 定性）：本机常驻 API 的 `/diagnostics` 报 `adapter=sqlite`，而本探针**默认读 PG**
⇒ 那时给出的 `STALE_VIEW`（"服务视图冻结在启动时刻"）其实是**跨库缺失**，会把人引向"重启服务"。

判据（每条都能失败，且带反事实/牙齿）：
| # | 判据 | 反向设计 |
|---|---|---|
| C1 | **adapter≠backend ⇒ `CROSS_STORE_MISSING`**，rc=**3**，且 `verdict` 字段**不是** `STALE_VIEW` | 误归因的正面判据 |
| C2 | **adapter==backend ⇒ 原语义逐字保持**（`STALE_VIEW` + rc=1） | 反事实：不得为新分支改坏旧分支 |
| C3 | `--json` 失败/成功两路 **各恰好一个合法 JSON**（沿用 t73 C1/C2 口径） | 契约不得被破坏 |
| C4 | ⭐ **牙齿**：把"比对"那步摘掉（让跨库恒为 False）⇒ **C1 必须红** | 证明 C1 真在测比对 |
| C5 | **判定不了（/diagnostics 取不到）⇒ 不静默**：保留原 verdict，但在 `why` 与 `adapter_check` 里写明"前提未验证" | 反"静默猜测" |

测试用**本地假服务**（回环、无生产库、无写入）：
`/diagnostics` 报可配置的 adapter；`/memory/search/hybrid` 返回空；`/memories/<id>` 返回 404。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = r"D:\trinity-code"
PROBE_PATH = os.path.join(ROOT, "scripts", "service_freshness_probe.py")
for p in (ROOT, os.path.dirname(PROBE_PATH)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_probe():
    spec = importlib.util.spec_from_file_location("svc_freshness_t77", PROBE_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Handler(BaseHTTPRequestHandler):
    """`type(self).adapter` 决定 /diagnostics 报什么；`None` ⇒ 该端点 404（模拟取不到）。"""

    adapter = "sqlite"

    def _json(self, code: int, payload: dict) -> None:
        b = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self) -> None:  # noqa: N802
        ad = type(self).adapter
        if self.path.startswith("/diagnostics"):
            if ad is None:
                self._json(404, {"error": "no_diagnostics"})
            else:
                self._json(200, {"adapter": {"adapter": ad, "db_path": "<fake>"}})
        elif self.path.startswith("/memories/"):
            self._json(404, {"error": "not_found"})
        else:
            self._json(404, {"error": "no_route"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
        except Exception:  # noqa: BLE001
            #: T94/C2：假服务**必须把请求体读掉**（否则客户端可能看到 broken pipe）；
            #: 读失败本身不影响判定（本假服务恒返回空结果）⇒ 不阻断，但**留痕**。
            logging.getLogger(__name__).debug("fake service: 读请求体失败（继续返回空结果）",
                                              exc_info=True)
        self._json(200, {"results": []})          # 空检索 ⇒ 全部"不可见"

    def log_message(self, *a) -> None:
        return


@pytest.fixture()
def fake_factory():
    servers = []

    def _start(adapter):
        cls = type("H_%s" % adapter, (_Handler,), {"adapter": adapter})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), cls)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return "http://127.0.0.1:%d" % srv.server_address[1]
    yield _start
    for s in servers:
        s.shutdown()


def _fake_rows(*_a, **_k):
    """让探针不必真连库：返回若干"启动后写入"的行（正文>60 字符）。"""
    return [("mem_fake_%02d" % i, "内容" * 40, "episodic", "2026-10-07T11:00:00+00:00")
            for i in range(3)]


def _run_probe(mod, monkeypatch, capsys, base, backend="postgresql", rows=True):
    monkeypatch.setattr(mod, "_fresh_rows", _fake_rows if rows else
                        (lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no db"))))
    monkeypatch.setattr(mod, "_probe_backend", lambda: (backend, "instance", None))
    monkeypatch.setattr(sys, "argv", ["service_freshness_probe.py", "--json", "--base", base,
                                     "--n", "3"])
    rc = mod.main()
    out = capsys.readouterr()
    return rc, out


# ── C1 adapter≠backend ⇒ 新 verdict ──────────────────────────────────────
def test_c1_cross_store_verdict(monkeypatch, capsys, fake_factory):
    mod = _load_probe()
    base = fake_factory("sqlite")
    rc, out = _run_probe(mod, monkeypatch, capsys, base, backend="postgresql")
    obj = json.loads(out.out)                      # --json：stdout 恰一个合法 JSON
    assert rc == 3, "跨库缺失必须单列 rc=3（不得用 rc 表达'服务有问题'）"
    assert obj["verdict"] == "CROSS_STORE_MISSING"
    assert obj["verdict"] != "STALE_VIEW", "跨库时**绝不再叫** STALE_VIEW"
    assert obj["cross_store"] is True
    assert obj["service_adapter"] == "sqlite" and obj["probe_backend"] == "postgresql"
    assert "重启服务与病因无关" in obj["why"]


# ── C2 adapter==backend ⇒ 原语义逐字保持（反事实）──────────────────────
def test_c2_same_store_semantics_unchanged(monkeypatch, capsys, fake_factory):
    mod = _load_probe()
    base = fake_factory("postgresql")
    rc, out = _run_probe(mod, monkeypatch, capsys, base, backend="postgresql")
    obj = json.loads(out.out)
    assert rc == 1, "同库时的 rc 语义必须保持 1"
    assert obj["verdict"] == "STALE_VIEW", "同库时必须仍是 STALE_VIEW（原语义不变）"
    assert obj["cross_store"] is False
    assert "verdict_before_adapter_check" not in obj, "同库分支不该有改写痕迹"


# ── C3 --json 两路各恰好一个合法 JSON ───────────────────────────────────
def test_c3_json_contract_both_paths(monkeypatch, capsys, fake_factory):
    mod = _load_probe()
    base = fake_factory("sqlite")
    _, out = _run_probe(mod, monkeypatch, capsys, base)               # 成功/判定路
    obj = json.loads(out.out)
    assert isinstance(obj, dict) and obj["verdict"] == "CROSS_STORE_MISSING"
    assert out.out.strip().startswith("{") and out.out.strip().endswith("}")
    # 失败路（t73 的 _fail 路径）：读库失败 ⇒ 仍恰一个合法 JSON
    rc2, out2 = _run_probe(mod, monkeypatch, capsys, base, rows=False)
    obj2 = json.loads(out2.out)
    assert rc2 == 2 and obj2["ok"] is False and obj2["reason"]
    assert "[UNTESTABLE]" in out2.err, "人类可读行仍走 stderr"


# ── C4 牙齿：摘掉比对照样红 ─────────────────────────────────────────────
def test_c4_teeth_without_comparison_c1_goes_red(monkeypatch, capsys, fake_factory):
    mod = _load_probe()
    base = fake_factory("sqlite")
    #: 模拟"把比对摘掉"：服务侧 adapter 恒等于探针 backend ⇒ cross_store 恒 False
    monkeypatch.setattr(mod, "_service_adapter", lambda *a, **k: ("postgresql", None))
    rc, out = _run_probe(mod, monkeypatch, capsys, base, backend="postgresql")
    obj = json.loads(out.out)
    assert obj["verdict"] == "STALE_VIEW" and rc == 1, \
        "摘掉比对后必须退回 STALE_VIEW —— 若这里没退回，说明 C1 的判据不是由比对决定的"


# ── C5 判定不了 ⇒ 不静默 ────────────────────────────────────────────────
def test_c5_inconclusive_not_silent(monkeypatch, capsys, fake_factory):
    mod = _load_probe()
    base = fake_factory(None)                      # /diagnostics 404 ⇒ 取不到
    rc, out = _run_probe(mod, monkeypatch, capsys, base, backend="postgresql")
    obj = json.loads(out.out)
    assert rc == 1 and obj["verdict"] == "STALE_VIEW"        # 不新造 verdict
    assert obj["adapter_check"]["conclusive"] is False
    assert "归因前提**未验证**" in obj["why"], "判定不了必须写出来（不得静默）"
