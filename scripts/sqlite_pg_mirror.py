#!/usr/bin/env python3
"""
SQLite → PostgreSQL 单向镜像（M2-2）
======================================
把 SQLite runtime store（系统记录源）的 active 记忆幂等镜像到 PG batch 层
（docker trinity-db，127.0.0.1:5430）。

- 幂等：按 sha256_hash / memory_id 去重，重复运行零重复；
- 自动 schema 对齐：PG memories 缺列时补齐（幂等 ALTER，来自 dsh-ops/align-pg-schema.sql）；
- 默认只推 active（status='active'）记忆；
- 安全：连接参数优先级 环境变量 → ~/.dsh/.credentials.yaml → 默认值。

用法：
    python scripts/sqlite_pg_mirror.py                 # sqlite -> pg（默认）
    python scripts/sqlite_pg_mirror.py --dry-run       # 只统计不写入
    python scripts/sqlite_pg_mirror.py --sqlite <path> # 指定 SQLite 文件
    python scripts/sqlite_pg_mirror.py --pg-port 5430 --pg-user trinity --pg-password trinity
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

TRINITY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRINITY_ROOT))

DEFAULT_SQLITE = os.path.expanduser("~/.trinity/store/trinity_store.db")
ALIGN_SQL = TRINITY_ROOT / "dsh-ops" / "align-pg-schema.sql"

# 幂等 schema 对齐（与 dsh-ops/align-pg-schema.sql 一致的最小集，避免依赖文件编码）
ALIGN_STATEMENTS = [
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS agent_id VARCHAR(128)",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS ttl_seconds INTEGER",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMP",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS access_count INTEGER DEFAULT 0",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS importance_score DOUBLE PRECISION",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS conflict_group_id VARCHAR(64)",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS is_resolved BOOLEAN DEFAULT false",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS modality VARCHAR(32) DEFAULT 'text'",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS metadata JSONB",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS source_uri VARCHAR(512)",
]

# 新部署（docker trinity-db）的 id 列是 UUID 型且有外键约束（不能改列型），
# 因此镜像时把 SQLite 字符串 id 确定性映射为 UUIDv5（同输入 → 同 UUID，
# 保持引用一致性且幂等），不改 PG schema。
def to_uuid(value: str, salt: str = "") -> str:
    import uuid as _uuid
    if not value:
        return str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"trinity:{salt}:empty"))
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"trinity:{salt}:{value}"))


def load_credentials(path=os.path.expanduser("~/.dsh/.credentials.yaml")):
    creds = {}
    if os.path.exists(path):
        raw = open(path, "r", encoding="utf-8-sig").read()
        for line in raw.splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
            if m and not line.strip().startswith("#"):
                creds[m.group(1)] = m.group(2).strip().strip("'\"")
    return creds


def read_sqlite_active(db_path: str) -> list:
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT memory_id, persona_id, session_id, tenant_id, content, role, "
        "importance, tags, category, sha256_hash, created_at, updated_at "
        "FROM memories WHERE status='active' ORDER BY created_at"
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# 新部署（docker trinity-db，init_pg.sql）的 id 列是 UUID 型且带外键约束。
# 但 trinity.adapters.postgresql 与批处理脚本按 **VARCHAR id** 设计（写入 "default"、
# "squad_bench"、handoff_* 等非 UUID 值）——UUID 列会让 adapter 自身的
# store_memory 也报 InvalidTextRepresentation。因此这里做一次**幂等类型手术**：
# 检测 memory_id 仍为 uuid 时，整体转 VARCHAR(128) 并重建外键。
VARCHAR_ALTERS = [
    ("tenants", "tenant_id"),
    ("personas", "persona_id"), ("personas", "tenant_id"),
    ("sessions", "session_id"), ("sessions", "persona_id"), ("sessions", "tenant_id"),
    ("memories", "memory_id"), ("memories", "session_id"),
    ("memories", "persona_id"), ("memories", "tenant_id"),
    ("memory_versions", "memory_id"),
]


def _ensure_varchar_ids(cur) -> bool:
    """若 id 列为 uuid 型则整体转 VARCHAR(128) 并重建外键；返回是否执行了转换。"""
    cur.execute("SELECT data_type FROM information_schema.columns "
                "WHERE table_name='memories' AND column_name='memory_id'")
    row = cur.fetchone()
    converted = bool(row and row[0] == "uuid")
    if converted:
        cur.execute("""SELECT conname, conrelid::regclass::text, pg_get_constraintdef(oid)
                       FROM pg_constraint WHERE contype='f'""")
        fks = cur.fetchall()
        for conname, tbl, _defn in fks:
            cur.execute(f'ALTER TABLE {tbl} DROP CONSTRAINT IF EXISTS "{conname}"')
        for tbl, col in VARCHAR_ALTERS:
            cur.execute(f'ALTER TABLE {tbl} ALTER COLUMN {col} TYPE VARCHAR(128)')
        for conname, tbl, defn in fks:
            cur.execute(f'ALTER TABLE {tbl} ADD CONSTRAINT "{conname}" {defn}')
        # 清空镜像产生的旧数据（UUID 型时期写入），保持幂等；tenants 保留（按 name 复用）
        cur.execute("TRUNCATE memories, memory_versions, sessions, personas")
    # PostgreSQLAdapter.store_memory 不预置 sessions/personas/tenants 行
    # （随机生成 session uuid），故移除 memories 上的三个 FK 与既有工作形态一致；
    # 其余 FK（memory_versions→memories、sessions/personas→tenants）保留。
    for fk in ("memories_session_id_fkey", "memories_persona_id_fkey", "memories_tenant_id_fkey"):
        cur.execute(f'ALTER TABLE memories DROP CONSTRAINT IF EXISTS "{fk}"')
    return converted


def ensure_pg_schema(cur) -> bool:
    for stmt in ALIGN_STATEMENTS:
        cur.execute(stmt)
    converted = _ensure_varchar_ids(cur)
    try:
        cur.execute("SELECT to_regclass('memory_versions')")
        if cur.fetchone()[0]:
            cur.execute("ALTER TABLE memory_versions ALTER COLUMN version_id TYPE VARCHAR(64) USING version_id::text")
    except Exception:
        pass
    return converted


def seed_referenced_rows(cur, rows) -> dict:
    """按 FK 链预置 tenants → personas → sessions，返回 {原始值: uuid} 映射。

    策略：先按 name/session_id 查已有行，查到用其 uuid；查不到再插入（幂等）。
    """
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)

    def _resolve_tenant(name: str) -> str:
        cur.execute("SELECT tenant_id FROM tenants WHERE name = %s", (name,))
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute(
            "INSERT INTO tenants (tenant_id, name, created_at, is_active) VALUES (%s, %s, %s, true) "
            "ON CONFLICT (name) DO NOTHING",
            (name, name, now),
        )
        cur.execute("SELECT tenant_id FROM tenants WHERE name = %s", (name,))
        return str(cur.fetchone()[0])

    def _resolve_persona(name: str, tenant_uuid: str) -> str:
        cur.execute("SELECT persona_id FROM personas WHERE name = %s", (name,))
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute(
            "INSERT INTO personas (persona_id, tenant_id, name, created_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (persona_id) DO NOTHING",
            (name, tenant_uuid, name, now),
        )
        cur.execute("SELECT persona_id FROM personas WHERE name = %s", (name,))
        return str(cur.fetchone()[0])

    def _resolve_session(sid: str, persona_uuid: str, tenant_uuid: str) -> str:
        cur.execute("SELECT session_id FROM sessions WHERE session_id = %s", (sid,))
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute(
            "INSERT INTO sessions (session_id, persona_id, tenant_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING",
            (sid, persona_uuid, tenant_uuid, now, now),
        )
        return sid

    tenant_map, persona_map, session_map = {}, {}, {}
    for mem in rows:
        t = mem.get("tenant_id") or "default"
        p = mem.get("persona_id") or "default"
        s = mem.get("session_id") or "default"
        if t not in tenant_map:
            tenant_map[t] = _resolve_tenant(t)
        if p not in persona_map:
            persona_map[p] = _resolve_persona(p, tenant_map[t])
        if s not in session_map:
            session_map[s] = _resolve_session(s, persona_map[p], tenant_map[t])

    return {
        "tenant": tenant_map, "persona": persona_map, "session": session_map,
        "counts": {"tenants": len(tenant_map), "personas": len(persona_map),
                   "sessions": len(session_map)},
    }


def mirror_to_pg(rows, pg_cfg, dry_run=False) -> dict:
    import psycopg2
    import psycopg2.extras

    stats = {"added": 0, "skipped": 0, "errors": 0, "error_samples": []}
    conn = psycopg2.connect(
        host=pg_cfg["host"], port=pg_cfg["port"], dbname=pg_cfg["dbname"],
        user=pg_cfg["user"], password=pg_cfg["password"], connect_timeout=5,
    )
    conn.autocommit = False
    cur = conn.cursor()
    ensure_pg_schema(cur)
    refs = seed_referenced_rows(cur, rows)

    cur.execute("SELECT sha256_hash FROM memories WHERE sha256_hash IS NOT NULL")
    existing = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT memory_id::text FROM memories")
    existing_ids = {r[0] for r in cur.fetchall()}

    for mem in rows:
        mem_id = str(mem.get("memory_id", ""))
        h = mem.get("sha256_hash") or ""
        if h and h in existing:
            stats["skipped"] += 1
            continue
        if mem_id in existing_ids:
            stats["skipped"] += 1
            continue
        if dry_run:
            stats["added"] += 1
            continue
        try:
            tags = mem.get("tags")
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = [t.strip() for t in tags.split(",") if t.strip()]
            if not isinstance(tags, list):
                tags = []
            session_uuid = refs["session"].get(mem.get("session_id") or "default")
            persona_uuid = refs["persona"].get(mem.get("persona_id") or "default")
            tenant_uuid = refs["tenant"].get(mem.get("tenant_id") or "default")
            # 每行 SAVEPOINT：单行失败不回滚整批
            cur.execute("SAVEPOINT sp")
            try:
                cur.execute(
                    """INSERT INTO memories
                       (memory_id, session_id, persona_id, tenant_id, content, role,
                        importance, tags, category, sha256_hash, status, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s)""",
                    (mem_id,
                     session_uuid,
                     persona_uuid,
                     tenant_uuid,
                     mem.get("content", ""),
                     mem.get("role") or "user",
                     float(mem.get("importance") or 0.5),
                     tags,
                     mem.get("category") or "general",
                     h or None,
                     mem.get("created_at") or None,
                     mem.get("updated_at") or mem.get("created_at") or None,
                     ),
                )
                cur.execute("RELEASE SAVEPOINT sp")
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT sp")
                raise e
            stats["added"] += 1
        except Exception as e:
            stats["errors"] += 1
            if len(stats["error_samples"]) < 5:
                stats["error_samples"].append(f"{mem_id[:20]}: {str(e)[:100]}")
    stats["seeded"] = refs["counts"]

    if not dry_run:
        conn.commit()
    conn.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite -> PG mirror")
    parser.add_argument("--sqlite", default=DEFAULT_SQLITE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pg-host", default="")
    parser.add_argument("--pg-port", type=int, default=0)
    parser.add_argument("--pg-user", default="")
    parser.add_argument("--pg-password", default="")
    parser.add_argument("--pg-dbname", default="trinity")
    args = parser.parse_args()

    creds = load_credentials()
    pg_cfg = {
        "host": args.pg_host or os.environ.get("TRINITY_PG_HOST")
                or creds.get("TRINITY_PG_HOST") or "127.0.0.1",
        "port": args.pg_port or int(os.environ.get("TRINITY_PG_PORT")
                or creds.get("TRINITY_PG_PORT") or "5430"),
        "user": args.pg_user or os.environ.get("TRINITY_PG_USER")
                or creds.get("TRINITY_PG_USER") or "trinity",
        "password": args.pg_password or os.environ.get("TRINITY_PG_PASSWORD")
                    or creds.get("TRINITY_PG_PASSWORD") or "trinity",
        "dbname": args.pg_dbname or os.environ.get("TRINITY_PG_DB")
                  or creds.get("TRINITY_PG_DB") or "trinity",
    }

    if not os.path.exists(args.sqlite):
        print(f"ERROR: sqlite store not found: {args.sqlite}")
        return 1

    print(f"SQLite: {args.sqlite}")
    print(f"PG:     {pg_cfg['host']}:{pg_cfg['port']}/{pg_cfg['dbname']} as {pg_cfg['user']}")

    rows = read_sqlite_active(args.sqlite)
    print(f"SQLite active memories: {len(rows)}")

    t0 = time.time()
    stats = mirror_to_pg(rows, pg_cfg, dry_run=args.dry_run)
    print(f"Mirror ({'dry-run' if args.dry_run else 'commit'}): "
          f"added={stats['added']} skipped={stats['skipped']} errors={stats['errors']} "
          f"({time.time() - t0:.1f}s)")
    for s in stats["error_samples"]:
        print(f"  [error] {s}")

    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
