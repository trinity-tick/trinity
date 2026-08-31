# -*- coding: utf-8 -*-
"""记忆管理每日（EXECUTION 205）——短期→长期升级+巩固。"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    from trinity.brain.memory_manager import promote_working_memory, stabilize, memory_report
    p = promote_working_memory()
    s = stabilize()
    rep = memory_report()
    print(json.dumps({"promoted": p.get("promoted", 0), "stabilized": s.get("stabilized", 0),
                      "short_to_long": rep.get("short_to_long_ratio")}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
