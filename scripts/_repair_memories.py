# -*- coding: utf-8 -*-
"""紧急修复：memories 表迁移失败后数据在 memories_legacy。
步骤: DROP 空 memories → 把 legacy 改回 memories → 验证行数。
"""
import sqlite3

DB = r"C:\Users\Administrator\.trinity\store\trinity_store.db"
conn = sqlite3.connect(DB, timeout=30)
cur = conn.cursor()

tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("tables:", tables)

if "memories_legacy" in tables:
    n_legacy = cur.execute("SELECT COUNT(*) FROM memories_legacy").fetchone()[0]
    n_mem = cur.execute("SELECT COUNT(*) FROM memories").fetchone()[0] if "memories" in tables else -1
    print(f"memories={n_mem}  memories_legacy={n_legacy}")
    if n_legacy > n_mem:
        print(">> 执行修复: DROP memories, legacy RENAME -> memories")
        cur.execute("DROP TABLE IF EXISTS memories")
        cur.execute("ALTER TABLE memories_legacy RENAME TO memories")
        conn.commit()
        n = cur.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        print(f">> 修复后 memories={n}")
    else:
        print(">> legacy 不占优，跳过（避免覆盖）")
else:
    print(">> 无 memories_legacy，无需修复")
    print("memories count:", cur.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
conn.close()
