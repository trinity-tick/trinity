# -*- coding: utf-8 -*-
"""体检优化：存量 content_hash 回填（sha256(content)）。

策略：分批 UPDATE；撞 (persona_id, agent_id, content_hash) 唯一索引的行跳过（保持 NULL），
避免破坏去重约束。回填后 active 重复组应仍为 0。
"""
import hashlib
import sqlite3
import sys

DB = r"C:\Users\Administrator\.trinity\store\trinity_store.db"


def main() -> None:
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT memory_id, persona_id, agent_id, content FROM memories WHERE content_hash IS NULL"
    ).fetchall()
    print(f"待回填: {len(rows)} 条")

    n = 0
    skipped = 0
    for r in rows:
        content = r["content"] or ""
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()
        try:
            conn.execute(
                "UPDATE memories SET content_hash=? WHERE memory_id=? AND content_hash IS NULL",
                (h, r["memory_id"]))
            conn.commit()
            n += 1
        except sqlite3.IntegrityError:
            conn.rollback()
            skipped += 1
        if n % 500 == 0 and n:
            print(f"  ... {n} (skip {skipped})")
    print(f"回填: {n} 条 | 跳过(重复冲突): {skipped}")

    # 验证：active 重复组应=0
    dup = conn.execute("""
        SELECT COUNT(*) c FROM (
            SELECT persona_id, agent_id, content_hash FROM memories
            WHERE status='active' AND content_hash IS NOT NULL
            GROUP BY persona_id, agent_id, content_hash HAVING COUNT(*) > 1)
    """).fetchone()[0]
    print(f"active 重复组: {dup}（应 0）")
    conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
