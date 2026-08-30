# -*- coding: utf-8 -*-
"""好奇心驱动每日（EXECUTION 185）——好奇主题→主动搜索。"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    from trinity.brain.curiosity import compute_curiosity, curiosity_drive
    topics = compute_curiosity(top_k=3)
    drive = curiosity_drive(topics) if topics else {"searched": [], "failed": []}
    print(json.dumps({"curious_topics": [t["topic"] for t in topics],
                      "searched": drive.get("searched", [])}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
