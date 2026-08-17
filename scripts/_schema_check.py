# -*- coding: utf-8 -*-
import sqlite3, sys
conn = sqlite3.connect(r"C:\Users\Administrator\.trinity\store\trinity_store.db")
n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
print("memories count:", n)
ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='memories'").fetchone()[0]
print("has legacy UNIQUE:", "sha256_hash TEXT UNIQUE" in ddl or "sha256_hash UNIQUE" in ddl)
idx = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memories'").fetchall()
print("indexes:", [r[0] for r in idx])
fts = conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
print("fts rows:", fts)
