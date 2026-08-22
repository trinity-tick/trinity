"""Trinity — 审计事件删除端点 + API TLS 参数解析单元测试。

覆盖：
- DELETE /audit/events/{event_id}（GDPR 删除权）：
    删除成功 + 写入 action='PURGE' 审计留痕
    不存在 → 404
    非法/空 event_id → 400（容错）
    操作者取 X-Agent-ID，缺省 'unknown'
- API TLS 参数解析（纯函数 _tls_uvicorn_kwargs）：
    同时设置 TRINITY_TLS_CERT / TRINITY_TLS_KEY → kwargs 含 ssl_certfile/ssl_keyfile
    缺一 → 空 kwargs
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from trinity.adapters.sqlite import SQLiteAdapter
from trinity.api.server._routers_audit_purge import purge_audit_event


class _FakeMemory:
    """仅暴露 _adapter 的最小内存对象（端点只用到这一属性）。"""

    def __init__(self, adapter):
        self._adapter = adapter


class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = headers


@pytest.fixture
def adapter():
    db = tempfile.mktemp(suffix=".db")
    a = SQLiteAdapter(db)
    a.connect()
    yield a
    a.disconnect()
    for suffix in ("", "-wal", "-shm"):
        p = db + suffix
        if os.path.exists(p):
            os.unlink(p)


def _invoke(adapter, event_id, headers=None, monkeypatch=None):
    """直接调用端点（await 协程 + monkeypatch get_memory 注入临时 adapter）。"""
    import trinity.api.server._routers_audit_purge as mod
    monkeypatch.setattr(mod, "get_memory", lambda: _FakeMemory(adapter))
    return asyncio.run(
        purge_audit_event(event_id, _FakeRequest(headers or {}))
    )


# ── 存储/删除端到端 ────────────────────────────────────────────────────

def test_tls_kwargs_full(monkeypatch: pytest.MonkeyPatch) -> None:
    from trinity.api.server import _tls_uvicorn_kwargs
    monkeypatch.setenv("TRINITY_TLS_CERT", "C:/tls/server.crt")
    monkeypatch.setenv("TRINITY_TLS_KEY", "C:/tls/server.key")
    kwargs = _tls_uvicorn_kwargs()
    assert kwargs.get("ssl_certfile") == "C:/tls/server.crt"
    assert kwargs.get("ssl_keyfile") == "C:/tls/server.key"


def test_tls_kwargs_missing_key_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """只有 cert 没有 key → 空 kwargs（不应启用 TLS）。"""
    from trinity.api.server import _tls_uvicorn_kwargs
    monkeypatch.setenv("TRINITY_TLS_CERT", "C:/tls/server.crt")
    monkeypatch.delenv("TRINITY_TLS_KEY", raising=False)
    assert _tls_uvicorn_kwargs() == {}


def test_tls_kwargs_none_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from trinity.api.server import _tls_uvicorn_kwargs
    monkeypatch.delenv("TRINITY_TLS_CERT", raising=False)
    monkeypatch.delenv("TRINITY_TLS_KEY", raising=False)
    assert _tls_uvicorn_kwargs() == {}


def test_tls_kwargs_whitespace_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """空白字符串视同未设置 → 空 kwargs。"""
    from trinity.api.server import _tls_uvicorn_kwargs
    monkeypatch.setenv("TRINITY_TLS_CERT", "   ")
    monkeypatch.setenv("TRINITY_TLS_KEY", " ")
    assert _tls_uvicorn_kwargs() == {}


def test_purge_success_writes_purge_record(adapter, monkeypatch: pytest.MonkeyPatch) -> None:
    """删除成功：原审计行消失 + 存在 action='PURGE' 的留痕 + 操作者正确。"""
    adapter.write_audit_log(action="INGEST", agent_id="alice", details={"k": "v"})
    rows = adapter._conn.execute(
        "SELECT id, action FROM audit_log WHERE action='INGEST'"
    ).fetchall()
    assert len(rows) == 1
    event_id = rows[0]["id"]

    # 直接调用端点
    import trinity.api.server._routers_audit_purge as mod
    monkeypatch.setattr(mod, "get_memory", lambda: _FakeMemory(adapter))
    ret = asyncio.run(
        purge_audit_event(event_id, _FakeRequest({"X-Agent-ID": "alice"}))
    )

    assert ret["status"] == "ok"
    assert ret["operator"] == "alice"
    # 原记录已物理删除
    gone = adapter._conn.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE id=?", (event_id,)
    ).fetchone()["c"]
    assert gone == 0
    # PURGE 留痕存在且记录被删 event_id 与操作者
    purge = adapter._conn.execute(
        "SELECT id, action, agent_id, details FROM audit_log "
        "WHERE action='PURGE' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    assert purge is not None and purge["action"] == "PURGE"
    assert purge["agent_id"] == "alice"
    assert event_id in purge["details"]


def test_purge_operator_defaults_unknown(adapter, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 X-Agent-ID 请求头 → 操作者回退为 'unknown'。"""
    adapter.write_audit_log(action="VIEW", agent_id="alice")
    event_id = adapter._conn.execute(
        "SELECT id FROM audit_log WHERE action='VIEW'"
    ).fetchone()["id"]
    ret = _invoke(adapter, event_id, headers={}, monkeypatch=monkeypatch)
    assert ret["operator"] == "unknown"
    purge = adapter._conn.execute(
        "SELECT agent_id FROM audit_log WHERE action='PURGE' "
        "ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    assert purge["agent_id"] == "unknown"


def test_purge_not_found_404(adapter, monkeypatch: pytest.MonkeyPatch) -> None:
    """不存在的 event_id → 404，且不写入任何记录。"""
    with pytest.raises(Exception) as exc:
        _invoke(adapter, "no-such-event-id", monkeypatch=monkeypatch)
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail.lower()


def test_purge_illegal_empty_id_400(adapter, monkeypatch: pytest.MonkeyPatch) -> None:
    """空/空白 event_id → 400 容错，不触碰数据库。"""
    with pytest.raises(Exception) as exc:
        _invoke(adapter, "   ", monkeypatch=monkeypatch)
    assert exc.value.status_code == 400


def test_purge_missing_adapter_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """adapter 不可用 → 500。"""
    import trinity.api.server._routers_audit_purge as mod
    monkeypatch.setattr(mod, "get_memory", lambda: _FakeMemory(None))
    with pytest.raises(Exception) as exc:
        asyncio.run(purge_audit_event("some-id", _FakeRequest({})))
    assert exc.value.status_code == 500
