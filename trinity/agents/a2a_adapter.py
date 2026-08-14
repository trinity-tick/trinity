"""
A2A Adapter — Agent-to-Agent Protocol Adapter
==============================================
Implements the Microsoft ISE A2A protocol structures for contextId management,
Message/Part data types, and AgentCard capability descriptors.

Alignments:
  - Microsoft ISE A2A: Embedded Context Pattern + contextId lifecycle (2026.06)
  - Innoflexion Enterprise Multi-Agent (2026): MCP+A2A dual protocol orchestration

Components:
  - contextId manager: one contextId maps to multiple task IDs
  - Message / Part: structured agent communication payloads
  - AgentCard: per-agent capability and interface description
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# ── Configuration constants ──────────────────────────────────────────────

A2A_CONTEXT_TTL = 3600.0        # 1 hour
A2A_MESSAGE_MAX_PARTS = 10
A2A_PART_MAX_TEXT_LENGTH = 4096


# ── Enums ─────────────────────────────────────────────────────────────────

class PartType(Enum):
    """Content type discriminator for A2A message parts."""
    TEXT = "text"
    FILE = "file"
    DATA = "data"
    METADATA = "metadata"


class MessageRole(Enum):
    """Role of the sender in an A2A message."""
    COORDINATOR = "coordinator"   # Main Agent dispatching tasks
    AGENT = "agent"               # Sub-agent responding
    SYSTEM = "system"             # System-level notification


class TaskState(Enum):
    """Lifecycle state of an A2A task within a context."""
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ── Data Classes ──────────────────────────────────────────────────────────

@dataclass
class Part:
    """A single content part within an A2A message.

    Supports type discrimination (text/file/data/metadata) as specified
    in the Microsoft ISE A2A protocol.
    """
    part_type: PartType = PartType.TEXT
    content: Union[str, Dict[str, Any], bytes] = ""
    mime_type: str = "text/plain"
    encoding: str = "utf-8"
    metadata: Dict[str, Any] = field(default_factory=dict)
    part_id: str = ""

    def __post_init__(self):
        if not self.part_id:
            self.part_id = uuid.uuid4().hex[:8]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize part for transmission."""
        content_val: Any = self.content
        if isinstance(self.content, bytes):
            import base64
            content_val = base64.b64encode(self.content).decode("ascii")

        return {
            "part_id": self.part_id,
            "type": self.part_type.value,
            "content": content_val,
            "mime_type": self.mime_type,
            "encoding": self.encoding,
            "metadata": self.metadata,
        }

    def text_preview(self, max_len: int = 200) -> str:
        """Human-readable preview of part content."""
        if isinstance(self.content, str):
            return self.content[:max_len]
        if isinstance(self.content, dict):
            return str(self.content)[:max_len]
        return f"<{self.part_type.value} data, {len(self.content)} bytes>" if isinstance(self.content, bytes) else "<empty>"


@dataclass
class Message:
    """An A2A message exchanged between Coordinator and Agent.

    Each message belongs to a context (via context_id) and may optionally
    be associated with a specific task (via task_id).
    """
    message_id: str = ""
    context_id: str = ""
    task_id: str = ""
    role: MessageRole = MessageRole.COORDINATOR
    parts: List[Part] = field(default_factory=list)
    sender_agent: str = ""
    recipient_agent: str = ""
    timestamp: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.message_id:
            self.message_id = uuid.uuid4().hex[:16]
        if not self.timestamp:
            self.timestamp = time.time()

    def add_text_part(self, text: str, mime_type: str = "text/plain") -> Part:
        """Add a text content part to the message."""
        if len(self.parts) >= A2A_MESSAGE_MAX_PARTS:
            raise ValueError(f"Message already has {A2A_MESSAGE_MAX_PARTS} parts (max)")
        part = Part(part_type=PartType.TEXT, content=text, mime_type=mime_type)
        self.parts.append(part)
        return part

    def add_data_part(self, data: Dict[str, Any], mime_type: str = "application/json") -> Part:
        """Add a structured data part to the message."""
        if len(self.parts) >= A2A_MESSAGE_MAX_PARTS:
            raise ValueError(f"Message already has {A2A_MESSAGE_MAX_PARTS} parts (max)")
        part = Part(part_type=PartType.DATA, content=data, mime_type=mime_type)
        self.parts.append(part)
        return part

    def add_file_part(self, file_path: str, metadata: Optional[Dict[str, Any]] = None) -> Part:
        """Add a file reference part to the message."""
        if len(self.parts) >= A2A_MESSAGE_MAX_PARTS:
            raise ValueError(f"Message already has {A2A_MESSAGE_MAX_PARTS} parts (max)")
        part = Part(
            part_type=PartType.FILE,
            content=file_path,
            mime_type="application/octet-stream",
            metadata=metadata or {},
        )
        self.parts.append(part)
        return part

    def get_text_content(self) -> str:
        """Concatenate all text parts."""
        return "\n".join(
            p.content for p in self.parts
            if p.part_type == PartType.TEXT and isinstance(p.content, str)
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize message for transmission."""
        return {
            "message_id": self.message_id,
            "context_id": self.context_id,
            "task_id": self.task_id,
            "role": self.role.value,
            "sender": self.sender_agent,
            "recipient": self.recipient_agent,
            "timestamp": self.timestamp,
            "parts": [p.to_dict() for p in self.parts],
            "metadata": self.metadata,
        }


@dataclass
class AgentCard:
    """Capability descriptor for an agent — part of A2A discovery.

    Each agent publishes an AgentCard describing its capabilities,
    interface, and supported memory types.
    """
    agent_name: str = ""
    display_name: str = ""
    description: str = ""
    capabilities: List[str] = field(default_factory=list)
    supported_memory_types: List[str] = field(default_factory=list)
    input_formats: List[str] = field(default_factory=list)
    output_formats: List[str] = field(default_factory=list)
    max_context_size: int = 4096
    version: str = "1.0.0"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize AgentCard for registry."""
        return {
            "agent_name": self.agent_name,
            "display_name": self.display_name,
            "description": self.description,
            "capabilities": self.capabilities,
            "supported_memory_types": self.supported_memory_types,
            "input_formats": self.input_formats,
            "output_formats": self.output_formats,
            "max_context_size": self.max_context_size,
            "version": self.version,
            "metadata": self.metadata,
        }


# ── ContextManager ────────────────────────────────────────────────────────

@dataclass
class A2AContext:
    """An A2A context — one contextId maps to multiple task IDs.

    Per Microsoft ISE spec: contextId is the session-level identifier;
    each dispatched task gets its own task_id under the same context.
    """
    context_id: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    coordinator_agent: str = "main"
    task_states: Dict[str, TaskState] = field(default_factory=dict)
    message_history: List[Message] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.context_id:
            self.context_id = f"a2a_{uuid.uuid4().hex[:12]}"
        if not self.created_at:
            self.created_at = time.time()
        if not self.expires_at:
            self.expires_at = self.created_at + A2A_CONTEXT_TTL

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) > self.expires_at

    def active_tasks(self) -> List[str]:
        return [
            tid for tid, state in self.task_states.items()
            if state in (TaskState.PENDING, TaskState.DISPATCHED, TaskState.RUNNING)
        ]

    def completed_tasks(self) -> List[str]:
        return [
            tid for tid, state in self.task_states.items()
            if state == TaskState.COMPLETED
        ]


class A2AContextManager:
    """Manages the lifecycle of A2A contexts (contextId ↔ tasks).

    Each context can hold multiple tasks; expired contexts are
    cleaned up during maintenance cycles.
    """

    def __init__(self, context_ttl: float = A2A_CONTEXT_TTL):
        self.context_ttl = context_ttl
        self._lock = threading.RLock()
        self._contexts: Dict[str, A2AContext] = {}
        logger.info("A2AContextManager initialized (ttl=%.0fs)", context_ttl)

    def create_context(self, coordinator: str = "main",
                       metadata: Optional[Dict[str, Any]] = None) -> A2AContext:
        """Create a new A2A context for a multi-task session."""
        with self._lock:
            now = time.time()
            ctx = A2AContext(
                coordinator_agent=coordinator,
                created_at=now,
                expires_at=now + self.context_ttl,
                metadata=metadata or {},
            )
            self._contexts[ctx.context_id] = ctx
            logger.debug("Created A2A context %s (coordinator=%s, ttl=%.0fs)",
                         ctx.context_id, coordinator, self.context_ttl)
            return ctx

    def get_context(self, context_id: str) -> Optional[A2AContext]:
        """Retrieve an existing context."""
        with self._lock:
            return self._contexts.get(context_id)

    def register_task(self, context_id: str, task_id: str,
                      state: TaskState = TaskState.PENDING) -> bool:
        """Register a task under a context."""
        with self._lock:
            ctx = self._contexts.get(context_id)
            if ctx is None:
                return False
            ctx.task_states[task_id] = state
            return True

    def update_task_state(self, context_id: str, task_id: str,
                          state: TaskState) -> bool:
        """Update the state of a task within a context."""
        with self._lock:
            ctx = self._contexts.get(context_id)
            if ctx is None:
                return False
            ctx.task_states[task_id] = state
            logger.debug("Task %s in context %s → %s", task_id, context_id, state.value)
            return True

    def add_message(self, context_id: str, message: Message) -> bool:
        """Append a message to context history."""
        with self._lock:
            ctx = self._contexts.get(context_id)
            if ctx is None:
                return False
            ctx.message_history.append(message)
            return True

    def get_context_summary(self, context_id: str) -> Dict[str, Any]:
        """Get a summary of a context's state."""
        with self._lock:
            ctx = self._contexts.get(context_id)
            if ctx is None:
                return {"error": "context not found"}

            state_counts: Dict[str, int] = {}
            for state in ctx.task_states.values():
                state_counts[state.value] = state_counts.get(state.value, 0) + 1

            return {
                "context_id": ctx.context_id,
                "coordinator": ctx.coordinator_agent,
                "created_at": ctx.created_at,
                "expires_at": ctx.expires_at,
                "expired": ctx.is_expired(),
                "total_tasks": len(ctx.task_states),
                "task_states": state_counts,
                "active_tasks": ctx.active_tasks(),
                "message_count": len(ctx.message_history),
            }

    def cleanup_expired(self) -> int:
        """Remove expired contexts. Returns count of cleaned contexts."""
        with self._lock:
            now = time.time()
            expired = [
                cid for cid, ctx in self._contexts.items()
                if ctx.is_expired(now)
            ]
            for cid in expired:
                del self._contexts[cid]
            if expired:
                logger.info("Cleaned up %d expired A2A contexts", len(expired))
            return len(expired)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_contexts": len(self._contexts),
                "context_ttl": self.context_ttl,
            }


# ── AgentCardRegistry ─────────────────────────────────────────────────────

class AgentCardRegistry:
    """Registry of AgentCards for agent discovery."""

    def __init__(self):
        self._lock = threading.RLock()
        self._cards: Dict[str, AgentCard] = {}
        self._init_default_cards()
        logger.info("AgentCardRegistry initialized with %d agents",
                     len(self._cards))

    def _init_default_cards(self) -> None:
        """Initialize default AgentCards for known Marvis agents."""
        defaults = {
            "file-agent": AgentCard(
                agent_name="file-agent",
                display_name="File Agent",
                description="Local file system intelligent assistant: extraction, analysis, search, and physical file operations",
                capabilities=["file_search", "file_read", "file_write", "file_delete",
                              "file_organize", "format_convert", "content_analysis"],
                supported_memory_types=["policy", "fact", "episodic"],
                input_formats=["text", "pdf", "docx", "xlsx", "pptx", "image"],
                output_formats=["text", "pdf", "docx", "xlsx", "pptx", "html"],
                max_context_size=4096,
            ),
            "browser": AgentCard(
                agent_name="browser",
                display_name="Browser Agent",
                description="Web browser automation: URL navigation, search, form filling, page extraction",
                capabilities=["web_search", "page_navigate", "form_fill", "content_extract",
                              "screenshot", "authentication"],
                supported_memory_types=["fact", "episodic", "trace"],
                input_formats=["text", "html", "url"],
                output_formats=["text", "html", "screenshot", "markdown"],
                max_context_size=4096,
            ),
            "app-agent": AgentCard(
                agent_name="app-agent",
                display_name="App Agent",
                description="Application lifecycle management: download, install, uninstall, launch, UI interaction",
                capabilities=["app_install", "app_uninstall", "app_launch", "ui_click",
                              "ui_input", "app_recommend"],
                supported_memory_types=["preference", "episodic", "trace"],
                input_formats=["text", "package_name"],
                output_formats=["text", "status"],
                max_context_size=2048,
            ),
            "computer-agent": AgentCard(
                agent_name="computer-agent",
                display_name="Computer Agent",
                description="Windows system expert: settings, diagnostics, process management, desktop layout",
                capabilities=["system_query", "settings_manage", "process_manage",
                              "diagnostics", "desktop_layout", "session_control"],
                supported_memory_types=["policy", "fact", "episodic"],
                input_formats=["text", "command"],
                output_formats=["text", "status", "log"],
                max_context_size=4096,
            ),
            "search-agent": AgentCard(
                agent_name="search-agent",
                display_name="Search Agent",
                description="Deep search and research: multi-round retrieval, comparative analysis, paper survey",
                capabilities=["deep_search", "comparison_analysis", "paper_survey",
                              "trend_analysis", "fact_verification"],
                supported_memory_types=["fact", "episodic"],
                input_formats=["text", "query"],
                output_formats=["text", "markdown", "report"],
                max_context_size=8192,
            ),
            "main": AgentCard(
                agent_name="main",
                display_name="Main Agent",
                description="General-purpose coordinator: task dispatch, result aggregation, MCP toolset",
                capabilities=["task_dispatch", "result_aggregate", "tool_routing",
                              "context_assembly", "memory_query"],
                supported_memory_types=["policy", "preference", "fact", "episodic", "trace"],
                input_formats=["text"],
                output_formats=["text", "json", "markdown"],
                max_context_size=16384,
            ),
        }
        self._cards.update(defaults)

    def register(self, card: AgentCard) -> None:
        """Register or update an AgentCard."""
        with self._lock:
            self._cards[card.agent_name] = card

    def get(self, agent_name: str) -> Optional[AgentCard]:
        """Get an AgentCard by agent name."""
        with self._lock:
            return self._cards.get(agent_name)

    def list_all(self) -> List[AgentCard]:
        """List all registered agent cards."""
        with self._lock:
            return list(self._cards.values())

    def find_by_capability(self, capability: str) -> List[AgentCard]:
        """Find agents that have a specific capability."""
        with self._lock:
            return [
                card for card in self._cards.values()
                if capability in card.capabilities
            ]

    def find_by_memory_type(self, memory_type: str) -> List[AgentCard]:
        """Find agents that support a specific memory type."""
        with self._lock:
            return [
                card for card in self._cards.values()
                if memory_type in card.supported_memory_types
            ]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_agents": len(self._cards),
                "agent_names": list(self._cards.keys()),
            }


# ── Factory ───────────────────────────────────────────────────────────────

def create_a2a_adapter() -> Dict[str, Any]:
    """Create the full A2A adapter stack."""
    ctx_mgr = A2AContextManager()
    registry = AgentCardRegistry()
    return {
        "context_manager": ctx_mgr,
        "card_registry": registry,
    }


# ── Self-Test ─────────────────────────────────────────────────────────────

def self_test() -> bool:
    """Comprehensive self-test for the A2A Adapter module."""
    print("=" * 60)
    print("  Trinity A2A Adapter — Self Test")
    print("=" * 60)
    passed = 0
    total = 0

    # ── Test 1: Message + Part creation ──
    total += 1
    print("\n[Test 1] Message + Part creation")
    try:
        msg = Message(
            sender_agent="main",
            recipient_agent="file-agent",
            role=MessageRole.COORDINATOR,
        )
        msg.context_id = "ctx_test_1"
        msg.task_id = "task_001"

        # Add text part
        p1 = msg.add_text_part("Process invoice files in /docs/invoices/")
        assert p1.part_type == PartType.TEXT
        assert len(msg.parts) == 1

        # Add data part
        p2 = msg.add_data_part({"file_count": 5, "format": "pdf"})
        assert p2.part_type == PartType.DATA
        assert len(msg.parts) == 2

        # Add file part
        p3 = msg.add_file_part("/docs/invoices/invoice_001.pdf")
        assert p3.part_type == PartType.FILE

        # Serialize
        d = msg.to_dict()
        assert d["sender"] == "main"
        assert len(d["parts"]) == 3
        assert d["role"] == "coordinator"

        # Get text
        text = msg.get_text_content()
        assert "Process invoice" in text

        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 2: A2AContextManager ──
    total += 1
    print("\n[Test 2] A2AContextManager")
    try:
        mgr = A2AContextManager(context_ttl=99999.0)

        # Create context
        ctx = mgr.create_context(coordinator="main")
        assert ctx.context_id.startswith("a2a_")
        assert not ctx.is_expired()

        # Register tasks
        mgr.register_task(ctx.context_id, "task_001", TaskState.PENDING)
        mgr.register_task(ctx.context_id, "task_002", TaskState.PENDING)
        mgr.register_task(ctx.context_id, "task_003", TaskState.PENDING)

        # Update states
        mgr.update_task_state(ctx.context_id, "task_001", TaskState.RUNNING)
        mgr.update_task_state(ctx.context_id, "task_002", TaskState.COMPLETED)

        # Add message
        msg = Message(
            context_id=ctx.context_id,
            task_id="task_001",
            sender_agent="main",
            recipient_agent="file-agent",
        )
        msg.add_text_part("Start processing")
        mgr.add_message(ctx.context_id, msg)

        # Summary
        summary = mgr.get_context_summary(ctx.context_id)
        assert summary["total_tasks"] == 3
        assert "running" in summary["task_states"]
        assert summary["message_count"] == 1

        print(f"    tasks: {summary['total_tasks']}, states: {summary['task_states']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 3: context expiration ──
    total += 1
    print("\n[Test 3] context expiration")
    try:
        mgr2 = A2AContextManager(context_ttl=0.3)
        ctx2 = mgr2.create_context()
        time.sleep(0.6)
        assert ctx2.is_expired()

        cleaned = mgr2.cleanup_expired()
        assert cleaned >= 1
        assert mgr2.get_context(ctx2.context_id) is None
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 4: AgentCardRegistry ──
    total += 1
    print("\n[Test 4] AgentCardRegistry")
    try:
        registry = AgentCardRegistry()

        # Get by name
        card = registry.get("file-agent")
        assert card is not None
        assert card.display_name == "File Agent"
        assert "file_search" in card.capabilities

        # List all
        all_cards = registry.list_all()
        assert len(all_cards) >= 4

        # Find by capability
        searchers = registry.find_by_capability("deep_search")
        assert len(searchers) >= 1
        assert searchers[0].agent_name == "search-agent"

        # Find by memory type
        fact_agents = registry.find_by_memory_type("fact")
        assert any(a.agent_name == "file-agent" for a in fact_agents)

        print(f"    total agents: {len(all_cards)}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 5: AgentCard serialization ──
    total += 1
    print("\n[Test 5] AgentCard serialization")
    try:
        card = AgentCard(
            agent_name="test-agent",
            display_name="Test Agent",
            description="A test agent",
            capabilities=["test"],
            supported_memory_types=["episodic"],
        )
        d = card.to_dict()
        assert d["agent_name"] == "test-agent"
        assert d["capabilities"] == ["test"]
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 6: Message part limits ──
    total += 1
    print("\n[Test 6] Message part limits")
    try:
        msg = Message()
        for i in range(A2A_MESSAGE_MAX_PARTS):
            msg.add_text_part(f"Part {i}")
        assert len(msg.parts) == A2A_MESSAGE_MAX_PARTS

        # Exceeding max should raise
        try:
            msg.add_text_part("overflow")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass  # Expected

        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 7: Part preview ──
    total += 1
    print("\n[Test 7] Part text_preview")
    try:
        p = Part(part_type=PartType.TEXT,
                 content="This is a test message that is somewhat long")
        preview = p.text_preview(max_len=20)
        assert len(preview) <= 20
        print(f"    preview: '{preview}'")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 8: statistics ──
    total += 1
    print("\n[Test 8] statistics")
    try:
        stats = mgr.statistics()
        assert "total_contexts" in stats
        reg_stats = registry.statistics()
        assert "total_agents" in reg_stats
        print(f"    contexts: {stats['total_contexts']}, agents: {reg_stats['total_agents']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Summary ──
    print("\n" + "=" * 60)
    print(f"  RESULTS: {passed}/{total} passed")
    print("=" * 60)
    return passed == total


if __name__ == "__main__":
    ok = self_test()
    raise SystemExit(0 if ok else 1)
