# -*- coding: utf-8 -*-
"""体检修复：对存量 active 记忆回填 jieba 分词（tokenized_content）+ FTS rebuild。

背景：早期写入的记忆 tokenized_content 为空，FTS 用 unicode61 切原始中文，
中文检索命中弱。回填后 rebuild FTS 索引即生效。
"""
import sqlite3
import sys
import time

DB = r"C:\Users\Administrator\.trinity\store\trinity_store.db"
_CJK = None


def main() -> None:
    import re
    import jieba

    global _CJK
    _CJK = re.compile(r"[\u4e00-\u9fff]")
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT memory_id, content FROM memories "
        "WHERE status='active' AND (tokenized_content IS NULL OR tokenized_content='')"
    ).fetchall()
    print(f"待回填: {len(rows)} 条 active 记忆")

    n = 0
    batch = []
    for r in rows:
        content = r["content"] or ""
        if not _CJK.search(content):
            continue
        try:
            tokens = [t for t in jieba.cut(content) if t.strip()]
            tok = " ".join(tokens)
        except Exception:
            continue
        batch.append((tok, r["memory_id"]))
        if len(batch) >= 200:
            conn.executemany("UPDATE memories SET tokenized_content=? WHERE memory_id=?", batch)
            conn.commit()
            n += len(batch)
            batch = []
            print(f"  ... {n}")
    if batch:
        conn.executemany("UPDATE memories SET tokenized_content=? WHERE memory_id=?", batch)
        conn.commit()
        n += len(batch)
    print(f"回填: {n} 条")

    # FTS rebuild（外部内容表重新索引，含回填后的 tokenized_content）
    try:
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        conn.commit()
        print("FTS rebuild OK")
    except Exception as e:
        print("FTS rebuild FAIL:", e)
    conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
