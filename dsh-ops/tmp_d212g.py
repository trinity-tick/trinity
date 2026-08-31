# -*- coding: utf-8 -*-
print("START", flush=True)
import sys, os
sys.path.insert(0, r"D:\trinity-code")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")
import psycopg2
c = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity", user="trinity", password="trinity")
cur = c.cursor()
cur.execute("DELETE FROM memories WHERE memory_id LIKE 'dream-t-%'")
c.commit()
print("deleted", flush=True)
cur.execute("SELECT count(*) FROM memories WHERE status='active' AND content NOT LIKE 'enc:%' AND category NOT IN ('perception','self-identity','dcpm-core')")
print("active plain:", cur.fetchone()[0], flush=True)
c.close()
print("DONE", flush=True)
