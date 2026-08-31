# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, r"D:\trinity-code")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")
import trinity.brain.foresight_planning as fp
# 直接构造（跳过检索——验证核心逻辑）
fp.search_hybrid = None
import types
def fake_search(self, q, top_k=2):
    return [{"content": "备份先行"}, {"content": "测试后迁移"}]
fp.Trinity = type("M", (), {"search_hybrid": fake_search, "adapter": None})
f = fp.foresee("完成数据库迁移", 3)
print("预见:", f["horizon"], "步 | 证据:", len(f.get("evidence", [])))
p = fp.plan_today("完成数据库迁移", ["修复告警"])
print("今天计划:", len(p["today_plan"]), "项")
for x in p["today_plan"]:
    print(" -", x["action"][:22], "|", x["reason"])
