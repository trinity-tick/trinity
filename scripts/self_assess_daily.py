# -*- coding: utf-8 -*-
"""自我评估每日（EXECUTION 186）——真实指标评估写入记忆。"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    from trinity.brain.self_assessment import assess_recent, assess_to_memory
    r = assess_recent()
    ok = assess_to_memory()
    print(json.dumps({"assessment": r["assessment"][:100], "memory": ok}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
