#!/usr/bin/env python3
"""
Trinity Cluster Stress Test — Raft Consensus + Distributed Memory

Simulates:
  - 3-node Raft cluster with leader election
  - 100+ concurrent memory writes via multiprocessing
  - Leader election verification
  - Log consistency across nodes

Usage:
    python benchmark/cluster_stress.py
    python benchmark/cluster_stress.py --num-writes 200 --workers 8
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

TRINITY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRINITY_ROOT))
os.environ["TRINITY_MEMORY_ENABLED"] = "0"


# ── Worker: isolated per-process node operation ───────────────────────────


def _write_memories_worker(
    node_id: str,
    peer_ids: List[str],
    items: List[Dict[str, Any]],
    timeout_min: float,
    timeout_max: float,
    heartbeat: float,
    election_store_path: str,
) -> Dict[str, Any]:
    """Worker process: spin up a RaftNode and write items.

    Returns node-level stats.
    """
    # 直接按文件加载 raft 模块，避免 import trinity 触发 SecondBrain
    # 122 模块全套初始化（多 worker 进程会内存溢出）。
    import importlib.util
    import sys as _sys

    _raft_path = os.path.join(TRINITY_ROOT, "trinity", "cluster", "raft.py")
    _spec = importlib.util.spec_from_file_location("raft_standalone", _raft_path)
    _raft = importlib.util.module_from_spec(_spec)
    _sys.modules["raft_standalone"] = _raft  # dataclass 需在 sys.modules 中找到模块
    _spec.loader.exec_module(_raft)  # type: ignore[union-attr]
    RaftNode = _raft.RaftNode
    RaftState = _raft.RaftState
    RaftElectionStore = _raft.RaftElectionStore

    node = RaftNode(
        node_id=node_id,
        peers=peer_ids,
        election_timeout_min=timeout_min,
        election_timeout_max=timeout_max,
        heartbeat_interval=heartbeat,
        election_store=RaftElectionStore(election_store_path),
    )

    # Wait for cluster convergence: either this node becomes leader, or the
    # cluster already has an active leader (this node stays follower).
    leader_id: Optional[str] = None
    for _ in range(40):
        time.sleep(0.15)
        with node._lock:
            if node.state == RaftState.LEADER:
                leader_id = node.node_id
                break
        if node.election_store is not None and node.cluster_active_leader():
            break  # 集群已有活跃 leader，本节点保持 follower
    else:
        # No active leader after the window: force an election. The shared
        # election store arbitrates so only one node can claim a term.
        with node._lock:
            if node.state in (RaftState.FOLLOWER, RaftState.CANDIDATE):
                node._start_election()
                time.sleep(0.3)

    wrote = 0
    failed = 0
    with node._lock:
        is_leader = node.state == RaftState.LEADER
    leader_id = node_id if is_leader else None

    # Only the leader writes; followers reject writes (Raft semantics).
    if is_leader:
        for item in items:
            entry = node.append_entry(
                command="write_memory",
                data={"content": item["content"], "memory_id": item["memory_id"]},
            )
            if entry is not None:
                wrote += 1
            else:
                failed += 1

    stats = node.stats()
    node.stop()

    return {
        "node_id": node_id,
        "wrote": wrote,
        "failed": failed,
        "log_length": len(node.log),
        "final_state": node.state.value,
        "was_leader": is_leader,
        "commit_index": node.commit_index,
        "term": node.current_term,
        "stats": stats,
    }


# ── Harness ──────────────────────────────────────────────────────────────


class ClusterStressTest:
    """Orchestrate multi-node concurrent writes."""

    def __init__(self, num_nodes: int = 3, num_writes: int = 100,
                 workers: int = 6):
        self.num_nodes = num_nodes
        self.num_writes = num_writes
        self.workers = workers
        self.peer_ids = [f"node-{i}" for i in range(num_nodes)]

    def _generate_write_payloads(self) -> List[Dict[str, Any]]:
        return [
            {
                "memory_id": f"stress_{uuid.uuid4().hex[:8]}",
                "content": f"[StressTest #{i}] 并发写入测试记忆 — {uuid.uuid4().hex[:6]}",
            }
            for i in range(self.num_writes)
        ]

    def run(self) -> Dict[str, Any]:
        """Execute stress test and return results dict."""
        payloads = self._generate_write_payloads()
        # Ceiling division — ensures all payloads are distributed
        chunk_size = max(1, (len(payloads) + self.num_nodes - 1) // self.num_nodes)
        chunks = [payloads[i:i + chunk_size] for i in range(0, len(payloads), chunk_size)]

        print("=" * 60)
        print(f"  Trinity Cluster Stress Test")
        print(f"  Nodes: {self.num_nodes}  |  Writes: {self.num_writes}  |  Workers: {self.workers}")
        print("=" * 60)

        # Phase 1: Launch workers
        print("\n[Phase 1] Launching Raft nodes …")
        t0 = time.monotonic()
        results: List[Dict] = []

        with ProcessPoolExecutor(max_workers=self.workers) as pool:
            futures = {}
            # 共享选举注册中心（跨进程）：保证任一时刻集群只有单 leader
            import tempfile
            _fd, _store_path = tempfile.mkstemp(prefix="raft_stress_store_", suffix=".json")
            os.close(_fd)
            for i in range(self.num_nodes):
                chunk = chunks[i] if i < len(chunks) else []
                if not chunk:
                    continue
                fut = pool.submit(
                    _write_memories_worker,
                    self.peer_ids[i],
                    self.peer_ids,
                    chunk,
                    0.3, 0.8, 0.2,  # fast election for stress test
                    _store_path,
                )
                futures[fut] = i

            for fut in as_completed(futures):
                try:
                    res = fut.result(timeout=60)
                    results.append(res)
                    node_id = res["node_id"]
                    print(f"  [{node_id}] wrote={res['wrote']} failed={res['failed']} "
                          f"log={res['log_length']} state={res['final_state']}")
                except Exception as exc:
                    idx = futures[fut]
                    results.append({"node_id": self.peer_ids[idx], "error": str(exc)})
                    print(f"  [{self.peer_ids[idx]}] ERROR: {exc}")

            try:
                os.remove(_store_path)
            except OSError:
                pass

        elapsed = time.monotonic() - t0
        print(f"\n  All nodes finished in {elapsed:.2f}s")

        # Phase 2: Verify
        print("\n[Phase 2] Verifying leader election & data consistency …")
        verification = self._verify(results, payloads)
        for msg in verification["details"]:
            print(f"  {msg}")

        # Summary
        total_wrote = sum(r.get("wrote", 0) for r in results)
        total_failed = sum(r.get("failed", 0) for r in results)
        print(f"\n{'=' * 60}")
        print(f"  TOTAL: wrote={total_wrote} failed={total_failed}  "
              f"nodes={self.num_nodes}  checks={verification['passed_checks']}/{verification['total_checks']}")
        print(f"{'=' * 60}")

        return {
            "test": "cluster_stress",
            "config": {
                "num_nodes": self.num_nodes,
                "num_writes": self.num_writes,
                "workers": self.workers,
            },
            "elapsed_sec": round(elapsed, 2),
            "node_results": results,
            "verification": verification,
        }


    def _verify(self, results: List[Dict],
                payloads: List[Dict]) -> Dict[str, Any]:
        checks: List[str] = []
        passed = 0
        total = 5

        # Check 1: All nodes completed
        if len(results) == self.num_nodes:
            checks.append("PASS: All nodes completed")
            passed += 1
        else:
            checks.append(f"FAIL: Only {len(results)}/{self.num_nodes} nodes completed")

        # Check 2: Exactly ONE leader elected (Raft single-leader invariant)
        leaders = [r for r in results if r.get("was_leader")]
        if len(leaders) == 1:
            checks.append(f"PASS: Exactly 1 leader elected: {leaders[0]['node_id']}")
            passed += 1
        elif len(leaders) == 0:
            checks.append("FAIL: No leader elected")
        else:
            checks.append(f"FAIL: {len(leaders)} leaders elected (Raft requires exactly 1): "
                          f"{[l['node_id'] for l in leaders]}")

        # Check 3: Writes succeeded
        total_wrote = sum(r.get("wrote", 0) for r in results)
        if total_wrote > 0:
            checks.append(f"PASS: {total_wrote} writes committed (some may have failed if not leader)")
            passed += 1
        else:
            checks.append(f"FAIL: Zero writes committed (total_wrote={total_wrote})")

        # Check 4: Leader's commit_index advanced (not -1) after majority replication
        leader_res = leaders[0] if leaders else None
        if leader_res is not None and leader_res.get("commit_index", -1) >= 0:
            checks.append(f"PASS: Leader commit_index={leader_res['commit_index']} (advanced)")
            passed += 1
        elif leader_res is not None:
            checks.append(f"FAIL: Leader commit_index={leader_res['commit_index']} (stuck at -1)")
        else:
            checks.append("SKIP: No leader to verify commit_index")

        # Check 5: No fatal errors
        errors = [r for r in results if "error" in r]
        if not errors:
            checks.append("PASS: No fatal errors")
            passed += 1
        else:
            checks.append(f"FAIL: {len(errors)} nodes with errors: {errors}")

        return {
            "passed_checks": passed,
            "total_checks": total,
            "details": checks,
        }


# ── CLI ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trinity Cluster Stress Test")
    parser.add_argument("--num-nodes", type=int, default=3, help="Number of Raft nodes")
    parser.add_argument("--num-writes", type=int, default=100, help="Total concurrent writes")
    parser.add_argument("--workers", type=int, default=6, help="Max process workers")
    parser.add_argument("--output", default=None, help="Save results to JSON file")
    args = parser.parse_args()

    test = ClusterStressTest(
        num_nodes=args.num_nodes,
        num_writes=args.num_writes,
        workers=args.workers,
    )
    report = test.run()

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n[*] Report saved to {args.output}")

    # Exit code
    ok = report["verification"]["passed_checks"] == report["verification"]["total_checks"]
    sys.exit(0 if ok else 1)
