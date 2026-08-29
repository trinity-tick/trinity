# -*- coding: utf-8 -*-
"""sync_sqlite_to_pg.py — SQLite → PG 增量镜像同步（2026-08-29 双写过渡）。

幂等 upsert（ON CONFLICT DO UPDATE）；按 memory_id 对齐；统计新增/更新。
用法: python scripts/sync_sqlite_to_pg.py [--limit N] [--full]
"""
import os
import sys
import json
import time
import argparse

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

PG_URL = os.environ.get("TRINITY_PG_URL",
                        "postgresql://trinity:trinity@127.0.0.1:5432/trinity")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--full", action="store_true", help="全量重同步（重建表）")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    import psycopg2
    import sqlite3 as _sq
    # 2026-08-29 (full): direct SQL - include ALL statuses (archived/lme etc)
    _db = os.path.join(os.path.expanduser("~/.trinity/store"), "trinity_store.db")
    if os.environ.get("TRINITY_DB_PATH"):
        _db = os.environ["TRINITY_DB_PATH"]
    _conn = _sq.connect(_db)
    _conn.row_factory = _sq.Row
    _limit = args.limit or 100000
    _cursor = _conn.execute("SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (_limit,))
    rows = [dict(r) for r in _cursor.fetchall()]
    _conn.close()
    conn = psycopg2.connect(PG_URL)
    cur = conn.cursor()
    if args.full:
        cur.execute("DROP TABLE IF EXISTS memories")
        cur.execute("""
          CREATE TABLE memories (
            memory_id TEXT PRIMARY KEY, content TEXT, content_hash TEXT,
            persona_id TEXT, session_id TEXT, agent_id TEXT, app_id TEXT,
            tenant_id TEXT, category TEXT, tags JSONB, importance REAL,
            created_at TEXT, updated_at TEXT, last_accessed_at TEXT,
            memory_layer TEXT, access_count INTEGER DEFAULT 0, source_uri TEXT,
            status TEXT DEFAULT 'active', conflict_group_id TEXT,
            is_resolved BOOLEAN DEFAULT false, metadata JSONB)
        """)
        conn.commit()
    t0 = time.time(); ins = 0; upd = 0; err = 0
    for r in rows:
        if not r.get("memory_id"):
            continue  # 2026-08-29: skip rows without memory_id (PG PK constraint)
        try:
            cur.execute("""
                INSERT INTO memories (memory_id, content, content_hash, persona_id,
                  session_id, agent_id, app_id, tenant_id, category, tags,
                  importance, created_at, updated_at, last_accessed_at,
                  memory_layer, access_count, source_uri, status,
                  conflict_group_id, is_resolved, metadata)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (memory_id) DO UPDATE SET
                  content=EXCLUDED.content, updated_at=EXCLUDED.updated_at,
                  access_count=EXCLUDED.access_count, status=EXCLUDED.status,
                  last_accessed_at=EXCLUDED.last_accessed_at""",
                (r.get("memory_id"), r.get("content"), r.get("content_hash"),
                 r.get("persona_id"), r.get("session_id"), r.get("agent_id"),
                 r.get("app_id"), r.get("tenant_id"), r.get("category"),
                 json.dumps(r.get("tags") or []), r.get("importance"),
                 str(r.get("created_at")), str(r.get("updated_at")),
                 str(r.get("last_accessed_at")), r.get("memory_layer"),
                 int(r.get("access_count") or 0), r.get("source_uri"),
                 r.get("status"), r.get("conflict_group_id"),
                 bool(r.get("is_resolved")), json.dumps(r.get("metadata") or {})))
            if cur.rowcount == 1 and cur.statusmessage.startswith("INSERT"):
                ins += 1
            else:
                upd += 1
        except Exception as _e:
            err += 1
            if err <= 2:
                print("ERR:", type(_e).__name__, str(_e)[:150])
    conn.commit()
    cur.execute("SELECT count(*) FROM memories")
    total = cur.fetchone()[0]
    print(f"sync {len(rows)} rows | inserted {ins} | updated {upd} | err {err} | "
          f"PG total {total} | {time.time()-t0:.1f}s")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
