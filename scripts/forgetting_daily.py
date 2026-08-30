# -*- coding: utf-8 -*-
"""主动遗忘每日驱动（EXECUTION 183）——修剪低价值未用记忆。"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    from trinity.adapters.postgresql import PostgreSQLAdapter
    a = PostgreSQLAdapter(auto_connect=True); a.connect()
    try:
        cands = a.forget_candidates(limit=50, min_age_days=30)
        res = a.apply_forgetting(cands, dry_run=False)
        print(json.dumps({"candidates": len(cands), **res}, ensure_ascii=False))
        return 0
    finally:
        a.disconnect()

if __name__ == "__main__":
    sys.exit(main())
