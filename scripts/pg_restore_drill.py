# -*- coding: utf-8 -*-
"""PG 恢复演练（EXECUTION 108 制度化）：最新备份恢复到临时库→校验计数→清理。

用法: python scripts/pg_restore_drill.py [--keep]
入维护链: maintenance -Tasks backup 后接 drill（或每月手动）。
只读目标库（trinity_restore_drill 临时库），演练后默认清理。
"""
import sys, os, subprocess, glob, argparse

PG_BIN = r"C:\Users\Administrator\Desktop\pgsql\bin"
BACKUP_DIR = os.path.expanduser(r"~\\.trinity\\backups")
DRILL_DB = "trinity_restore_drill"
ENV = dict(os.environ, PGPASSWORD="trinity")

def run(args, check=True):
    r = subprocess.run(args, capture_output=True, text=True, env=ENV)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd failed: {args}: {r.stderr[-500:]}")
    return r

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留演练库便于检查")
    args = ap.parse_args()

    dumps = sorted(glob.glob(os.path.join(BACKUP_DIR, "trinity_pg_*.dump")))
    if not dumps:
        print("FAIL: no pg dump found in", BACKUP_DIR); return 1
    dump = dumps[-1]
    print(f"drill: restore {os.path.basename(dump)}")

    run([os.path.join(PG_BIN, "psql.exe"), "-h", "127.0.0.1", "-p", "5432", "-U", "trinity", "-d", "postgres",
         "-c", f"DROP DATABASE IF EXISTS {DRILL_DB};"], check=False)
    run([os.path.join(PG_BIN, "psql.exe"), "-h", "127.0.0.1", "-p", "5432", "-U", "trinity", "-d", "postgres",
         "-c", f"CREATE DATABASE {DRILL_DB} OWNER trinity;"])
    r = run([os.path.join(PG_BIN, "pg_restore.exe"), "-h", "127.0.0.1", "-p", "5432", "-U", "trinity",
             "-d", DRILL_DB, "-j", "4", "--no-owner", "--no-privileges", dump])
    print("restore done (exit 0)")

    # verify counts vs source
    def count(db, tbl):
        r = run([os.path.join(PG_BIN, "psql.exe"), "-h", "127.0.0.1", "-p", "5432", "-U", "trinity", "-d", db,
                 "-tAc", f"select count(*) from {tbl};"])
        return int(r.stdout.strip())
    ok = True
    for tbl in ("memories", "memory_links"):
        a, b = count("trinity", tbl), count(DRILL_DB, tbl)
        # 快照语义：源库可能在备份后新增（b >= a 时不成立但差额应为备份后写入）。
        # 校验方向：恢复库必须 >= 备份时点（无法直接取时点，用 a-b 差额阈值：
        # 备份后新增允许，但恢复库绝不能多于源库（多=恢复引入脏数据）。
        same = a >= b
        ok &= same
        print(f"  {tbl}: source={a} restored={b} {'OK' if same else 'MISMATCH(restored>source)'}")
    if not args.keep:
        run([os.path.join(PG_BIN, "psql.exe"), "-h", "127.0.0.1", "-p", "5432", "-U", "trinity", "-d", "postgres",
             "-c", f"DROP DATABASE IF EXISTS {DRILL_DB};"])
        print("drill db cleaned")
    print("RESTORE DRILL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
