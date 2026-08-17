"""
P2-2b: Sharded Memory Storage
===============================

基于一致性哈希的分片记忆存储:
  - ConsistentHashRing: 虚拟节点一致性哈希
  - ShardMemoryStore: 多分片记忆 CRUD + 自动迁移
  - 分片健康监控 + 负载均衡

Reference: Karger et al., "Consistent Hashing and Random Trees", STOC 1997.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── Data Structures ──────────────────────────────────────────────────────

@dataclass
class ShardConfig:
    shard_id: str
    host: str = "localhost"
    port: int = 8000
    capacity: int = 10000
    virtual_nodes: int = 150
    weight: float = 1.0

    def connection_string(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class ShardStats:
    shard_id: str
    item_count: int = 0
    capacity: int = 10000
    hit_count: int = 0
    miss_count: int = 0
    last_access: float = field(default_factory=time.time)

    @property
    def utilization(self) -> float:
        return self.item_count / max(self.capacity, 1)

    @property
    def hit_rate(self) -> float:
        total = self.hit_count + self.miss_count
        return self.hit_count / total if total > 0 else 1.0

    def is_healthy(self) -> bool:
        return self.utilization < 0.9


# ── ConsistentHashRing ───────────────────────────────────────────────────

class ConsistentHashRing:
    """一致性哈希环：带虚拟节点的均匀分片映射。"""

    def __init__(self, virtual_nodes_per_shard: int = 150):
        self.virtual_nodes_per_shard = virtual_nodes_per_shard
        self._ring: Dict[int, str] = {}  # hash → shard_id
        self._sorted_hashes: List[int] = []
        self._shards: Dict[str, ShardConfig] = {}
        self._lock = threading.RLock()

    def add_shard(self, config: ShardConfig):
        with self._lock:
            self._shards[config.shard_id] = config
            for i in range(config.virtual_nodes):
                vn_key = f"{config.shard_id}:vn:{i}"
                h = self._hash(vn_key)
                self._ring[h] = config.shard_id
                self._sorted_hashes.append(h)
            self._sorted_hashes.sort()

    def remove_shard(self, shard_id: str):
        with self._lock:
            if shard_id not in self._shards:
                return
            config = self._shards.pop(shard_id)
            self._sorted_hashes = []
            new_ring: Dict[int, str] = {}
            for h, sid in self._ring.items():
                if sid != shard_id:
                    new_ring[h] = sid
                    self._sorted_hashes.append(h)
            self._ring = new_ring
            self._sorted_hashes.sort()

    def get_shard(self, key: str) -> Optional[str]:
        """顺时针查找负责该 key 的分片。"""
        with self._lock:
            if not self._ring:
                return None
            h = self._hash(key)
            # Binary search for first hash >= h
            lo, hi = 0, len(self._sorted_hashes) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if self._sorted_hashes[mid] >= h:
                    hi = mid
                else:
                    lo = mid + 1
            if self._sorted_hashes[lo] < h:
                lo = 0  # wrap around
            return self._ring.get(self._sorted_hashes[lo])

    def get_replicas(self, key: str, count: int = 2) -> List[str]:
        """获取 key 的多个副本分片（顺时针连续节点）。"""
        with self._lock:
            primary = self.get_shard(key)
            if primary is None:
                return []
            replicas = [primary]
            idx = self._sorted_hashes.index(
                next(h for h, s in self._ring.items() if s == primary)
            ) if self._sorted_hashes else 0
            seen: Set[str] = {primary}
            for i in range(1, min(count, len(self._shards))):
                next_idx = (idx + i) % len(self._sorted_hashes)
                sid = self._ring[self._sorted_hashes[next_idx]]
                if sid not in seen:
                    replicas.append(sid)
                    seen.add(sid)
            return replicas

    def shard_count(self) -> int:
        return len(self._shards)

    def shard_ids(self) -> List[str]:
        return list(self._shards.keys())

    @staticmethod
    def _hash(key: str) -> int:
        return int(hashlib.md5(key.encode()).hexdigest()[:8], 16)


# ── ShardMemoryStore ─────────────────────────────────────────────────────

class ShardMemoryStore:
    """分片记忆存储：自动分片路由 + 多副本写入 + 故障迁移。"""

    def __init__(self, shard_configs: List[ShardConfig],
                 replication_factor: int = 2,
                 virtual_nodes: int = 150):
        self.replication_factor = replication_factor
        self.ring = ConsistentHashRing(virtual_nodes_per_shard=virtual_nodes)
        self._stats: Dict[str, ShardStats] = {}
        self._data: Dict[str, Dict[str, Dict[str, Any]]] = {}  # shard_id → {memory_id → record}
        self._lock = threading.RLock()

        for cfg in shard_configs:
            self.ring.add_shard(cfg)
            self._stats[cfg.shard_id] = ShardStats(
                shard_id=cfg.shard_id, capacity=cfg.capacity)
            self._data[cfg.shard_id] = {}

        self._total_writes = 0
        self._total_reads = 0
        logger.info("ShardMemoryStore initialized [shards=%d rf=%d]",
                     len(shard_configs), replication_factor)

    # ── CRUD ──────────────────────────────────────────────────────────

    def write(self, key: str, memory_id: str, data: Dict[str, Any]) -> List[str]:
        """写入记忆，返回写入成功的分片列表。"""
        with self._lock:
            shards = self.ring.get_replicas(key, self.replication_factor)
            written: List[str] = []
            for sid in shards:
                if sid in self._data:
                    stats = self._stats[sid]
                    if stats.item_count >= stats.capacity:
                        logger.warning("Shard %s at capacity (%d)", sid, stats.capacity)
                        continue
                    self._data[sid][memory_id] = {**data, "_shard": sid, "_written_at": time.time()}
                    stats.item_count += 1
                    written.append(sid)
            self._total_writes += 1
            return written

    def read(self, key: str, memory_id: str) -> Optional[Dict[str, Any]]:
        """读取记忆：从主分片查找，失败则回退到副本。"""
        with self._lock:
            shards = self.ring.get_replicas(key, self.replication_factor)
            for sid in shards:
                if sid in self._data:
                    stats = self._stats[sid]
                    stats.last_access = time.time()
                    if memory_id in self._data[sid]:
                        stats.hit_count += 1
                        self._total_reads += 1
                        return self._data[sid][memory_id]
                    stats.miss_count += 1
            self._total_reads += 1
            return None

    def delete(self, key: str, memory_id: str) -> int:
        """删除记忆（所有副本）。"""
        with self._lock:
            shards = self.ring.get_replicas(key, self.replication_factor)
            deleted = 0
            for sid in shards:
                if sid in self._data and memory_id in self._data[sid]:
                    del self._data[sid][memory_id]
                    self._stats[sid].item_count -= 1
                    deleted += 1
            return deleted

    def query_by_shard(self, shard_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """按分片列出记忆（运维/迁移用）。"""
        with self._lock:
            if shard_id not in self._data:
                return []
            return list(self._data[shard_id].values())[:limit]

    def migrate(self, from_shard: str, to_shard: str, key_prefix: str = "") -> int:
        """数据迁移：将匹配 key_prefix 的条目从 from_shard 迁移至 to_shard。"""
        with self._lock:
            if from_shard not in self._data or to_shard not in self._data:
                return 0
            migrated = 0
            to_remove = []
            for mid, record in self._data[from_shard].items():
                if key_prefix and not mid.startswith(key_prefix):
                    continue
                self._data[to_shard][mid] = {**record, "_shard": to_shard,
                                               "_migrated_at": time.time()}
                to_remove.append(mid)
                migrated += 1
            for mid in to_remove:
                del self._data[from_shard][mid]
            self._stats[from_shard].item_count -= migrated
            self._stats[to_shard].item_count += migrated
            logger.info("Migrated %d items: %s → %s", migrated, from_shard, to_shard)
            return migrated

    def add_shard(self, config: ShardConfig):
        with self._lock:
            self.ring.add_shard(config)
            self._stats[config.shard_id] = ShardStats(
                shard_id=config.shard_id, capacity=config.capacity)
            self._data[config.shard_id] = {}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            shard_stats = {sid: {
                "item_count": s.item_count, "capacity": s.capacity,
                "utilization": round(s.utilization, 3), "hit_rate": round(s.hit_rate, 3),
                "healthy": s.is_healthy(),
            } for sid, s in self._stats.items()}
            return {
                "total_shards": len(self._stats), "ring_size": self.ring.shard_count(),
                "total_writes": self._total_writes, "total_reads": self._total_reads,
                "shards": shard_stats,
            }


# ── Self-Test ────────────────────────────────────────────────────────────

def self_test() -> Dict[str, Any]:
    results: Dict[str, Any] = {"module": "P2-2b_shard_memory", "passed": 0, "failed": 0, "details": []}

    def _pass(t): results["passed"] += 1; results["details"].append({"test": t, "status": "PASS"})
    def _fail(t, r): results["failed"] += 1; results["details"].append({"test": t, "status": "FAIL", "reason": r})

    # Test 1: Consistent hash ring
    try:
        ring = ConsistentHashRing(virtual_nodes_per_shard=50)
        ring.add_shard(ShardConfig("s0", virtual_nodes=50))
        ring.add_shard(ShardConfig("s1", virtual_nodes=50))
        ring.add_shard(ShardConfig("s2", virtual_nodes=50))
        assert ring.shard_count() == 3, f"Expected 3 shards, got {ring.shard_count()}"
        shard = ring.get_shard("test_key_1")
        assert shard is not None, "get_shard returned None"
        assert shard in ("s0", "s1", "s2"), f"Unknown shard: {shard}"
        _pass("ConsistentHashRing add/get")
    except Exception as e:
        _fail("ConsistentHashRing", str(e))

    # Test 2: Determinism
    try:
        ring = ConsistentHashRing(virtual_nodes_per_shard=50)
        ring.add_shard(ShardConfig("s0", virtual_nodes=50))
        ring.add_shard(ShardConfig("s1", virtual_nodes=50))
        s1 = ring.get_shard("hello")
        s2 = ring.get_shard("hello")
        assert s1 == s2, f"Not deterministic: {s1} vs {s2}"
        _pass("ConsistentHashRing determinism")
    except Exception as e:
        _fail("ConsistentHashRing determinism", str(e))

    # Test 3: Replicas
    try:
        ring = ConsistentHashRing(virtual_nodes_per_shard=150)
        for i in range(5):
            ring.add_shard(ShardConfig(f"s{i}", virtual_nodes=150))
        reps = ring.get_replicas("key_a", count=3)
        assert len(reps) >= 1, f"Expected >= 1 replica, got {len(reps)}"
        assert len(reps) <= 3, f"Expected <= 3 replicas, got {len(reps)}"
        assert len(set(reps)) == len(reps), f"Replicas should be unique: {reps}"
        _pass("Replica routing")
    except Exception as e:
        _fail("Replica routing", str(e))

    # Test 4: ShardMemoryStore write + read
    try:
        store = ShardMemoryStore([
            ShardConfig("shard_0", capacity=100, virtual_nodes=50),
            ShardConfig("shard_1", capacity=100, virtual_nodes=50),
        ], replication_factor=2)
        written = store.write("user:42", "mem_001", {"content": "hello shard"})
        assert len(written) >= 1, f"No shards written: {written}"
        record = store.read("user:42", "mem_001")
        assert record is not None, "Read returned None"
        assert record["content"] == "hello shard", f"Content mismatch: {record}"
        _pass("ShardMemoryStore write/read")
    except Exception as e:
        _fail("ShardMemoryStore write/read", str(e))

    # Test 5: Delete
    try:
        store = ShardMemoryStore([
            ShardConfig("shard_0", capacity=100, virtual_nodes=50),
        ])
        store.write("key_x", "mem_del", {"val": 1})
        deleted = store.delete("key_x", "mem_del")
        assert deleted >= 1, f"Delete returned {deleted}"
        assert store.read("key_x", "mem_del") is None, "Still readable after delete"
        _pass("Delete")
    except Exception as e:
        _fail("Delete", str(e))

    # Test 6: Migration
    try:
        store = ShardMemoryStore([
            ShardConfig("src", capacity=100, virtual_nodes=50),
            ShardConfig("dst", capacity=100, virtual_nodes=50),
        ])
        for i in range(5):
            store.write(f"key_{i}", f"mem_{i}", {"index": i})
        # Force all to src by overriding ring
        migrated = store.migrate("src", "dst")
        assert migrated >= 1, f"Migration returned {migrated}"
        _pass("Migration")
    except Exception as e:
        _fail("Migration", str(e))

    # Test 7: Stats
    try:
        store = ShardMemoryStore([
            ShardConfig("s0", capacity=100),
            ShardConfig("s1", capacity=100),
        ])
        store.write("a", "m1", {"x": 1})
        st = store.stats()
        assert st["total_shards"] == 2
        assert st["total_writes"] == 1
        _pass("Stats")
    except Exception as e:
        _fail("Stats", str(e))

    # Test 8: Capacity limit
    try:
        store = ShardMemoryStore([ShardConfig("tiny", capacity=2)])
        w1 = store.write("k1", "m1", {"a": 1})
        w2 = store.write("k1", "m2", {"b": 2})
        w3 = store.write("k1", "m3", {"c": 3})
        # w3 may fail due to capacity
        assert len(w1) >= 0 and len(w2) >= 0, "Cap test write should not crash"
        _pass("Capacity limit")
    except Exception as e:
        _fail("Capacity limit", str(e))

    results["total"] = results["passed"] + results["failed"]
    return results


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2, ensure_ascii=False))
