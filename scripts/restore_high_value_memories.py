# -*- coding: utf-8 -*-
"""恢复被误归档的高价值记忆 (2026-08-18, 2026-09-01 加 PG 路径)

背景：active 集仅 10.5%，审计发现 16 条 importance>=0.8 且 access_count>=10
的记忆被 archive_echo / archive_dedup / UPDATE_MEMORY 连带归档（非 decay）。
这些是高访问的高价值知识，恢复为 active。

条件：status='archived' AND importance>=0.8 AND access_count>=10，
      且排除「合法归档」——archive_dedup（精确重复）与 archive_echo 的 sync echo meta。
动作：status -> active + audit action=restore_knowledge（幂等，可重复运行）

后端：默认 sqlite（镜像）；RESTORE_BACKEND=pg 时作用于 PG 主存储
（2026-09-01 PG 单写主后，decay/tiers 的归档状态以 PG 为准）。
"""
import os
import sys

os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")  # 防 import 期聚合器自举卡顿
DB = os.path.expanduser("~/.trinity/store/trinity_store.db")
BACKEND = os.environ.get("RESTORE_BACKEND", "").strip().lower() or "sqlite"


def main() -> int:
    if BACKEND == "pg":
        return _restore_pg()
    import sqlite3
    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute(
        "SELECT m.memory_id, m.importance, m.access_count, "
        "substr(replace(replace(m.content, char(10), ' '), char(13), ' '), 1, 50) AS preview "
        "FROM memories m "
        "WHERE m.status='archived' AND m.importance>=0.8 AND m.access_count>=10 "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM audit_log a WHERE a.memory_id = m.memory_id "
        "  AND (a.action='archive_dedup' OR (a.action='archive_echo' AND a.details LIKE '%meta%'))"
        ") ORDER BY m.access_count DESC"
    ).fetchall()
    if not rows:
        print("RESTORE-HIGH-VALUE: nothing to restore (0 candidates)")
        conn.close()
        return 0

    from trinity.adapters.sqlite import SQLiteAdapter
    adapter = SQLiteAdapter(db_path=DB)
    adapter.connect()

    restored = 0
    try:
        for r in rows:
            upd = cur.execute(
                "UPDATE memories SET status='active', updated_at=datetime('now') "
                "WHERE memory_id=? AND status='archived'",
                (r["memory_id"],),
            )
            if upd.rowcount == 0:
                continue
            conn.commit()
            adapter.write_audit_log(
                memory_id=r["memory_id"],
                action="restore_knowledge",
                agent_id="system-maintenance",
                details={
                    "reason": "archived by echo/dedup cleanup but high importance & high access (collateral)",
                    "importance": r["importance"],
                    "access_count": r["access_count"],
                },
            )
            restored += 1
            print("  restored %s imp=%s acc=%s :: %s" % (r["memory_id"], r["importance"], r["access_count"], r["preview"]))
        conn.commit()
    finally:
        try:
            adapter.disconnect()
        except Exception:
            pass
        conn.close()

    print("RESTORE-HIGH-VALUE: restored %d/%d memories" % (restored, len(rows)))
    return 0


def _restore_pg() -> int:
    """PG 主存储路径：同条件 SELECT + UPDATE + PostgreSQLAdapter 审计。"""
    import psycopg2
    conn = psycopg2.connect(host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
                            port=os.environ.get("TRINITY_PG_PORT", "5432"),
                            dbname="trinity", user=os.environ.get("TRINITY_PG_USER", "trinity"),
                            password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"))
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT m.memory_id, m.importance, m.access_count "  # PG content 密文(bytea)，不读取内容列（2026-09-01 实测）
        "FROM memories m "
        "WHERE m.status='archived' AND m.importance>=0.8 AND m.access_count>=10 "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM audit_log a WHERE a.memory_id = m.memory_id "
        "  AND (a.action='archive_dedup' OR (a.action='archive_echo' AND a.details::text LIKE '%meta%'))"
        ") ORDER BY m.access_count DESC"
    )
    rows = cur.fetchall()
    if not rows:
        print("RESTORE-HIGH-VALUE: nothing to restore (0 candidates) [pg]")
        conn.close()
        return 0
    from trinity.adapters.postgresql import PostgreSQLAdapter
    adapter = PostgreSQLAdapter(
        host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
        dbname="trinity", user=os.environ.get("TRINITY_PG_USER", "trinity"),
        password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"))
    adapter.connect()
    restored = 0
    try:
        for mid, imp, acc in rows:
            cur.execute(
                "UPDATE memories SET status='active', updated_at=NOW() "
                "WHERE memory_id=%s AND status='archived'", (mid,))
            if cur.rowcount == 0:  # autocommit 下 execute 返回 None，rowcount 取 cur
                continue
            adapter.write_audit_log(
                memory_id=mid, action="restore_knowledge",
                agent_id="system-maintenance",
                details={"reason": "archived by echo/dedup cleanup but high importance & high access (collateral)",
                         "importance": imp, "access_count": acc})
            restored += 1
            print("  restored %s imp=%s acc=%s" % (mid, imp, acc))
    finally:
        try:
            adapter.disconnect()
        except Exception:
            pass
        conn.close()
    print("RESTORE-HIGH-VALUE: restored %d/%d memories [pg]" % (restored, len(rows)))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
