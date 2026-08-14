"""Pytest fixtures for Trinity v8.0 performance benchmarks.

Provides in-process Trinity instance backed by isolated SQLite.
Import-time banner spam is suppressed via logging.disable().
Embedding backend is forced to 'hash' to avoid external API dependency.
"""

import os
import sys
import tempfile
import logging
import pytest

# Suppress P21–P129 import banners
logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


@pytest.fixture(scope="function")
def trinity_bench():
    """Create an isolated Trinity instance backed by temp SQLite.

    Yields the Trinity instance; cleans up temp DB on teardown.
    Auto-linking via embeddings is disabled for benchmark isolation.
    """
    from trinity import Trinity

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="trinity_bench_")
    os.close(fd)
    os.environ["TRINITY_DB_PATH"] = db_path

    tri = Trinity()

    # Patch _auto_link_semantic to no-op (avoids Ollama HTTP calls)
    tri._auto_link_semantic = lambda *a, **kw: []

    yield tri

    tri._adapter.disconnect()
    for path in [db_path, db_path + "-wal", db_path + "-shm"]:
        try:
            os.unlink(path)
        except OSError:
            pass
