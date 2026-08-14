"""
benchmarks.datasets — Standard dataset loaders for memory system evaluation.

Supported formats: JSON, JSONL, Parquet.
Built-in datasets: LoCoMo (Long-Context Memory), LongMemEval, MemoryAgentBench.
"""

from __future__ import annotations

import json
import os
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import numpy as np


_CACHE_DIR: Path = Path(__file__).parent / ".dataset_cache"


@dataclass
class DatasetSample:
    """Single sample from a memory evaluation dataset."""

    sample_id: str
    conversation: list[dict[str, str]]
    question: str
    answer: str
    evidence_spans: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Base ────────────────────────────────────────────────────────────────────

class DatasetLoader(ABC):
    """Abstract dataset loader."""

    name: str = "base"
    url: str = ""
    source_file: str = ""

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else _CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._samples: list[DatasetSample] = []

    def download(self) -> Path:
        """Download dataset from URL if not cached."""
        dest = self.cache_dir / self.source_file
        if dest.exists():
            return dest
        if self.url:
            print(f"[DatasetLoader] Downloading {self.name} from {self.url} ...")
            urllib.request.urlretrieve(self.url, dest)
        return dest

    @abstractmethod
    def load(self) -> list[DatasetSample]:
        """Load and parse dataset into samples."""
        ...

    def __iter__(self) -> Iterator[DatasetSample]:
        return iter(self._samples)

    def __len__(self) -> int:
        return len(self._samples)

    def __getitem__(self, idx: int) -> DatasetSample:
        return self._samples[idx]


# ── Helpers ─────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file, one JSON object per line."""
    samples: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(json.loads(line))
    return samples


def _read_json(path: Path) -> list[dict[str, Any]]:
    """Read a JSON file, supports list-of-objects or single object."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else [data]


def _read_parquet(path: Path) -> list[dict[str, Any]]:
    """Read Parquet via pandas (optional dependency)."""
    try:
        import pandas as pd
    except ImportError:
        raise ImportError("pandas required for Parquet; install with: pip install pandas pyarrow")
    df = pd.read_parquet(path)
    return df.to_dict(orient="records")


# ── Concrete Datasets ───────────────────────────────────────────────────────

class LoCoMoDataset(DatasetLoader):
    """LoCoMo: Long-Context Memory benchmark (up to 100K turns)."""

    name = "LoCoMo"
    url = "https://huggingface.co/datasets/memarena/locomo/resolve/main/locomo_test.jsonl"
    source_file = "locomo_test.jsonl"

    def load(self) -> list[DatasetSample]:
        dest = self.download()
        raw = _read_jsonl(dest)
        self._samples = [
            DatasetSample(
                sample_id=item.get("id", f"locomo_{i}"),
                conversation=item.get("conversation", []),
                question=item.get("question", ""),
                answer=item.get("answer", ""),
                evidence_spans=item.get("evidence", []),
                metadata={k: v for k, v in item.items() if k not in {"id", "conversation", "question", "answer", "evidence"}},
            )
            for i, item in enumerate(raw)
        ]
        return self._samples


class LongMemEvalDataset(DatasetLoader):
    """LongMemEval: Multi-session long-term memory benchmark."""

    name = "LongMemEval"
    source_file = "longmem_eval_test.json"

    def load(self, file_path: str | Path | None = None) -> list[DatasetSample]:
        if file_path:
            dest = Path(file_path)
        else:
            dest = self.cache_dir / self.source_file
        if not dest.exists():
            raise FileNotFoundError(f"LongMemEval dataset not found at {dest}. Provide --dataset-path or place file at {dest}")

        raw_data = _read_json(dest)
        self._samples = [
            DatasetSample(
                sample_id=item.get("sample_id", f"lme_{i}"),
                conversation=item.get("history", []),
                question=item.get("probe", ""),
                answer=item.get("ground_truth", ""),
                evidence_spans=item.get("relevant_facts", []),
                metadata={k: v for k, v in item.items() if k not in {"sample_id", "history", "probe", "ground_truth", "relevant_facts"}},
            )
            for i, item in enumerate(raw_data)
        ]
        return self._samples


class LoCoMoR1Dataset(DatasetLoader):
    """LoCoMo-R1: Long-context memory with reasoning traces (DeepSeek-R1 style)."""

    name = "LoCoMo-R1"
    source_file = "locomo_r1_test.jsonl"

    def load(self, file_path: str | Path | None = None) -> list[DatasetSample]:
        if file_path:
            dest = Path(file_path)
        else:
            dest = self.cache_dir / self.source_file
        if not dest.exists():
            raise FileNotFoundError(f"{self.name} not found at {dest}")

        raw = _read_jsonl(dest)
        self._samples = [
            DatasetSample(
                sample_id=item.get("id", f"r1_{i}"),
                conversation=item.get("conversation", []),
                question=item.get("question", ""),
                answer=item.get("answer", ""),
                evidence_spans=item.get("evidence_spans", []),
                metadata={"reasoning_trace": item.get("reasoning_trace", ""),
                          **{k: v for k, v in item.items()
                             if k not in {"id", "conversation", "question", "answer", "evidence_spans", "reasoning_trace"}}},
            )
            for i, item in enumerate(raw)
        ]
        return self._samples


class MemoryAgentBenchDataset(DatasetLoader):
    """MemoryAgentBench (ICLR 2026): Agent memory stress test."""

    name = "MemoryAgentBench"
    source_file = "memory_agent_bench.jsonl"

    def load(self, file_path: str | Path | None = None) -> list[DatasetSample]:
        if file_path:
            dest = Path(file_path)
        else:
            dest = self.cache_dir / self.source_file
        if not dest.exists():
            raise FileNotFoundError(f"{self.name} not found at {dest}. Place file at {dest}")

        raw = _read_jsonl(dest)
        self._samples = [
            DatasetSample(
                sample_id=item.get("sample_id", f"mab_{i}"),
                conversation=item.get("turns", []),
                question=item.get("query", ""),
                answer=item.get("target", ""),
                evidence_spans=item.get("grounding", []),
                metadata={k: v for k, v in item.items() if k not in {"sample_id", "turns", "query", "target", "grounding"}},
            )
            for i, item in enumerate(raw)
        ]
        return self._samples
