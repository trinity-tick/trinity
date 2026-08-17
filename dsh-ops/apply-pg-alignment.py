#!/usr/bin/env python3
"""apply-pg-alignment.py — 备份 memories 表并应用 align-pg-schema.sql（事务内执行）。

用法:
    python dsh-ops/apply-pg-alignment.py [--host 127.0.0.1] [--port 5432]
                                        [--user postgres] [--password postgres]

备份写入: C:\\Users\\Administrator\\.trinity\\backups\\memories_backup_<ts>.csv
"""
import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_BACKUP_DIR = Path(r"C:\Users\Administrator\.trinity\backups")
SQL_FILE = Path(__file__).resolve().parent / "align-pg-schema.sql"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default="postgres")
    parser.add_argument("--dbname", default="trinity")
    parser.add_argument("--no-backup", action="store_true", help="跳过备份（不推荐）")
    args = parser.parse_args()

    import psycopg2

    conn = psycopg2.connect(
        host=args.host, port=args.port, dbname=args.dbname,
        user=args.user, password=args.password, connect_timeout=5,
    )
    conn.autocommit = False
    cur = conn.cursor()

    # ── 1. 备份 ──────────────────────────────────────────────────
    if not args.no_backup:
        DEFAULT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = DEFAULT_BACKUP_DIR / f"memories_backup_{ts}.csv"
        with open(backup_path, "w", newline="", encoding="utf-8") as f:
            cur.copy_expert("COPY memories TO STDOUT WITH (FORMAT csv, HEADER true)", f)
        print(f"[OK] backed up memories -> {backup_path}")

    # ── 2. 应用 ALTER ────────────────────────────────────────────
    sql = SQL_FILE.read_text(encoding="utf-8")
    # 先去掉整行注释，再按 ; 切分（避免文件头注释与第一条语句合并被误跳过）
    clean_lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    statements = [s.strip() for s in "\n".join(clean_lines).split(";") if s.strip()]
    applied = 0
    for stmt in statements:
        if stmt.upper().startswith("--") or not stmt:
            continue
        cur.execute(stmt)
        applied += 1
    conn.commit()
    print(f"[OK] applied {applied} ALTER statement(s)")

    # ── 3. 校验 ──────────────────────────────────────────────────
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='memories' ORDER BY ordinal_position"
    )
    cols = [r[0] for r in cur.fetchall()]
    expected = {"agent_id", "ttl_seconds", "last_accessed_at", "access_count",
                "importance_score", "content_hash", "conflict_group_id",
                "is_resolved", "modality", "metadata", "source_uri"}
    missing = expected - set(cols)
    if missing:
        print(f"[WARN] still missing columns: {sorted(missing)}")
    else:
        print(f"[OK] memories now has {len(cols)} columns; all expected columns present")
    cur.execute("SELECT count(*) FROM memories")
    print(f"[OK] memories rows: {cur.fetchone()[0]}")
    conn.close()
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
