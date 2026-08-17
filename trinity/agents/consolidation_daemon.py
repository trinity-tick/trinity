"""
Trinity P1-6 & v7.0.0: Consolidation Daemon
============================================
Background tasks for memory lifecycle management: auto-compress,
importance decay, periodic cleanup, and memory consolidation.

Aligned with: Zep / Graphiti (background memory maintenance)
"""

from __future__ import annotations

import threading
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ConsolidationDaemon:
    """Background daemon for memory lifecycle management.

    Runs periodic cycles:
      1. Cleanup expired memories (delegates to aggregator.cleanup)
      2. Merge similar memories when pool exceeds threshold
         (delegates to aggregator.merge_memories)

    Usage:
        daemon = ConsolidationDaemon(aggregator, interval_seconds=300)
        daemon.start()
        # ... application lifecycle ...
        daemon.stop()
    """

    def __init__(self, aggregator, interval_seconds: int = 300):
        self._aggregator = aggregator
        self._interval = interval_seconds
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._cycle_count = 0

    def start(self) -> None:
        """Start the background consolidation loop.

        No-op if already running.
        """
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="trinity-consolidation"
        )
        self._thread.start()
        logger.info(
            "ConsolidationDaemon started (interval=%ss)", self._interval
        )

    def stop(self) -> None:
        """Stop the background loop and join the thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info(
            "ConsolidationDaemon stopped (cycles=%d)", self._cycle_count
        )

    def _loop(self) -> None:
        """Main daemon loop: sleep → cleanup → merge → repeat."""
        while self._running:
            time.sleep(self._interval)
            if not self._running:
                break
            try:
                self._cycle_count += 1

                # 1. Cleanup expired memories
                removed = self._aggregator.cleanup()
                if removed > 0:
                    logger.info(
                        "Daemon cycle %d: cleaned %d expired memories",
                        self._cycle_count, removed,
                    )

                # 2. Merge similar memories (consolidation)
                pool_size = len(self._aggregator._pool)
                if pool_size > 50:
                    merged = self._aggregator.merge_memories()
                    if merged > 0:
                        logger.info(
                            "Daemon cycle %d: merged %d memories",
                            self._cycle_count, merged,
                        )

            except Exception as exc:
                logger.error(
                    "Daemon cycle %d error: %s", self._cycle_count, exc
                )

    def statistics(self) -> dict:
        """Return current daemon status."""
        return {
            "running": self._running,
            "interval_seconds": self._interval,
            "cycles": self._cycle_count,
        }
