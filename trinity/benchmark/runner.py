"""
Trinity Benchmark Runner — unified benchmark execution.

Benchmarks:
  - longmemeval  LongMemEval (Recall@1/5/10, QA Accuracy)
  - memsyco      MemSyco (Sycophancy resistance scoring)
  - latency      Retrieval latency profiling (P50/P95/P99)
  - mock         Quick mock benchmark (no network required)
  - all          Run all benchmarks
"""

import json
import sys
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional


BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark_scripts"


def run_benchmark(name: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run a benchmark by name.

    Args:
        name: Benchmark name (longmemeval, memsyco, latency, mock, all).
        config: Configuration dict with overrides.

    Returns:
        Benchmark results dict.
    """
    config = config or {}

    if name == "longmemeval":
        return _run_longmemeval(config)
    elif name == "memsyco":
        return _run_memsyco(config)
    elif name == "latency":
        return _run_latency(config)
    elif name == "mock":
        return _run_mock(config)
    elif name == "all":
        results = {
            "longmemeval": _run_longmemeval(config),
            "memsyco": _run_memsyco(config),
            "latency": _run_latency(config),
            "mock": _run_mock(config),
        }
        results["summary"] = {"benchmark": "Full Suite", "status": "completed"}
        return results
    else:
        return {"error": f"Unknown benchmark: {name}",
                "valid": ["longmemeval", "memsyco", "latency", "mock", "all"]}


def _run_longmemeval(config: Dict[str, Any]) -> Dict[str, Any]:
    """Run LongMemEval benchmark via subprocess."""
    script = BENCHMARK_DIR / "run_benchmark.py"
    if not script.exists():
        return _benchmark_not_found("LongMemEval", str(script))

    try:
        cmd = [sys.executable, str(script)]
        if "top_k" in config:
            cmd += ["--top-k", str(config["top_k"])]
        if "skip_qa" in config and config["skip_qa"]:
            cmd += ["--skip-qa"]

        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
        return {
            "benchmark": "LongMemEval",
            "config": config,
            "stdout": proc.stdout[-3000:] if len(proc.stdout) > 3000 else proc.stdout,
            "stderr": proc.stderr[-1000:] if len(proc.stderr) > 1000 else proc.stderr,
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error": "LongMemEval benchmark timed out (300s)"}
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}


def _run_memsyco(config: Dict[str, Any]) -> Dict[str, Any]:
    """Run MemSyco benchmark via subprocess."""
    script = BENCHMARK_DIR / "memsyco_evaluator.py"
    if not script.exists():
        return _benchmark_not_found("MemSyco", str(script))

    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=120,
        )
        return {
            "benchmark": "MemSyco",
            "stdout": proc.stdout[-2000:],
            "returncode": proc.returncode,
        }
    except Exception as e:
        return {"error": str(e)}


def _run_latency(config: Dict[str, Any]) -> Dict[str, Any]:
    """Run latency profiling via subprocess."""
    script = BENCHMARK_DIR / "run_latency_bench.py"
    if not script.exists():
        return _benchmark_not_found("Latency", str(script))

    try:
        iterations = config.get("iterations", 100)
        proc = subprocess.run(
            [sys.executable, str(script), "--iterations", str(iterations)],
            capture_output=True, text=True, timeout=120,
        )
        return {
            "benchmark": "Latency Profile",
            "iterations": iterations,
            "stdout": proc.stdout[-2000:],
            "returncode": proc.returncode,
        }
    except Exception as e:
        return {"error": str(e)}


def _run_mock(config: Dict[str, Any]) -> Dict[str, Any]:
    """Run mock benchmark (no network, no external deps).

    Uses synthetic data to validate that the retrieval pipeline
    can find memories injected and then retrieved.
    """
    script = BENCHMARK_DIR / "mock_data.py"
    if not script.exists():
        return _benchmark_not_found("Mock", str(script))

    try:
        sys.path.insert(0, str(BENCHMARK_DIR))
        import mock_data

        personas = config.get("personas", 3)
        results = mock_data.evaluate_mock_retrieval(None, top_k=config.get("top_k", 10))

        return {
            "benchmark": "LongMemEval (Mock)",
            "version": "1.0",
            "config": config,
            "results": {
                "total_questions": results["total_questions"],
                "Recall@1": f"{results['recall_at_1']:.1%}",
                "Recall@5": f"{results['recall_at_5']:.1%}",
                "Recall@10": f"{results['recall_at_10']:.1%}",
            },
            "summary": (
                f"Recall@1={results['recall_at_1']:.1%} | "
                f"Recall@5={results['recall_at_5']:.1%} | "
                f"Recall@10={results['recall_at_10']:.1%}"
            ),
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}


def _benchmark_not_found(name: str, path: str) -> Dict[str, Any]:
    """Return a standard 'not found' error."""
    return {
        "error": f"{name} benchmark not available",
        "expected_path": path,
        "hint": "Benchmark scripts should be in trinity/benchmark_scripts/",
    }
