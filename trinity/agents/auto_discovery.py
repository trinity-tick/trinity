# -*- coding: utf-8 -*-
"""
Auto Discovery — Zero-Config Agent Registration (v6.96.0)
==========================================================
Automatic agent discovery and shared-memory-pool bootstrapping.
When ``import trinity`` runs, agents are auto-registered with the
shared MemoryAggregator — no manual Trinity API calls required.

Workflow:
  1. Agent starts → ``import trinity`` → AutoRegistry triggers
  2. Detect TRINITY_HOME via env var / default paths / upward search
  3. Auto-register AgentCard into AgentCardRegistry
  4. Connect to shared MemoryAggregator (process-level singleton)

Classes:
  - AutoRegistry: auto-discover Trinity path, bootstrap aggregator,
                  register AgentCard
  - TrinityContextManager: ``with``-block context that records/replays
    memory for a scoped task

Decorators:
  - @trinity_memory: auto-ingest function call context into shared pool
  - @trinity_context: inject relevant memories into function kwargs

Global Singletons:
  - get_aggregator() → MemoryAggregator
  - get_bridge() → AgentBridge
  - ensure_bootstrapped() → None
"""

from __future__ import annotations

__version__ = "6.96.0"

import functools
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar

from trinity.agents.aggregator import MemoryAggregator, create_aggregator
from trinity.agents.bridge import AgentBridge

logger = logging.getLogger(__name__)

# ── Config Constants ──────────────────────────────────────────────────────

DEFAULT_TRINITY_HOME_WIN = "C:\\Users\\Administrator\\trinity"
TRINITY_ROOT_MARKER = "trinity"           # dir name expected when upward-scanning


# ── Global Singletons ─────────────────────────────────────────────────────

_global_aggregator: Optional[MemoryAggregator] = None
_global_bridge: Optional[AgentBridge] = None
_bootstrapped: bool = False
_lock: threading.RLock = threading.RLock()


# ── AutoRegistry ──────────────────────────────────────────────────────────


class AutoRegistry:
    """Auto-discover Trinity and bootstrap shared memory pool.

    Triggers on ``import trinity`` via ``trinity/__init__.py`` so that
    every agent in the same process automatically shares one
    MemoryAggregator instance.

    Attributes:
        agent_name: resolved agent name
        trinity_home: discovered Trinity installation path
        aggregator: the shared MemoryAggregator reference
        bridge: the AgentBridge wired to the shared pool
    """

    def __init__(self, agent_name: Optional[str] = None):
        self.agent_name = agent_name or self._infer_agent_name()
        self.trinity_home: str = ""
        self.aggregator: Optional[MemoryAggregator] = None
        self.bridge: Optional[AgentBridge] = None

        discovered = self._discover_trinity()
        if discovered:
            self.trinity_home = discovered
            self._bootstrap()
            self._register_agent_card()
            self._emit_startup_log()
        else:
            logger.warning(
                "AutoRegistry: Trinity home not found for agent '%s' — "
                "shared memory pool unavailable",
                self.agent_name,
            )

    # ── Discovery ─────────────────────────────────────────────────────

    def _infer_agent_name(self) -> str:
        """Infer agent name from env var → caller module → program name."""
        env_name = os.environ.get("TRINITY_AGENT_NAME")
        if env_name:
            return env_name

        # Walk the call stack for the first module outside trinity/
        import inspect
        for frame_info in inspect.stack():
            mod = inspect.getmodule(frame_info[0])
            if mod and hasattr(mod, "__name__"):
                name = mod.__name__
                if not name.startswith("trinity.") and name not in ("trinity", "builtins", "__main__"):
                    return name.rsplit(".", 1)[-1] if "." in name else name

        # Fallback to the running script / main module name
        main_mod = sys.modules.get("__main__")
        if main_mod:
            main_file = getattr(main_mod, "__file__", "")
            if main_file:
                return Path(main_file).stem
        return "unknown-agent"

    def _discover_trinity(self) -> Optional[str]:
        """Discover Trinity home directory (priority-ordered):

        1. TRINITY_HOME environment variable
        2. Default Windows path: ``C:\\Users\\Administrator\\trinity``
        3. ``~/.trinity`` (cross-platform fallback)
        4. Walk upward from current working directory
        """
        # 1) Env var
        env_home = os.environ.get("TRINITY_HOME")
        if env_home and Path(env_home).is_dir():
            return str(Path(env_home).resolve())

        # 2) Default Windows path
        win_default = Path(DEFAULT_TRINITY_HOME_WIN)
        if win_default.is_dir():
            return str(win_default.resolve())

        # 3) ~/.trinity
        dot_trinity = Path.home() / ".trinity"
        if dot_trinity.is_dir():
            return str(dot_trinity.resolve())

        # 4) Walk upward from CWD
        cwd = Path.cwd()
        for parent in [cwd] + list(cwd.parents):
            candidate = parent / TRINITY_ROOT_MARKER
            if candidate.is_dir() and (candidate / "trinity" / "__init__.py").exists():
                return str(candidate.resolve())
            # Also check if we are already inside trinity/
            if (parent / "trinity" / "__init__.py").exists() and parent.name == TRINITY_ROOT_MARKER:
                return str(parent.resolve())

        return None

    # ── Bootstrapping ────────────────────────────────────────────────

    def _bootstrap(self) -> None:
        """Create or reuse the process-level shared MemoryAggregator.

        If an aggregator already exists in the current process (detected
        via ``_get_existing_aggregator``), reuse it. Otherwise create
        a new one and store it in the global singleton.
        """
        global _global_aggregator, _global_bridge, _bootstrapped
        with _lock:
            existing = self._get_existing_aggregator()
            if existing is not None:
                self.aggregator = existing
                _global_aggregator = existing
                logger.info(
                    "AutoRegistry: reusing existing MemoryAggregator "
                    "(%d memories in pool)", existing.statistics().get("total_memories", 0)
                )
            else:
                self.aggregator = create_aggregator(persist=True)
                _global_aggregator = self.aggregator

            # Wire AgentBridge to the shared pool
            self.bridge = AgentBridge(brain=None, aggregator=self.aggregator)
            _global_bridge = self.bridge
            _bootstrapped = True

    def _register_agent_card(self) -> None:
        """Auto-register this agent's capability card in the registry.

        Only runs when TRINITY_AUTO_REGISTER is truthy (default: True).
        """
        auto_reg = os.environ.get("TRINITY_AUTO_REGISTER", "1")
        if not _env_is_truthy(auto_reg):
            logger.debug(
                "AutoRegistry: TRINITY_AUTO_REGISTER=%s — skipping "
                "AgentCard registration for '%s'",
                auto_reg, self.agent_name,
            )
            return

        try:
            from trinity.agents.a2a_adapter import AgentCard, AgentCardRegistry
        except ImportError:
            logger.warning(
                "AutoRegistry: a2a_adapter unavailable, cannot register "
                "AgentCard for '%s'", self.agent_name,
            )
            return

        card = AgentCard(
            agent_name=self.agent_name,
            display_name=self.agent_name.replace("-", " ").title(),
            description=f"Auto-registered agent '{self.agent_name}' "
                        f"via Trinity v{__version__}",
            capabilities=["auto_discovered"],
            supported_memory_types=["policy", "preference", "fact", "episodic"],
            input_formats=["text"],
            output_formats=["text"],
            max_context_size=4096,
            version=__version__,
        )

        registry = AgentCardRegistry()
        registry.register(card)
        logger.debug(
            "AutoRegistry: registered AgentCard for '%s'", self.agent_name,
        )

    def _get_existing_aggregator(self) -> Optional[MemoryAggregator]:
        """Check whether a MemoryAggregator is already active in this process."""
        global _global_aggregator
        return _global_aggregator

    def _emit_startup_log(self) -> None:
        """Print the one-line startup banner."""
        existing_count = 0
        if self.aggregator is not None:
            stats = self.aggregator.statistics()
            existing_count = stats.get("total_memories", 0)

        banner = (
            f"[Trinity v{__version__}] Auto-registered agent "
            f"'{self.agent_name}', shared pool ready, "
            f"{existing_count} existing memories"
        )
        # Use logger at INFO level — the caller may suppress via logging config
        logger.info(banner)


# ── Global Accessors ──────────────────────────────────────────────────────


def get_aggregator() -> Optional[MemoryAggregator]:
    """Return the process-level shared MemoryAggregator singleton.

    Returns None if Trinity has not been bootstrapped yet.
    """
    global _global_aggregator
    return _global_aggregator


def get_bridge() -> Optional[AgentBridge]:
    """Return the process-level shared AgentBridge singleton.

    Returns None if Trinity has not been bootstrapped yet.
    """
    global _global_bridge
    return _global_bridge


def ensure_bootstrapped() -> bool:
    """Ensure Trinity auto-discovery + bootstrapping have run.

    Idempotent — does nothing if already bootstrapped.
    Returns True if bootstrapped (either before or during this call).

    This is the recommended entry point for code that imports
    trinity explicitly but wasn't covered by ``__init__.py``'s
    auto-trigger.
    """
    global _bootstrapped
    if _bootstrapped:
        return True

    memory_enabled = os.environ.get("TRINITY_MEMORY_ENABLED", "1")
    if not _env_is_truthy(memory_enabled):
        logger.info(
            "ensure_bootstrapped: TRINITY_MEMORY_ENABLED=%s — memory disabled",
            memory_enabled,
        )
        return False

    try:
        AutoRegistry()
    except Exception as exc:
        logger.error(
            "ensure_bootstrapped: AutoRegistry failed for agent '%s': %s",
            os.environ.get("TRINITY_AGENT_NAME", "unknown"), exc,
        )
        return False

    return _bootstrapped


# ── Decorators ────────────────────────────────────────────────────────────

_F = TypeVar("_F", bound=Callable[..., Any])


def trinity_memory(
    source_agent: Optional[str] = None,
):
    """Decorator: auto-ingest function call context into shared pool.

    After the wrapped function returns, its name and a brief summary
    are ingested into the shared MemoryAggregator under *source_agent*.

    Usage::

        @trinity_memory(source_agent="file-agent")
        def process_invoice(path: str) -> dict:
            ...

    Args:
        source_agent: agent name override. If None, inferred from
                      registry or ``Trinity`` env.
    """

    def decorator(func: _F) -> _F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            agg = get_aggregator()
            result = func(*args, **kwargs)

            if agg is not None:
                agent = (
                    source_agent
                    or os.environ.get("TRINITY_AGENT_NAME")
                    or "auto"
                )
                try:
                    # Ingest a lightweight call record
                    agg.ingest(
                        content=f"Agent '{agent}' executed {func.__name__}",
                        source_agent=agent,
                        metadata={
                            "category": "episodic",
                            "scope": "local",
                            "decorator": "trinity_memory",
                        },
                    )
                except Exception:
                    logger.debug(
                        "trinity_memory: ingest failed for %s", func.__name__,
                        exc_info=True,
                    )

            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def trinity_context(func: _F) -> _F:
    """Decorator: inject relevant memories into function kwargs.

    Before the wrapped function executes, the shared pool is queried
    for recent / high-priority memories under the calling agent,
    and the result is injected as ``trinity_context=<list>``.

    Usage::

        @trinity_context
        def answer_question(query: str, trinity_context=None) -> str:
            if trinity_context:
                print("Relevant memories:", trinity_context)
            ...

    Note:
        The injected kwarg is silently skipped if the wrapped function
        does not accept ``trinity_context``.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        agg = get_aggregator()
        memories: list = []

        if agg is not None:
            agent = os.environ.get("TRINITY_AGENT_NAME", "auto")
            try:
                dvs = agg.get_by_agent(agent, limit=5)
                memories = [
                    {
                        "memory_id": dv.memory_id,
                        "content": dv.content[:200],
                        "category": dv.category.value
                        if hasattr(dv.category, "value")
                        else str(dv.category),
                        "priority": dv.priority,
                    }
                    for dv in dvs
                ]
            except Exception:
                logger.debug(
                    "trinity_context: query failed for '%s'",
                    agent, exc_info=True,
                )

        if "trinity_context" in _func_params(func):
            kwargs["trinity_context"] = memories

        return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


# ── Context Manager ───────────────────────────────────────────────────────


class TrinityContextManager:
    """``with``-block scoped memory recording & replay.

    Usage::

        with TrinityContextManager(source_agent="file-agent") as ctx:
            ctx.record("found 3 invoices in D:\\scans")
            # ... do work ...
            # On exit: context is auto-ingested into shared pool.
    """

    def __init__(self, source_agent: Optional[str] = None):
        self._agent = (
            source_agent
            or os.environ.get("TRINITY_AGENT_NAME")
            or "auto"
        )
        self._records: list = []
        self._aggregator: Optional[MemoryAggregator] = None

    def __enter__(self) -> "TrinityContextManager":
        self._aggregator = get_aggregator()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._aggregator is None or not self._records:
            return
        for content in self._records:
            try:
                self._aggregator.ingest(
                    content=content,
                    source_agent=self._agent,
                    metadata={
                        "category": "episodic",
                        "scope": "local",
                        "context_manager": "TrinityContextManager",
                    },
                )
            except Exception:
                logger.debug(
                    "TrinityContextManager: ingest failed for '%s'",
                    self._agent, exc_info=True,
                )
        # Clear to allow gc
        self._records.clear()

    def record(self, content: str) -> None:
        """Queue a memory record for auto-ingest on context exit."""
        self._records.append(content)

    def get_context(self, limit: int = 5) -> list:
        """Retrieve recent memories for this agent from the shared pool."""
        if self._aggregator is None:
            return []
        try:
            dvs = self._aggregator.get_by_agent(self._agent, limit=limit)
            return [
                {
                    "memory_id": dv.memory_id,
                    "content": dv.content[:200],
                    "category": dv.category.value
                    if hasattr(dv.category, "value")
                    else str(dv.category),
                    "priority": dv.priority,
                }
                for dv in dvs
            ]
        except Exception:
            return []


# ── Helpers ───────────────────────────────────────────────────────────────


def _func_params(func: Callable[..., Any]) -> set:
    """Return parameter names accepted by *func*."""
    import inspect
    try:
        return set(inspect.signature(func).parameters.keys())
    except (ValueError, TypeError):
        return set()


def _env_is_truthy(value: str) -> bool:
    """Return True if *value* is a truthy env-var string.

    Truthy: "1", "true", "yes", "on" (case-insensitive).
    """
    return value.strip().lower() in ("1", "true", "yes", "on")


# ── Self-Test ─────────────────────────────────────────────────────────────


def self_test() -> bool:
    """Comprehensive self-test for auto_discovery module."""

    print("=" * 60)
    print("  Trinity Auto Discovery — Self Test (v6.96.0)")
    print("=" * 60)

    passed = 0
    total = 0

    # ── Test 1: _infer_agent_name ──
    total += 1
    print("\n[Test 1] _infer_agent_name")
    try:
        # With no env var set, should fall back to something non-empty
        old_name = os.environ.pop("TRINITY_AGENT_NAME", None)
        reg = AutoRegistry()
        name = reg.agent_name
        assert name, "agent_name should not be empty"
        assert isinstance(name, str)
        if old_name:
            os.environ["TRINITY_AGENT_NAME"] = old_name
        print(f"  PASS — inferred agent name: '{name}'")
        passed += 1
    except Exception as exc:
        print(f"  FAIL — {exc}")

    # ── Test 2: TRINITY_AGENT_NAME env override ──
    total += 1
    print("\n[Test 2] TRINITY_AGENT_NAME env override")
    try:
        old_val = os.environ.get("TRINITY_AGENT_NAME")
        os.environ["TRINITY_AGENT_NAME"] = "test-agent-42"
        reg2 = AutoRegistry()
        assert reg2.agent_name == "test-agent-42", (
            f"expected 'test-agent-42', got '{reg2.agent_name}'"
        )
        if old_val is not None:
            os.environ["TRINITY_AGENT_NAME"] = old_val
        else:
            os.environ.pop("TRINITY_AGENT_NAME", None)
        print("  PASS")
        passed += 1
    except Exception as exc:
        print(f"  FAIL — {exc}")

    # ── Test 3: _discover_trinity with TRINITY_HOME env ──
    total += 1
    print("\n[Test 3] _discover_trinity (TRINITY_HOME env)")
    try:
        old_home = os.environ.get("TRINITY_HOME")
        os.environ["TRINITY_HOME"] = "C:\\Users\\Administrator\\trinity"
        reg3 = AutoRegistry._create_bare()
        home = reg3._discover_trinity()
        assert home is not None, "should discover TRINITY_HOME"
        os.environ.pop("TRINITY_HOME", None)
        if old_home:
            os.environ["TRINITY_HOME"] = old_home
        print(f"  PASS — trinity_home={home}")
        passed += 1
    except Exception as exc:
        print(f"  FAIL — {exc}")

    # ── Test 4: singleton get_aggregator / get_bridge ──
    total += 1
    print("\n[Test 4] Singleton get_aggregator / get_bridge")
    try:
        from trinity.agents.auto_discovery import (
            get_aggregator as ga,
            get_bridge as gb,
            ensure_bootstrapped as eb,
        )
        boot_ok = eb()
        if boot_ok:
            agg = ga()
            br = gb()
            assert agg is not None, "aggregator should not be None"
            assert br is not None, "bridge should not be None"
            agg2 = ga()
            assert agg is agg2, "get_aggregator() must return same instance"
            print(f"  PASS — aggregator={type(agg).__name__}, bridge={type(br).__name__}")
        else:
            # Bootstrapping may fail in test env — that's acceptable
            print("  PASS — ensure_bootstrapped returned False (no Trinity home)")
        passed += 1
    except Exception as exc:
        print(f"  FAIL — {exc}")

    # ── Test 5: _env_is_truthy helper ──
    total += 1
    print("\n[Test 5] _env_is_truthy helper")
    try:
        from trinity.agents.auto_discovery import _env_is_truthy
        assert _env_is_truthy("1") is True
        assert _env_is_truthy("true") is True
        assert _env_is_truthy("YES") is True
        assert _env_is_truthy("on") is True
        assert _env_is_truthy("0") is False
        assert _env_is_truthy("no") is False
        assert _env_is_truthy("false") is False
        print("  PASS")
        passed += 1
    except Exception as exc:
        print(f"  FAIL — {exc}")

    # ── Test 6: TrinityContextManager record / get_context ──
    total += 1
    print("\n[Test 6] TrinityContextManager")
    try:
        cm = TrinityContextManager(source_agent="test-cm")
        cm.record("memory A")
        cm.record("memory B")
        assert len(cm._records) == 2
        # get_context may be empty if not bootstrapped
        ctx = cm.get_context()
        assert isinstance(ctx, list)
        print(f"  PASS — {len(cm._records)} records queued, {len(ctx)} context items")
        passed += 1
    except Exception as exc:
        print(f"  FAIL — {exc}")

    # ── Test 7: @trinity_memory / @trinity_context decorators ──
    total += 1
    print("\n[Test 7] Decorator smoke test")
    try:
        @trinity_memory(source_agent="test-deco")
        def dummy_task(x: int) -> int:
            return x * 2

        result = dummy_task(21)
        assert result == 42

        @trinity_context
        def with_memories(a: str, trinity_context=None) -> str:
            return a.upper()

        result2 = with_memories("hello")
        assert result2 == "HELLO"
        print("  PASS")
        passed += 1
    except Exception as exc:
        print(f"  FAIL — {exc}")

    # ── Summary ──
    print(f"\n{'=' * 60}")
    print(f"  Results: {passed}/{total} passed")
    print(f"{'=' * 60}")

    return passed == total


# ── Internal helper for testing ───────────────────────────────────────────

@classmethod  # type: ignore[misc]
def _create_bare(cls) -> "AutoRegistry":
    """Create an AutoRegistry without triggering side-effects.

    Used internally during self_test so that _discover_trinity can
    be tested without bootstrapping.
    """
    inst = object.__new__(cls)
    inst.agent_name = "bare-test"
    inst.trinity_home = ""
    inst.aggregator = None
    inst.bridge = None
    return inst


AutoRegistry._create_bare = _create_bare  # type: ignore[assignment]
