# -*- coding: utf-8 -*-
"""过期复核队列（2026-09-02, Fable 5.1 对照审计 P1-④）。

背景：Fable 5.1 泄露揭示的记忆治理四问含 "When will it expire? Can the
user inspect, correct, or delete it?"。Trinity 原有 ttl_seconds（created_at
+ ttl 静态到期 age-out），缺逐条 wall-clock expires_at 的显式复核通道。

本脚本（maintenance 任务 expiry-review 的 Python 入口）：
  - 扫指定存储（默认 PG 主存储；--store sqlite 可选）中 status='active'
    且 metadata 含 expires_at（ISO-8601）的记忆；
  - 产出复核队列报告 ~/.trinity/state/expiry_review_<ts>.json：
      expired = 已到期（expires_at <= now）
      due     = 临期（now < expires_at <= now + horizon，默认 7 天）
    条目含 memory_id/content_preview/expires_at/importance/category；
  - 默认 dry-run 只出队列（供 agent/人工复核决定 保留/归档/删除）；
    --apply-expired 才把已到期记忆置 status='expired'（不进检索面）并写
    链式审计 action=EXPIRED_AT（details 含 expires_at）。
入库侧"写入即到期"的即时归档在 core/client/_ingestion.py（同 action，
source=ingest）。聚合池/MCP 侧到期由现有 pool clean_expired 处理（独立
expire_at 字段），本脚本只管引擎库双存储。

用法：
    python scripts/run_expiry_review.py [--store pg|sqlite] [--db PATH]
        [--horizon-days 7] [--limit 500] [--apply-expired]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

REVIEW_DIR = Path.home() / ".trinity" / "state"


def _parse_iso(value: str):
    """容忍 Z/无时区 ISO 字符串 → aware datetime(UTC)。"""
    s = str(value).strip()
    if not s:
        return None
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            d = _dt.datetime.fromisoformat(s)
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def _scan_sqlite(db_path: str) -> list:
    import sqlite3
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT memory_id, metadata, importance, category, content, created_at "
            "FROM memories WHERE status='active'").fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        md = r["metadata"] or "{}"
        try:
            md = json.loads(md) if isinstance(md, str) else (md or {})
        except Exception:
            md = {}
        ea = md.get("expires_at")
        if not ea:
            continue
        exp = _parse_iso(ea)
        if exp is None:
            continue
        content = r["content"] or ""
        # sqlite content 列可能 enc:v1 密文——preview 尽力而为，取不到就用标记
        if content.startswith("enc:v1:"):
            try:
                from trinity.security.crypto import decrypt_content
                content = decrypt_content(content) or ""
            except Exception:
                content = ""
        out.append({
            "memory_id": r["memory_id"], "expires_at": exp.isoformat(),
            "importance": r["importance"], "category": r["category"],
            "content_preview": (content or "")[:100],
            "created_at": r["created_at"],
        })
    return out


def _scan_pg(limit: int) -> list:
    import psycopg2
    from trinity.security.credentials import resolve_credentials
    creds = resolve_credentials()
    out = []
    with psycopg2.connect(host=creds["host"], port=creds["port"],
                          dbname=creds["dbname"], user=creds["user"],
                          password=creds["password"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT memory_id, metadata, importance, category, content, created_at "
                "FROM memories WHERE status='active' "
                "AND metadata IS NOT NULL AND metadata->>'expires_at' IS NOT NULL "
                "LIMIT %s", (limit,))
            for row in cur.fetchall():
                md = row[1] or {}
                ea = md.get("expires_at")
                if not ea:
                    continue
                exp = _parse_iso(ea)
                if exp is None:
                    continue
                content = row[4] or ""
                if content.startswith("enc:v1:"):
                    try:
                        from trinity.security.crypto import decrypt_content
                        content = decrypt_content(content) or ""
                    except Exception:
                        content = ""
                out.append({
                    "memory_id": str(row[0]), "expires_at": exp.isoformat(),
                    "importance": row[2], "category": row[3],
                    "content_preview": (content or "")[:100],
                    "created_at": row[5].isoformat() if hasattr(row[5], "isoformat") else str(row[5]),
                })
    return out


def _apply_expired_pg(memory_ids):
    import psycopg2
    from trinity.security.credentials import resolve_credentials
    creds = resolve_credentials()
    with psycopg2.connect(host=creds["host"], port=creds["port"],
                          dbname=creds["dbname"], user=creds["user"],
                          password=creds["password"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET status='expired', updated_at=NOW() "
                "WHERE memory_id = ANY(%s) AND status='active'", (memory_ids,))
    from trinity.adapters.postgresql import PostgreSQLAdapter
    ad = PostgreSQLAdapter(host=creds["host"], port=creds["port"],
                           dbname=creds["dbname"], user=creds["user"],
                           password=creds["password"])
    ad.connect()
    try:
        for mid in memory_ids:
            ad.write_audit_log(memory_id=mid, action="EXPIRED_AT",
                               agent_id="maintenance",
                               details={"source": "expiry-review", "policy": "apply"})
    finally:
        try:
            ad.disconnect()
        except Exception:
            pass


def _apply_expired_sqlite(db_path: str, memory_ids):
    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "UPDATE memories SET status='expired' "
            "WHERE memory_id IN (%s) AND status='active'"
            % ",".join("?" * len(memory_ids)), memory_ids)
        con.commit()
    finally:
        con.close()
    from trinity.adapters.sqlite import SQLiteAdapter
    ad = SQLiteAdapter(db_path=db_path)
    ad.connect()
    try:
        for mid in memory_ids:
            ad.write_audit_log(memory_id=mid, action="EXPIRED_AT",
                               agent_id="maintenance",
                               details={"source": "expiry-review", "policy": "apply"})
    finally:
        try:
            ad.disconnect()
        except Exception:
            pass


def run_review(store: str = "pg", db_path: str = "",
               horizon_days: int = 7, limit: int = 500,
               apply_expired: bool = False, out_dir=None):
    """执行过期复核。返回报告 dict。"""
    now = _dt.datetime.now(_dt.timezone.utc)
    horizon = _dt.timedelta(days=max(0, int(horizon_days)))

    if store == "sqlite":
        db_path = db_path or str(Path.home() / ".trinity" / "store" / "trinity_store.db")
        items = _scan_sqlite(db_path)
    else:
        items = _scan_pg(limit)

    expired = []
    due = []
    broken = 0
    for it in items:
        exp = _parse_iso(it["expires_at"])
        if exp is None:
            broken += 1
            continue
        if exp <= now:
            expired.append(it)
        elif exp <= now + horizon:
            it["days_left"] = round((exp - now).total_seconds() / 86400.0, 1)
            due.append(it)
    expired.sort(key=lambda x: x["expires_at"])
    due.sort(key=lambda x: x["expires_at"])

    report = {
        "generated": now.isoformat(),
        "store": store,
        "horizon_days": horizon_days,
        "counts": {"expired": len(expired), "due": len(due), "broken": broken},
        "expired": expired[:limit],
        "due": due[:limit],
    }
    if apply_expired and expired:
        ids = [x["memory_id"] for x in expired]
        if store == "sqlite":
            _apply_expired_sqlite(db_path, ids)
        else:
            _apply_expired_pg(ids)
        report["applied"] = {"status": "expired", "count": len(ids)}

    out_dir = Path(out_dir) if out_dir else REVIEW_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / ("expiry_review_" + now.strftime("%Y%m%d_%H%M%S") + ".json")
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    report["out_file"] = str(out_file)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="expiry review queue")
    ap.add_argument("--store", choices=["pg", "sqlite"], default="pg")
    ap.add_argument("--db", default="", help="sqlite db path (test/override)")
    ap.add_argument("--horizon-days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--apply-expired", action="store_true",
                    help="mark already-expired memories status=expired + audit")
    ap.add_argument("--dry-run", action="store_true",
                    help="compat: queue-only mode (default behaviour; blocks apply)")
    ap.add_argument("--out-dir", default="")
    args = ap.parse_args(argv)
    report = run_review(store=args.store, db_path=args.db,
                        horizon_days=args.horizon_days, limit=args.limit,
                        apply_expired=args.apply_expired and not args.dry_run,
                        out_dir=args.out_dir or None)
    print("expiry-review store=%s expired=%d due=%d broken=%d%s" % (
        report["store"], report["counts"]["expired"], report["counts"]["due"],
        report["counts"]["broken"],
        (" applied=%d" % report.get("applied", {}).get("count", 0))
        if report.get("applied") else ""))
    print("queue:", report["out_file"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
