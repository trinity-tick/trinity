#!/usr/bin/env python3
"""
Trinity — 多进程写并发压测（2026-08-15 二轮评价建议①）

模拟生产拓扑：API 进程 + collector 进程（+可选 worker）同时打开同一
SQLite 库（WAL），各自多线程并发写（store_memory + 审计链），验证新连接池
架构（每线程只读连接 + 主写连接 + _write_lock）在**多进程**下无
database is locked / 无锁错误 / 审计链一致。--procs 3 时含 worker 进程。

用法：
    python scripts/multi_process_stress.py --writes 300 --threads 4
    python scripts/multi_process_stress.py --db <生产库路径>

产出：~/.trinity/logs/multi_process_stress_report.json + 控制台摘要
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))

REPORT_DIR = Path(os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity"))) / "logs"

# worker 子进程代码（多进程共享同一副本库；各自独立 Trinity 实例）
_WORKER_CODE = r"""
import json, random, sys, threading, time
sys.path.insert(0, __ROOT__)
from trinity.core.client import Trinity

ROLE = sys.argv[1]
DB = sys.argv[2]
WRITES = int(sys.argv[3])
THREADS = int(sys.argv[4])

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

mem = Trinity(store_path=DB)
errors = []
lat = []
barrier = threading.Barrier(THREADS)

def worker(wid):
    barrier.wait()
    for i in range(WRITES // THREADS):
        content = f"[{ROLE}-{wid}-{i}] " + random.choice(_CORPUS)
        t0 = time.time()
        try:
            mem.ingest(content, persona_id=f"stress-{ROLE}",
                       metadata={"category": "mixed",
                                 "source": "multi-proc-stress"},
                       postprocess=False)
        except Exception as e:
            errors.append(f"w{wid}-{i}: {type(e).__name__}: {e}")
        lat.append((time.time() - t0) * 1000)

ts = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
t0 = time.time()
for t in ts: t.start()
for t in ts: t.join()
wall = time.time() - t0
lat.sort()
n = len(lat)
print(json.dumps({
    "role": ROLE, "count": n, "wall_s": round(wall, 2),
    "qps": round(n / max(wall, 0.001), 1),
    "p50_ms": round(lat[n // 2], 2) if n else 0,
    "p99_ms": round(lat[int(n * 0.99)], 2) if n else 0,
    "errors": errors,
}, ensure_ascii=False))
"""


def copy_db_snapshot(db_path: str) -> Optional[str]:
    """复制生产库到临时文件（VACUUM INTO，含 WAL 全部内容、rowid 一致）。

    2026-08-16（深挖修复）：原 shutil.copy2 跳过 WAL → FTS rowid 错位 →
    写入触发器撞 rowid → IntegrityError；VACUUM INTO 无此问题且免疫
    WinError 33 锁。
    """
    src = Path(db_path)
    if not src.is_file():
        return None
    import sqlite3 as _sqlite3
    tmp = Path(tempfile.mkdtemp(prefix="trinity_mpstress_")) / src.name
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity multi-process write stress")
    parser.add_argument("--writes", type=int, default=300)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--procs", type=int, default=2,
                        help="并发进程数（默认 2：api+collector；3：+worker）")
    parser.add_argument("--db", default=None, help="生产库路径（默认自动探测）")
    args = parser.parse_args()

    roles = ["api", "collector", "worker"][: max(1, min(args.procs, 3))]

    db_path = args.db or os.path.expanduser(
        "~/.trinity/store/trinity_store.db")
    snapshot = copy_db_snapshot(db_path)
    if not snapshot:
        print(f"FAIL: 库不存在 {db_path}")
        return 1

    print(f"== 多进程写并发压测 (db={db_path}, writes={args.writes}, "
          f"threads={args.threads}, procs={len(roles)}: {','.join(roles)}) ==")
    print(f"   副本: {snapshot}")

    worker_file = os.path.join(tempfile.mkdtemp(prefix="mpw_"), "worker.py")
    with open(worker_file, "w", encoding="utf-8") as f:
        f.write(_WORKER_CODE.replace("__ROOT__", repr(str(_TRINITY_ROOT))))

    python = sys.executable
    procs: List[subprocess.Popen] = []
    for role in roles:
        p = subprocess.Popen(
            [python, worker_file, role, snapshot,
             str(args.writes), str(args.threads)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        procs.append(p)

    results: List[Dict[str, Any]] = []
    for p in procs:
        out, err = p.communicate(timeout=600)
        for line in out.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                results.append(json.loads(line))
        if err.strip():
            tail = [l for l in err.strip().splitlines()
                    if "Traceback" in l or "Error" in l][-2:]
            if tail:
                print(f"   stderr: {tail}")

    # 一致性校验（双进程写后）：复用 full_stress_test.consistency_check
    sys.path.insert(0, str(_TRINITY_ROOT / "scripts"))
    from full_stress_test import consistency_check
    cons = consistency_check(snapshot)

    report: Dict[str, Any] = {
        "config": vars(args), "db_snapshot": snapshot,
        "processes": results, "consistency": cons,
    }
    all_errors = [e for r in results for e in r.get("errors", [])]
    lock_errors = [e for e in all_errors
                   if "locked" in e.lower() or "deadlock" in e.lower()]
    ok = (len(results) == len(roles) and not all_errors and not lock_errors
          and cons.get("memories_total", 0) > 0)

    for r in results:
        print(f"   [{r['role']}] {r['count']} 条, QPS={r['qps']}, "
              f"p50={r['p50_ms']}ms, p99={r['p99_ms']}ms, "
              f"errors={len(r['errors'])}")
    print(f"   一致性: memories={cons.get('memories_total')}, "
          f"audit={cons.get('audit_entries')}, "
          f"lock_errors={len(lock_errors)}")
    report["pass"] = ok

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / "multi_process_stress_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"报告: {out}")
    print(f"RESULT: {'PASS ✅' if ok else 'FAIL ❌'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
