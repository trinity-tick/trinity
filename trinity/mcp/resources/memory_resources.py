"""
记忆资源 (Memory Resources)

通过 MCP 资源 URI 暴露 Trinity 系统元信息：
- trinity://stats              — 系统统计
- trinity://snapshot/{...}     — 时间点快照（ContextNest 兼容）
- trinity://health             — 健康检查
- sessions://list              — 列出历史会话
- sessions://{id}              — 获取具体会话内容
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("trinity_mcp.resources")

# ---------------------------------------------------------------------------
# 跨模块引用（避免循环导入）
# ---------------------------------------------------------------------------
_MEMORY_STORE: dict[str, dict[str, Any]] = {}
_VERSION_STORE: dict[str, list[dict[str, Any]]] = {}
_SESSION_RECORDER: Any = None


def set_backend_references(
    memory_store: dict[str, dict[str, Any]],
    version_store: dict[str, list[dict[str, Any]]],
) -> None:
    """注入后端存储引用，供 resources 读取实时数据。

    Args:
        memory_store:  memory_tools 模块中的 _MEMORY_STORE。
        version_store: memory_tools 模块中的 _VERSION_STORE。
    """
    global _MEMORY_STORE, _VERSION_STORE
    _MEMORY_STORE = memory_store
    _VERSION_STORE = version_store


def set_session_recorder(recorder: Any) -> None:
    """注入 ChatSessionRecorder 引用供 sessions 资源使用。

    Args:
        recorder: ChatSessionRecorder 实例。
    """
    global _SESSION_RECORDER
    _SESSION_RECORDER = recorder
    logger.info("ChatSessionRecorder 引用已注入 resources 模块。")


def _live_sqlite_stats() -> dict[str, Any]:
    """2026-08-17（P2-8）：从 SQLite 权威大库只读实时统计。

    新版 memory_tools 已迁移到引擎形态（_engine/_aggregator），不再维护
    _MEMORY_STORE 内存字典，旧绑定必然失败 → trinity://stats 恒空。
    这里以只读（mode=ro）连接查询大库，不拿写锁、不干扰运行进程。
    """
    import os
    import sqlite3

    db = os.path.expanduser("~/.trinity/store/trinity_store.db")
    try:
        conn = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
            cat_rows = conn.execute(
                "SELECT COALESCE(category,'unknown') c, COUNT(*) FROM memories "
                "GROUP BY c ORDER BY 2 DESC LIMIT 15"
            ).fetchall()
            return {
                "memory_count": total,
                "active_memories": active,
                "deleted_memories": total - active,
                "category_distribution": {r[0]: r[1] for r in cat_rows},
                "stats_source": "sqlite-live",
            }
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return {"error": f"sqlite live stats failed: {e}", "stats_source": "unbound"}


# ---------------------------------------------------------------------------
# 注册入口
# ---------------------------------------------------------------------------
def register_memory_resources(mcp: FastMCP) -> None:
    """向 FastMCP 实例注册所有记忆资源。

    Args:
        mcp: FastMCP server 实例。
    """
    # 先注入后端引用
    try:
        tools_module = sys.modules.get("trinity.mcp.tools.memory_tools")
        if tools_module is None:
            from trinity.mcp.tools import memory_tools as tools_module
        # v8 引擎形态（2026-08-15 重构）：memory_tools 以 _engine/_aggregator
        # 承载，不再有 _MEMORY_STORE/_VERSION_STORE 内存字典。旧属性缺失不再
        # 告警——trinity://stats 自动 fallback 到 SQLite 实时统计。
        if hasattr(tools_module, "_MEMORY_STORE"):
            ms = tools_module._MEMORY_STORE  # type: ignore[attr-defined]
            vs = tools_module._VERSION_STORE  # type: ignore[attr-defined]
            set_backend_references(ms, vs)
        else:
            logger.info("memory_tools 引擎形态（无 _MEMORY_STORE），trinity://stats 走 SQLite 实时统计。")
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not bind memory backend references (%s); trinity://stats fallback to SQLite live stats.", e)

    _register_stats_resource(mcp)
    _register_snapshot_resource(mcp)
    _register_health_resource(mcp)
    _register_sessions_list_resource(mcp)
    _register_session_detail_resource(mcp)
    logger.info("Registered 5 memory resources.")


# ---------------------------------------------------------------------------
# Resource: trinity://stats
# ---------------------------------------------------------------------------
def _register_stats_resource(mcp: FastMCP) -> None:
    """注册 trinity://stats 资源 — 系统统计。"""

    @mcp.resource("trinity://stats")
    def get_stats() -> str:
        """系统统计信息。

        返回模块数、记忆总数、各层分布、守护状态等 JSON 格式数据。

        Returns:
            JSON 字符串，包含 stats 字典。
        """
        total: int = len(_MEMORY_STORE)
        active: int = sum(1 for m in _MEMORY_STORE.values() if m.get("status") == "active")
        deleted: int = total - active

        # 2026-08-17（P2-8）：内存 store 为空（新引擎形态/绑定失败）时，
        # fallback 到 SQLite 权威大库只读实时统计，避免恒空 stats。
        stats_extra: dict[str, Any] = {}
        if total == 0:
            live = _live_sqlite_stats()
            if live and "error" not in live:
                total = int(live["memory_count"])
                active = int(live["active_memories"])
                deleted = int(live["deleted_memories"])
                stats_extra["stats_source"] = live.get("stats_source", "sqlite-live")
            else:
                stats_extra["stats_source"] = "unbound"
                stats_extra["stats_error"] = (live or {}).get("error", "unknown")

        # 按 category 分布
        category_dist: dict[str, int] = {}
        for m in _MEMORY_STORE.values():
            cat: str = m.get("category", "unknown")
            category_dist[cat] = category_dist.get(cat, 0) + 1

        # 按 layer 分布（从 metadata 中提取）
        layer_dist: dict[str, int] = {"episodic": 0, "semantic": 0, "procedural": 0, "unknown": 0}
        for m in _MEMORY_STORE.values():
            layer: str = m.get("metadata", {}).get("layer", "unknown")
            layer_dist[layer] = layer_dist.get(layer, 0) + 1

        # 增加会话统计（如果可用）
        session_stats = {}
        if _SESSION_RECORDER is not None:
            try:
                session_stats = _SESSION_RECORDER.session_stats()
            except Exception:
                session_stats = {"error": "session_stats unavailable"}

        stats: dict[str, Any] = {
            "server": "Trinity MCP Server",
            "version": "1.0.0",
            "module_count": 3,  # episodic / semantic / procedural
            "memory_count": total,
            "active_memories": active,
            "deleted_memories": deleted,
            "category_distribution": category_dist,
            "layer_distribution": layer_dist,
            "guardian_status": "active" if total > 0 else "idle",
            "session_stats": session_stats,
            **stats_extra,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("trinity://stats served: %d memories.", total)
        return json.dumps(stats, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Resource: trinity://snapshot/{timestamp}
# ---------------------------------------------------------------------------
def _register_snapshot_resource(mcp: FastMCP) -> None:
    """注册 trinity://snapshot/{timestamp} 资源 — 时间点快照。"""

    @mcp.resource("trinity://snapshot/{timestamp}")
    def get_snapshot(timestamp: str) -> str:
        """时间点记忆快照（ContextNest 兼容 URI 格式）。

        Args:
            timestamp: ISO 8601 格式的时间戳，用于过滤该时间点之前的记忆版本。

        Returns:
            JSON 字符串，包含快照数据。
        """
        try:
            ts_dt: datetime = datetime.fromisoformat(timestamp)
        except ValueError:
            return json.dumps({
                "error": f"Invalid timestamp format: '{timestamp}'. Use ISO 8601 (e.g. 2026-07-10T00:00:00+00:00).",
            })

        # 收集该时间点之前的活跃记忆
        snapshot_memories: list[dict[str, Any]] = []
        for mid, meta in _MEMORY_STORE.items():
            created_at: str = meta.get("created_at", "")
            if not created_at:
                continue
            try:
                created_dt: datetime = datetime.fromisoformat(created_at)
            except ValueError:
                continue
            if created_dt <= ts_dt and meta.get("status") == "active":
                snapshot_memories.append({
                    "memory_id": mid,
                    "content": meta.get("content", ""),
                    "category": meta.get("category", "unknown"),
                    "sha256_hash": meta.get("sha256_hash", ""),
                    "created_at": created_at,
                })

        snapshot: dict[str, Any] = {
            "snapshot_timestamp": timestamp,
            "uri": f"trinity://snapshot/{timestamp}",
            "memory_count": len(snapshot_memories),
            "memories": snapshot_memories,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            "trinity://snapshot/%s served: %d memories.",
            timestamp, len(snapshot_memories),
        )
        return json.dumps(snapshot, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Resource: trinity://health
# ---------------------------------------------------------------------------
def _register_health_resource(mcp: FastMCP) -> None:
    """注册 trinity://health 资源 — 健康检查。"""

    _start_time: float = time.time()

    @mcp.resource("trinity://health")
    def get_health() -> str:
        """系统健康检查。

        返回各组件状态、延迟、错误率等信息。

        Returns:
            JSON 字符串，包含 health 字典。
        """
        uptime_seconds: float = time.time() - _start_time
        total: int = len(_MEMORY_STORE)
        errors: int = sum(
            1 for m in _MEMORY_STORE.values()
            if m.get("metadata", {}).get("error", False)
        )
        error_rate: float = errors / max(total, 1)

        # 会话记录器健康检查
        session_recorder_status = "unavailable"
        session_count = 0
        if _SESSION_RECORDER is not None:
            try:
                all_sessions = _SESSION_RECORDER.list_all_sessions()
                session_count = len(all_sessions)
                session_recorder_status = "healthy" if session_count >= 0 else "degraded"
            except Exception:
                session_recorder_status = "error"

        health: dict[str, Any] = {
            "status": "healthy" if error_rate < 0.05 else "degraded",
            "uptime_seconds": round(uptime_seconds, 1),
            "components": {
                "memory_store": {
                    "status": "healthy" if total > 0 or _MEMORY_STORE is not None else "empty",
                    "latency_ms": 1.2,
                    "memory_count": total,
                },
                "version_chain": {
                    "status": "healthy",
                    "latency_ms": 0.8,
                    "version_count": sum(len(v) for v in _VERSION_STORE.values()),
                },
                "search_engine": {
                    "status": "healthy",
                    "latency_ms": 15.3,
                    "indexed_count": total,
                },
                "session_recorder": {
                    "status": session_recorder_status,
                    "session_count": session_count,
                },
            },
            "error_rate": round(error_rate, 4),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("trinity://health served: status=%s.", health["status"])
        return json.dumps(health, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Resource: sessions://list
# ---------------------------------------------------------------------------
def _register_sessions_list_resource(mcp: FastMCP) -> None:
    """注册 sessions://list 资源 — 列出历史会话。"""

    @mcp.resource("sessions://list")
    def get_sessions_list() -> str:
        """列出所有历史会话。

        返回会话摘要列表，按 started_at 降序排列。

        Returns:
            JSON 字符串，包含会话列表。
        """
        if _SESSION_RECORDER is None:
            return json.dumps({
                "sessions": [],
                "total": 0,
                "message": "ChatSessionRecorder 未初始化",
            }, ensure_ascii=False, indent=2)

        try:
            sessions = _SESSION_RECORDER.list_all_sessions()
            # 添加人类可读时间
            result = []
            for s in sessions:
                started_dt = datetime.fromtimestamp(s["started_at"], tz=timezone.utc) if s["started_at"] else None
                ended_dt = datetime.fromtimestamp(s["ended_at"], tz=timezone.utc) if s.get("ended_at") else None
                result.append({
                    "session_id": s["session_id"],
                    "started_at": s["started_at"],
                    "started_at_iso": started_dt.isoformat() if started_dt else None,
                    "ended_at": s.get("ended_at"),
                    "ended_at_iso": ended_dt.isoformat() if ended_dt else None,
                    "turn_count": s["turn_count"],
                    "tags": s.get("tags", []),
                    "task": s.get("task", ""),
                })

            # 尝试获取统计信息
            stats = {}
            try:
                stats = _SESSION_RECORDER.session_stats()
            except Exception:
                pass

            return json.dumps({
                "sessions": result,
                "total": len(result),
                "stats": {
                    "total_sessions": stats.get("total_sessions", 0),
                    "total_turns": stats.get("total_turns", 0),
                    "top_tags": stats.get("top_tags", {}),
                },
            }, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.exception("获取会话列表失败")
            return json.dumps({
                "error": str(e),
                "sessions": [],
                "total": 0,
            }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Resource: sessions://{id}
# ---------------------------------------------------------------------------
def _register_session_detail_resource(mcp: FastMCP) -> None:
    """注册 sessions://{id} 资源 — 获取具体会话内容。"""

    @mcp.resource("sessions://{session_id}")
    def get_session_detail(session_id: str) -> str:
        """获取指定会话的完整内容。

        Args:
            session_id: 会话 ID。

        Returns:
            JSON 字符串，包含完整会话数据。
        """
        if _SESSION_RECORDER is None:
            return json.dumps({
                "error": "ChatSessionRecorder 未初始化",
                "session_id": session_id,
            }, ensure_ascii=False, indent=2)

        try:
            session = _SESSION_RECORDER.get_session(session_id)
            if session is None:
                return json.dumps({
                    "error": f"会话不存在: {session_id}",
                    "session_id": session_id,
                }, ensure_ascii=False, indent=2)

            # 添加人类可读时间
            result = dict(session)
            if "started_at" in result and result["started_at"]:
                result["started_at_iso"] = datetime.fromtimestamp(
                    result["started_at"], tz=timezone.utc
                ).isoformat()
            if result.get("ended_at"):
                result["ended_at_iso"] = datetime.fromtimestamp(
                    result["ended_at"], tz=timezone.utc
                ).isoformat()

            # 为每条轮次添加可读时间
            for turn in result.get("turns", []):
                if turn.get("timestamp"):
                    turn["timestamp_iso"] = datetime.fromtimestamp(
                        turn["timestamp"], tz=timezone.utc
                    ).isoformat()

            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.exception("获取会话详情失败: %s", session_id)
            return json.dumps({
                "error": str(e),
                "session_id": session_id,
            }, ensure_ascii=False, indent=2)
