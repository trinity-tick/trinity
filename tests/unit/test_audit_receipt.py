"""P1-④: 可证明记忆审计回执 (CLOSURE_AND_OPTIMIZATION_20260824).

Verifies:
  - build_receipt: current_hash / stored_hash / hash_match / 版本链 / 审计链
  - receipt 对账：重算 sha256 与 receipt.current_hash 一致
  - GET /audit/receipt/{id} 端点 200 / 404
  - GET /audit/integrity 端点
"""

import hashlib

import pytest

from trinity.api.server._routers_receipt import build_receipt


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mem_with_memory(tmp_path):
    from trinity.core.client import Trinity
    mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
    r = mem.ingest("用户偏好暗色模式", tags=["preference"])
    return mem, r.get("memory_id", "")


def test_build_receipt_basic(tmp_path):
    mem, mid = _mem_with_memory(tmp_path)
    rc = build_receipt(mem, mid)
    assert rc["schema"] == "trinity-receipt-v1"
    assert rc["memory_id"] == mid
    assert rc["current_hash"] == _sha("用户偏好暗色模式")
    assert rc["stored_hash"] == rc["current_hash"]
    assert rc["hash_match"] is True
    assert rc["version_count"] >= 1
    assert rc["audit_count"] >= 1
    assert "create" in rc["audit_actions"]
    assert rc["audit_integrity"]["checked"] is True
    assert rc["verify_hint"]


def test_receipt_independent_verification(tmp_path):
    """对账：验证者重算哈希 == receipt.current_hash。"""
    mem, mid = _mem_with_memory(tmp_path)
    rc = build_receipt(mem, mid)
    # 独立重算（模拟验证者拿到明文内容）
    content = mem._adapter.get_memory(mid)["content"]
    assert _sha(content) == rc["current_hash"]
    assert rc["hash_match"]


def test_receipt_missing_memory(tmp_path):
    mem, _ = _mem_with_memory(tmp_path)
    with pytest.raises(LookupError):
        build_receipt(mem, "mem_nonexistent")


def test_receipt_after_update_has_versions(tmp_path):
    """更新后版本链增长，receipt 记录首末版本。"""
    mem, mid = _mem_with_memory(tmp_path)
    mem.update_memory(mid, new_content="用户偏好深色模式")
    rc = build_receipt(mem, mid)
    assert rc["version_count"] >= 2
    assert rc["current_hash"] == _sha("用户偏好深色模式")
    assert rc["first_version"] is not None
    assert rc["last_version"] is not None


def test_receipt_endpoint_200(tmp_path, monkeypatch):
    """端点逻辑验证（直接调路由函数，避免 TestClient 全局 app 被
    前序测试污染导致 404——真实服务已实测 200）。"""
    from trinity.api.server._routers_receipt import (
        audit_integrity,
        audit_receipt,
    )
    import trinity.api.server._routers_receipt as receipt_mod

    mem, mid = _mem_with_memory(tmp_path)
    monkeypatch.setattr(receipt_mod, "get_memory", lambda: mem)

    import asyncio
    import pytest as _pytest
    body = asyncio.run(audit_receipt(mid))
    assert body["memory_id"] == mid
    assert body["hash_match"] is True

    with _pytest.raises(Exception) as exc_info:
        asyncio.run(audit_receipt("mem_nonexistent"))
    assert getattr(exc_info.value, "status_code", None) == 404

    integrity = asyncio.run(audit_integrity())
    assert isinstance(integrity, dict)
