# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, r"D:\trinity-code")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")
os.environ.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")
from trinity.brain.foresight_planning import foresee, plan_today
f = foresee("完成数据库迁移", 3)
print("预见:", f["horizon"], "步")
p = plan_today("完成数据库迁移", ["修复告警"])
print("今天:", [(x["action"][:20], x["reason"]) for x in p["today_plan"]])
