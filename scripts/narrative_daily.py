# -*- coding: utf-8 -*-
"""自传体叙事每日（EXECUTION 190）——我的故事更新。"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    from trinity.brain.autobiographical import build_narrative, narrative_to_memory
    r = build_narrative()
    ok = narrative_to_memory()
    print(json.dumps({"chapters": r["chapters"], "memory": ok}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
