# -*- coding: utf-8 -*-
"""中文 tsvector 回填（EXECUTION 109，pg_jieba 方案 C 应用层落地）。

jieba 分词 → to_tsvector('simple', '词1 词2 ...') → 写入 memories.content_tsv_zh。
幂等：只处理 content_tsv_zh IS NULL 的行；断点续传。
用法: python scripts/backfill_tsv_zh.py [--batch 500]
"""
import sys, os, time, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PGHOST", "127.0.0.1")
os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity")
os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=500)
    args = ap.parse_args()
    import jieba, psycopg2, psycopg2.extras

    jieba.setLogLevel(60)  # quiet
    conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                            user="trinity", password="trinity")
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM memories WHERE content_tsv_zh IS NULL")
    todo = cur.fetchone()[0]
    print(f"missing tsv_zh: {todo}")
    if todo == 0:
        return 0
    done = 0
    t0 = time.time()
    while True:
        cur.execute("SELECT memory_id, content FROM memories WHERE content_tsv_zh IS NULL ORDER BY created_at LIMIT %s",
                    (args.batch,))
        rows = cur.fetchall()
        if not rows:
            break
        for mid, content in rows:
            if not content:
                cur.execute("UPDATE memories SET content_tsv_zh = ''::tsvector WHERE memory_id = %s", (mid,))
                continue
            words = [w.strip() for w in jieba.cut(content) if w.strip() and len(w.strip()) <= 40]
            if not words:
                cur.execute("UPDATE memories SET content_tsv_zh = ''::tsvector WHERE memory_id = %s", (mid,))
                continue
            tsv = " ".join(words)
            cur.execute(
                "UPDATE memories SET content_tsv_zh = to_tsvector('simple', %s) WHERE memory_id = %s",
                (tsv, mid))
        conn.commit()
        done += len(rows)
        rate = done / max(time.time() - t0, 0.001)
        print(f"batch {len(rows)} | done={done}/{todo} | {rate:.0f}/s | eta {(todo-done)/max(rate,0.1)/60:.0f}min", flush=True)
    conn.close()
    print(f"TSV_ZH BACKFILL DONE: {done}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
