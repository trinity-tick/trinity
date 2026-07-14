#!/usr/bin/env python3
"""
并发压力测试 — concurrency_bench.py

使用 asyncio 进行并发梯度测试，测量 QPS / 分位数延迟 / 内存峰值，
生成 CSV 可视化数据。

依赖: Python 3.8+, psutil (可选，用于内存测量)
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

# 可选 psutil
try:
    import psutil

    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

from latency_profiler import Profiler


# ──────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────


@dataclass
class ConcurrencyResult:
    """单档位并发测试结果。"""

    concurrency: int
    total_requests: int
    total_elapsed_s: float
    qps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    p99_9_ms: float
    mean_ms: float
    min_ms: float
    max_ms: float
    peak_memory_mb: float = 0.0
    errors: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concurrency": self.concurrency,
            "total_requests": self.total_requests,
            "total_elapsed_s": round(self.total_elapsed_s, 4),
            "qps": round(self.qps, 2),
            "p50_ms": round(self.p50_ms, 4),
            "p95_ms": round(self.p95_ms, 4),
            "p99_ms": round(self.p99_ms, 4),
            "p99_9_ms": round(self.p99_9_ms, 4),
            "mean_ms": round(self.mean_ms, 4),
            "min_ms": round(self.min_ms, 4),
            "max_ms": round(self.max_ms, 4),
            "peak_memory_mb": round(self.peak_memory_mb, 2),
            "errors": self.errors,
        }


def _percentile(sorted_data: List[float], p: float) -> float:
    """线性插值分位数。"""
    n = len(sorted_data)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_data[0])
    rank = (p / 100.0) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_data[lo] * (1 - frac) + sorted_data[hi] * frac


# ──────────────────────────────────────────────────────────────
# 内存峰值测量
# ──────────────────────────────────────────────────────────────


def _get_memory_mb() -> float:
    """获取当前进程 RSS (MB)。"""
    if not _PSUTIL_AVAILABLE:
        return 0.0
    try:
        proc = psutil.Process()
        return proc.memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0


async def _memory_monitor(interval: float, stop_event: asyncio.Event) -> float:
    """后台内存监控协程，返回峰值 (MB)。"""
    peak = 0.0
    while not stop_event.is_set():
        mem = _get_memory_mb()
        if mem > peak:
            peak = mem
        await asyncio.sleep(interval)
    return peak


# ──────────────────────────────────────────────────────────────
# 并发执行器
# ──────────────────────────────────────────────────────────────


async def _worker(
    worker_id: int,
    task_fn: Callable[[], Coroutine],
    results: List[float],
    error_counter: List[int],
    sem: asyncio.Semaphore,
) -> None:
    """单个 worker：获取信号量 → 执行任务 → 记录延迟。"""
    async with sem:
        t0 = time.perf_counter()
        try:
            await task_fn()
        except Exception:
            error_counter[0] += 1
        elapsed = (time.perf_counter() - t0) * 1000.0  # ms
        results.append(elapsed)


async def run_concurrency_test(
    task_fn: Callable[[], Coroutine],
    concurrency: int,
    total_requests: int = 500,
) -> ConcurrencyResult:
    """
    运行单个并发档位测试。

    Parameters
    ----------
    task_fn : async callable 工厂
        每次请求时调用 ``await task_fn()`` 执行单次任务。
    concurrency : int
        并发度。
    total_requests : int
        总请求数。

    Returns
    -------
    ConcurrencyResult
    """
    latencies: List[float] = []
    error_counter: List[int] = [0]

    # 启动内存监控
    stop_event = asyncio.Event()
    mem_task = asyncio.create_task(_memory_monitor(0.1, stop_event))

    sem = asyncio.Semaphore(concurrency)

    t0 = time.perf_counter()

    # 并发执行
    workers = [
        _worker(i, task_fn, latencies, error_counter, sem)
        for i in range(total_requests)
    ]
    await asyncio.gather(*workers)

    t1 = time.perf_counter()

    # 停止内存监控
    stop_event.set()
    peak_mem = await mem_task

    # 统计
    total_elapsed = t1 - t0
    sorted_lat = sorted(latencies)

    return ConcurrencyResult(
        concurrency=concurrency,
        total_requests=len(latencies),
        total_elapsed_s=total_elapsed,
        qps=len(latencies) / total_elapsed if total_elapsed > 0 else 0.0,
        p50_ms=_percentile(sorted_lat, 50),
        p95_ms=_percentile(sorted_lat, 95),
        p99_ms=_percentile(sorted_lat, 99),
        p99_9_ms=_percentile(sorted_lat, 99.9),
        mean_ms=sum(latencies) / len(latencies) if latencies else 0.0,
        min_ms=sorted_lat[0] if sorted_lat else 0.0,
        max_ms=sorted_lat[-1] if sorted_lat else 0.0,
        peak_memory_mb=peak_mem,
        errors=error_counter[0],
    )


# ──────────────────────────────────────────────────────────────
# 梯度测试
# ──────────────────────────────────────────────────────────────


async def run_all_concurrency_levels(
    task_fn: Callable[[], Coroutine],
    levels: Optional[List[int]] = None,
    requests_per_level: int = 300,
    output_csv: str = "",
    output_json: str = "",
) -> List[ConcurrencyResult]:
    """
    按梯度执行并发测试。

    Parameters
    ----------
    task_fn : 任务工厂
    levels : 并发梯度列表，默认 [10, 50, 100, 200]
    requests_per_level : 每档位总请求数
    output_csv : CSV 输出路径 (可选)
    output_json : JSON 输出路径 (可选)

    Returns
    -------
    List[ConcurrencyResult]
    """
    if levels is None:
        levels = [10, 50, 100, 200]

    results: List[ConcurrencyResult] = []

    print(f"\n{'='*60}")
    print(f"并发压力测试 (梯度: {levels})")
    print(f"每档请求数: {requests_per_level}")
    print(f"{'='*60}\n")

    for i, c in enumerate(levels):
        print(f"  [{i+1}/{len(levels)}] 并发={c:>4d} ...", end=" ", flush=True)

        result = await run_concurrency_test(task_fn, c, requests_per_level)

        results.append(result)
        print(
            f"QPS={result.qps:>8.1f}  P50={result.p50_ms:>7.2f}ms  "
            f"P95={result.p95_ms:>7.2f}ms  P99={result.p99_ms:>7.2f}ms  "
            f"Mem={result.peak_memory_mb:>7.1f}MB  errors={result.errors}"
        )

    # 输出
    if output_csv:
        _write_csv(results, output_csv)
        print(f"\n[CSV]  → {output_csv}")

    if output_json:
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in results], f, indent=2, ensure_ascii=False)
        print(f"[JSON] → {output_json}")

    return results


def _write_csv(results: List[ConcurrencyResult], path: str) -> None:
    fields = [
        "concurrency",
        "total_requests",
        "total_elapsed_s",
        "qps",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "p99_9_ms",
        "mean_ms",
        "min_ms",
        "max_ms",
        "peak_memory_mb",
        "errors",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            writer.writerow(r.to_dict())


# ──────────────────────────────────────────────────────────────
# 默认 stub 任务 (模拟 Trinity 查询)
# ──────────────────────────────────────────────────────────────


async def default_trinity_stub_query() -> None:
    """默认 stub：模拟一次 Trinity 端到端查询 (~40ms)。"""
    # 模拟语义向量 + BFS + KV + RRF 融合
    await asyncio.sleep(random.gauss(0.040, 0.010))


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="并发压力测试")
    parser.add_argument(
        "--levels",
        type=int,
        nargs="+",
        default=[10, 50, 100, 200],
        help="并发梯度，如 --levels 10 50 100 200",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=300,
        help="每档位请求数 (默认 300)",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="结果输出目录",
    )
    args = parser.parse_args()

    csv_path = os.path.join(args.output_dir, "concurrency_results.csv")
    json_path = os.path.join(args.output_dir, "concurrency_results.json")

    async def main():
        await run_all_concurrency_levels(
            task_fn=default_trinity_stub_query,
            levels=args.levels,
            requests_per_level=args.requests,
            output_csv=csv_path,
            output_json=json_path,
        )

    asyncio.run(main())
