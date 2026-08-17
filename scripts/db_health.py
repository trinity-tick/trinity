# -*- coding: utf-8 -*-
"""DB health maintenance: integrity_check + WAL checkpoint (2026-08-16 稳定性)。
定期运行防止 WAL 膨胀、提前发现库损坏。
"""
import sqlite3, sys, os

DB = r'C:\Users\Administrator\.trinity\store\trinity_store.db'

def main():
    conn = sqlite3.connect(DB, timeout=20)
    try:
        # WAL checkpoint (truncate)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            print(f'DB-HEALTH: wal_checkpoint warn: {e}')
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
