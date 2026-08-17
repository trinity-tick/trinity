"""SQLite adapter - anchors, agent cards, A2A mixin (split from sqlite.py, 2026-08-17).

Part of the SQLiteAdapter package decomposition. Behavior identical to the
pre-split single-file implementation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import functools
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...security.crypto import get_storage_cipher, StorageCipher  # type: ignore[attr-defined]
from .._util import _safe_write

logger = logging.getLogger("trinity.adapters.sqlite")


class _AnchorsMixin:
    def upsert_anchor(self, agent_id: str, anchor_type: str,
                      content: str, version: int = 1) -> Dict[str, Any]:
        """注册或更新身份锚点（幂等：按 agent_id + anchor_type 去重）。

        Args:
            agent_id: Agent 标识。
            anchor_type: 锚点类型（identity_files/procedural_patterns/episodic_keys/value_specifications）。
            content: JSON 格式的锚点内容。
            version: 锚点版本号（更新时自增）。

        Returns:
            操作结果。
        """
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}

        now = datetime.now(timezone.utc).isoformat()
        checksum = self._compute_sha256(content)

        # 查找已有锚点
        cursor = conn.execute(
            "SELECT id, version FROM identity_anchors WHERE agent_id = ? AND anchor_type = ?",
            (agent_id, anchor_type),
        )
        existing = cursor.fetchone()

        if existing:
            anchor_id = existing["id"]
            new_version = existing["version"] + 1
            conn.execute("""
                UPDATE identity_anchors
                SET content = ?, version = ?, checksum = ?, updated_at = ?
                WHERE id = ?
            """, (content, new_version, checksum, now, anchor_id))
        else:
            anchor_id = f"anchor_{uuid.uuid4().hex[:12]}"
            conn.execute("""
                INSERT INTO identity_anchors (id, agent_id, anchor_type, content, version, checksum, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (anchor_id, agent_id, anchor_type, content, version, checksum, now, now))

        conn.commit()
        return {
            "id": anchor_id,
            "agent_id": agent_id,
            "anchor_type": anchor_type,
            "version": existing["version"] + 1 if existing else version,
            "checksum": checksum,
            "updated_at": now,
        }
    def get_anchors(self, agent_id: str,
                    anchor_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取指定 Agent 的锚点列表。

        Args:
            agent_id: Agent 标识。
            anchor_type: 锚点类型过滤（可选）。

        Returns:
            锚点列表。
        """
        conn = self._conn
        if not conn:
            return []

        if anchor_type:
            cursor = conn.execute(
                "SELECT * FROM identity_anchors WHERE agent_id = ? AND anchor_type = ? ORDER BY anchor_type, version DESC",
                (agent_id, anchor_type),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM identity_anchors WHERE agent_id = ? ORDER BY anchor_type, version DESC",
                (agent_id,),
            )
        return [dict(row) for row in cursor.fetchall()]
    def get_all_anchors(self, agent_id: str) -> Dict[str, List[Dict[str, Any]]]:
        """获取指定 Agent 按类型分组的所有锚点。

        Args:
            agent_id: Agent 标识。

        Returns:
            Dict[anchor_type, List[Dict]] 分组后的锚点。
        """
        anchors = self.get_anchors(agent_id)
        grouped: Dict[str, list] = {
            "identity_files": [],
            "procedural_patterns": [],
            "episodic_keys": [],
            "value_specifications": [],
        }
        for a in anchors:
            atype = a.get("anchor_type", "")
            if atype in grouped:
                grouped[atype].append(a)
        return grouped
    def get_latest_anchor_version(self, agent_id: str,
                                   anchor_type: str) -> Optional[Dict[str, Any]]:
        """获取指定 Agent 指定类型的最高版本锚点。

        Args:
            agent_id: Agent 标识。
            anchor_type: 锚点类型。

        Returns:
            最新锚点字典，或 None。
        """
        conn = self._conn
        if not conn:
            return None

        cursor = conn.execute(
            "SELECT * FROM identity_anchors WHERE agent_id = ? AND anchor_type = ? "
            "ORDER BY version DESC LIMIT 1",
            (agent_id, anchor_type),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    def register_agent_card(self, agent_id: str, card_json: str) -> bool:
        """注册或更新 Agent Card 到全局注册中心。"""
        conn = self._conn
        if not conn:
            return False
        try:
            conn.execute(
                "INSERT OR REPLACE INTO agent_registry "
                "(agent_id, card_json, last_heartbeat, status) "
                "VALUES (?, ?, datetime('now'), 'active')",
                (agent_id, card_json),
            )
            conn.commit()
            return True
        except Exception:
            return False
    def get_agent_card(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """获取 Agent 的注册卡片。"""
        conn = self._conn
        if not conn:
            return None
        cursor = conn.execute(
            "SELECT * FROM agent_registry WHERE agent_id = ?", (agent_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    def create_a2a_task(self, task_id: str, from_agent: str, to_agent: str,
                         payload: str, status: str = "pending",
                         result: Optional[str] = None) -> bool:
        """创建跨 Agent 任务记录。"""
        conn = self._conn
        if not conn:
            return False
        try:
            conn.execute(
                "INSERT OR REPLACE INTO a2a_tasks "
                "(task_id, from_agent, to_agent, payload, status, result) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, from_agent, to_agent, payload, status, result),
            )
            conn.commit()
            return True
        except Exception:
            return False
    def update_a2a_task(self, task_id: str, status: str,
                         result: Optional[str] = None) -> bool:
        """更新跨 Agent 任务状态。"""
        conn = self._conn
        if not conn:
            return False
        try:
            conn.execute(
                "UPDATE a2a_tasks SET status = ?, result = ?, "
                "updated_at = datetime('now') WHERE task_id = ?",
                (status, result, task_id),
            )
            conn.commit()
            return True
        except Exception:
            return False
    def list_a2a_tasks(self, task_id: Optional[str] = None,
                        agent_id: Optional[str] = None,
                        status: Optional[str] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        """列出跨 Agent 任务，支持按 agent_id / status / task_id 过滤。"""
        conn = self._conn
        if not conn:
            return []
        if task_id:
            cursor = conn.execute(
                "SELECT * FROM a2a_tasks WHERE task_id = ?", (task_id,),
            )
            return [dict(r) for r in cursor.fetchall()]
        query = "SELECT * FROM a2a_tasks WHERE 1=1"
        params: list = []
        if agent_id:
            query += " AND (from_agent = ? OR to_agent = ?)"
            params.extend([agent_id, agent_id])
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = conn.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]
    def update_agent_heartbeat(self, agent_id: str) -> bool:
        """更新 Agent 注册中心的心跳时间戳。"""
        conn = self._conn
        if not conn:
            return False
        try:
            conn.execute(
                "UPDATE agent_registry SET last_heartbeat = datetime('now') "
                "WHERE agent_id = ?",
                (agent_id,),
            )
            conn.commit()
            return True
        except Exception:
            return False
