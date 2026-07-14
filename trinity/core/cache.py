"""
Engine initialization cache — module-level singleton for the second_brain engine.

The second_brain engine imports and initialises 122 modules upon construction,
touching retrieval channels, guardian chains, and dozens of sub-modules.
Every call to ``Trinity()`` in legacy mode (no adapter) would previously
re-import and rebuild the entire engine, incurring a significant startup cost.

This module provides a simple module-level singleton cache that:

  * Stores the engine instance in a module-level global after first creation.
  * Returns the cached instance on subsequent ``get_engine()`` calls.
  * Supports explicit ``reset_engine()`` for testing or configuration changes.
  * Is thread-safe via a basic lock (no double-initialisation races).

Usage:
    from trinity.core.cache import get_engine, reset_engine

    engine = get_engine()       # Created once, cached thereafter
    engine = get_engine()       # Returns same instance
    reset_engine()              # Force re-creation on next get_engine()
"""

import threading
from typing import Any, Optional

# Module-level singleton
_engine: Any = None
_engine_lock = threading.Lock()


def get_engine() -> Any:
    """Return the cached second_brain Engine singleton.

    On the first call this imports ``Engine`` from
    ``trinity.modules.second_brain`` and instantiates it.
    Subsequent calls return the same instance unless ``reset_engine()``
    has been called in between.

    Returns:
        A ``SecondBrainV636`` (or compatible) engine instance.
    """
    global _engine

    if _engine is not None:
        return _engine

    with _engine_lock:
        # Double-checked locking pattern
        if _engine is not None:
            return _engine

        from trinity.modules.second_brain import Engine as _SecondBrainEngine

        _engine = _SecondBrainEngine()
        return _engine


def reset_engine() -> None:
    """Reset the cached engine singleton.

    The next call to ``get_engine()`` will perform a fresh import and
    instantiation of the second_brain engine.

    This is primarily useful for:
        * Testing scenarios where a fresh engine state is required.
        * Reloading after configuration changes.
    """
    global _engine

    with _engine_lock:
        _engine = None


def get_engine_status() -> dict:
    """Return diagnostics about the engine cache state.

    Returns:
        A dict with ``cached`` (bool) and ``engine_type`` (str or None).
    """
    return {
        "cached": _engine is not None,
        "engine_type": type(_engine).__name__ if _engine is not None else None,
    }
