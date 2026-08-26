"""P0-3: MCP streamable-http default Bearer auth (COMPARISON_VS_2026_SOTA_R7).

Verifies:
  - _resolve_mcp_api_key priority (TRINITY_MCP_API_KEY > TRINITY_API_KEY)
  - ApiKeyTokenVerifier accepts the matching token, rejects others
  - create_server(auth_enabled=True) wires token_verifier + auth settings
  - auth degrades to disabled when no key is configured
"""

import os

import pytest

from trinity.mcp.server import (
    ApiKeyTokenVerifier,
    _resolve_mcp_api_key,
    create_server,
)


def test_resolve_key_priority(monkeypatch):
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "mcp-key")
    monkeypatch.setenv("TRINITY_API_KEY", "api-key")
    monkeypatch.setenv("GATEWAY_API_KEY", "gw-key")
    assert _resolve_mcp_api_key() == "mcp-key"


def test_resolve_key_fallback(monkeypatch):
    monkeypatch.delenv("TRINITY_MCP_API_KEY", raising=False)
    monkeypatch.setenv("TRINITY_API_KEY", "api-key")
    assert _resolve_mcp_api_key() == "api-key"


def test_resolve_key_gateway_fallback(monkeypatch):
    monkeypatch.delenv("TRINITY_MCP_API_KEY", raising=False)
    monkeypatch.delenv("TRINITY_API_KEY", raising=False)
    monkeypatch.setenv("GATEWAY_API_KEY", "gw-key")
    assert _resolve_mcp_api_key() == "gw-key"


def test_resolve_key_none(monkeypatch):
    monkeypatch.delenv("TRINITY_MCP_API_KEY", raising=False)
    monkeypatch.delenv("TRINITY_API_KEY", raising=False)
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    assert _resolve_mcp_api_key() is None


@pytest.mark.asyncio
async def test_verifier_accepts_matching_token():
    v = ApiKeyTokenVerifier("secret-token")
    tok = await v.verify_token("secret-token")
    assert tok is not None
    assert tok.client_id == "trinity-mcp"
    assert "memory.read" in tok.scopes


@pytest.mark.asyncio
async def test_verifier_rejects_wrong_token():
    v = ApiKeyTokenVerifier("secret-token")
    assert await v.verify_token("wrong-token") is None
    assert await v.verify_token("") is None


@pytest.mark.asyncio
async def test_verifier_rejects_empty_key():
    v = ApiKeyTokenVerifier("")
    assert await v.verify_token("") is None


def test_create_server_auth_enabled(monkeypatch):
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "mcp-key")
    mcp = create_server(auth_enabled=True)
    assert mcp._token_verifier is not None
    assert mcp.settings.auth is not None
    assert mcp.settings.auth.required_scopes == ["memory.read", "memory.write"]


def test_create_server_auth_degrades_without_key(monkeypatch, caplog):
    import logging
    monkeypatch.delenv("TRINITY_MCP_API_KEY", raising=False)
    monkeypatch.delenv("TRINITY_API_KEY", raising=False)
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="trinity.mcp"):
        mcp = create_server(auth_enabled=True)
    assert mcp._token_verifier is None
    assert mcp.settings.auth is None
    assert any("WITHOUT auth" in r.message for r in caplog.records)


def test_create_server_no_auth_by_default(monkeypatch):
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "mcp-key")
    mcp = create_server(auth_enabled=False)
    assert mcp._token_verifier is None
    assert mcp.settings.auth is None


def test_tools_still_registered_when_auth_on(monkeypatch):
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "mcp-key")
    mcp = create_server(auth_enabled=True)
    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "memory_search" in tool_names
    assert "memory_write" in tool_names
