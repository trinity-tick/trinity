import sqlite3

db = r"C:\Users\Administrator\.trinity\store\trinity_store.db"
c = sqlite3.connect(db)

n_total = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
n_active = c.execute("SELECT COUNT(*) FROM memories WHERE status != 'deleted'").fetchone()[0]
print(f"memories 总数: {n_total}, 非删除: {n_active}")

print("\n最近 8 条:")
for r in c.execute(
    "SELECT memory_id, category, substr(content,1,45), created_at "
    "FROM memories WHERE status != 'deleted' ORDER BY created_at DESC LIMIT 8"
).fetchall():
    print(f"  {r[0][:14]} | {r[1]:<14} | {r[2]} | {r[3]}")

print("\n按 category 分布:")
for r in c.execute(
    "SELECT category, COUNT(*) FROM memories WHERE status != 'deleted' GROUP BY category ORDER BY 2 DESC LIMIT 10"
).fetchall():
    print(f"  {r[0]}: {r[1]}")

print("\n按来源 agent 分布 (top8):")
try:
    for r in c.execute(
        "SELECT agent_id, COUNT(*) FROM memories WHERE status != 'deleted' AND agent_id IS NOT NULL GROUP BY agent_id ORDER BY 2 DESC LIMIT 8"
    ).fetchall():
        print(f"  {r[0]}: {r[1]}")
except Exception as e:
    print(f"  (agent_id 查询失败: {e})")

c.close()
