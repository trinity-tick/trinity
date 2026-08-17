"""Pytest fixtures for Trinity v8.0 E2E test suite.

Provides:
  - trinity_server: start/stop Trinity FastAPI on port 8001
  - test_db: isolated SQLite test database path
  - marvis_adapter: pre-configured MarvisAdapter instance
"""

import os
import sys
import socket
import subprocess
import time
import threading
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# ── Constants ──────────────────────────────────────────────────────────
TRINITY_PORT = 18001  # use non-default port to avoid conflicts
TRINITY_HOST = "127.0.0.1"
TRINITY_URL = f"http://{TRINITY_HOST}:{TRINITY_PORT}"
TEST_DB_PATH = os.path.join(os.path.dirname(__file__), ".test_trinity.db")

# ── Testing isolation guard (2026-08-16) ─────────────────────────────
# 持久化模块(orderbook/reputation/trust_exchange/optimization_engine)在测试
# 环境必须禁用真实文件读写,否则 __init__ 会加载上次运行残留的真实状态,
# 破坏测试隔离(ValueError already listed / stats != 0 等)。
os.environ.setdefault("TRINITY_TESTING", "1")


# ── Helpers ────────────────────────────────────────────────────────────

def _port_free(host: str, port: int) -> bool:
    """Check if a TCP port is available."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect((host, port))
            return False
    except (ConnectionRefusedError, socket.timeout, OSError):
        return True


def _wait_for_server(url: str, timeout: int = 20) -> bool:
    """Wait for the Trinity server to be ready."""
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(f"{url}/health", method="GET")
            resp = urllib.request.urlopen(req, timeout=2)
            if resp.status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def trinity_server():
    """Start Trinity API server as a session-scoped fixture.

    Launches the server in a background thread, waits for it to be ready,
    and tears it down after all tests complete.
    """
    if not _port_free(TRINITY_HOST, TRINITY_PORT):
        pytest.skip(f"Port {TRINITY_PORT} is not available — skip server-dependent tests")

    # Use a subprocess for clean isolation
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    env["TRINITY_DB_PATH"] = TEST_DB_PATH

    # Remove test DB if it exists to start fresh
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "trinity.api.server:app",
            "--host", TRINITY_HOST,
            "--port", str(TRINITY_PORT),
            "--log-level", "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    ready = _wait_for_server(TRINITY_URL)
    if not ready:
        proc.terminate()
        proc.wait(timeout=5)
        pytest.fail(f"Trinity server did not start on {TRINITY_URL} within timeout")

    yield TRINITY_URL

    # Teardown
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    # Clean up test database
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    wal = TEST_DB_PATH + "-wal"
    shm = TEST_DB_PATH + "-shm"
    for path in (wal, shm):
        if os.path.exists(path):
            os.remove(path)


@pytest.fixture(scope="session")
def test_db(trinity_server):
    """Provide the isolated SQLite test database path."""
    return TEST_DB_PATH


@pytest.fixture(scope="session")
def marvis_adapter(trinity_server):
    """Create a MarvisAdapter instance pointed at the test server."""
    from trinity.a2a.adapters.marvis_adapter import MarvisAdapter
    adapter = MarvisAdapter(trinity_base_url=TRINITY_URL, agent_id="marvis-main")
    # RBAC middleware (default-deny) requires agent identity headers on
    # protected routes; the test client acts as the marvis orchestrator.
    adapter._session.headers.update({
        "X-Agent-ID": "marvis-main",
        "X-Agent-Role": "admin",
    })
    # Register the Marvis orchestrator card
    try:
        adapter.register_marvis_agent_card()
    except Exception:
        pass  # server may have bootstrapped it already
    return adapter


@pytest.fixture
def fresh_sub_agent(marvis_adapter):
    """Fixture that registers a unique sub-agent and cleans up after test."""
    import uuid
    agent_name = f"test-agent-{uuid.uuid4().hex[:8]}"
    capabilities = ["test_capability"]
    result = marvis_adapter.register_sub_agent(agent_name, capabilities)
    yield agent_name, result
    # Cleanup: no explicit unregister needed for in-memory test runs
