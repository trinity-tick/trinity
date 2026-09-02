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


def _health_pg() -> int:
    """PG 主存储路径（2026-09-01）：与 SQLite 版同口径的只读指标。"""
    conn = _psycopg2.connect(host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
                             port=os.environ.get("TRINITY_PG_PORT", "5432"),
                             dbname="trinity", user=os.environ.get("TRINITY_PG_USER", "trinity"),
                             password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"))
    try:
        cur = conn.cursor()
        total = cur.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = cur.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
        archived = cur.execute("SELECT COUNT(*) FROM memories WHERE status='archived'").fetchone()[0]
        arch_hi = cur.execute("SELECT COUNT(*) FROM memories WHERE status='archived' AND importance>=0.8").fetchone()[0]
        arch_hi_acc = cur.execute(
            "SELECT COUNT(*) FROM memories m WHERE m.status='archived' AND m.importance>=0.8 "
            "AND m.access_count>=10 AND NOT EXISTS ("
            "  SELECT 1 FROM audit_log a WHERE a.memory_id = m.memory_id "
            "  AND (a.action='archive_dedup' OR (a.action='archive_echo' AND a.details::text LIKE '%meta%'))"
            ")").fetchone()[0]
        ratio = active / total if total else 0.0
        cats = cur.execute(
            "SELECT category, COUNT(*) FROM memories WHERE status='active' "
            "GROUP BY category ORDER BY 2 DESC LIMIT 6").fetchall()
        print("ACTIVE-HEALTH: total=%d active=%d archived=%d active_ratio=%.1f%% archived_high_imp=%d archived_high_imp_high_access=%d"
              % (total, active, archived, ratio * 100, arch_hi, arch_hi_acc))
        for cat, n in cats:
            print("  active[%s]=%d" % (cat, n))
        if arch_hi_acc > 0:
            print("ACTIVE-HEALTH WARN: %d high-value memories archived (run restore_high_value_memories.py with RESTORE_BACKEND=pg to restore)"
                  % arch_hi_acc)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
