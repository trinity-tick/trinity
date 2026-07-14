#!/usr/bin/env python3
"""
Trinity 专项 Profiler — trinity_profiler.py

分模块测量 Trinity 三位一体系统的各组件延迟，当前为 stub 模式，
预留对接接口供真实 Trinity 代码接入。

组件列表:
- M101 海马体双通道写入延迟
- CB35 冲突合并索引延迟
- CB38 双轨融合检索延迟 (RRF 权重融合)
- GoSStateIndex BFS 图遍历延迟
- CB36 精确 KV 缓存命中率与 miss 延迟
- 端到端 query→结果 延迟 (语义向量 + 图遍历 + KV 精确三路融合)
"""

from __future__ import annotations

import functools
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from latency_profiler import Profiler, ProfilerResult, results_to_json, results_to_markdown

# ──────────────────────────────────────────────────────────────
# @profile 装饰器
# ──────────────────────────────────────────────────────────────

_registry: Dict[str, ProfilerResult] = {}


def profile(name: str, warmup: int = 3, rounds: int = 50, unit: str = "ms"):
    """装饰器：自动对函数进行延迟 profiling 并将结果存入全局注册表。"""

    def decorator(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            p = Profiler(name=name, warmup=warmup, rounds=rounds, unit=unit)
            # 检查是否是 async 函数
            import asyncio

            if asyncio.iscoroutinefunction(fn):

                async def _async_runner():
                    return await p.aprofile(lambda: fn(*args, **kwargs))

                # 如果在事件循环中，直接 await；否则 run
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    result = asyncio.run(_async_runner())
                else:
                    # 已在事件循环 — 返回协程
                    import warnings

                    warnings.warn(
                        f"@profile {name}: async function called inside running event loop; "
                        "result will not be captured. Use `await aprofile_method()` instead."
                    )
                    return fn(*args, **kwargs)
            else:
                result = p.profile(lambda: fn(*args, **kwargs))

            _registry[name] = result
            return result

        wrapper._profile_name = name
        wrapper._is_profiled = True
        return wrapper

    return decorator


def get_registry() -> Dict[str, ProfilerResult]:
    return _registry


# ──────────────────────────────────────────────────────────────
# Stub 辅助函数
# ──────────────────────────────────────────────────────────────

def _simulate_latency(mu_ms: float, sigma_ms: float = 1.0) -> None:
    """模拟正态分布延迟 (stub)。"""
    time.sleep(max(0, random.gauss(mu_ms, sigma_ms)) / 1000.0)


# ──────────────────────────────────────────────────────────────
# M101 海马体双通道写入
# ──────────────────────────────────────────────────────────────

@profile(name="M101_hippocampus_dual_write", warmup=3, rounds=50)
def measure_hippocampus_write(
    num_entries: int = 100,
    channel_a_latency_ms: float = 2.5,
    channel_b_latency_ms: float = 3.0,
) -> ProfilerResult:
    """
    海马体双通道写入延迟测量。

    参数 (对接真实代码时替换为实际 API 调用):
    - num_entries: 写入条目数
    - channel_a_latency_ms: A 通道平均延迟 (stub)
    - channel_b_latency_ms: B 通道平均延迟 (stub)

    真实对接时：替换 _simulate_latency 为真实的 write 调用。
    """
    # Stub: 模拟双通道并行写入
    entries = [f"mem_{i:05d}" for i in range(num_entries)]
    _simulate_latency(channel_a_latency_ms, 0.5)  # Channel A
    _simulate_latency(channel_b_latency_ms, 0.6)  # Channel B
    # 真实代码示例:
    # result = trinity.m101.write_dual(entries, channel_config={"A": {...}, "B": {...}})
    return get_registry().get("M101_hippocampus_dual_write")


# ──────────────────────────────────────────────────────────────
# CB35 冲突合并索引
# ──────────────────────────────────────────────────────────────

@profile(name="CB35_conflict_merge_index", warmup=3, rounds=50)
def measure_conflict_merge(
    num_conflicts: int = 50,
    merge_latency_ms: float = 1.8,
) -> ProfilerResult:
    """
    CB35 冲突合并索引延迟测量。

    真实对接时：替换为 trinity.cb35.merge_index(conflicts) 调用。
    """
    conflicts = [(f"key_{i}", f"val_v{i % 3}") for i in range(num_conflicts)]
    _simulate_latency(merge_latency_ms, 0.3)
    return get_registry().get("CB35_conflict_merge_index")


# ──────────────────────────────────────────────────────────────
# CB38 双轨融合检索 (RRF 权重融合)
# ──────────────────────────────────────────────────────────────

@profile(name="CB38_dual_track_rrf_retrieval", warmup=3, rounds=50)
def measure_dual_track_rrf(
    num_docs: int = 10000,
    k: int = 100,
    rrf_k: int = 60,
    vector_weight: float = 0.6,
    keyword_weight: float = 0.4,
) -> ProfilerResult:
    """
    CB38 双轨融合检索延迟测量 (语义向量 + 关键词 BM25，RRF 融合)。

    参数:
    - num_docs: 索引文档数
    - k: Top-K 返回数
    - rrf_k: RRF 平滑常数
    - vector_weight: 语义向量权重
    - keyword_weight: 关键词权重

    真实对接时：替换为 trinity.cb38.rrf_search(query, ...) 调用。
    """
    # Stub: 模拟向量检索 + BM25 + RRF 融合三段延迟
    _simulate_latency(15.0, 2.0)  # 向量检索 (dense)
    _simulate_latency(8.0, 1.0)  # BM25 检索 (sparse)
    _simulate_latency(3.0, 0.5)  # RRF 融合 + 重排
    return get_registry().get("CB38_dual_track_rrf_retrieval")


# ──────────────────────────────────────────────────────────────
# GoSStateIndex BFS 图遍历
# ──────────────────────────────────────────────────────────────

@profile(name="GoSStateIndex_bfs_traversal", warmup=3, rounds=50)
def measure_bfs_traversal(
    num_nodes: int = 5000,
    branching_factor: int = 5,
    max_depth: int = 3,
) -> ProfilerResult:
    """
    GoSStateIndex BFS 图遍历延迟测量。

    真实对接时：替换为 trinity.gos.bfs_search(start_node, max_depth) 调用。
    """
    visited = min(num_nodes, branching_factor**max_depth)
    _simulate_latency(12.0 * (visited / 125), 1.5)
    return get_registry().get("GoSStateIndex_bfs_traversal")


# ──────────────────────────────────────────────────────────────
# CB36 精确 KV 缓存命中率与 miss 延迟
# ──────────────────────────────────────────────────────────────

@profile(name="CB36_kv_cache_hit_miss", warmup=3, rounds=100)
def measure_kv_cache(
    cache_size: int = 1000,
    query_count: int = 200,
    hit_ratio: float = 0.85,
    hit_latency_ms: float = 0.3,
    miss_latency_ms: float = 4.0,
) -> ProfilerResult:
    """
    CB36 精确 KV 缓存命中率与 miss 延迟测量。

    在统计轮次中按 hit_ratio 模拟命中和未命中，测量平均延迟。
    """
    for _ in range(query_count):
        if random.random() < hit_ratio:
            _simulate_latency(hit_latency_ms, 0.05)
        else:
            _simulate_latency(miss_latency_ms, 0.4)
    return get_registry().get("CB36_kv_cache_hit_miss")


# ──────────────────────────────────────────────────────────────
# 端到端 query→结果 延迟 (三路融合)
# ──────────────────────────────────────────────────────────────

@profile(name="E2E_trinity_query", warmup=2, rounds=30)
def measure_e2e_query(
    query_text: str = "什么是三位一体记忆系统？",
    vector_dim: int = 768,
    top_k: int = 50,
) -> ProfilerResult:
    """
    端到端 query→结果 延迟:

    1. 语义向量检索 (dense)
    2. GoSStateIndex BFS 图遍历
    3. CB36 KV 精确检索
    4. CB38 RRF 三路融合

    真实对接时：替换为 trinity.query(query_text) 调用。
    """
    _simulate_latency(18.0, 3.0)  # 语义向量检索
    _simulate_latency(14.0, 2.0)  # BFS 图遍历
    _simulate_latency(2.0, 0.8)  # KV 精确检索
    _simulate_latency(6.0, 1.0)  # RRF 三路融合
    return get_registry().get("E2E_trinity_query")


# ──────────────────────────────────────────────────────────────
# 批量运行
# ──────────────────────────────────────────────────────────────

def run_all_profiles() -> List[ProfilerResult]:
    """执行所有 Trinity 专项测量模块，返回结果列表。"""
    results: List[ProfilerResult] = []

    print("=" * 60)
    print("Trinity 专项 Profiling")
    print("=" * 60)

    profiles = [
        ("M101 海马体双通道写入", measure_hippocampus_write),
        ("CB35 冲突合并索引", measure_conflict_merge),
        ("CB38 双轨融合检索 (RRF)", measure_dual_track_rrf),
        ("GoSStateIndex BFS 图遍历", measure_bfs_traversal),
        ("CB36 KV 缓存命中/miss", measure_kv_cache),
        ("端到端 Query (三路融合)", measure_e2e_query),
    ]

    for label, fn in profiles:
        print(f"\n  [{label}] ...", end=" ", flush=True)
        result = fn()
        results.append(result)
        print(f"P50={result.p50:.4f}{result.unit}  P95={result.p95:.4f}{result.unit}  "
              f"P99={result.p99:.4f}{result.unit}")

    return results


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Trinity 专项 Profiler")
    parser.add_argument(
        "--trinity-path",
        default="",
        help="Trinity 代码路径 (stub 模式下忽略)",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="结果输出目录 (默认: 当前目录)",
    )
    args = parser.parse_args()

    # 如果提供了 trinity-path，记录但 stub 模式下不实际导入
    if args.trinity_path:
        print(f"[INFO] Trinity 路径已指定: {args.trinity_path}")
        print("[INFO] 当前为 stub 模式，未实际对接真实 Trinity 代码。")

    results = run_all_profiles()
    output_dir = args.output_dir or "."

    # 输出 JSON
    json_path = os.path.join(output_dir, "trinity_profile_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        f.write(results_to_json(results))
    print(f"\n[JSON] → {json_path}")

    # 输出 Markdown
    md_path = os.path.join(output_dir, "trinity_profile_results.md")
    md = results_to_markdown(results, title="Trinity 专项延迟 Profiling 报告")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[MD]  → {md_path}")

    print("\n完成。")
