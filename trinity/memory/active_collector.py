"""
Trinity 主动记忆采集器 (Active Memory Collector) — v8.3.0
==========================================================
从被动「等待 write 调用」升级为主动「事件驱动 + 后台扫描」双通道采集，
覆盖 6 个内置 Agent 的完整生命周期事件流。

三大组件：
  - EventDrivenCollector：6 hook 事件驱动采集器
  - BackgroundScanner：   后台守护扫描器（默认 30s 间隔）
  - AgentConnector：      为 6 个内置 Agent 提供适配连接器

设计对齐：
  - agentmemory 12-hooks 事件驱动捕获模式（MCP 协议层监听）
  - LangChain OpenWiki Brains 主动记忆框架（connector + scheduled refresh）
  - Mem0 多层记忆 + 自动提取 / 整合 / 存储 / 检索闭环
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ── Configuration defaults ────────────────────────────────────────────────

DEFAULT_SCAN_INTERVAL = 30        # BackgroundScanner 默认扫描间隔（秒）
DEFAULT_MAX_EVENTS_PER_FLUSH = 50 # 每次 flush 到 SQLite 的最大事件数
DEFAULT_DEDUP_WINDOW_SECONDS = 300  # 内容去重窗口（秒）
DEFAULT_HOOK_ENABLED = True
DEFAULT_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "trinity_store.db",
)

# 6 个内置 Agent 标识
BUILTIN_AGENTS = [
    "main",
    "file-agent",
    "browser",
    "app-agent",
    "computer-agent",
    "search-agent",
]


# ── Enums ─────────────────────────────────────────────────────────────────

class HookPoint(Enum):
    """6 个核心事件 hook 点。"""
    CONVERSATION_START = "conversation_start"   # 会话开始
    TOOL_CALL = "tool_call"                     # 工具调用
    DECISION_POINT = "decision_point"           # 重要决策
    SESSION_END = "session_end"                 # 会话结束
    CONTEXT_COMPACT = "context_compact"         # 上下文压缩
    ERROR_EVENT = "error_event"                 # 错误事件


class CollectorState(Enum):
    """采集器运行状态。"""
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


# ── Data Classes ──────────────────────────────────────────────────────────

@dataclass
class MemoryPayload:
    """标准化记忆载荷，由 hook 事件生成。"""
    hook_point: HookPoint
    agent_name: str
    content: str
    importance: float = 0.5
    category: str = "episodic"
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_store_dict(self) -> Dict[str, Any]:
        """将 MemoryPayload 转为 store_memory(**kwargs) 可接受的参数字典。

        content_hash 由适配器内部通过 sha256 自动计算，无需在此传入。
        """
        return {
            "content": self.content,
            "agent_id": self.agent_name,
            "persona_id": (self.metadata or {}).get("persona_id", "default"),
            "importance": self.importance,
            "category": self.category,
            "tags": self.tags,
            "metadata": {
                **(self.metadata or {}),
                "hook_point": self.hook_point.value,
                "collector": "active_collector",
                "session_id": self.session_id or f"sess_{uuid.uuid4().hex[:12]}",
            },
            "session_id": (self.session_id or f"sess_{uuid.uuid4().hex[:12]}"),
            "role": "agent",
            "modality": "text",
        }


@dataclass
class CollectorStats:
    """采集器统计信息。"""
    events_captured: int = 0
    events_flushed: int = 0
    events_skipped_dup: int = 0
    events_skipped_low_importance: int = 0
    errors: int = 0
    last_flush_at: Optional[float] = None
    last_scan_at: Optional[float] = None
    scan_cycles: int = 0
    scans_found_new: int = 0
    start_time: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "events_captured": self.events_captured,
            "events_flushed": self.events_flushed,
            "events_skipped_dup": self.events_skipped_dup,
            "events_skipped_low_importance": self.events_skipped_low_importance,
            "errors": self.errors,
            "last_flush_at": self.last_flush_at,
            "last_scan_at": self.last_scan_at,
            "scan_cycles": self.scan_cycles,
            "scans_found_new": self.scans_found_new,
            "uptime_seconds": (
                time.time() - self.start_time if self.start_time else 0
            ),
        }


# ── EventDrivenCollector ──────────────────────────────────────────────────

class EventDrivenCollector:
    """事件驱动采集器。

    在 Agent 对话/工具调用/决策等关键节点自动触发，提取结构化记忆
    并写入 SQLite。6 个核心 hook 点覆盖完整生命周期。

    设计对齐 agentmemory 的 12-hooks 事件驱动捕获模式，
    但对 Trinity 已有 memory/agent 体系做了深度适配。
    """

    def __init__(
        self,
        store_adapter: Any = None,
        importance_threshold: float = 0.15,
        dedup_window: int = DEFAULT_DEDUP_WINDOW_SECONDS,
        enabled_hooks: Optional[Set[HookPoint]] = None,
    ):
        """
        Args:
            store_adapter: SQLiteAdapter 等持久化适配器实例。None 时
                           自动创建默认 SQLiteAdapter。
            importance_threshold: 低于此阈值的事件丢弃不存储。
            dedup_window: 内容去重窗口（秒）。
            enabled_hooks: 启用的 hook 点集合，默认全部启用。
        """
        self._adapter = store_adapter
        self.importance_threshold = importance_threshold
        self.dedup_window = dedup_window
        self.enabled_hooks = enabled_hooks or set(HookPoint)
        self._buffer: List[MemoryPayload] = []
        self._recent_hashes: Set[str] = set()
        self._hash_timestamps: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._stats = CollectorStats()
        self._state = CollectorState.STOPPED
        self._session_ids: Dict[str, str] = {}
        self._flush_timer: Optional[threading.Timer] = None
        logger.info(
            "EventDrivenCollector initialized (threshold=%.2f, dedup=%ds, "
            "hooks=%s)",
            importance_threshold,
            dedup_window,
            [h.value for h in self.enabled_hooks],
        )

    # ── Adapter lazy init ─────────────────────────────────────────────

    def _ensure_adapter(self):
        if self._adapter is not None:
            return
        try:
            from trinity.adapters.sqlite import SQLiteAdapter
            self._adapter = SQLiteAdapter()
            self._adapter.connect()
            logger.info("EventDrivenCollector: auto-created SQLiteAdapter")
        except Exception as e:
            logger.error("EventDrivenCollector: failed to init adapter: %s", e)
            raise

    # ── 6 Hook Points ────────────────────────────────────────────────

    def hook_conversation_start(
        self,
        agent_name: str,
        task_desc: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemoryPayload]:
        """会话开始 hook。记录新会话的启动元数据。"""
        return self._emit(
            HookPoint.CONVERSATION_START,
            agent_name,
            content=f"[Session Start] {task_desc}" if task_desc else "[Session Start]",
            importance=0.25,
            category="episodic",
            tags=["session", "start"],
            metadata={
                **(metadata or {}),
                "task_desc": task_desc,
                "action": "session_start",
            },
        )

    def hook_tool_call(
        self,
        agent_name: str,
        tool_name: str,
        phase: str = "before",
        tool_args: Optional[Dict[str, Any]] = None,
        result_preview: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemoryPayload]:
        """工具调用 hook（前后均可触发）。

        Args:
            phase: "before" 或 "after"，标记调用阶段。
            tool_name: 被调用的工具名。
            tool_args: 工具调用参数（phase=before 时传入）。
            result_preview: 工具返回结果的摘要（phase=after 时传入）。
        """
        if phase == "before":
            content = f"[Tool Call: {tool_name}] Parameters: {self._safe_truncate(str(tool_args or {}))}"
            importance = 0.20
        else:
            content = f"[Tool Result: {tool_name}] {result_preview}" if result_preview else f"[Tool Result: {tool_name}]"
            importance = 0.30 if result_preview else 0.20

        return self._emit(
            HookPoint.TOOL_CALL,
            agent_name,
            content=content,
            importance=importance,
            category="trace",
            tags=["tool", tool_name,
                  "phase:" + phase],
            metadata={
                **(metadata or {}),
                "tool_name": tool_name,
                "phase": phase,
                "tool_args_summary": self._safe_truncate(str(tool_args or {}), 200),
            },
        )

    def hook_decision_point(
        self,
        agent_name: str,
        decision: str,
        reasoning: str = "",
        options_considered: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemoryPayload]:
        """重要决策 hook。记录 Agent 的关键决策及其推理上下文。"""
        content_parts = [f"[Decision: {decision}]"]
        if reasoning:
            content_parts.append(f"Reasoning: {reasoning}")
        if options_considered:
            if isinstance(options_considered, str):
                options_considered = [options_considered]
            content_parts.append(f"Options: {' | '.join(options_considered)}")

        return self._emit(
            HookPoint.DECISION_POINT,
            agent_name,
            content=" ".join(content_parts),
            importance=0.45,
            category="episodic",
            tags=["decision"],
            metadata={
                **(metadata or {}),
                "decision": decision,
                "reasoning": reasoning,
                "options_considered": options_considered or [],
            },
        )

    def hook_session_end(
        self,
        agent_name: str,
        session_summary: str = "",
        task_count: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemoryPayload]:
        """会话结束 hook。记录会话最终状态与摘要。"""
        content = (
            f"[Session End] {session_summary}"
            if session_summary
            else f"[Session End] Completed with {task_count} tasks"
        )
        return self._emit(
            HookPoint.SESSION_END,
            agent_name,
            content=content,
            importance=0.35,
            category="episodic",
            tags=["session", "end"],
            metadata={
                **(metadata or {}),
                "session_summary": session_summary,
                "task_count": task_count,
                "action": "session_end",
            },
        )

    def hook_context_compact(
        self,
        agent_name: str,
        compacted_from: int = 0,
        compacted_to: int = 0,
        summary: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemoryPayload]:
        """上下文压缩 hook。记录上下文窗口的压缩事件。"""
        content = (
            f"[Context Compaction] {compacted_from} tokens → "
            f"{compacted_to} tokens. {summary}"
        )
        return self._emit(
            HookPoint.CONTEXT_COMPACT,
            agent_name,
            content=content,
            importance=0.30,
            category="trace",
            tags=["context", "compaction"],
            metadata={
                **(metadata or {}),
                "compacted_from": compacted_from,
                "compacted_to": compacted_to,
                "compression_ratio": (
                    compacted_to / compacted_from if compacted_from else 0
                ),
            },
        )

    def hook_error_event(
        self,
        agent_name: str,
        error_type: str,
        error_message: str,
        stack_trace: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemoryPayload]:
        """错误事件 hook。记录异常和错误信息。"""
        content = f"[Error: {error_type}] {error_message}"
        if stack_trace:
            content += f" | Stack: {self._safe_truncate(stack_trace, 300)}"

        return self._emit(
            HookPoint.ERROR_EVENT,
            agent_name,
            content=content,
            importance=0.60,
            category="episodic",
            tags=["error", error_type.lower()],
            metadata={
                **(metadata or {}),
                "error_type": error_type,
                "error_message": error_message,
                "stack_trace_truncated": self._safe_truncate(stack_trace, 500),
            },
        )

    # ── 通用发射方法 ─────────────────────────────────────────────────

    def _emit(
        self,
        hook_point: HookPoint,
        agent_name: str,
        content: str,
        importance: float = 0.5,
        category: str = "episodic",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemoryPayload]:
        """通用事件发射：校验 + 去重 + 入缓冲。

        Returns:
            MemoryPayload 如果成功入缓冲；None 如果被过滤/跳过。
        """
        with self._lock:
            self._stats.events_captured += 1

            # 检查 hook 是否启用
            if hook_point not in self.enabled_hooks:
                return None

            # 重要性阈值过滤
            if importance < self.importance_threshold:
                self._stats.events_skipped_low_importance += 1
                return None

            # 内容去重（基于 hash）
            content_hash = self._content_hash(content)
            now = time.time()

            # 清理过期 hash
            self._hash_timestamps = {
                h: t for h, t in self._hash_timestamps.items()
                if now - t < self.dedup_window
            }
            self._recent_hashes = set(self._hash_timestamps.keys())

            if content_hash in self._recent_hashes:
                self._stats.events_skipped_dup += 1
                return None

            self._recent_hashes.add(content_hash)
            self._hash_timestamps[content_hash] = now

            # 获取或创建 session_id
            if agent_name not in self._session_ids:
                self._session_ids[agent_name] = f"sess_{uuid.uuid4().hex[:12]}"

            payload = MemoryPayload(
                hook_point=hook_point,
                agent_name=agent_name,
                content=content,
                importance=importance,
                category=category,
                tags=tags or [],
                metadata=metadata or {},
                session_id=self._session_ids[agent_name],
            )

            self._buffer.append(payload)
            self._maybe_flush()
            return payload

    # ── Buffer 管理 ──────────────────────────────────────────────────

    def _maybe_flush(self):
        """缓冲区达到阈值时自动 flush。"""
        if len(self._buffer) >= DEFAULT_MAX_EVENTS_PER_FLUSH:
            self.flush()

    def flush(self) -> int:
        """将缓冲区中所有事件写入 SQLite。"""
        with self._lock:
            if not self._buffer:
                return 0

            self._ensure_adapter()

            batch = list(self._buffer)
            self._buffer.clear()
            written = 0

            try:
                records = [p.to_store_dict() for p in batch]
                results = self._adapter.ingest_batch(records) if hasattr(
                    self._adapter, "ingest_batch"
                ) else [
                    self._adapter.store_memory(**r) for r in records
                ]
                written = len([r for r in results if r.get("memory_id")])
                self._stats.events_flushed += written
                self._stats.last_flush_at = time.time()
                logger.debug("EventDrivenCollector: flushed %d/%d events",
                             written, len(batch))
            except Exception as e:
                self._stats.errors += 1
                logger.error("EventDrivenCollector: flush failed: %s", e)
                # 失败的事件回写缓冲（上限保护）
                if len(self._buffer) < 200:
                    self._buffer = batch + self._buffer

            return written

    # ── 工具方法 ─────────────────────────────────────────────────────

    @staticmethod
    def _content_hash(content: str) -> str:
        """生成内容 hash（用于去重）。"""
        import hashlib
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_truncate(text: str, max_len: int = 300) -> str:
        """安全截断文本。"""
        if len(text) <= max_len:
            return text
        return text[:max_len] + "..."

    def statistics(self) -> Dict[str, Any]:
        """采集器统计信息。"""
        with self._lock:
            return {
                **self._stats.to_dict(),
                "buffer_size": len(self._buffer),
                "state": self._state.value,
                "enabled_hooks": [h.value for h in self.enabled_hooks],
            }

    def reset_session(self, agent_name: str):
        """重置指定 Agent 的会话 ID。"""
        with self._lock:
            if agent_name in self._session_ids:
                del self._session_ids[agent_name]


# ── BackgroundScanner ─────────────────────────────────────────────────────

class BackgroundScanner:
    """后台守护扫描器。

    定期检查 Agent 上下文缓存目录，增量采集未被主动写入的新记忆。
    对齐 OpenWiki Brains 的 scheduled refresh 模式。

    Attributes:
        scan_interval: 扫描间隔（秒），默认 30。
        cache_dirs: 要扫描的缓存目录列表。
    """

    def __init__(
        self,
        event_collector: EventDrivenCollector,
        scan_interval: float = DEFAULT_SCAN_INTERVAL,
        cache_dirs: Optional[List[str]] = None,
    ):
        """
        Args:
            event_collector: 关联的 EventDrivenCollector 实例。
            scan_interval: 扫描间隔（秒）。
            cache_dirs: 要监控的 Agent 缓存目录列表。
        """
        self.event_collector = event_collector
        self.scan_interval = scan_interval
        self.cache_dirs = cache_dirs or self._default_cache_dirs()
        self._seen_files: Set[str] = set()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state = CollectorState.STOPPED
        self._stats = CollectorStats()
        logger.info(
            "BackgroundScanner initialized (interval=%.1fs, dirs=%d)",
            scan_interval,
            len(self.cache_dirs),
        )

    @staticmethod
    def _default_cache_dirs() -> List[str]:
        """获取默认的 Agent 缓存目录列表。

        从 agent_config.yaml 的 active_collection.listen_agents 读取被监听
        的 Agent 列表，为每个 Agent 构造其上下文缓存目录
        （TRINITY_HOME/data/cache/{agent_name}/），并确保目录存在。
        """
        import yaml
        dirs = []

        # TRINITY_HOME = active_collector.py 向上两级目录
        # trinity/memory/active_collector.py → trinity/
        trinity_home = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..")
        )
        data_cache_base = os.path.join(trinity_home, "data", "cache")

        # 从 agent_config.yaml 读取 listen_agents
        try:
            config_path = os.path.join(
                trinity_home, "agents", "agent_config.yaml"
            )
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            listen_agents = (
                config.get("active_collection", {}).get("listen_agents", [])
                or BUILTIN_AGENTS
            )
        except Exception as e:
            logger.warning(
                "BackgroundScanner: failed to read agent_config.yaml, "
                "falling back to BUILTIN_AGENTS: %s", e
            )
            listen_agents = BUILTIN_AGENTS

        for agent_name in listen_agents:
            agent_cache = os.path.join(data_cache_base, agent_name)
            os.makedirs(agent_cache, exist_ok=True)
            dirs.append(agent_cache)
            logger.debug(
                "BackgroundScanner: cache dir for %s → %s",
                agent_name, agent_cache,
            )

        logger.info(
            "BackgroundScanner: initialized %d cache dirs under %s",
            len(dirs), data_cache_base,
        )
        return dirs

    def start(self):
        """启动后台扫描线程。"""
        if self._thread and self._thread.is_alive():
            logger.warning("BackgroundScanner: already running")
            return

        self._stop_event.clear()
        self._state = CollectorState.RUNNING
        self._stats.start_time = time.time()
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="trinity-bg-scanner"
        )
        self._thread.start()
        logger.info("BackgroundScanner: started (interval=%.1fs)", self.scan_interval)

    def stop(self):
        """停止后台扫描线程。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10.0)
            self._state = CollectorState.STOPPED
        self.event_collector.flush()
        logger.info("BackgroundScanner: stopped")

    def _scan_loop(self):
        """后台扫描主循环。"""
        while not self._stop_event.is_set():
            try:
                self._scan_once()
            except Exception as e:
                self._stats.errors += 1
                logger.error("BackgroundScanner: scan error: %s", e)
            self._stop_event.wait(self.scan_interval)

    def _scan_once(self):
        """执行一次扫描。"""
        with self._lock:
            self._stats.scan_cycles += 1
            new_files = 0

            for cache_dir in self.cache_dirs:
                if not os.path.isdir(cache_dir):
                    continue
                try:
                    for root, _, files in os.walk(cache_dir):
                        # 限制扫描深度，避免性能问题
                        relative = os.path.relpath(root, cache_dir)
                        if relative.count(os.sep) > 3:
                            continue

                        for fname in files:
                            fpath = os.path.join(root, fname)
                            if fpath in self._seen_files:
                                continue

                            self._seen_files.add(fpath)
                            new_files += 1

                            # 根据文件后缀确定 Agent 归属
                            agent_name = self._infer_agent(fpath)
                            payload = self.event_collector._emit(
                                HookPoint.CONTEXT_COMPACT,
                                agent_name,
                                content=f"[Background Scan] New file detected: "
                                        f"{self._safe_relpath(fpath)}",
                                importance=0.18,
                                category="trace",
                                tags=["background_scan", "new_file"],
                                metadata={
                                    "file_path": fpath,
                                    "file_name": fname,
                                    "scan_cycle": self._stats.scan_cycles,
                                },
                            )
                            if payload:
                                self._stats.scans_found_new += 1
                except PermissionError:
                    continue
                except Exception as e:
                    logger.debug("BackgroundScanner: skip dir %s: %s", cache_dir, e)

            self._stats.last_scan_at = time.time()
            if new_files > 0:
                logger.debug("BackgroundScanner: found %d new files", new_files)

    @staticmethod
    def _infer_agent(file_path: str) -> str:
        """根据文件路径推断所属 Agent。"""
        lower = file_path.lower()
        for agent in BUILTIN_AGENTS:
            if agent.lower() in lower:
                return agent
        return "main"

    @staticmethod
    def _safe_relpath(path: str, max_len: int = 200) -> str:
        try:
            home = os.path.expanduser("~")
            return path.replace(home, "~")
        except Exception:
            return path[-max_len:]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                **self._stats.to_dict(),
                "state": self._state.value,
                "seen_files_count": len(self._seen_files),
                "cache_dirs": self.cache_dirs,
            }


# ── AgentConnector ────────────────────────────────────────────────────────

class AgentConnector:
    """Agent 适配连接器。

    为每个内置 Agent 提供独立的事件流桥接，连接 Agent 生命周期事件
    （对话开始/结束、工具调用、决策等）到 EventDrivenCollector 的
    对应 hook 点。

    设计对齐 OpenWiki Brains 的 connector 模式：
      - 每个 connector 封装一类数据源（Agent）的采集逻辑
      - 支持 agent-specific 的重要性阈值和分类策略
      - 统一通过 EventDrivenCollector._emit 写入缓冲
    """

    def __init__(
        self,
        event_collector: Optional[EventDrivenCollector] = None,
        agent_name: str = "main",
        agent_config: Optional[Dict[str, Any]] = None,
    ):
        """
        Args:
            event_collector: 共享的 EventDrivenCollector 实例。
            agent_name: 目标 Agent 名称。
            agent_config: Agent 特定配置（从 agent_config.yaml 读取）。
        """
        self.agent_name = agent_name
        self._collector = event_collector or EventDrivenCollector()
        self._config = agent_config or {}
        self._bridge_ref: Any = None  # AgentBridge 引用，bind 时设置
        self._session_id: Optional[str] = None

    def bind_bridge(self, bridge):
        """绑定到 AgentBridge 实例。"""
        self._bridge_ref = bridge

    def on_conversation_start(
        self, task_desc: str = "", metadata: Optional[Dict[str, Any]] = None
    ):
        """Agent 开始接收任务时调用。"""
        self._session_id = f"sess_{uuid.uuid4().hex[:12]}"
        return self._collector.hook_conversation_start(
            self.agent_name, task_desc, metadata
        )

    def on_tool_call_before(
        self, tool_name: str, tool_args: Optional[Dict[str, Any]] = None
    ):
        """工具调用前。"""
        return self._collector.hook_tool_call(
            self.agent_name, tool_name, phase="before", tool_args=tool_args
        )

    def on_tool_call_after(
        self, tool_name: str, result_preview: str = ""
    ):
        """工具调用后。"""
        return self._collector.hook_tool_call(
            self.agent_name, tool_name, phase="after", result_preview=result_preview
        )

    def on_decision(
        self,
        decision: str,
        reasoning: str = "",
        options: Optional[List[str]] = None,
    ):
        """做出重要决策时调用。"""
        return self._collector.hook_decision_point(
            self.agent_name, decision, reasoning, options
        )

    def on_session_end(
        self,
        summary: str = "",
        task_count: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """会话结束时调用。"""
        result = self._collector.hook_session_end(
            self.agent_name, summary, task_count, metadata
        )
        self._session_id = None
        return result

    def on_context_compact(
        self,
        compacted_from: int = 0,
        compacted_to: int = 0,
        summary: str = "",
    ):
        """上下文压缩时调用。"""
        return self._collector.hook_context_compact(
            self.agent_name, compacted_from, compacted_to, summary
        )

    def on_error(
        self,
        error_type: str,
        error_message: str,
        stack_trace: str = "",
    ):
        """错误发生时调用。"""
        return self._collector.hook_error_event(
            self.agent_name, error_type, error_message, stack_trace
        )

    def flush(self):
        """手动刷新缓冲。"""
        return self._collector.flush()

    def statistics(self) -> Dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "session_id": self._session_id,
            "collector": self._collector.statistics(),
        }


# ── Multi-Agent Connector Factory ─────────────────────────────────────────

class CollectorManager:
    """统一管理 6 个 AgentConnector + BackgroundScanner 的生命周期。

    用法：
        manager = CollectorManager()
        manager.start()
        # ... Agent 运行期间各 connector 自动采集 ...
        manager.stop()
    """

    def __init__(
        self,
        adapters: Optional[Dict[str, AgentConnector]] = None,
        scan_interval: float = DEFAULT_SCAN_INTERVAL,
    ):
        self._shared_collector = EventDrivenCollector()
        self._connectors: Dict[str, AgentConnector] = {}
        self._scanner = BackgroundScanner(
            self._shared_collector, scan_interval=scan_interval
        )
        self._init_connectors(adapters or {})

    def _init_connectors(self, adapters: Dict[str, AgentConnector]):
        for agent_name in BUILTIN_AGENTS:
            if agent_name in adapters:
                self._connectors[agent_name] = adapters[agent_name]
            else:
                self._connectors[agent_name] = AgentConnector(
                    event_collector=self._shared_collector,
                    agent_name=agent_name,
                )

    def get(self, agent_name: str) -> Optional[AgentConnector]:
        return self._connectors.get(agent_name)

    def start(self):
        """启动后台扫描器。"""
        self._scanner.start()

    def stop(self):
        """停止后台扫描器并 flush 所有缓冲。"""
        self._scanner.stop()
        for connector in self._connectors.values():
            connector.flush()

    def statistics(self) -> Dict[str, Any]:
        return {
            "scanner": self._scanner.statistics(),
            "collector": self._shared_collector.statistics(),
            "connectors": {
                name: conn.statistics()
                for name, conn in self._connectors.items()
            },
        }


# ── Self-Test ─────────────────────────────────────────────────────────────

def self_test() -> bool:
    """Active Collector 自检。"""
    print("=" * 60)
    print("  Trinity Active Collector v8.3.0 — Self Test")
    print("=" * 60)
    passed = 0
    total = 0

    # Test 1: EventDrivenCollector creation
    total += 1
    print("\n[Test 1] EventDrivenCollector creation")
    try:
        collector = EventDrivenCollector(importance_threshold=0.10)
        assert collector.importance_threshold == 0.10
        assert len(collector._buffer) == 0
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test 2: hook_conversation_start
    total += 1
    print("\n[Test 2] hook_conversation_start")
    try:
        payload = collector.hook_conversation_start(
            "file-agent", "Process invoices"
        )
        assert payload is not None
        assert payload.hook_point == HookPoint.CONVERSATION_START
        assert payload.agent_name == "file-agent"
        assert payload.category == "episodic"
        print(f"    agent={payload.agent_name}, content={payload.content[:50]}...")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test 3: hook_tool_call (before)
    total += 1
    print("\n[Test 3] hook_tool_call (before)")
    try:
        payload = collector.hook_tool_call(
            "file-agent", "read_file", phase="before",
            tool_args={"file_path": "/tmp/test.pdf"}
        )
        assert payload is not None
        assert payload.hook_point == HookPoint.TOOL_CALL
        assert "read_file" in payload.content
        print(f"    content={payload.content[:60]}...")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test 4: hook_decision_point
    total += 1
    print("\n[Test 4] hook_decision_point")
    try:
        payload = collector.hook_decision_point(
            "main",
            decision="delegate to file-agent",
            reasoning="file-agent is the best match for invoice processing",
            options_considered=["file-agent", "search-agent"],
        )
        assert payload is not None
        assert payload.importance >= 0.40
        print(f"    decision={payload.content[:60]}..., importance={payload.importance:.2f}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test 5: hook_error_event
    total += 1
    print("\n[Test 5] hook_error_event")
    try:
        payload = collector.hook_error_event(
            "browser", "ConnectionError",
            "Failed to connect to https://example.com",
            stack_trace="Traceback... line 42 in connect()"
        )
        assert payload is not None
        assert payload.importance >= 0.50
        assert "ConnectionError" in payload.content
        print(f"    error={payload.content[:60]}..., importance={payload.importance:.2f}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test 6: 去重测试
    total += 1
    print("\n[Test 6] Duplicate deduplication")
    try:
        collector2 = EventDrivenCollector(importance_threshold=0.10)
        p1 = collector2.hook_conversation_start("main", "Same task")
        p2 = collector2.hook_conversation_start("main", "Same task")
        assert p1 is not None, "First call should succeed"
        assert p2 is None, "Duplicate should be skipped"
        print(f"    first={bool(p1)}, duplicate={bool(p2)}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test 7: Importance threshold filtering
    total += 1
    print("\n[Test 7] Importance threshold filtering")
    try:
        collector3 = EventDrivenCollector(importance_threshold=0.50)
        p = collector3.hook_conversation_start("main", "trivial task")
        assert p is None, "Low importance should be filtered"
        print(f"    low-importance event filtered: {p is None}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test 8: AgentConnector
    total += 1
    print("\n[Test 8] AgentConnector creation and binding")
    try:
        collector4 = EventDrivenCollector(importance_threshold=0.10)
        connector = AgentConnector(
            event_collector=collector4,
            agent_name="file-agent",
        )
        assert connector.agent_name == "file-agent"
        p = connector.on_conversation_start("Process PDFs")
        assert p is not None
        print(f"    agent={connector.agent_name}, session={connector._session_id}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test 9: CollectorManager
    total += 1
    print("\n[Test 9] CollectorManager creation")
    try:
        manager = CollectorManager()
        assert len(manager._connectors) == 6
        assert manager.get("main") is not None
        assert manager.get("file-agent") is not None
        assert manager.get("unknown") is None
        print(f"    connectors: {list(manager._connectors.keys())}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test 10: MemoryPayload.to_store_dict()
    total += 1
    print("\n[Test 10] MemoryPayload.to_store_dict()")
    try:
        payload = MemoryPayload(
            hook_point=HookPoint.TOOL_CALL,
            agent_name="search-agent",
            content="Test memory",
            importance=0.60,
            category="trace",
            tags=["test"],
        )
        d = payload.to_store_dict()
        assert d["agent_id"] == "search-agent"
        assert d["importance"] == 0.60
        assert d["category"] == "trace"
        assert "collector" in d["metadata"]["collector"]
        print(f"    dict keys: {list(d.keys())}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # Summary
    print("\n" + "=" * 60)
    print(f"  RESULTS: {passed}/{total} passed")
    print("=" * 60)
    return passed == total


if __name__ == "__main__":
    ok = self_test()
    raise SystemExit(0 if ok else 1)
