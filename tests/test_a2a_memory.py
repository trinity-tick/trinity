"""Tests for A2A memory sync (trinity.a2a_memory)."""

import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.a2a_memory import (
    A2AMemorySync,
    MemoryEntry,
    ConflictResolution,
    create_memory_entry,
)
from trinity.a2a_registry import AgentRegistry, AgentInfo


class TestMemoryEntry:
    def test_create_defaults(self):
        entry = create_memory_entry("hello world")
        assert entry.memory_id.startswith("mem_")
        assert entry.content == "hello world"
        assert entry.persona_id == "default"
        assert entry.source_agent == "local"
        assert entry.sha256_hash  # hash is computed

    def test_create_custom(self):
        entry = create_memory_entry(
            content="test", persona_id="user1",
            tenant_id="acme", source_agent="alpha",
            importance=0.9, tags=["tag1"],
        )
        assert entry.persona_id == "user1"
        assert entry.tenant_id == "acme"
        assert entry.importance == 0.9
        assert "tag1" in entry.tags

    def test_sha256_consistency(self):
        e1 = create_memory_entry("same content")
        e2 = create_memory_entry("same content")
        assert e1.sha256_hash == e2.sha256_hash

    def test_sha256_different(self):
        e1 = create_memory_entry("content A")
        e2 = create_memory_entry("content B")
        assert e1.sha256_hash != e2.sha256_hash


class TestConflictResolution:
    def setup_method(self):
        self.local = MemoryEntry(
            memory_id="mem_1", content="local",
            persona_id="t", tenant_id="d",
            source_agent="alpha", version=1, timestamp=1000,
            sha256_hash="aaa",
        )
        self.remote = MemoryEntry(
            memory_id="mem_1", content="remote",
            persona_id="t", tenant_id="d",
            source_agent="beta", version=2, timestamp=2000,
            sha256_hash="bbb",
        )

    def test_local_wins(self):
        m = ConflictResolution.resolve_local_wins(self.local, self.remote)
        assert m.content == "local"

    def test_remote_wins(self):
        m = ConflictResolution.resolve_remote_wins(self.local, self.remote)
        assert m.content == "remote"

    def test_newest_wins(self):
        m = ConflictResolution.resolve_newest_wins(self.local, self.remote)
        assert m.content == "remote"  # remote has higher timestamp

    def test_highest_version(self):
        m = ConflictResolution.resolve_highest_version(self.local, self.remote)
        assert m.version == 2

    def test_merge(self):
        m = ConflictResolution.resolve_merge(self.local, self.remote)
        assert "local" in m.content
        assert "remote" in m.content
        assert m.version == 3

    def test_merge_identical(self):
        same = MemoryEntry(
            memory_id="mem_1", content="same",
            persona_id="t", tenant_id="d",
            source_agent="alpha", sha256_hash="abc",
        )
        same_remote = MemoryEntry(
            memory_id="mem_1", content="same",
            persona_id="t", tenant_id="d",
            source_agent="beta", sha256_hash="abc",
        )
        m = ConflictResolution.resolve_merge(same, same_remote)
        assert m.content == "same"  # no merge needed


class TestA2AMemorySync:
    def setup_method(self):
        import tempfile
        self.tmp_dir = tempfile.mkdtemp(prefix="a2a_test_")
        self.registry = AgentRegistry(db_path=f"{self.tmp_dir}/registry.json")
        self.local_store = []
        self.sync = A2AMemorySync(
            local_agent_id="trinity-test",
            registry=self.registry,
            local_store=lambda e: self.local_store.append(e) or True,
            local_search=lambda q, k: [{"content": e.content, "score": 1.0} for e in self.local_store][:k],
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_register_self(self):
        """Instance registers itself on creation"""
        agents = self.registry.discover()
        assert any(a.agent_id == "trinity-test" for a in agents)

    def test_heartbeat(self):
        """Heartbeat keeps registration alive"""
        self.sync.heartbeat()
        self.registry.heartbeat("trinity-test")
        agents = self.registry.discover()
        assert any(a.agent_id == "trinity-test" for a in agents)

    def test_discover_peers_empty(self):
        """No peers when only self is registered"""
        peers = self.sync.discover_peers()
        assert len(peers) == 0

    def test_discover_peers_with_other(self):
        """Discover another instance"""
        self.registry.register(AgentInfo(
            agent_id="trinity-beta", name="Beta", version="1.0",
            capabilities=["memory.search"], endpoint="mem://beta",
            status="active", last_heartbeat=time.time(),
        ))
        peers = self.sync.discover_peers()
        assert len(peers) == 1
        assert peers[0].agent_id == "trinity-beta"

    def test_search_peers(self):
        """Search across peers"""
        self.registry.register(AgentInfo(
            agent_id="trinity-beta", name="Beta", version="1.0",
            capabilities=["memory.search"], endpoint="mem://beta",
            status="active", last_heartbeat=time.time(),
        ))
        from trinity.a2a_memory import create_memory_entry
        entry = create_memory_entry("hello world from alpha")
        self.local_store.append(entry)
        results = self.sync.search_peers("hello")
        # Should find at least the local store (via callback)
        assert isinstance(results, dict)

    def test_share_to_peer(self):
        """Share a memory entry to a peer"""
        self.registry.register(AgentInfo(
            agent_id="trinity-beta", name="Beta", version="1.0",
            capabilities=["memory.store"], endpoint="mem://beta",
            status="active", last_heartbeat=time.time(),
        ))
        entry = create_memory_entry("shared content")
        result = self.sync.share_to_peer("trinity-beta", entry)
        assert result.success
        assert result.entries_count == 1

    def test_share_to_all(self):
        """Broadcast to all peers"""
        self.registry.register(AgentInfo(
            agent_id="trinity-beta", name="Beta", version="1.0",
            capabilities=["memory.store"], endpoint="mem://beta",
            status="active", last_heartbeat=time.time(),
        ))
        self.registry.register(AgentInfo(
            agent_id="trinity-gamma", name="Gamma", version="1.0",
            capabilities=["memory.store"], endpoint="mem://gamma",
            status="active", last_heartbeat=time.time(),
        ))
        entry = create_memory_entry("broadcast content")
        results = self.sync.share_to_all(entry)
        assert len(results) == 2
        assert all(r.success for r in results)

    def test_detect_conflicts(self):
        """Detect version conflicts by sha256 hash"""
        local = MemoryEntry(memory_id="m1", content="a", persona_id="t",
                            tenant_id="d", source_agent="alpha",
                            sha256_hash="hash_a")
        remote = MemoryEntry(memory_id="m1", content="b", persona_id="t",
                             tenant_id="d", source_agent="beta",
                             sha256_hash="hash_b")
        conflicts = self.sync.detect_conflicts([local], [remote])
        assert len(conflicts) == 1

    def test_no_conflict_for_same_hash(self):
        """Same content should not trigger conflict"""
        local = MemoryEntry(memory_id="m1", content="same", persona_id="t",
                            tenant_id="d", source_agent="alpha",
                            sha256_hash="abc")
        remote = MemoryEntry(memory_id="m1", content="same", persona_id="t",
                             tenant_id="d", source_agent="beta",
                             sha256_hash="abc")
        conflicts = self.sync.detect_conflicts([local], [remote])
        assert len(conflicts) == 0

    def test_get_stats(self):
        """Stats should return meaningful info"""
        stats = self.sync.get_stats()
        assert stats["local_agent"] == "trinity-test"
        assert "online_peers" in stats
        assert "sync_operations" in stats
