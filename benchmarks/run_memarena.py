#!/usr/bin/env python3
"""
benchmarks.run_memarena — CLI entry point for MemArena benchmark suite.

Usage:
    python -m benchmarks.run_memarena --dataset locomo
    python -m benchmarks.run_memarena --dataset longmem_eval --dataset-path data/lme.json
    python -m benchmarks.run_memarena --dataset locomo --systems trinity,mock --output reports/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks import __version__ as BENCH_VERSION
from benchmarks.arena import ArenaRunner, MockMemorySystem
from benchmarks.datasets import (
    DatasetLoader,
    LoCoMoDataset,
    LongMemEvalDataset,
    MemoryAgentBenchDataset,
    LoCoMoR1Dataset,
)


# ── Dataset factory ─────────────────────────────────────────────────────────

DATASET_REGISTRY: dict[str, type[DatasetLoader]] = {
    "locomo": LoCoMoDataset,
    "longmem_eval": LongMemEvalDataset,
    "memory_agent_bench": MemoryAgentBenchDataset,
    "locomo_r1": LoCoMoR1Dataset,
}


def _resolve_dataset(name: str, path: str | None = None) -> DatasetLoader:
    """Instantiate the correct DatasetLoader subclass."""
    cls = DATASET_REGISTRY.get(name.lower())
    if cls is None:
        choices = ", ".join(DATASET_REGISTRY)
        raise ValueError(f"Unknown dataset '{name}'. Choices: {choices}")
    loader = cls()
    if path:
        loader.cache_dir = Path(path).parent
        loader.source_file = Path(path).name
    return loader


# ── System factory ──────────────────────────────────────────────────────────

def _resolve_systems(names: list[str]) -> list[Any]:
    """Resolve system names to memory system instances."""
    systems: list[Any] = []
    for name in names:
        name_lower = name.strip().lower()
        if name_lower == "mock":
            systems.append(MockMemorySystem())
        elif name_lower in ("trinity", "default"):
            try:
                from trinity.modules.second_brain.engine_core import SecondBrainV636

                sb = SecondBrainV636()

                class TrinityAdapter:
                    name: str = "Trinity"
                    _sb: Any

                    def __init__(self, brain: Any) -> None:
                        self._sb = brain
                        self._stored_bytes: int = 0

                    def ingest(self, conversation: list[dict[str, str]]) -> None:
                        for turn in conversation:
                            content = turn.get("content", "")
                            self._stored_bytes += len(content.encode())

                    def retrieve(self, query: str, top_k: int = 10) -> list[str]:
                        try:
                            diag = self._sb.run_diagnostics()
                            return [f"{k}: {v}" for k, v in list(diag.items())[:top_k] if k != "ALL_PASS"]
                        except Exception:
                            return [f"Trinity probe: {query}"]

                    def generate(self, query: str, context: list[str]) -> str:
                        return "\n".join(context[:5])

                    def stats(self) -> dict[str, Any]:
                        return {"stored_bytes": self._stored_bytes, "modules": getattr(self._sb, "total_modules", 0)}

                systems.append(TrinityAdapter(sb))
            except ImportError as e:
                print(f"[WARN] Trinity not available ({e}), falling back to Mock")
                systems.append(MockMemorySystem(name="Trinity-fallback"))
        else:
            print(f"[WARN] Unknown system '{name}', skipping. Available: mock, trinity")
    return systems


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"MemArena Benchmark Suite v{BENCH_VERSION} — Multi-system memory evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --dataset locomo
  %(prog)s --dataset longmem_eval --dataset-path ./data/lme.json
  %(prog)s --dataset locomo --systems trinity,mock --output reports/
  %(prog)s --dataset locomo_r1 --verbose
  %(prog)s --list-datasets
  %(prog)s --list-metrics
        """.strip(),
    )
    parser.add_argument("--dataset", "-d", type=str, default="locomo",
                        help="Dataset name (locomo, longmem_eval, memory_agent_bench, locomo_r1)")
    parser.add_argument("--dataset-path", type=str, default=None,
                        help="Local path to dataset file (overrides auto-download)")
    parser.add_argument("--systems", "-s", type=str, default="mock",
                        help="Comma-separated system names (mock, trinity)")
    parser.add_argument("--output", "-o", type=str, default="benchmark_reports",
                        help="Output directory for reports")
    parser.add_argument("--metrics", "-m", type=str, default=None,
                        help="Comma-separated metric names (default: all registered)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Verbose progress output")
    parser.add_argument("--list-datasets", action="store_true",
                        help="List available datasets and exit")
    parser.add_argument("--list-metrics", action="store_true",
                        help="List available metrics and exit")
    parser.add_argument("--version", action="store_true",
                        help="Print version and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"MemArena Benchmark Suite v{BENCH_VERSION}")
        return 0

    if args.list_datasets:
        print("Available datasets:")
        for name, cls in DATASET_REGISTRY.items():
            doc = (cls.__doc__ or cls.name).strip().split("\n")[0]
            print(f"  {name:25s} — {doc}")
        return 0

    if args.list_metrics:
        from benchmarks.metrics import MetricRegistry
        print("Available metrics:")
        for name in sorted(MetricRegistry.list_all()):
            metric = MetricRegistry.get(name)
            if metric:
                print(f"  {name:25s} — {metric.__class__.__name__}")
        return 0

    # Resolve
    dataset = _resolve_dataset(args.dataset, args.dataset_path)
    system_names = [s.strip() for s in args.systems.split(",")]
    systems = _resolve_systems(system_names)
    metric_list = [m.strip() for m in args.metrics.split(",")] if args.metrics else None

    if not systems:
        print("[ERROR] No valid systems specified")
        return 1

    print(f"MemArena v{BENCH_VERSION} — Dataset: {args.dataset}, Systems: {[s.name for s in systems]}")
    print(f"Output: {args.output}")
    print()

    # Run
    runner = ArenaRunner(dataset=dataset, systems=systems, metrics=metric_list, verbose=args.verbose)
    result = runner.run()

    # Report
    report_path = runner.report(args.output)

    # Print summary
    print()
    print("=" * 60)
    print(f"  MemArena Benchmark Complete")
    print(f"  Dataset: {result.dataset_name} ({result.num_samples} samples)")
    print(f"  Elapsed: {result.elapsed_seconds:.1f}s")
    print(f"  Report:  {report_path}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
