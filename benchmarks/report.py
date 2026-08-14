"""
benchmarks.report — Multi-system benchmark report generator.

Outputs Markdown tables, radar chart data (JSON), and raw JSON.
Designed for side-by-side memory system comparison.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from benchmarks.arena import ArenaResult, SystemRunResult

from benchmarks.metrics import MetricResult


# ── Helpers ─────────────────────────────────────────────────────────────────

def _agg(system_runs: list[SystemRunResult], metric_name: str) -> tuple[float, float, int]:
    """Aggregate (mean, stdev, count) for a named metric across runs."""
    values: list[float] = []
    for srr in system_runs:
        for m in srr.metrics:
            if m.name == metric_name:
                values.append(m.value)
    if not values:
        return (0.0, 0.0, 0)
    return (
        statistics.mean(values),
        statistics.stdev(values) if len(values) > 1 else 0.0,
        len(values),
    )


def _extract_metric_names(system_results: dict[str, list[SystemRunResult]]) -> list[str]:
    """Extract sorted unique metric names from results."""
    names: set[str] = set()
    for runs in system_results.values():
        for srr in runs:
            for m in srr.metrics:
                names.add(m.name)
    return sorted(names)


# ── Radar Data ───────────────────────────────────────────────────────────────

@dataclass
class RadarData:
    """Structured radar-chart data (JSON-serializable)."""

    labels: list[str] = field(default_factory=list)
    systems: list[dict[str, Any]] = field(default_factory=list)
    # Each system: {"name": str, "scores": list[float]}


def build_radar(result: ArenaResult) -> RadarData:
    """Build radar chart data from ArenaResult."""
    metric_names = _extract_metric_names(result.system_results)
    radar = RadarData(labels=metric_names)

    for sys_name, runs in result.system_results.items():
        scores: list[float] = []
        for mn in metric_names:
            mean, _, _ = _agg(runs, mn)
            scores.append(round(mean, 4))
        radar.systems.append({"name": sys_name, "scores": scores})
    return radar


# ── Report Generator ─────────────────────────────────────────────────────────

class BenchmarkReport:
    """Generates Markdown + JSON + radar-data reports."""

    def __init__(self, result: ArenaResult) -> None:
        self.result = result

    def generate(self, output_dir: Path) -> Path:
        """Generate all report artifacts under output_dir."""
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"memarena_{self.result.dataset_name}_{timestamp}"

        # 1. Markdown report
        md_path = output_dir / f"{stem}.md"
        md_path.write_text(self._build_markdown(), encoding="utf-8")

        # 2. JSON report
        json_path = output_dir / f"{stem}.json"
        json_path.write_text(self._build_json(), encoding="utf-8")

        # 3. Radar data
        radar_path = output_dir / f"{stem}_radar.json"
        radar = build_radar(self.result)
        radar_path.write_text(
            json.dumps({"labels": radar.labels, "systems": radar.systems}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        print(f"[Report] Generated: {md_path}")
        return md_path

    def _build_markdown(self) -> str:
        r = self.result
        metric_names = _extract_metric_names(r.system_results)
        lines: list[str] = []

        lines.append(f"# MemArena Benchmark Report")
        lines.append(f"")
        lines.append(f"- **Dataset**: {r.dataset_name}")
        lines.append(f"- **Samples**: {r.num_samples}")
        lines.append(f"- **Systems**: {', '.join(r.system_results.keys())}")
        lines.append(f"- **Elapsed**: {r.elapsed_seconds:.1f}s")
        lines.append(f"- **Generated**: {datetime.now().isoformat()}")
        lines.append(f"")

        # Summary table
        lines.append(f"## Metric Summary")
        lines.append(f"")
        header = "| Metric | " + " | ".join(r.system_results.keys()) + " |"
        sep = "|" + "|".join([" --- " for _ in range(len(r.system_results) + 1)]) + "|"
        lines.append(header)
        lines.append(sep)

        for mn in metric_names:
            row_parts = [mn]
            for sys_name in r.system_results:
                mean, stdev, count = _agg(r.system_results[sys_name], mn)
                cell = f"{mean:.4f}"
                if stdev > 0:
                    cell += f" ± {stdev:.4f}"
                cell += f" (n={count})"
                row_parts.append(cell)
            lines.append("| " + " | ".join(row_parts) + " |")

        lines.append(f"")

        # Winner declaration
        lines.append(f"## Ranking")
        lines.append(f"")
        for mn in metric_names:
            scores: list[tuple[str, float]] = []
            for sys_name in r.system_results:
                mean, _, _ = _agg(r.system_results[sys_name], mn)
                scores.append((sys_name, mean))
            scores.sort(key=lambda x: x[1], reverse=True)
            ranking = " > ".join(f"{name} ({s:.4f})" for name, s in scores)
            lines.append(f"- **{mn}**: {ranking}")

        lines.append(f"")

        # Best per system
        lines.append(f"## Per-System Breakdown")
        lines.append(f"")
        for sys_name, runs in r.system_results.items():
            lines.append(f"### {sys_name}")
            lines.append(f"")
            for mn in metric_names:
                mean, stdev, count = _agg(runs, mn)
                lines.append(f"- **{mn}**: {mean:.4f} ± {stdev:.4f} (n={count})")
            avg_lat = statistics.mean(srr.latency_ms for srr in runs if srr.latency_ms > 0)
            lines.append(f"- **Avg Latency**: {avg_lat:.1f} ms")
            lines.append(f"")

        return "\n".join(lines)

    def _build_json(self) -> str:
        r = self.result
        metric_names = _extract_metric_names(r.system_results)
        output: dict[str, Any] = {
            "dataset": r.dataset_name,
            "num_samples": r.num_samples,
            "elapsed_s": r.elapsed_seconds,
            "generated_at": datetime.now().isoformat(),
            "systems": {},
            "ranking": {},
        }
        for sys_name, runs in r.system_results.items():
            sys_data: dict[str, Any] = {"metrics": {}}
            for mn in metric_names:
                mean, stdev, count = _agg(runs, mn)
                sys_data["metrics"][mn] = {"mean": mean, "stdev": stdev, "n": count}
            sys_data["avg_latency_ms"] = statistics.mean(srr.latency_ms for srr in runs if srr.latency_ms > 0)
            output["systems"][sys_name] = sys_data

        for mn in metric_names:
            ranking: list[dict[str, Any]] = []
            scores = [(name, _agg(r.system_results[name], mn)[0]) for name in r.system_results]
            scores.sort(key=lambda x: x[1], reverse=True)
            for rank, (name, score) in enumerate(scores, 1):
                ranking.append({"rank": rank, "system": name, "score": score})
            output["ranking"][mn] = ranking

        return json.dumps(output, indent=2, ensure_ascii=False)
