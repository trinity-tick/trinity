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

⭐ 滞后上限（2026-10-08 / t159 定案，G17 t160 落成条文）：
  SQLite 是**派生镜像、不是实时库** ⇒ ⭐ **"读到的可能是最多约 1 个镜像周期（≈1 天）之前的状态"**。
  · 于是"**写进 PG 却读不到**"在**一个镜像周期内是【预期行为】，不是丢失**；
  · 可观测：`python scripts/mirror_freshness.py`（只读 SQLite `audit_log` 的
    `PG_MIRROR_STATUS` / `PG_BACKFILL` 的 `max(timestamp)` ⇒ 报"滞后多少小时"+ fresh/stale，退出码 0/1/2）；
  · 需要**立即**读到 ⇒ 直接查 PG（**提高镜像频率属另一件事，代价未评估 ⇒ 用户决定**）；
  · 本条**只声明边界，不保证"小时级可见"**：2026-10-08 实测队长 13:13:58 写入的记忆在 SQLite 全表 0 条，
    起因是镜像自 03:05:04 +08 未再跑（滞后 10.15 h，**仍在"≈1 天"之内 ⇒ 按本条不算违约**）——
    也就是说：**要小时级可见，必须改周期/改事件驱动，而不是靠本条**。

⭐ 存量例外（2026-10-08 / G29 t172，**队长决定**）：
  **2026-10-08 之前**由镜像写入的行，其 `content_hash` / `sha256_hash` **可能是基于密文算的**（实测 270 条里 3 条族）；
  ⛔ **历史回填/重算【不在本修法范围】**（那是写操作，且会改变唯一索引 `idx_memories_content_hash` 的碰撞行为）。
  本脚本自 2026-10-08 起：**明文口径**（`content` 列仍写 PG 那条密文；解不开 ⇒ 两个 hash 写 NULL + 打印留痕）。

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
    from trinity._tags import normalize_tags  # EXECUTION 771

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
                          password=os.environ.get("TRINITY_PG_PASSWORD", ""))
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
    n_ins = n_ign = n_st = errors = 0
    for rec in todo_insert:
        try:
            if _insert(adapter, rec, sq_db):
                n_ins += 1
            else:
                n_ign += 1
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
    print("MIRROR done: inserted=%d dedup-skipped=%d status-aligned=%d errors=%d (%.1fs)" %
          (n_ins, n_ign, n_st, errors, time.time() - t0))
    return 0


def _sha(content: str) -> str:
    import hashlib
    return hashlib.sha256(str(content).encode("utf-8")).hexdigest()


def _insert(adapter, rec, sq_db):
    import sqlite3
    import uuid as _uuid
    # 2026-09-16（本轮）：normalize_tags 原先**只在 main() 内导入**（函数局部作用域），
    # 而本函数在**另一个**作用域里使用它 ⇒ 真正执行到这一行时必然 NameError
    # （scripts/undefined_global_audit.py 全仓唯一命中，命中是对的、不是误报）。
    # 修法：在本函数内按同一模式惰性导入（与上面两条 import 一致）。
    from trinity._tags import normalize_tags  # EXECUTION 771
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
    tags_json = json.dumps(normalize_tags(rec.get("tags")), ensure_ascii=False)  # EXECUTION 771
    #: ⭐ G29/t172（2026-10-08 队长拍板）：`sha256_hash`/`content_hash` 迁移到**明文口径**。
    #: 契约：`docs/STORAGE_ENCRYPTION_20260815.md:35`「`sha256_hash / content_hash` | **基于明文计算**」。
    #: ⚠️ 与 t156 的 `_plain` **共用同一份明文副本与同一道 fail-open 防护**（解密后仍是 `enc:v1:` ⇒ 视为解不开）。
    #: ⛔ 解不开时**两个 hash 写 NULL**（列可空；唯一索引 `WHERE content_hash IS NOT NULL` 也不参与去重）
    #: —— 绝不把密文当明文取 sha。回滚：`TRINITY_MIRROR_HASH_PLAIN=0`。
    _hash_plain = os.environ.get("TRINITY_MIRROR_HASH_PLAIN", "1").strip() not in (
        "0", "off", "false", "")
    _tok_plain = os.environ.get("TRINITY_MIRROR_TOKENIZE_PLAIN", "1").strip() not in (
        "0", "off", "false", "")
    _plain, _plain_ok = content, True
    if content.startswith("enc:v1:") and (_hash_plain or _tok_plain):
        try:
            from trinity.security.crypto import decrypt_content as _dc
            _plain = str(_dc(content) or "")
        except Exception:      # noqa: BLE001 — 解不开 ⇒ 下面统一按"解不开"处理（不是静默：会打印留痕）
            _plain = ""
        if _plain.startswith("enc:v1:"):
            _plain, _plain_ok = "", False
        if not _plain_ok:
            print("MIRROR-PLAIN-CALIBER-FAIL memory_id=%s（hash/分词均按'解不开'处理）" % mid)
    sha = _sha(_plain) if (_hash_plain and _plain_ok and _plain) else (
        _sha(content) if not _hash_plain else None)
    encrypted = adapter._encrypt_content(content)
    #: ⭐ G13/t156（2026-10-08）：PG 侧 content **可能已经是密文**（TRINITY_PG_ENCRYPT_CATEGORIES=perception），
    #: 而本行原本把它当明文推进 tokenize ⇒ `tokenized_content` 装密文（实测 2,623 行）、FTS 里也是密文。
    #: 照抄常规路径范式（`sqlite/_crud.py`：**先给明文副本分词、再加密**）：
    #: 这里**只改"拿什么去分词"** —— `content` 列存什么、`content_hash` 怎么算**不在本任务范围**（用户决定）。
    #: 回滚/牙齿：`TRINITY_MIRROR_TOKENIZE_PLAIN=0` 退回旧行为。
    #: ⭐ G13/t156 + G29/t172：分词源**复用**上面那份 `_plain`（同一道 fail-open 防护，不再重复解密）。
    _fts_src = _plain if _tok_plain else content
    tokenized = adapter._tokenized_for_storage(_fts_src, adapter._tokenize_content_for_fts(_fts_src))
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
                # 2026-09-09：被唯一索引 idx_memories_content_hash(persona_id, agent_id,
                # content_hash) 去重跳过——属预期语义，但必须回报，否则统计把"忽略"
                # 记成"插入"（实测 291 条 todo 里 290 条被静默忽略）。
                return False
            c.execute(
                "INSERT OR IGNORE INTO memory_versions (version_id, memory_id, content, sha256_hash, operation, created_at) VALUES (?,?,?,?,?,?)",
                (vid, mid, encrypted, sha, "PG_MIRROR_CREATE", created_iso))
    finally:
        c.close()
    adapter.write_audit_log(
        memory_id=mid, action="PG_BACKFILL",
        agent_id="system-maintenance",
        details={"reason": "pg->sqlite mirror insert", "category": rec.get("category") or "general"})
    return True


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
