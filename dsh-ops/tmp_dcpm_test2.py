# -*- coding: utf-8 -*-
import sys, time, os
sys.path.insert(0, r"D:\trinity-code")
os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")
os.environ.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")
from trinity import Trinity
m = Trinity(adapter="postgresql")
r = m.search_hybrid("用户偏好 咖啡", top_k=5)
print("hits:", len(r.get("results", [])))
print("meta:", r.get("metacognition", {}))
eng = m.dcpm
if eng:
    print("beliefs:", len(eng["system1"]._beliefs))
