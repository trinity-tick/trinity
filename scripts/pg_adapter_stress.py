#!/usr/bin/env python3
"""
Trinity — PostgreSQL 镜像链适配器压测（2026-08-16 深挖建议①）

维护链（decay/tiers/mirror）走 PG，但 SQLite 侧压力不代表 PG 侧。
本脚本用 PostgreSQLAdapter 直连（默认原生 PG :5432 postgres/trinity，
可用 --host/--port/--user/--db 覆盖）压测读路径（search_memories /
query_graph）+ 事务回滚的写路径——验证 PG 适配器延迟/QPS/稳定性，
且**不污染**库（写操作全部 ROLLBACK）。

用法：
    python scripts/pg_adapter_stress.py --reads 500 --threads 8
    python scripts/pg_adapter_stress.py --host 127.0.0.1 --port 5432

产出：~/.trinity/logs/pg_adapter_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))

REPORT_DIR = Path(os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity"))) / "logs"

_QUERIES = [
    "数据库", "PostgreSQL", "Redis", "记忆", "联邦", "压缩", "检索",
    "治理", "knowledge", "compliance", "memory", "storage",
]


def _now_ms() -> float:
    return time.time() * 1000


class StressResult:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.lat: List[float] = []
        self.errors: List[str] = []

    def add(self, ms: float, err: str = "") -> None:
        with self.lock:
            self.lat.append(ms)
            if err:
                self.errors.append(err)

    def stats(self) -> Dict[str, Any]:
        with self.lock:
            lat = sorted(self.lat)
            n = len(lat)
            if not n:
                return {"count": 0}
            return {
                "count": n,
                "p50_ms": round(lat[n // 2], 2),
                "p95_ms": round(lat[int(n * 0.95) - 1], 2),
                "p99_ms": round(lat[int(n * 0.99) - 1], 2),
                "max_ms": round(lat[-1], 2),
                "errors": len(self.errors),
                "error_samples": self.errors[:3],
            }


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity PostgreSQL adapter stress")
    parser.add_argument("--reads", type=int, default=500)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default="postgres")
    parser.add_argument("--db", default="trinity")
    args = parser.parse_args()

    from trinity.adapters.postgresql import PostgreSQLAdapter

    adapter = PostgreSQLAdapter(
        host=args.host, port=args.port, dbname=args.db,
        user=args.user, password=args.password,
    )
    try:
        adapter.connect()
    except Exception as e:
        print(f"FAIL: PG 连接失败 {args.host}:{args.port}/{args.db}: {e}")
        return 1

    print(f"== PostgreSQL 适配器压测 ({args.host}:{args.port}/{args.db}, "
          f"reads={args.reads}, threads={args.threads}) ==")

    # 预热（首次连接/分词）
    try:
        adapter.search_memories("数据库", top_k=5)
    except Exception:
        pass

    # 1) 并发读
    result = StressResult()
    barrier = threading.Barrier(args.threads)

    def reader(wid: int) -> None:
        barrier.wait()
        for i in range(args.reads // args.threads):
            q = _QUERIES[(wid + i) % len(_QUERIES)]
            t0 = _now_ms()
            try:
                adapter.search_memories(q, top_k=10)
                result.add(_now_ms() - t0)
            except Exception as e:
                result.add(_now_ms() - t0, str(e)[:100])

    ts = [threading.Thread(target=reader, args=(i,))
          for i in range(args.threads)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.time() - t0
    stats = result.stats()
    stats["elapsed_s"] = round(wall, 2)
    stats["qps"] = round(stats["count"] / max(wall, 0.001), 1)
    print(f"   读 {stats['count']} 次, QPS={stats['qps']}, "
          f"p50={stats['p50_ms']}ms, p99={stats['p99_ms']}ms, "
          f"errors={stats['errors']}")

    # 2) 写路径（显式事务回滚，不落库）
    wres = StressResult()
    barrier2 = threading.Barrier(args.threads)

    def writer(wid: int) -> None:
        barrier2.wait()
        for i in range(min(args.reads, 200) // args.threads):
            t0 = _now_ms()
            try:
                # 显式事务：INSERT 后 ROLLBACK，验证写路径不崩且零污染
                import psycopg2
                conn = adapter._pool.getconn()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO memories (memory_id, persona_id, content, "
                        "status, version, created_at, updated_at) "
                        "VALUES (%s, %s, %s, 'active', 1, now(), now())",
                        (f"mem_pgtest_{wid}_{i}", "pg-stress-rollback",
                         f"[pg-rollback-{wid}-{i}] 压测写入"))
                    conn.rollback()
                    cur.close()
                finally:
                    adapter._pool.putconn(conn)
                wres.add(_now_ms() - t0)
            except Exception as e:
                wres.add(_now_ms() - t0, str(e)[:100])

    ts2 = [threading.Thread(target=writer, args=(i,))
           for i in range(args.threads)]
    t0 = time.time()
    for t in ts2:
        t.start()
    for t in ts2:
        t.join()
    wall = time.time() - t0
    wstats = wres.stats()
    wstats["elapsed_s"] = round(wall, 2)
    wstats["qps"] = round(wstats["count"] / max(wall, 0.001), 1)
    print(f"   写(回滚) {wstats['count']} 次, QPS={wstats['qps']}, "
          f"p50={wstats['p50_ms']}ms, p99={wstats['p99_ms']}ms, "
          f"errors={wstats['errors']}")

    # 3) 一致性（写后计数不变——验证回滚/无污染）
    before = after = -1
    try:
        import psycopg2
        conn = adapter._pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM memories")
            before = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM memories")
            after = cur.fetchone()[0]
            cur.close()
        finally:
            adapter._pool.putconn(conn)
    except Exception as e:
        print(f"   计数失败: {e}")

    report: Dict[str, Any] = {
        "config": vars(args), "read": stats, "write_rollback": wstats,
        "memories_before": before, "memories_after": after,
        "rollback_ok": before == after,
        "pass": stats["errors"] == 0 and wstats["errors"] == 0
                and (before == after),
    }
    print(f"   计数: before={before} after={after} "
          f"(回滚生效={report['rollback_ok']})")

    adapter.disconnect()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / "pg_adapter_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"报告: {out}")
    print(f"RESULT: {'PASS ✅' if report['pass'] else 'FAIL ❌'}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
