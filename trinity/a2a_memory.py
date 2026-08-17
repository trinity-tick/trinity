"""
A2A Memory Sync — Trinity 跨实例记忆共享层
============================================
在 AgentRegistry（服务发现）之上构建记忆同步管道：
  - 发现其他 Trinity 实例（通过 A2A 注册表）
  - 跨实例记忆搜索（搜索远端实例的记忆）
  - 记忆推送/拉取同步
  - 版本冲突检测与解决（基于 CB49 RelationalVersioning）
  - MCP 传输适配

Usage:
    from trinity.a2a_memory import A2AMemorySync

    sync = A2AMemorySync(local_agent_id="trinity-alpha")
    results = sync.search_peers("user preferences")
    sync.share_to_peer("trinity-beta", "user_pref_dark_mode", "dark mode enabled")
"""

from __future__ import annotations

import json
import time
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Callable
from collections import defaultdict

from trinity.a2a_registry import AgentRegistry, AgentInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class MemoryEntry:
    """跨实例记忆条目（最小信息集）"""
    memory_id: str
    content: str
    persona_id: str
    tenant_id: str
    source_agent: str
    version: int = 1
    importance: float = 0.5
    tags: List[str] = field(default_factory=list)
    sha256_hash: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class SyncResult:
    """同步操作结果"""
    action: str           # "push" | "pull" | "search"
    peer: str
    success: bool
    entries_count: int = 0
    conflicts: int = 0
    error: str = ""


# ---------------------------------------------------------------------------
# 冲突解决策略
# ---------------------------------------------------------------------------

class ConflictResolution:
    """记忆版本冲突解决策略"""

    @staticmethod
    def resolve_local_wins(local: MemoryEntry, remote: MemoryEntry) -> MemoryEntry:
        """本地优先：无论版本号，保留本地"""
        return local

    @staticmethod
    def resolve_remote_wins(local: MemoryEntry, remote: MemoryEntry) -> MemoryEntry:
        """远端优先：直接覆盖"""
        return remote

    @staticmethod
    def resolve_newest_wins(local: MemoryEntry, remote: MemoryEntry) -> MemoryEntry:
        """最新优先：比较时间戳"""
        return remote if remote.timestamp > local.timestamp else local

    @staticmethod
    def resolve_highest_version(local: MemoryEntry, remote: MemoryEntry) -> MemoryEntry:
        """最高版本优先"""
        return remote if remote.version > local.version else local

    @staticmethod
    def resolve_merge(local: MemoryEntry, remote: MemoryEntry) -> MemoryEntry:
        """合并策略：保留两者内容（用 ' | ' 连接）"""
        if local.sha256_hash == remote.sha256_hash:
            return local
        merged = MemoryEntry(
            memory_id=local.memory_id or remote.memory_id,
            content=f"{local.content} | {remote.content}",
            persona_id=local.persona_id or remote.persona_id,
            tenant_id=local.tenant_id or remote.tenant_id,
            source_agent=f"{local.source_agent}+{remote.source_agent}",
            version=max(local.version, remote.version) + 1,
            importance=max(local.importance, remote.importance),
            tags=list(set(local.tags + remote.tags)),
        )
        return merged


# ---------------------------------------------------------------------------
# Adapter 支撑的记忆存储（A2A 同步的落盘 + 检索层）
# ---------------------------------------------------------------------------

class AdapterMemoryStore:
    """MemoryEntry 存储，底层挂在 StorageAdapter（如 SQLiteAdapter）上。

    作为 A2A 同步的"本地仓库"：
      - 内存字典维护 memory_id -> MemoryEntry 的权威索引（幂等 + 冲突合并）
      - 每条记忆镜像写入底层 adapter（SQLite memories 表），可用
        adapter.search_memories() 直接检索
      - put() 幂等：同一 memory_id 且 sha256 相同的内容重复写入不会产生
        重复行；内容不同时按 resolver（默认 newest_wins）解决冲突后 upsert

    Usage::

        store = AdapterMemoryStore(adapter, resolver=ConflictResolution.resolve_newest_wins)
        store.put(entry)
        hits = store.search("dark mode", top_k=5)
    """

    def __init__(self, adapter, resolver: Optional[Callable] = None):
        self.adapter = adapter
        self.resolver = resolver or ConflictResolution.resolve_newest_wins
        self._entries: Dict[str, MemoryEntry] = {}

    # ── 读写 ─────────────────────────────────────────────────────────

    def put(self, entry: MemoryEntry) -> MemoryEntry:
        """写入/更新一条记忆（幂等 + 冲突解决），返回最终生效的条目。"""
        existing = self._entries.get(entry.memory_id)
        if existing is not None:
            if existing.sha256_hash == entry.sha256_hash:
                return existing  # 幂等：内容未变，直接返回，不重复落盘
            entry = self.resolver(existing, entry)
        self._entries[entry.memory_id] = entry
        self._upsert_adapter(entry)
        return entry

    def get(self, memory_id: str) -> Optional[MemoryEntry]:
        """按 memory_id 取当前生效条目。"""
        return self._entries.get(memory_id)

    def list(self) -> List[MemoryEntry]:
        """返回全部条目（权威索引视图）。"""
        return list(self._entries.values())

    def count(self, agent_id: Optional[str] = None) -> int:
        """条目计数；传 agent_id 时只统计来自该 agent 的条目。"""
        if agent_id is None:
            return len(self._entries)
        return sum(1 for e in self._entries.values() if e.source_agent == agent_id)

    def search(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """通过底层 adapter 检索（返回 list[dict]，与 adapter 返回一致）。"""
        try:
            rows = self.adapter.search_memories(query, top_k=top_k)
            return list(rows)[:top_k]
        except Exception as e:
            logger.warning("AdapterMemoryStore.search failed: %s", e)
            return []

    # ── 镜像到底层 adapter ──────────────────────────────────────────

    def _upsert_adapter(self, entry: MemoryEntry) -> None:
        """按 memory_id upsert 到 SQLite memories 表，保证检索一致性。

        复用 adapter 自身的连接（与 trinity.memory.memory_agent 等模块
        相同的内部约定）：Windows 下对 WAL 库开第二个写连接会触发
        "database is locked"，因此不走独立 sqlite3 连接。
        FTS5 触发器随 INSERT/UPDATE 自动维护全文索引。
        """
        conn = getattr(self.adapter, "_conn", None)
        if conn is None:
            logger.warning("AdapterMemoryStore: adapter 未连接，跳过镜像 %s", entry.memory_id)
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            session_id = f"sess_{uuid.uuid4().hex[:12]}"
            tags_json = json.dumps(entry.tags, ensure_ascii=False)
            metadata_json = json.dumps({
                "a2a": True,
                "a2a_memory_id": entry.memory_id,
                "source_agent": entry.source_agent,
                "version": entry.version,
                "timestamp": entry.timestamp,
            }, ensure_ascii=False)
            conn.execute("""
                INSERT INTO memories (
                    memory_id, session_id, persona_id, tenant_id, agent_id,
                    content, tokenized_content, role, importance, tags,
                    category, memory_layer, sha256_hash, status, version,
                    ttl_seconds, last_accessed_at, access_count,
                    importance_score, content_hash, conflict_group_id,
                    is_resolved, modality, metadata, source_uri,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'assistant', ?, ?, 'general',
                          'a2a', ?, 'active', 1, NULL, ?, 0, 0.0, NULL, NULL,
                          0, 'text', ?, NULL, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    content = excluded.content,
                    persona_id = excluded.persona_id,
                    tenant_id = excluded.tenant_id,
                    agent_id = excluded.agent_id,
                    importance = excluded.importance,
                    tags = excluded.tags,
                    sha256_hash = excluded.sha256_hash,
                    version = excluded.version,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
            """, (
                entry.memory_id, session_id, entry.persona_id, entry.tenant_id,
                entry.source_agent, entry.content, entry.importance, tags_json,
                entry.sha256_hash, now, metadata_json, now, now,
            ))
            conn.execute("""
                INSERT INTO memory_versions
                    (version_id, memory_id, content, sha256_hash, operation, created_at)
                VALUES (?, ?, ?, ?, 'A2A_SYNC', ?)
            """, (f"ver_{uuid.uuid4().hex[:12]}", entry.memory_id, entry.content,
                  entry.sha256_hash, now))
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning("AdapterMemoryStore._upsert_adapter failed for %s: %s",
                           entry.memory_id, e)


# ---------------------------------------------------------------------------
# MCP 传输适配器
# ---------------------------------------------------------------------------

class MCPTransport:
    """通过 MCP 协议传输记忆数据"""

    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    def send(self, target_agent_id: str, payload: Dict[str, Any]) -> Optional[str]:
        """通过 A2A 注册表的传输包发送数据"""
        return self.registry.prepare_transfer(target_agent_id, payload)

    def receive(self, packet_json: str) -> Optional[Dict[str, Any]]:
        """接收并验证传输包"""
        return AgentRegistry.receive_transfer(packet_json)


# ---------------------------------------------------------------------------
# A2A 记忆同步主类
# ---------------------------------------------------------------------------

class A2AMemorySync:
    """
    跨 Trinity 实例的 A2A 记忆同步引擎。

    功能:
      1. 发现远端实例（通过 AgentRegistry）
      2. 搜索远端实例的记忆
      3. 推送/拉取记忆
      4. 版本冲突检测与解决

    使用示例::

        sync = A2AMemorySync(local_agent_id="trinity-alpha")

        # 在所有在线实例中搜索
        results = sync.search_peers("user preferences")

        # 将本地记忆共享给其他实例
        sync.share_to_all(MemoryEntry(
            memory_id="mem_abc123",
            content="user prefers dark mode",
            persona_id="default",
            tenant_id="default",
            source_agent="trinity-alpha",
        ))

        # 从指定实例拉取
        sync.pull_from_peer("trinity-beta")
    """

    def __init__(
        self,
        local_agent_id: str,
        registry: Optional[AgentRegistry] = None,
        transport: Optional[MCPTransport] = None,
        conflict_resolver: Optional[Callable] = None,
        local_store: Optional[Callable] = None,  # fn(MemoryEntry) -> bool
        local_search: Optional[Callable] = None,  # fn(query) -> list[dict]
    ):
        self.local_agent_id = local_agent_id
        self.registry = registry or AgentRegistry()
        self.transport = transport or MCPTransport(self.registry)
        self.resolve_conflict = conflict_resolver or ConflictResolution.resolve_newest_wins

        # 本地存储回调（由 Trinity 主实例设置）
        self._local_store = local_store
        self._local_search = local_search

        # 同步历史
        self.sync_log: List[SyncResult] = []

        # 已注册自身
        self._register_self()

    def _register_self(self):
        """将本地实例注册到 A2A 注册表中"""
        info = AgentInfo(
            agent_id=self.local_agent_id,
            name=f"Trinity {self.local_agent_id}",
            version="6.36.0",
            capabilities=[
                "memory.search",
                "memory.store",
                "memory.sync",
                "memory.share",
            ],
            endpoint="memory://local",
            status="active",
            last_heartbeat=time.time(),
            metadata={"type": "trinity_memory_instance"},
        )
        self.registry.register(info)

    def heartbeat(self):
        """发送心跳，保持注册状态"""
        self.registry.heartbeat(self.local_agent_id)

    def store_local(self, entry: MemoryEntry) -> bool:
        """将一条记忆写入本地存储（走 local_store 回调）。

        供本地生产方写入：例如 AdapterMemoryStore 的 put 方法会幂等落盘
        并镜像到 adapter，供后续检索。
        """
        if not self._local_store:
            logger.warning("[A2A] %s 未配置 local_store，无法本地写入", self.local_agent_id)
            return False
        return bool(self._local_store(entry))

    # ------------------------------------------------------------------
    # 发现远端实例
    # ------------------------------------------------------------------

    def discover_peers(self, capability: str = "memory.search") -> List[AgentInfo]:
        """发现支持指定能力的在线实例"""
        peers = self.registry.discover(capability=capability)
        # 排除自身
        return [p for p in peers if p.agent_id != self.local_agent_id]

    # ------------------------------------------------------------------
    # 跨实例记忆搜索
    # ------------------------------------------------------------------

    def search_peers(self, query: str, top_k: int = 10) -> Dict[str, List[Dict]]:
        """
        在所有在线对等实例中搜索记忆。

        返回: { peer_agent_id: [result, ...], ... }
        """
        peers = self.discover_peers("memory.search")
        results: Dict[str, List[Dict]] = {}

        for peer in peers:
            try:
                peer_results = self._search_peer(peer, query, top_k)
                if peer_results:
                    results[peer.agent_id] = peer_results
            except Exception as e:
                logger.warning(f"[A2A] 搜索远端 {peer.agent_id} 失败: {e}")

        self.sync_log.append(SyncResult(
            action="search", peer=f"{len(peers)} peers",
            success=True, entries_count=sum(len(v) for v in results.values()),
        ))
        return results

    def _search_peer(self, peer: AgentInfo, query: str, top_k: int) -> List[Dict]:
        """搜索单个远端实例（通过 MCP 传输包模拟）"""
        # 实际场景中会通过 MCP/HTTP 调用远端 API
        # 这里构造请求包
        req = {
            "action": "memory.search",
            "query": query,
            "top_k": top_k,
            "requester": self.local_agent_id,
            "request_id": uuid.uuid4().hex[:12],
        }
        packet = self.transport.send(peer.agent_id, req)
        if not packet:
            return []

        # 模拟远端响应（实际使用时替换为网络调用）
        # 如果本地有搜索回调，尝试搜索本地作为仿真
        if self._local_search:
            local_results = self._local_search(query, top_k)
            if local_results:
                return local_results[:top_k]

        return []

    # ------------------------------------------------------------------
    # 记忆共享（推送）
    # ------------------------------------------------------------------

    def share_to_peer(self, target_agent_id: str, entry: MemoryEntry) -> SyncResult:
        """
        将一条记忆推送到指定远端实例。
        """
        # 构造同步包
        sync_payload = {
            "action": "memory.store",
            "entry": {
                "memory_id": entry.memory_id,
                "content": entry.content,
                "persona_id": entry.persona_id,
                "tenant_id": entry.tenant_id,
                "source_agent": entry.source_agent,
                "version": entry.version,
                "importance": entry.importance,
                "tags": entry.tags,
                "sha256_hash": entry.sha256_hash,
                "timestamp": entry.timestamp,
            },
            "requester": self.local_agent_id,
        }

        packet = self.transport.send(target_agent_id, sync_payload)
        if not packet:
            result = SyncResult(
                action="push", peer=target_agent_id,
                success=False, error="传输包生成失败",
            )
            self.sync_log.append(result)
            return result

        result = SyncResult(
            action="push", peer=target_agent_id,
            success=True, entries_count=1,
        )
        self.sync_log.append(result)
        return result

    def share_to_all(self, entry: MemoryEntry) -> List[SyncResult]:
        """
        将一条记忆广播到所有在线实例。
        """
        peers = self.discover_peers("memory.store")
        results = []
        for peer in peers:
            result = self.share_to_peer(peer.agent_id, entry)
            results.append(result)
        return results

    def share_batch(self, target_agent_id: str, entries: List[MemoryEntry]) -> SyncResult:
        """批量推送记忆到指定实例"""
        success_count = 0
        conflict_count = 0
        for entry in entries:
            result = self.share_to_peer(target_agent_id, entry)
            if result.success:
                success_count += 1
            else:
                conflict_count += 1

        result = SyncResult(
            action="push", peer=target_agent_id,
            success=success_count > 0,
            entries_count=success_count,
            conflicts=conflict_count,
        )
        self.sync_log.append(result)
        return result

    # ------------------------------------------------------------------
    # 记忆接收（对端推送 -> 本地合并）
    # ------------------------------------------------------------------

    def receive_packet(self, packet_json: str,
                       store: Optional[AdapterMemoryStore] = None) -> SyncResult:
        """接收并合并一条 memory.store 传输包（幂等 + 冲突解决）。

        解析 A2A 传输包（AgentRegistry.receive_transfer）取出 entry，
        交给本地 store（AdapterMemoryStore）落盘：
          - 同一 memory_id + 相同 sha256 重复接收不产生重复条目（幂等）
          - 同一 memory_id 内容不同时视为版本冲突，由 store 的 resolver
            （默认 newest_wins）解决后写入

        Args:
            packet_json: prepare_transfer 生成的传输包 JSON 字符串。
            store: 可选 AdapterMemoryStore；为 None 时退回 local_store 回调。

        Returns:
            SyncResult(action="receive", ...)，conflicts>0 表示发生冲突。
        """
        data = AgentRegistry.receive_transfer(packet_json)
        if not data:
            result = SyncResult(action="receive", peer="unknown",
                                success=False, error="传输包解析失败")
            self.sync_log.append(result)
            return result

        payload = data.get("payload") or {}
        peer = data.get("source_agent_id", "unknown")
        if payload.get("action") != "memory.store" or "entry" not in payload:
            result = SyncResult(action="receive", peer=peer, success=False,
                                error=f"未知动作: {payload.get('action')}")
            self.sync_log.append(result)
            return result

        try:
            entry = MemoryEntry(**payload["entry"])
        except (TypeError, ValueError) as e:
            result = SyncResult(action="receive", peer=peer, success=False,
                                error=f"entry 字段无效: {e}")
            self.sync_log.append(result)
            return result

        conflict = 0
        if store is not None:
            existing = store.get(entry.memory_id)
            if existing is not None and existing.sha256_hash != entry.sha256_hash:
                conflict = 1
            store.put(entry)
            stored_ok = True
        elif self._local_store:
            stored_ok = bool(self._local_store(entry))
        else:
            stored_ok = False

        result = SyncResult(
            action="receive", peer=peer,
            success=stored_ok, entries_count=1,
            conflicts=conflict,
            error="" if stored_ok else "本地存储回调未配置/失败",
        )
        self.sync_log.append(result)
        return result

    # ------------------------------------------------------------------
    # 记忆拉取
    # ------------------------------------------------------------------

    def pull_from_peer(self, source_agent_id: str, query: str = "",
                       top_k: int = 50) -> SyncResult:
        """
        从指定远端实例拉取记忆。

        如果提供了 query，拉取匹配的记忆；
        否则拉取该实例最近 top_k 条记忆。
        """
        req = {
            "action": "memory.pull",
            "query": query,
            "top_k": top_k,
            "requester": self.local_agent_id,
        }
        packet = self.transport.send(source_agent_id, req)
        if not packet:
            result = SyncResult(
                action="pull", peer=source_agent_id,
                success=False, error="请求包生成失败",
            )
            self.sync_log.append(result)
            return result

        # 模拟远端返回（实际使用网络）
        if self._local_search and query:
            entries = self._local_search(query, top_k)
        else:
            entries = []

        result = SyncResult(
            action="pull", peer=source_agent_id,
            success=True,
            entries_count=len(entries),
        )
        self.sync_log.append(result)
        return result

    def sync_all(self, query: str = "") -> List[SyncResult]:
        """从所有在线实例同步记忆"""
        peers = self.discover_peers("memory.sync")
        results = []
        for peer in peers:
            result = self.pull_from_peer(peer.agent_id, query)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # 冲突检测
    # ------------------------------------------------------------------

    def detect_conflicts(self, local_entries: List[MemoryEntry],
                         remote_entries: List[MemoryEntry]) -> List[tuple]:
        """
        检测本地与远端记忆之间的版本冲突。

        返回: [(local, remote), ...] 冲突对列表
        """
        local_by_id = {e.memory_id: e for e in local_entries}
        conflicts = []

        for remote in remote_entries:
            local = local_by_id.get(remote.memory_id)
            if local and local.sha256_hash != remote.sha256_hash:
                conflicts.append((local, remote))

        return conflicts

    def resolve_and_merge(self, local: MemoryEntry, remote: MemoryEntry) -> MemoryEntry:
        """解决一条冲突并返回合并后的条目"""
        return self.resolve_conflict(local, remote)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """同步统计"""
        peers = self.discover_peers()
        sync_counts = defaultdict(int)
        for log in self.sync_log:
            sync_counts[log.action] += 1

        return {
            "local_agent": self.local_agent_id,
            "online_peers": len(peers),
            "peers": [a.agent_id for a in peers],
            "sync_operations": dict(sync_counts),
            "total_syncs": len(self.sync_log),
            "last_sync": self.sync_log[-1] if self.sync_log else None,
            "heartbeat": time.time(),
        }


# ======================================================================
# Trinity 集成辅助函数
# ======================================================================

def create_memory_entry(content: str, persona_id: str = "default",
                        tenant_id: str = "default",
                        source_agent: str = "local",
                        importance: float = 0.5,
                        tags: List[str] = None) -> MemoryEntry:
    """快捷创建 MemoryEntry"""
    import hashlib
    return MemoryEntry(
        memory_id=f"mem_{uuid.uuid4().hex[:10]}",
        content=content,
        persona_id=persona_id,
        tenant_id=tenant_id,
        source_agent=source_agent,
        importance=importance,
        tags=tags or [],
        sha256_hash=hashlib.sha256(content.encode()).hexdigest()[:16],
    )


# ======================================================================
# 自检
# ======================================================================

def _selftest():
    """运行自检流程"""
    print("=" * 60)
    print("  A2A Memory Sync 自检")
    print("=" * 60)

    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="a2a_memory_test_")

    # 创建本地存储模拟
    local_store: List[MemoryEntry] = []

    def mock_store(entry: MemoryEntry) -> bool:
        local_store.append(entry)
        return True

    def mock_search(query: str, top_k: int = 10) -> List[Dict]:
        q = query.lower()
        results = []
        for e in local_store:
            if q in e.content.lower():
                import dataclasses
                results.append(dataclasses.asdict(e))
        return results[:top_k]

    # 创建同步引擎
    registry = AgentRegistry(db_path=f"{tmp_dir}/registry.json")
    sync = A2AMemorySync(
        local_agent_id="trinity-alpha",
        registry=registry,
        local_store=mock_store,
        local_search=mock_search,
    )

    # 1. 注册与心跳
    print("\n[步骤 1/5] 注册与心跳...")
    info = registry.discover()
    assert any(a.agent_id == "trinity-alpha" for a in info)
    sync.heartbeat()
    print("  [OK] 注册成功，心跳已发送")

    # 2. 发现对等实例
    print("\n[步骤 2/5] 发现对等实例...")
    peers = sync.discover_peers()
    # 只有自身，所以 peers 应为空
    assert len(peers) == 0, f"期望 0 个对等实例，发现 {len(peers)}"
    print("  [OK] 正确发现 0 个对等实例（仅自身在线）")

    # 注册第二个实例（模拟）
    registry.register(AgentInfo(
        agent_id="trinity-beta",
        name="Beta Instance",
        version="6.36.0",
        capabilities=["memory.search", "memory.store", "memory.sync"],
        endpoint="memory://beta",
        status="active",
        last_heartbeat=time.time(),
    ))
    peers = sync.discover_peers()
    assert len(peers) == 1, f"期望 1 个对等实例，发现 {len(peers)}"
    assert peers[0].agent_id == "trinity-beta"
    print("  [OK] 正确发现 trinity-beta 实例")

    # 3. 创建并共享记忆
    print("\n[步骤 3/5] 创建并共享记忆...")
    entry = create_memory_entry(
        content="user prefers dark mode for all interfaces",
        persona_id="user1",
        source_agent="trinity-alpha",
        importance=0.8,
        tags=["preference", "ui"],
    )
    assert entry.memory_id.startswith("mem_")
    assert entry.sha256_hash
    print(f"  [OK] 创建 MemoryEntry: {entry.memory_id}")
    print(f"  [OK] 内容: {entry.content[:40]}...")
    print(f"  [OK] 重要性: {entry.importance}, 标签: {entry.tags}")

    # 4. 推送共享
    print("\n[步骤 4/5] 推送共享到对等实例...")
    result = sync.share_to_peer("trinity-beta", entry)
    assert result.success, f"共享失败: {result.error}"
    print(f"  [OK] 推送到 trinity-beta: entries={result.entries_count}")

    # 批量共享
    entries = [
        create_memory_entry(f"memory batch {i}", persona_id="batch_test")
        for i in range(3)
    ]
    batch_result = sync.share_batch("trinity-beta", entries)
    assert batch_result.success
    print(f"  [OK] 批量推送 {batch_result.entries_count} 条记忆")

    # 广播到所有
    broadcast_results = sync.share_to_all(entry)
    print(f"  [OK] 广播到 {len(broadcast_results)} 个实例")

    # 5. 冲突检测
    print("\n[步骤 5/5] 冲突检测与解决...")
    local_ver = MemoryEntry(
        memory_id="mem_conflict_001",
        content="local version",
        persona_id="test",
        tenant_id="default",
        source_agent="trinity-alpha",
        version=1,
        timestamp=1000,
        sha256_hash="abc123",
    )
    remote_ver = MemoryEntry(
        memory_id="mem_conflict_001",
        content="remote version",
        persona_id="test",
        tenant_id="default",
        source_agent="trinity-beta",
        version=2,
        timestamp=2000,
        sha256_hash="def456",
    )
    conflicts = sync.detect_conflicts([local_ver], [remote_ver])
    assert len(conflicts) == 1, f"期望 1 条冲突，检测到 {len(conflicts)}"
    print(f"  [OK] 检测到 {len(conflicts)} 条冲突")

    # 测试各种解决策略
    for name, resolver in [
        ("local_wins", ConflictResolution.resolve_local_wins),
        ("remote_wins", ConflictResolution.resolve_remote_wins),
        ("newest_wins", ConflictResolution.resolve_newest_wins),
        ("highest_version", ConflictResolution.resolve_highest_version),
        ("merge", ConflictResolution.resolve_merge),
    ]:
        sync.resolve_conflict = resolver
        merged = sync.resolve_and_merge(local_ver, remote_ver)
        print(f"  [OK] {name}: content='{merged.content[:30]}...' ver={merged.version}")

    # 相同内容不冲突
    same_local = MemoryEntry(
        memory_id="mem_same", content="same content",
        persona_id="t", tenant_id="d", source_agent="alpha",
        sha256_hash="abc123",
    )
    same_remote = MemoryEntry(
        memory_id="mem_same", content="same content",
        persona_id="t", tenant_id="d", source_agent="beta",
        sha256_hash="abc123",
    )
    no_conflict = sync.detect_conflicts([same_local], [same_remote])
    assert len(no_conflict) == 0, f"相同内容不应冲突: {len(no_conflict)}"
    print("  [OK] 相同内容正确不冲突")

    # 统计
    stats = sync.get_stats()
    print(f"\n  同步统计: {stats['total_syncs']} 次操作")
    print(f"  [OK] 在线实例: {stats['online_peers']}")

    # 清理
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)

    print("\n" + "=" * 60)
    print("  自检结果: OK 全部通过")
    print("=" * 60)


if __name__ == "__main__":
    _selftest()
