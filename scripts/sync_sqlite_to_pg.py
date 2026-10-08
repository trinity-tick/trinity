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
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:  # 独立脚本可能没有 trinity 路径：退回原静默行为
    def swallow(*_a, **_k):
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(*_a, **_k)
        except Exception:
            return None

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
from trinity._tags import normalize_tags  # EXECUTION 771：SQLite 的 tags 是 JSON 文本，直接 dumps 会双重编码


def _pg_connect():
    """统一凭据入口（2026-10-06 T5 明文凭据收口）。

    改前（明文，已实测能连 —— 所以它"看起来没问题"）：
        PG_URL = os.environ.get("TRINITY_PG_URL", "<一个把口令写进源码的 DSN 常量>")
    明文口令进源码，且带一个"看起来能连"的弱默认值 ⇒ 口令一旦轮换，
    本脚本会静默用旧口令连库或报认证失败，而口令本身长期留在仓库里。

    改后：TRINITY_PG_URL（显式）→ 统一凭据（env TRINITY_PG_* → ~/.dsh/.credentials.yaml）
    → **取不到口令即报错退出（fail-closed）**，源码里不再有任何口令字面量。
    """
    import psycopg2
    url = (os.environ.get("TRINITY_PG_URL") or "").strip()
    if url:
        return psycopg2.connect(url)
    from trinity.security.credentials import dsn_redacted, resolve_pg_dsn
    dsn = resolve_pg_dsn()
    if not dsn:
        raise SystemExit(
            "PG 凭据未解析到（TRINITY_PG_URL / TRINITY_PG_PASSWORD / ~/.dsh/.credentials.yaml "
            "均无有效口令）⇒ 拒绝用弱默认口令连接。期望形态：%s"
            % dsn_redacted("postgresql://user@host/db"))
    return psycopg2.connect(dsn)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--full", action="store_true", help="全量重同步（重建表）")
    ap.add_argument("--check-connection", action="store_true",
                    help="只验证凭据链能连（只读 SELECT 1，不写任何数据）")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    import sqlite3 as _sq
    if args.check_connection:
        # 「改前能连、改后仍能连」的实测入口：只读，不落任何写入。
        from trinity.security.credentials import credential_provenance, dsn_redacted, resolve_pg_dsn
        conn = _pg_connect()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        one = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories")
        total = cur.fetchone()[0]
        conn.close()
        print("PG connect OK | SELECT 1 = %s | memories = %s | dsn = %s | password 来源 = %s"
              % (one, total, dsn_redacted(resolve_pg_dsn() or "postgresql://user@host/db"),
                 credential_provenance().get("password")))
        return 0

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
    conn = _pg_connect()
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
        _cat = str(r.get("category") or "general")
        if _cat in ("lme", "stress-test"):
            # EXECUTION 486: 基准类目不回填 PG（458C 清理防复发——SQLite 镜像仍含 lme，
            # 此前每日 pg-sync 把它再次灌回 PG archived 13,742 条）
            continue
        # 2026-09-10（658.43）：时间字段清洗 + 逐行隔离。
        # 背景：镜像里存在字符串 'None'（历史脏写），而 PG 侧 created_at 已迁移为
        # timestamptz（658.34）→ 'None'::timestamptz 报 InvalidDatetimeFormat →
        # **整个事务中止**，后续所有行连带失败（实测一次 sync err=22,767、静默丢弃
        # 全部待同步增量，含知识采集结果）。现在：①'None'/''/非法 → NULL；
        # ②每行 SAVEPOINT 隔离，单行失败不再污染整批。
        def _ts(_v):
            if _v is None:
                return None
            _s = str(_v).strip()
            if _s == "" or _s.lower() in ("none", "null", "nan"):
                return None
            return _s
        try:
            cur.execute("SAVEPOINT sp_row")
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
                 json.dumps(normalize_tags(r.get("tags"))), r.get("importance"),
                 _ts(r.get("created_at")), _ts(r.get("updated_at")),
                 _ts(r.get("last_accessed_at")), r.get("memory_layer"),
                 int(r.get("access_count") or 0), r.get("source_uri"),
                 r.get("status"), r.get("conflict_group_id"),
                 bool(r.get("is_resolved")), json.dumps(r.get("metadata") or {})))
            cur.execute("RELEASE SAVEPOINT sp_row")
            if cur.rowcount == 1 and cur.statusmessage.startswith("INSERT"):
                ins += 1
            else:
                upd += 1
        except Exception as _e:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT sp_row")
            except Exception as _e:
                swallow(__name__, _e)
            err += 1
            if err <= 3:
                print("ERR:", type(_e).__name__, str(_e)[:150],
                      "| memory_id=", str(r.get("memory_id"))[:40])
    conn.commit()
    cur.execute("SELECT count(*) FROM memories")
    total = cur.fetchone()[0]
    print(f"sync {len(rows)} rows | inserted {ins} | updated {upd} | err {err} | "
          f"PG total {total} | {time.time()-t0:.1f}s")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
