"""Storage adapters — SQLite (default/dev) and PostgreSQL (production/multi-tenant).

Auto-detection:
  1. If ``DATABASE_URL`` starts with ``postgresql://`` → PostgreSQL
  2. If ``PGHOST`` or ``PGUSER`` environment variables are set → PostgreSQL
  3. Otherwise → SQLite (default for dev/backup)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .base import StorageAdapter
from .sqlite import SQLiteAdapter
from .postgresql import PostgreSQLAdapter

logger = logging.getLogger(__name__)

__all__ = [
    "StorageAdapter",
    "SQLiteAdapter",
    "PostgreSQLAdapter",
    "get_adapter",
]


def get_adapter(
    backend: Optional[str] = None,
    auto_migrate: bool = False,
    **kwargs,
) -> StorageAdapter:
    """Create and connect the appropriate storage adapter.

    Auto-detection order:
      ``backend`` parameter → ``DATABASE_URL`` env → default (SQLite)

    Args:
        backend: ``"sqlite"``, ``"postgresql"``, or ``None`` for auto-detect.
        auto_migrate: If True and switching from SQLite to PostgreSQL,
                      automatically migrate existing data.
        **kwargs: Additional keyword arguments passed to the adapter.

    Returns:
        A connected StorageAdapter instance.

    Examples:
        # Auto-detect (SQLite default)
        adapter = get_adapter()

        # Force PostgreSQL with env vars
        adapter = get_adapter("postgresql")

        # Force PostgreSQL with explicit config
        adapter = get_adapter(
            "postgresql",
            host="db.example.com",
            dbname="trinity_prod",
            user="app",
            password="secret",
        )

        # Force PostgreSQL with DSN
        import os
        os.environ["DATABASE_URL"] = "postgresql://user:pass@host:5432/db"
        adapter = get_adapter()
    """
    if backend is None:
        # Auto-detect
        db_url = os.environ.get("DATABASE_URL", "")
        if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
            backend = "postgresql"
        elif os.environ.get("PGHOST") or os.environ.get("PGUSER"):
            backend = "postgresql"
        else:
            backend = "sqlite"

    if backend == "postgresql":
        adapter = PostgreSQLAdapter(**kwargs)
        try:
            adapter.connect()
            logger.info("Connected to PostgreSQL backend")
        except Exception as e:
            logger.warning(
                "PostgreSQL connection failed (%s), falling back to SQLite", e
            )
            # Fallback to SQLite for resilience
            adapter = SQLiteAdapter(**kwargs)
            adapter.connect()

        # Auto-migrate from SQLite if requested
        if auto_migrate and isinstance(adapter, PostgreSQLAdapter):
            sqlite_path = kwargs.get(
                "db_path",
                os.path.join(os.path.dirname(__file__), "..", "..", "trinity.db"),
            )
            if os.path.exists(sqlite_path):
                logger.info("Migrating data from SQLite to PostgreSQL...")
                stats = adapter.migrate_from_sqlite(sqlite_path)
                logger.info(
                    "Migration complete: %d memories, %d versions",
                    stats["memories_migrated"],
                    stats["versions_migrated"],
                )

    else:
        adapter = SQLiteAdapter(**kwargs)
        adapter.connect()
        logger.info("Connected to SQLite backend")

    return adapter


def configure_adapter(config: dict) -> StorageAdapter:
    """Configure and connect an adapter from a config dictionary.

    Supports both direct loading and reading from a JSON/YAML config file.

    Args:
        config: Configuration dict with keys:
            - ``backend``: ``"sqlite"`` or ``"postgresql"``
            - ``**kwargs``: Backend-specific config (host, port, etc.)

    Returns:
        Connected StorageAdapter instance.

    Example config dict:
        {
            "backend": "postgresql",
            "host": "localhost",
            "port": 5432,
            "dbname": "trinity",
            "user": "trinity",
            "password": "secret",
        }
    """
    backend = config.pop("backend", "sqlite")
    return get_adapter(backend, **config)
