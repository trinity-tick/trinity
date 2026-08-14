"""
P12-4: Git-like Memory Version Control.

Reference: Memoria (GTC 2026) — Copy-on-Write snapshots + branch/merge/rollback.

Design: Enables isolated memory experiments via COW snapshots, branch creation,
        diff-based merge, one-click rollback to tagged stable checkpoints,
        and provenance-aware versioning compatible with audit_trail.py.

Interface-compatible with: audit_trail.py (provenance chain)
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DiffAction(Enum):
    ADDED = auto()
    MODIFIED = auto()
    DELETED = auto()
    UNCHANGED = auto()


class MergeStatus(Enum):
    CLEAN = auto()
    CONFLICT = auto()
    RESOLVED = auto()
    FAILED = auto()


class RollbackStatus(Enum):
    SUCCESS = auto()
    TAG_NOT_FOUND = auto()
    CONFLICT = auto()
    FAILED = auto()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MemorySnapshot:
    """A point-in-time snapshot of the memory store."""
    snapshot_id: str
    parent_id: Optional[str]
    timestamp: float
    data: Dict[str, Any] = field(default_factory=dict)
    tag: Optional[str] = None
    provenance: Dict[str, str] = field(default_factory=dict)


@dataclass
class MemoryDiff:
    """Incremental diff between two memory snapshots."""
    source_id: str
    target_id: str
    additions: Dict[str, Any] = field(default_factory=dict)
    modifications: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)  # old -> new
    deletions: Set[str] = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        return not self.additions and not self.modifications and not self.deletions

    @property
    def change_count(self) -> int:
        return len(self.additions) + len(self.modifications) + len(self.deletions)


@dataclass
class BranchInfo:
    """Metadata about a memory branch."""
    branch_name: str
    base_snapshot_id: str
    head_snapshot_id: str
    created_at: float
    description: str = ""


@dataclass
class MergeResult:
    """Result of a branch merge operation."""
    status: MergeStatus
    merged_snapshot_id: str
    conflicts: List[str] = field(default_factory=list)
    resolved_automatically: int = 0
    required_manual_resolution: int = 0


@dataclass
class TagEntry:
    """A named tag pointing to a stable snapshot."""
    tag_name: str
    snapshot_id: str
    created_at: float
    description: str = ""


# ---------------------------------------------------------------------------
# CopyOnWriteSnapshot
# ---------------------------------------------------------------------------

class CopyOnWriteSnapshot:
    """
    Copy-on-Write snapshot engine. Writes are isolated from the
    main branch until explicitly committed, enabling safe memory
    experiments without risk of contamination.
    """

    def __init__(self) -> None:
        self._snapshots: Dict[str, MemorySnapshot] = {}
        self._current_id: Optional[str] = None
        self._lock = threading.RLock()

    def create(
        self,
        data: Dict[str, Any],
        tag: Optional[str] = None,
        prov_context: Optional[Dict[str, str]] = None,
    ) -> MemorySnapshot:
        """Create a new COW snapshot from the given data."""
        with self._lock:
            sid = hashlib.sha256(
                f"{time.time()}_{len(self._snapshots)}".encode()
            ).hexdigest()[:12]
            snapshot = MemorySnapshot(
                snapshot_id=sid,
                parent_id=self._current_id,
                timestamp=time.time(),
                data=copy.deepcopy(data),
                tag=tag,
                provenance=prov_context or {},
            )
            self._snapshots[sid] = snapshot
            self._current_id = sid
            logger.info("COW snapshot %s created (parent=%s)", sid, snapshot.parent_id)
            return snapshot

    def get(self, snapshot_id: str) -> Optional[MemorySnapshot]:
        """Retrieve a snapshot by ID."""
        with self._lock:
            return self._snapshots.get(snapshot_id)

    def get_current(self) -> Optional[MemorySnapshot]:
        """Get the current (HEAD) snapshot."""
        with self._lock:
            if self._current_id is None:
                return None
            return self._snapshots.get(self._current_id)

    def lineage(self, snapshot_id: Optional[str] = None) -> List[str]:
        """Return the ancestor chain (oldest to newest) for a snapshot."""
        with self._lock:
            sid = snapshot_id or self._current_id
            chain: List[str] = []
            while sid:
                snap = self._snapshots.get(sid)
                if snap is None:
                    break
                chain.append(sid)
                sid = snap.parent_id if snap.parent_id != sid else None
            return list(reversed(chain))

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_snapshots": len(self._snapshots),
                "current_snapshot_id": self._current_id,
                "tags": [(s.tag, sid) for sid, s in self._snapshots.items() if s.tag],
            }


# ---------------------------------------------------------------------------
# MemoryBranch
# ---------------------------------------------------------------------------

class MemoryBranch:
    """
    Creates and manages isolated memory branches for experimental
    modifications without affecting the main branch.
    """

    def __init__(self, cow: CopyOnWriteSnapshot) -> None:
        self._cow = cow
        self._branches: Dict[str, BranchInfo] = {}
        self._active_branch: Optional[str] = None
        self._lock = threading.RLock()

    def create_branch(
        self,
        name: str,
        from_snapshot_id: Optional[str] = None,
        description: str = "",
    ) -> BranchInfo:
        """Create a new named branch from a base snapshot."""
        with self._lock:
            if name in self._branches:
                raise ValueError(f"Branch '{name}' already exists")

            base_id = from_snapshot_id or (
                self._cow.get_current().snapshot_id
                if self._cow.get_current() else None
            )
            if base_id is None:
                raise ValueError("No base snapshot available")

            branch = BranchInfo(
                branch_name=name,
                base_snapshot_id=base_id,
                head_snapshot_id=base_id,
                created_at=time.time(),
                description=description,
            )
            self._branches[name] = branch
            self._active_branch = name
            return branch

    def switch(self, branch_name: str) -> None:
        """Switch active branch."""
        with self._lock:
            if branch_name not in self._branches:
                raise KeyError(f"Branch '{branch_name}' not found")
            self._active_branch = branch_name

    def commit_to_branch(self, snapshot_id: str) -> None:
        """Point branch HEAD to a new snapshot."""
        with self._lock:
            if self._active_branch is None:
                raise RuntimeError("No active branch")
            branch = self._branches[self._active_branch]
            branch.head_snapshot_id = snapshot_id

    def list_branches(self) -> List[BranchInfo]:
        with self._lock:
            return list(self._branches.values())

    def get_active_branch(self) -> Optional[str]:
        with self._lock:
            return self._active_branch

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_branches": len(self._branches),
                "active_branch": self._active_branch,
                "branch_names": list(self._branches.keys()),
            }


# ---------------------------------------------------------------------------
# MemoryDiffer
# ---------------------------------------------------------------------------

class MemoryDiffer:
    """
    Computes incremental diffs between two memory snapshots,
    identifying additions, modifications, and deletions.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def diff(
        self,
        source: MemorySnapshot,
        target: MemorySnapshot,
    ) -> MemoryDiff:
        """Compute the diff between source and target snapshots."""
        with self._lock:
            src_data = source.data
            tgt_data = target.data

            # Collect all keys
            all_keys = set(src_data.keys()) | set(tgt_data.keys())
            src_keys = set(src_data.keys())
            tgt_keys = set(tgt_data.keys())

            additions: Dict[str, Any] = {}
            modifications: Dict[str, Tuple[Any, Any]] = {}
            deletions: Set[str] = set()

            for key in all_keys:
                in_src = key in src_keys
                in_tgt = key in tgt_keys

                if not in_src and in_tgt:
                    additions[key] = tgt_data[key]
                elif in_src and not in_tgt:
                    deletions.add(key)
                elif in_src and in_tgt:
                    if not self._deep_equal(src_data[key], tgt_data[key]):
                        modifications[key] = (src_data[key], tgt_data[key])

            return MemoryDiff(
                source_id=source.snapshot_id,
                target_id=target.snapshot_id,
                additions=additions,
                modifications=modifications,
                deletions=deletions,
            )

    @staticmethod
    def _deep_equal(a: Any, b: Any) -> bool:
        """Deep equality check supporting numpy arrays and nested dicts."""
        if isinstance(a, np.ndarray) and isinstance(b, np.ndarray):
            return bool(np.array_equal(a, b))
        if isinstance(a, dict) and isinstance(b, dict):
            if set(a.keys()) != set(b.keys()):
                return False
            return all(MemoryDiff._deep_equal(a[k], b[k]) for k in a)
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            if len(a) != len(b):
                return False
            return all(MemoryDiff._deep_equal(x, y) for x, y in zip(a, b))
        return a == b

    def diff_current(
        self,
        cow: CopyOnWriteSnapshot,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
    ) -> MemoryDiff:
        """Convenience: diff two snapshots from the COW store."""
        from_snap = cow.get(from_id) if from_id else cow.get_current()
        to_snap = cow.get(to_id) if to_id else cow.get_current()
        if from_snap is None or to_snap is None:
            raise ValueError("Source or target snapshot not found")
        return self.diff(from_snap, to_snap)

    @staticmethod
    def summarize(diff: MemoryDiff) -> str:
        lines = [
            f"Diff: {diff.source_id}..{diff.target_id}",
            f"  + {len(diff.additions)} additions",
            f"  ~ {len(diff.modifications)} modifications",
            f"  - {len(diff.deletions)} deletions",
        ]
        return "\n".join(lines)

    def statistics(self) -> Dict[str, Any]:
        return {"status": "ready"}


# ---------------------------------------------------------------------------
# MemoryMerge
# ---------------------------------------------------------------------------

class MemoryMerge:
    """
    Merges changes from an experimental branch back into the main
    branch, handling conflicts with configurable strategies.
    """

    def __init__(self, differ: MemoryDiffer) -> None:
        self._differ = differ
        self._lock = threading.RLock()

    def merge(
        self,
        base: MemorySnapshot,
        branch_snapshot: MemorySnapshot,
        strategy: str = "branch_wins",
    ) -> MergeResult:
        """
        Merge branch_snapshot into base.
        Strategy: 'branch_wins' | 'base_wins' | 'manual'
        """
        with self._lock:
            diff = self._differ.diff(base, branch_snapshot)
            conflicts: List[str] = []
            resolved_auto = 0
            manual_needed = 0

            merged_data = copy.deepcopy(base.data)

            # Auto-resolve additions (no conflict possible)
            for key, val in diff.additions.items():
                if key not in merged_data:
                    merged_data[key] = val
                    resolved_auto += 1

            # Handle modifications
            for key, (old_val, new_val) in diff.modifications.items():
                if key not in merged_data:
                    merged_data[key] = new_val
                    resolved_auto += 1
                elif strategy == "branch_wins":
                    merged_data[key] = new_val
                    resolved_auto += 1
                elif strategy == "base_wins":
                    # keep base value
                    pass
                elif strategy == "manual":
                    conflicts.append(f"MODIFY {key}: base={old_val} vs branch={new_val}")
                    manual_needed += 1

            # Handle deletions
            for key in diff.deletions:
                if strategy in ("branch_wins", "manual"):
                    if key in merged_data:
                        del merged_data[key]
                        resolved_auto += 1
                elif strategy == "base_wins":
                    conflicts.append(f"DELETE {key}: branch deleted, base retained")

            status = MergeStatus.CLEAN if not conflicts else MergeStatus.CONFLICT

            # Build merged snapshot ID
            merged_id = hashlib.sha256(
                f"merge_{base.snapshot_id}_{branch_snapshot.snapshot_id}_{time.time()}".encode()
            ).hexdigest()[:12]

            return MergeResult(
                status=status,
                merged_snapshot_id=merged_id,
                conflicts=conflicts,
                resolved_automatically=resolved_auto,
                required_manual_resolution=manual_needed,
            )

    def statistics(self) -> Dict[str, Any]:
        return {"status": "ready"}


# ---------------------------------------------------------------------------
# MemoryRollback
# ---------------------------------------------------------------------------

class MemoryRollback:
    """
    One-click rollback to a previously tagged stable checkpoint.
    """

    def __init__(self, cow: CopyOnWriteSnapshot) -> None:
        self._cow = cow
        self._rollback_history: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    def rollback_to_tag(self, tag_name: str) -> RollbackStatus:
        """Rollback the current state to a tagged snapshot."""
        with self._lock:
            # Find tag
            target_snap: Optional[MemorySnapshot] = None
            for sid, snap in self._cow._snapshots.items():
                if snap.tag == tag_name:
                    target_snap = snap
                    break

            if target_snap is None:
                return RollbackStatus.TAG_NOT_FOUND

            # Record rollback event
            prev = self._cow.get_current()
            self._rollback_history.append({
                "timestamp": time.time(),
                "from": prev.snapshot_id if prev else None,
                "to": target_snap.snapshot_id,
                "tag": tag_name,
            })

            # Set current to target
            self._cow._current_id = target_snap.snapshot_id

            logger.info("Rollback to tag '%s' (snapshot %s)", tag_name, target_snap.snapshot_id)
            return RollbackStatus.SUCCESS

    def rollback_to_snapshot(self, snapshot_id: str) -> RollbackStatus:
        """Rollback to a specific snapshot ID."""
        with self._lock:
            target = self._cow.get(snapshot_id)
            if target is None:
                return RollbackStatus.TAG_NOT_FOUND

            prev = self._cow.get_current()
            self._rollback_history.append({
                "timestamp": time.time(),
                "from": prev.snapshot_id if prev else None,
                "to": snapshot_id,
                "tag": None,
            })
            self._cow._current_id = snapshot_id
            return RollbackStatus.SUCCESS

    def history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._rollback_history)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_rollbacks": len(self._rollback_history),
                "latest_rollback": self._rollback_history[-1] if self._rollback_history else None,
            }


# ---------------------------------------------------------------------------
# MemoryTag
# ---------------------------------------------------------------------------

class MemoryTag:
    """
    Tags stable memory states for easy reference and rollback.
    """

    def __init__(self, cow: CopyOnWriteSnapshot) -> None:
        self._cow = cow
        self._tags: Dict[str, TagEntry] = {}
        self._lock = threading.RLock()

    def tag(
        self,
        tag_name: str,
        snapshot_id: Optional[str] = None,
        description: str = "",
    ) -> TagEntry:
        """Tag the current or specified snapshot."""
        with self._lock:
            if tag_name in self._tags:
                raise ValueError(f"Tag '{tag_name}' already exists")

            target_id = snapshot_id
            if target_id is None:
                current = self._cow.get_current()
                target_id = current.snapshot_id if current else None

            if target_id is None:
                raise ValueError("No snapshot to tag")

            entry = TagEntry(
                tag_name=tag_name,
                snapshot_id=target_id,
                created_at=time.time(),
                description=description,
            )
            self._tags[tag_name] = entry

            # Also update the snapshot's tag field
            snap = self._cow.get(target_id)
            if snap:
                snap.tag = tag_name

            return entry

    def resolve(self, tag_name: str) -> Optional[str]:
        """Return the snapshot_id for a given tag."""
        with self._lock:
            entry = self._tags.get(tag_name)
            return entry.snapshot_id if entry else None

    def list_tags(self) -> List[TagEntry]:
        with self._lock:
            return list(self._tags.values())

    def delete_tag(self, tag_name: str) -> bool:
        with self._lock:
            if tag_name not in self._tags:
                return False
            del self._tags[tag_name]
            return True

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_tags": len(self._tags),
                "tag_names": list(self._tags.keys()),
            }
