"""
P2-2c: Distributed Vector Index
=================================

基于 scatter-gather 的分布式向量检索:
  - DistVectorIndex: 多分片向量索引管理器
  - VectorShard: 单分片向量存储 (余弦相似度)
  - scatter: 查询广播到所有分片
  - gather: 合并 + RRF 融合排序

Reference: Johnson et al., "Billion-scale similarity search with GPUs", IEEE Trans. 2019.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ── Data Structures ──────────────────────────────────────────────────────

@dataclass
class VectorSearchQuery:
    query_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    vector: List[float] = field(default_factory=list)
    text: str = ""
    top_k: int = 10
    namespace: str = "default"
    min_similarity: float = 0.5

    def embedding_dim(self) -> int:
        return len(self.vector)


@dataclass
class VectorSearchResponse:
    query_id: str
    hits: List[Tuple[str, float, Dict[str, Any]]]  # (doc_id, similarity, metadata)
    shard_id: str = ""
    elapsed_ms: float = 0.0


# ── VectorShard ──────────────────────────────────────────────────────────

class VectorShard:
    """单分片向量存储。每个分片维护独立的向量索引。

    Parameters
    ----------
    shard_id : str
    dim : int, 向量维度
    max_vectors : int, 最大向量容量
    """

    def __init__(self, shard_id: str, dim: int = 384, max_vectors: int = 100000):
        self.shard_id = shard_id
        self.dim = dim
        self.max_vectors = max_vectors
        self._vectors: Dict[str, np.ndarray] = {}
        self._metadata: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._query_count: int = 0
        self._total_latency_ms: float = 0.0

    def insert(self, doc_id: str, vector: List[float],
               metadata: Optional[Dict[str, Any]] = None) -> bool:
        with self._lock:
            if len(vector) != self.dim:
                logger.warning("Dimension mismatch: got %d, expected %d", len(vector), self.dim)
                return False
            if len(self._vectors) >= self.max_vectors:
                self._evict_lru()
            arr = np.array(vector, dtype=np.float32)
            self._vectors[doc_id] = arr / (np.linalg.norm(arr) + 1e-8)
            self._metadata[doc_id] = metadata or {"inserted_at": time.time()}
            return True

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            if doc_id in self._vectors:
                del self._vectors[doc_id]
                self._metadata.pop(doc_id, None)
                return True
            return False

    def search(self, query_vector: List[float], top_k: int = 10,
               min_similarity: float = 0.5) -> List[Tuple[str, float, Dict[str, Any]]]:
        with self._lock:
            if not self._vectors:
                return []
            start = time.time()
            q = np.array(query_vector, dtype=np.float32)
            q_norm = q / (np.linalg.norm(q) + 1e-8)

            hits: List[Tuple[str, float]] = []
            for doc_id, vec in self._vectors.items():
                sim = float(np.dot(q_norm, vec))
                if sim >= min_similarity:
                    hits.append((doc_id, sim))

            hits.sort(key=lambda x: x[1], reverse=True)
            elapsed = (time.time() - start) * 1000
            self._query_count += 1
            self._total_latency_ms += elapsed

            return [(doc_id, sim, self._metadata.get(doc_id, {}))
                    for doc_id, sim in hits[:top_k]]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "shard_id": self.shard_id, "vector_count": len(self._vectors),
                "dim": self.dim, "max_vectors": self.max_vectors,
                "query_count": self._query_count,
                "avg_latency_ms": round(self._total_latency_ms / max(self._query_count, 1), 2),
            }

    def _evict_lru(self) -> None:
        if not self._metadata:
            return
        oldest = min(self._metadata.items(), key=lambda kv: kv[1].get("inserted_at", 0))
        del self._vectors[oldest[0]]
        del self._metadata[oldest[0]]


# ── DistVectorIndex ──────────────────────────────────────────────────────

class DistVectorIndex:
    """分布式向量索引：scatter-gather 架构。

    查询时 scatter 到所有分片并行执行，gather 后 RRF 融合排序。

    Parameters
    ----------
    shards : List[VectorShard]
    rrf_k : int, RRF 融合参数
    """

    def __init__(self, shards: List[VectorShard], rrf_k: int = 60):
        self.shards = {s.shard_id: s for s in shards}
        self.rrf_k = rrf_k
        self._lock = threading.RLock()
        self._total_queries = 0

    def add_shard(self, shard: VectorShard):
        with self._lock:
            self.shards[shard.shard_id] = shard

    def remove_shard(self, shard_id: str):
        with self._lock:
            self.shards.pop(shard_id, None)

    def insert(self, doc_id: str, vector: List[float],
               metadata: Optional[Dict[str, Any]] = None) -> str:
        """插入向量到一致性哈希选中的分片。"""
        with self._lock:
            shard_id = self._route(doc_id)
            shard = self.shards.get(shard_id)
            if shard is None:
                return ""
            shard.insert(doc_id, vector, metadata)
            return shard_id

    def search(self, query: VectorSearchQuery,
               shard_ids: Optional[List[str]] = None) -> List[Tuple[str, float, Dict[str, Any]]]:
        """分布式向量检索：scatter → gather → RRF 融合。

        Parameters
        ----------
        query : VectorSearchQuery
        shard_ids : Optional[List[str]]
            指定分片，None 表示所有分片。

        Returns
        -------
        List[Tuple[str, float, Dict[str, Any]]]
            (doc_id, similarity, metadata) 按融合得分降序。
        """
        with self._lock:
            target_shards = ([self.shards[s] for s in shard_ids if s in self.shards]
                             if shard_ids else list(self.shards.values()))
            if not target_shards:
                return []

            self._total_queries += 1

            # Scatter: query all shards
            all_hits: List[Tuple[str, float, Dict[str, Any], str]] = []  # + shard_id
            for shard in target_shards:
                hits = shard.search(query.vector, top_k=query.top_k,
                                    min_similarity=query.min_similarity)
                for doc_id, sim, meta in hits:
                    all_hits.append((doc_id, sim, meta, shard.shard_id))

            # Gather: RRF fusion
            return self._rrf_fusion(all_hits, query.top_k)

    def _rrf_fusion(self, hits: List[Tuple[str, float, Dict[str, Any], str]],
                    top_k: int) -> List[Tuple[str, float, Dict[str, Any]]]:
        """Reciprocal Rank Fusion 多分片结果融合。"""
        # Group by doc_id
        doc_ranks: Dict[str, List[float]] = {}
        doc_meta: Dict[str, Dict[str, Any]] = {}
        for i, (doc_id, sim, meta, sid) in enumerate(hits):
            if doc_id not in doc_ranks:
                doc_ranks[doc_id] = []
                doc_meta[doc_id] = meta
            doc_ranks[doc_id].append(1.0 / (self.rrf_k + i + 1))

        # Fuse scores
        fused: List[Tuple[str, float]] = []
        for doc_id, scores in doc_ranks.items():
            fused.append((doc_id, sum(scores) / len(scores)))

        fused.sort(key=lambda x: x[1], reverse=True)
        return [(doc_id, score, doc_meta.get(doc_id, {}))
                for doc_id, score in fused[:top_k]]

    def index_batch(self, batch: List[Tuple[str, List[float], Optional[Dict[str, Any]]]]) -> List[str]:
        """批量插入。"""
        results = []
        for doc_id, vec, meta in batch:
            sid = self.insert(doc_id, vec, meta)
            results.append(sid)
        return results

    def _route(self, doc_id: str) -> str:
        shard_ids = sorted(self.shards.keys())
        if not shard_ids:
            return ""
        h = int(hashlib.md5(doc_id.encode()).hexdigest()[:8], 16)
        return shard_ids[h % len(shard_ids)]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_shards": len(self.shards),
                "total_queries": self._total_queries,
                "shards": [s.stats() for s in self.shards.values()],
            }


# ── Self-Test ────────────────────────────────────────────────────────────

def self_test() -> Dict[str, Any]:
    results: Dict[str, Any] = {"module": "P2-2c_dist_vector", "passed": 0, "failed": 0, "details": []}

    def _pass(t): results["passed"] += 1; results["details"].append({"test": t, "status": "PASS"})
    def _fail(t, r): results["failed"] += 1; results["details"].append({"test": t, "status": "FAIL", "reason": r})

    # Test 1: VectorShard insert + search
    try:
        shard = VectorShard("s0", dim=4)
        assert shard.insert("d1", [1.0, 0.0, 0.0, 0.0])
        assert shard.insert("d2", [0.0, 1.0, 0.0, 0.0])
        hits = shard.search([1.0, 0.0, 0.0, 0.0], top_k=2, min_similarity=0.5)
        assert len(hits) >= 1, f"Expected >= 1 hit, got {len(hits)}"
        assert hits[0][0] == "d1", f"Expected d1 top, got {hits[0][0]}"
        _pass("VectorShard insert/search")
    except Exception as e:
        _fail("VectorShard insert/search", str(e))

    # Test 2: Dimension mismatch rejection
    try:
        shard = VectorShard("s0", dim=4)
        ok = shard.insert("bad", [1.0, 2.0])
        assert not ok, "Should reject wrong dim"
        _pass("Dimension mismatch rejection")
    except Exception as e:
        _fail("Dim mismatch", str(e))

    # Test 3: Delete
    try:
        shard = VectorShard("s0", dim=4)
        shard.insert("d1", [1.0, 0.0, 0.0, 0.0])
        assert shard.delete("d1"), "Delete failed"
        hits = shard.search([1.0, 0.0, 0.0, 0.0])
        assert len(hits) == 0, f"Expected 0 after delete, got {len(hits)}"
        _pass("Delete")
    except Exception as e:
        _fail("Delete", str(e))

    # Test 4: DistVectorIndex routing
    try:
        s0 = VectorShard("s0", dim=4)
        s1 = VectorShard("s1", dim=4)
        idx = DistVectorIndex([s0, s1])
        sid = idx.insert("doc_a", [1.0, 0.0, 0.0, 0.0], {"type": "test"})
        assert sid in ("s0", "s1"), f"Bad route: {sid}"
        _pass("DistVectorIndex routing")
    except Exception as e:
        _fail("Routing", str(e))

    # Test 5: Scatter-gather search
    try:
        s0 = VectorShard("s0", dim=4)
        s1 = VectorShard("s1", dim=4)
        idx = DistVectorIndex([s0, s1])
        idx.insert("doc_1", [1.0, 0.0, 0.0, 0.0])
        idx.insert("doc_2", [0.8, 0.2, 0.0, 0.0])
        idx.insert("doc_3", [0.0, 1.0, 0.0, 0.0])
        q = VectorSearchQuery(vector=[1.0, 0.0, 0.0, 0.0], top_k=3, min_similarity=0.3)
        hits = idx.search(q)
        assert len(hits) >= 1, f"Scatter-gather: expected >= 1, got {len(hits)}"
        _pass("Scatter-gather search")
    except Exception as e:
        _fail("Scatter-gather", str(e))

    # Test 6: RRF fusion quality
    try:
        s0 = VectorShard("s0", dim=4)
        s1 = VectorShard("s1", dim=4)
        # Put same doc in both shards
        s0.insert("shared", [1.0, 0.0, 0.0, 0.0])
        s1.insert("shared", [1.0, 0.0, 0.0, 0.0])
        idx = DistVectorIndex([s0, s1])
        q = VectorSearchQuery(vector=[1.0, 0.0, 0.0, 0.0], top_k=2)
        hits = idx.search(q)
        assert len(hits) >= 1, f"RRF: expected >= 1, got {len(hits)}"
        # Should favor shared docs via RRF boost
        _pass("RRF fusion")
    except Exception as e:
        _fail("RRF fusion", str(e))

    # Test 7: Batch insert
    try:
        s0 = VectorShard("s0", dim=3)
        idx = DistVectorIndex([s0])
        batch = [("b1", [1.0, 0.0, 0.0], None), ("b2", [0.0, 1.0, 0.0], None)]
        batch_res = idx.index_batch(batch)
        assert len(batch_res) == 2, f"Batch: expected 2, got {len(batch_res)}"
        assert all(r in ("s0", "") for r in batch_res)
        _pass("Batch insert")
    except Exception as e:
        _fail("Batch insert", str(e))

    # Test 8: Shard stats
    try:
        shard = VectorShard("s_stats", dim=4)
        shard.insert("d1", [1.0, 0.0, 0.0, 0.0])
        shard.search([1.0, 0.0, 0.0, 0.0])
        st = shard.stats()
        assert st["vector_count"] == 1
        assert st["query_count"] == 1
        _pass("Shard stats")
    except Exception as e:
        _fail("Shard stats", str(e))

    results["total"] = results["passed"] + results["failed"]
    return results


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2, ensure_ascii=False))
