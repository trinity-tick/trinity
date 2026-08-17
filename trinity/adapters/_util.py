"""Shared helpers for the sqlite adapter package."""

from __future__ import annotations

import functools


def _safe_write(fn):
    """write-path guard: rollback on any exception to avoid dangling write tx."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise
    return wrapper

