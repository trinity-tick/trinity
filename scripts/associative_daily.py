# -*- coding: utf-8 -*-
"""联想记忆每日（EXECUTION 192）——激活扩散联想。"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    import psycopg2
    conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity", user="trinity", password="trinity")
    cur = conn.cursor()
    cur.execute("SELECT memory_id FROM memories WHERE embedding IS NOT NULL AND category NOT IN ('perception','dcpm-core') ORDER BY RANDOM() LIMIT 3")
    mids = [r[0] for r in cur.fetchall()]
    conn.close()
    from trinity.brain.associative_memory import associative_jump
    jumps = [associative_jump(m) for m in mids]
    total = sum(j.get("count", 0) for j in jumps if j.get("jumped"))
    print(json.dumps({"sources": len(mids), "associations_found": total}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
