"""
Open-Domain Reasoning Module.

Three components aligned with Hindsight (95.1% LoCoMo open-domain score):

1. BeliefNetwork — Evidence/Inference separation (structured reasoning chain)
2. ContextExpander — Query decomposition + multi-hop expansion
3. OpenDomainReasoner — Combined pipeline: retrieve → expand → reason → answer

The key insight from Hindsight: separating "what the facts say" (evidence)
from "what we can conclude" (inference) prevents reasoning collapse and
enables accurate open-domain QA.

Usage:
    from trinity.modules.open_domain import OpenDomainReasoner
    
    reasoner = OpenDomainReasoner()
    answer = reasoner.answer("What is the capital of France?")
    print(answer["response"])
"""

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Evidence:
    """A factual piece of evidence retrieved for reasoning."""
    source: str
    content: str
    confidence: float = 1.0
    retrieval_score: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class Inference:
    """An inference drawn from evidence."""
    statement: str
    supporting_evidence: List[str]  # source IDs
    confidence: float
    reasoning_step: str = "direct"


@dataclass
class BeliefState:
    """The current belief state for a given topic/question."""
    topic: str
    evidence: List[Evidence] = field(default_factory=list)
    inferences: List[Inference] = field(default_factory=list)
    contradictions: List[Tuple[str, str]] = field(default_factory=list)
    confidence: float = 0.0
    updated_at: float = field(default_factory=time.time)


class BeliefNetwork:
    """
    Structured evidence/inference separation (Hindsight-aligned).

    Maintains two separate stores:
      - Evidence Store: factual claims with sources
      - Inference Store: conclusions drawn from evidence
    
    This separation prevents reasoning collapse where models confuse
    what they inferred with what the evidence actually says.
    """

    def __init__(self):
        self._beliefs: Dict[str, BeliefState] = {}
        self._total_evidence = 0
        self._total_inferences = 0

    def add_evidence(self, topic: str, content: str, source: str = "retrieval",
                     confidence: float = 1.0, score: float = 0.0) -> str:
        """Add a piece of evidence to a topic's belief state.

        Args:
            topic: The topic/question this evidence relates to.
            content: The factual content.
            source: Source identifier.
            confidence: Confidence in this evidence.
            score: Retrieval score.

        Returns:
            Evidence ID.
        """
        if topic not in self._beliefs:
            self._beliefs[topic] = BeliefState(topic=topic)
        
        ev = Evidence(
            source=source,
            content=content,
            confidence=confidence,
            retrieval_score=score,
        )
        self._beliefs[topic].evidence.append(ev)
        self._total_evidence += 1
        
        # Update confidence (average of top evidence)
        if self._beliefs[topic].evidence:
            top_scores = sorted(
                [e.confidence for e in self._beliefs[topic].evidence],
                reverse=True
            )[:3]
            self._beliefs[topic].confidence = sum(top_scores) / len(top_scores)
        
        return f"ev_{self._total_evidence}"

    def add_inference(self, topic: str, statement: str,
                      supporting_ids: List[str],
                      confidence: float,
                      step: str = "direct") -> str:
        """Add an inference drawn from evidence.

        Args:
            topic: The topic this inference relates to.
            statement: The inferred statement.
            supporting_ids: IDs of supporting evidence.
            confidence: Confidence in this inference.
            step: Reasoning step type (direct, multi_hop, deductive, etc.)

        Returns:
            Inference ID.
        """
        if topic not in self._beliefs:
            raise ValueError(f"No belief state for topic: {topic}")
        
        inf = Inference(
            statement=statement,
            supporting_evidence=supporting_ids,
            confidence=confidence,
            reasoning_step=step,
        )
        self._beliefs[topic].inferences.append(inf)
        self._total_inferences += 1
        
        # Detect contradictions
        self._detect_contradictions(topic)
        
        return f"inf_{self._total_inferences}"

    def _detect_contradictions(self, topic: str) -> None:
        """Detect contradictions between evidence and inferences."""
        belief = self._beliefs[topic]
        
        # Evidence vs Evidence contradictions
        for i, e1 in enumerate(belief.evidence):
            for e2 in belief.evidence[i+1:]:
                if self._is_contradictory(e1.content, e2.content):
                    belief.contradictions.append((e1.content[:50], e2.content[:50]))
        
        # Evidence vs Inference contradictions
        for e in belief.evidence:
            for inf in belief.inferences:
                if self._is_contradictory(e.content, inf.statement):
                    belief.contradictions.append((e.content[:50], inf.statement[:50]))

    def _is_contradictory(self, a: str, b: str) -> bool:
        """Simple contradiction detection based on negation patterns."""
        a_lower, b_lower = a.lower(), b.lower()
        # Check if one negates the other
        negations = ["not ", "don't ", "doesn't ", "isn't ", "cannot ", "never "]
        for neg in negations:
            # If A contains negation and B states the same thing
            if neg in a_lower and neg not in b_lower:
                cleaned = a_lower.replace(neg, "")
                if cleaned.strip()[:20] in b_lower or b_lower[:20] in cleaned:
                    return True
            if neg in b_lower and neg not in a_lower:
                cleaned = b_lower.replace(neg, "")
                if cleaned.strip()[:20] in a_lower or a_lower[:20] in cleaned:
                    return True
        return False

    def get_state(self, topic: str) -> Optional[BeliefState]:
        """Get the full belief state for a topic."""
        return self._beliefs.get(topic)

    def get_evidence(self, topic: str, top_k: int = 5) -> List[Evidence]:
        """Get top evidence for a topic, by confidence."""
        belief = self._beliefs.get(topic)
        if not belief:
            return []
        sorted_ev = sorted(belief.evidence, key=lambda e: e.confidence, reverse=True)
        return sorted_ev[:top_k]

    def get_inferences(self, topic: str, top_k: int = 5) -> List[Inference]:
        """Get top inferences for a topic, by confidence."""
        belief = self._beliefs.get(topic)
        if not belief:
            return []
        sorted_inf = sorted(belief.inferences, key=lambda i: i.confidence, reverse=True)
        return sorted_inf[:top_k]

    def resolve_contradictions(self, topic: str) -> List[Dict[str, Any]]:
        """Resolve contradictions by preferring higher-confidence evidence."""
        belief = self._beliefs.get(topic)
        if not belief or not belief.contradictions:
            return []
        
        resolutions = []
        for a, b in belief.contradictions:
            # Find the evidence with higher confidence
            ev_a = next((e for e in belief.evidence if e.content.startswith(a[:20])), None)
            ev_b = next((e for e in belief.evidence if e.content.startswith(b[:20])), None)
            
            if ev_a and ev_b:
                if ev_a.confidence >= ev_b.confidence:
                    resolutions.append({"preferred": a, "superseded": b, "reason": "higher confidence"})
                else:
                    resolutions.append({"preferred": b, "superseded": a, "reason": "higher confidence"})
        
        return resolutions

    def diagnostics(self) -> Dict[str, Any]:
        """Return diagnostic information."""
        total_topics = len(self._beliefs)
        total_contradictions = sum(len(b.contradictions) for b in self._beliefs.values())
        return {
            "module": "BeliefNetwork",
            "total_topics": total_topics,
            "total_evidence": self._total_evidence,
            "total_inferences": self._total_inferences,
            "total_contradictions": total_contradictions,
            "topics": list(self._beliefs.keys()),
        }


class ContextExpander:
    """
    Query decomposition + multi-hop expansion.

    Expands a user query into sub-queries for better coverage,
    then gathers evidence across expansion paths.
    """

    def __init__(self, max_hops: int = 2, expansion_factor: int = 3):
        self.max_hops = max_hops
        self.expansion_factor = expansion_factor
        self._expansion_stats: Dict[str, int] = {"total_expansions": 0, "total_hops": 0}

    def expand_query(self, query: str) -> List[str]:
        """Expand a query into sub-queries.

        Uses keyword-based decomposition:
          - Extracts key entities/topics
          - Generates related sub-queries

        In production, this would use an LLM for semantic expansion.
        """
        sub_queries = [query]  # Always include the original
        self._expansion_stats["total_expansions"] += 1

        # Extract key terms (simple heuristic)
        stop_words = {"the", "a", "an", "is", "are", "was", "were", 
                      "what", "how", "why", "when", "where", "who"}
        words = query.lower().split()
        key_terms = [w for w in words if w not in stop_words and len(w) > 3]
        
        # Generate sub-queries based on key terms
        for i, term in enumerate(key_terms[:self.expansion_factor]):
            sub_queries.append(term)
            # Two-term combinations
            for j in range(i+1, min(i+2, len(key_terms))):
                if j < len(key_terms):
                    sub_queries.append(f"{term} {key_terms[j]}")

        # Temporal expansions
        temporal_markers = ["before", "after", "during", "recent", "historical",
                           "current", "future", "past", "present"]
        for marker in temporal_markers:
            if marker in words:
                sub_queries.append(f"related to {query}")

        return list(set(sub_queries))[:self.expansion_factor * 3]

    def multi_hop_expand(self, query: str, retriever_fn) -> List[str]:
        """Multi-hop query expansion using retrieval feedback.

        Args:
            query: Original query.
            retriever_fn: Callable(query) -> list of result dicts.

        Returns:
            Expanded list of queries after N hops.
        """
        self._expansion_stats["total_hops"] += 1
        all_queries = [query]
        current = query

        for hop in range(self.max_hops):
            # Retrieve based on current query
            try:
                results = retriever_fn(current)
                if not results:
                    break
                
                # Extract new terms from retrieved content
                content = " ".join(
                    r.get("content_preview", r.get("content", "")) 
                    for r in (results if isinstance(results, list) else results.get("results", []))
                )
                
                # Simple term extraction from retrieved content
                words = content.split()
                new_terms = [w for w in words if len(w) > 5 and w.lower() not in query.lower()]
                
                if new_terms:
                    current = new_terms[0]
                    if current not in all_queries:
                        all_queries.append(current)
                        self._expansion_stats["total_expansions"] += 1
                else:
                    break
            except Exception:
                break

        return all_queries

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "module": "ContextExpander",
            "max_hops": self.max_hops,
            "expansion_factor": self.expansion_factor,
            "stats": self._expansion_stats,
        }


class OpenDomainReasoner:
    """
    Open-domain reasoning pipeline.

    Pipeline:
        1. Expand query (decomposition + multi-hop)
        2. Retrieve evidence for each sub-query
        3. Build belief network (evidence/inference separation)
        4. Resolve contradictions
        5. Synthesize answer from evidence + inferences

    This addresses the key gap: Hindsight achieves 95.1% open-domain
    by separating evidence from inference and using structured reasoning.
    """

    def __init__(self):
        self.belief_network = BeliefNetwork()
        self.context_expander = ContextExpander()
        self._answer_count = 0

    def answer(self, query: str, retriever=None, top_k: int = 5) -> Dict[str, Any]:
        """Answer an open-domain question.

        Args:
            query: The user's question.
            retriever: Optional retrieval function (uses Trinity if None).
            top_k: Number of evidence pieces to gather per sub-query.

        Returns:
            Dict with answer, confidence, evidence, and reasoning chain.
        """
        start = time.time()
        self._answer_count += 1

        # 1. Expand query
        sub_queries = self.context_expander.expand_query(query)

        # 2. Retrieve evidence
        all_evidence = []
        for sq in sub_queries:
            if retriever:
                try:
                    results = retriever(sq)
                    hits = results if isinstance(results, list) else results.get("results", [])
                    for hit in hits[:top_k]:
                        preview = hit.get("content_preview", hit.get("content", ""))
                        score = hit.get("score", hit.get("final_score", 0))
                        all_evidence.append((preview, score))
                except Exception:
                    pass

        # 3. Build belief network
        for content, score in all_evidence:
            self.belief_network.add_evidence(
                topic=query,
                content=content,
                source="retrieval",
                confidence=min(score * 1.5, 1.0) if score > 0 else 0.5,
                score=score,
            )

        # 4. Make inferences
        evidence_list = self.belief_network.get_evidence(query)
        if evidence_list:
            # Simple inference: combine top evidence
            top_content = " ".join([e.content[:100] for e in evidence_list[:3]])
            self.belief_network.add_inference(
                topic=query,
                statement=f"Based on retrieved evidence: {top_content[:200]}",
                supporting_ids=[f"ev_{i}" for i in range(min(3, len(evidence_list)))],
                confidence=evidence_list[0].confidence if evidence_list else 0.5,
                step="evidence_synthesis",
            )

        # 5. Resolve contradictions
        resolutions = self.belief_network.resolve_contradictions(query)

        # 6. Build response
        state = self.belief_network.get_state(query)
        if state and state.evidence:
            # Take top evidence as the primary answer
            top_ev = state.evidence[0]
            response = top_ev.content
            confidence = state.confidence
            
            # Add inference if it adds value
            if state.inferences and state.confidence > 0.5:
                top_inf = state.inferences[0]
                response += f" {top_inf.statement[:100]}"
        else:
            response = "No relevant information found."
            confidence = 0.0

        duration = (time.time() - start) * 1000

        return {
            "query": query,
            "response": response,
            "confidence": round(confidence, 4),
            "evidence_count": len(all_evidence),
            "evidence_sources": list(set(e[0][:50] for e in all_evidence)),
            "sub_queries_expanded": len(sub_queries),
            "inferences_made": len(state.inferences) if state else 0,
            "contradictions_resolved": len(resolutions),
            "duration_ms": round(duration, 1),
        }

    def answer_multi_hop(self, query: str, retriever=None, top_k: int = 5) -> Dict[str, Any]:
        """Multi-hop answer with query expansion via retrieval feedback."""
        # Use multi-hop expansion
        sub_queries = self.context_expander.multi_hop_expand(query, retriever)
        
        # Gather evidence from all hops
        all_evidence = []
        for sq in sub_queries:
            if retriever:
                try:
                    results = retriever(sq)
                    hits = results if isinstance(results, list) else results.get("results", [])
                    for hit in hits[:top_k]:
                        preview = hit.get("content_preview", hit.get("content", ""))
                        score = hit.get("score", hit.get("final_score", 0))
                        all_evidence.append((preview, score, sq))
                except Exception:
                    pass

        # Build belief network
        for content, score, src_query in all_evidence:
            self.belief_network.add_evidence(
                topic=query,
                content=content,
                source=f"multi_hop:{src_query[:30]}",
                confidence=min(score * 1.5, 1.0) if score > 0 else 0.5,
                score=score,
            )

        # Build response
        state = self.belief_network.get_state(query)
        response = state.evidence[0].content if state and state.evidence else "No information found."
        confidence = state.confidence if state else 0.0

        return {
            "query": query,
            "response": response,
            "confidence": round(confidence, 4),
            "hops_used": len(sub_queries),
            "sub_queries": sub_queries,
            "total_evidence": len(all_evidence),
        }

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "module": "OpenDomainReasoner",
            "total_answers": self._answer_count,
            "belief_network": self.belief_network.diagnostics(),
            "context_expander": self.context_expander.diagnostics(),
        }
