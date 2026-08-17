"""
Memory Tools — Trinity MCP tools backed by the real engine.

Tools:
  - memory_search       Tri-signal semantic search (supports semantic/graph/exact/hybrid)
  - memory_write        Write memory (CRDT versioned, SHA-256 audited)
  - memory_update       Update memory (conflict-preserving)
  - memory_delete       Soft delete memory (audit chain preserved)
  - audit_query         SHA-256 provenance query
  - trinity_diagnostics Full system diagnostics
  - memory_chronicle    Record event sequences (journal-style)
  - memory_tag_search   Search memories by tags
"""

import functools
import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from trinity.core.client import Trinity
from trinity.agents.aggregator import MemoryAggregator
from trinity.agents.auto_discovery import get_aggregator

logger = logging.getLogger("trinity.mcp.tools")

# Shared Trinity engine instance
_engine: Optional[Trinity] = None

# Shared MemoryAggregator instance (v6.96.0 — dual-write to shared pool)
_aggregator: Optional[MemoryAggregator] = None

# ChatSessionRecorder reference (injected by server.py)
_session_recorder: Any = None


def _get_aggregator() -> MemoryAggregator:
    """Lazy-init the shared MemoryAggregator singleton with persistence."""
    global _aggregator
    if _aggregator is None:
        from trinity.agents.aggregator import MemoryAggregator
        _aggregator = get_aggregator()
        if _aggregator is None:
            _aggregator = MemoryAggregator()
        logger.info("MemoryAggregator 共享池已接入 MCP write 路径（persist=%s）。",
                    _aggregator._persist_path or "disabled")
    return _aggregator


def _get_engine() -> Trinity:
    global _engine
    if _engine is None:
        _engine = Trinity()
    return _engine


def set_session_recorder(recorder: Any) -> None:
    """注入 ChatSessionRecorder 引用供 tools 使用。

    Args:
        recorder: ChatSessionRecorder 实例。
    """
    global _session_recorder
    _session_recorder = recorder
    logger.info("ChatSessionRecorder 引用已注入 tools 模块。")


def get_session_recorder():
    """获取 ChatSessionRecorder 实例。"""
    return _session_recorder


@asynccontextmanager
async def _trace_span(name: str, **attributes: Any):
    """包装 async MCP 工具执行体的遥测 span。

    MCP 工具是 async 函数，不能直接套用同步 @traced 装饰器
    （同步包装器会在协程创建时就关闭 span），因此用 async 上下文管理器。
    """
    from trinity.telemetry import get_tracer

    tracer = get_tracer()
    span = tracer.start_span(name, attributes=attributes or None)
    try:
        yield span
        span.ok()
    except Exception as exc:
        span.error(exc)
        raise
    finally:
        span.finish()
        tracer.end_span(span)


def _traced_tool(name: str):
    """Async 装饰器：把 async MCP 工具执行包进遥测 span（用于其余 6 个工具）。"""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            async with _trace_span(name, tool=fn.__name__):
                return await fn(*args, **kwargs)
        return wrapper
    return deco


def register_memory_tools(mcp: FastMCP) -> None:
    """Register all memory tools with the FastMCP instance."""
    _register_memory_search(mcp)
    _register_memory_write(mcp)
    _register_memory_update(mcp)
    _register_memory_delete(mcp)
    _register_audit_query(mcp)
    _register_trinity_diagnostics(mcp)
    _register_memory_chronicle(mcp)
    _register_memory_tag_search(mcp)
    _register_memory_feedback(mcp)
    logger.info("Registered 9 memory tools (backed by real engine).")


# ---------------------------------------------------------------------------
# Tool: memory_feedback (RL 强化信号, MemRL 对齐)
# ---------------------------------------------------------------------------
def _register_memory_feedback(mcp: FastMCP) -> None:

    @mcp.tool()
    @_traced_tool("mcp.memory_feedback")
    async def memory_feedback(memory_id: str, positive: bool = True) -> dict[str, Any]:
        """RL memory feedback — record user confirmation/correction to update Q-value.

        对标 MemRL (arxiv.org/abs/2601.03192)：检索-使用-反馈闭环的在线更新。
        正反馈（用户确认/任务成功）提升该记忆后续检索排序微调权重；
        负反馈（纠正/任务失败）降低。未注册记忆冷启动注册后记录。

        Args:
            memory_id: Target memory ID (pool or engine side).
            positive: True=confirm/success, False=correct/failure (default True).

        Returns:
            Dict with memory_id, rl, q_value.
        """
        agg = _get_aggregator()
        r = agg.rl_feedback(memory_id, positive=positive)
        return {"memory_id": memory_id, "positive": positive, **r}


# ---------------------------------------------------------------------------
# Tool: memory_search
# ---------------------------------------------------------------------------
def _register_memory_search(mcp: FastMCP) -> None:

    @mcp.tool()
    async def memory_search(
        query: str,
        top_k: int = 5,
        mode: str = "hybrid",
    ) -> list[dict[str, Any]]:
        """Tri-signal semantic memory search.

        Supports four modes:
        - semantic: vector semantic similarity
        - graph:    GoS BFS graph traversal
        - exact:    KV exact match
        - hybrid:   multi-channel RRF fusion (default)

        如果语义搜索结果为空，自动回退到 ChatSessionRecorder.fulltext 搜索。

        Args:
            query:  Search query string.
            top_k:  Number of results (default: 5).
            mode:   Retrieval mode (semantic/graph/exact/hybrid).

        Returns:
            List of matching memory entries with scores.
        """
        async with _trace_span("mcp.memory_search", tool="memory_search", query_len=len(query)):
            engine = _get_engine()
            result = engine.search(query=query, top_k=top_k)
            results = result.get("results", result if isinstance(result, list) else [])

            # 如果结果为空，回退到会话全文搜索
            if not results and _session_recorder is not None:
                logger.info("memory_search 结果为空，回退到 ChatSessionRecorder.fulltext 搜索。")
                fallback = _session_recorder.search(query=query, top_k=top_k)
                if fallback:
                    results = [
                        {
                            "session_id": r["session_id"],
                            "content": r["content"],
                            "role": r["role"],
                            "timestamp": r["timestamp"],
                            "tags": r["tags"],
                            "score": r["score"],
                            "source": "session_recorder",
                        }
                        for r in fallback
                    ]

            return results


# ---------------------------------------------------------------------------
# Tool: memory_write
# ---------------------------------------------------------------------------
def _register_memory_write(mcp: FastMCP) -> None:

    @mcp.tool()
    async def memory_write(
        content: str,
        metadata: Optional[dict[str, Any]] = None,
        category: str = "general",
        tags: Optional[list[str]] = None,
        importance: float = 0.5,
    ) -> dict[str, Any]:
        """Write memory (CRDT versioned, SHA-256 audited).

        Each write generates a unique version_id and SHA-256 content hash,
        recorded in the audit log.

        Args:
            content:    Memory text content.
            metadata:   Additional metadata dict.
            category:   Memory category (default: general).
            tags:       List of tags.
            importance: Importance 0-1 (default: 0.5).

        Returns:
            Dict with memory_id, version_id, sha256_hash, timestamp.
        """
        async with _trace_span("mcp.memory_write", tool="memory_write", content_len=len(content)):
            engine = _get_engine()
            # 异步化：核心写入 + SHA-256 审计同步完成（即时返回），
            # 语义关联 / 实体提取 / 主动推送交由后台线程（消除嵌入引擎冷启动阻塞写入）。
            result = engine.ingest(
                content=content,
                role=metadata.get("role", "user") if metadata else "user",
                importance=importance,
                tags=tags or [],
                category=category,
                metadata=metadata,
                postprocess=False,
            )

            memory_id = result.get("memory_id", "")
            if memory_id:
                import threading
                threading.Thread(
                    target=engine._postprocess_memory,
                    args=(memory_id, content),
                    kwargs={"result": result},
                    daemon=True,
                ).start()
                logger.debug("memory_write 后台加工已调度: memory_id=%s", memory_id)

            # v6.96.0: Dual-write to shared MemoryAggregator
            try:
                agg = _get_aggregator()
                source = metadata.get("source_agent", "mcp-marvis") if metadata else "mcp-marvis"
                agg.ingest(
                    content=content,
                    source=source,
                    importance=importance,
                    tags=tags,
                    category=category,
                    metadata=metadata,
                )
                logger.debug("Dual-write to aggregator OK: source=%s", source)
            except Exception as exc:
                logger.warning("Dual-write to aggregator failed (non-fatal): %s", exc)

            return result


# ---------------------------------------------------------------------------
# Tool: memory_update
# ---------------------------------------------------------------------------
def _register_memory_update(mcp: FastMCP) -> None:

    @mcp.tool()
    @_traced_tool("mcp.memory_update")
    async def memory_update(
        memory_id: str,
        new_content: str,
    ) -> dict[str, Any]:
        """Update memory (conflict-preserving strategy).

        Old version is marked as superseded. Full version chain is retained
        in the audit log for provenance.

        Args:
            memory_id:   Target memory ID.
            new_content: New content text.

        Returns:
            Dict with memory_id, old_version, new_version, sha256_hash.

        Raises:
            ValueError: If memory_id not found.
        """
        # Delegate to the shared engine (Trinity core client, adapter-backed).
        engine = _get_engine()
        return engine.update_memory(memory_id=memory_id, new_content=new_content)


# ---------------------------------------------------------------------------
# Tool: memory_delete
# ---------------------------------------------------------------------------
def _register_memory_delete(mcp: FastMCP) -> None:

    @mcp.tool()
    @_traced_tool("mcp.memory_delete")
    async def memory_delete(memory_id: str) -> dict[str, Any]:
        """Soft-delete memory (audit chain preserved).

        Memory status is marked as 'deleted'. Data and full version chain
        remain queryable via audit_query.

        Args:
            memory_id: Target memory ID.

        Returns:
            Dict with memory_id, deleted_version, timestamp.

        Raises:
            ValueError: If memory_id not found.
        """
        engine = _get_engine()
        deleted = engine.delete_memory(memory_id=memory_id)
        if not deleted:
            raise ValueError(f"Memory not found: {memory_id}")
        return {
            "memory_id": memory_id,
            "deleted": True,
            "deleted_version": f"{memory_id}_del",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Tool: audit_query
# ---------------------------------------------------------------------------
def _register_audit_query(mcp: FastMCP) -> None:

    @mcp.tool()
    @_traced_tool("mcp.audit_query")
    async def audit_query(memory_id: str) -> dict[str, Any]:
        """SHA-256 provenance query.

        Returns the full version chain for a memory entry:
        version -> timestamp -> SHA-256 -> operation type.

        Args:
            memory_id: Target memory ID.

        Returns:
            Dict with memory_id, version_chain, total_versions, current_status.

        Raises:
            ValueError: If memory_id not found.
        """
        engine = _get_engine()
        chain = engine.get_version_chain(memory_id=memory_id)
        if not chain:
            raise ValueError(f"Memory not found: {memory_id}")
        return {
            "memory_id": memory_id,
            "version_chain": chain,
            "total_versions": len(chain),
            "current_status": chain[-1].get("operation", ""),
        }


# ---------------------------------------------------------------------------
# Tool: trinity_diagnostics
# ---------------------------------------------------------------------------
def _register_trinity_diagnostics(mcp: FastMCP) -> None:

    @mcp.tool()
    @_traced_tool("mcp.trinity_diagnostics")
    async def trinity_diagnostics() -> dict[str, Any]:
        """Run full Trinity system diagnostics.

        Returns module states, guardian chain status, storage info,
        and retrieval channel status for all 47 channels.
        """
        engine = _get_engine()
        return engine.diagnostics()


# ---------------------------------------------------------------------------
# Tool: memory_chronicle
# ---------------------------------------------------------------------------
def _register_memory_chronicle(mcp: FastMCP) -> None:

    @mcp.tool()
    @_traced_tool("mcp.memory_chronicle")
    async def memory_chronicle(
        events: list[dict[str, Any]],
        title: str = "",
        session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record a sequence of events (journal/diary-style chronicle).

        记录一系列事件到会话历史中，适合日志式记录。

        Args:
            events:     事件列表。每项含 role, content, 可选 metadata。
            title:      可选的条目标题。
            session_id: 可选的目标会话 ID。不指定则自动创建新会话。

        Returns:
            Dict with session_id, event_count, tags.
        """
        global _session_recorder
        if _session_recorder is None:
            return {
                "error": "ChatSessionRecorder 未初始化",
                "event_count": 0,
            }

        # 如果指定了 title 但没有活跃会话，或需要新会话
        sid = session_id
        if title and not sid:
            # 开始新会话
            sid = _session_recorder.start_session(task=title)
        elif sid is None and _session_recorder.current_session is None:
            sid = _session_recorder.start_session(task=title or "chronicle")

        all_tags: list[str] = []
        for event in events:
            role = event.get("role", "user")
            content = event.get("content", "")
            metadata = event.get("metadata")
            result = _session_recorder.record_turn(
                role=role,
                content=content,
                metadata=metadata,
                session_id=sid,
            )
            all_tags.extend(result.get("tags", []))

        return {
            "session_id": sid or _session_recorder.current_session,
            "event_count": len(events),
            "tags": list(set(all_tags)),
        }


# ---------------------------------------------------------------------------
# Tool: memory_tag_search
# ---------------------------------------------------------------------------
def _register_memory_tag_search(mcp: FastMCP) -> None:

    @mcp.tool()
    @_traced_tool("mcp.memory_tag_search")
    async def memory_tag_search(
        tags: list[str],
        top_k: int = 10,
        session_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """按标签搜索记忆或会话。

        搜索所有包含指定标签的记忆条目。
        如果指定 session_id，则仅在该会话中搜索。

        Args:
            tags:       要搜索的标签列表（OR 逻辑 — 匹配任一标签即可）。
            top_k:      返回结果数量上限。
            session_id: 可选，限定搜索范围到指定会话。

        Returns:
            匹配的记忆条目列表。
        """
        global _session_recorder
        if _session_recorder is None:
            return []

        matches: list[dict[str, Any]] = []
        tag_set = set(t.lower() for t in tags)

        if session_id:
            # 限定到单个会话
            session = _session_recorder.get_session(session_id)
            if not session:
                return []
            for i, turn in enumerate(session.get("turns", [])):
                turn_tags = set(t.lower() for t in turn.get("tags", []))
                if turn_tags & tag_set:  # OR 匹配
                    matches.append({
                        "session_id": session_id,
                        "turn_index": i,
                        "role": turn.get("role", "unknown"),
                        "content": turn.get("content", ""),
                        "timestamp": turn.get("timestamp", 0.0),
                        "tags": turn.get("tags", []),
                        "match_type": "tag_or",
                    })
        else:
            # 扫描所有会话
            all_sessions = _session_recorder.list_all_sessions()
            for sess_summary in all_sessions:
                sess_id = sess_summary["session_id"]
                session = _session_recorder.get_session(sess_id)
                if not session:
                    continue
                for i, turn in enumerate(session.get("turns", [])):
                    turn_tags = set(t.lower() for t in turn.get("tags", []))
                    if turn_tags & tag_set:
                        matches.append({
                            "session_id": sess_id,
                            "turn_index": i,
                            "role": turn.get("role", "unknown"),
                            "content": turn.get("content", ""),
                            "timestamp": turn.get("timestamp", 0.0),
                            "tags": turn.get("tags", []),
                            "match_type": "tag_or",
                        })

        # 按时间戳倒序排列，取 top_k
        matches.sort(key=lambda m: m["timestamp"], reverse=True)
        return matches[:top_k]
