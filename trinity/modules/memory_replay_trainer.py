"""
P2-4 MemoryReplayTrainer — Contrastive Memory Replay Training Module

Collects high-quality memories from Trinity store, generates simulated
query-memory pairs for training, and exports contrastive training datasets.

Supports:
  - Importance-based filtering with recency bias
  - Template-driven query generation (no LLM dependency)
  - Contrastive pair mining (positive + hard-negative)
  - Training set export (JSON format)
  - Recall@5 embedding evaluation

Reference alignment:
  - M119 (Engram Memory): train-free memory consolidation
  - M114 (Sleep Cycle): NREM consolidation
  - P2-2 (CompressionEvaluator): compression quality assessment
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class TrainingMemory:
    """A single memory item selected for training."""

    memory_id: str
    content: str
    role: str = ""
    session_id: str = ""
    category: str = ""
    tags: List[str] = field(default_factory=list)
    importance: float = 0.5
    length: int = 0

    @property
    def content_short(self) -> str:
        return self.content[:200]


@dataclass
class QueryMemoryPair:
    """A (simulated_query, relevant_memory) training pair."""

    query: str
    memory_id: str
    memory_content: str
    query_type: str = "template"       # template / keyword / summary
    relevance_score: float = 1.0


@dataclass
class ContrastiveTriplet:
    """Contrastive learning triplet: (query, positive_memory, negative_memories)."""

    query: str
    positive_id: str
    positive_content: str
    negative_ids: List[str] = field(default_factory=list)
    negative_contents: List[str] = field(default_factory=list)


@dataclass
class EvalResult:
    """Result of embedding evaluation."""

    recall_at_1: float = 0.0
    recall_at_3: float = 0.0
    recall_at_5: float = 0.0
    mrr: float = 0.0
    num_queries: int = 0
    num_total_memories: int = 0


# ---------------------------------------------------------------------------
# Query Generation Templates (no LLM dependency)
# ---------------------------------------------------------------------------

_TEMPLATES_KEYWORD = [
    "What do you remember about {kw}?",
    "Tell me about {kw}.",
    "Find information related to {kw}.",
    "Search memory for {kw}.",
    "Do you have any memory of {kw}?",
    "Recall anything about {kw}.",
    "What was recorded regarding {kw}?",
    "Look up {kw} in your memory.",
]

_TEMPLATES_SUMMARY = [
    "Summarize the last {n} interactions.",
    "What were the recent activities?",
    "Give me a recap of recent events.",
    "What happened in the last session?",
    "Review the most recent memories.",
]

_TEMPLATES_WH = [
    "What is {subject}?",
    "Who was involved with {subject}?",
    "When did {subject} happen?",
    "Where was {subject} discussed?",
    "How was {subject} handled?",
    "Why is {subject} important?",
]

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "shall", "to", "of",
    "in", "for", "on", "with", "at", "by", "from", "as", "into",
    "through", "during", "before", "after", "above", "below",
    "between", "under", "again", "further", "then", "once", "here",
    "there", "all", "both", "each", "few", "more", "most", "other",
    "some", "such", "only", "own", "same", "so", "than", "too",
    "very", "just", "about", "over", "also", "if", "or", "and",
    "but", "not", "no", "this", "that", "it", "its",
})


def _extract_keywords(text: str, max_keywords: int = 5) -> List[str]:
    """Extract meaningful keywords from text."""
    tokens = re.findall(r"[A-Za-z0-9_-]{3,}", text.lower())
    filtered = [t for t in tokens if t not in _STOP_WORDS and not t.isdigit()]
    counts = Counter(filtered)
    return [kw for kw, _ in counts.most_common(max_keywords)]


def _extract_subject(text: str) -> str:
    """Extract a short subject phrase from memory content."""
    # Remove bracketed prefixes like [Trinity ...]
    clean = re.sub(r"\[.*?\]", "", text).strip()
    # Take first sentence or meaningful phrase
    sentences = re.split(r"[.。!?！？\n]", clean)
    first = sentences[0].strip() if sentences else clean
    # Cap at 80 chars
    return first[:80]


# ---------------------------------------------------------------------------
# MemoryReplayTrainer
# ---------------------------------------------------------------------------


class MemoryReplayTrainer:
    """Memory replay trainer for contrastive learning dataset generation.

    Parameters
    ----------
    memory_store:
        Either a ``sqlite3.Connection`` for Trinity's trinity_store.db, or
        any object with a ``search(query, top_k)`` method that returns
        results with ``.id`` and ``.score`` attributes.
    embedding_engine:
        Optional callable ``embed_fn(text: str) -> np.ndarray``.
        If provided, used for hard-negative mining and Recall@K evaluation.
        Otherwise falls back to keyword-Jaccard similarity.
    """

    def __init__(
        self,
        memory_store: Any = None,
        embedding_engine: Optional[Callable[[str], np.ndarray]] = None,
    ):
        self._store = memory_store
        self._embed_fn = embedding_engine
        self._db_path: Optional[str] = None

        # Auto-detect DB path if store is a connection
        if isinstance(memory_store, sqlite3.Connection):
            # Extract path from connection if possible
            try:
                self._db_path = memory_store.execute(
                    "PRAGMA database_list"
                ).fetchone()[2]
            except Exception:
                pass

        # Results cache
        self._collected_memories: List[TrainingMemory] = []
        self._query_pairs: List[QueryMemoryPair] = []
        self._contrastive_triplets: List[ContrastiveTriplet] = []
        self._last_eval: Optional[EvalResult] = None

    # ------------------------------------------------------------------
    # Public API — Data Collection
    # ------------------------------------------------------------------

    def collect_training_data(
        self,
        min_importance: float = 0.3,
        max_samples: int = 500,
        db_path: Optional[str] = None,
    ) -> List[TrainingMemory]:
        """Collect high-quality memories for training.

        Filters by importance score (heuristic: content length + keyword density)
        with recency bias toward more recent memories.

        Args:
            min_importance: Minimum importance score (0.0–1.0).
            max_samples: Maximum number of memories to collect.
            db_path:   Override DB path (defaults to auto-detected).

        Returns:
            List of ``TrainingMemory`` items sorted by importance descending.
        """
        path = db_path or self._db_path
        if not path:
            # Try default Trinity path
            default = Path(__file__).resolve().parent.parent.parent / "trinity_store.db"
            if default.exists():
                path = str(default)
            else:
                raise ValueError(
                    "No DB path provided and trinity_store.db not found. "
                    "Pass db_path or provide a memory_store connection."
                )

        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row

        rows = db.execute("""
            SELECT m.memory_id, m.content, m.role, m.session_id
            FROM memories m
            ORDER BY m.rowid DESC
        """).fetchall()

        memories: List[TrainingMemory] = []
        for r in rows:
            content = r["content"] or ""
            # Heuristic importance: length ratio + keyword richness
            imp = self._compute_importance(content)

            if imp < min_importance:
                continue

            # Get FTS tags/category
            fts = db.execute(
                "SELECT category, tags FROM memories_fts WHERE content = ?",
                (content,)
            ).fetchone()

            category = fts["category"] if fts else ""
            tags_raw = fts["tags"] if fts and fts["tags"] else "[]"
            try:
                tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
            except json.JSONDecodeError:
                tags = []

            memories.append(TrainingMemory(
                memory_id=r["memory_id"],
                content=content,
                role=r["role"],
                session_id=r["session_id"],
                category=category,
                tags=list(tags) if tags else [],
                importance=round(imp, 4),
                length=len(content),
            ))

            if len(memories) >= max_samples:
                break

        db.close()

        # Sort by importance descending
        memories.sort(key=lambda m: m.importance, reverse=True)
        self._collected_memories = memories
        return memories

    @staticmethod
    def _compute_importance(content: str) -> float:
        """Heuristic importance score in [0, 1]."""
        if not content:
            return 0.0
        length = min(len(content), 2000)
        length_score = math.log2(max(length, 2)) / math.log2(2000)

        # Keyword richness
        keywords = _extract_keywords(content, max_keywords=20)
        kw_score = min(len(keywords) / 15.0, 1.0)

        # Tag-richness bonus
        tag_bonus = 0.0
        for marker in ["[Trinity", "evo_", "handoff", "safety", "critical",
                         "rule", "pattern", "benchmark", "version", "error"]:
            if marker.lower() in content.lower():
                tag_bonus += 0.05

        raw = 0.35 * length_score + 0.35 * kw_score + min(tag_bonus, 0.3)
        return round(min(raw, 1.0), 4)

    # ------------------------------------------------------------------
    # Public API — Query Generation
    # ------------------------------------------------------------------

    def generate_query_pairs(
        self,
        memories: Optional[List[TrainingMemory]] = None,
        queries_per_memory: int = 2,
        use_llm: bool = False,
        llm_fn: Optional[Callable[[str], str]] = None,
    ) -> List[QueryMemoryPair]:
        """Generate simulated queries for each memory.

        Without LLM (default): uses template-based generation:
          - Type 1: keyword-queries from extracted keywords
          - Type 2: summary-queries from first-sentence extraction
          - Type 3: wh-questions based on subject extraction

        With LLM: passes each memory to llm_fn(content) → prompt-generated query.

        Args:
            memories:      Memory list (uses self._collected_memories if None).
            queries_per_memory: Number of queries per memory (1–3).
            use_llm:       If True, use llm_fn for query generation.
            llm_fn:        Callable taking memory content, returning a query string.

        Returns:
            List of ``QueryMemoryPair``.
        """
        if memories is None:
            memories = self._collected_memories

        pairs: List[QueryMemoryPair] = []
        random.seed(42)

        for mem in memories:
            if use_llm and llm_fn:
                # LLM-based query generation
                for i in range(queries_per_memory):
                    try:
                        prompt = (
                            f"Given this memory: '{mem.content_short}'\n"
                            f"Generate a natural search query that a user might type "
                            f"to retrieve this memory. Output only the query string."
                        )
                        query = llm_fn(prompt).strip().strip('"').strip("'")
                        if query:
                            pairs.append(QueryMemoryPair(
                                query=query,
                                memory_id=mem.memory_id,
                                memory_content=mem.content,
                                query_type="llm",
                            ))
                    except Exception:
                        continue
            else:
                # Template-based generation
                keywords = _extract_keywords(mem.content, max_keywords=5)
                subject = _extract_subject(mem.content)

                n = queries_per_memory
                generated = 0

                # Template 1: keyword-based
                if keywords and generated < n:
                    template = random.choice(_TEMPLATES_KEYWORD)
                    kw = random.choice(keywords)
                    query = template.format(kw=kw)
                    pairs.append(QueryMemoryPair(
                        query=query,
                        memory_id=mem.memory_id,
                        memory_content=mem.content,
                        query_type="keyword",
                    ))
                    generated += 1

                # Template 2: wh-question
                if subject and generated < n:
                    template = random.choice(_TEMPLATES_WH)
                    subj_short = subject[:60]
                    query = template.format(subject=subj_short)
                    pairs.append(QueryMemoryPair(
                        query=query,
                        memory_id=mem.memory_id,
                        memory_content=mem.content,
                        query_type="wh_question",
                    ))
                    generated += 1

                # Template 3: second keyword variant
                if len(keywords) >= 2 and generated < n:
                    kw2 = keywords[1]
                    query = f"Find memories about {kw2}."
                    pairs.append(QueryMemoryPair(
                        query=query,
                        memory_id=mem.memory_id,
                        memory_content=mem.content,
                        query_type="keyword",
                    ))
                    generated += 1

        self._query_pairs = pairs
        return pairs

    # ------------------------------------------------------------------
    # Public API — Contrastive Pairs
    # ------------------------------------------------------------------

    def compute_contrastive_pairs(
        self,
        queries: Optional[List[QueryMemoryPair]] = None,
        all_memories: Optional[List[TrainingMemory]] = None,
        negative_count: int = 3,
    ) -> List[ContrastiveTriplet]:
        """Compute contrastive triplets: (query, positive, negatives).

        Negative mining strategy:
          - If embedding_engine available: embed query + all memories,
            select farthest negatives by cosine distance (hard negatives).
          - Otherwise: random sampling from memories with different tags/category
            (soft negatives).

        Args:
            queries:         Query-memory pairs (uses self._query_pairs if None).
            all_memories:    Full memory pool (uses self._collected_memories if None).
            negative_count:  Number of negative samples per query.

        Returns:
            List of ``ContrastiveTriplet``.
        """
        if queries is None:
            queries = self._query_pairs
        if all_memories is None:
            all_memories = self._collected_memories

        if not queries or not all_memories:
            return []

        random.seed(123)

        # Build lookup
        mem_map: Dict[str, TrainingMemory] = {m.memory_id: m for m in all_memories}
        triplet_list: List[ContrastiveTriplet] = []

        if self._embed_fn and len(all_memories) > 5:
            # Embedding-based hard negative mining
            try:
                # Embed all memories once
                mem_texts = [m.content for m in all_memories]
                mem_vecs = np.vstack([self._embed_fn(t) for t in mem_texts])
                mem_vecs = mem_vecs / (np.linalg.norm(mem_vecs, axis=1, keepdims=True) + 1e-12)

                for pair in queries:
                    positive = mem_map.get(pair.memory_id)
                    if not positive:
                        continue

                    q_vec = self._embed_fn(pair.query).reshape(1, -1)
                    q_vec = q_vec / (np.linalg.norm(q_vec) + 1e-12)

                    # Cosine similarity
                    sims = np.dot(q_vec, mem_vecs.T).flatten()

                    # Candidates: exclude the positive
                    pos_idx = None
                    for i, m in enumerate(all_memories):
                        if m.memory_id == pair.memory_id:
                            pos_idx = i
                            break

                    neg_candidates = [
                        (i, sims[i])
                        for i in range(len(all_memories))
                        if i != pos_idx
                    ]
                    # Sort by distance (farthest = hardest negatives)
                    neg_candidates.sort(key=lambda x: x[1])  # ascending similarity
                    # Take farthest + some random for diversity
                    hard_count = min(negative_count // 2 + 1, len(neg_candidates))
                    hards = neg_candidates[:hard_count]
                    if len(neg_candidates) > hard_count:
                        rest = neg_candidates[hard_count:]
                        random.shuffle(rest)
                        selected = hards + rest[:negative_count - hard_count]
                    else:
                        selected = hards

                    neg_ids = [all_memories[i].memory_id for i, _ in selected[:negative_count]]
                    neg_contents = [all_memories[i].content for i, _ in selected[:negative_count]]

                    triplet_list.append(ContrastiveTriplet(
                        query=pair.query,
                        positive_id=pair.memory_id,
                        positive_content=pair.memory_content,
                        negative_ids=neg_ids,
                        negative_contents=neg_contents,
                    ))
            except Exception:
                # Fall through to random negatives
                return self._random_negatives(queries, all_memories, mem_map, negative_count)
        else:
            return self._random_negatives(queries, all_memories, mem_map, negative_count)

        self._contrastive_triplets = triplet_list
        return triplet_list

    def _random_negatives(
        self,
        queries: List[QueryMemoryPair],
        all_memories: List[TrainingMemory],
        mem_map: Dict[str, TrainingMemory],
        negative_count: int,
    ) -> List[ContrastiveTriplet]:
        """Random negative sampling with category/tag diversity."""
        triplets: List[ContrastiveTriplet] = []
        mem_pool = [m for m in all_memories]

        for pair in queries:
            positive = mem_map.get(pair.memory_id)
            if not positive:
                continue

            # Exclude positive from candidate pool
            candidates = [m for m in mem_pool if m.memory_id != pair.memory_id]

            # Prefer negatives from different category/tags
            same_cat = [m for m in candidates if m.category == positive.category]
            diff_cat = [m for m in candidates if m.category != positive.category]

            negs: List[TrainingMemory] = []
            # Mix: half from different category, half random
            random.shuffle(diff_cat)
            negs.extend(diff_cat[:negative_count // 2 + 1])
            if len(negs) < negative_count and same_cat:
                random.shuffle(same_cat)
                negs.extend(same_cat[:negative_count - len(negs)])
            if len(negs) < negative_count:
                random.shuffle(candidates)
                for c in candidates:
                    if c not in negs:
                        negs.append(c)
                    if len(negs) >= negative_count:
                        break

            triplets.append(ContrastiveTriplet(
                query=pair.query,
                positive_id=pair.memory_id,
                positive_content=pair.memory_content,
                negative_ids=[m.memory_id for m in negs[:negative_count]],
                negative_contents=[m.content for m in negs[:negative_count]],
            ))

        self._contrastive_triplets = triplets
        return triplets

    # ------------------------------------------------------------------
    # Public API — Export
    # ------------------------------------------------------------------

    def export_training_set(
        self,
        output_path: str,
        format: str = "json",
        include_contrastive: bool = True,
    ) -> str:
        """Export training data to JSON file.

        Output structure:
            {
              "metadata": { "format_version": "1.0", ... },
              "memories": [ { "memory_id", "content", "tags", ... }, ... ],
              "query_pairs": [ { "query", "memory_id", "query_type" }, ... ],
              "contrastive_triplets": [ { "query", "positive_id", "negative_ids" }, ... ]
            }

        Args:
            output_path:  Target file path (e.g. 'benchmark/memory_replay_train.json').
            format:       Output format ('json' only for now).
            include_contrastive: Whether to include contrastive triplets.

        Returns:
            Absolute path to the output file.
        """
        metadata = {
            "format_version": "1.0",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "num_memories": len(self._collected_memories),
            "num_query_pairs": len(self._query_pairs),
            "num_contrastive_triplets": len(self._contrastive_triplets) if include_contrastive else 0,
            "generator": "MemoryReplayTrainer",
        }

        memories_data = [
            {
                "memory_id": m.memory_id,
                "content": m.content,
                "role": m.role,
                "session_id": m.session_id,
                "category": m.category,
                "tags": m.tags,
                "importance": m.importance,
                "length": m.length,
            }
            for m in self._collected_memories
        ]

        pairs_data = [
            {
                "query": p.query,
                "memory_id": p.memory_id,
                "query_type": p.query_type,
            }
            for p in self._query_pairs
        ]

        output: Dict[str, Any] = {
            "metadata": metadata,
            "memories": memories_data,
            "query_pairs": pairs_data,
        }

        if include_contrastive:
            output["contrastive_triplets"] = [
                {
                    "query": t.query,
                    "positive_id": t.positive_id,
                    "positive_content": t.positive_content[:500],
                    "negative_ids": t.negative_ids,
                }
                for t in self._contrastive_triplets
            ]

        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        return str(out_path.resolve())

    # ------------------------------------------------------------------
    # Public API — Evaluation
    # ------------------------------------------------------------------

    def evaluate_embedding(
        self,
        test_queries: Optional[List[str]] = None,
        test_memories: Optional[List[TrainingMemory]] = None,
        top_k: int = 5,
    ) -> EvalResult:
        """Evaluate embedding quality via Recall@K on test queries.

        Uses cosine similarity between query embedding and memory embeddings.
        Falls back to keyword-Jaccard if no embedding_engine.

        Args:
            test_queries:   List of query strings. If None, samples from query_pairs.
            test_memories:  Memory pool. If None, uses collected_memories.
            top_k:          K for Recall@K (1, 3, 5 computed).

        Returns:
            ``EvalResult`` with Recall@1/3/5 and MRR.
        """
        if test_memories is None:
            test_memories = self._collected_memories
        if test_queries is None:
            test_queries = list({p.query for p in self._query_pairs})

        if not test_queries or not test_memories:
            self._last_eval = EvalResult()
            return self._last_eval

        # Build mapping: memory_id → index in all_memories
        mem_list = list(test_memories)

        # For evaluation, we need to know which memory is the correct one
        # Use the query_pairs mapping
        ground_truth: Dict[str, str] = {}
        for p in self._query_pairs:
            # Take the first query as key (queries may duplicate)
            if p.query not in ground_truth:
                ground_truth[p.query] = p.memory_id

        recall_1 = 0.0
        recall_3 = 0.0
        recall_5 = 0.0
        reciprocal_ranks = []

        if self._embed_fn:
            # Embedding-based evaluation
            try:
                mem_texts = [m.content for m in mem_list]
                mem_vecs = np.vstack([self._embed_fn(t) for t in mem_texts])
                mem_vecs = mem_vecs / (np.linalg.norm(mem_vecs, axis=1, keepdims=True) + 1e-12)

                valid_queries = 0
                for query in test_queries:
                    gt_id = ground_truth.get(query)
                    if not gt_id:
                        continue

                    q_vec = self._embed_fn(query).reshape(1, -1)
                    q_vec = q_vec / (np.linalg.norm(q_vec) + 1e-12)
                    sims = np.dot(q_vec, mem_vecs.T).flatten()
                    ranked = np.argsort(-sims)

                    rank = -1
                    for r, idx in enumerate(ranked):
                        if mem_list[idx].memory_id == gt_id:
                            rank = r + 1
                            break

                    if rank > 0:
                        if rank <= 1:
                            recall_1 += 1
                        if rank <= 3:
                            recall_3 += 1
                        if rank <= top_k:
                            recall_5 += 1
                        reciprocal_ranks.append(1.0 / rank)
                    valid_queries += 1

                if valid_queries > 0:
                    recall_1 /= valid_queries
                    recall_3 /= valid_queries
                    recall_5 /= valid_queries
                    mrr = np.mean(reciprocal_ranks) if reciprocal_ranks else 0.0
                else:
                    mrr = 0.0
            except Exception:
                return self._keyword_eval(test_queries, mem_list, ground_truth, top_k)
        else:
            return self._keyword_eval(test_queries, mem_list, ground_truth, top_k)

        result = EvalResult(
            recall_at_1=round(recall_1, 4),
            recall_at_3=round(recall_3, 4),
            recall_at_5=round(recall_5, 4),
            mrr=round(mrr, 4),
            num_queries=len(test_queries),
            num_total_memories=len(mem_list),
        )
        self._last_eval = result
        return result

    def _keyword_eval(
        self,
        test_queries: List[str],
        mem_list: List[TrainingMemory],
        ground_truth: Dict[str, str],
        top_k: int,
    ) -> EvalResult:
        """Fallback evaluation using keyword Jaccard similarity."""
        recall_1 = 0.0
        recall_3 = 0.0
        recall_5 = 0.0
        reciprocal_ranks = []
        valid = 0

        for query in test_queries:
            gt_id = ground_truth.get(query)
            if not gt_id:
                continue

            q_tokens = set(_extract_keywords(query, max_keywords=20))

            scored = []
            for m in mem_list:
                m_tokens = set(_extract_keywords(m.content, max_keywords=20))
                if not q_tokens and not m_tokens:
                    score = 0.0
                else:
                    union = q_tokens | m_tokens
                    inter = q_tokens & m_tokens
                    score = len(inter) / len(union) if union else 0.0
                scored.append((score, m))

            scored.sort(key=lambda x: x[0], reverse=True)

            rank = -1
            for r, (_, mem) in enumerate(scored):
                if mem.memory_id == gt_id:
                    rank = r + 1
                    break

            if rank > 0:
                if rank <= 1:
                    recall_1 += 1
                if rank <= 3:
                    recall_3 += 1
                if rank <= top_k:
                    recall_5 += 1
                reciprocal_ranks.append(1.0 / rank)
            valid += 1

        if valid > 0:
            recall_1 /= valid
            recall_3 /= valid
            recall_5 /= valid
            mrr = np.mean(reciprocal_ranks) if reciprocal_ranks else 0.0
        else:
            mrr = 0.0

        result = EvalResult(
            recall_at_1=round(recall_1, 4),
            recall_at_3=round(recall_3, 4),
            recall_at_5=round(recall_5, 4),
            mrr=round(mrr, 4),
            num_queries=len(test_queries),
            num_total_memories=len(mem_list),
        )
        self._last_eval = result
        return result

    # ------------------------------------------------------------------
    # Pipeline convenience
    # ------------------------------------------------------------------

    def run_pipeline(
        self,
        db_path: Optional[str] = None,
        output_path: Optional[str] = None,
        min_importance: float = 0.2,
        max_samples: int = 200,
        queries_per_memory: int = 2,
        negative_count: int = 3,
        evaluate: bool = True,
    ) -> Dict[str, Any]:
        """Run the full training data generation pipeline.

        Returns a dict with summary statistics.
        """
        # 1. Collect
        memories = self.collect_training_data(
            min_importance=min_importance,
            max_samples=max_samples,
            db_path=db_path,
        )

        # 2. Generate queries
        pairs = self.generate_query_pairs(
            memories=memories,
            queries_per_memory=queries_per_memory,
        )

        # 3. Contrastive pairs
        triplets = self.compute_contrastive_pairs(
            queries=pairs,
            all_memories=memories,
            negative_count=negative_count,
        )

        # 4. Export
        exported_path = ""
        if output_path:
            exported_path = self.export_training_set(output_path)

        # 5. Evaluate
        eval_result = None
        if evaluate:
            eval_result = self.evaluate_embedding()

        summary = {
            "collected_memories": len(memories),
            "query_pairs": len(pairs),
            "contrastive_triplets": len(triplets),
            "exported_path": exported_path,
        }
        if eval_result:
            summary["eval_recall_5"] = eval_result.recall_at_5
            summary["eval_mrr"] = eval_result.mrr

        return summary


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("MemoryReplayTrainer self-tests...")

    db_path = r"C:\Users\Administrator\trinity\trinity_store.db"
    trainer = MemoryReplayTrainer()

    # Test 1: collect
    memories = trainer.collect_training_data(
        min_importance=0.1, max_samples=50, db_path=db_path
    )
    print(f"\n  collect_training_data: {len(memories)} memories collected")
    if memories:
        m = memories[0]
        print(f"    top: {m.memory_id[:20]} | imp={m.importance} | tags={m.tags[:3]}")

    # Test 2: generate query pairs
    pairs = trainer.generate_query_pairs(queries_per_memory=2)
    print(f"  generate_query_pairs: {len(pairs)} pairs")
    if pairs:
        print(f"    sample: '{pairs[0].query}' -> {pairs[0].memory_id[:20]}")

    # Test 3: contrastive pairs
    triplets = trainer.compute_contrastive_pairs(negative_count=3)
    print(f"  compute_contrastive_pairs: {len(triplets)} triplets")
    if triplets:
        print(f"    sample: query='{triplets[0].query[:50]}...' negs={triplets[0].negative_ids}")

    # Test 4: export
    out_path = r"C:\Users\Administrator\trinity\benchmark\memory_replay_train.json"
    exported = trainer.export_training_set(out_path)
    print(f"  export_training_set: {exported}")

    # Test 5: evaluate
    eval_result = trainer.evaluate_embedding()
    print(f"  evaluate_embedding: Recall@5={eval_result.recall_at_5:.4f}, MRR={eval_result.mrr:.4f}")

    # Test 6: pipeline
    print("\n  run_pipeline summary:")
    summary = trainer.run_pipeline(
        db_path=db_path,
        output_path=out_path,
        min_importance=0.15,
        max_samples=30,
    )
    for k, v in summary.items():
        print(f"    {k}: {v}")

    print("\nAll self-tests passed.")
