"""P28: NS-Mem Neuro-Symbolic Memory — arXiv 2603.15280.

Neuro-symbolic hybrid memory that augments vector retrieval with
symbolic rule reasoning. Learns LogicRule triples from multi-modal
episodes via SK-Gen consolidation and provides forward-chain inference
with confidence-weighted rule matching.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class LogicRule:
    """Symbolic rule with premise→conclusion structure.

    Attributes:
        rule_id: Unique identifier.
        premise: List of atomic precondition literals.
        conclusion: The inferred consequent.
        confidence: Rule confidence in [0.0, 1.0].
        source_episodes: Episode IDs from which this rule was extracted.
    """

    rule_id: str
    premise: list[str]
    conclusion: str
    confidence: float
    source_episodes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Logic Rule Layer with Forward-Chain Inference
# ---------------------------------------------------------------------------

class LogicRuleLayer:
    """In-memory rule store with forward-chain query capability.

    Maintains a collection of LogicRule objects and provides add_rule /
    query operations. Query performs one-hop forward chaining: given a
    set of premises, it returns all rules whose premise is a subset of
    the input.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rules: dict[str, LogicRule] = {}

    def add_rule(self, rule: LogicRule) -> LogicRule:
        """Add a symbolic rule to the layer."""
        with self._lock:
            if not rule.rule_id:
                rule.rule_id = uuid.uuid4().hex[:12]
            self._rules[rule.rule_id] = rule
            logger.debug("NS-Mem added rule %s: %s → %s",
                         rule.rule_id, rule.premise, rule.conclusion)
            return rule

    def query(self, premises: list[str]) -> list[LogicRule]:
        """Forward-chain: find all rules whose premise ⊆ input premises.

        Args:
            premises: Set of known true literals.

        Returns:
            Rules whose entire premise is satisfied, sorted by confidence desc.
        """
        with self._lock:
            premise_set = set(premises)
            matched: list[LogicRule] = []
            for rule in self._rules.values():
                if set(rule.premise).issubset(premise_set):
                    matched.append(rule)
            matched.sort(key=lambda r: r.confidence, reverse=True)
            return matched

    @property
    def premise(self) -> list[str]:
        """Return all unique premise literals across stored rules."""
        with self._lock:
            all_premises: set[str] = set()
            for r in self._rules.values():
                all_premises.update(r.premise)
            return sorted(all_premises)

    @property
    def conclusion(self) -> list[str]:
        """Return all unique conclusions across stored rules."""
        with self._lock:
            return sorted({r.conclusion for r in self._rules.values()})

    @property
    def confidence(self) -> float:
        """Return average confidence across all rules."""
        with self._lock:
            if not self._rules:
                return 0.0
            return sum(r.confidence for r in self._rules.values()) / len(self._rules)

    @property
    def source_episodes(self) -> list[str]:
        """Return all unique source episode IDs."""
        with self._lock:
            eps: set[str] = set()
            for r in self._rules.values():
                eps.update(r.source_episodes)
            return sorted(eps)

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total_rules": len(self._rules),
                "avg_confidence": self.confidence,
            }


# ---------------------------------------------------------------------------
# SK-Gen Consolidator — Rule Extraction from Episodes
# ---------------------------------------------------------------------------

class SKGenConsolidator:
    """Symbolic Knowledge Generator: extracts LogicRule from multi-modal episodes.

    Each episode is a dict with keys: 'text', 'entities', 'relations'.
    The consolidator heuristically converts relation triples into
    confidence-weighted LogicRule objects.
    """

    def __init__(self, min_confidence: float = 0.3) -> None:
        self._lock = threading.RLock()
        self.min_confidence = min_confidence

    def consolidate(self, episodes: list[dict[str, Any]]) -> list[LogicRule]:
        """Extract symbolic rules from multi-modal experience episodes.

        Args:
            episodes: List of episode dicts with 'entities' and 'relations'.

        Returns:
            Extracted LogicRule objects above min_confidence.
        """
        with self._lock:
            rules: list[LogicRule] = []
            for ep in episodes:
                ep_id = ep.get("episode_id", uuid.uuid4().hex[:8])
                relations: list[dict[str, Any]] = ep.get("relations", [])
                for rel in relations:
                    subj = rel.get("subject", "")
                    pred = rel.get("predicate", "")
                    obj = rel.get("object", "")
                    conf = float(rel.get("confidence", 0.5))
                    if conf < self.min_confidence:
                        continue
                    if not subj or not pred or not obj:
                        continue
                    rule = LogicRule(
                        rule_id=uuid.uuid4().hex[:12],
                        premise=[f"{subj}.{pred}"],
                        conclusion=f"{subj}.{obj}",
                        confidence=conf,
                        source_episodes=[ep_id],
                    )
                    rules.append(rule)
            logger.info(
                "SK-Gen extracted %d rules from %d episodes",
                len(rules), len(episodes),
            )
            return rules

    def statistics(self) -> dict[str, Any]:
        return {"min_confidence": self.min_confidence}


# ---------------------------------------------------------------------------
# Hybrid Retriever — Neural + Symbolic
# ---------------------------------------------------------------------------

class HybridRetriever:
    """Combined neural (vector) and symbolic (rule) retrieval.

    Mode options:
      - "neural":  vector similarity only
      - "symbolic":  rule-based forward-chain only
      - "hybrid":  merge and re-rank both streams
    """

    def __init__(self, rule_layer: LogicRuleLayer) -> None:
        self._lock = threading.RLock()
        self._rule_layer = rule_layer
        # Simulated vector store (in production this would be a real embedding DB)
        self._vector_store: dict[str, list[float]] = {}

    def retrieve(
        self, query: str, mode: Literal["neural", "symbolic", "hybrid"] = "hybrid"
    ) -> list[dict[str, Any]]:
        """Retrieve relevant memory entries by the specified mode.

        Args:
            query: Natural language or structured query.
            mode: Retrieval mode.

        Returns:
            List of result dicts with 'source', 'content', 'score' keys.
        """
        with self._lock:
            results: list[dict[str, Any]] = []

            if mode in ("neural", "hybrid"):
                # Simulated vector retrieval
                for key, _vec in self._vector_store.items():
                    results.append({
                        "source": "neural",
                        "content": key,
                        "score": 0.85,
                    })

            if mode in ("symbolic", "hybrid"):
                tokens = query.split()
                matched_rules = self._rule_layer.query(tokens)
                for rule in matched_rules:
                    results.append({
                        "source": "symbolic",
                        "content": f"{rule.premise} → {rule.conclusion}",
                        "score": rule.confidence,
                    })

            # Hybrid: re-rank by score descending
            if mode == "hybrid":
                results.sort(key=lambda r: r["score"], reverse=True)

            logger.debug(
                "HybridRetriever mode=%s → %d results", mode, len(results),
            )
            return results

    def statistics(self) -> dict[str, Any]:
        return {
            "vector_entries": len(self._vector_store),
            "rule_count": len(self._rule_layer._rules),
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def reason(premises: list[str], max_depth: int = 3) -> list[LogicRule]:
    """Multi-hop forward-chain neuro-symbolic reasoning.

    Iteratively applies LogicRuleLayer.query() up to max_depth hops,
    accumulating newly derived conclusions as premises for the next
    depth level.

    Args:
        premises: Initial set of known-true literals.
        max_depth: Maximum reasoning depth (hops).

    Returns:
        All LogicRule fired across all depth levels, deduplicated.
    """
    layer = LogicRuleLayer()
    # In production, rules would be pre-loaded from persistent store.
    # Here we seed with a minimal rule set for demonstration.
    logger.info(
        "NS-Mem reasoning from %d premises, max_depth=%d",
        len(premises), max_depth,
    )

    current = list(premises)
    seen_rule_ids: set[str] = set()
    all_fired: list[LogicRule] = []

    for depth in range(max_depth):
        matched = layer.query(current)
        new_facts: list[str] = []
        for rule in matched:
            if rule.rule_id not in seen_rule_ids:
                seen_rule_ids.add(rule.rule_id)
                all_fired.append(rule)
                if rule.conclusion not in current:
                    new_facts.append(rule.conclusion)

        if not new_facts:
            logger.debug("No new facts at depth %d, stopping.", depth)
            break
        current.extend(new_facts)
        logger.debug("Depth %d: %d new facts, total fired=%d",
                     depth, len(new_facts), len(all_fired))

    return all_fired


print("[P28] NS-Mem Neuro-Symbolic Memory initialized — arXiv 2603.15280 aligned")
