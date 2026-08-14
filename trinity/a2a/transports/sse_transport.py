"""
Trinity A2A SSE Transport

Implements Server-Sent Events (SSE) transport for real-time A2A task
monitoring and streaming.  Based on asyncio + aiohttp for async I/O.

Key features:
  - TaskEvent stream with state change / progress / result notifications
  - Client SSE subscription with automatic reconnection
  - Event filtering by task_id, agent_name, or event_type
  - Keepalive pings every 15 seconds
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """SSE event types for A2A task lifecycle."""

    TASK_CREATED = "task_created"
    TASK_ASSIGNED = "task_assigned"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    STATE_CHANGE = "state_change"
    HEARTBEAT = "heartbeat"


@dataclass
class TaskEvent:
    """A single A2A task event emitted over SSE."""

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    event_type: EventType = EventType.TASK_PROGRESS
    task_id: str = ""
    agent_name: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        """Serialize to SSE text/event-stream format.

        Returns
        -------
        str
            SSE-formatted string (id, event, data lines separated by \\n\\n).
        """
        lines: List[str] = []
        lines.append(f"id: {self.event_id}")
        lines.append(f"event: {self.event_type.value}")
        payload = {
            "task_id": self.task_id,
            "agent_name": self.agent_name,
            "event_type": self.event_type.value,
            "data": self.data,
            "timestamp": self.timestamp,
        }
        lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}")
        return "\n".join(lines) + "\n\n"


# ── SSE Transport ──────────────────────────────────────────────────


class SSETransport:
    """Server-Sent Events transport for Trinity A2A.

    Manages client subscriptions, event broadcasting, and keepalive
    pings.  Designed to be embedded in an aiohttp web application.

    Usage::

        from aiohttp import web
        app = web.Application()
        sse = SSETransport()
        sse.mount(app, path="/a2a/events")

        # Emit events
        sse.emit(TaskEvent(
            event_type=EventType.TASK_PROGRESS,
            task_id="task-1",
            agent_name="file-agent",
            data={"progress": 0.5},
        ))

    Client subscription (JavaScript)::

        const evt = new EventSource("/a2a/events");
        evt.addEventListener("task_progress", (e) => {
            const data = JSON.parse(e.data);
            console.log(data);
        });
        // Auto-reconnect is built into EventSource spec
    """

    # Keepalive interval in seconds
    KEEPALIVE_INTERVAL = 15

    # Maximum subscribers before rejecting new connections
    MAX_SUBSCRIBERS = 1000

    def __init__(self) -> None:
        self._subscribers: Dict[str, asyncio.Queue] = {}
        self._filters: Dict[str, Dict[str, Any]] = {}
        self._keepalive_task: Optional[asyncio.Task] = None
        self._running = False

    # ── Public API ─────────────────────────────────────────────────

    def mount(self, app: Any, path: str = "/a2a/events") -> None:
        """Mount SSE handler on an aiohttp application.

        Parameters
        ----------
        app : aiohttp.web.Application
            The aiohttp application to mount on.
        path : str
            URL path for the SSE endpoint.
        """
        try:
            from aiohttp import web

            app.router.add_get(path, self._handle_sse)
            app.on_startup.append(self._on_startup)
            app.on_shutdown.append(self._on_shutdown)
            logger.info("SSE transport mounted at %s", path)
        except ImportError:
            raise ImportError(
                "SSE transport requires 'aiohttp' package. "
                "Install with: pip install aiohttp"
            )

    def emit(self, event: TaskEvent) -> None:
        """Emit a TaskEvent to all matching subscribers.

        Parameters
        ----------
        event : TaskEvent
            The event to broadcast.
        """
        sse_text = event.to_sse()
        delivered = 0
        for sub_id, queue in list(self._subscribers.items()):
            if self._matches_filter(sub_id, event):
                try:
                    queue.put_nowait(sse_text)
                    delivered += 1
                except asyncio.QueueFull:
                    logger.debug("SSE subscriber %s queue full, dropping event", sub_id)

        logger.debug(
            "SSE event %s → %d subscribers (task=%s, agent=%s)",
            event.event_type.value,
            delivered,
            event.task_id,
            event.agent_name,
        )

    def emit_task_progress(
        self,
        task_id: str,
        agent_name: str,
        progress: float,
        message: str = "",
    ) -> None:
        """Convenience method for emitting progress events.

        Parameters
        ----------
        task_id : str
            Task identifier.
        agent_name : str
            Source agent name.
        progress : float
            Progress value 0.0–1.0.
        message : str
            Optional progress message.
        """
        self.emit(
            TaskEvent(
                event_type=EventType.TASK_PROGRESS,
                task_id=task_id,
                agent_name=agent_name,
                data={"progress": round(progress, 4), "message": message},
            )
        )

    def emit_state_change(
        self,
        task_id: str,
        agent_name: str,
        from_state: str,
        to_state: str,
        reason: str = "",
    ) -> None:
        """Convenience method for state transition events."""
        self.emit(
            TaskEvent(
                event_type=EventType.STATE_CHANGE,
                task_id=task_id,
                agent_name=agent_name,
                data={
                    "from_state": from_state,
                    "to_state": to_state,
                    "reason": reason,
                },
            )
        )

    def subscriber_count(self) -> int:
        """Return current subscriber count."""
        return len(self._subscribers)

    # ── Internal ───────────────────────────────────────────────────

    def _matches_filter(self, sub_id: str, event: TaskEvent) -> bool:
        """Check if event matches subscriber filter."""
        sub_filter = self._filters.get(sub_id)
        if sub_filter is None:
            return True  # No filter → receive all

        if "task_id" in sub_filter and sub_filter["task_id"]:
            if sub_filter["task_id"] != event.task_id:
                return False

        if "agent_name" in sub_filter and sub_filter["agent_name"]:
            if sub_filter["agent_name"] != event.agent_name:
                return False

        if "event_types" in sub_filter and sub_filter["event_types"]:
            if event.event_type.value not in sub_filter["event_types"]:
                return False

        return True

    async def _handle_sse(self, request: Any) -> Any:
        """aiohttp request handler for SSE endpoint."""
        from aiohttp import web

        if len(self._subscribers) >= self.MAX_SUBSCRIBERS:
            return web.json_response(
                {"error": "Too many subscribers"},
                status=503,
            )

        # Parse optional filter params from query string
        task_id = request.query.get("task_id", "")
        agent_name = request.query.get("agent_name", "")
        event_types_str = request.query.get(
            "event_types", ""
        )  # comma-separated

        event_types: Optional[List[str]] = None
        if event_types_str:
            event_types = [
                t.strip() for t in event_types_str.split(",") if t.strip()
            ]

        sub_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers[sub_id] = queue
        self._filters[sub_id] = {
            "task_id": task_id,
            "agent_name": agent_name,
            "event_types": event_types,
        }

        logger.debug(
            "SSE subscriber %s connected (task=%s, agent=%s, types=%s)",
            sub_id, task_id, agent_name, event_types,
        )

        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )
        await response.prepare(request)

        try:
            # Send initial connected event
            connected = TaskEvent(
                event_type=EventType.HEARTBEAT,
                data={"status": "connected", "subscriber_id": sub_id},
            )
            await response.write(connected.to_sse().encode("utf-8"))

            while True:
                try:
                    sse_text = await asyncio.wait_for(
                        queue.get(), timeout=self.KEEPALIVE_INTERVAL
                    )
                    await response.write(sse_text.encode("utf-8"))
                except asyncio.TimeoutError:
                    # Send keepalive comment (SSE spec — lines starting with ':' are ignored)
                    await response.write(b": keepalive\n\n")
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self._subscribers.pop(sub_id, None)
            self._filters.pop(sub_id, None)
            logger.debug("SSE subscriber %s disconnected", sub_id)

        return response

    async def _on_startup(self, app: Any) -> None:
        """aiohttp on_startup callback."""
        self._running = True
        logger.info("SSE transport started")

    async def _on_shutdown(self, app: Any) -> None:
        """aiohttp on_shutdown callback."""
        self._running = False
        # Notify all subscribers
        shutdown_event = TaskEvent(
            event_type=EventType.HEARTBEAT,
            data={"status": "shutdown"},
        ).to_sse()
        for queue in self._subscribers.values():
            try:
                queue.put_nowait(shutdown_event)
            except asyncio.QueueFull:
                pass
        logger.info("SSE transport stopped (%d subscribers)", len(self._subscribers))


# ── Server Start Helper ────────────────────────────────────────────


def start_sse_server(
    task_manager: Any = None,
    host: str = "0.0.0.0",
    port: int = 8080,
    path: str = "/a2a/events",
) -> Optional[Any]:
    """Start a standalone aiohttp server for SSE transport.

    Parameters
    ----------
    task_manager : TaskManager, optional
        A2A TaskManager for state change propagation.
    host : str
        Bind address.
    port : int
        Bind port.
    path : str
        SSE endpoint path.

    Returns
    -------
    aiohttp.web.Application or None (if aiohttp not installed).
    """
    try:
        from aiohttp import web

        app = web.Application()
        sse = SSETransport()
        sse.mount(app, path=path)

        # If task manager is provided, wire up state change propagation
        if task_manager is not None:
            original_transition = getattr(task_manager, "_transition_task", None)

            def _hook(task_id: str, from_state: str, to_state: str, reason: str = "") -> None:
                if original_transition:
                    original_transition(task_id, from_state, to_state, reason)
                sse.emit_state_change(
                    task_id=task_id,
                    agent_name=getattr(task_manager, "_agent_name", "unknown"),
                    from_state=from_state,
                    to_state=to_state,
                    reason=reason,
                )
                # Emit specific lifecycle events
                if to_state == "completed":
                    sse.emit(
                        TaskEvent(
                            event_type=EventType.TASK_COMPLETED,
                            task_id=task_id,
                            agent_name=getattr(task_manager, "_agent_name", "unknown"),
                            data={"state": to_state},
                        )
                    )
                elif to_state == "failed":
                    sse.emit(
                        TaskEvent(
                            event_type=EventType.TASK_FAILED,
                            task_id=task_id,
                            agent_name=getattr(task_manager, "_agent_name", "unknown"),
                            data={"state": to_state},
                        )
                    )

            task_manager._transition_hook = _hook

        web.run_app(app, host=host, port=port, print=lambda *_: None)
        return app

    except ImportError:
        logger.warning("aiohttp not installed — SSE server not started")
        return None


# ── Self-Test ──────────────────────────────────────────────────────

def self_test() -> Dict[str, Any]:
    """Run in-process SSE transport self-tests."""
    checks: List[Dict[str, Any]] = []

    # 1: TaskEvent serialization
    try:
        event = TaskEvent(
            event_type=EventType.TASK_PROGRESS,
            task_id="test-1",
            agent_name="file-agent",
            data={"progress": 0.5},
        )
        sse_text = event.to_sse()
        assert "id:" in sse_text
        assert "event: task_progress" in sse_text
        assert '"task_id":"test-1"' in sse_text
        checks.append({"name": "event_serialization", "pass": True, "detail": "valid SSE format"})
    except Exception as e:
        checks.append({"name": "event_serialization", "pass": False, "detail": str(e)})

    # 2: SSETransport subscribe/filter/emit
    try:
        sse = SSETransport()
        assert sse.subscriber_count() == 0
        assert sse._matches_filter("nonexistent", event)  # no filter
        checks.append({"name": "transport_init", "pass": True, "detail": "0 subscribers"})
    except Exception as e:
        checks.append({"name": "transport_init", "pass": False, "detail": str(e)})

    # 3: EventType enum
    try:
        assert EventType.TASK_CREATED.value == "task_created"
        assert EventType.HEARTBEAT.value == "heartbeat"
        assert len(EventType) == 8
        checks.append({"name": "event_types", "pass": True, "detail": f"{len(EventType)} types"})
    except Exception as e:
        checks.append({"name": "event_types", "pass": False, "detail": str(e)})

    all_pass = all(c["pass"] for c in checks)
    return {"pass": all_pass, "checks": checks}


if __name__ == "__main__":
    result = self_test()
    print(json.dumps(result, indent=2, ensure_ascii=False))
