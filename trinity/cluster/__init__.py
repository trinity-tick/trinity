"""
P2-2: Trinity Distributed Cluster Sub-package
==============================================

Raft 共识 + 分片记忆存储 + 向量索引分布式查询。

子模块:
  - raft: Raft 共识算法（Leader Election + Log Replication）
  - shard_memory: 一致性哈希分片记忆存储
  - dist_vector: 分布式向量索引（scatter-gather）

Version: 1.0.0
"""

from trinity.cluster.raft import RaftNode, RaftState, LogEntry, RaftCluster
from trinity.cluster.shard_memory import (
    ShardMemoryStore,
    ConsistentHashRing,
    ShardConfig,
    ShardStats,
)
from trinity.cluster.dist_vector import (
    DistVectorIndex,
    VectorShard,
    VectorSearchQuery,
    VectorSearchResponse,
)

__all__ = [
    "RaftNode",
    "RaftState",
    "LogEntry",
    "RaftCluster",
    "ShardMemoryStore",
    "ConsistentHashRing",
    "ShardConfig",
    "ShardStats",
    "DistVectorIndex",
    "VectorShard",
    "VectorSearchQuery",
    "VectorSearchResponse",
]

# ── Package-level self_test ──────────────────────────────────────────────

import json

def self_test() -> dict:
    """全子包集成自检。"""
    from trinity.cluster.raft import self_test as raft_st
    from trinity.cluster.shard_memory import self_test as shard_st
    from trinity.cluster.dist_vector import self_test as vec_st

    r = raft_st()
    s = shard_st()
    v = vec_st()

    total_passed = r["passed"] + s["passed"] + v["passed"]
    total_failed = r["failed"] + s["failed"] + v["failed"]

    return {
        "module": "P2-2_cluster",
        "raft": r, "shard_memory": s, "dist_vector": v,
        "total_passed": total_passed, "total_failed": total_failed,
        "all_pass": total_failed == 0,
    }


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2, ensure_ascii=False))
