# -*- coding: utf-8 -*-
"""Active 集健康监控 (2026-08-18)

输出：total/active/archived、active 占比、归档高价值记忆数、active 分类分布。
告警：归档高重要性(>=0.8)且高访问(>=10)的记忆 > 0 时 WARN，
     提示运行 scripts/restore_high_value_memories.py 恢复（dedup/echo 连带归档）。

接入：dsh-ops/trinity-dsh-maintenance.ps1 的 active-health 任务（每日链）。
"""
import os
import sqlite3
import sys

DB = os.path.expanduser("~/.trinity/store/trinity_store.db")


def main() -> int:
    conn = sqlite3.connect(DB, timeout=20)
    try:
        cur = conn.cursor()
        total = cur.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = cur.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
        archived = cur.execute("SELECT COUNT(*) FROM memories WHERE status='archived'").fetchone()[0]
        arch_hi = cur.execute(
            "SELECT COUNT(*) FROM memories WHERE status='archived' AND importance>=0.8"
        ).fetchone()[0]
        arch_hi_acc = cur.execute(
            "SELECT COUNT(*) FROM memories m WHERE m.status='archived' AND m.importance>=0.8 "
            "AND m.access_count>=10 AND NOT EXISTS ("
            "  SELECT 1 FROM audit_log a WHERE a.memory_id = m.memory_id "
            "  AND (a.action='archive_dedup' OR (a.action='archive_echo' AND a.details LIKE '%meta%'))"
            ")"
        ).fetchone()[0]
        ratio = active / total if total else 0.0
        cats = cur.execute(
            "SELECT category, COUNT(*) FROM memories WHERE status='active' "
            "GROUP BY category ORDER BY 2 DESC LIMIT 6"
        ).fetchall()

        print(
            f"ACTIVE-HEALTH: total={total} active={active} archived={archived} "
            f"active_ratio={ratio:.1%} archived_high_imp={arch_hi} "
            f"archived_high_imp_high_access={arch_hi_acc}"
        )
        for cat, n in cats:
            print(f"  active[{cat}]={n}")

        if arch_hi_acc > 0:
            print(
                f"ACTIVE-HEALTH WARN: {arch_hi_acc} high-value memories archived "
                f"(run scripts/restore_high_value_memories.py to restore)"
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
