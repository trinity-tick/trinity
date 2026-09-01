#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PG → SQLite active 回填（2026-09-01，自查短板 #2 修复——补上缺失的镜像方向）

背景：对账显示 PG 主存储比 SQLite 镜像多 5,158 条（5,152 条 active）——PG 成为
主存储后 API/gateway/MCP 直写 PG 的记忆从未回流 SQLite。日链 pg-sync 只有
SQLite→PG 方向，缺反向。

本脚本：把 PG 中 active 且 SQLite 不存在的记忆按原 memory_id 回填进 SQLite，
走与 adapter 相同的加密 / FTS(触发器自动) / memory_versions / 审计链（action=PG_BACKFILL）。
幂等：已存在的 memory_id 跳过；可每日运行，作为 pg-backfill 维护任务与日链反向同步。

用法: python scripts/backfill_sqlite_from_pg.py [--limit 0] [--dry-run]
"""
import argparse
import datetime
import json
import os
import sys
import time
import uuid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0=全部")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
    import sqlite3
    import psycopg2
    from trinity.adapters.sqlite import SQLiteAdapter

    sq_db = os.environ.get("TRINITY_STORE_DB") or os.path.expanduser("~/.trinity/store/trinity_store.db")
    conn = sqlite3.connect(sq_db, timeout=60)
    conn.row_factory = sqlite3.Row
    existing = set(r[0] for r in conn.execute("SELECT memory_id FROM memories") if r[0])
    conn.close()

    pg = psycopg2.connect(host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
                          port=os.environ.get("TRINITY_PG_PORT", "5432"),
                          dbname="trinity", user=os.environ.get("TRINITY_PG_USER", "trinity"),
                          password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"))
    cur = pg.cursor()
    cols = "memory_id, session_id, persona_id, tenant_id, agent_id, content, role, importance, tags, category, created_at, updated_at, access_count, source_uri, modality, metadata"
    cur.execute("SELECT " + cols + " FROM memories WHERE status='active' ORDER BY created_at")
    rows = [dict(zip([c.strip() for c in cols.split(",")], r)) for r in cur.fetchall()]
    pg.close()
    if args.limit:
        rows = rows[:args.limit]
    todo = [r for r in rows if r["memory_id"] and r["memory_id"] not in existing]
    print("PG active total=%d, missing in SQLite=%d" % (len(rows), len(todo)))

    if args.dry_run:
        print("DRY-RUN: would backfill %d memories" % len(todo))
        return 0
    if not todo:
        print("BACKFILL: nothing to do (mirror already aligned)")
        return 0

    adapter = SQLiteAdapter(db_path=sq_db)
    adapter.connect()
    # 复用 adapter 的加密/分词助手（同一包内私有方法，与写入路径完全一致）
    backfilled = 0
    errors = 0
    t0 = time.time()
    for i, rec in enumerate(todo):
        try:
            content = str(rec.get("content") or "")
            if not content.strip():
                continue
            mid = rec["memory_id"]
            vid = "ver_" + uuid.uuid4().hex[:12]
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            created = rec.get("created_at")
            created_iso = created.isoformat() if hasattr(created, "isoformat") else (str(created) if created else now)
            updated = rec.get("updated_at")
            updated_iso = updated.isoformat() if hasattr(updated, "isoformat") else (str(updated) if updated else now)
            tags_json = json.dumps(rec.get("tags") or [], ensure_ascii=False)
            sha = adapter._compute_sha256(content)
            encrypted = adapter._encrypt_content(content)
            tokenized = adapter._tokenized_for_storage(content, adapter._tokenize_content_for_fts(content))
            metadata_json = json.dumps(rec.get("metadata") or {}, ensure_ascii=False)

            c = sqlite3.connect(sq_db, timeout=60)
            try:
                with c:
                    ins = c.execute(
                        """INSERT OR IGNORE INTO memories
                           (memory_id, session_id, persona_id, tenant_id, agent_id, app_id, content,
                            tokenized_content, role, importance, tags, category, memory_layer, sha256_hash,
                            status, version, ttl_seconds, last_accessed_at, access_count, importance_score,
                            content_hash, conflict_group_id, is_resolved, modality, metadata, source_uri,
                            created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',1,NULL,NULL,?,0.0,?,NULL,0,?,?,?,?,?)""",
                        (mid, rec.get("session_id"), rec.get("persona_id") or "default",
                         rec.get("tenant_id") or "default", rec.get("agent_id") or "default",
                         None, encrypted, tokenized, rec.get("role") or "user",
                         rec.get("importance") or 0.5, tags_json, rec.get("category") or "general",
                         None, sha, rec.get("access_count") or 0, sha,
                         rec.get("modality") or "text", metadata_json, rec.get("source_uri"),
                         created_iso, updated_iso))
                    if ins.rowcount == 0:
                        continue
                    c.execute(
                        "INSERT OR IGNORE INTO memory_versions (version_id, memory_id, content, sha256_hash, operation, created_at) VALUES (?,?,?,?,'PG_BACKFILL_CREATE',?)",
                        (vid, mid, encrypted, sha, created_iso))
            finally:
                c.close()
            adapter.write_audit_log(
                memory_id=mid, action="PG_BACKFILL",
                agent_id="system-maintenance",
                details={"reason": "pg->sqlite active backfill (missing mirror direction)",
                         "category": rec.get("category") or "general",
                         "backfilled_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            )
            backfilled += 1
            if backfilled % 200 == 0:
                print("  ...%d/%d (%.1fs)" % (backfilled, len(todo), time.time() - t0), flush=True)
        except Exception as e:  # noqa: BLE001
            errors += 1
            if errors <= 5:
                print("  ERR %s: %s" % (rec.get("memory_id"), str(e)[:120]))
    adapter.disconnect()
    print("BACKFILL done: backfilled=%d errors=%d (%.1fs)" % (backfilled, errors, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
