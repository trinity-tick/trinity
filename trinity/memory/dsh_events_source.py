"""DSH 事件源连接器 (2026-08-18)

把 DSH 结构层事件流（dsh_events 表，位于运行时权威库
~/.trinity/store/trinity_store.db）接入 Active Collector，让采集通道
从「空转」（6 个内置 Agent 无生产者）变为有真实数据源。

选择性映射（避免淹没记忆库）：
  - user/message          -> conversation_start (importance 0.25)
  - goal/write            -> decision_point    (importance 0.45)
  - tool/call 持久化类工具  -> decision_point    (importance 0.45)
  - tool/result 含错误     -> error_event       (importance 0.60)
  - turn/end 中止/失败     -> error_event       (importance 0.60)

游标持久化在 ~/.trinity/data/dsh_events_cursor.json，按 seq 单调递增增量消费。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("trinity.memory.dsh_events_source")

# 运行时权威库（与 engine_worker / client 一致）
DEFAULT_STORE_PATH = os.path.expanduser("~/.trinity/store/trinity_store.db")
DEFAULT_CURSOR_FILE = os.path.expanduser("~/.trinity/data/dsh_events_cursor.json")

# 触发 decision_point 的「持久化类」DSH 工具（写记忆/目标/日程）
PERSIST_TOOLS = {
    "create_goal", "update_goal", "trinity_write", "trinity_update",
    "trinity_goal", "schedule_create", "trinity_schedule",
}

# 每轮扫描最大事件数（防一次追赶过量）
MAX_EVENTS_PER_SCAN = 2000


# ── 纯分类函数（可单测）──────────────────────────────────────────────

def classify_event(ev: Dict[str, Any], captures: Optional[Dict[str, bool]] = None) -> Optional[Dict[str, Any]]:
    """把一条 dsh_events 行分类为 hook 调用参数。

    Returns:
        形如 {"hook": "conversation_start", ...kwargs} 的 dict；不捕获返回 None。
    """
    captures = captures or {}
    etype = ev.get("type")
    data = ev.get("data") or {}
    agent_name = "dsh-agent"
    meta = {
        "dsh_session": ev.get("session_id"),
        "dsh_event_type": etype,
        "dsh_seq": ev.get("seq"),
        "collector": "active_collector",
    }

    if etype == "user/message":
        if captures.get("user_messages", True) is False:
            return None
        text = str(data.get("content") or data.get("text") or "")[:200]
        return {
            "hook": "conversation_start",
            "agent_name": agent_name,
            "task_desc": text or "(user message)",
            "metadata": meta,
        }

    if etype == "goal/write":
        if captures.get("goal_write", True) is False:
            return None
        objective = str(data.get("objective") or data.get("content") or "")[:180]
        return {
            "hook": "decision_point",
            "agent_name": agent_name,
            "decision": f"Goal lifecycle: {data.get('action') or data.get('status') or 'update'}",
            "reasoning": objective or "",
            "options": None,
            "metadata": meta,
        }

    if etype == "tool/call":
        if captures.get("persist_tools", True) is False:
            return None
        name = str(data.get("name") or "")
        if name in PERSIST_TOOLS:
            return {
                "hook": "decision_point",
                "agent_name": agent_name,
                "decision": f"Persist via {name}",
                "reasoning": "",
                "options": None,
                "metadata": meta,
            }
        return None

    if etype == "tool/result":
        if captures.get("errors", True) is False:
            return None
        err = data.get("error") or {}
        is_err = bool(err) or bool(data.get("isError"))
        if is_err:
            etype_ = str(err.get("name") or "ToolError")
            msg = str(err.get("message") or "")[:200]
            return {
                "hook": "error_event",
                "agent_name": agent_name,
                "error_type": etype_,
                "error_message": msg or "tool result error",
                "stack_trace": "",
                "metadata": meta,
            }
        return None

    if etype == "turn/end":
        if captures.get("errors", True) is False:
            return None
        reason = data.get("reason") or {}
        kind = reason.get("kind") or data.get("kind") or ""
        if kind in ("aborted", "failed", "error", "cancelled"):
            return {
                "hook": "error_event",
                "agent_name": agent_name,
                "error_type": "TurnAborted",
                "error_message": f"DSH turn ended with kind={kind}",
                "stack_trace": "",
                "metadata": meta,
            }
        return None

    return None


def apply_event(collector: Any, ev: Dict[str, Any], captures: Optional[Dict[str, bool]] = None):
    """把 dsh_events 行投递到 EventDrivenCollector 的 hook。返回 MemoryPayload 或 None。"""
    cls = classify_event(ev, captures)
    if cls is None:
        return None
    hook = cls["hook"]
    if hook == "conversation_start":
        return collector.hook_conversation_start(
            cls["agent_name"], cls["task_desc"], cls["metadata"])
    if hook == "decision_point":
        return collector.hook_decision_point(
            cls["agent_name"], cls["decision"], cls.get("reasoning", ""),
            cls.get("options"), cls["metadata"])
    if hook == "error_event":
        return collector.hook_error_event(
            cls["agent_name"], cls["error_type"], cls["error_message"],
            cls.get("stack_trace", ""), cls["metadata"])
    return None


# ── 事件源（轮询线程）────────────────────────────────────────────────

class DshEventsSource:
    """轮询 DSH 结构层 dsh_events 表并投递到共享 collector 的事件源。"""

    def __init__(
        self,
        event_collector: Any,
        store_path: Optional[str] = None,
        cursor_file: Optional[str] = None,
        poll_interval: float = 30.0,
        captures: Optional[Dict[str, bool]] = None,
    ):
        self._collector = event_collector
        self._store_path = os.path.normpath(os.path.expanduser(store_path or DEFAULT_STORE_PATH))
        self._cursor_file = os.path.normpath(os.path.expanduser(cursor_file or DEFAULT_CURSOR_FILE))
        self._poll_interval = float(poll_interval or 30.0)
        self._captures = captures or {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cursor_existed = os.path.exists(self._cursor_file)
        self._stats = {
            "scans": 0,
            "events_seen": 0,
            "events_emitted": 0,
            "errors": 0,
            "last_id": self._load_cursor(),
            "last_scan_at": None,
        }

    # ── 游标 ──────────────────────────────────────────────────────
    def _load_cursor(self) -> int:
        try:
            if os.path.exists(self._cursor_file):
                with open(self._cursor_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # 2026-08-18: seq 非全局单调（按会话分配），改用 id(rowid)
                    # 作游标；旧 last_seq 游标文件直接废弃重来
                    return int(data.get("last_id", 0) or 0)
        except Exception as e:
            logger.warning("DshEventsSource: load cursor failed: %s", e)
        return 0

    def _save_cursor(self, last_id: int) -> None:
        try:
            os.makedirs(os.path.dirname(self._cursor_file), exist_ok=True)
            with open(self._cursor_file, "w", encoding="utf-8") as f:
                json.dump({"last_id": last_id, "updated_at": time.time()}, f)
        except Exception as e:
            logger.warning("DshEventsSource: save cursor failed: %s", e)

    # ── 生命周期 ──────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="trinity-dsh-events-source")
        self._thread.start()
        logger.info("DshEventsSource: started (store=%s, cursor=%s, interval=%.1fs, last_id=%d)",
                    self._store_path, self._cursor_file, self._poll_interval, self._stats["last_id"])

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10.0)
        self._stats["last_id"] = self._load_cursor()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as e:
                self._stats["errors"] += 1
                logger.error("DshEventsSource: scan error: %s", e)
            self._stop.wait(self._poll_interval)

    # ── 扫描 ──────────────────────────────────────────────────────
    def scan_once(self) -> int:
        """消费一轮新事件。返回投递数。"""
        if not os.path.exists(self._store_path):
            logger.warning("DshEventsSource: store not found: %s", self._store_path)
            return 0
        try:
            # 普通连接（Windows 上 URI mode=ro 的 file:C:\... 解析有兼容问题）
            conn = sqlite3.connect(self._store_path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
        except Exception as e:
            self._stats["errors"] += 1
            logger.error("DshEventsSource: connect failed: %s", e)
            return 0

        last_id = self._stats["last_id"]
        emitted = 0
        seen = 0
        max_id = last_id

        # 首次运行只向前：把游标初始化到当前最大 id，跳过历史回填
        # （否则会把结构层积累的历史事件一次性灌入记忆库）
        if not self._cursor_existed:
            try:
                max_row = conn.execute("SELECT MAX(id) AS m FROM dsh_events").fetchone()
                init_id = int(max_row["m"] or 0) if max_row else 0
                self._stats["last_id"] = init_id
                self._save_cursor(init_id)
                self._cursor_existed = True
                logger.info("DshEventsSource: first run — cursor initialized to max_id=%d (history skipped, forward-only)", init_id)
            except Exception as e:
                logger.warning("DshEventsSource: cursor init failed: %s", e)
            finally:
                conn.close()
                self._stats["scans"] += 1
                self._stats["last_scan_at"] = time.time()
                return 0

        try:
            try:
                # dsh_events 表结构：id/session_id/seq/type/turn/step/time/payload
                # （payload 为 JSON 文本，即结构层 API 的 data 字段）
                # 注意：seq 按会话分配非全局单调，游标必须用 id(rowid)
                rows = conn.execute(
                    "SELECT id, seq, session_id, type, payload FROM dsh_events "
                    "WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (last_id, MAX_EVENTS_PER_SCAN),
                ).fetchall()
            except sqlite3.OperationalError as e:
                self._stats["errors"] += 1
                logger.warning("DshEventsSource: dsh_events query failed: %s", e)
                return 0
            for row in rows:
                seen += 1
                try:
                    payload = json.loads(row["payload"] or "{}")
                except (ValueError, TypeError):
                    payload = {}
                ev = {"id": row["id"], "seq": row["seq"], "session_id": row["session_id"],
                      "type": row["type"], "data": payload}
                try:
                    p = apply_event(self._collector, ev, self._captures)
                    if p is not None:
                        emitted += 1
                except Exception as e:
                    logger.debug("DshEventsSource: apply failed for id=%s: %s", row["id"], e)
                max_id = row["id"]
        finally:
            conn.close()

        if seen > 0:
            self._stats["events_seen"] += seen
            self._stats["events_emitted"] += emitted
            self._stats["last_id"] = max_id
            self._save_cursor(max_id)
            logger.info("DshEventsSource: scan done — seen=%d emitted=%d (last_id=%d)",
                        seen, emitted, max_id)
        self._stats["scans"] += 1
        self._stats["last_scan_at"] = time.time()
        return emitted

    def statistics(self) -> Dict[str, Any]:
        return {
            "store_path": self._store_path,
            "cursor_file": self._cursor_file,
            "poll_interval": self._poll_interval,
            "running": bool(self._thread and self._thread.is_alive()),
            **self._stats,
        }


# ── 配置加载（agent_config.yaml 的 active_collection.dsh_events）─────────

def load_config(trinity_home: Optional[str] = None) -> Dict[str, Any]:
    """从 agent_config.yaml 读取 active_collection.dsh_events 配置。"""
    import yaml
    trinity_home = trinity_home or os.path.abspath(
        os.path.join(os.path.dirname(__file__), ".."))
    config_path = os.path.join(trinity_home, "agents", "agent_config.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return (cfg.get("active_collection", {}) or {}).get("dsh_events", {}) or {}
    except Exception as e:
        logger.warning("DshEventsSource: failed to read agent_config.yaml: %s", e)
        return {}


def create_source(event_collector: Any) -> Optional["DshEventsSource"]:
    """按配置创建事件源；未启用或配置缺失时返回 None。"""
    cfg = load_config()
    if cfg.get("enabled", True) is False:
        logger.info("DshEventsSource: disabled by agent_config.yaml")
        return None
    try:
        return DshEventsSource(
            event_collector=event_collector,
            store_path=cfg.get("store_path"),
            cursor_file=cfg.get("cursor_file"),
            poll_interval=cfg.get("poll_interval", 30.0),
            captures=cfg.get("captures") or {},
        )
    except Exception as e:
        logger.error("DshEventsSource: create failed: %s", e)
        return None
