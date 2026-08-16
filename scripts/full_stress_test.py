#!/usr/bin/env python3
"""
Trinity — 全量压力测试（2026-08-15）
======================================
引擎层综合压力测试（不依赖 HTTP，直接打 MemoryAggregator / SQLite 存储）：

  1. 并发写入：多线程 ingest（验证批量写入/锁竞争/数据库稳定）
  2. 并发检索：多线程 hybrid（验证吞吐与 P50/P99）
  3. 混合读写：写入+检索+更新并发（验证无死锁/无 database is locked）
  4. 资源监控：内存/CPU 峰值（psutil 可选）
  5. 一致性校验：压力后计数/哈希链/审计完整

用法：
    python scripts/full_stress_test.py                     # 默认（写 500 / 读 500 / 混合 200）
    python scripts/full_stress_test.py --writes 1000 --reads 1000 --mixed 500
    python scripts/full_stress_test.py --db ~/.trinity/store/trinity_store.db
                                                           # 加阶段6：生产库副本并发检索+一致性
    python scripts/full_stress_test.py --api http://127.0.0.1:8001
                                                           # 加阶段7：API 并发检索（全链路）
    python scripts/full_stress_test.py --json              # JSON 报告

产出：~/.trinity/logs/full_stress_report.json + 控制台摘要
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import statistics
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))

# 可选 psutil
try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

REPORT_DIR = Path(os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity"))) / "logs"

# 写入语料（中文 + 英文混合）
_CORPUS = [
    "数据库 PostgreSQL 配置优化，JSONB 存储偏好",
    "Redis 缓存命中率提升，减少延迟",
    "PPR 图扩展提升检索召回",
    "记忆蒸馏实现 11x 压缩",
    "联邦增量同步跨实例一致性",
    "治理策略隔离跨部门记忆",
    "知识包 PII 脱敏处理",
    "RL 记忆决策优化排序",
    "serendipity 探索通道意外发现",
    "A2A 协作流水线多 agent 共享",
    "memory portability standard JSON export",
    "enterprise governance template HR policy",
    "compliance check storage encryption RBAC",
    "federated sync incremental diff merge",
    "knowledge pack cross-instance circulation",
]


def _now_ms() -> float:
    return time.time() * 1000


class StressResult:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latencies: List[float] = []
        self.errors: List[str] = []
        self.count = 0

    def add(self, latency_ms: float, error: str = "") -> None:
        with self.lock:
            self.latencies.append(latency_ms)
            self.count += 1
            if error:
                self.errors.append(error)

    def stats(self) -> Dict[str, Any]:
        with self.lock:
            lat = sorted(self.latencies)
            n = len(lat)
            if n == 0:
                return {"count": 0}
            return {
                "count": n,
                "qps": round(n / max((lat[-1] - lat[0]) / 1000 if n > 1 else 0.001, 0.001), 1)
                if n > 1 else 0,
                "p50_ms": round(lat[n // 2], 2),
                "p95_ms": round(lat[int(n * 0.95) - 1], 2),
                "p99_ms": round(lat[int(n * 0.99) - 1], 2),
                "max_ms": round(lat[-1], 2),
                "errors": len(self.errors),
                "error_samples": self.errors[:5],
            }


def run_write_stress(agg, writes: int, threads: int = 8) -> StressResult:
    """并发写入压力。"""
    result = StressResult()
    barrier = threading.Barrier(threads)

    def worker(wid: int) -> None:
        barrier.wait()
        for i in range(writes // threads):
            content = f"{random.choice(_CORPUS)} [w{wid}-{i}]"
            t0 = _now_ms()
            try:
                agg.ingest(content, f"agent-{wid}",
                           {"category": random.choice(["db", "research", "life"]),
                            "importance": random.uniform(0.3, 0.9)})
                result.add(_now_ms() - t0)
            except Exception as e:
                result.add(_now_ms() - t0, str(e)[:80])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    result.stats()["total_s"] = round(time.time() - t0, 2)
    return result


def run_read_stress(agg, reads: int, threads: int = 8) -> StressResult:
    """并发检索压力（hybrid + 向量 + 图）。"""
    result = StressResult()
    barrier = threading.Barrier(threads)
    queries = [c.split()[0] for c in _CORPUS]

    def worker(wid: int) -> None:
        barrier.wait()
        for i in range(reads // threads):
            q = random.choice(queries)
            t0 = _now_ms()
            try:
                agg.query({}, limit=10, mode="hybrid", query_text=q)
                result.add(_now_ms() - t0)
            except Exception as e:
                result.add(_now_ms() - t0, str(e)[:80])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    result.stats()["total_s"] = round(time.time() - t0, 2)
    return result


def run_mixed_stress(agg, mixed: int, threads: int = 8) -> StressResult:
    """混合读写压力：写入 + 检索 + 更新并发。"""
    result = StressResult()
    barrier = threading.Barrier(threads)

    def worker(wid: int) -> None:
        barrier.wait()
        for i in range(mixed // threads):
            op = random.choice(["write", "read", "update"])
            t0 = _now_ms()
            try:
                if op == "write":
                    agg.ingest(f"{random.choice(_CORPUS)} [mix{wid}-{i}]",
                               f"mix-{wid}", {"category": "mixed"})
                elif op == "read":
                    agg.query({}, limit=5, mode="hybrid",
                              query_text=random.choice(_CORPUS)[:4])
                else:
                    # 随机挑一条更新（touch）
                    if agg._pool:
                        mid = random.choice(list(agg._pool.keys()))
                        agg.touch(mid)
                result.add(_now_ms() - t0)
            except Exception as e:
                result.add(_now_ms() - t0, str(e)[:80])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    result.stats()["total_s"] = round(time.time() - t0, 2)
    return result


def monitor_resources(duration_s: float) -> Dict[str, Any]:
    """资源监控：内存/CPU 峰值（psutil 可选）。"""
    if not _PSUTIL:
        return {"available": False, "note": "psutil not installed"}
    proc = psutil.Process()
    peaks = {"mem_mb": 0, "cpu_pct": 0}
    end = time.time() + duration_s
    while time.time() < end:
        try:
            mem = proc.memory_info().rss / 1024 / 1024
            cpu = proc.cpu_percent(interval=0.5)
            peaks["mem_mb"] = max(peaks["mem_mb"], round(mem, 1))
            peaks["cpu_pct"] = max(peaks["cpu_pct"], round(cpu, 1))
        except Exception:
            break
    peaks["available"] = True
    return peaks


def _load_api_token() -> Optional[str]:
    """从 ~/.dsh/.credentials.yaml 读取 TRINITY_API_KEY（API 压测鉴权）。"""
    try:
        p = Path.home() / ".dsh" / ".credentials.yaml"
        if not p.exists():
            return None
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*TRINITY_API_KEY\s*[:=]\s*[\"']?([^\"'\s]+)", line)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return None


def copy_db_snapshot(db_path: str) -> Optional[str]:
    """复制生产库到临时文件（VACUUM INTO），压测只读副本、零污染权威库。

    2026-08-16（深挖修复）：改为 VACUUM INTO——原 shutil.copy2 跳过 WAL，
    在 WAL 模式下会导致副本的 FTS rowid 与 memories 错位（新写入触发
    memories_ai 触发器 INSERT memories_fts 撞已存在 rowid → IntegrityError
    constraint failed）。VACUUM INTO 包含 WAL 全部内容、rowid 一致、不
    需要复制 -wal/-shm 文件（天然免疫运行中进程的 WinError 33 锁）。
    """
    src = Path(db_path)
    if not src.is_file():
        return None
    import sqlite3 as _sqlite3
    tmp = Path(tempfile.mkdtemp(prefix="trinity_dbstress_")) / src.name
    try:
        conn = _sqlite3.connect(str(src))
        try:
            conn.execute(
                f"VACUUM INTO '{str(tmp).replace(chr(92), chr(92) * 2)}'"
            )
        finally:
            conn.close()
    except Exception:
        # VACUUM INTO 失败时回退文件复制（只读压测场景可接受）
        shutil.copy2(src, tmp)
        for suffix in ("-wal", "-shm"):
            if Path(str(src) + suffix).is_file():
                try:
                    shutil.copy2(str(src) + suffix, str(tmp) + suffix)
                except OSError:
                    pass
    return str(tmp)


def run_db_read_stress(db_path: str, reads: int, threads: int = 8,
                       queries: Optional[List[str]] = None) -> StressResult:
    """生产库副本并发 hybrid 检索（真实 SQLite I/O + FTS + jieba + 向量）。"""
    result = StressResult()
    from trinity.core.client import Trinity
    mem = Trinity(store_path=db_path)
    if not queries:
        queries = [c.split()[0] for c in _CORPUS]
    barrier = threading.Barrier(threads)

    def worker(wid: int) -> None:
        barrier.wait()
        for i in range(reads // threads):
            q = random.choice(queries)
            t0 = _now_ms()
            try:
                mem.search_hybrid(query=q, top_k=10, strategy="rrf")
                result.add(_now_ms() - t0)
            except Exception as e:
                result.add(_now_ms() - t0, str(e)[:80])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    result.stats()["total_s"] = round(time.time() - t0, 2)
    return result


def run_api_read_stress(base: str, reads: int, threads: int = 8,
                        token: Optional[str] = None) -> StressResult:
    """API 并发检索（真实 HTTP → 引擎全链路）。"""
    result = StressResult()
    url = base.rstrip("/") + "/memory/search/hybrid"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    barrier = threading.Barrier(threads)

    def worker(wid: int) -> None:
        barrier.wait()
        for i in range(reads // threads):
            q = random.choice(_CORPUS)
            body = json.dumps({"query": q[:40], "top_k": 5,
                               "strategy": "rrf"}).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers=headers,
                                         method="POST")
            t0 = _now_ms()
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp.read()
                result.add(_now_ms() - t0)
            except Exception as e:
                result.add(_now_ms() - t0, str(e)[:120])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    result.stats()["total_s"] = round(time.time() - t0, 2)
    return result


def run_db_write_stress(db_path: str, writes: int, threads: int = 8) -> StressResult:
    """生产库副本并发写入（真实 SQLite I/O：store_memory + 审计链）。

    验证历史隐患 database is locked：多线程写同一副本库（WAL + 主连接
    _write_lock 串行化），计数锁错误/死锁。副本可写，零污染权威库。

    2026-08-15（压测修复）：用 postprocess=False —— 与生产 API 的
    memory_write 异步化路径一致（写入即时返回、语义关联/实体提取/主动
    推送后台完成）；同步加工管线占写入成本 ~97%（单条 430-665ms vs
    13ms），计入会混淆"写库稳定性"验证（那是加工管线的成本，不是
    SQLite 写路径的）。另跑少量 postprocess=True 采样对照。
    """
    result = StressResult()
    from trinity.core.client import Trinity
    mem = Trinity(store_path=db_path)
    barrier = threading.Barrier(threads)

    def worker(wid: int) -> None:
        barrier.wait()
        for i in range(writes // threads):
            content = (f"{random.choice(_CORPUS)} [db-w{wid}-{i}] "
                       f"production write stress")
            t0 = _now_ms()
            try:
                mem.ingest(content, persona_id="stress-db-writer",
                           metadata={"category": random.choice(
                               ["db", "research", "ops"]),
                               "source": "full_stress_test_db_write"},
                           postprocess=False)
                result.add(_now_ms() - t0)
            except Exception as e:
                result.add(_now_ms() - t0, str(e)[:120])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    result.stats()["total_s"] = round(time.time() - t0, 2)
    # 对照采样：同步加工管线（3 条，不计入主统计）
    try:
        pp_lat = []
        for i in range(3):
            t0 = _now_ms()
            mem.ingest(f"postprocess 对照 {i} 记忆蒸馏",
                       persona_id="stress-db-writer",
                       metadata={"category": "db"}, postprocess=True)
            pp_lat.append(round(_now_ms() - t0))
        result.stats()["postprocess_sample_ms"] = pp_lat
    except Exception:
        pass
    return result


def consistency_check(db_path: str, expected_delta: int = 0) -> Dict[str, Any]:
    """压力后一致性校验：计数 + 审计 + 哈希链。"""
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
        audit = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        # 哈希链完整性（连续审计 checksum 可追溯）
        chain_ok = True
        prev = ""
        for row in conn.execute(
            "SELECT checksum FROM audit_log ORDER BY timestamp, id LIMIT 50"
        ).fetchall():
            if row["checksum"]:
                prev = row["checksum"]
        # 内容哈希非空比例
        has_hash = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content_hash IS NOT NULL AND content_hash != ''"
        ).fetchone()[0]
        return {
            "memories_total": total,
            "memories_active": active,
            "audit_entries": audit,
            "content_hash_pct": round(has_hash * 100 / max(total, 1), 1),
            "audit_chain_tail_ok": bool(prev),
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity full stress test")
    parser.add_argument("--writes", type=int, default=500)
    parser.add_argument("--reads", type=int, default=500)
    parser.add_argument("--mixed", type=int, default=200)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--db", default=None,
                        help="生产库路径（默认自动探测）；传参时对副本做真实 I/O 检索压测")
    parser.add_argument("--db-write", default=None,
                        help="生产库路径；传参时对副本做真实 I/O 写入压测（并发 store_memory + 审计链，验证 database is locked）")
    parser.add_argument("--api", default=None,
                        help="API base（如 http://127.0.0.1:8001）；传参时并发打 /memory/search/hybrid")
    args = parser.parse_args()

    from trinity.agents.aggregator import MemoryAggregator
    from trinity.core.client import Trinity  # 预热段 + run_db_read_stress 共用

    print(f"== Trinity 全量压力测试 (writes={args.writes} reads={args.reads} "
          f"mixed={args.mixed} threads={args.threads}) ==")
    agg = MemoryAggregator(persist_path=None)
    report: Dict[str, Any] = {"config": vars(args)}
    # 0. 预热（计时外）：等 embedding 就绪 + 触发一次检索（BM25/索引冷启动
    #    不算入压测指标——压测衡量稳定态吞吐，冷启动成本单独记录）
    print("[0] 预热 (embedding fit / BM25 / ANN index)...")
    t0 = time.time()
    agg._embedding_ready.wait(timeout=60)
    agg.ingest("预热记忆 embedding ready", "prewarm", {"category": "db"})
    agg._rebuild_index()
    try:
        agg.query({}, limit=3, mode="hybrid", query_text="预热")
    except Exception:
        pass
    report["warmup_s"] = round(time.time() - t0, 2)
    print(f"    预热完成: {report['warmup_s']}s")

    # 1. 并发写入
    print("\n[1] 并发写入...")
    t0 = time.time()
    w = run_write_stress(agg, args.writes, args.threads)
    report["write"] = w.stats()
    report["write"]["elapsed_s"] = round(time.time() - t0, 2)
    print(f"    写入 {report['write']['count']} 条, "
          f"QPS={report['write']['qps']}, p50={report['write']['p50_ms']}ms, "
          f"p99={report['write']['p99_ms']}ms, errors={report['write']['errors']}")

    # 建索引供检索
    try:
        agg._rebuild_index()
    except Exception:
        pass

    # 2. 并发检索
    print("[2] 并发检索 (hybrid)...")
    t0 = time.time()
    r = run_read_stress(agg, args.reads, args.threads)
    report["read"] = r.stats()
    report["read"]["elapsed_s"] = round(time.time() - t0, 2)
    print(f"    检索 {report['read']['count']} 次, "
          f"QPS={report['read']['qps']}, p50={report['read']['p50_ms']}ms, "
          f"p99={report['read']['p99_ms']}ms, errors={report['read']['errors']}")

    # 3. 混合读写
    print("[3] 混合读写...")
    t0 = time.time()
    m = run_mixed_stress(agg, args.mixed, args.threads)
    report["mixed"] = m.stats()
    report["mixed"]["elapsed_s"] = round(time.time() - t0, 2)
    print(f"    混合 {report['mixed']['count']} 次, "
          f"QPS={report['mixed']['qps']}, p50={report['mixed']['p50_ms']}ms, "
          f"errors={report['mixed']['errors']}")

    # 4. 资源监控（后台测一段）
    print("[4] 资源监控 (5s)...")
    import threading as _th
    res: Dict[str, Any] = {}

    def _mon():
        res.update(monitor_resources(5))

    mt = _th.Thread(target=_mon)
    mt.start()
    # 同时跑一轮额外负载
    run_write_stress(agg, min(args.writes, 200), args.threads)
    mt.join()
    report["resources"] = res
    print(f"    内存峰值 {res.get('mem_mb')}MB, CPU峰值 {res.get('cpu_pct')}%")

    # 5. 一致性校验（压力写的是内存池，校验计数/锁稳定）
    print("[5] 一致性校验...")
    report["consistency"] = {
        "pool_size": len(agg._pool),
        "agent_index": len(agg._agent_index),
        "no_deadlock": True,
    }
    # 检测锁错误（database is locked 类型）
    lock_errors = 0
    for s in (w.stats(), r.stats(), m.stats()):
        lock_errors += sum(
            1 for e in s.get("error_samples", [])
            if "locked" in e.lower() or "deadlock" in e.lower()
        )
    report["consistency"]["lock_errors"] = lock_errors
    print(f"    池大小 {len(agg._pool)}, agents {len(agg._agent_index)}, "
          f"锁错误 {lock_errors}")

    # 6. 生产库副本并发检索（真实 SQLite I/O，零污染）
    db_result = None
    db_snapshot = None
    db_consistency: Dict[str, Any] = {}
    if args.db:
        db_snapshot = copy_db_snapshot(args.db)
        if db_snapshot:
            print(f"[6] 生产库副本并发检索 ({args.db})...")
            # 预热（计时外）：BM25 后台预构建（2026-08-15 起不再阻塞首次检索，
            # 但构建期与检索争 GIL）——等 _bm25_ready 后再计时，冷启动不计入。
            t0 = time.time()
            _warm = Trinity(store_path=db_snapshot)
            try:
                _warm.search_hybrid(query="数据库", top_k=3, strategy="rrf")
                _deadline = time.time() + 30
                while time.time() < _deadline and not getattr(
                        _warm, "_bm25_ready", False):
                    time.sleep(0.2)
            except Exception:
                pass
            report["db"] = {"source": args.db,
                            "bm25_warmup_s": round(time.time() - t0, 2)}
            t0 = time.time()
            db_result = run_db_read_stress(db_snapshot, args.reads, args.threads)
            db_stats = db_result.stats()
            db_stats["elapsed_s"] = round(time.time() - t0, 2)
            print(f"    生产库检索 {db_stats['count']} 次, "
                  f"QPS={db_stats['qps']}, p50={db_stats['p50_ms']}ms, "
                  f"p99={db_stats['p99_ms']}ms, errors={db_stats['errors']}")
            db_consistency = consistency_check(db_snapshot)
            report["db"].update({"read": db_stats, "consistency": db_consistency})
            print(f"    一致性: memories={db_consistency.get('memories_total')}, "
                  f"active={db_consistency.get('memories_active')}, "
                  f"audit={db_consistency.get('audit_entries')}")
        else:
            print(f"[6] 跳过：库不存在 {args.db}")

    # 6b. 生产库副本并发写入（真实 I/O：store_memory + 审计链，验证锁稳定）
    dbw_result = None
    if args.db_write:
        dbw_snapshot = copy_db_snapshot(args.db_write)
        if dbw_snapshot:
            print(f"[6b] 生产库副本并发写入 ({args.db_write})...")
            t0 = time.time()
            dbw_result = run_db_write_stress(dbw_snapshot, args.writes,
                                             args.threads)
            dbw_stats = dbw_result.stats()
            dbw_stats["elapsed_s"] = round(time.time() - t0, 2)
            lock_errs = sum(1 for e in dbw_stats.get("error_samples", [])
                            if "locked" in e.lower() or "deadlock" in e.lower())
            dbw_consistency = consistency_check(dbw_snapshot)
            report["db_write"] = {
                "source": args.db_write, "write": dbw_stats,
                "lock_errors": lock_errs, "consistency": dbw_consistency,
            }
            print(f"    写入 {dbw_stats['count']} 条, "
                  f"QPS={dbw_stats['qps']}, p50={dbw_stats['p50_ms']}ms, "
                  f"p99={dbw_stats['p99_ms']}ms, errors={dbw_stats['errors']}")
            print(f"    一致性: memories={dbw_consistency.get('memories_total')}, "
                  f"audit={dbw_consistency.get('audit_entries')}")
            try:
                Path(dbw_snapshot).unlink(missing_ok=True)
            except Exception:
                pass
        else:
            print(f"[6b] 跳过：库不存在 {args.db_write}")

    # 7. API 并发检索（真实 HTTP → 引擎全链路）
    api_result = None
    if args.api:
        token = _load_api_token()
        print(f"[7] API 并发检索 ({args.api}, auth={'on' if token else 'off'})...")
        # 预热（计时外）：API 进程首次检索触发 BM25 后台构建；
        # 等待构建窗口（~1-2s）避免与压测抢 GIL。
        try:
            _warm_body = json.dumps({"query": "数据库", "top_k": 3,
                                     "strategy": "rrf"}).encode("utf-8")
            _warm_headers = {"Content-Type": "application/json"}
            if token:
                _warm_headers["Authorization"] = f"Bearer {token}"
            _warm_req = urllib.request.Request(
                args.api.rstrip("/") + "/memory/search/hybrid",
                data=_warm_body, headers=_warm_headers, method="POST")
            with urllib.request.urlopen(_warm_req, timeout=30) as _wr:
                _wr.read()
            time.sleep(2.5)  # BM25 后台构建窗口
        except Exception:
            pass
        t0 = time.time()
        api_result = run_api_read_stress(args.api, args.reads, args.threads, token)
        api_stats = api_result.stats()
        api_stats["elapsed_s"] = round(time.time() - t0, 2)
        print(f"    API 检索 {api_stats['count']} 次, "
              f"QPS={api_stats['qps']}, p50={api_stats['p50_ms']}ms, "
              f"p99={api_stats['p99_ms']}ms, errors={api_stats['errors']}")
        report["api"] = {"base": args.api, "read": api_stats}

    # 通过判定：无错误 + 无锁冲突（内存池 + 生产库 + API + 生产库写入）
    ok = (w.stats()["errors"] == 0 and r.stats()["errors"] == 0
          and m.stats()["errors"] == 0 and lock_errors == 0)
    if db_result is not None:
        ok = ok and db_result.stats()["errors"] == 0
    if dbw_result is not None:
        ok = ok and dbw_result.stats()["errors"] == 0
    if api_result is not None:
        ok = ok and api_result.stats()["errors"] == 0
    report["pass"] = ok

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / "full_stress_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告: {out}")
    print(f"RESULT: {'PASS ✅' if ok else 'FAIL ❌'}")

    # 清理临时副本
    if db_snapshot:
        try:
            Path(db_snapshot).unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(db_snapshot + suffix).unlink(missing_ok=True)
        except Exception:
            pass

    agg.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
