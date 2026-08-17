"""Unit test fixtures for Trinity v8.0 identity / audit / a2a packages."""

import os
import sys
import tempfile
import logging

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

logging.disable(logging.CRITICAL)


@pytest.fixture
def adapter():
    """Isolated SQLite adapter on a temporary database file."""
    from trinity.adapters.sqlite import SQLiteAdapter

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    a = SQLiteAdapter(db_path=db_path)
    a.connect()
    yield a
    a.disconnect()
    if os.path.exists(db_path):
        os.unlink(db_path)
    wal = db_path + "-wal"
    shm = db_path + "-shm"
    for f in (wal, shm):
        if os.path.exists(f):
            os.unlink(f)


@pytest.fixture
def identity_manager(adapter):
    """IdentityManager backed by an isolated SQLite DB."""
    from trinity.identity.identity_manager import IdentityManager

    return IdentityManager(storage_adapter=adapter)


@pytest.fixture
def auditor():
    """Auditor instance with fresh metrics."""
    from trinity.audit.auditor import Auditor

    return Auditor(adapter=None)


@pytest.fixture
def task_manager(adapter):
    """TaskManager backed by an isolated SQLite DB."""
    from trinity.a2a.task_manager import TaskManager

    return TaskManager(adapter=adapter)


@pytest.fixture
def capability_registry():
    """CapabilityRegistry with in-memory cache, no persistence adapter."""
    from trinity.a2a.capability_registry import CapabilityRegistry

    return CapabilityRegistry(adapter=None)
