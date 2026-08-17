"""
MarvisTrinityBridge — Marvis 对话实时同步到 Trinity 的 HTTP 桥接层
==================================================================

通过 Trinity API 的 /agents/memory/write 和 /agents/register 端点
将 Marvis 每轮对话的 lifecycle 事件写入 Trinity 共享记忆池。

设计原则：
  - 异步非阻塞：所有 HTTP 调用在 threading.Thread 中执行
  - 静默降级：Trinity 不可达时不阻塞 Marvis 主流程
  - 指数退避重试：最多 3 次，失败写入 bridge_failures.log
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── 可选依赖 ──────────────────────────────────────────────────────────
try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    import urllib.request
    import urllib.error
    _HAS_URLLIB = True
except ImportError:
    _HAS_URLLIB = True  # stdlib, always present

# ── 有界并发（2026-08-15，根治同步守护持锁）──────────────────────
# 原 _post_async 无界 spawn 线程：Marvis 同步守护每 60s 扫描有新对话时，
# 每个对话 spawn 一个线程并发 POST /agents/memory/write → API 聚合池
# 写路径被并发压垮/长事务悬挂 → 引擎库写锁（database is locked）。
# 信号量限制同时推送数，防线程风暴。
_ASYNC_PUSH_SEMAPHORE = threading.BoundedSemaphore(8)


# ── 常量 ──────────────────────────────────────────────────────────────

BUILTIN_AGENTS = [
    "main",
    "file-agent",
    "browser",
    "app-agent",
    "computer-agent",
    "search-agent",
]

DEFAULT_API_BASE = "http://localhost:8005"

# 失败日志路径：<项目根>/data/bridge_failures.log
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_FAIL_LOG = _DATA_DIR / "bridge_failures.log"


# ── Logger ────────────────────────────────────────────────────────────

logger = logging.getLogger("trinity.bridges.marvis")


def _ensure_log_dir() -> None:
    """确保 data 目录和失败日志文件存在。"""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not _FAIL_LOG.exists():
        _FAIL_LOG.touch()


def _log_failure(payload: Dict[str, Any], error: str) -> None:
    """将发送失败的消息写入 bridge_failures.log（JSONL）。"""
    _ensure_log_dir()
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "payload": payload,
    }
    try:
        with open(_FAIL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── DataClass ─────────────────────────────────────────────────────────

@dataclass
class PushResult:
    """单次推送结果。"""
    success: bool
    memory_id: Optional[str] = None
    error: Optional[str] = None
    merged: bool = False


@dataclass
class BulkPushResult:
    """批量推送结果（push_raw_bulk）。"""
    written: int = 0
    failed: int = 0
    memory_ids: List[str] = field(default_factory=list)
    error: Optional[str] = None


# ── MarvisTrinityBridge ───────────────────────────────────────────────

class MarvisTrinityBridge:
    """Marvis → Trinity 实时同步桥接器。

    用法：
        bridge = MarvisTrinityBridge(api_base="http://localhost:8005")
        bridge.register_all_agents()

        # 在 Marvis Agent 生命周期回调中调用：
        bridge.push_session_start("main", "处理发票扫描", {})
        bridge.push_tool_call("file-agent", "read_file",
                              {"file_path": "/tmp/a.pdf"}, "读取成功")
        bridge.push_session_end("main", "完成发票扫描，共 5 份", 5, {})

    线程安全：所有 HTTP 调用在 threading.Thread 中异步执行，
    不会阻塞 Marvis 主流程。
    """

    MAX_RETRIES = 3
    RETRY_BACKOFF_BASE = 0.5    # 首次重试等待 0.5s
    RETRY_BACKOFF_MAX = 4.0     # 最大等待 4.0s

    def __init__(self, api_base: str = DEFAULT_API_BASE):
        self.api_base = api_base.rstrip("/")
        self._session: Any = None
        self._lock = threading.Lock()

        if _HAS_REQUESTS:
            self._session = requests.Session()
            self._session.headers.update({
                "Content-Type": "application/json",
                # RBAC 中间件要求受保护路由携带 X-Agent-ID，否则返回 401
                "X-Agent-ID": "marvis-main",
                "X-Agent-Role": "admin",
            })
        else:
            logger.warning("requests 不可用，fallback 到 urllib")

    # ── 底层 HTTP ──────────────────────────────────────────────────

    def _post_sync(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """同步 POST 请求（含重试）。返回 {"ok": bool, "data": ..., "error": ...}"""
        url = f"{self.api_base}{endpoint}"
        last_error = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                if _HAS_REQUESTS and self._session:
                    resp = self._session.post(url, json=payload, timeout=5)
                    resp.raise_for_status()
                    return {"ok": True, "data": resp.json()}

                # urllib fallback
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    url, data=data,
                    headers={
                        "Content-Type": "application/json",
                        "X-Agent-ID": "marvis-main",
                        "X-Agent-Role": "admin",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return {"ok": True, "data": json.loads(resp.read().decode())}

            except Exception as e:
                last_error = str(e)
                if attempt < self.MAX_RETRIES:
                    wait = min(
                        self.RETRY_BACKOFF_BASE * (2 ** (attempt - 1)),
                        self.RETRY_BACKOFF_MAX,
                    )
                    logger.debug(
                        "POST %s 失败 (attempt %d/%d): %s, %.1fs 后重试",
                        endpoint, attempt, self.MAX_RETRIES, e, wait,
                    )
                    time.sleep(wait)

        _log_failure(payload, last_error or "unknown")
        return {"ok": False, "error": last_error}

    def _post_async(self, endpoint: str, payload: Dict[str, Any]) -> None:
        """异步 POST：在新线程中执行，不阻塞调用方。

        2026-08-15：加有界并发（信号量 _ASYNC_PUSH_SEMAPHORE，最多 8 个
        同时推送）——原实现无界 spawn 线程，Marvis 同步守护批量推送时
        产生并发写风暴，曾导致引擎库写锁（database is locked）。
        """
        def _worker():
            try:
                with _ASYNC_PUSH_SEMAPHORE:
                    self._post_sync(endpoint, payload)
            except Exception as exc:
                logger.warning("async push %s failed: %s", endpoint, exc)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    # ── Agent 注册 ─────────────────────────────────────────────────

    def register_all_agents(self) -> List[Dict[str, Any]]:
        """批量注册 6 个内置 Agent 到 Trinity。

        Returns:
            每个 agent 的注册结果列表。
        """
        results = []
        for agent_name in BUILTIN_AGENTS:
            agent_id = f"marvis-{agent_name}"
            payload = {
                "agent_id": agent_id,
                "agent_name": agent_name,
                "capabilities": self._default_capabilities(agent_name),
                "metadata": {
                    "source": "marvis_bridge",
                    "registered_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            ret = self._post_sync("/agents/register", payload)
            results.append({
                "agent_name": agent_name,
                "agent_id": agent_id,
                **ret,
            })
            logger.info("register agent: %s → %s", agent_name, ret.get("data", {}).get("status", "FAIL"))
        return results

    @staticmethod
    def _default_capabilities(agent_name: str) -> List[str]:
        """返回各 Agent 的默认能力声明。"""
        caps = {
            "main": ["task_orchestration", "agent_routing", "error_recovery"],
            "file-agent": ["file_read", "file_write", "file_organize", "content_search"],
            "browser": ["web_browsing", "form_filling", "page_extraction"],
            "app-agent": ["app_download", "app_operation", "app_recommendation"],
            "computer-agent": ["system_settings", "process_management", "window_layout"],
            "search-agent": ["deep_search", "academic_research", "comparative_analysis"],
        }
        return caps.get(agent_name, ["general"])

    # ── 对话生命周期 Push ──────────────────────────────────────────

    def push_session_start(
        self,
        agent_id: str,
        task_desc: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """会话开始。"""
        ts = datetime.now(timezone.utc).isoformat()
        self._post_async("/agents/memory/write", {
            "agent_id": agent_id,
            "content": task_desc,
            "category": "episodic",
            "importance": 0.6,
            "tags": ["session_start"],
            "metadata": {
                **(metadata or {}),
                "event": "session_start",
                "timestamp": ts,
            },
        })

    def push_session_end(
        self,
        agent_id: str,
        summary: str,
        task_count: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """会话结束。"""
        ts = datetime.now(timezone.utc).isoformat()
        self._post_async("/agents/memory/write", {
            "agent_id": agent_id,
            "content": summary,
            "category": "episodic",
            "importance": 0.7,
            "tags": ["session_end"],
            "metadata": {
                **(metadata or {}),
                "event": "session_end",
                "task_count": task_count,
                "timestamp": ts,
            },
        })

    def push_tool_call(
        self,
        agent_id: str,
        tool_name: str,
        tool_args: Optional[Dict[str, Any]] = None,
        result_preview: str = "",
    ) -> None:
        """工具调用。"""
        ts = datetime.now(timezone.utc).isoformat()
        content = f"[{tool_name}] {result_preview}" if result_preview else f"[{tool_name}] called"
        self._post_async("/agents/memory/write", {
            "agent_id": agent_id,
            "content": content,
            "category": "episodic",
            "importance": 0.4,
            "tags": ["tool_call", tool_name],
            "metadata": {
                "event": "tool_call",
                "tool_name": tool_name,
                "tool_args": tool_args or {},
                "timestamp": ts,
            },
        })

    def push_decision(
        self,
        agent_id: str,
        decision: str,
        reasoning: str = "",
    ) -> None:
        """重要决策。"""
        ts = datetime.now(timezone.utc).isoformat()
        content = f"Decision: {decision}"
        if reasoning:
            content += f" | Reasoning: {reasoning}"
        self._post_async("/agents/memory/write", {
            "agent_id": agent_id,
            "content": content,
            "category": "episodic",
            "importance": 0.5,
            "tags": ["decision"],
            "metadata": {
                "event": "decision",
                "decision": decision,
                "reasoning": reasoning,
                "timestamp": ts,
            },
        })

    def push_error(
        self,
        agent_id: str,
        error_type: str,
        error_msg: str,
    ) -> None:
        """错误事件。"""
        ts = datetime.now(timezone.utc).isoformat()
        self._post_async("/agents/memory/write", {
            "agent_id": agent_id,
            "content": f"[{error_type}] {error_msg}",
            "category": "error",
            "importance": 0.8,
            "tags": ["error", error_type],
            "metadata": {
                "event": "error",
                "error_type": error_type,
                "timestamp": ts,
            },
        })

    def push_raw(
        self,
        agent_id: str,
        content: str,
        category: str = "conversation",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """通用写入，不做语义封装。"""
        ts = datetime.now(timezone.utc).isoformat()
        self._post_async("/agents/memory/write", {
            "agent_id": agent_id,
            "content": content,
            "category": category,
            "importance": 0.4,
            "tags": tags or [],
            "metadata": {
                **(metadata or {}),
                "event": "raw_push",
                "timestamp": ts,
            },
        })

    # ── 同步版 (用于验证/调试) ──────────────────────────────────────

    def push_raw_sync(
        self,
        agent_id: str,
        content: str,
        category: str = "conversation",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PushResult:
        """同步推送（调试/验证用），返回 PushResult。"""
        ts = datetime.now(timezone.utc).isoformat()
        ret = self._post_sync("/agents/memory/write", {
            "agent_id": agent_id,
            "content": content,
            "category": category,
            "importance": 0.4,
            "tags": tags or [],
            "metadata": {
                **(metadata or {}),
                "event": "raw_push",
                "timestamp": ts,
            },
        })
        if ret["ok"]:
            data = ret["data"]
            return PushResult(
                success=True,
                memory_id=data.get("memory_id"),
                merged=data.get("merged", False),
            )
        return PushResult(success=False, error=ret.get("error"))

    # ── 批量推送（2026-08-15：根治同步守护写锁）─────────────────────
    # 原 push_raw 对每个对话一次 HTTP + 异步线程；Marvis 同步守护批量
    # 推送时产生并发写风暴（EXECUTION 33 根因）。本方法把多条合并为
    # 一次 POST /agents/memory/bulk_write（单请求多条目，串行写），
    # 从源头消除并发。

    def push_raw_bulk(
        self,
        entries: List[Dict[str, Any]],
    ) -> "BulkPushResult":
        """批量推送多条记忆（一次 HTTP 请求）。

        Args:
            entries: [{agent_id, content, category?, tags?, metadata?}, ...]

        Returns:
            BulkPushResult(written, failed, memory_ids, error)。
        """
        ts = datetime.now(timezone.utc).isoformat()
        payload_entries = []
        for e in entries:
            payload_entries.append({
                "agent_id": e.get("agent_id", "marvis-main"),
                "content": e.get("content", ""),
                "category": e.get("category", "conversation"),
                "importance": 0.4,
                "tags": e.get("tags") or [],
                "metadata": {
                    **(e.get("metadata") or {}),
                    "event": "raw_push_bulk",
                    "timestamp": ts,
                },
            })
        if not payload_entries:
            return BulkPushResult(written=0, failed=0, memory_ids=[], error=None)
        ret = self._post_sync("/agents/memory/bulk_write", {"entries": payload_entries})
        if ret["ok"]:
            data = ret.get("data", {})
            return BulkPushResult(
                written=data.get("written", 0),
                failed=data.get("failed", 0),
                memory_ids=data.get("memory_ids", []),
                error=None,
            )
        return BulkPushResult(written=0, failed=len(payload_entries), memory_ids=[], error=ret.get("error"))


# ── Self-Test ─────────────────────────────────────────────────────────

def self_test(api_base: str = DEFAULT_API_BASE) -> bool:
    """模块自检：注册 agent + 推送测试记忆 + 验证。"""
    print("=" * 60)
    print("  MarvisTrinityBridge v1.0.0 — Self Test")
    print("=" * 60)
    passed = 0
    total = 0

    bridge = MarvisTrinityBridge(api_base=api_base)

    # Test 1: register_all_agents
    total += 1
    print("\n[Test 1] register_all_agents()")
    try:
        results = bridge.register_all_agents()
        ok_count = sum(1 for r in results if r["ok"])
        print(f"  {ok_count}/{len(results)} agents registered")
        for r in results:
            status = "OK" if r["ok"] else f"FAIL: {r.get('error', '?')}"
            print(f"    {r['agent_name']}: {status}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test 2: push_raw_sync (同步验证)
    total += 1
    print("\n[Test 2] push_raw_sync() — 写入测试记忆")
    try:
        test_content = f"bridge-self-test-{uuid.uuid4().hex[:8]}"
        result = bridge.push_raw_sync(
            agent_id="marvis-main",
            content=test_content,
            category="test",
            tags=["self_test"],
            metadata={"source": "bridge_self_test"},
        )
        if result.success:
            print(f"  PASS — memory_id={result.memory_id}")
            passed += 1
        else:
            print(f"  FAIL — {result.error}")
    except Exception as e:
        print(f"  FAIL: {e}")

    # Test 3: push_session_start (异步)
    total += 1
    print("\n[Test 3] push_session_start() — 异步推送")
    try:
        bridge.push_session_start("marvis-main", "Bridge self-test session", {})
        time.sleep(0.5)
        print("  PASS — async push dispatched")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    print(f"\n  Result: {passed}/{total} PASS")
    print("=" * 60)
    return passed == total
