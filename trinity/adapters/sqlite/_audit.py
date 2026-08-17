"""SQLite adapter - audit log, GDPR & audit trails mixin (split from sqlite.py, 2026-08-17).

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


class _AuditMixin:
    @_safe_write
    def write_audit_log(self, memory_id: str = None, action: str = "",
                         agent_id: str = None, persona_id: str = None,
                         details: dict = None) -> None:
        """向 audit_log 表写入一条审计记录（链式 SHA-256 防篡改）。

        每条记录的 checksum = SHA-256(本条数据 JSON + 前一条记录的 checksum)。

        2026-08-15（压测修复）：加 _write_lock —— search_hybrid 并发路径
        每查询写审计，单连接必须串行化（SELECT prev + INSERT + commit）。
        """
        with self._write_lock:
            conn = self._conn
            if not conn:
                return
            audit_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            details_json = json.dumps(details or {}, ensure_ascii=False)

            # 获取上一条审计记录的 checksum 用于链式哈希
            prev_checksum = ""
            cursor = conn.execute(
                "SELECT checksum FROM audit_log ORDER BY timestamp DESC, id DESC LIMIT 1"
            )
            prev_row = cursor.fetchone()
            if prev_row and prev_row["checksum"]:
                prev_checksum = prev_row["checksum"]

            # 计算链式哈希
            payload = json.dumps({
                "id": audit_id,
                "memory_id": memory_id,
                "action": action,
                "agent_id": agent_id,
                "persona_id": persona_id,
                "timestamp": now,
                "details": details,
                "prev_checksum": prev_checksum,
            }, sort_keys=True, ensure_ascii=False)
            chain_checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()

            conn.execute("""
                INSERT INTO audit_log (id, memory_id, action, agent_id, persona_id, timestamp, details, checksum)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                audit_id, memory_id, action, agent_id, persona_id, now,
                details_json, chain_checksum,
            ))
            conn.commit()  # 立即提交，否则每次 search 都会挂一个未提交写事务，永久占用库锁（database is locked）
    def _write_audit_log(self, action: str, memory_id: str = None,
                          persona_id: str = None, content_hash: str = None,
                          metadata: dict = None) -> None:
        """向后兼容转发到新的 write_audit_log。"""
        self.write_audit_log(
            memory_id=memory_id,
            action=action,
            persona_id=persona_id,
            details=metadata or {},
        )
    def export_user_data(self, persona_id: str) -> Optional[str]:
        """导出指定 persona 的所有记忆数据为 JSON 字符串。

        Args:
            persona_id: 用户标识

        Returns:
            JSON 格式的字符串，包含用户所有记忆及关联信息
        """
        conn = self._conn
        if not conn:
            return None

        # 获取用户所有记忆
        memories = self.get_persona_memories(persona_id, limit=999999)

        # 获取用户信息
        cursor = conn.execute(
            "SELECT * FROM personas WHERE persona_id = ?", (persona_id,)
        )
        persona_row = cursor.fetchone()
        persona_info = dict(persona_row) if persona_row else {"persona_id": persona_id}

        # 获取审计日志
        cursor = conn.execute(
            "SELECT action, memory_id, timestamp FROM audit_log WHERE persona_id = ? ORDER BY timestamp",
            (persona_id,)
        )
        audit_entries = [dict(row) for row in cursor.fetchall()]

        export_data = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "persona": persona_info,
            "memories_count": len(memories),
            "memories": [
                {
                    "memory_id": m.get("memory_id"),
                    "content": m.get("content"),
                    "role": m.get("role"),
                    "importance": m.get("importance"),
                    "tags": json.loads(m.get("tags", "[]")) if isinstance(m.get("tags"), str) else m.get("tags", []),
                    "category": m.get("category"),
                    "created_at": m.get("created_at"),
                    "updated_at": m.get("updated_at"),
                }
                for m in memories
            ],
            "audit_log": audit_entries,
        }

        self._write_audit_log("EXPORT_USER_DATA", persona_id=persona_id,
                              metadata={"memories_count": len(memories)})
        self._conn.commit()

        return json.dumps(export_data, ensure_ascii=False, indent=2)
    def forget_user(self, persona_id: str) -> Dict[str, Any]:
        """GDPR 被遗忘权 — 级联匿名化/删除指定用户的所有关联记录。

        执行操作：
        1. 记忆内容匿名化为 '[GDPR erased]'
        2. 记忆状态标记为 'gdpr_deleted'
        3. memory_versions 内容同法处理
        4. 写入审计日志

        Args:
            persona_id: 要遗忘的用户标识

        Returns:
            操作结果统计
        """
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}

        # 1. 统计受影响记录数
        cursor = conn.execute(
            "SELECT COUNT(*) as c FROM memories WHERE persona_id = ?",
            (persona_id,)
        )
        memories_count = cursor.fetchone()["c"]

        cursor = conn.execute(
            "SELECT COUNT(*) as c FROM memory_versions mv "
            "WHERE mv.memory_id IN (SELECT memory_id FROM memories WHERE persona_id = ?)",
            (persona_id,)
        )
        versions_count = cursor.fetchone()["c"]

        # 2. 匿名化 memories
        conn.execute("""
            UPDATE memories SET
                content = '[GDPR erased]',
                sha256_hash = NULL,
                status = 'gdpr_deleted',
                updated_at = datetime('now')
            WHERE persona_id = ?
        """, (persona_id,))

        # 3. 匿名化 memory_versions
        conn.execute("""
            UPDATE memory_versions SET
                content = '[GDPR erased]',
                sha256_hash = NULL
            WHERE memory_id IN (
                SELECT memory_id FROM memories WHERE persona_id = ?
            )
        """, (persona_id,))

        # 4. 写入审计日志
        self._write_audit_log("FORGET_USER", persona_id=persona_id,
                              metadata={
                                  "memories_erased": memories_count,
                                  "versions_erased": versions_count,
                              })
        conn.commit()

        return {
            "persona_id": persona_id,
            "memories_erased": memories_count,
            "versions_erased": versions_count,
            "status": "GDPR forgotten",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    def get_audit_trail(self, memory_id: str) -> List[Dict[str, Any]]:
        """查看某条记忆的完整变更历史（按时间排序）。"""
        conn = self._conn
        if not conn:
            return []
        cursor = conn.execute("""
            SELECT id, memory_id, action, agent_id, persona_id,
                   timestamp, details, checksum
            FROM audit_log
            WHERE memory_id = ?
            ORDER BY timestamp ASC, id ASC
        """, (memory_id,))
        results = []
        for row in cursor.fetchall():
            d = dict(row)
            d["details"] = json.loads(d.get("details", "{}"))
            results.append(d)
        return results
    def replay_agent_session(self, agent_id: str,
                              start_time: str = None,
                              end_time: str = None) -> List[Dict[str, Any]]:
        """回放某 Agent 在时间段内的所有操作（按时间排序）。

        Args:
            agent_id: Agent 标识。
            start_time: ISO 格式起始时间（可选）。
            end_time: ISO 格式结束时间（可选）。

        Returns:
            操作列表，按时间升序。
        """
        conn = self._conn
        if not conn:
            return []
        query = """
            SELECT id, memory_id, action, agent_id, persona_id,
                   timestamp, details, checksum
            FROM audit_log
            WHERE agent_id = ?
        """
        params: list = [agent_id]
        if start_time:
            query += " AND timestamp >= ?"
            params.append(start_time)
        if end_time:
            query += " AND timestamp <= ?"
            params.append(end_time)
        query += " ORDER BY timestamp ASC, id ASC"
        cursor = conn.execute(query, params)
        results = []
        for row in cursor.fetchall():
            d = dict(row)
            d["details"] = json.loads(d.get("details", "{}"))
            results.append(d)
        return results
    def verify_audit_integrity(self) -> Dict[str, Any]:
        """验证审计链完整性：遍历全链重新计算 checksum，检测篡改。

        Returns:
            {"integrity_ok": bool, "total_entries": int, "tampered": list, "details": str}
        """
        conn = self._conn
        if not conn:
            return {"integrity_ok": False, "error": "Not connected"}

        cursor = conn.execute(
            "SELECT id, memory_id, action, agent_id, persona_id, "
            "timestamp, details, checksum "
            "FROM audit_log ORDER BY timestamp ASC, id ASC"
        )
        entries = cursor.fetchall()
        if not entries:
            return {"integrity_ok": True, "total_entries": 0, "tampered": [], "details": "审计日志为空"}

        tampered = []
        prev_checksum = ""
        for row in entries:
            d = dict(row)
            payload = json.dumps({
                "id": d["id"],
                "memory_id": d.get("memory_id"),
                "action": d["action"],
                "agent_id": d.get("agent_id"),
                "persona_id": d.get("persona_id"),
                "timestamp": d["timestamp"],
                "details": json.loads(d.get("details", "{}")),
                "prev_checksum": prev_checksum,
            }, sort_keys=True, ensure_ascii=False)
            expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if expected != d["checksum"]:
                tampered.append({
                    "id": d["id"],
                    "expected": expected,
                    "actual": d["checksum"],
                })
            prev_checksum = d["checksum"]

        return {
            "integrity_ok": len(tampered) == 0,
            "total_entries": len(entries),
            "tampered_count": len(tampered),
            "tampered": tampered,
            "details": "所有审计记录完整一致" if len(tampered) == 0
                        else f"发现 {len(tampered)} 条记录校验和不匹配，可能存在篡改",
        }
    def get_audit_summary(self, start_time: str = None,
                           end_time: str = None) -> Dict[str, Any]:
        """审计摘要：各操作计数、活跃 Agent、操作峰值时段。

        Args:
            start_time: ISO 格式起始时间（可选）。
            end_time: ISO 格式结束时间（可选）。

        Returns:
            Dict with total_entries, action_counts, active_agents,
                 active_personas, peak_hour, time_range.
        """
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}

        query_base = "FROM audit_log WHERE 1=1"
        params: list = []
        if start_time:
            query_base += " AND timestamp >= ?"
            params.append(start_time)
        if end_time:
            query_base += " AND timestamp <= ?"
            params.append(end_time)

        # 总条目
        cursor = conn.execute(f"SELECT COUNT(*) as c {query_base}", params)
        total = cursor.fetchone()["c"]

        # 各操作计数
        cursor = conn.execute(
            f"SELECT action, COUNT(*) as c {query_base} GROUP BY action ORDER BY c DESC",
            params,
        )
        action_counts = {row["action"]: row["c"] for row in cursor.fetchall()}

        # 活跃 Agent
        cursor = conn.execute(
            f"SELECT agent_id, COUNT(*) as c {query_base}"
            f" AND agent_id IS NOT NULL GROUP BY agent_id ORDER BY c DESC",
            params,
        )
        active_agents = {row["agent_id"]: row["c"] for row in cursor.fetchall()}

        # 活跃 Persona
        cursor = conn.execute(
            f"SELECT persona_id, COUNT(*) as c {query_base}"
            f" AND persona_id IS NOT NULL GROUP BY persona_id ORDER BY c DESC",
            params,
        )
        active_personas = {row["persona_id"]: row["c"] for row in cursor.fetchall()}

        # 峰值时段（按小时聚合）
        cursor = conn.execute(
            f"SELECT SUBSTR(timestamp, 1, 13) as hour_bucket, COUNT(*) as c "
            f"{query_base} GROUP BY hour_bucket ORDER BY c DESC LIMIT 5",
            params,
        )
        peak_hours = [{"hour": row["hour_bucket"], "count": row["c"]} for row in cursor.fetchall()]

        return {
            "total_entries": total,
            "action_counts": action_counts,
            "active_agents": active_agents,
            "active_personas": active_personas,
            "peak_hours": peak_hours,
            "time_range": {"start": start_time, "end": end_time},
        }
    def log_audit_run(self, run_id: str, agent_id: str, task: str,
                       executor_result: str, auditor_result: str,
                       disagreement_flag: bool = False,
                       packet_json: str = "{}") -> bool:
        conn = self._conn
        if not conn:
            return False
        try:
            conn.execute(
                "INSERT OR REPLACE INTO audit_runs "
                "(run_id, agent_id, task, executor_result, auditor_result, disagreement_flag, packet_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, agent_id, task, executor_result, auditor_result,
                 1 if disagreement_flag else 0, packet_json),
            )
            conn.commit()
            return True
        except Exception:
            return False
    def log_constitutional_violation(self, run_id: str, invariant: str,
                                      severity: str, context: str = "{}") -> bool:
        import uuid as _uuid
        conn = self._conn
        if not conn:
            return False
        try:
            conn.execute(
                "INSERT INTO constitutional_violations "
                "(violation_id, run_id, invariant, severity, context) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"cv_{_uuid.uuid4().hex[:12]}", run_id, invariant, severity, context),
            )
            conn.commit()
            return True
        except Exception:
            return False
    def get_audit_history(self, agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self._conn
        if not conn:
            return []
        cursor = conn.execute(
            "SELECT * FROM audit_runs WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, limit),
        )
        return [dict(r) for r in cursor.fetchall()]
    def get_audit_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        conn = self._conn
        if not conn:
            return None
        cursor = conn.execute("SELECT * FROM audit_runs WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        if not row:
            return None
        result = dict(row)
        # Also fetch violations
        cv_cursor = conn.execute(
            "SELECT * FROM constitutional_violations WHERE run_id = ? ORDER BY timestamp",
            (run_id,),
        )
        result["violations"] = [dict(r) for r in cv_cursor.fetchall()]
        return result
    def get_violation_trends(self, agent_id: Optional[str] = None,
                              limit: int = 100) -> List[Dict[str, Any]]:
        conn = self._conn
        if not conn:
            return []
        if agent_id:
            cursor = conn.execute(
                "SELECT cv.*, ar.agent_id FROM constitutional_violations cv "
                "JOIN audit_runs ar ON cv.run_id = ar.run_id "
                "WHERE ar.agent_id = ? ORDER BY cv.timestamp DESC LIMIT ?",
                (agent_id, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT cv.*, ar.agent_id FROM constitutional_violations cv "
                "JOIN audit_runs ar ON cv.run_id = ar.run_id "
                "ORDER BY cv.timestamp DESC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in cursor.fetchall()]
