"""SQLite adapter - stats, conflicts, agent weights mixin (split from sqlite.py, 2026-08-17).

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


class _StatsMixin:
    def get_memory_stats(self) -> Dict[str, Any]:
        """返回记忆统计信息（总数、过期数、Agent 分布、平均访问频率等）。

        Returns:
            Stats dict.
        """
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}

        cursor = conn.execute("SELECT COUNT(*) as c FROM memories WHERE status = 'active'")
        active = cursor.fetchone()["c"]

        cursor = conn.execute("SELECT COUNT(*) as c FROM memories WHERE status = 'expired'")
        expired = cursor.fetchone()["c"]

        cursor = conn.execute("SELECT COUNT(*) as c FROM memories")
        total = cursor.fetchone()["c"]

        # TTL 到期的活跃记忆数
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute("""
            SELECT COUNT(*) as c FROM memories
            WHERE status = 'active'
              AND ttl_seconds IS NOT NULL
              AND created_at IS NOT NULL
              AND datetime(created_at, '+' || ttl_seconds || ' seconds') < datetime(?)
        """, (now,))
        due_expired = cursor.fetchone()["c"]

        # 各 Agent 记忆量分布
        cursor = conn.execute("""
            SELECT agent_id, COUNT(*) as cnt FROM memories
            WHERE status = 'active'
            GROUP BY agent_id
            ORDER BY cnt DESC
        """)
        agent_distribution = {row["agent_id"]: row["cnt"] for row in cursor.fetchall()}

        # 平均访问频率
        cursor = conn.execute("""
            SELECT AVG(access_count) as avg_access FROM memories WHERE status = 'active'
        """)
        avg_access = cursor.fetchone()["avg_access"] or 0

        return {
            "total_memories": total,
            "active_memories": active,
            "expired_memories": expired,
            "due_expired": due_expired,
            "agent_distribution": agent_distribution,
            "avg_access_count": round(avg_access, 2),
        }
    def get_modality_stats(self) -> Dict[str, Any]:
        """返回各模态记忆数量、存储占比统计。

        Returns:
            Dict with modality counts, percentages, and total.
        """
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}

        cursor = conn.execute("SELECT COUNT(*) as c FROM memories WHERE status = 'active'")
        total = cursor.fetchone()["c"]

        cursor = conn.execute("""
            SELECT modality, COUNT(*) as cnt
            FROM memories
            WHERE status = 'active'
            GROUP BY modality
            ORDER BY cnt DESC
        """)
        distribution = {row["modality"]: row["cnt"] for row in cursor.fetchall()}

        return {
            "total_active": total,
            "modalities": distribution,
            "percentages": {
                m: round(c / total * 100, 2) if total > 0 else 0.0
                for m, c in distribution.items()
            },
        }
    def check_content_hash_collision(
        self, persona_id: str, agent_id: str, content_hash: str
    ) -> Optional[Dict[str, Any]]:
        """检查同一 persona+agent 下是否已存在相同 content_hash 的记忆。

        Returns:
            如果存在冲突记忆则返回其信息，否则返回 None。
        """
        conn = self._conn
        if not conn:
            return None
        cursor = conn.execute("""
            SELECT memory_id, content, conflict_group_id, is_resolved,
                   created_at, status
            FROM memories
            WHERE persona_id = ? AND agent_id = ?
              AND content_hash = ? AND status = 'active'
            LIMIT 1
        """, (persona_id, agent_id, content_hash))
        row = cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("content"):
            d["content"] = self._decrypt_content(d["content"])
        return d
    def get_conflicts(
        self, memory_id: str
    ) -> Dict[str, Any]:
        """查看指定记忆的冲突链（同一 conflict_group_id 的所有版本）。

        Args:
            memory_id: 记忆 ID。

        Returns:
            冲突链信息，包含 conflict_group_id 与所有冲突版本列表。
        """
        conn = self._conn
        if not conn:
            return {"memory_id": memory_id, "conflicts": [], "error": "Not connected"}

        # 先查该记忆的 conflict_group_id
        cursor = conn.execute("""
            SELECT conflict_group_id FROM memories WHERE memory_id = ?
        """, (memory_id,))
        row = cursor.fetchone()
        if not row or not row["conflict_group_id"]:
            return {"memory_id": memory_id, "conflicts": [], "conflict_group_id": None}

        conflict_group_id = row["conflict_group_id"]
        cursor = conn.execute("""
            SELECT memory_id, content, content_hash, is_resolved,
                   created_at, updated_at, status
            FROM memories
            WHERE conflict_group_id = ?
            ORDER BY created_at ASC
        """, (conflict_group_id,))
        conflicts = []
        for r in cursor.fetchall():
            d = dict(r)
            if d.get("content"):
                d["content"] = self._decrypt_content(d["content"])
            conflicts.append(d)

        return {
            "memory_id": memory_id,
            "conflict_group_id": conflict_group_id,
            "conflicts": conflicts,
        }
    def resolve_conflict(
        self,
        conflict_group_id: str,
        keep_memory_id: str,
    ) -> Dict[str, Any]:
        """解决冲突：保留选定版本，软删除同一冲突组的其他版本。

        Args:
            conflict_group_id: 冲突组 ID。
            keep_memory_id: 保留的记忆 ID。

        Returns:
            操作结果，含 resolved_count 与 discarded_ids。
        """
        with self._write_lock:

            conn = self._conn
            if not conn:
                return {"error": "Not connected", "resolved_count": 0}

            now = datetime.now(timezone.utc).isoformat()

            # 标记保留版本为已解决
            conn.execute("""
                UPDATE memories SET is_resolved = 1, updated_at = ?
                WHERE memory_id = ? AND conflict_group_id = ?
            """, (now, keep_memory_id, conflict_group_id))

            # 软删除同组其他活跃版本
            cursor = conn.execute("""
                SELECT memory_id FROM memories
                WHERE conflict_group_id = ?
                  AND memory_id != ?
                  AND status = 'active'
            """, (conflict_group_id, keep_memory_id))
            discard_ids = [r["memory_id"] for r in cursor.fetchall()]

            if discard_ids:
                placeholders = ",".join("?" for _ in discard_ids)
                conn.execute(f"""
                    UPDATE memories SET status = 'expired', is_resolved = 1, updated_at = ?
                    WHERE memory_id IN ({placeholders})
                """, [now] + discard_ids)

            conn.commit()

            return {
                "conflict_group_id": conflict_group_id,
                "kept_memory_id": keep_memory_id,
                "discarded_ids": discard_ids,
                "resolved_count": len(discard_ids),
            }
    def dedup_stats(self) -> Dict[str, Any]:
        """返回去重统计信息。

        Returns:
            Dict with duplicate_groups, total_conflicts, resolved_conflicts.
        """
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}

        cursor = conn.execute("""
            SELECT COUNT(*) as c FROM memories
            WHERE conflict_group_id IS NOT NULL
        """)
        total_in_conflicts = cursor.fetchone()["c"]

        cursor = conn.execute("""
            SELECT COUNT(DISTINCT conflict_group_id) as c FROM memories
            WHERE conflict_group_id IS NOT NULL
        """)
        conflict_groups = cursor.fetchone()["c"]

        cursor = conn.execute("""
            SELECT COUNT(*) as c FROM memories
            WHERE conflict_group_id IS NOT NULL AND is_resolved = 1
        """)
        resolved = cursor.fetchone()["c"]

        cursor = conn.execute("""
            SELECT COUNT(DISTINCT content_hash) as c FROM memories
            WHERE content_hash IS NOT NULL AND status = 'active'
        """)
        unique_hashes = cursor.fetchone()["c"]

        return {
            "total_in_conflict_groups": total_in_conflicts,
            "conflict_groups": conflict_groups,
            "resolved_conflicts": resolved,
            "unique_content_hashes": unique_hashes,
        }
    def set_agent_weight(self, agent_id: str, weight: float) -> Dict[str, Any]:
        """设置 Agent 的检索权重。

        Args:
            agent_id: Agent 标识。
            weight: 权重值（建议 0.1-2.0）。

        Returns:
            操作结果。
        """
        with self._write_lock:

            conn = self._conn
            if not conn:
                return {"error": "Not connected"}
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("""
                INSERT INTO agent_weights (agent_id, weight, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at
            """, (agent_id, weight, now))
            conn.commit()
            return {"agent_id": agent_id, "weight": weight, "updated_at": now}
    def get_agent_weights(self) -> Dict[str, float]:
        """获取所有 Agent 权重配置。

        Returns:
            Dict[agent_id, weight]
        """
        conn = self._conn
        if not conn:
            return {}
        cursor = conn.execute("SELECT agent_id, weight FROM agent_weights")
        return {row["agent_id"]: row["weight"] for row in cursor.fetchall()}
    def delete_agent_weight(self, agent_id: str) -> bool:
        """删除 Agent 权重配置。

        Args:
            agent_id: Agent 标识。

        Returns:
            是否删除成功。
        """
        conn = self._conn
        if not conn:
            return False
        cursor = conn.execute("DELETE FROM agent_weights WHERE agent_id = ?", (agent_id,))
        conn.commit()
        return cursor.rowcount > 0
