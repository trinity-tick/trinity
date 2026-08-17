"""
Trinity Identity — Hybrid RAG + RLM Router
===========================================
Intelligent query router that selects retrieval strategy based on
query type. Supports four modes:

- fact_query  → RAG (precise retrieval)
- identity_query → Identity Anchors (anchor-first)
- fuzzy_query → RLM (generative retrieval)
- hybrid_query → RAG + Anchors fusion
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum, auto


class QueryType(Enum):
    """Query classification for routing."""
    FACT = auto()          # e.g. "How many users?"
    IDENTITY = auto()      # e.g. "Who am I?", "What are my values?"
    FUZZY = auto()         # e.g. "Tell me about..."
    HYBRID = auto()        # e.g. "As an agent, what did I learn about..."

    @classmethod
    def from_string(cls, s: str) -> "QueryType":
        mapping = {
            "fact": cls.FACT,
            "identity": cls.IDENTITY,
            "fuzzy": cls.FUZZY,
            "hybrid": cls.HYBRID,
        }
        return mapping.get(s.lower(), cls.FACT)


class HybridRouter:
    """Routes queries to optimal retrieval strategy.

    Decision logic:
    - Fact queries ("how many", "when was", "list all") → RAG
    - Identity queries ("who am I", "what are my", "my values") → Anchors
    - Fuzzy/open-ended queries → RLM
    - Mixed queries (identity + fact) → Fusion (RAG + Anchors)
    """

    # Regex patterns for query classification
    _FACT_PATTERNS: List[re.Pattern] = [
        re.compile(r"\b(how many|count|catalog|enumerate|list|when|where|which file|what is the date)\b", re.I),
        re.compile(r"\b(retrieve|look up|find|search|query)\b.*\b(document|record|entry|item)\b", re.I),
    ]

    _IDENTITY_PATTERNS: List[re.Pattern] = [
        re.compile(r"\b(who am i|what am i|my (core )?(personality|values|identity|beliefs|traits|goals|rules|purpose))\b", re.I),
        re.compile(r"\b(constitutional|value specification|behavioral rule|define me|describe yourself)\b", re.I),
        re.compile(r"\b(anchor|profile|reconstruct).*identity\b", re.I),
    ]

    _HYBRID_PATTERNS: List[re.Pattern] = [
        re.compile(r"\b(as (an|my) (agent|assistant))\b.*\b(learn|know|remember|tell)\b", re.I),
        re.compile(r"\b(my|our).*\b(identity|values?).*\b(and|with|about).*\b(memory|knowledge|data)\b", re.I),
    ]

    _TEMPORAL_PATTERNS: List[re.Pattern] = [
        re.compile(r"\b(when|what time|schedule|routine|daily|weekly|active hours?|timezone|last seen|temporal)\b.*\b(pattern|anchor|rhythm|session)\b", re.I),
        re.compile(r"\b(is this (a )?(normal|typical|expected|usual))\b.*\b(time|hour|schedule|pattern)\b", re.I),
        re.compile(r"\b(temporal|time-based).*drift\b", re.I),
        re.compile(r"\b(access|login).*pattern.*(anomal|suspicious|unusual|odd|strange)\b", re.I),
    ]

    def __init__(self):
        # Route accuracy tracking (v8.6.0)
        self._route_history: List[Dict[str, Any]] = []
        self._route_stats: Dict[str, Dict[str, int]] = {
            qtype: {"correct": 0, "total": 0}
            for qtype in ["FACT", "IDENTITY", "FUZZY", "HYBRID"]
        }

    # ── Keyword Classifier (v8.6.0) ─────────────────────────────────

    # Keyword weights for multi-dimensional classification
    _KEYWORD_WEIGHTS: Dict[str, Dict[str, List[str]]] = {
        "IDENTITY": {
            "strong": [
                "who am i", "my identity", "my personality", "my values",
                "my core", "define me", "describe yourself", "anchor",
                "constitutional", "behavioral rule",
            ],
            "weak": [
                "identity", "self", "personality", "beliefs", "traits",
                "goals", "purpose", "profile", "character",
            ],
        },
        "FACT": {
            "strong": [
                "how many", "list all", "enumerate", "catalog",
                "when was", "where is", "which file", "count",
            ],
            "weak": [
                "find", "search", "look up", "retrieve", "query",
                "document", "record", "how much", "what is",
            ],
        },
        "FUZZY": {
            "strong": [
                "tell me about", "explain", "describe", "summarize",
                "what do you think", "how would you",
            ],
            "weak": [
                "about", "why", "how", "opinion", "suggestion",
            ],
        },
    }

    def _keyword_classify(self, query: str) -> Dict[str, float]:
        """Lightweight keyword-based classification with co-occurrence scoring.

        Computes scores for IDENTITY / FACT / FUZZY dimensions based on
        weighted keyword hits. Strong keywords contribute 0.4 each,
        weak keywords contribute 0.15 each (capped at 1.0 per dimension).

        Parameters
        ----------
        query: Lowercased query string.

        Returns
        -------
        Dict mapping dimension name to score (0.0–1.0).
        """
        query_lower = query.lower()
        scores: Dict[str, float] = {}

        for dim, weight_groups in self._KEYWORD_WEIGHTS.items():
            score = 0.0
            for kw in weight_groups.get("strong", []):
                if kw in query_lower:
                    score += 0.4
            for kw in weight_groups.get("weak", []):
                if kw in query_lower:
                    score += 0.15
            scores[dim] = min(score, 1.0)

        return scores

    def classify(self, query: str) -> Tuple[QueryType, float]:
        """Classify a query and return (QueryType, confidence).

        Uses combined regex + keyword classification (v8.6.0) for
        improved routing confidence.

        Args:
            query: Natural language query string.

        Returns:
            Tuple of (QueryType, confidence 0.0–1.0).
        """
        # Check identity patterns first (highest specificity)
        identity_score = self._match_score(query, self._IDENTITY_PATTERNS)
        if identity_score >= 0.7:
            return QueryType.IDENTITY, identity_score

        # Check temporal patterns — route to identity (temporal anchors)
        temporal_score = self._match_score(query, self._TEMPORAL_PATTERNS)
        if temporal_score >= 0.5:
            return QueryType.IDENTITY, temporal_score

        # Check hybrid patterns
        hybrid_score = self._match_score(query, self._HYBRID_PATTERNS)
        if hybrid_score >= 0.6:
            return QueryType.HYBRID, hybrid_score

        # Check fact patterns
        fact_score = self._match_score(query, self._FACT_PATTERNS)
        if fact_score >= 0.5:
            return QueryType.FACT, fact_score

        # ── Keyword classifier fallback (v8.6.0) ──────────────────
        kw_scores = self._keyword_classify(query)
        if kw_scores.get("IDENTITY", 0) >= 0.4:
            return QueryType.IDENTITY, kw_scores["IDENTITY"]
        if kw_scores.get("FACT", 0) >= 0.3:
            return QueryType.FACT, kw_scores["FACT"]
        if kw_scores.get("FUZZY", 0) >= 0.2:
            return QueryType.FUZZY, kw_scores["FUZZY"]

        # Default: fuzzy/RLM
        return QueryType.FUZZY, 0.3

    # ── Route Accuracy Tracking (v8.6.0) ────────────────────────────

    def report_route_feedback(
        self,
        query: str,
        routed_type: str,
        was_correct: bool,
    ) -> Dict[str, Any]:
        """Report routing accuracy feedback for continuous improvement.

        Parameters
        ----------
        query: The original query string.
        routed_type: The QueryType the router chose (e.g. 'fact').
        was_correct: Whether the routing was appropriate.

        Returns
        -------
        Dict with current accuracy stats for the reported type.
        """
        qtype_upper = routed_type.upper()
        if qtype_upper not in self._route_stats:
            qtype_upper = "FUZZY"

        self._route_stats[qtype_upper]["total"] += 1
        if was_correct:
            self._route_stats[qtype_upper]["correct"] += 1

        self._route_history.append({
            "query": query[:120],
            "routed_type": qtype_upper,
            "correct": was_correct,
        })

        # Keep history bounded
        if len(self._route_history) > 1000:
            self._route_history = self._route_history[-500:]

        return self.get_route_accuracy()

    def get_route_accuracy(self) -> Dict[str, Any]:
        """Return per-type routing accuracy statistics.

        Returns
        -------
        Dict with per-type {correct, total, accuracy} and overall accuracy.
        """
        per_type = {}
        total_correct = 0
        total_all = 0

        for qtype, stats in self._route_stats.items():
            correct = stats["correct"]
            total = stats["total"]
            acc = round(correct / max(total, 1), 4)
            per_type[qtype] = {
                "correct": correct,
                "total": total,
                "accuracy": acc,
            }
            total_correct += correct
            total_all += total

        return {
            "per_type": per_type,
            "overall_accuracy": round(total_correct / max(total_all, 1), 4),
            "total_feedback": total_all,
        }

    def _match_score(self, query: str, patterns: List[re.Pattern]) -> float:
        """Compute match score for a set of patterns."""
        max_score = 0.0
        for p in patterns:
            if p.search(query):
                # Score based on proportion of pattern matched
                match = p.search(query)
                if match:
                    matched_len = match.end() - match.start()
                    query_len = len(query)
                    score = min(1.0, matched_len / max(query_len, 1) * 3.0)
                    max_score = max(max_score, score)

        # Bonus for short queries that match (more likely targeted)
        if max_score > 0 and len(query.split()) <= 10:
            max_score = min(1.0, max_score + 0.15)

        return max_score

    def route(
        self,
        query: str,
        rag_results: Optional[List[Dict[str, Any]]] = None,
        anchor_results: Optional[List[Dict[str, Any]]] = None,
        rlm_results: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Route query and fuse results based on classification.

        Args:
            query: Original query.
            rag_results: Results from RAG retrieval.
            anchor_results: Results from identity anchor retrieval.
            rlm_results: Results from RLM generative retrieval.

        Returns:
            Dict with 'strategy', 'results', 'metadata'.
        """
        qtype, confidence = self.classify(query)

        rag_results = rag_results or []
        anchor_results = anchor_results or []
        rlm_results = rlm_results or []

        strategy_map = {
            QueryType.FACT: {
                "primary": "rag",
                "results": rag_results,
                "fallback": anchor_results,
            },
            QueryType.IDENTITY: {
                "primary": "anchors",
                "results": anchor_results,
                "fallback": rag_results,
            },
            QueryType.FUZZY: {
                "primary": "rlm",
                "results": rlm_results,
                "fallback": [],
            },
            QueryType.HYBRID: {
                "primary": "fusion",
                "results": self._fuse(rag_results, anchor_results),
                "fallback": [],
            },
        }

        strategy = strategy_map[qtype]

        return {
            "query": query,
            "query_type": qtype.name.lower(),
            "confidence": round(confidence, 4),
            "strategy": strategy["primary"],
            "results": strategy["results"],
            "fallback_results": strategy["fallback"],
            "metadata": {
                "rag_count": len(rag_results),
                "anchor_count": len(anchor_results),
                "rlm_count": len(rlm_results),
            },
        }

    def _fuse(
        self,
        rag_results: List[Dict[str, Any]],
        anchor_results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Fuse RAG and anchor results with anchors prioritized."""
        fused: List[Dict[str, Any]] = []
        seen: set = set()

        # Anchors first (identity takes priority)
        for r in anchor_results:
            key = r.get("content", "") if isinstance(r, dict) else str(r)
            digest = hash(key) if key else id(r)
            if digest not in seen:
                seen.add(digest)
                fused.append({**r, "_source": "anchors"})

        # Then RAG results, deduped
        for r in rag_results:
            key = r.get("content", "") if isinstance(r, dict) else str(r)
            digest = hash(key) if key else id(r)
            if digest not in seen:
                seen.add(digest)
                fused.append({**r, "_source": "rag"})

        return fused

    # ── Convenience: full pipeline ─────────────────────────────────────

    def classify_only(self, query: str) -> Dict[str, Any]:
        """Classify query without fusing results."""
        qtype, confidence = self.classify(query)
        return {
            "query": query,
            "query_type": qtype.name.lower(),
            "confidence": round(confidence, 4),
        }

    # ── Self-Test ──────────────────────────────────────────────────────

    def self_test(self) -> Dict[str, Any]:
        """Runtime self-diagnostic: 4 routing strategies + fallback.

        Returns:
            {"pass": bool, "checks": [...], "summary": str}
        """
        checks = []

        # Check 1: fact-type query → FACT strategy (primary: rag)
        try:
            qtype, conf = self.classify("how many users are in the system")
            assert qtype.name == "FACT", f"Expected FACT, got {qtype.name}"
            route = self.route("how many users are in the system", [{"content": "42 users"}])
            assert route["strategy"] == "rag", f"Expected rag, got {route['strategy']}"
            checks.append({"name": "fact_routing", "pass": True, "detail": f"qtype={qtype.name}, conf={conf:.3f}, strategy=rag"})
        except Exception as e:
            checks.append({"name": "fact_routing", "pass": False, "detail": str(e)})

        # Check 2: identity-type query → IDENTITY strategy (primary: anchors)
        try:
            qtype, conf = self.classify("who am I and what defines me")
            assert qtype.name == "IDENTITY", f"Expected IDENTITY, got {qtype.name}"
            route = self.route("who am I and what defines me", [{"content": "You are a file agent"}])
            assert route["strategy"] == "anchors", f"Expected anchors, got {route['strategy']}"
            checks.append({"name": "identity_routing", "pass": True, "detail": f"qtype={qtype.name}, conf={conf:.3f}, strategy=anchors"})
        except Exception as e:
            checks.append({"name": "identity_routing", "pass": False, "detail": str(e)})

        # Check 3: hybrid query → HYBRID strategy (primary: fusion)
        try:
            qtype, conf = self.classify("as an agent what did I learn about the user preferences")
            assert qtype.name == "HYBRID", f"Expected HYBRID, got {qtype.name}"
            route = self.route("as an agent what did I learn", [{"content": "analysis"}], [{"content": "policy"}])
            assert route["strategy"] == "fusion", f"Expected fusion, got {route['strategy']}"
            checks.append({"name": "hybrid_routing", "pass": True, "detail": f"qtype={qtype.name}, conf={conf:.3f}, strategy=fusion"})
        except Exception as e:
            checks.append({"name": "hybrid_routing", "pass": False, "detail": str(e)})

        # Check 4: fusion merges anchor + rag deduped
        try:
            result = self._fuse(
                [{"content": "mem-1"}, {"content": "mem-2"}],
                [{"content": "anchor-1"}, {"content": "mem-1"}],
            )
            assert len(result) == 3, f"Expected 3 fused items, got {len(result)}"
            sources = [r["_source"] for r in result]
            assert sources.count("anchors") == 2
            assert sources.count("rag") == 1
            checks.append({"name": "fusion_dedup", "pass": True, "detail": f"3 items: {sources}"})
        except Exception as e:
            checks.append({"name": "fusion_dedup", "pass": False, "detail": str(e)})

        # Check 5: classify_only returns structured dict
        try:
            result = self.classify_only("test query")
            assert result["query"] == "test query"
            assert result["query_type"] in ("fact", "identity", "fuzzy", "hybrid"), f"Unknown type: {result['query_type']}"
            checks.append({"name": "classify_only_struct", "pass": True, "detail": f"type={result['query_type']}, conf={result['confidence']}"})
        except Exception as e:
            checks.append({"name": "classify_only_struct", "pass": False, "detail": str(e)})

        # Check 6: route with empty inputs returns valid structure
        try:
            route = self.route("some query", [], [])
            assert route["strategy"] in ("rag", "anchors", "rlm", "fusion"), f"Unknown strategy: {route['strategy']}"
            assert isinstance(route.get("results", []), list)
            checks.append({"name": "route_empty_inputs", "pass": True, "detail": f"strategy={route['strategy']}, {len(route.get('results',[]))} results"})
        except Exception as e:
            checks.append({"name": "route_empty_inputs", "pass": False, "detail": str(e)})

        # Check 7: temporal pattern queries → IDENTITY
        try:
            qtype, conf = self.classify("when is my normal active time pattern")
            assert qtype.name == "IDENTITY", f"Expected IDENTITY, got {qtype.name}"
            checks.append({"name": "temporal_routing", "pass": True, "detail": f"qtype={qtype.name}, conf={conf:.3f}"})
        except Exception as e:
            checks.append({"name": "temporal_routing", "pass": False, "detail": str(e)})

        # Check 8: access anomaly detection → IDENTITY
        try:
            qtype, conf = self.classify("detect if this login pattern is suspicious")
            assert qtype.name == "IDENTITY", f"Expected IDENTITY, got {qtype.name}"
            checks.append({"name": "anomaly_routing", "pass": True, "detail": f"qtype={qtype.name}, conf={conf:.3f}"})
        except Exception as e:
            checks.append({"name": "anomaly_routing", "pass": False, "detail": str(e)})

        all_pass = all(c["pass"] for c in checks)
        return {
            "pass": all_pass,
            "checks": checks,
            "summary": f"HybridRouter self-test: {sum(1 for c in checks if c['pass'])}/{len(checks)} passed",
        }


# ── Module-level self_test ─────────────────────────────────────────


def self_test() -> Dict[str, Any]:
    """Module-level entry point for regression testing."""
    router = HybridRouter()
    return router.self_test()
