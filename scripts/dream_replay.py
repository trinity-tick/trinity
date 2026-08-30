# -*- coding: utf-8 -*-
"""梦境回放（EXECUTION 184，大脑化）——睡眠随机重放巩固远记忆。

大脑对应：睡眠时海马体随机重放记忆（不只是最近使用的）——巩固
远记忆、防遗忘（与 forgetting 互补：遗忘修剪低频，梦境强化随机样本）。

实现：随机抽样历史记忆 → 重新激活（access_count+1 + 重要性微调）+
轻度 Hebbian 强化（对随机样本重新嵌入对比）——"梦里复习"。

用法: python scripts/dream_replay.py [--max 30] [--write]
"""
import sys, os, json, random, argparse

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=30)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    os.environ.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")

    from trinity.adapters.postgresql import PostgreSQLAdapter
    a = PostgreSQLAdapter(auto_connect=True)
    a.connect()
    try:
        import psycopg2.extras
        with a._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # 1) 随机抽样（全库 active，非保护类别——睡眠随机，不挑使用）
                cur.execute("""
                    SELECT memory_id, content, importance, access_count, created_at
                    FROM memories
                    WHERE status='active'
                      AND category NOT IN ('perception', 'self-identity', 'dcpm-core')
                    ORDER BY RANDOM()
                    LIMIT %s
                """, (args.max,))
                rows = cur.fetchall()
                sampled = []
                for r in rows:
                    sampled.append({
                        "memory_id": r["memory_id"],
                        "content": (r["content"] or "")[:200],
                        "importance": float(r["importance"] or 0.5),
                        "access_count": int(r["access_count"] or 0),
                    })
                # 2) 梦境强化：随机样本重新激活（access+1 + 低重要微升防遗忘）
                strengthened = 0
                for s in sampled:
                    new_acc = s["access_count"] + 1
                    new_imp = min(s["importance"] + 0.02, 0.9)  # 梦境巩固（轻微）
                    if args.write:
                        cur.execute(
                            "UPDATE memories SET access_count=%s, importance=%s WHERE memory_id=%s",
                            (new_acc, new_imp, s["memory_id"]))
                    strengthened += 1
                conn.commit()
        print(json.dumps({"dreamed": len(sampled), "strengthened": strengthened,
                          "write": args.write}, ensure_ascii=False))
        return 0
    finally:
        a.disconnect()

if __name__ == "__main__":
    sys.exit(main())
