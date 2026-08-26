# -*- coding: utf-8 -*-
"""DB health maintenance: integrity_check + WAL checkpoint (2026-08-16 稳定性)。
定期运行防止 WAL 膨胀、提前发现库损坏。
2026-08-24（R9 P0-2a）：加入 daily all 链；checkpoint 失败时告警 WAL 占用
（写锁被持有），并支持 TRINITY_DB_PATH 覆盖。
"""
import sqlite3, sys, os

DB = os.environ.get("TRINITY_DB_PATH") or r'C:\Users\Administrator\.trinity\store\trinity_store.db'

def main():
    conn = sqlite3.connect(DB, timeout=20)
    try:
        # WAL checkpoint (truncate)
        try:
            (busy, log, ckpt) = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy:
                print(f'DB-HEALTH: wal_checkpoint busy={busy} log={log} ckpt={ckpt} — WAL 未完全回收（写锁可能被持有）')
            else:
                print(f'DB-HEALTH: wal_checkpoint ok (log={log} ckpt={ckpt})')
        except Exception as e:
            print(f'DB-HEALTH: wal_checkpoint warn: {e}（写锁被持有？见 EXECUTION.md R9 P0-2 持锁排查）')
        # integrity
        res = conn.execute("PRAGMA integrity_check").fetchone()[0]
        # quick_stats
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
        wal = os.path.getsize(DB + '-wal') if os.path.exists(DB + '-wal') else 0
        print(f'DB-HEALTH: integrity={res} memories={total} active={active} wal_size={wal}')
        return 0 if res == 'ok' else 2
    finally:
        conn.close()

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.exit(main())
