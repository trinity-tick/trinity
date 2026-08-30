# -*- coding: utf-8 -*-
"""情绪记忆巩固每日（EXECUTION 189）——杏仁核效应。"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    from trinity.brain.emotional_consolidation import emotional_consolidate, protect_emotional_from_forgetting
    r = emotional_consolidate(limit=100)
    p = protect_emotional_from_forgetting(limit=100)
    print(json.dumps({"emotional": r["emotional"], "protected": p["protected"]}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
