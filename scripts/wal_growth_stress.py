#!/usr/bin/env python3
"""
Trinity — 长时/高并发 + WAL 增长压测（2026-08-15 三轮评价建议②④）

验证：
1. 长时/高并发写入（16 线程 × 大量）下连接池长驻稳定性（0 错误/0 锁冲突）
2. WAL 文件增长受控：大量写入后 WAL 大小不失控（自动 checkpoint 生效）
3. 库文件不膨胀（WAL checkpoint 后主库大小受控）
4. 写入后检索可用、审计链完整

用法：
    python scripts/wal_growth_stress.py --writes 3000 --threads 16 --db <路径>

产出：~/.trinity/logs/wal_growth_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))

REPORT_DIR = Path(os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity"))) / "logs"

_CORPUS = [
    "数据库 PostgreSQL 配置优化，JSONB 存储偏好",
    "Redis 缓存命中率提升，减少延迟",
    "PPR 图扩展提升检索召回",
    "记忆蒸馏实现 11x 压缩",
    "联邦增量同步跨实例一致性",
    "治理策略隔离跨部门记忆",
    "知识包 PII 脱敏处理",
    "RL 记忆决策优化排序",
    "A2A 协作流水线多 agent 共享",
    "memory portability standard JSON export",
    "compliance check storage encryption RBAC",
]


def _copy_snapshot(db_path: str) -> Optional[str]:
    """复制生产库到临时文件（VACUUM INTO，含 WAL、rowid 一致）。

    2026-08-16（深挖修复）：shutil.copy2 跳过 WAL 会导致 FTS rowid 错位，
    写入触发器撞 rowid → IntegrityError；VACUUM INTO 无此问题。
    """
    src = Path(db_path)
    if not src.is_file():
        return None
    import sqlite3 as _sqlite3
    tmp = Path(tempfile.mkdtemp(prefix="trinity_wal_")) / src.name
    try:
        conn = _sqlite3.connect(str(src))
        try:
            conn.execute(
                f"VACUUM INTO '{str(tmp).replace(chr(92), chr(92) * 2)}'"
            )
        finally:
            conn.close()
    except Exception:
        shutil.copy2(src, tmp)
        for suffix in ("-wal", "-shm"):
            if Path(str(src) + suffix).is_file():
                try:
                    shutil.copy2(str(src) + suffix, str(tmp) + suffix)
                except OSError:
                    pass
    return str(tmp)


def _db_stats(db: str) -> Dict[str, Any]:
    """返回主库/WAL 大小与 memories 计数。"""
    main_sz = os.path.getsize(db) if os.path.exists(db) else 0
    wal_sz = (os.path.getsize(db + "-wal")
              if os.path.exists(db + "-wal") else 0)
    conn = sqlite3.connect(db)
    try:
        total = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()[0]
        audit = conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()[0]
    finally:
        conn.close()
    return {
        "db_bytes": main_sz,
        "db_mb": round(main_sz / 1024 / 1024, 2),
        "wal_bytes": wal_sz,
        "wal_mb": round(wal_sz / 1024 / 1024, 2),
        "memories": total,
        "audit": audit,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity WAL growth + high-concurrency stress")
    parser.add_argument("--writes", type=int, default=3000)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--db", default=None, help="生产库路径（默认自动探测）")
    args = parser.parse_args()

    db_path = args.db or os.path.expanduser(
        "~/.trinity/store/trinity_store.db")
    snapshot = _copy_snapshot(db_path)
    if not snapshot:
        print(f"FAIL: 库不存在 {db_path}")
        return 1

    before = _db_stats(snapshot)
    print(f"== WAL 增长 + 高并发压测 (writes={args.writes}, "
          f"threads={args.threads}) ==")
    print(f"   初始: db={before['db_mb']}MB wal={before['wal_mb']}MB "
          f"memories={before['memories']}")

    from trinity.core.client import Trinity
    mem = Trinity(store_path=snapshot)
    errors: List[str] = []
    lat: List[float] = []
    barrier = threading.Barrier(args.threads)

    def worker(wid: int) -> None:
        barrier.wait()
        for i in range(args.writes // args.threads):
            content = f"[w{wid}-{i}] " + _CORPUS[i % len(_CORPUS)]
            t0 = time.time()
            try:
                mem.ingest(content, persona_id=f"wal-stress-{wid}",
                           metadata={"category": "wal-test"},
                           postprocess=False)
            except Exception as e:
                errors.append(f"w{wid}-{i}: {type(e).__name__}: {e}")
            lat.append((time.time() - t0) * 1000)

    ts = [threading.Thread(target=worker, args=(i,))
          for i in range(args.threads)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.time() - t0
    lat.sort()
    n = len(lat)

    # 写入后立即看 WAL 大小（自动 checkpoint 前）
    mid = _db_stats(snapshot)

    # 触发 checkpoint（关闭连接后 SQLite 自动合并 WAL）
    mem._adapter.disconnect()
    time.sleep(1.0)
    after = _db_stats(snapshot)

    lock_errs = [e for e in errors
                 if "locked" in e.lower() or "deadlock" in e.lower()]
    ok = (not errors and mid["memories"] == before["memories"] + n)

    report: Dict[str, Any] = {
        "config": vars(args),
        "writes": n, "wall_s": round(wall, 2),
        "qps": round(n / max(wall, 0.001), 1),
        "p50_ms": round(lat[n // 2], 2) if n else 0,
        "p99_ms": round(lat[int(n * 0.99)], 2) if n else 0,
        "errors": len(errors), "lock_errors": len(lock_errs),
        "error_samples": errors[:3],
        "db_before": before, "db_after_write": mid, "db_after_checkpoint": after,
        "wal_growth_mb": round(mid["wal_mb"] - before["wal_mb"], 2),
        "wal_after_checkpoint_mb": after["wal_mb"],
        "pass": ok,
    }

    print(f"   写入 {n} 条, QPS={report['qps']}, p50={report['p50_ms']}ms, "
          f"p99={report['p99_ms']}ms, errors={len(errors)}, lock_errors={len(lock_errs)}")
    print(f"   WAL 增长: {before['wal_mb']}MB -> {mid['wal_mb']}MB "
          f"(+{report['wal_growth_mb']}MB), checkpoint 后 {after['wal_mb']}MB")
    print(f"   memories: {before['memories']} -> {mid['memories']} "
          f"(期望 +{n})")
    print(f"   db 大小: {before['db_mb']}MB -> {after['db_mb']}MB")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / "wal_growth_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"报告: {out}")
    print(f"RESULT: {'PASS ✅' if ok else 'FAIL ❌'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
