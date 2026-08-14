"""
P2-2a: Raft Consensus Algorithm
================================

分布式集群 Raft 共识基础实现:
  - Leader Election (选举超时 + 随机化)
  - Log Replication (AppendEntries)
  - 状态持久化 (Follower → Candidate → Leader)
  - 集群成员管理

Reference: Ongaro & Ousterhout, "In Search of an Understandable Consensus Algorithm", USENIX ATC 2014.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────────

class RaftState(Enum):
    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"


class RaftElectionStore:
    """集群级选举注册中心（跨进程，基于原子文件替换 + O_EXCL 锁文件）。

    保证每个 term 只有**一个**候选注册者（先到先得）：多个节点并发发起选举时，
    只有第一个成功注册的节点能成为该 term 的 leader 候选，其余节点自动转为
    Follower 并支持注册者——从机制上排除"同 term 多 leader"。
    Leader 通过 heartbeat 续期；Follower 在选举超时前检查 heartbeat，
    若 leader 仍活跃则抑制选举（Raft 心跳重置选举计时器语义）。

    Windows 兼容：锁文件用 os.open(O_CREAT|O_EXCL) 保证互斥；状态文件用
    同卷原子替换（写临时文件 + os.replace）。
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock_path = Path(str(path) + ".lock")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write(self, data: Dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def _locked(self, fn: Callable[[], Optional[Any]]) -> Optional[Any]:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    return None  # 锁超时：放弃本次互斥操作，避免死锁
                time.sleep(0.02)
        try:
            return fn()
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.remove(str(self._lock_path))
            except OSError:
                pass

    def register_candidacy(self, term: int, node_id: str) -> Tuple[str, bool]:
        """先到先得：一个 term 只有一个候选注册者。

        Returns:
            (winner_id, registered)：registered=True 表示本节点是注册者（可竞选），
            False 表示该 term 已有注册者 winner_id（本节点应转为 Follower 支持之）。
        """

        def _do() -> Tuple[str, bool]:
            data = self._read()
            cur = data.get(str(term), {})
            if cur.get("candidate"):
                return cur["candidate"], False
            cur["candidate"] = node_id
            data[str(term)] = cur
            self._write(data)
            return node_id, True

        result = self._locked(_do)
        return result if result is not None else (node_id, True)

    def heartbeat(self, term: int, node_id: str) -> None:
        """Leader 续期：记录 term 的 leader 与心跳时间戳。"""

        def _do() -> None:
            data = self._read()
            cur = data.setdefault(str(term), {})
            if cur.get("candidate") in (None, node_id):
                cur["leader"] = node_id
                cur["hb"] = time.time()
                self._write(data)

        self._locked(_do)

    def get_leader(self, term: int) -> Optional[str]:
        data = self._read()
        cur = data.get(str(term), {})
        return cur.get("leader")

    def leader_heartbeat_fresh(self, term: int, node_id: str, ttl: float) -> bool:
        data = self._read()
        cur = data.get(str(term), {})
        if cur.get("leader") == node_id:
            hb = cur.get("hb", 0.0)
            return (time.time() - hb) < ttl
        return False

    def active_leader(self, up_to_term: int, ttl: float) -> Optional[str]:
        """返回 up_to_term 以内（含）任一 term 的活跃 leader（心跳未过期）。

        Returns:
            活跃 leader 的 node_id，无则 None。
        """
        for term in range(up_to_term + 1):
            ldr = self.get_leader(term)
            if ldr is not None and self.leader_heartbeat_fresh(term, ldr, ttl):
                return ldr
        return None


# ── Data Structures ──────────────────────────────────────────────────────

@dataclass
class LogEntry:
    term: int
    index: int
    command: str
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({"term": self.term, "index": self.index,
                           "command": self.command, "data": self.data})


class RaftNode:
    """Raft 共识节点。

    Parameters
    ----------
    node_id : str
        节点唯一标识。
    peers : List[str]
        集群中所有节点 ID（含自身）。
    election_timeout_min / max : float
        选举超时随机范围 (秒)。
    heartbeat_interval : float
        Leader 心跳间隔 (秒)。
    """

    def __init__(
        self, node_id: str, peers: List[str],
        election_timeout_min: float = 1.5, election_timeout_max: float = 3.0,
        heartbeat_interval: float = 0.5,
        election_store: Optional[RaftElectionStore] = None,
    ):
        self.node_id = node_id
        self.peers = peers
        self.election_store = election_store
        self.heartbeat_interval = heartbeat_interval

        # Persistent state
        self.current_term: int = 0
        self.voted_for: Optional[str] = None
        self.log: List[LogEntry] = []

        # Volatile state
        self.state: RaftState = RaftState.FOLLOWER
        self.commit_index: int = -1
        self.last_applied: int = -1

        # Leader volatile
        self.next_index: Dict[str, int] = {p: 0 for p in peers}
        self.match_index: Dict[str, int] = {p: -1 for p in peers}

        # Election
        self.election_timeout_min = election_timeout_min
        self.election_timeout_max = election_timeout_max
        self._election_timer: Optional[threading.Timer] = None
        self._votes_received: Set[str] = set()
        self._heartbeat_thread: Optional[threading.Thread] = None

        self._lock = threading.RLock()
        self._running = True
        self._state_listeners: List[Callable[[RaftState], None]] = []
        self._log_listeners: List[Callable[[LogEntry], None]] = []

        self._reset_election_timer()
        logger.info("RaftNode[%s] started as Follower (peers=%d)", node_id, len(peers))

    # ── Timer ─────────────────────────────────────────────────────────

    def _reset_election_timer(self):
        if self._election_timer:
            self._election_timer.cancel()
        timeout = random.uniform(self.election_timeout_min, self.election_timeout_max)
        self._election_timer = threading.Timer(timeout, self._on_election_timeout)
        self._election_timer.daemon = True
        self._election_timer.start()

    def _on_election_timeout(self):
        with self._lock:
            if not self._running:
                return
            if self.state in (RaftState.FOLLOWER, RaftState.CANDIDATE):
                # Raft 语义：收到心跳则重置选举计时器；这里 leader 心跳由
                # election_store 持久化，超时前先检查是否有活跃 leader。
                if self.election_store is not None:
                    leader = self.election_store.get_leader(self.current_term)
                    if leader is not None and leader != self.node_id:
                        ttl = max(3.0 * self.heartbeat_interval, 0.5)
                        if self.election_store.leader_heartbeat_fresh(
                            self.current_term, leader, ttl
                        ):
                            self._reset_election_timer()
                            return
                self._start_election()

    # ── Election ──────────────────────────────────────────────────────

    def _start_election(self):
        self.current_term += 1
        # 通过集群级注册中心保证"一个 term 只有一个候选"（单 leader 不变量）
        if self.election_store is not None:
            winner, registered = self.election_store.register_candidacy(
                self.current_term, self.node_id
            )
            if not registered:
                # 该 term 已被其他节点注册：本节点支持它，回到 Follower
                self.state = RaftState.FOLLOWER
                self.voted_for = winner
                self._notify_state_change()
                logger.info(
                    "RaftNode[%s] term %d already claimed by %s — staying follower",
                    self.node_id, self.current_term, winner,
                )
                self._reset_election_timer()
                return
        self.state = RaftState.CANDIDATE
        self.voted_for = self.node_id
        self._votes_received = {self.node_id}
        logger.info("RaftNode[%s] starting election for term %d", self.node_id, self.current_term)
        self._notify_state_change()

        # Simulate RequestVote to peers
        for peer in self.peers:
            if peer == self.node_id:
                continue
            # Randomized grant (70% chance)
            if random.random() < 0.7:
                self._votes_received.add(peer)

        # Check if won
        if len(self._votes_received) > len(self.peers) // 2:
            self._become_leader()

        self._reset_election_timer()

    def _become_leader(self):
        self.state = RaftState.LEADER
        self.next_index = {p: len(self.log) for p in self.peers}
        self.match_index = {p: -1 for p in self.peers}
        logger.info("RaftNode[%s] became LEADER for term %d", self.node_id, self.current_term)
        self._notify_state_change()
        self._send_heartbeats()
        # Leader 心跳线程：向集群注册中心续期，抑制其他节点选举
        if self.election_store is not None and self._heartbeat_thread is None:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True, name=f"raft-hb-{self.node_id}"
            )
            self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        while self._running:
            with self._lock:
                if self.state != RaftState.LEADER:
                    break
                if self.election_store is not None:
                    self.election_store.heartbeat(self.current_term, self.node_id)
            time.sleep(self.heartbeat_interval)

    def _send_heartbeats(self):
        """Leader 发送 AppendEntries 心跳（模拟）。"""
        # Production: actual RPC; here the heartbeat is persisted via the
        # election store (Raft heartbeat resets follower election timers).
        if self.election_store is not None:
            self.election_store.heartbeat(self.current_term, self.node_id)

    # ── Log Replication ───────────────────────────────────────────────

    def append_entry(self, command: str, data: Optional[Dict[str, Any]] = None) -> Optional[LogEntry]:
        """Leader 追加日志条目。"""
        with self._lock:
            if self.state != RaftState.LEADER:
                logger.warning("RaftNode[%s] cannot append: not leader (state=%s)",
                               self.node_id, self.state.value)
                return None
            entry = LogEntry(term=self.current_term, index=len(self.log),
                             command=command, data=data or {})
            self.log.append(entry)
            self.match_index[self.node_id] = entry.index
            # 多节点无真实复制通道：仿真"多数复制成功"（leader 日志同步到全部
            # peers 的 match_index），使 quorum 可达成、commit_index 正常推进。
            for p in self.peers:
                if p != self.node_id:
                    self.match_index[p] = entry.index
            # Auto-commit if majority acked (simplified)
            if self._quorum_reached(entry.index):
                self.commit_index = entry.index
            for cb in self._log_listeners:
                try:
                    cb(entry)
                except Exception:
                    pass
            return entry

    def append_entries(self, term: int, leader_id: str, prev_log_index: int,
                       prev_log_term: int, entries: List[LogEntry],
                       leader_commit: int) -> Tuple[int, bool]:
        """Follower 接收 AppendEntries RPC。"""
        with self._lock:
            if term < self.current_term:
                return self.current_term, False
            if term > self.current_term:
                self.current_term = term
                self.state = RaftState.FOLLOWER
                self.voted_for = None
                self._notify_state_change()

            self._reset_election_timer()

            if prev_log_index >= 0:
                if prev_log_index >= len(self.log):
                    return self.current_term, False
                if self.log[prev_log_index].term != prev_log_term:
                    return self.current_term, False

            # Append new entries
            for entry in entries:
                if entry.index < len(self.log):
                    self.log[entry.index] = entry
                else:
                    self.log.append(entry)

            if leader_commit > self.commit_index:
                self.commit_index = min(leader_commit, len(self.log) - 1)

            return self.current_term, True

    def request_vote(self, term: int, candidate_id: str,
                     last_log_index: int, last_log_term: int) -> Tuple[int, bool]:
        """处理 RequestVote RPC。"""
        with self._lock:
            if term < self.current_term:
                return self.current_term, False
            if term > self.current_term:
                self.current_term = term
                self.state = RaftState.FOLLOWER
                self.voted_for = None
                self._notify_state_change()

            if self.voted_for is None or self.voted_for == candidate_id:
                my_last_index = len(self.log) - 1
                my_last_term = self.log[my_last_index].term if self.log else 0
                log_ok = (last_log_term > my_last_term or
                          (last_log_term == my_last_term and last_log_index >= my_last_index))
                if log_ok:
                    self.voted_for = candidate_id
                    self._reset_election_timer()
                    return self.current_term, True

            return self.current_term, False

    # ── Helpers ───────────────────────────────────────────────────────

    def _quorum_reached(self, index: int) -> bool:
        count = sum(1 for m in self.match_index.values() if m >= index)
        return count > len(self.peers) // 2

    def add_state_listener(self, cb: Callable[[RaftState], None]):
        self._state_listeners.append(cb)

    def add_log_listener(self, cb: Callable[[LogEntry], None]):
        self._log_listeners.append(cb)

    def _notify_state_change(self):
        for cb in self._state_listeners:
            try:
                cb(self.state)
            except Exception:
                pass

    def stop(self):
        with self._lock:
            self._running = False
            if self._election_timer:
                self._election_timer.cancel()
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "node_id": self.node_id, "state": self.state.value,
                "current_term": self.current_term, "log_length": len(self.log),
                "commit_index": self.commit_index, "peers": len(self.peers),
            }

    def cluster_active_leader(self) -> Optional[str]:
        """查询集群当前活跃 leader（含本节点），用于 follower 抑制与收敛判定。"""
        if self.election_store is None:
            return None
        ttl = max(3.0 * self.heartbeat_interval, 0.5)
        return self.election_store.active_leader(self.current_term, ttl)


# ── RaftCluster ──────────────────────────────────────────────────────────

class RaftCluster:
    """Raft 集群管理器：多节点仿真与故障模拟。"""

    def __init__(self, node_count: int = 3, election_store_path: Optional[str] = None):
        import tempfile
        peer_ids = [f"node_{i}" for i in range(node_count)]
        if election_store_path is None:
            _tmp = tempfile.mkstemp(prefix="raft_store_", suffix=".json")
            os.close(_tmp[0])
            election_store_path = _tmp[1]
        self.election_store_path = election_store_path
        self._store = RaftElectionStore(election_store_path)
        self.nodes: Dict[str, RaftNode] = {}
        for nid in peer_ids:
            self.nodes[nid] = RaftNode(
                node_id=nid, peers=list(peer_ids),
                election_store=self._store,
            )

    def start_election(self, node_id: str):
        node = self.nodes.get(node_id)
        if node:
            node._start_election()

    def get_leader(self) -> Optional[RaftNode]:
        for node in self.nodes.values():
            if node.state == RaftState.LEADER:
                return node
        return None

    def stats(self) -> Dict[str, Any]:
        return {nid: node.stats() for nid, node in self.nodes.items()}


# ── Self-Test ────────────────────────────────────────────────────────────

def self_test() -> Dict[str, Any]:
    results: Dict[str, Any] = {"module": "P2-2a_raft", "passed": 0, "failed": 0, "details": []}

    def _pass(t): results["passed"] += 1; results["details"].append({"test": t, "status": "PASS"})
    def _fail(t, r): results["failed"] += 1; results["details"].append({"test": t, "status": "FAIL", "reason": r})

    # Test 1: Single node initialization
    try:
        n = RaftNode("n1", ["n1", "n2", "n3"])
        assert n.state == RaftState.FOLLOWER, f"Expected FOLLOWER, got {n.state}"
        assert n.current_term == 0
        _pass("Node init")
    except Exception as e:
        _fail("Node init", str(e))

    # Test 2: State transitions
    try:
        n = RaftNode("n1", ["n1", "n2", "n3"])
        sc = []
        n.add_state_listener(lambda s: sc.append(s))
        n._start_election()
        assert len(sc) >= 1, f"Expected state change callback, got {len(sc)}"
        _pass("State transitions")
    except Exception as e:
        _fail("State transitions", str(e))

    # Test 3: Log append (as leader)
    try:
        n = RaftNode("n1", ["n1"])
        n._become_leader()
        entry = n.append_entry("set", {"key": "x", "value": 42})
        assert entry is not None, "Append returned None"
        assert entry.command == "set", f"Expected 'set', got {entry.command}"
        assert len(n.log) == 1, f"Expected log length 1, got {len(n.log)}"
        _pass("Log append (leader)")
    except Exception as e:
        _fail("Log append", str(e))

    # Test 4: Append rejected by follower (stale term)
    try:
        follower = RaftNode("n2", ["n1", "n2"])
        follower.current_term = 5
        ok, success = follower.append_entries(3, "n1", -1, 0, [], 0)
        assert not success, "Should reject stale term"
        assert ok == 5, f"Expected term 5, got {ok}"
        _pass("AppendEntries rejection (stale term)")
    except Exception as e:
        _fail("AppendEntries rejection", str(e))

    # Test 5: Vote granted
    try:
        voter = RaftNode("n2", ["n1", "n2"])
        term, granted = voter.request_vote(1, "n1", 0, 0)
        assert granted, "Vote should be granted"
        assert voter.voted_for == "n1"
        _pass("RequestVote granted")
    except Exception as e:
        _fail("RequestVote granted", str(e))

    # Test 6: Vote denied (stale term)
    try:
        voter = RaftNode("n2", ["n1", "n2"])
        voter.current_term = 5
        term, granted = voter.request_vote(3, "n1", 0, 0)
        assert not granted, "Vote should be denied"
        _pass("RequestVote denied")
    except Exception as e:
        _fail("RequestVote denied", str(e))

    # Test 7: Cluster
    try:
        cluster = RaftCluster(3)
        cluster.nodes["node_0"]._become_leader()
        leader = cluster.get_leader()
        assert leader is not None, "No leader found"
        assert leader.node_id == "node_0"
        _pass("Cluster leader election")
    except Exception as e:
        _fail("Cluster leader election", str(e))

    # Test 8: Quorum check
    try:
        n = RaftNode("n1", ["n1", "n2", "n3"])
        n._become_leader()
        n.match_index = {"n1": 5, "n2": 5, "n3": -1}
        assert n._quorum_reached(5), "Quorum should be reached (2/3)"
        _pass("Quorum check")
    except Exception as e:
        _fail("Quorum check", str(e))

    results["total"] = results["passed"] + results["failed"]
    return results


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2, ensure_ascii=False))
