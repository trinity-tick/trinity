#!/usr/bin/env python3
"""
OPT2: PG FTS GIN 索引对比基准（10K 规模）
==========================================
在 trinity DB（docker :5430）上：
  1) 生成 10K BEAM 标签测试记忆（复用 beam_data_generator）
  2) 无索引跑 50 查询 → 记录延迟
  3) CREATE INDEX ... GIN (to_tsvector('simple', content))
  4) 有索引重跑 → 记录延迟
  5) 输出对比表；删除测试数据（索引保留，供生产 FTS 查询使用）

用法:
    python benchmark/beam_gin_index.py [--scale 10000] [--queries 50]
"""

import argparse
import os
import sys
import time
from pathlib import Path

TRINITY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRINITY_ROOT))

# 环境指向 docker trinity-db
os.environ.setdefault("PGHOST", "127.0.0.1")
os.environ.setdefault("PGPORT", "5430")
os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")
os.environ.setdefault("PGDATABASE", "trinity")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", type=int, default=10000)
    parser.add_argument("--queries", type=int, default=50)
    args = parser.parse_args()

    from scripts.beam_data_generator import (
        get_pg_config, connect_pg, ensure_benchmark_table,
        generate_data, clean_test_data,
    )
    from scripts.beam_benchmark import QUERY_SET, _build_or_tsquery, connect_pg as _unused  # noqa

    conn = connect_pg()
    print(f"PG: {get_pg_config()['host']}:{get_pg_config()['port']}/{get_pg_config()['dbname']}")

    print(f"[1/5] generating {args.scale} test memories ...")
    ensure_benchmark_table(conn)
    generate_data(args.scale, conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memories WHERE tags::text LIKE '%BEAM%'")
        print(f"  BEAM rows: {cur.fetchone()[0]}")

    queries = QUERY_SET[: args.queries]

    def run_timed(label: str) -> dict:
        lat = []
        hits = 0
        total = 0
        for topic, q in queries:
            t0 = time.perf_counter()
            with conn.cursor() as cur:
                tsq = _build_or_tsquery(q)
                cur.execute(
                    """SELECT memory_id, content FROM memories
                       WHERE to_tsvector('simple', content) @@ to_tsquery('simple', %s)
                       ORDER BY ts_rank(to_tsvector('simple', content), to_tsquery('simple', %s)) DESC
                       LIMIT 5""",
                    (tsq, tsq),
                )
                rows = cur.fetchall()
            lat.append((time.perf_counter() - t0) * 1000)
            total += 1
            if any(f"[{topic}:" in (r[1] or "") for r in rows):
                hits += 1
        lat.sort()
        n = len(lat)
        return {
            "label": label,
            "p50": round(lat[n // 2], 2),
            "p95": round(lat[int(n * 0.95) - 1], 2),
            "p99": round(lat[int(n * 0.99) - 1], 2),
            "mean": round(sum(lat) / n, 2),
            "qps": round(n / (sum(lat) / 1000), 2),
            "recall@5": round(hits / total, 3),
        }

    print("[2/5] benchmark WITHOUT index ...")
    before = run_timed("no-index")

    print("[3/5] creating GIN index ...")
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS idx_memories_content_gin")
        cur.execute(
            "CREATE INDEX idx_memories_content_gin ON memories USING GIN (to_tsvector('simple', content))"
        )
    conn.commit()
    print(f"  index created in {time.time() - t0:.1f}s")

    print("[4/5] benchmark WITH index ...")
    after = run_timed("gin-index")

    print("[5/5] cleanup test data (index kept) ...")
    clean_test_data(conn)

    print("\n" + "=" * 70)
    print(f"  BEAM GIN Index Comparison — scale={args.scale} queries={len(queries)}")
    print(f"  {'config':<12}{'P50(ms)':<10}{'P95(ms)':<10}{'P99(ms)':<10}{'QPS':<8}{'Recall@5':<10}")
    for r in (before, after):
        print(f"  {r['label']:<12}{r['p50']:<10}{r['p95']:<10}{r['p99']:<10}{r['qps']:<8}{r['recall@5']:<10}")
    speedup = before["p50"] / after["p50"] if after["p50"] else float("inf")
    print(f"  P50 speedup: {speedup:.1f}x")
    print("=" * 70)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
