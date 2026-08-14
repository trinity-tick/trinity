"""
A2A TaskManager — Google A2A v0.3 task lifecycle management.

Implements the A2A task state machine:
  pending → in_progress → completed / failed / cancelled

All state transitions are logged via the adapter layer for auditability.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Lazy-import to avoid circular dependency
_security_warning_issued = False


class TaskState(str, Enum):
    """A2A v0.3 task lifecycle states."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Valid state transitions
_STATE_TRANSITIONS: Dict[TaskState, List[TaskState]] = {
    TaskState.PENDING: [TaskState.IN_PROGRESS, TaskState.CANCELLED],
    TaskState.IN_PROGRESS: [TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED],
    # Terminal states — no transitions out
    TaskState.COMPLETED: [],
    TaskState.FAILED: [],
    TaskState.CANCELLED: [],
}


@dataclass
class A2ATask:
    """A2A cross-agent task record."""
    task_id: str = ""
    from_agent: str = ""
    to_agent: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    result: Optional[Dict[str, Any]] = None
    created_at: str = ""
    updated_at: str = ""


class TaskManager:
    """A2A v0.3 Task Lifecycle Manager.

    Manages the full lifecycle of cross-agent tasks with state machine
    enforcement and SSE push capabilities.
    """

    def __init__(self, adapter=None, task_permission=None):
        self._adapter = adapter
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[callable]] = {}  # task_id → callbacks
        self._stats: Dict[str, int] = {
            "total_created": 0,
            "total_completed": 0,
            "total_failed": 0,
            "total_cancelled": 0,
        }
        # A2A Security — TaskPermission integration
        if task_permission is None:
            from trinity.a2a.security import get_task_permission
            self._task_permission = get_task_permission()
        else:
            self._task_permission = task_permission

    # ── State Machine ───────────────────────────────────────────────

    def _validate_transition(self, current: str, target: str) -> bool:
        """Check if a state transition is valid."""
        try:
            cur = TaskState(current)
            tgt = TaskState(target)
            return tgt in _STATE_TRANSITIONS.get(cur, [])
        except ValueError:
            return False

    # ── Task CRUD ───────────────────────────────────────────────────

    def create_task(
        self,
        from_agent: str,
        to_agent: str,
        payload: Dict[str, Any],
    ) -> A2ATask:
        """Create a cross-agent task.

        Args:
            from_agent: Originating agent ID.
            to_agent: Target agent ID.
            payload: Task payload dict with method, params, etc.

        Returns:
            Created A2ATask with pending status.
        """
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()

        # A2A Security: check task creation permission
        if not self._task_permission.can_create_task(from_agent, to_agent):
            raise PermissionError(
                f"Agent '{from_agent}' is not allowed to create tasks for '{to_agent}'"
            )

        task = A2ATask(
            task_id=task_id,
            from_agent=from_agent,
            to_agent=to_agent,
            payload=payload,
            status=TaskState.PENDING.value,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._stats["total_created"] += 1
            self._subscribers.setdefault(task_id, [])

        # A2A Security: register task ACL
        self._task_permission.register_task(task_id, from_agent, to_agent)

        # Persist
        if self._adapter and hasattr(self._adapter, "create_a2a_task"):
            try:
                self._adapter.create_a2a_task(
                    task_id=task_id,
                    from_agent=from_agent,
                    to_agent=to_agent,
                    payload=json.dumps(payload, ensure_ascii=False),
                    status=task.status,
                    result=json.dumps(task.result) if task.result else None,
                )
            except Exception as e:
                logger.warning("Failed to persist a2a task: %s", e)

        logger.info("A2A task %s created: %s → %s", task_id, from_agent, to_agent)
        return task

    def query_task(self, task_id: str, agent_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Query task status from storage.

        A2A Security: when *agent_id* is provided, the read permission
        is enforced — only the creator, assignee, superior, or an explicitly
        granted guest can read the task.

        Args:
            task_id: Task identifier.
            agent_id: Optional requesting agent (enables permission check).
        """
        if agent_id and not self._task_permission.can_read_task(agent_id, task_id):
            raise PermissionError(
                f"Agent '{agent_id}' is not allowed to read task '{task_id}'"
            )
        if self._adapter and hasattr(self._adapter, "list_a2a_tasks"):
            tasks = self._adapter.list_a2a_tasks(task_id=task_id)
            if tasks:
                return tasks[0]
        return None

    def update_task(self, task_id: str, status: str,
                    result: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Update task status with state transition validation.

        Args:
            task_id: Task identifier.
            status: New status (must be valid transition).
            result: Optional result payload.

        Returns:
            Updated task dict, or None if task not found / invalid transition.
        """
        current = self.query_task(task_id)
        if not current:
            logger.warning("Task %s not found", task_id)
            return None

        if not self._validate_transition(current["status"], status):
            logger.warning(
                "Invalid transition for task %s: %s → %s",
                task_id, current["status"], status,
            )
            return None

        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if status == TaskState.COMPLETED.value:
                self._stats["total_completed"] += 1
            elif status == TaskState.FAILED.value:
                self._stats["total_failed"] += 1
            elif status == TaskState.CANCELLED.value:
                self._stats["total_cancelled"] += 1

        # Persist
        if self._adapter and hasattr(self._adapter, "update_a2a_task"):
            try:
                self._adapter.update_a2a_task(
                    task_id=task_id,
                    status=status,
                    result=json.dumps(result, ensure_ascii=False) if result else None,
                )
            except Exception as e:
                logger.warning("Failed to update a2a task: %s", e)

        # Notify SSE subscribers
        self._notify_subscribers(task_id, status, result)

        logger.info("A2A task %s: %s → %s", task_id, current["status"], status)
        return self.query_task(task_id)

    def cancel_task(self, task_id: str, agent_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Cancel a pending or in-progress task.

        A2A Security: only the creator or a superior agent may cancel.
        The optional *agent_id* parameter identifies the requesting agent.
        """
        if agent_id and not self._task_permission.can_cancel_task(agent_id, task_id):
            raise PermissionError(
                f"Agent '{agent_id}' is not allowed to cancel task '{task_id}'"
            )
        return self.update_task(task_id, TaskState.CANCELLED.value)

    def list_tasks(self, agent_id: Optional[str] = None,
                   status: Optional[str] = None,
                   limit: int = 50) -> List[Dict[str, Any]]:
        """List tasks with optional filters.

        Args:
            agent_id: Filter by from_agent or to_agent.
            status: Filter by task status.
            limit: Maximum results.

        Returns:
            List of task dicts.
        """
        if self._adapter and hasattr(self._adapter, "list_a2a_tasks"):
            return self._adapter.list_a2a_tasks(
                agent_id=agent_id, status=status, limit=limit,
            )
        return []

    # ── SSE Push ────────────────────────────────────────────────────

    def subscribe(self, task_id: str, callback: callable) -> None:
        """Subscribe to SSE status updates for a task."""
        with self._lock:
            self._subscribers.setdefault(task_id, []).append(callback)

    def _notify_subscribers(self, task_id: str, status: str,
                            result: Optional[Dict[str, Any]]) -> None:
        """Push status update to all subscribers."""
        for cb in self._subscribers.get(task_id, []):
            try:
                cb({"task_id": task_id, "status": status, "result": result})
            except Exception:
                pass

    # ── Stats ───────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._stats)

    # ── Self-Test ──────────────────────────────────────────────────────

    def self_test(self) -> Dict[str, Any]:
        """Runtime self-diagnostic: task lifecycle + state machine validation.

        Returns:
            {"pass": bool, "checks": [...], "summary": str}
        """
        import os
        import tempfile
        db_path = os.path.join(tempfile.gettempdir(), f"trinity_task_test_{os.getpid()}.db")
        checks = []

        try:
            from trinity.adapters.sqlite import SQLiteAdapter
            adapter = SQLiteAdapter(db_path)
            adapter.connect()
            tm = TaskManager(adapter=adapter)

            # Check 1: create_task returns valid task_id
            try:
                task = tm.create_task("agent_a", "agent_b", {"method": "search", "params": {"q": "test"}})
                assert task.task_id.startswith("task_"), f"Unexpected task_id: {task.task_id}"
                assert task.status == "pending", f"Expected pending, got {task.status}"
                assert task.from_agent == "agent_a"
                checks.append({"name": "create_task", "pass": True, "detail": f"task_id={task.task_id}, status={task.status}"})
            except Exception as e:
                checks.append({"name": "create_task", "pass": False, "detail": str(e)})

            # Check 2: query_task finds the task
            try:
                result = tm.query_task(task.task_id)
                assert result is not None, "query_task returned None"
                assert result["task_id"] == task.task_id
                checks.append({"name": "query_task", "pass": True, "detail": f"Found task {task.task_id}"})
            except Exception as e:
                checks.append({"name": "query_task", "pass": False, "detail": str(e)})

            # Check 3: update_status transitions pending → in_progress
            try:
                updated = tm.update_task(task.task_id, "in_progress")
                assert updated is not None, "update_task returned None"
                assert updated["status"] == "in_progress", f"Expected in_progress, got {updated['status']}"
                checks.append({"name": "update_status", "pass": True, "detail": f"pending → {updated['status']}"})
            except Exception as e:
                checks.append({"name": "update_status", "pass": False, "detail": str(e)})

            # Check 4: cancel_task on pending task
            task2 = tm.create_task("agent_c", "agent_d", {"method": "compute", "params": {}})
            try:
                cancelled = tm.cancel_task(task2.task_id)
                assert cancelled is not None, "cancel_task returned None"
                assert cancelled["status"] == "cancelled", f"Expected cancelled, got {cancelled['status']}"
                checks.append({"name": "cancel_pending_task", "pass": True, "detail": f"Status: pending → {cancelled['status']}"})
            except Exception as e:
                checks.append({"name": "cancel_pending_task", "pass": False, "detail": str(e)})

            # Check 5: completed task cannot be cancelled (state machine)
            try:
                updated = tm.update_task(task.task_id, "completed", {"output": "done"})
                assert updated["status"] == "completed"
                # Try to cancel completed task — should fail
                cancelled = tm.cancel_task(task.task_id)
                assert cancelled is None, "Should not cancel completed task"
                checks.append({"name": "state_machine_terminal", "pass": True, "detail": "Completed task correctly refused cancellation"})
            except Exception as e:
                checks.append({"name": "state_machine_terminal", "pass": False, "detail": str(e)})

            # Check 6: invalid transition rejected
            try:
                task3 = tm.create_task("agent_e", "agent_f", {"method": "ping"})
                result = tm.update_task(task3.task_id, "completed")  # pending → completed is invalid (must go through in_progress)
                assert result is None, f"Invalid transition should return None, got {result}"
                checks.append({"name": "invalid_transition", "pass": True, "detail": "pending→completed correctly rejected"})
            except Exception as e:
                checks.append({"name": "invalid_transition", "pass": False, "detail": str(e)})

            # Check 7: get_stats
            try:
                stats = tm.get_stats()
                assert stats["total_created"] >= 3, f"Expected >=3 created, got {stats['total_created']}"
                assert stats["total_cancelled"] >= 1
                assert stats["total_completed"] >= 1
                checks.append({"name": "stats", "pass": True, "detail": f"created={stats['total_created']}, completed={stats['total_completed']}, cancelled={stats['total_cancelled']}"})
            except Exception as e:
                checks.append({"name": "stats", "pass": False, "detail": str(e)})

            adapter.disconnect()
        except Exception as e:
            checks.append({"name": "setup", "pass": False, "detail": f"Test harness failure: {e}"})
        finally:
            try:
                if os.path.exists(db_path):
                    os.unlink(db_path)
            except OSError:
                pass

        all_pass = all(c["pass"] for c in checks)
        return {
            "pass": all_pass,
            "checks": checks,
            "summary": f"TaskManager self-test: {sum(1 for c in checks if c['pass'])}/{len(checks)} passed",
        }


# ── Module-level self_test ─────────────────────────────────────────


def self_test() -> Dict[str, Any]:
    """Module-level entry point for regression testing."""
    tm = TaskManager()
    return tm.self_test()
