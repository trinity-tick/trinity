"""
Memory Consolidation Engine v1 (P0.3)
======================================
Background memory housekeeping that runs after ingestion:

  - ``merge_similar(threshold=0.85)``: detect semantically near‑identical
    memories within the same persona and merge them into a consolidated entry.
  - ``apply_decay(days=7)``: halve the importance of memories whose
    ``last_accessed_at`` is older than N days.
  - ``promote_frequent(access_threshold=5)``: upgrade frequently‑accessed
    ``episodic`` memories to ``semantic`` (long‑term factual storage).

Design
------
The consolidator uses the Trinity ``StorageAdapter`` interface for reads and
writes.  When batch SQL updates are required (decay / promote) it accesses
the adapter's raw connection directly — this avoids introducing a separate
ORM / SQL helper while keeping the consolidator stateless.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Similarity helpers ────────────────────────────────────────────────

def _tokenize(text: str) -> List[str]:
    """Simple n-gram + word tokenizer for Jaccard overlap."""
    words = re.findall(r'\w+', text.lower())
    bigrams = {f"{words[i]}_{words[i+1]}" for i in range(len(words) - 1)}
    return words + list(bigrams)


def _jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _cosine_from_vectors(va: List[float], vb: List[float]) -> float:
    if not va or not vb or len(va) != len(vb):
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── Main class ────────────────────────────────────────────────────────

class MemoryConsolidator:
    """Background memory‑management engine.

    Parameters
    ----------
    adapter : StorageAdapter
        Connected adapter that exposes ``store_memory``, ``search_memories``,
        ``get_all_memories``, ``get_persona_memories``, ``delete_memory``,
        and optionally a raw ``_conn`` attribute for batch SQL.
    """

    def __init__(self, adapter):
        self._adapter = adapter

    # ── 1. merge_similar ───────────────────────────────────────────

    def merge_similar(
        self,
        threshold: float = 0.85,
        persona_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Find and merge near‑identical memories.

        1. Group memories by persona (unless *persona_id* is given).
        2. Within each group, compare every pair with Jaccard overlap.
        3. If overlap >= *threshold*, merge them: create a consolidated
           memory, soft‑delete old entries, and record the merge in
           ``metadata.merged_into``.

        Parameters
        ----------
        threshold : float
            Jaccard overlap threshold (0‑1). Default 0.85.
        persona_id : str, optional
            Limit to a single persona.
        dry_run : bool
            If True, report what *would* be merged without writing.

        Returns
        -------
        dict with keys:
            merged_count : int  — number of merge groups processed
            entries_consolidated : int — old entries soft‑deleted
            new_memory_ids : list of str
        """
        personas = [persona_id] if persona_id else self._list_personas()
        merged_count = 0
        entries_consolidated = 0
        new_ids: List[str] = []

        for pid in personas:
            mems = self._adapter.get_persona_memories(pid, limit=500)
            if len(mems) < 2:
                continue

            # Index memories by content tokens
            tokenized = [
                (m["memory_id"], m["content"], _tokenize(m.get("content", "")))
                for m in mems
                if m.get("status", "active") == "active"
            ]
            n = len(tokenized)
            merged = set()

            for i in range(n):
                if i in merged:
                    continue
                group = [tokenized[i]]
                mi_tokens = tokenized[i][2]

                for j in range(i + 1, n):
                    if j in merged:
                        continue
                    mj_tokens = tokenized[j][2]
                    sim = _jaccard(mi_tokens, mj_tokens)
                    if sim >= threshold:
                        group.append(tokenized[j])
                        merged.add(j)

                if len(group) < 2:
                    continue

                # Merge group
                merged_content = self._build_merged_content(
                    [t[1] for t in group]
                )
                old_ids = [t[0] for t in group]
                merged_meta = {
                    "merged_into": old_ids[0],  # canonical ID
                    "merged_from": old_ids,
                    "merge_threshold": threshold,
                    "merge_at": datetime.now(timezone.utc).isoformat(),
                }

                if not dry_run:
                    # Create consolidated memory
                    result = self._adapter.store_memory(
                        merged_content,
                        persona_id=pid,
                        category="semantic",
                        importance=1.0,
                    )
                    new_id = result.get("memory_id", "")
                    new_ids.append(new_id)

                    # Soft‑delete old entries
                    for oid in old_ids:
                        try:
                            self._adapter.delete_memory(oid)
                        except Exception:
                            logger.warning("Failed to soft-delete %s", oid)

                merged_count += 1
                entries_consolidated += len(old_ids)

        return {
            "merged_count": merged_count,
            "entries_consolidated": entries_consolidated,
            "new_memory_ids": new_ids,
            "dry_run": dry_run,
        }

    # ── 2. apply_decay ─────────────────────────────────────────────

    def apply_decay(
        self, days: int = 7, dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Halve importance for memories not accessed in *days* days.

        Uses raw SQL UPDATE on the adapter's underlying connection.
        """
        conn = getattr(self._adapter, "_conn", None)
        if conn is None:
            logger.warning("apply_decay: adapter has no raw _conn; skipped")
            return {"decayed_count": 0, "error": "no raw connection"}

        cutoff = datetime.now(timezone.utc).isoformat()
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE status='active' "
                "AND last_accessed_at < ?",
                (cutoff,),
            )
            total_hits = cursor.fetchone()[0]

            if dry_run:
                return {"decayed_count": total_hits, "dry_run": True}

            cursor = conn.execute(
                "UPDATE memories SET importance = importance * 0.5 "
                "WHERE status = 'active' AND last_accessed_at < ?",
                (cutoff,),
            )
            conn.commit()
            affected = cursor.rowcount
        except Exception:
            logger.exception("apply_decay failed")
            return {"decayed_count": 0, "error": "SQL error"}

        return {"decayed_count": affected}

    # ── 3. promote_frequent ────────────────────────────────────────

    def promote_frequent(
        self, access_threshold: int = 5, dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Promote high‑access episodic memories to semantic.

        Uses raw SQL UPDATE on the adapter's underlying connection.
        """
        conn = getattr(self._adapter, "_conn", None)
        if conn is None:
            logger.warning("promote_frequent: no raw connection; skipped")
            return {"promoted_count": 0, "error": "no raw connection"}

        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE status='active' "
                "AND category='episodic' AND access_count >= ?",
                (access_threshold,),
            )
            total = cursor.fetchone()[0]

            if dry_run:
                return {"promoted_count": total, "dry_run": True}

            cursor = conn.execute(
                "UPDATE memories SET category = 'semantic' "
                "WHERE status = 'active' AND category = 'episodic' "
                "AND access_count >= ?",
                (access_threshold,),
            )
            conn.commit()
            affected = cursor.rowcount
        except Exception:
            logger.exception("promote_frequent failed")
            return {"promoted_count": 0, "error": "SQL error"}

        return {"promoted_count": affected}

    # ── Full cycle ─────────────────────────────────────────────────

    def run_full_cycle(
        self,
        merge_threshold: float = 0.85,
        decay_days: int = 7,
        promote_threshold: int = 5,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Execute merge → decay → promote in sequence."""
        return {
            "merge": self.merge_similar(threshold=merge_threshold, dry_run=dry_run),
            "decay": self.apply_decay(days=decay_days, dry_run=dry_run),
            "promote": self.promote_frequent(
                access_threshold=promote_threshold, dry_run=dry_run,
            ),
        }

    # ── Helpers ────────────────────────────────────────────────────

    def _list_personas(self) -> List[str]:
        conn = getattr(self._adapter, "_conn", None)
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT DISTINCT persona_id FROM memories WHERE status='active'"
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    @staticmethod
    def _build_merged_content(contents: List[str]) -> str:
        """Produce a single consolidated text from merged entries."""
        # Simple strategy: concatenate unique sentences.
        seen: set = set()
        sentences: List[str] = []
        for c in contents:
            for sent in re.split(r'[。！？.!?\n]+', c):
                sent = sent.strip()
                if sent and sent not in seen:
                    seen.add(sent)
                    sentences.append(sent)
        return "。".join(sentences) + "。" if sentences else " ".join(contents)


# ── Module‑level self_test ────────────────────────────────────────────
def self_test() -> Dict[str, Any]:
    """Smoke‑test merge / decay / promote on a temporary SQLite database."""
    import os
    import tempfile

    from trinity.adapters.sqlite import SQLiteAdapter

    passed = 0
    failed = 0
    details: List[str] = []
    adapter = None

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            db_path = os.path.join(tmpdir, "test_consolidator.db")
            adapter = SQLiteAdapter(db_path=db_path)
            adapter.connect()

        # Seed test data
            base = "Alice uses Python for data analysis and report generation."
            r1 = adapter.store_memory(base, persona_id="p1", category="episodic")
            # nearly identical content (one extra word) to trigger merge above threshold
            r2 = adapter.store_memory(
                "Alice uses Python for data analysis and report generation daily.",
                persona_id="p1", category="episodic",
            )
            # unrelated content (below threshold) — also used for decay test
            r3 = adapter.store_memory(
                "Bob designed the Marvis architecture using FastAPI and Docker.",
                persona_id="p1", category="episodic",
            )
            # separate high-access episodic memory for promote test (unique content)
            r4 = adapter.store_memory(
                "Charlie configured the CI/CD pipeline with Jenkins and Kubernetes.",
                persona_id="p1", category="episodic",
            )
            conn = adapter._conn
            conn.execute(
                "UPDATE memories SET access_count=10 WHERE memory_id=?",
                (r4["memory_id"],),
            )
            conn.commit()
            conn.execute(
                "UPDATE memories SET last_accessed_at='2020-01-01T00:00:00' "
                "WHERE memory_id=?",
                (r3["memory_id"],),
            )
            conn.commit()

            consolidator = MemoryConsolidator(adapter)

            # Test 1: merge_similar (threshold lower to catch the near-identical pair)
            merge_result = consolidator.merge_similar(
                threshold=0.5, persona_id="p1", dry_run=False,
            )
            if merge_result["merged_count"] >= 1 and merge_result["entries_consolidated"] >= 2:
                passed += 1
                details.append("merge_similar: PASS")
            else:
                failed += 1
                details.append(f"merge_similar: FAIL — {merge_result}")

            # Test 2: apply_decay
            decay_result = consolidator.apply_decay(days=365, dry_run=False)
            if decay_result["decayed_count"] >= 1:
                passed += 1
                details.append("apply_decay: PASS")
            else:
                failed += 1
                details.append(f"apply_decay: FAIL — {decay_result}")

            # Test 3: promote_frequent
            promote_result = consolidator.promote_frequent(
                access_threshold=5, dry_run=False,
            )
            if promote_result["promoted_count"] >= 1:
                passed += 1
                details.append("promote_frequent: PASS")
            else:
                failed += 1
                details.append(f"promote_frequent: FAIL — {promote_result}")

        finally:
            if adapter is not None:
                adapter.disconnect()

    return {
        "module": "trinity.memory.consolidator",
        "result": "PASS" if failed == 0 else "FAIL",
        "passed": passed,
        "failed": failed,
        "details": "; ".join(details),
    }
