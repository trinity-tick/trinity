#!/usr/bin/env python3
"""
延迟测量核心模块 — Latency Profiler

支持对任意 callable 进行高精度延迟采样，自动计算分位数统计，
输出 JSON + Markdown 表格格式。

依赖: Python 3.8+ (标准库)
"""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ──────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────


@dataclass
class ProfilerResult:
    """单次 profiling 的完整统计结果。"""

    name: str
    unit: str = "ms"
    total_rounds: int = 0
    warmup_rounds: int = 0
    stat_rounds: int = 0
    raw_samples: List[float] = field(default_factory=list)
    min_val: float = 0.0
    max_val: float = 0.0
    mean: float = 0.0
    stddev: float = 0.0
    p50: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    p99_9: float = 0.0
    total_elapsed: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "total_rounds": self.total_rounds,
            "warmup_rounds": self.warmup_rounds,
            "stat_rounds": self.stat_rounds,
            "min": round(self.min_val, 6),
            "max": round(self.max_val, 6),
            "mean": round(self.mean, 6),
            "stddev": round(self.stddev, 6),
            "p50": round(self.p50, 6),
            "p95": round(self.p95, 6),
            "p99": round(self.p99, 6),
            "p99.9": round(self.p99_9, 6),
            "total_elapsed_s": round(self.total_elapsed, 6),
            "metadata": self.metadata,
        }

    def to_markdown_row(self) -> str:
        """单行 Markdown 表格行。"""
        return (
            f"| {self.name} | {self.stat_rounds} | "
            f"{self.min_val:.4f} | {self.mean:.4f} | {self.p50:.4f} | "
            f"{self.p95:.4f} | {self.p99:.4f} | {self.p99_9:.4f} | "
            f"{self.max_val:.4f} | {self.total_elapsed:.4f}s |"
        )

    @staticmethod
    def markdown_header() -> str:
        return (
            "| 名称 | 采样数 | Min | Mean | P50 | P95 | P99 | P99.9 | Max | 总耗时 |\n"
            "|------|--------|-----|------|-----|-----|-----|-------|-----|--------|"
        )


# ──────────────────────────────────────────────────────────────
# 核心 Profiler
# ──────────────────────────────────────────────────────────────


class Profiler:
    """高精度延迟分析器。

    Parameters
    ----------
    name : str
        测试名称，用于输出标识。
    warmup : int
        预热轮次（不计入统计）。
    rounds : int
        正式统计轮次。
    unit : str
        单位: "s" | "ms" | "us" | "ns"。
        原始计时使用 ``time.perf_counter()`` 返回秒，
        乘以对应系数后存储/展示。
    """

    _UNIT_FACTORS: Dict[str, float] = {
        "s": 1.0,
        "ms": 1e3,
        "us": 1e6,
        "ns": 1e9,
    }

    def __init__(
        self,
        name: str = "unnamed",
        warmup: int = 5,
        rounds: int = 100,
        unit: str = "ms",
    ):
        self.name = name
        self.warmup = warmup
        self.rounds = rounds
        if unit not in self._UNIT_FACTORS:
            raise ValueError(f"unit must be one of {list(self._UNIT_FACTORS)}")
        self.unit = unit
        self._factor = self._UNIT_FACTORS[unit]

        # 存储
        self._samples: List[float] = []

    # ── 公共 API ────────────────────────────────────────────

    def profile(self, fn: Callable[[], Any], *args: Any, **kwargs: Any) -> ProfilerResult:
        """对 ``fn(*args, **kwargs)`` 进行 profiling。

        Returns
        -------
        ProfilerResult
        """
        self._samples.clear()
        t0_overall = time.perf_counter()

        total = self.warmup + self.rounds

        for i in range(total):
            if i == self.warmup:
                # 预热结束，重置统计起点
                pass

            t0 = time.perf_counter()
            fn(*args, **kwargs)
            elapsed_raw = time.perf_counter() - t0

            if i >= self.warmup:
                self._samples.append(elapsed_raw * self._factor)

        t1_overall = time.perf_counter()

        return self._build_result(t0_overall, t1_overall)

    async def aprofile(
        self, coro_fn: Callable[[], Any], *args: Any, **kwargs: Any
    ) -> ProfilerResult:
        """异步版本 profile —— 对 ``await coro_fn(*args, **kwargs)`` 进行采样。"""
        self._samples.clear()
        t0_overall = time.perf_counter()

        total = self.warmup + self.rounds

        for i in range(total):
            t0 = time.perf_counter()
            await coro_fn(*args, **kwargs)
            elapsed_raw = time.perf_counter() - t0

            if i >= self.warmup:
                self._samples.append(elapsed_raw * self._factor)

        t1_overall = time.perf_counter()
        return self._build_result(t0_overall, t1_overall)

    # ── 统计计算 ────────────────────────────────────────────

    @staticmethod
    def percentile(sorted_data: Sequence[float], p: float) -> float:
        """线性插值分位数 (与 numpy.percentile 对齐)。"""
        n = len(sorted_data)
        if n == 0:
            return 0.0
        if n == 1:
            return float(sorted_data[0])

        rank = (p / 100.0) * (n - 1)
        lo = int(math.floor(rank))
        hi = int(math.ceil(rank))
        if lo == hi:
            return float(sorted_data[lo])
        frac = rank - lo
        return sorted_data[lo] * (1 - frac) + sorted_data[hi] * frac

    def _build_result(self, t0: float, t1: float) -> ProfilerResult:
        sorted_samples = sorted(self._samples)
        stat_rounds = len(sorted_samples)

        return ProfilerResult(
            name=self.name,
            unit=self.unit,
            total_rounds=self.warmup + self.rounds,
            warmup_rounds=self.warmup,
            stat_rounds=stat_rounds,
            raw_samples=list(self._samples),
            min_val=sorted_samples[0] if stat_rounds else 0.0,
            max_val=sorted_samples[-1] if stat_rounds else 0.0,
            mean=statistics.mean(self._samples) if stat_rounds else 0.0,
            stddev=statistics.stdev(self._samples) if stat_rounds > 1 else 0.0,
            p50=self.percentile(sorted_samples, 50),
            p95=self.percentile(sorted_samples, 95),
            p99=self.percentile(sorted_samples, 99),
            p99_9=self.percentile(sorted_samples, 99.9),
            total_elapsed=t1 - t0,
        )


# ──────────────────────────────────────────────────────────────
# 报告生成
# ──────────────────────────────────────────────────────────────


def results_to_json(results: List[ProfilerResult], indent: int = 2) -> str:
    """将结果列表序列化为 JSON 字符串。"""
    return json.dumps([r.to_dict() for r in results], indent=indent, ensure_ascii=False)


def results_to_markdown(results: List[ProfilerResult], title: str = "Latency Profiling Report") -> str:
    """将结果列表格式化为 Markdown 表格。"""
    lines = [
        f"# {title}",
        "",
        ProfilerResult.markdown_header(),
    ]
    for r in results:
        lines.append(r.to_markdown_row())
    lines.append("")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Latency Profiler — 延迟测量核心")
    parser.add_argument("--warmup", type=int, default=5, help="预热轮次")
    parser.add_argument("--rounds", type=int, default=100, help="统计轮次")
    parser.add_argument("--unit", default="ms", choices=["s", "ms", "us", "ns"])
    parser.add_argument("--output-json", default="", help="JSON 输出路径")
    parser.add_argument("--output-md", default="", help="Markdown 输出路径")
    args = parser.parse_args()

    # 示例：测量 sleep 1ms 的延迟
    profiler = Profiler(name="sleep_1ms", warmup=args.warmup, rounds=args.rounds, unit=args.unit)
    result = profiler.profile(lambda: time.sleep(0.001))

    print(result.to_markdown_row())

    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as f:
            f.write(results_to_json([result]))
        print(f"JSON → {args.output_json}")

    if args.output_md:
        md = results_to_markdown([result])
        with open(args.output_md, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"Markdown → {args.output_md}")
