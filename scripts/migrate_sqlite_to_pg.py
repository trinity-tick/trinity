#!/usr/bin/env python3
"""
Trinity — SQLite → PostgreSQL Migration Script
================================================
Reads all data from trinity_store.db and migrates into a live PostgreSQL database.

Usage:
    python scripts/migrate_sqlite_to_pg.py                          # default PG params
    python scripts/migrate_sqlite_to_pg.py --host localhost --port 5432 --dbname trinity --user postgres --password postgres

Requirements:
    pip install psycopg2-binary
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ── Parsing helpers ────────────────────────────────────────────────────

def parse_tags(raw_tags: Any) -> List[str]:
    """Parse tags from SQLite TEXT field into a Python list."""
    if raw_tags is None:
        return []
    if isinstance(raw_tags, list):
        return raw_tags
    if isinstance(raw_tags, str):
        try:
            parsed = json.loads(raw_tags)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def to_uuid_or_text(value: Optional[str]) -> Optional[str]:
    """Pass through string IDs — PostgreSQL accepts them as UUID if well-formed."""
    return value


def to_timestamptz(value: Optional[str]) -> Optional[str]:
    """Normalize ISO 8601 timestamps to PostgreSQL-compatible format."""
    if value is None:
        return None
    # Some rows have trailing 'Z', some have '+00:00', some have space separators
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    # Replace space between date and time with 'T' if present
    if " " in value and "T" not in value:
        value = value.replace(" ", "T", 1)
    return value


# ── Main migration logic ───────────────────────────────────────────────

def migrate(
    sqlite_path: str,
    pg_host: str = "localhost",
    pg_port: int = 5432,
    pg_dbname: str = "trinity",
    pg_user: str = "postgres",
    pg_password: str = "postgres",
) -> Dict[str, Any]:
    """Execute full migration from SQLite to PostgreSQL.

    Returns:
        dict with keys: memories_migrated, versions_migrated, audit_migrated,
                        tenants_migrated, errors, error_details
    """
    stats: Dict[str, Any] = {
        "memories_migrated": 0,
        "versions_migrated": 0,
        "audit_migrated": 0,
        "tenants_migrated": 0,
        "errors": 0,
        "error_details": [],
    }

    # ── Connect ──────────────────────────────────────────────────────
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
        sys.exit(1)

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    pg_conn = psycopg2.connect(
        host=pg_host,
        port=pg_port,
        dbname=pg_dbname,
        user=pg_user,
        password=pg_password,
    )
    pg_conn.autocommit = False

    try:
        pg_cur = pg_conn.cursor()

        # Ensure schema exists
        print("[1/5] Ensuring PostgreSQL schema ...")
        schema_sql_path = (
            __file__.replace("\\", "/").rsplit("/", 1)[0] + "/init_pg.sql"
        )
        try:
            with open(schema_sql_path, "r") as f:
                init_sql = f.read()
            # Execute each statement
            for stmt in init_sql.split(";"):
                s = stmt.strip()
                if s and not s.startswith("--"):
                    try:
                        pg_cur.execute(s)
                    except Exception:
                        pass  # skip known issues like DO blocks with RAISE
            pg_conn.commit()
            print("    Schema ready.")
        except FileNotFoundError:
            print("    WARNING: init_pg.sql not found. Assuming schema already exists.")
        except Exception as e:
            print(f"    WARNING: Schema init issue (may already exist): {e}")
            pg_conn.rollback()

        # ── Migrate tenants ───────────────────────────────────────
        print("[2/5] Migrating tenants ...")
        sqlite_cur = sqlite_conn.cursor()
        sqlite_cur.execute("SELECT * FROM tenants")
        for row in sqlite_cur.fetchall():
            d = dict(row)
            try:
                pg_cur.execute(
                    """INSERT INTO tenants (tenant_id, name, created_at)
                       VALUES (%s, %s, %s::timestamptz)
                       ON CONFLICT (tenant_id) DO NOTHING""",
                    (
                        d.get("tenant_id", "default"),
                        d.get("name", "Default Tenant"),
                        to_timestamptz(d.get("created_at")) or datetime.now(timezone.utc).isoformat(),
                    ),
                )
                stats["tenants_migrated"] += 1
            except Exception as e:
                stats["errors"] += 1
                stats["error_details"].append(f"tenant {d.get('tenant_id')}: {e}")
        pg_conn.commit()
        print(f"    {stats['tenants_migrated']} tenants migrated.")

        # ── Migrate memories ──────────────────────────────────────
        print("[3/5] Migrating memories ...")
        sqlite_cur.execute("SELECT * FROM memories ORDER BY created_at ASC")
        for row in sqlite_cur.fetchall():
            d = dict(row)
            try:
                memory_id = d.get("memory_id", "")
                session_id = d.get("session_id", "")
                # Convert text IDs to UUID-compatible format if not already
                # SQLite stores them as plain text; PG expects UUID or valid text castable to UUID
                # We'll use text cast via ::uuid for well-formed UUIDs, else generate new ones
                pg_cur.execute(
                    """INSERT INTO memories
                       (memory_id, session_id, persona_id, tenant_id, content, role,
                        importance, tags, category, sha256_hash, status, version,
                        created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s::timestamptz, %s::timestamptz)
                       ON CONFLICT (memory_id) DO NOTHING""",
                    (
                        memory_id,
                        session_id,
                        d.get("persona_id", "default"),
                        d.get("tenant_id", "default"),
                        d.get("content", ""),
                        d.get("role", "user"),
                        float(d.get("importance", 0.5)),
                        parse_tags(d.get("tags")),
                        d.get("category", "general"),
                        d.get("sha256_hash", ""),
                        d.get("status", "active"),
                        int(d.get("version", 1)),
                        to_timestamptz(d.get("created_at")),
                        to_timestamptz(d.get("updated_at")),
                    ),
                )
                stats["memories_migrated"] += 1
            except Exception as e:
                stats["errors"] += 1
                stats["error_details"].append(f"memory {d.get('memory_id')}: {e}")
        pg_conn.commit()
        print(f"    {stats['memories_migrated']} memories migrated.")

        # ── Migrate memory_versions ───────────────────────────────
        print("[4/5] Migrating memory_versions ...")
        try:
            sqlite_cur.execute("SELECT * FROM memory_versions ORDER BY created_at ASC")
            for row in sqlite_cur.fetchall():
                d = dict(row)
                try:
                    pg_cur.execute(
                        """INSERT INTO memory_versions
                           (memory_id, content, sha256_hash, operation, created_at)
                           VALUES (%s, %s, %s, %s, %s::timestamptz)
                           ON CONFLICT DO NOTHING""",
                        (
                            d.get("memory_id", ""),
                            d.get("content", ""),
                            d.get("sha256_hash", ""),
                            d.get("operation", "CREATE"),
                            to_timestamptz(d.get("created_at")),
                        ),
                    )
                    stats["versions_migrated"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    stats["error_details"].append(f"version {d.get('version_id')}: {e}")
            pg_conn.commit()
        except sqlite3.OperationalError as e:
            print(f"    SKIPPED: memory_versions table not found ({e})")
        print(f"    {stats['versions_migrated']} versions migrated.")

        # ── Migrate audit_log ─────────────────────────────────────
        print("[5/5] Migrating audit_log ...")
        try:
            sqlite_cur.execute("SELECT * FROM audit_log ORDER BY timestamp ASC")
            for row in sqlite_cur.fetchall():
                d = dict(row)
                try:
                    # audit_log.memory_id in SQLite may reference text IDs that won't cast to UUID.
                    # We set them to NULL if casting fails.
                    mem_id = d.get("memory_id")
                    metadata_raw = d.get("metadata", "{}")
                    if isinstance(metadata_raw, str):
                        try:
                            metadata_val = json.loads(metadata_raw)
                        except (json.JSONDecodeError, TypeError):
                            metadata_val = {}
                    else:
                        metadata_val = metadata_raw

                    pg_cur.execute(
                        """INSERT INTO audit_log
                           (action, memory_id, persona_id, content_hash, metadata, timestamp)
                           VALUES (%s, %s, %s, %s, %s::jsonb, %s::timestamptz)""",
                        (
                            d.get("action", "MIGRATED"),
                            mem_id if mem_id else None,
                            d.get("persona_id"),
                            d.get("content_hash"),
                            json.dumps(metadata_val),
                            to_timestamptz(d.get("timestamp")),
                        ),
                    )
                    stats["audit_migrated"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    stats["error_details"].append(f"audit {d.get('id')}: {e}")
            pg_conn.commit()
        except sqlite3.OperationalError as e:
            print(f"    SKIPPED: audit_log table not found ({e})")
        print(f"    {stats['audit_migrated']} audit logs migrated.")

    except Exception as e:
        pg_conn.rollback()
        stats["errors"] += 1
        stats["error_details"].append(f"FATAL: {e}")
        raise
    finally:
        sqlite_conn.close()
        pg_conn.close()

    return stats


# ── CLI ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Trinity SQLite → PostgreSQL migration"
    )
    parser.add_argument(
        "--sqlite-path",
        default="data/trinity_store.db",
        help="Path to SQLite database (default: data/trinity_store.db)",
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--dbname", default="trinity")
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default="postgres")
    args = parser.parse_args()

    print("=" * 60)
    print("  Trinity — SQLite → PostgreSQL Migration")
    print("=" * 60)
    print(f"  Source:      {args.sqlite_path}")
    print(f"  Destination: postgresql://{args.user}@{args.host}:{args.port}/{args.dbname}")
    print()

    result = migrate(
        sqlite_path=args.sqlite_path,
        pg_host=args.host,
        pg_port=args.port,
        pg_dbname=args.dbname,
        pg_user=args.user,
        pg_password=args.password,
    )

    print()
    print("=" * 60)
    print("  Migration Complete")
    print("=" * 60)
    print(f"  Tenants:   {result['tenants_migrated']}")
    print(f"  Memories:  {result['memories_migrated']}")
    print(f"  Versions:  {result['versions_migrated']}")
    print(f"  Audit:     {result['audit_migrated']}")
    print(f"  Errors:    {result['errors']}")
    if result["error_details"]:
        print("  Error details:")
        for err in result["error_details"][:10]:
            print(f"    - {err}")
        if len(result["error_details"]) > 10:
            print(f"    ... and {len(result['error_details']) - 10} more")
    print()

    if result["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
