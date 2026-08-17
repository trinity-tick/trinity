"""Tests for real-LLM memory compression (trinity.daemon.memory_compressor)."""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.daemon.memory_compressor import (
    MemoryCompressor,
    CompressionStatus,
    create_llm_compress_callable,
)


# ── Stub LLM HTTP server (OpenAI-compatible) ─────────────────────────────

class _StubHandler(BaseHTTPRequestHandler):
    """Serves POST /v1/chat/completions with a canned summary."""

    seen = []  # class-level: record request bodies

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        _StubHandler.seen.append(body)
        summary = ("StubSummary: user said 'hello world' and decided to "
                   "migrate on 2026-08-14.")
        payload = {
            "choices": [{"message": {"role": "assistant", "content": summary}}],
            "model": body.get("model", "stub"),
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class _StubServer:
    """Context manager wrapping the stub HTTP server on an ephemeral port."""

    def __init__(self):
        self.server = HTTPServer(("127.0.0.1", 0), _StubHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        _StubHandler.seen = []
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


# ── Fake PG adapter ──────────────────────────────────────────────────────

class FakePgAdapter:
    """Minimal adapter recording store/archive calls."""

    def __init__(self):
        self.stored = []
        self.archived = []

    def store_memory(self, content, persona_id="system", importance=0.3,
                     tags=None, category="general", role="system", **kw):
        mem_id = f"fake_{len(self.stored)}"
        self.stored.append({
            "memory_id": mem_id, "content": content, "persona_id": persona_id,
            "importance": importance, "tags": tags or [], "category": category,
        })
        return {"memory_id": mem_id}

    def update_memory(self, memory_id, tags=None, **kw):
        self.archived.append(memory_id)
        return True

    def _get_conn(self):
        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return self

            def execute(self, *a, **kw):
                return None

            def commit(self):
                return None

        return _Conn()


# ── Tests ────────────────────────────────────────────────────────────────

class TestCreateLlmCallable:
    def test_missing_api_key_raises(self):
        os.environ.pop("TRINITY_LLM_API_KEY", None)
        try:
            create_llm_compress_callable(api_key=None)
            assert False, "expected ValueError"
        except ValueError as e:
            assert "TRINITY_LLM_API_KEY" in str(e)

    def test_callable_hits_stub(self):
        with _StubServer() as srv:
            call = create_llm_compress_callable(
                base_url=f"http://127.0.0.1:{srv.port}/v1",
                api_key="test-key-123",
                model="stub-model",
            )
            out = call("system prompt", "user prompt")
        assert "StubSummary" in out
        assert _StubHandler.seen, "stub received no request"
        assert _StubHandler.seen[0]["model"] == "stub-model"
        assert _StubHandler.seen[0]["messages"][0]["role"] == "system"

    def test_env_fallbacks(self):
        with _StubServer() as srv:
            os.environ["TRINITY_LLM_BASE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
            os.environ["TRINITY_LLM_API_KEY"] = "env-key"
            os.environ["TRINITY_LLM_MODEL"] = "env-model"
            try:
                call = create_llm_compress_callable()
                out = call("s", "u")
            finally:
                os.environ.pop("TRINITY_LLM_BASE_URL", None)
                os.environ.pop("TRINITY_LLM_API_KEY", None)
                os.environ.pop("TRINITY_LLM_MODEL", None)
        assert "StubSummary" in out
        assert _StubHandler.seen[0]["model"] == "env-model"
        assert _StubHandler.seen[0]["messages"][1]["content"] == "u"


class TestRealLlmCompression:
    def _memories(self):
        return [
            {"memory_id": "m1", "content": "Alice likes Sichuan food.", "importance": 0.8},
            {"memory_id": "m2", "content": "Project deadline moved to 2026-08-14.", "importance": 0.6},
            {"memory_id": "m3", "content": "Server deployed on AWS us-east-1.", "importance": 0.7},
        ]

    def test_compress_batch_success_with_real_llm(self):
        with _StubServer() as srv:
            call = create_llm_compress_callable(
                base_url=f"http://127.0.0.1:{srv.port}/v1",
                api_key="k",
            )
            adapter = FakePgAdapter()
            compressor = MemoryCompressor(pg_adapter=adapter, llm_callable=call)
            result = compressor.compress_batch(self._memories(), memory_type="general")

        assert result.status == CompressionStatus.SUCCESS, result.error_message
        assert result.compressed is not None
        assert "StubSummary" in result.compressed.content
        assert set(result.archived_ids) == {"m1", "m2", "m3"}
        assert len(adapter.stored) == 1
        assert adapter.stored[0]["category"] == "compressed_general"
        assert "compressed" in adapter.stored[0]["tags"]
        assert len(adapter.archived) == 3

    def test_batch_too_small_skipped(self):
        with _StubServer() as srv:
            call = create_llm_compress_callable(
                base_url=f"http://127.0.0.1:{srv.port}/v1", api_key="k",
            )
            compressor = MemoryCompressor(pg_adapter=FakePgAdapter(), llm_callable=call)
            result = compressor.compress_batch([self._memories()[0]])
        assert result.status == CompressionStatus.SKIPPED

    def test_no_llm_callable_fails_gracefully(self):
        compressor = MemoryCompressor(pg_adapter=FakePgAdapter(), llm_callable=None)
        result = compressor.compress_batch(self._memories())
        assert result.status == CompressionStatus.SKIPPED
        assert "No LLM callable" in result.error_message
