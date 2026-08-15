"""
# status: orphan (2026-08-15 audit, not in runtime path)
P2-2 CompressionEvaluator — Memory Compression Quality Assessment

Evaluates compression quality across multiple dimensions:
  - Compression Ratio (length reduction)
  - Entity Retention Rate (key entities preserved post-compression)
  - Semantic Similarity (Jaccard n-gram fallback; embedding_fn pluggable)
  - Information Density (entities per character)
  - Overall Score (weighted: 0.3×ratio + 0.4×entity_ret + 0.3×semantic)

Compatible with PromptCompressionAuditor (Layer 7a) —
accepts AuditorResult to incorporate rule-integrity data into evaluation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class EvalMetrics:
    """Single-pair evaluation result."""

    original_length: int
    compressed_length: int
    compression_ratio: float          # [0, 1] — 0 = no compression, 1 = extreme
    entity_retention: float           # [0, 1] — fraction of original entities preserved
    semantic_similarity: float        # [0, 1] — Jaccard / embedding cosine
    info_density_before: float        # entities / original_length
    info_density_after: float         # entities / compressed_length
    overall_score: float              # [0, 1] weighted composite
    # Metadata
    entities_found: int               # total in original
    entities_preserved: int           # matched in compressed
    entities_lost: List[str] = field(default_factory=list)
    # Auditor integration
    auditor_rule_preservation: Optional[float] = None  # from PromptCompressionAuditor
    auditor_risk_level: str = ""
    # Identification
    pair_id: str = ""


@dataclass
class BatchSummary:
    """Aggregated statistics from batch_evaluate."""

    total_pairs: int
    avg_compression_ratio: float
    avg_entity_retention: float
    avg_semantic_similarity: float
    avg_overall_score: float
    min_score: float
    max_score: float
    std_score: float
    score_distribution: Dict[str, int] = field(default_factory=dict)  # "excellent"/"good"/"fair"/"poor"
    per_pair_metrics: List[EvalMetrics] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Entity Extraction Engine (lightweight regex-based NER)
# ---------------------------------------------------------------------------

_ENTITY_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("EMAIL",       re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("URL",         re.compile(r"https?://[^\s<>\"{}|\\^`\[\]]+")),
    ("DATE",        re.compile(
        r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}"
    )),
    ("AMOUNT",      re.compile(r"[$€£¥]\s*\d+(?:[,.]\d+)*|\d+(?:[,.]\d+)*\s*(?:元|USD|EUR|GBP|JPY|CNY|dollars?|euros?)")),
    ("PERCENT",     re.compile(r"\d+(?:\.\d+)?%")),
    ("VERSION",     re.compile(r"v?\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9_]+)?")),
    ("PROPER_NAME", re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")),
    ("PHONE",       re.compile(r"\+?\d[\d\s\-().]{7,}\d")),
    ("NUMBER",      re.compile(r"\b\d{4,}\b")),  # standalone large numbers (years, IDs, etc.)
]

# Python keywords + common stop words to avoid as "entities"
_STOP_ENTITIES = frozenset({
    "The", "This", "That", "There", "These", "Those", "They", "Their",
    "With", "From", "About", "After", "Before", "Would", "Could",
    "Should", "Other", "Such", "Some", "Many", "Each", "Every",
    "Which", "Where", "When", "While", "What", "Also", "Then", "Than",
    "Only", "Just", "More", "Most", "Much", "Very", "Over", "Into",
    "Like", "Been", "Being", "Does", "Have", "Were", "Will", "Still",
    "However", "Therefore", "Because", "Between", "Through", "During",
    "Without", "Within", "Among", "Although", "Another",
})


def extract_entities(text: str) -> List[str]:
    """Extract named entities from text using regex patterns.

    Returns deduplicated list of entity strings (stable order).
    """
    seen: Dict[str, None] = {}
    for _etype, pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            entity = match.group(0)
            # Filter stop entities from proper names
            if _etype == "PROPER_NAME" and entity in _STOP_ENTITIES:
                continue
            seen[entity] = None
    return list(seen.keys())


# ---------------------------------------------------------------------------
# Semantic Similarity — Jaccard n-gram (fallback when no embedding_fn)
# ---------------------------------------------------------------------------

def _char_ngrams(text: str, n: int = 3) -> Counter:
    """Character n-gram counter for text comparison."""
    clean = re.sub(r"\s+", " ", text.lower().strip())
    return Counter(clean[i : i + n] for i in range(max(0, len(clean) - n + 1)))


def jaccard_similarity(a: str, b: str, n: int = 3) -> float:
    """Jaccard similarity on character n-grams."""
    nga = _char_ngrams(a, n)
    ngb = _char_ngrams(b, n)
    if not nga and not ngb:
        return 1.0
    intersection = sum((nga & ngb).values())
    union = sum((nga | ngb).values())
    return intersection / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# CompressionEvaluator
# ---------------------------------------------------------------------------

class CompressionEvaluator:
    """Multi-dimensional memory compression quality evaluator.

    Weights (configurable):
        compression_ratio  × 0.30
        entity_retention   × 0.40
        semantic_similarity × 0.30

    Usage:
        evaluator = CompressionEvaluator()
        metrics = evaluator.evaluate(original, compressed)

        # With auditor integration:
        from trinity.daemon.prompt_compression_auditor import AuditorResult
        metrics = evaluator.evaluate(original, compressed, auditor_result=audit)

        summary = evaluator.batch_evaluate(pairs)
        report = evaluator.generate_report()
    """

    def __init__(
        self,
        w_compression: float = 0.30,
        w_entity: float = 0.40,
        w_semantic: float = 0.30,
        embedding_fn: Optional[Callable[[str], np.ndarray]] = None,
        semantic_ngram_n: int = 3,
    ):
        total = w_compression + w_entity + w_semantic
        self.w_compression = w_compression / total
        self.w_entity = w_entity / total
        self.w_semantic = w_semantic / total
        self.embedding_fn = embedding_fn
        self.semantic_ngram_n = semantic_ngram_n

        # Internal storage for report generation
        self._last_batch: Optional[List[EvalMetrics]] = None
        self._last_summary: Optional[BatchSummary] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        original_text: str,
        compressed_text: str,
        auditor_result: Any = None,
        pair_id: str = "",
    ) -> EvalMetrics:
        """Evaluate a single (original, compressed) text pair.

        Args:
            original_text:   Full text before compression.
            compressed_text: Text after compression.
            auditor_result:  Optional ``AuditorResult`` from
                             ``PromptCompressionAuditor`` for rule-integrity data.
            pair_id:         Optional identifier for this pair.

        Returns:
            EvalMetrics with all quality dimensions.
        """
        orig_len = len(original_text)
        comp_len = len(compressed_text)

        # --- Compression Ratio ---
        # ratio = how much was removed; 0 = no change, 1 = all removed
        if orig_len > 0:
            compression_ratio = 1.0 - min(comp_len / orig_len, 1.0)
        else:
            compression_ratio = 0.0

        # --- Entity Retention ---
        orig_entities = extract_entities(original_text)
        comp_entities = set(extract_entities(compressed_text))
        entities_found = len(orig_entities)
        entities_preserved = sum(1 for e in orig_entities if e in comp_entities)
        entity_retention = (
            entities_preserved / entities_found if entities_found > 0 else 1.0
        )
        entities_lost = [e for e in orig_entities if e not in comp_entities]

        # --- Semantic Similarity ---
        if self.embedding_fn:
            vec_orig = self.embedding_fn(original_text)
            vec_comp = self.embedding_fn(compressed_text)
            norm_a = np.linalg.norm(vec_orig)
            norm_b = np.linalg.norm(vec_comp)
            if norm_a > 0 and norm_b > 0:
                semantic_similarity = float(np.dot(vec_orig, vec_comp) / (norm_a * norm_b))
            else:
                semantic_similarity = 0.0
        else:
            semantic_similarity = jaccard_similarity(
                original_text, compressed_text, self.semantic_ngram_n
            )

        # --- Information Density ---
        info_density_before = entities_found / orig_len if orig_len > 0 else 0.0
        info_density_after = (
            len(comp_entities) / comp_len if comp_len > 0 else 0.0
        )

        # --- Overall Score ---
        overall_score = (
            self.w_compression * compression_ratio
            + self.w_entity * entity_retention
            + self.w_semantic * semantic_similarity
        )

        # --- Auditor Integration ---
        auditor_rule_preservation: Optional[float] = None
        auditor_risk_level = ""
        if auditor_result is not None:
            try:
                reports = getattr(auditor_result, "rule_reports", [])
                if reports:
                    preserved = sum(
                        1 for r in reports
                        if getattr(r, "status", None) is not None
                        and r.status.value == "PRESERVED"
                    )
                    auditor_rule_preservation = preserved / len(reports) if reports else None
                risk = getattr(auditor_result, "attack_report", None)
                if risk is not None:
                    auditor_risk_level = getattr(risk, "risk_level", "")
                    if hasattr(auditor_risk_level, "value"):
                        auditor_risk_level = auditor_risk_level.value
            except Exception:
                pass  # graceful degradation on auditor parse errors

        return EvalMetrics(
            original_length=orig_len,
            compressed_length=comp_len,
            compression_ratio=round(compression_ratio, 4),
            entity_retention=round(entity_retention, 4),
            semantic_similarity=round(semantic_similarity, 4),
            info_density_before=round(info_density_before, 6),
            info_density_after=round(info_density_after, 6),
            overall_score=round(overall_score, 4),
            entities_found=entities_found,
            entities_preserved=entities_preserved,
            entities_lost=entities_lost[:20],
            auditor_rule_preservation=(
                round(auditor_rule_preservation, 4)
                if auditor_rule_preservation is not None
                else None
            ),
            auditor_risk_level=str(auditor_risk_level),
            pair_id=pair_id,
        )

    def batch_evaluate(
        self,
        pairs: List[Tuple[str, str]],
        auditor_results: Optional[List[Any]] = None,
    ) -> BatchSummary:
        """Batch evaluate multiple (original, compressed) pairs.

        Args:
            pairs:           List of (original_text, compressed_text) tuples.
            auditor_results: Optional parallel list of AuditorResult objects.

        Returns:
            BatchSummary with aggregated statistics.
        """
        metrics_list: List[EvalMetrics] = []
        for i, (orig, comp) in enumerate(pairs):
            auditor = auditor_results[i] if auditor_results and i < len(auditor_results) else None
            m = self.evaluate(orig, comp, auditor_result=auditor, pair_id=f"pair_{i:04d}")
            metrics_list.append(m)

        n = len(metrics_list)
        if n == 0:
            self._last_batch = []
            self._last_summary = BatchSummary(
                total_pairs=0,
                avg_compression_ratio=0.0,
                avg_entity_retention=0.0,
                avg_semantic_similarity=0.0,
                avg_overall_score=0.0,
                min_score=0.0,
                max_score=0.0,
                std_score=0.0,
            )
            return self._last_summary

        scores = [m.overall_score for m in metrics_list]
        compression_ratios = [m.compression_ratio for m in metrics_list]
        entity_retentions = [m.entity_retention for m in metrics_list]
        semantic_sims = [m.semantic_similarity for m in metrics_list]

        distribution: Dict[str, int] = {"excellent": 0, "good": 0, "fair": 0, "poor": 0}
        for s in scores:
            if s >= 0.80:
                distribution["excellent"] += 1
            elif s >= 0.60:
                distribution["good"] += 1
            elif s >= 0.40:
                distribution["fair"] += 1
            else:
                distribution["poor"] += 1

        summary = BatchSummary(
            total_pairs=n,
            avg_compression_ratio=round(float(np.mean(compression_ratios)), 4),
            avg_entity_retention=round(float(np.mean(entity_retentions)), 4),
            avg_semantic_similarity=round(float(np.mean(semantic_sims)), 4),
            avg_overall_score=round(float(np.mean(scores)), 4),
            min_score=round(float(np.min(scores)), 4),
            max_score=round(float(np.max(scores)), 4),
            std_score=round(float(np.std(scores)), 4),
            score_distribution=distribution,
            per_pair_metrics=metrics_list,
        )
        self._last_batch = metrics_list
        self._last_summary = summary
        return summary

    def generate_report(self, output_path: Optional[str] = None) -> str:
        """Generate a Markdown evaluation report from the last batch.

        Args:
            output_path: If provided, write report to this file path.

        Returns:
            Report string in Markdown format.
        """
        summary = self._last_summary
        if summary is None:
            report = (
                "# Compression Evaluation Report\n\n"
                "**No evaluation data available.**\n"
                "Run `batch_evaluate()` or `evaluate()` first.\n"
            )
            if output_path:
                Path(output_path).write_text(report, encoding="utf-8")
            return report

        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        lines: List[str] = [
            f"# Compression Evaluation Report",
            f"**Generated**: {ts}",
            f"**Total Pairs**: {summary.total_pairs}",
            "",
            "## Aggregate Metrics",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Average Compression Ratio | {summary.avg_compression_ratio:.2%} |",
            f"| Average Entity Retention | {summary.avg_entity_retention:.2%} |",
            f"| Average Semantic Similarity | {summary.avg_semantic_similarity:.2%} |",
            f"| Average Overall Score | {summary.avg_overall_score:.4f} |",
            f"| Min / Max Score | {summary.min_score:.4f} / {summary.max_score:.4f} |",
            f"| Std Dev | {summary.std_score:.4f} |",
            "",
            "## Score Distribution",
            "",
        ]

        dist = summary.score_distribution
        lines.append("| Grade | Count | Criteria |")
        lines.append("|---|---|---|")
        lines.append(f"| Excellent | {dist.get('excellent', 0)} | score ≥ 0.80 |")
        lines.append(f"| Good      | {dist.get('good', 0)} | score ≥ 0.60 |")
        lines.append(f"| Fair      | {dist.get('fair', 0)} | score ≥ 0.40 |")
        lines.append(f"| Poor      | {dist.get('poor', 0)} | score < 0.40 |")
        lines.append("")

        lines.append("## Per-Pair Details")
        lines.append("")
        lines.append(
            "| ID | Orig Len | Comp Len | CmpRatio | EntRet | SemSim | Score | Entities | Grade |"
        )
        lines.append(
            "|---|---|---|---|---|---|---|---|---|"
        )
        for m in summary.per_pair_metrics:
            if m.overall_score >= 0.80:
                grade = "Excellent"
            elif m.overall_score >= 0.60:
                grade = "Good"
            elif m.overall_score >= 0.40:
                grade = "Fair"
            else:
                grade = "Poor"
            lines.append(
                f"| {m.pair_id} | {m.original_length} | {m.compressed_length}"
                f" | {m.compression_ratio:.2%} | {m.entity_retention:.2%}"
                f" | {m.semantic_similarity:.2%} | {m.overall_score:.4f}"
                f" | {m.entities_preserved}/{m.entities_found} | {grade} |"
            )

        lines.append("")
        lines.append("## Weights")
        lines.append(f"- Compression Ratio: {self.w_compression:.0%}")
        lines.append(f"- Entity Retention:  {self.w_entity:.0%}")
        lines.append(f"- Semantic Similarity: {self.w_semantic:.0%}")
        lines.append("")

        report = "\n".join(lines)

        if output_path:
            Path(output_path).write_text(report, encoding="utf-8")

        return report


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Running CompressionEvaluator self-tests...")

    evaluator = CompressionEvaluator()

    # Test 1: Single pair evaluation
    original = (
        "John Smith from Acme Corporation reported that the quarterly revenue "
        "reached $1,250,000 on 2024-03-15. The CEO Alice Johnson approved the "
        "budget for Q2 2024 at the board meeting in New York. Contact: "
        "john.smith@acme.com or call +1-555-123-4567. Version 2.3.1 deployed."
    )
    compressed = (
        "John Smith (Acme Corp): Q1 revenue $1.25M on 2024-03-15. "
        "CEO Alice Johnson approved Q2 budget. v2.3.1 deployed."
    )

    m = evaluator.evaluate(original, compressed, pair_id="test_01")
    print(f"\n  Single evaluate OK:")
    print(f"    compression_ratio={m.compression_ratio:.4f}")
    print(f"    entity_retention={m.entity_retention:.4f} ({m.entities_preserved}/{m.entities_found})")
    print(f"    semantic_similarity={m.semantic_similarity:.4f}")
    print(f"    overall_score={m.overall_score:.4f}")
    print(f"    entities_lost={m.entities_lost}")

    # Test 2: Batch evaluation
    pairs = [
        (original, compressed),
        (
            "Dr. Maria Garcia published her paper on Neural Architecture Search at NeurIPS 2025 in Vancouver. "
            "The study shows 15.3% improvement over baseline. Contact at maria@stanford.edu.",
            "Maria Garcia: NAS paper at NeurIPS 2025, 15.3% improvement. maria@stanford.edu.",
        ),
        (
            "The database contains 10,000 records from Shanghai branch. "
            "Transaction ID: TXN-982341. Amount: ¥500,000. Processed by Zhang Wei.",
            "DB: 10000 records, TXN-982341, ¥500000. Zhang Wei.",
        ),
    ]

    summary = evaluator.batch_evaluate(pairs)
    print(f"\n  Batch evaluate OK: {summary.total_pairs} pairs")
    print(f"    avg_score={summary.avg_overall_score:.4f}")
    print(f"    distribution={summary.score_distribution}")

    # Test 3: Report generation
    report = evaluator.generate_report()
    print(f"\n  Report generated: {len(report)} chars")

    print("\nAll self-tests passed.")
