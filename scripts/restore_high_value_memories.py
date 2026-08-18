# -*- coding: utf-8 -*-
"""恢复被误归档的高价值记忆 (2026-08-18)

背景：active 集仅 10.5%，审计发现 16 条 importance>=0.8 且 access_count>=10
的记忆被 archive_echo / archive_dedup / UPDATE_MEMORY 连带归档（非 decay，
decay 只选 active 里最冷的一条）。这些是高访问的高价值知识，恢复为 active。

条件：status='archived' AND importance>=0.8 AND access_count>=10，
      且排除「合法归档」——archive_dedup（精确重复，dup_of 已在 active）与
      archive_echo 的 sync echo meta（"pollutes search"），恢复它们反而污染检索面。
动作：status -> active + audit action=restore_knowledge（幂等，可重复运行）
"""
import os
import sys

os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")  # 防 import 期聚合器自举卡顿
DB = os.path.expanduser("~/.trinity/store/trinity_store.db")


def main() -> int:
    import sqlite3
    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 排除合法归档（archive_dedup=精确重复 / archive_echo 的 echo meta）
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

    # 用 adapter 的 write_audit_log 保证审计链式哈希正确
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
            # 先提交 UPDATE 释放写锁，再走 adapter 审计（adapter 用独立连接，
            # 未提交 UPDATE 会占锁导致 database is locked，见已知坑 #9）
            conn.commit()
            adapter.write_audit_log(
                memory_id=r["memory_id"],
                action="restore_knowledge",
                agent_id="system-maintenance",
                details={
                    "reason": "archived by echo/dedup cleanup but high importance & high access (collateral)",
                    "importance": r["importance"],
                    "access_count": r["access_count"],
                    "restored_at": "2026-08-18",
                },
            )
            restored += 1
            print(f"  restored {r['memory_id']} imp={r['importance']} acc={r['access_count']} :: {r['preview']}")
        conn.commit()
    finally:
        try:
            adapter.disconnect()
        except Exception:
            pass
        conn.close()

    print(f"RESTORE-HIGH-VALUE: restored {restored}/{len(rows)} memories")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
