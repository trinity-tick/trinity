"""
auto_syncer.py — Marvis 对话自动同步到 Trinity MemoryAggregator
================================================================

定期扫描 Marvis 本地数据库（data.db），提取用户对话记录，通过
MarvisTrinityBridge 推送到 Trinity 聚合池。

模式：
  - oneshot：一次性全量/增量同步
  - daemon： 定时轮询守护进程（默认 60s 间隔）

去重机制：
  - 基于 data/sync_state.json 记录 last_sync_ts
  - 仅同步 updated_at > last_sync_ts 的会话
  - 自动过滤定时任务/系统事件等非用户对话
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from trinity.bridges.marvis_bridge import MarvisTrinityBridge, BUILTIN_AGENTS

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────

# 默认 Marvis 数据目录（支持多用户）
DEFAULT_MARVIS_USER_DIR = Path(os.environ.get(
    "MARVIS_USER_DIR",
    r"C:\Users\Administrator\AppData\Roaming\Tencent\Marvis\User",
))

# 跳过的对话标题关键词（非用户对话）
SKIP_TITLE_KEYWORDS = [
    "定时任务执行",
    "定时任务",
    "schedule",
]

# 最小用户消息数才算有效对话
MIN_USER_MESSAGES = 1

# 摘要最大用户消息数
MAX_SUMMARY_MESSAGES = 3

# 摘要每条截断长度
SUMMARY_TRUNCATE = 200

# 默认轮询间隔（秒）
DEFAULT_POLL_INTERVAL = 60


@dataclass
class SyncState:
    """同步状态持久化结构。"""
    last_sync_ts: str = ""           # ISO8601 时间戳
    total_synced: int = 0
    synced_conv_ids: List[str] = field(default_factory=list)
    user_dirs: Dict[str, str] = field(default_factory=dict)  # user_id → last_sync_ts


class ConversationScanner:
    """扫描 Marvis 本地数据库，提取新对话并推送。"""

    def __init__(
        self,
        bridge: Optional[MarvisTrinityBridge] = None,
        user_dir: Optional[Path] = None,
        state_file: Optional[Path] = None,
        project_root: Optional[Path] = None,
    ):
        self.user_dir = Path(user_dir or DEFAULT_MARVIS_USER_DIR)
        self.bridge = bridge or MarvisTrinityBridge()

        if project_root is None:
            project_root = Path(__file__).resolve().parent.parent.parent
        self.state_file = Path(state_file or (project_root / "data" / "sync_state.json"))
        self.project_root = project_root

        self._state: SyncState = SyncState()
        self._load_state()

    # ── 状态管理 ──────────────────────────────────────────────────

    def _load_state(self) -> None:
        """从 JSON 文件加载同步状态。"""
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text("utf-8"))
                self._state = SyncState(
                    last_sync_ts=data.get("last_sync_ts", ""),
                    total_synced=data.get("total_synced", 0),
                    synced_conv_ids=data.get("synced_conv_ids", []),
                    user_dirs=data.get("user_dirs", {}),
                )
            except Exception:
                logger.warning("Failed to load sync state, starting fresh")

    def _save_state(self) -> None:
        """持久化同步状态。"""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps({
                "last_sync_ts": self._state.last_sync_ts,
                "total_synced": self._state.total_synced,
                "synced_conv_ids": self._state.synced_conv_ids[-200:],  # 只保留最近 200
                "user_dirs": self._state.user_dirs,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2, ensure_ascii=False),
            "utf-8",
        )

    # ── 用户目录发现 ──────────────────────────────────────────────

    def discover_users(self) -> List[Path]:
        """扫描 Marvis User 目录，返回所有子用户目录。"""
        if not self.user_dir.exists():
            logger.warning("User dir not found: %s", self.user_dir)
            return []

        return [
            p for p in self.user_dir.iterdir()
            if p.is_dir() and (p / "database" / "data.db").exists()
        ]

    # ── 对话查询 ──────────────────────────────────────────────────

    def _get_new_conversations(
        self,
        db_path: Path,
        since_ts: str,
    ) -> List[Dict[str, Any]]:
        """从 data.db 查询指定时间后的新对话。"""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            if since_ts:
                rows = conn.execute(
                    """SELECT * FROM conversations
                       WHERE updated_at > ?
                       ORDER BY updated_at ASC""",
                    (since_ts,),
                ).fetchall()
            else:
                # 无 since_ts → 全量（首次同步）
                rows = conn.execute(
                    "SELECT * FROM conversations ORDER BY updated_at ASC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _get_conversation_messages(
        self,
        db_path: Path,
        conversation_id: str,
    ) -> List[Dict[str, Any]]:
        """获取指定对话的消息列表。"""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT * FROM messages
                   WHERE conversation_id = ?
                   ORDER BY rowid ASC""",
                (conversation_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── 对话过滤 ──────────────────────────────────────────────────

    def _should_skip(self, conv: Dict[str, Any]) -> bool:
        """判断是否跳过此对话。"""
        title = conv.get("title") or ""

        # 跳过定时任务
        for kw in SKIP_TITLE_KEYWORDS:
            if kw in title:
                return True

        return False

    # ── 摘要提取 ──────────────────────────────────────────────────

    def _extract_summary(
        self,
        db_path: Path,
        conv: Dict[str, Any],
    ) -> str:
        """提取对话摘要：标题 + 前几条用户消息。"""
        title = conv.get("title", "未命名对话")[:100]
        messages = self._get_conversation_messages(db_path, conv["conversation_id"])

        user_msgs = [m for m in messages if m.get("role") == "user"]
        if not user_msgs:
            return f"[无用户消息] {title}"

        snippets = []
        for m in user_msgs[:MAX_SUMMARY_MESSAGES]:
            content = (m.get("content") or "").strip()[:SUMMARY_TRUNCATE]
            if content:
                snippets.append(content)

        summary_body = " | ".join(snippets)
        return f"{title}: {summary_body}"

    # ── Agent 路由 ────────────────────────────────────────────────

    def _route_agent(self, conv: Dict[str, Any]) -> str:
        """根据对话标题/内容推断参与 Agent。

        简单启发式：标题含特定关键词 → 对应 Agent。
        默认归类到 marvis-main。
        """
        title = (conv.get("title") or "").lower()

        keyword_map = {
            "file-agent": ["文件", "整理", "发票", "文档", "pdf", "excel", "word", "ppt"],
            "browser": ["网页", "浏览", "搜索", "网站", "google", "baidu"],
            "app-agent": ["应用", "app", "下载", "安装", "卸载", "微信"],
            "computer-agent": ["系统", "设置", "进程", "窗口", "桌面"],
            "search-agent": ["搜索", "调研", "论文", "对比", "深度"],
        }

        for agent_name, keywords in keyword_map.items():
            for kw in keywords:
                if kw in title:
                    return f"marvis-{agent_name}"

        return "marvis-main"

    # ── 核心扫描与同步 ────────────────────────────────────────────

    def scan_and_sync(self) -> Dict[str, Any]:
        """扫描所有用户目录，同步新对话。返回统计信息。"""
        stats = {
            "users_scanned": 0,
            "convs_scanned": 0,
            "convs_skipped": 0,
            "convs_synced": 0,
            "errors": 0,
            "new_last_sync_ts": "",
        }

        users = self.discover_users()
        stats["users_scanned"] = len(users)
        max_ts = self._state.last_sync_ts

        for user_dir in users:
            db_path = user_dir / "database" / "data.db"
            user_id = user_dir.name

            # 获取该用户上次同步时间
            since_ts = self._state.user_dirs.get(user_id, self._state.last_sync_ts)

            try:
                convs = self._get_new_conversations(db_path, since_ts)
            except Exception as e:
                logger.error("Failed to query %s: %s", db_path, e)
                stats["errors"] += 1
                continue

            for conv in convs:
                stats["convs_scanned"] += 1

                if self._should_skip(conv):
                    stats["convs_skipped"] += 1
                    continue

                try:
                    summary = self._extract_summary(db_path, conv)
                    agent_id = self._route_agent(conv)

                    self.bridge.push_raw(
                        agent_id=agent_id,
                        content=summary,
                        category="episodic",
                        tags=["marvis_conversation", "auto_sync"],
                        metadata={
                            "conversation_id": conv["conversation_id"],
                            "title": conv.get("title", "")[:200],
                            "user_id": user_id,
                            "created_at": conv.get("created_at"),
                            "updated_at": conv.get("updated_at"),
                            "status": conv.get("status"),
                            "synced_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    stats["convs_synced"] += 1

                    # 追踪已同步 ID
                    self._state.synced_conv_ids.append(conv["conversation_id"])
                    self._state.total_synced += 1

                    # 更新最大时间戳
                    updated = conv.get("updated_at", "")
                    if updated and updated > max_ts:
                        max_ts = updated

                except Exception as e:
                    logger.error("Failed to sync conv %s: %s", conv.get("conversation_id"), e)
                    stats["errors"] += 1

            # 更新该用户的同步时间
            self._state.user_dirs[user_id] = max_ts

        # 保存状态
        if max_ts and max_ts > self._state.last_sync_ts:
            self._state.last_sync_ts = max_ts
        stats["new_last_sync_ts"] = self._state.last_sync_ts
        self._save_state()

        return stats


class ConversationSyncDaemon:
    """自动同步守护进程（定时轮询模式）。"""

    def __init__(
        self,
        scanner: ConversationScanner,
        interval: int = DEFAULT_POLL_INTERVAL,
    ):
        self.scanner = scanner
        self.interval = interval
        self._running = False

    def run_forever(self) -> None:
        """启动定时轮询循环。"""
        self._running = True
        logger.info("Sync daemon started (interval=%ds)", self.interval)

        # 启动时先执行一次全量增量同步
        self._run_once()

        while self._running:
            time.sleep(self.interval)
            self._run_once()

    def stop(self) -> None:
        """停止守护进程。"""
        self._running = False
        logger.info("Sync daemon stopped")

    def _run_once(self) -> None:
        """执行一次扫描同步。"""
        try:
            stats = self.scanner.scan_and_sync()
            if stats["convs_synced"] > 0:
                logger.info(
                    "Sync cycle: scanned=%d skipped=%d synced=%d errors=%d",
                    stats["convs_scanned"],
                    stats["convs_skipped"],
                    stats["convs_synced"],
                    stats["errors"],
                )
            else:
                logger.debug("Sync cycle: no new conversations")
        except Exception as e:
            logger.error("Sync cycle failed: %s", e)


class BidirectionalSyncDaemon:
    """双向同步守护进程：正向（Agent→Trinity）+ 反向（Trinity→Agent）。

    每轮循环：
      1. 正向：扫描 Marvis 新对话 → 推送到 Trinity
      2. 反向：从 Trinity 拉取洞察 → 写入 data/trinity_insights.json
    """

    def __init__(
        self,
        scanner: Optional[ConversationScanner] = None,
        interval: int = DEFAULT_POLL_INTERVAL,
    ):
        from trinity.bridges.retrieval_bridge import InsightsWriter

        self.scanner = scanner or ConversationScanner()
        self.insights_writer = InsightsWriter()
        self.interval = interval
        self._running = False

    def run_forever(self) -> None:
        """启动双向定时轮询循环。"""
        self._running = True
        logger.info(
            "Bidirectional sync daemon started (interval=%ds)", self.interval
        )

        # 启动时先执行一次
        self._run_once()

        while self._running:
            time.sleep(self.interval)
            self._run_once()

    def stop(self) -> None:
        """停止守护进程。"""
        self._running = False
        logger.info("Bidirectional sync daemon stopped")

    def _run_once(self) -> None:
        """执行一次双向同步。"""
        # 正向：Agent → Trinity
        try:
            stats = self.scanner.scan_and_sync()
            if stats["convs_synced"] > 0:
                logger.info(
                    "[Forward] synced=%d skipped=%d",
                    stats["convs_synced"],
                    stats["convs_skipped"],
                )
        except Exception as e:
            logger.error("[Forward] scan failed: %s", e)

        # 反向：Trinity → Agent
        try:
            data = self.insights_writer.refresh()
            pool_total = data.get("pool", {}).get("total_memories", "?")
            logger.info(
                "[Reverse] insights refreshed, pool=%s memories",
                pool_total,
            )
        except Exception as e:
            logger.error("[Reverse] insights refresh failed: %s", e)
