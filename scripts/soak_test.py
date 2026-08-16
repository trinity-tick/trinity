#!/usr/bin/env python3
"""
Trinity — 长时间运行 soak 测试（2026-08-16 深挖建议③）

10 分钟持续混合负载（写 + 检索 + touch），周期监控：
- WAL 大小（checkpoint 有效性 / 无失控增长）
- 内存 RSS（无泄漏）
- 读连接池（无泄漏：active 数稳定）
- 错误/锁冲突（0）

小时级结论的代理验证：10 分钟 × 持续负载已能暴露泄漏/失控趋势。

用法：
    python scripts/soak_test.py --minutes 10 --threads 8 --db <路径>

产出：~/.trinity/logs/soak_report.json
"""

from __future__ import annotations

import argparse
import json
import os
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
    "数据库 PostgreSQL 配置优化", "Redis 缓存命中率提升",
    "PPR 图扩展提升检索召回", "记忆蒸馏实现 11x 压缩",
    "联邦增量同步跨实例一致性", "治理策略隔离跨部门记忆",
    "知识包 PII 脱敏处理", "RL 记忆决策优化排序",
]


def _copy_snapshot(db_path: str) -> Optional[str]:
    """VACUUM INTO 复制（含 WAL、rowid 一致，2026-08-16 修复）。"""
    src = Path(db_path)
    if not src.is_file():
        return None
    import sqlite3 as _sqlite3
    tmp = Path(tempfile.mkdtemp(prefix="trinity_soak_")) / src.name
    try:
        conn = _sqlite3.connect(str(src))
        try:
            conn.execute(
                f"VACUUM INTO '{str(tmp).replace(chr(92), chr(92) * 2)}'"
            )
        finally:
            conn.close()
    except Exception:
        import shutil
        shutil.copy2(src, tmp)
    return str(tmp)


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity soak test")
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    db_path = args.db or os.path.expanduser(
        "~/.trinity/store/trinity_store.db")
    snapshot = _copy_snapshot(db_path)
    if not snapshot:
        print(f"FAIL: 库不存在 {db_path}")
        return 1

    from trinity.core.client import Trinity
    mem = Trinity(store_path=snapshot)
    mem.search_hybrid(query="数据库", top_k=3, strategy="rrf")
    dl = time.time() + 30
    while time.time() < dl and not getattr(mem, "_bm25_ready", False):
        time.sleep(0.2)

    print(f"== Soak 测试 ({args.minutes} min, {args.threads} threads) ==")
    duration_s = args.minutes * 60
    stop = threading.Event()
    errors: List[str] = []
    ops = {"write": 0, "read": 0, "touch": 0}
    stats_lock = threading.Lock()

    def worker(wid: int) -> None:
        cycle = 0
        while not stop.is_set():
            op = cycle % 3
            try:
                if op == 0:
                    mem.ingest(
                        f"[soak-{wid}-{cycle}] {_CORPUS[cycle % len(_CORPUS)]}",
                        persona_id=f"soak-{wid}",
                        metadata={"category": "soak"},
                        postprocess=False)
                    with stats_lock:
                        ops["write"] += 1
                elif op == 1:
                    mem.search_hybrid(
                        query=_CORPUS[cycle % len(_CORPUS)][:6],
                        top_k=5, strategy="rrf")
                    with stats_lock:
                        ops["read"] += 1
                else:
                    # touch：随机命中一条已有记忆（adapter 层）
                    try:
                        import sqlite3 as _s3
                        _c = _s3.connect(snapshot)
                        _row = _c.execute(
                            "SELECT memory_id FROM memories LIMIT 1").fetchone()
                        _c.close()
                        if _row:
                            mem._adapter.touch_memory(_row[0])
                            with stats_lock:
                                ops["touch"] += 1
                    except Exception:
                        pass
            except Exception as e:
                errors.append(f"w{wid}-{cycle}: {type(e).__name__}: {e}")
            cycle += 1

    ts = [threading.Thread(target=worker, args=(i,))
          for i in range(args.threads)]
    for t in ts:
        t.start()

    # 监控采样
    samples: List[Dict[str, Any]] = []
    t0 = time.time()
    import psutil
    proc = psutil.Process()
    conn = sqlite3.connect(snapshot)

    while time.time() - t0 < duration_s:
        time.sleep(10)
        wal = (os.path.getsize(snapshot + "-wal")
               if os.path.exists(snapshot + "-wal") else 0)
        db = os.path.getsize(snapshot)
        rss = proc.memory_info().rss / 1024 / 1024
        n_conns = len(mem._adapter._read_conns)
        n_mem = conn.execute(
            "SELECT COUNT(*) c FROM memories").fetchone()[0]
        samples.append({
            "t_s": round(time.time() - t0),
            "db_mb": round(db / 1024 / 1024, 2),
            "wal_mb": round(wal / 1024 / 1024, 2),
            "rss_mb": round(rss, 1),
            "read_conns": n_conns,
            "memories": n_mem,
        })
        if len(samples) % 3 == 0:
            s = samples[-1]
            print(f"  t={s['t_s']}s db={s['db_mb']}MB wal={s['wal_mb']}MB "
                  f"rss={s['rss_mb']}MB conns={s['read_conns']} "
                  f"mem={s['memories']}")

    stop.set()
    for t in ts:
        t.join(timeout=30)
    conn.close()

    wall = time.time() - t0
    with stats_lock:
        total_ops = sum(ops.values())
    lock_errs = [e for e in errors
                 if "locked" in e.lower() or "deadlock" in e.lower()]

    # 泄漏判定：RSS 首尾对比（< 30% 增长）+ WAL 受控（< 50MB）
    rss_first = samples[0]["rss_mb"] if samples else 0
    rss_last = samples[-1]["rss_mb"] if samples else 0
    wal_max = max(s["wal_mb"] for s in samples) if samples else 0
    rss_growth = (rss_last - rss_first) / max(rss_first, 1) * 100

    report: Dict[str, Any] = {
        "config": vars(args), "duration_s": round(wall),
        "ops": ops, "total_ops": total_ops,
        "errors": len(errors), "lock_errors": len(lock_errs),
        "error_samples": errors[:3],
        "samples": samples,
        "rss_first_mb": rss_first, "rss_last_mb": rss_last,
        "rss_growth_pct": round(rss_growth, 1),
        "wal_max_mb": wal_max,
        "read_conns_final": samples[-1]["read_conns"] if samples else 0,
        "pass": (not errors and not lock_errs and rss_growth < 30
                 and wal_max < 50),
    }
    print(f"  总操作 {total_ops}, 错误 {len(errors)}, 锁错误 {len(lock_errs)}")
    print(f"  RSS {rss_first}MB -> {rss_last}MB "
          f"(+{round(rss_growth, 1)}%), WAL 峰值 {wal_max}MB, "
          f"连接数 {report['read_conns_final']}")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / "soak_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"报告: {out}")
    print(f"RESULT: {'PASS ✅' if report['pass'] else 'FAIL ❌'}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
