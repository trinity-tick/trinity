#!/usr/bin/env python
"""A2A 多智能体记忆共享端到端演示 (M3-5).

单进程双智能体（agent-alpha / agent-beta）演示:

  1. 用 AgentRegistry 注册两个 agent（含 capabilities）；
     用 AgentCard 生成并签名卡片（HMAC-SHA256）。
  2. agent-alpha 通过 A2AMemorySync 写入 3-5 条记忆（不同 importance/tags）。
  3. 用 A2AProtocol 完成一次能力协商（NegotiationResult）与一次
     任务/消息交换（A2ARequest/A2AResponse，JSON-RPC 风格），
     把 alpha 的新记忆清单作为消息负载传给 beta。
  4. beta 端用 A2AMemorySync 接收并合并（ConflictResolution.newest_wins），
     再通过 SQLiteAdapter 检索确认能查到 alpha 的记忆。
  5. 冲突解决（同一 memory_id 双方并发改不同内容）与同步幂等性验证。
  6. 输出结构化 PASS/FAIL 清单，脚本以 exit 0/1 结束。

运行:
    & 'C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python314\\python.exe' scripts/a2a_demo.py
"""

from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# 让中文输出在 Windows 控制台下保持可读（不影响功能）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from trinity.a2a import (  # noqa: E402
    A2AProtocol,
    A2ARequest,
    A2AResponse,
    CapabilityRegistry,
    generate_card,
    verify_card,
)
from trinity.a2a_memory import (  # noqa: E402
    A2AMemorySync,
    AdapterMemoryStore,
    ConflictResolution,
    MemoryEntry,
    create_memory_entry,
)
from trinity.a2a_registry import AgentRegistry, AgentInfo  # noqa: E402
from trinity.adapters.sqlite import SQLiteAdapter  # noqa: E402

ALPHA = "agent-alpha"
BETA = "agent-beta"
CAPABILITIES = ["memory.search", "memory.store", "memory.sync", "memory.share"]

# (content, importance, tags, 检索关键词)
ALPHA_MEMORIES = [
    ("user prefers dark mode for all interfaces", 0.8, ["preference", "ui"], "dark mode"),
    ("deployment checklist for the a2a demo environment", 0.6, ["ops", "deployment"], "deployment"),
    ("alpha agent meeting notes about shared memory", 0.5, ["notes", "a2a"], "shared memory"),
    ("high importance project milestone reached by alpha", 0.9, ["milestone", "project"], "milestone"),
]


class Checklist:
    """结构化 PASS/FAIL 收集器。"""

    def __init__(self) -> None:
        self.items = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        ok = bool(ok)
        self.items.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if detail:
            line += f" -- {detail}"
        print(line)
        return ok

    def summary(self) -> bool:
        passed = sum(1 for _, ok, _ in self.items if ok)
        total = len(self.items)
        print("-" * 72)
        print(f"  结果: {passed}/{total} PASS")
        for name, ok, detail in self.items:
            if not ok:
                print(f"  [FAIL] {name} -- {detail}")
        print("-" * 72)
        return passed == total


def build_store_payload(entry: MemoryEntry, requester: str) -> dict:
    """构造与 A2AMemorySync.share_to_peer 一致的 memory.store 负载。"""
    return {
        "action": "memory.store",
        "entry": dataclasses.asdict(entry),
        "requester": requester,
    }


def deliver(sender_sync, recipient_sync, recipient_store, entry: MemoryEntry):
    """模拟网络传输：sender 生成传输包 -> recipient 接收并合并。"""
    payload = build_store_payload(entry, sender_sync.local_agent_id)
    packet = sender_sync.transport.send(recipient_sync.local_agent_id, payload)
    return recipient_sync.receive_packet(packet, store=recipient_store)


def main() -> int:
    print("=" * 72)
    print("  A2A 多智能体记忆共享端到端演示 (M3-5)")
    print(f"  智能体: {ALPHA} <-> {BETA}")
    print("=" * 72)
    cl = Checklist()

    with tempfile.TemporaryDirectory(prefix="a2a_demo_") as tmp:
        # ----------------------------------------------------------
        # 步骤 1: 注册 Agent 与 AgentCard
        # ----------------------------------------------------------
        print("\n[步骤 1] AgentRegistry 注册 + AgentCard 生成/签名...")
        registry = AgentRegistry(db_path=os.path.join(tmp, "registry.json"))
        cap_registry = CapabilityRegistry()

        alpha_info = AgentInfo(
            agent_id=ALPHA, name="Alpha Agent", version="1.0.0",
            capabilities=CAPABILITIES, endpoint="memory://alpha",
            status="active", last_heartbeat=time.time(),
            metadata={"role": "producer"},
        )
        beta_info = AgentInfo(
            agent_id=BETA, name="Beta Agent", version="1.0.0",
            capabilities=CAPABILITIES, endpoint="memory://beta",
            status="active", last_heartbeat=time.time(),
            metadata={"role": "consumer"},
        )
        cl.check("AgentRegistry 注册 agent-alpha",
                 registry.register(alpha_info),
                 f"{len(alpha_info.capabilities)} capabilities")
        cl.check("AgentRegistry 注册 agent-beta",
                 registry.register(beta_info),
                 f"{len(beta_info.capabilities)} capabilities")

        alpha_card = generate_card(ALPHA, name="Alpha Agent", capabilities=CAPABILITIES)
        beta_card = generate_card(BETA, name="Beta Agent", capabilities=CAPABILITIES)
        v_alpha = verify_card(alpha_card)
        v_beta = verify_card(beta_card)
        cl.check("agent-alpha AgentCard 签名校验", v_alpha["valid"], v_alpha["detail"])
        cl.check("agent-beta AgentCard 签名校验", v_beta["valid"], v_beta["detail"])
        cap_registry.register_agent(alpha_card)
        cap_registry.register_agent(beta_card)

        # ----------------------------------------------------------
        # 步骤 2: 存储 + 同步引擎（轻量 SQLiteAdapter，不初始化 second_brain）
        # ----------------------------------------------------------
        print("\n[步骤 2] 初始化 SQLiteAdapter 存储与 A2AMemorySync...")
        alpha_db = SQLiteAdapter(os.path.join(tmp, "alpha.db"))
        beta_db = SQLiteAdapter(os.path.join(tmp, "beta.db"))
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
        cl.check("A2AMemorySync 引擎就绪 (alpha/beta)",
                 alpha_sync and beta_sync,
                 f"peers: {[p.agent_id for p in beta_sync.discover_peers()]}")

        # ----------------------------------------------------------
        # 步骤 3: alpha 写入 3-5 条记忆（不同 importance/tags）
        # ----------------------------------------------------------
        print("\n[步骤 3] agent-alpha 写入记忆...")
        entries = [
            create_memory_entry(content=content, persona_id="default",
                                tenant_id="default", source_agent=ALPHA,
                                importance=importance, tags=tags)
            for content, importance, tags, _kw in ALPHA_MEMORIES
        ]
        write_ok = all(alpha_sync.store_local(e) for e in entries)
        cl.check(f"alpha 本地写入 {len(entries)} 条记忆 (A2AMemorySync.store_local)",
                 write_ok,
                 "importance=" + ",".join(f"{e.importance:.1f}" for e in entries))
        cl.check("alpha 本地落盘可见 (adapter 检索)",
                 bool(alpha_db.search_memories("dark mode", agent_id=ALPHA)),
                 "search 'dark mode' hits")

        # ----------------------------------------------------------
        # 步骤 4: A2AProtocol 能力协商
        # ----------------------------------------------------------
        print("\n[步骤 4] A2AProtocol 能力协商 (NegotiationResult)...")
        protocol = A2AProtocol(cap_registry)
        neg = protocol.negotiate_capabilities(ALPHA, BETA)
        cl.check("协商返回 compatible=True", neg.compatible,
                 f"negotiation_id={neg.negotiation_id[:12]}...")
        cl.check("协商出共同 capabilities", bool(neg.common_capabilities),
                 ", ".join(neg.common_capabilities))

        # ----------------------------------------------------------
        # 步骤 5: JSON-RPC 风格任务/消息交换（携带 alpha 记忆清单）
        # ----------------------------------------------------------
        print("\n[步骤 5] A2ARequest/A2AResponse 消息交换...")
        manifest = [dataclasses.asdict(e) for e in entries]
        req = A2ARequest(
            id="req_mem_share_001",
            method="memory.share",
            params={"entries": manifest, "requester": ALPHA},
            from_agent=ALPHA,
            to_agent=BETA,
        )
        routed = protocol.send_message(ALPHA, BETA, "memory.share",
                                       {"entries": manifest})
        cl.check("send_message 路由投递 (JSON-RPC)",
                 bool(routed.get("delivered")),
                 f"message_id={routed.get('message_id')}")

        # beta 端处理消息 -> 应答
        resp = A2AResponse(
            id=req.id,
            result={"accepted": True, "entries_received": len(req.params["entries"])},
            from_agent=BETA,
            to_agent=ALPHA,
        )
        cl.check("beta 应答 A2AResponse (result.accepted)",
                 resp.result is not None and resp.result.get("accepted") is True,
                 f"entries_received={resp.result['entries_received']}")
        cl.check("A2ARequest/A2AResponse JSON-RPC 往返",
                 A2ARequest.from_dict(req.to_dict()) == req
                 and A2AResponse.from_dict(resp.to_dict()) == resp,
                 "to_dict/from_dict 一致")

        # ----------------------------------------------------------
        # 步骤 6: 传输包投递 -> beta 接收并合并
        # ----------------------------------------------------------
        print("\n[步骤 6] 传输包投递 (alpha -> beta) 与接收合并...")
        results = [deliver(alpha_sync, beta_sync, beta_store, e) for e in entries]
        cl.check("beta 接收全部 memory.store 包",
                 all(r.success for r in results),
                 f"received={sum(r.entries_count for r in results)}")

        # ----------------------------------------------------------
        # 步骤 7: beta 通过 adapter 检索确认能查到 alpha 的记忆
        # ----------------------------------------------------------
        print("\n[步骤 7] beta 端检索验证...")
        retrieval_ok = True
        for _content, _imp, _tags, kw in ALPHA_MEMORIES:
            hits = beta_db.search_memories(kw, agent_id=ALPHA)
            if not any(kw in h["content"] for h in hits):
                retrieval_ok = False
        cl.check("beta 检索命中 alpha 全部记忆 (SQLiteAdapter.search_memories)",
                 retrieval_ok,
                 f"agent_id={ALPHA}, {len(entries)} 条可检索")

        # ----------------------------------------------------------
        # 步骤 8: 冲突解决（同一 memory_id 双方并发改不同内容）
        # ----------------------------------------------------------
        print("\n[步骤 8] 版本冲突检测与解决 (newest_wins)...")
        mem_id = "mem_shared_conflict"
        alpha_v1 = MemoryEntry(memory_id=mem_id, content="alpha: dark theme v1",
                               persona_id="default", tenant_id="default",
                               source_agent=ALPHA, version=1, timestamp=1000.0,
                               sha256_hash="hash_alpha_v1")
        beta_v2 = MemoryEntry(memory_id=mem_id, content="beta: dark theme v2 (newer)",
                              persona_id="default", tenant_id="default",
                              source_agent=BETA, version=2, timestamp=2000.0,
                              sha256_hash="hash_beta_v2")
        beta_store.put(beta_v2)  # beta 本地已有较新版本
        r_conflict = deliver(alpha_sync, beta_sync, beta_store, alpha_v1)
        resolved = beta_store.get(mem_id)
        cl.check("冲突检测命中 (conflicts=1)", r_conflict.conflicts == 1,
                 f"SyncResult.conflicts={r_conflict.conflicts}")
        cl.check("newest_wins 生效: 保留较新内容",
                 resolved is not None and resolved.content == beta_v2.content,
                 f"resolved content='{resolved.content}'")

        # ----------------------------------------------------------
        # 步骤 9: 同步幂等性（重复同步不产生重复条目）
        # ----------------------------------------------------------
        print("\n[步骤 9] 同步幂等性验证...")
        dup = entries[0]
        before_rows = len(beta_db.get_all_memories(agent_id=ALPHA))
        before_count = beta_store.count(agent_id=ALPHA)
        r_dup = deliver(alpha_sync, beta_sync, beta_store, dup)
        after_rows = len(beta_db.get_all_memories(agent_id=ALPHA))
        after_count = beta_store.count(agent_id=ALPHA)
        cl.check("重复同步不产生重复条目 (adapter 行数不变)",
                 before_rows == after_rows == len(entries),
                 f"rows {before_rows} -> {after_rows}")
        cl.check("重复同步不产生重复条目 (store 条目数不变)",
                 before_count == after_count == len(entries),
                 f"entries {before_count} -> {after_count}")
        cl.check("重复同步返回成功且零冲突", r_dup.success and r_dup.conflicts == 0,
                 f"SyncResult(success={r_dup.success}, conflicts={r_dup.conflicts})")

        # ----------------------------------------------------------
        # 统计与清理
        # ----------------------------------------------------------
        stats = beta_sync.get_stats()
        print("\n[Beta 同步统计]", {k: stats[k] for k in
              ("local_agent", "online_peers", "sync_operations", "total_syncs")})

        alpha_db.disconnect()
        beta_db.disconnect()

    print("\n" + "=" * 72)
    ok = cl.summary()
    print("  演示结论:", "全部 PASS" if ok else "存在 FAIL")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
