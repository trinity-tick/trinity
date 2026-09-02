#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PG → SQLite 镜像同步（2026-09-01 升级为全量：PG 单写主后，SQLite 是派生镜像）

背景：PG 成为唯一写主（api/gateway/worker 直写 PG；decay/tiers 已切 PG）。本任务
把 PG 全量状态镜像回 SQLite：
  1. PG 有、SQLite 无 → 按原 id 插入（加密/FTS/版本链/审计 PG_BACKFILL）
  2. 状态不一致 → 对齐（PG active 而 SQLite archived → 恢复；PG archived 而 SQLite
     active → 归档——decay/tiers 的 PG 侧结果回灌镜像）
  3. （不做内容级覆盖：PG content 列是密文，原始 psycopg2 读取无法安全回灌；
     个别内容不一致行由 reconcile 报告后人工处理）
幂等；可每日运行（日链 pg-backfill 任务，顺序在 mirror/decay 之前）。

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
    ap.add_argument("--limit", type=int, default=0, help="0=全部（限处理行数，测试用）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
    import sqlite3
    import psycopg2
    from trinity.adapters.sqlite import SQLiteAdapter

    sq_db = os.environ.get("TRINITY_STORE_DB") or os.path.expanduser("~/.trinity/store/trinity_store.db")

    # ── 读取 SQLite 现状 ──
    conn = sqlite3.connect(sq_db, timeout=60)
    conn.row_factory = sqlite3.Row
    sq_rows = {r["memory_id"]: dict(r) for r in conn.execute(
        "SELECT memory_id, status, sha256_hash FROM memories") if r["memory_id"]}
    conn.close()

    # ── 读取 PG 全量 ──
    pg = psycopg2.connect(host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
                          port=os.environ.get("TRINITY_PG_PORT", "5432"),
                          dbname="trinity", user=os.environ.get("TRINITY_PG_USER", "trinity"),
                          password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"))
    cur = pg.cursor()
    cols = ("memory_id, session_id, persona_id, tenant_id, agent_id, content, role, importance, "
            "tags, category, created_at, updated_at, access_count, source_uri, modality, metadata, status")
    cur.execute("SELECT " + cols + " FROM memories ORDER BY created_at")
    pg_rows = [dict(zip([c.strip() for c in cols.split(",")], r)) for r in cur.fetchall()]
    pg.close()
    if args.limit:
        pg_rows = pg_rows[:args.limit]

    todo_insert = [r for r in pg_rows if r["memory_id"] and r["memory_id"] not in sq_rows]
    todo_status = [r for r in pg_rows if r["memory_id"] and r["memory_id"] in sq_rows
                   and sq_rows[r["memory_id"]]["status"] != r.get("status")]
    print("PG total=%d | insert=%d status-align=%d" % (len(pg_rows), len(todo_insert), len(todo_status)))
    if args.dry_run:
        print("DRY-RUN: insert=%d status-align=%d" % (len(todo_insert), len(todo_status)))
        return 0
    if not (todo_insert or todo_status):
        print("MIRROR: already aligned")
        return 0

    adapter = SQLiteAdapter(db_path=sq_db)
    adapter.connect()
    t0 = time.time()
    n_ins = n_st = errors = 0
    for rec in todo_insert:
        try:
            _insert(adapter, rec, sq_db)
            n_ins += 1
        except Exception as e:  # noqa: BLE001
            errors += 1
            if errors <= 5:
                print("  INS ERR %s: %s" % (rec["memory_id"], str(e)[:100]))
    for rec in todo_status:
        try:
            _align_status(adapter, rec, sq_db)
            n_st += 1
        except Exception as e:  # noqa: BLE001
            errors += 1
            if errors <= 5:
                print("  ST ERR %s: %s" % (rec["memory_id"], str(e)[:100]))
    adapter.disconnect()
    print("MIRROR done: inserted=%d status-aligned=%d errors=%d (%.1fs)" %
          (n_ins, n_st, errors, time.time() - t0))
    return 0


def _sha(content: str) -> str:
    import hashlib
    return hashlib.sha256(str(content).encode("utf-8")).hexdigest()


def _insert(adapter, rec, sq_db):
    import sqlite3
    import uuid as _uuid
    content = str(rec.get("content") or "")
    if not content.strip():
        return
    mid = rec["memory_id"]
    vid = "ver_" + _uuid.uuid4().hex[:12]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    created = rec.get("created_at")
    created_iso = created.isoformat() if hasattr(created, "isoformat") else (str(created) if created else now)
    updated = rec.get("updated_at")
    updated_iso = updated.isoformat() if hasattr(updated, "isoformat") else (str(updated) if updated else now)
    tags_json = json.dumps(rec.get("tags") or [], ensure_ascii=False)
    sha = _sha(content)
    encrypted = adapter._encrypt_content(content)
    tokenized = adapter._tokenized_for_storage(content, adapter._tokenize_content_for_fts(content))
    metadata_json = json.dumps(rec.get("metadata") or {}, ensure_ascii=False)
    status = rec.get("status") or "active"
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
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,NULL,?,0.0,?,NULL,0,?,?,?,?,?)""",
                (mid, rec.get("session_id"), rec.get("persona_id") or "default",
                 rec.get("tenant_id") or "default", rec.get("agent_id") or "default",
                 None, encrypted, tokenized, rec.get("role") or "user",
                 rec.get("importance") or 0.5, tags_json, rec.get("category") or "general",
                 None, sha, status, rec.get("access_count") or 0, sha,
                 rec.get("modality") or "text", metadata_json, rec.get("source_uri"),
                 created_iso, updated_iso))
            if ins.rowcount == 0:
                return
            c.execute(
                "INSERT OR IGNORE INTO memory_versions (version_id, memory_id, content, sha256_hash, operation, created_at) VALUES (?,?,?,?,?,?)",
                (vid, mid, encrypted, sha, "PG_MIRROR_CREATE", created_iso))
    finally:
        c.close()
    adapter.write_audit_log(
        memory_id=mid, action="PG_BACKFILL",
        agent_id="system-maintenance",
        details={"reason": "pg->sqlite mirror insert", "category": rec.get("category") or "general"})


def _align_status(adapter, rec, sq_db):
    import sqlite3
    mid = rec["memory_id"]
    status = rec.get("status") or "active"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    c = sqlite3.connect(sq_db, timeout=60)
    try:
        with c:
            upd = c.execute(
                "UPDATE memories SET status=?, updated_at=? WHERE memory_id=? AND status!=?",
                (status, now, mid, status))
            if upd.rowcount == 0:
                return
    finally:
        c.close()
    adapter.write_audit_log(
        memory_id=mid, action="PG_MIRROR_STATUS",
        agent_id="system-maintenance",
        details={"reason": "pg->sqlite status align", "status": status})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
