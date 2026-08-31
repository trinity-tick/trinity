# -*- coding: utf-8 -*-
"""主动发起每日（EXECUTION 213）——内部状态驱动自主行动。"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    from trinity.brain.proactive_initiative import collect_initiatives
    r = collect_initiatives()
    print(json.dumps({"reasons": r["count"], "score": r["initiative_score"],
                      "actions": [x["action"] for x in r["reasons"]]}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
