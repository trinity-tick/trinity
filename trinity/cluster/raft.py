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
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────────

class RaftState(Enum):
    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"


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
    ):
        self.node_id = node_id
        self.peers = peers

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
                self._start_election()

    # ── Election ──────────────────────────────────────────────────────

    def _start_election(self):
        self.current_term += 1
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

    def _send_heartbeats(self):
        """Leader 发送 AppendEntries 心跳（模拟）。"""
        # Production: actual RPC; here just log
        pass

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


# ── RaftCluster ──────────────────────────────────────────────────────────

class RaftCluster:
    """Raft 集群管理器：多节点仿真与故障模拟。"""

    def __init__(self, node_count: int = 3):
        peer_ids = [f"node_{i}" for i in range(node_count)]
        self.nodes: Dict[str, RaftNode] = {}
        for nid in peer_ids:
            self.nodes[nid] = RaftNode(node_id=nid, peers=list(peer_ids))

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
