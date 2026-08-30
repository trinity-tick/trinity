# -*- coding: utf-8 -*-
import sys, os, traceback
sys.path.insert(0, r"D:\trinity-code")
os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")
os.environ.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")
try:
    from trinity import Trinity
    m = Trinity(adapter="postgresql")
    r = m.search_hybrid("用户偏好 咖啡", top_k=5)
    print("hits:", len(r.get("results", [])))
    print("meta:", r.get("metacognition", {}))
except Exception:
    traceback.print_exc()
