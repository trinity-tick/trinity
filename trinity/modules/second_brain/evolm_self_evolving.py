"""P24: EvoLM Self-Evolving Rubric System — 2026.06.

# status: orphan (2026-08-15 audit, not in runtime path)
Co-evolved rubrics with feedback-driven adaptation.
Policy self-evaluation with per-dimension scoring and weighted pass/fail threshold.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CoEvolvedRubric:
    """A single evaluation rubric dimension that evolves over time.

    Each rubric tracks its version lineage for auditability.
    """
    dimension: str
    criteria: list[str]
    weight: float
    evolved_from_version: int = 0
    version: int = 1
    created_at: float = field(default_factory=time.time)
    performance_history: list[float] = field(default_factory=list)


@dataclass
class EvalReport:
    """Result of policy self-evaluation against a rubric set."""
    overall_score: float
    dimension_scores: dict[str, float]
    weighted_score: float
    rubric_versions: dict[str, int]
    passed: bool
    threshold: float
    recommendations: list[str] = field(default_factory=list)
    evaluated_at: float = field(default_factory=time.time)


class RubricEvolver:
    """Evolve rubrics based on performance feedback.

    Feedback-driven adaptation:
        - Low-scoring dimensions: tighten criteria, boost weight
        - High-scoring ceiling: relax criteria, reduce weight
        - Mid-range: gradual regression toward neutral weight
    """

    LOW: float = 0.4
    HIGH: float = 0.85
    MAX_HISTORY: int = 20

    def evolve(
        self,
        current_rubrics: list[CoEvolvedRubric],
        performance_feedback: dict[str, float],
    ) -> list[CoEvolvedRubric]:
        evolved: list[CoEvolvedRubric] = []

        for rubric in current_rubrics:
            score = performance_feedback.get(rubric.dimension)
            new_rubric = CoEvolvedRubric(
                dimension=rubric.dimension,
                criteria=list(rubric.criteria),
                weight=rubric.weight,
                evolved_from_version=rubric.version,
                version=rubric.version + 1,
                performance_history=list(rubric.performance_history),
            )

            if score is not None:
                new_rubric.performance_history.append(score)
                if len(new_rubric.performance_history) > self.MAX_HISTORY:
                    new_rubric.performance_history = new_rubric.performance_history[-self.MAX_HISTORY:]

                if score < self.LOW:
                    new_rubric.criteria.append(
                        f"[v{new_rubric.version}] Verify compliance with stricter standard"
                    )
                    new_rubric.weight = min(1.0, new_rubric.weight * 1.2)
                    logger.debug("EvoLM tighten: %s score=%.2f w=%.2f→%.2f",
                                 rubric.dimension, score, rubric.weight, new_rubric.weight)
                elif score > self.HIGH:
                    new_rubric.weight = max(0.05, new_rubric.weight * 0.9)
                    logger.debug("EvoLM relax: %s score=%.2f w=%.2f→%.2f",
                                 rubric.dimension, score, rubric.weight, new_rubric.weight)
                else:
                    new_rubric.weight = new_rubric.weight * 0.95 + 0.5 * 0.05

            evolved.append(new_rubric)

        total = sum(r.weight for r in evolved)
        if total > 0:
            for r in evolved:
                r.weight /= total

        logger.info("EvoLM evolved %d rubrics (v→v+1)", len(evolved))
        return evolved


class PolicySelfEvaluator:
    """Evaluate policy output against rubric set.

    Per-dimension scoring via heuristic keyword matching against criteria.
    Returns both raw and weighted scores.
    """

    def evaluate(
        self, response: str, rubrics: list[CoEvolvedRubric],
    ) -> dict[str, Any]:
        scores: dict[str, float] = {}
        details: dict[str, dict[str, Any]] = {}
        rl = response.lower()

        for rubric in rubrics:
            hits = 0
            matched: list[str] = []
            for criterion in rubric.criteria:
                keywords = [w for w in criterion.lower().split() if len(w) > 3]
                if not keywords:
                    continue
                if any(kw in rl for kw in keywords):
                    hits += 1
                    matched.append(criterion)

            base = min(1.0, len(rl.split()) / 50.0)
            crit_score = hits / max(len(rubric.criteria), 1)
            score = round(0.3 * base + 0.7 * crit_score, 3)

            scores[rubric.dimension] = score
            details[rubric.dimension] = {
                "score": score, "weight": rubric.weight,
                "criteria_matched": hits,
                "criteria_total": len(rubric.criteria),
                "matched_criteria": matched,
                "rubric_version": rubric.version,
            }

        return {"scores": scores, "details": details}

    def compute_weighted_score(
        self, scores: dict[str, float], rubrics: list[CoEvolvedRubric],
    ) -> float:
        total = 0.0
        ws = 0.0
        for rubric in rubrics:
            w = rubric.weight
            s = scores.get(rubric.dimension, 0.0)
            total += w * s
            ws += w
        return total / max(ws, 0.001)


DEFAULT_RUBRICS = [
    CoEvolvedRubric("accuracy", ["factually correct", "no hallucination", "grounded in context"], 0.30),
    CoEvolvedRubric("completeness", ["covers all required aspects", "no missing steps", "addresses query fully"], 0.25),
    CoEvolvedRubric("coherence", ["logical flow", "consistent reasoning", "no contradictions"], 0.20),
    CoEvolvedRubric("conciseness", ["no redundancy", "appropriate length", "relevant only"], 0.15),
    CoEvolvedRubric("constraint_adherence", ["follows format rules", "respects boundaries", "policy compliant"], 0.10),
]


def self_evaluate(
    policy_output: str,
    context: dict[str, Any] | None = None,
    rubric_set: list[CoEvolvedRubric] | None = None,
    threshold: float = 0.6,
) -> EvalReport:
    """Main EvoLM entry point: self-evaluate policy output.

    Args:
        policy_output: generated policy / agent response
        context: optional context metadata
        rubric_set: evaluation rubrics (default 5-dimension set if None)
        threshold: pass / fail threshold

    Returns:
        EvalReport with overall and per-dimension scores
    """
    if rubric_set is None:
        rubric_set = DEFAULT_RUBRICS

    evaluator = PolicySelfEvaluator()
    result = evaluator.evaluate(policy_output, rubric_set)
    scores = result["scores"]
    weighted = evaluator.compute_weighted_score(scores, rubric_set)

    recs: list[str] = []
    for rubric in rubric_set:
        ds = scores.get(rubric.dimension, 0.0)
        if ds < 0.4:
            recs.append(f"Improve {rubric.dimension}: score {ds:.2f} below threshold")

    report = EvalReport(
        overall_score=round(weighted, 3),
        dimension_scores=scores,
        weighted_score=round(weighted, 3),
        rubric_versions={r.dimension: r.version for r in rubric_set},
        passed=weighted >= threshold,
        threshold=threshold,
        recommendations=recs,
    )

    logger.info("EvoLM self_evaluate: overall=%.3f passed=%s", weighted, report.passed)
    return report


print("[P24] EvoLM SelfEvolving initialized — 2026.06 aligned")
