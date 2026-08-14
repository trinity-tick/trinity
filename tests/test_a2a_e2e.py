"""End-to-end A2A multi-agent memory sharing tests (M3-5).

把 scripts/a2a_demo.py 的演示流程固化为 pytest：
  (a) 两 agent 注册成功（AgentRegistry + AgentCard 签名）
  (b) 能力协商返回 success（NegotiationResult.compatible）
  (c) alpha 写入 N 条后 beta 同步后能检索到（SQLiteAdapter.search_memories）
  (d) 同一 memory_id 双方并发改不同内容时 ConflictResolution 生效
      （newest_wins 与 merge 两个策略）
  (e) 同步幂等（重复同步不产生重复条目）
"""

import dataclasses
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from trinity.a2a import (
    A2AProtocol,
    A2ARequest,
    A2AResponse,
    CapabilityRegistry,
    generate_card,
    verify_card,
)
from trinity.a2a_memory import (
    A2AMemorySync,
    AdapterMemoryStore,
    ConflictResolution,
    MemoryEntry,
    create_memory_entry,
)
from trinity.a2a_registry import AgentRegistry, AgentInfo
from trinity.adapters.sqlite import SQLiteAdapter

ALPHA = "agent-alpha"
BETA = "agent-beta"
CAPABILITIES = ["memory.search", "memory.store", "memory.sync", "memory.share"]


def _build_store_payload(entry, requester):
    """构造与 A2AMemorySync.share_to_peer 一致的 memory.store 负载。"""
    return {"action": "memory.store", "entry": dataclasses.asdict(entry), "requester": requester}


def _deliver(sender_sync, recipient_sync, recipient_store, entry):
    """模拟网络传输：sender 生成传输包 -> recipient 接收并合并。"""
    packet = sender_sync.transport.send(
        recipient_sync.local_agent_id,
        _build_store_payload(entry, sender_sync.local_agent_id),
    )
    assert packet, "transfer packet generation failed"
    return recipient_sync.receive_packet(packet, store=recipient_store)


@pytest.fixture()
def env(tmp_path):
    """一套完整的双智能体 A2A 环境（临时 SQLite + JSON registry）。"""
    registry = AgentRegistry(db_path=str(tmp_path / "registry.json"))
    cap_registry = CapabilityRegistry()

    for agent_id, role in ((ALPHA, "producer"), (BETA, "consumer")):
        registry.register(AgentInfo(
            agent_id=agent_id, name=f"{agent_id} Agent", version="1.0.0",
            capabilities=CAPABILITIES, endpoint=f"memory://{agent_id}",
            status="active", last_heartbeat=time.time(), metadata={"role": role},
        ))
    for agent_id in (ALPHA, BETA):
        card = generate_card(agent_id, name=f"{agent_id} Agent",
                             capabilities=CAPABILITIES)
        assert verify_card(card)["valid"]
        cap_registry.register_agent(card)

    alpha_db = SQLiteAdapter(str(tmp_path / "alpha.db"))
    beta_db = SQLiteAdapter(str(tmp_path / "beta.db"))
    alpha_db.connect()
    beta_db.connect()

    alpha_store = AdapterMemoryStore(alpha_db,
                                     resolver=ConflictResolution.resolve_newest_wins)
    beta_store = AdapterMemoryStore(beta_db,
                                    resolver=ConflictResolution.resolve_newest_wins)

    alpha_sync = A2AMemorySync(
        local_agent_id=ALPHA, registry=registry,
        local_store=alpha_store.put,
        local_search=lambda q, k: alpha_store.search(q, k),
        conflict_resolver=ConflictResolution.resolve_newest_wins,
    )
    beta_sync = A2AMemorySync(
        local_agent_id=BETA, registry=registry,
        local_store=beta_store.put,
        local_search=lambda q, k: beta_store.search(q, k),
        conflict_resolver=ConflictResolution.resolve_newest_wins,
    )

    protocol = A2AProtocol(cap_registry)

    yield {
        "registry": registry,
        "cap_registry": cap_registry,
        "protocol": protocol,
        "alpha_db": alpha_db,
        "beta_db": beta_db,
        "alpha_store": alpha_store,
        "beta_store": beta_store,
        "alpha_sync": alpha_sync,
        "beta_sync": beta_sync,
    }

    alpha_db.disconnect()
    beta_db.disconnect()


def _alpha_entries(n=4):
    """生成 n 条来自 agent-alpha 的测试记忆。"""
    return [
        create_memory_entry(
            content=f"alpha shared memory entry number {i} about dark mode",
            persona_id="default", tenant_id="default", source_agent=ALPHA,
            importance=round(0.5 + i * 0.1, 2), tags=["a2a", f"tag{i}"],
        )
        for i in range(1, n + 1)
    ]


class TestAgentRegistration:
    """(a) 两 agent 注册成功"""

    def test_both_agents_registered(self, env):
        agents = {a.agent_id for a in env["registry"].discover()}
        assert {ALPHA, BETA} <= agents

    def test_agent_cards_signed_and_valid(self, env):
        for agent_id in (ALPHA, BETA):
            card = env["cap_registry"].get_card(agent_id)
            assert card is not None, f"{agent_id} card missing"
            assert card.signed_card, f"{agent_id} card not signed"
            assert verify_card(card)["valid"], f"{agent_id} card signature invalid"

    def test_discover_peers(self, env):
        peers = {p.agent_id for p in env["beta_sync"].discover_peers()}
        assert ALPHA in peers
        assert BETA not in peers  # 排除自身


class TestNegotiation:
    """(b) 协商返回 success"""

    def test_negotiation_success(self, env):
        neg = env["protocol"].negotiate_capabilities(ALPHA, BETA)
        assert neg.compatible is True
        assert neg.from_agent == ALPHA
        assert neg.to_agent == BETA
        assert "memory.search" in neg.common_capabilities
        assert "memory.store" in neg.common_capabilities
        assert neg.negotiation_id

    def test_message_exchange_jsonrpc(self, env):
        """A2ARequest/A2AResponse JSON-RPC 风格消息交换。"""
        req = A2ARequest(id="req_e2e_001", method="memory.share",
                         params={"entries": [], "requester": ALPHA},
                         from_agent=ALPHA, to_agent=BETA)
        routed = env["protocol"].send_message(ALPHA, BETA, "memory.share",
                                              {"entries": []})
        assert routed["delivered"] is True
        resp = A2AResponse(id=req.id, result={"accepted": True, "entries_received": 0},
                           from_agent=BETA, to_agent=ALPHA)
        assert resp.result is not None and resp.result["accepted"] is True
        # JSON-RPC 序列化往返一致
        assert A2ARequest.from_dict(req.to_dict()) == req
        assert A2AResponse.from_dict(resp.to_dict()) == resp


class TestMemorySyncE2E:
    """(c) alpha 写入 -> beta 同步 -> 检索可见"""

    def test_alpha_writes_and_beta_retrieves(self, env):
        entries = _alpha_entries(4)
        for e in entries:
            assert env["alpha_sync"].store_local(e) is True

        for e in entries:
            r = _deliver(env["alpha_sync"], env["beta_sync"], env["beta_store"], e)
            assert r.success and r.entries_count == 1

        # beta 权威索引应有 4 条来自 alpha 的条目
        assert env["beta_store"].count(agent_id=ALPHA) == 4
        # beta 通过 adapter 检索确认能查到 alpha 的记忆
        hits = env["beta_db"].search_memories("dark mode", agent_id=ALPHA)
        assert hits, "beta 检索不到 alpha 的记忆"
        assert any("dark mode" in h["content"] for h in hits)

    def test_each_alpha_entry_retrievable(self, env):
        entries = _alpha_entries(3)
        for e in entries:
            env["alpha_store"].put(e)
        for e in entries:
            _deliver(env["alpha_sync"], env["beta_sync"], env["beta_store"], e)
        hits = env["beta_db"].search_memories("dark mode", agent_id=ALPHA)
        assert len(hits) >= 3
        ids = {h["memory_id"] for h in hits}
        assert all(e.memory_id in ids for e in entries)

    def test_sync_logged_on_beta(self, env):
        e = _alpha_entries(1)[0]
        env["alpha_store"].put(e)
        _deliver(env["alpha_sync"], env["beta_sync"], env["beta_store"], e)
        stats = env["beta_sync"].get_stats()
        assert stats["sync_operations"].get("receive", 0) >= 1


class TestConflictResolution:
    """(d) 同一 memory_id 双方并发改不同内容时 ConflictResolution 生效"""

    def test_newest_wins(self, env):
        mem_id = "mem_conflict_e2e"
        older = MemoryEntry(memory_id=mem_id, content="alpha version (older)",
                            persona_id="default", tenant_id="default",
                            source_agent=ALPHA, version=1, timestamp=1000.0,
                            sha256_hash="hash_older")
        newer = MemoryEntry(memory_id=mem_id, content="beta version (newer)",
                            persona_id="default", tenant_id="default",
                            source_agent=BETA, version=2, timestamp=2000.0,
                            sha256_hash="hash_newer")
        env["beta_store"].put(newer)  # beta 本地已有较新版本
        r = _deliver(env["alpha_sync"], env["beta_sync"], env["beta_store"], older)
        assert r.conflicts == 1, f"期望检测到冲突, 实际 {r}"
        resolved = env["beta_store"].get(mem_id)
        assert resolved is not None
        assert resolved.content == "beta version (newer)"  # 较新的胜出
        assert resolved.version == 2

    def test_merge(self, env):
        store = AdapterMemoryStore(env["beta_db"],
                                   resolver=ConflictResolution.resolve_merge)
        mem_id = "mem_merge_e2e"
        local = MemoryEntry(memory_id=mem_id, content="local part",
                            persona_id="default", tenant_id="default",
                            source_agent=BETA, version=1, timestamp=1000.0,
                            sha256_hash="hash_local")
        remote = MemoryEntry(memory_id=mem_id, content="remote part",
                             persona_id="default", tenant_id="default",
                             source_agent=ALPHA, version=2, timestamp=2000.0,
                             sha256_hash="hash_remote")
        store.put(local)
        merged = store.put(remote)
        assert "local part" in merged.content
        assert "remote part" in merged.content
        assert merged.version == 3


class TestSyncIdempotency:
    """(e) 同步幂等（重复同步不产生重复条目）"""

    def test_repeated_delivery_no_duplicates(self, env):
        e = _alpha_entries(1)[0]
        env["alpha_store"].put(e)

        r1 = _deliver(env["alpha_sync"], env["beta_sync"], env["beta_store"], e)
        assert r1.success and r1.entries_count == 1

        rows_before = len(env["beta_db"].get_all_memories(agent_id=ALPHA))
        count_before = env["beta_store"].count(agent_id=ALPHA)
        assert rows_before == count_before == 1

        r2 = _deliver(env["alpha_sync"], env["beta_sync"], env["beta_store"], e)
        assert r2.success
        assert r2.conflicts == 0

        rows_after = len(env["beta_db"].get_all_memories(agent_id=ALPHA))
        count_after = env["beta_store"].count(agent_id=ALPHA)
        assert rows_after == rows_before == 1
        assert count_after == count_before == 1

    def test_repeated_batch_no_duplicates(self, env):
        entries = _alpha_entries(4)
        for e in entries:
            env["alpha_store"].put(e)
        for e in entries:
            _deliver(env["alpha_sync"], env["beta_sync"], env["beta_store"], e)
        # 第二遍重复同步
        for e in entries:
            _deliver(env["alpha_sync"], env["beta_sync"], env["beta_store"], e)
        assert len(env["beta_db"].get_all_memories(agent_id=ALPHA)) == 4
        assert env["beta_store"].count(agent_id=ALPHA) == 4
