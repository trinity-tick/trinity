"""Storage adapters — SQLite (default) and PostgreSQL (multi-tenant)."""
from .base import StorageAdapter
from .sqlite import SQLiteAdapter
from .postgresql import PostgreSQLAdapter

__all__ = ["StorageAdapter", "SQLiteAdapter", "PostgreSQLAdapter"]
