"""SQLite adapter - memory CRUD & lifecycle mixin (split from sqlite.py, 2026-08-17).

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

# 2026-08-18（conflict 检测改进，agent-memory-bench 对齐）：
# 写入时相似性冲突检测的相似度阈值（search_memories 的 FTS5 归一化 score）。
# 可经 TRINITY_CONFLICT_SIM_THRESHOLD 覆盖；TRINITY_CONFLICT_DETECT=off 可关闭。
CONFLICT_SIM_THRESHOLD = 0.5  # 保留（兼容）；实际用 token 重叠
CONFLICT_TOKEN_OVERLAP = 0.6  # jieba token 集合重叠率阈值（conflict 检测）


class _CrudMixin:
    @_safe_write
    def store_memory(
        self,
        content: str,
        persona_id: str = "default",
        session_id: Optional[str] = None,
        tenant_id: str = "default",
        agent_id: str = "default",
        app_id: Optional[str] = None,
        role: str = "user",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        category: str = "general",
        memory_layer: Optional[str] = None,
        auto_redact_pii: bool = False,
        ttl_seconds: Optional[int] = None,
        modality: str = "text",
        metadata: Optional[Dict[str, Any]] = None,
        source_uri: Optional[str] = None,
    ) -> Dict[str, Any]:
        # 2026-08-24（R9 P0-1）：只读模式（建表/迁移写锁失败降级）——
        # 写操作明确报错，而不是静默失败或抛 database is locked 裸异常。
        if getattr(self, "_readonly_mode", False):
            return {"memory_id": "", "error": "readonly mode (schema init failed due to write lock)"}
        with self._write_lock:

            conn = self._conn
            if not conn:
                raise RuntimeError("Not connected. Call connect() first.")

            memory_id = f"mem_{uuid.uuid4().hex[:16]}"
            version_id = f"ver_{uuid.uuid4().hex[:12]}"
            if not session_id:
                session_id = f"sess_{uuid.uuid4().hex[:12]}"
            tags_json = json.dumps(tags or [])
            now = datetime.now(timezone.utc).isoformat()

            # ── PII 检测与脱敏 ──────────────────────────────────────────
            pii_info = None
            stored_content = content
            if auto_redact_pii:
                result = self._detect_pii(content)
                stored_content = result["redacted"]
                pii_found = {k: v for k, v in result["found"].items() if v}
                if pii_found:
                    pii_info = pii_found

            sha256_hash = self._compute_sha256(stored_content)
            # 2026-08-25（核心测试发现修复）：content_hash+persona+agent 幂等去重——
            # 同内容重复 ingest 返回现有 memory_id（CRDT 幂等语义），
            # 此前 UNIQUE 约束直接抛 IntegrityError。
            try:
                _dup = self._conn.execute(
                    "SELECT memory_id FROM memories WHERE content_hash=? "
                    "AND persona_id=? AND agent_id=? AND status='active' LIMIT 1",
                    (sha256_hash, persona_id, agent_id),
                ).fetchone()
                if _dup:
                    return {"memory_id": _dup["memory_id"], "version_id": None,
                            "sha256_hash": sha256_hash, "dedup": True,
                            "timestamp": now, "pushed_memories": []}
            except Exception:
                pass
            tokenized = self._tokenize_content_for_fts(stored_content)
            plain_content = stored_content  # 明文副本（加密前的原始文本）
            # B5 存储加密：content 列写密文；tokenized 明文；hash 基于明文
            stored_content = self._encrypt_content(stored_content)
            tokenized = self._tokenized_for_storage(plain_content, tokenized)

            self._batch_buffer.append({
                "memory_id": memory_id,
                "session_id": session_id,
                "persona_id": persona_id,
                "tenant_id": tenant_id,
                "agent_id": agent_id,
                "app_id": app_id,
                "content": stored_content,
                "role": role,
                "importance": importance,
                "tags_json": tags_json,
                "category": category,
                "memory_layer": memory_layer,
                "sha256_hash": sha256_hash,
                "now": now,
                "ttl_seconds": ttl_seconds,
                "modality": modality,
                "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
                "source_uri": source_uri,
            })

            conn.execute("""
                INSERT INTO memories
                (memory_id, session_id, persona_id, tenant_id, agent_id, app_id, content,
                 tokenized_content, role,
                 importance, tags, category, memory_layer, sha256_hash, status, version,
                 ttl_seconds, last_accessed_at, access_count, importance_score,
                 content_hash, conflict_group_id, is_resolved,
                 modality, metadata, source_uri,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, 0, 0.0,
                        ?, NULL, 0, ?, ?, ?, ?, ?)
            """, (memory_id, session_id, persona_id, tenant_id, agent_id, app_id, stored_content,
                  tokenized, role,
                  importance, tags_json, category, memory_layer, sha256_hash, ttl_seconds, now,
                  sha256_hash, modality, json.dumps(metadata or {}, ensure_ascii=False),
                  source_uri, now, now))

            conn.execute("""
                INSERT INTO memory_versions
                (version_id, memory_id, content, sha256_hash, operation, created_at)
                VALUES (?, ?, ?, ?, 'CREATE', ?)
            """, (version_id, memory_id, stored_content, sha256_hash, now))

            # ── 审计日志（只写一次） ────────────────────────────────────
            self._write_audit_log(
                action="STORE_MEMORY",
                memory_id=memory_id,
                persona_id=persona_id,
                content_hash=sha256_hash,
                metadata={
                    "has_pii": pii_info is not None,
                    "pii_types": list(pii_info.keys()) if pii_info else [],
                    "auto_redacted": auto_redact_pii,
                    "session_id": session_id,
                },
            )

            # 批量提交管理：加入缓冲区，达到条件再 commit
            self._maybe_flush()

            # 2026-08-18（conflict 检测改进）：写入后相似性冲突检测——
            # 高相似但内容不同的旧记忆自动分配 conflict_group_id（候选冲突组）。
            # 2026-08-24（R8 P1-5 修复）：传 plain_content（明文）而非
            # 加密后的 stored_content——密文 base64 分词与明文候选零重叠，
            # 加密默认开启后冲突检测曾整体失效。
            if os.environ.get("TRINITY_CONFLICT_DETECT", "on") != "off":
                try:
                    self._assign_conflicts(memory_id, plain_content)
                except Exception:
                    pass

            return {
                "memory_id": memory_id,
                "version_id": version_id,
                "sha256_hash": sha256_hash,
                "timestamp": now,
                "persona_id": persona_id,
                "session_id": session_id,
                "app_id": app_id,
                "auto_redacted": auto_redact_pii and pii_info is not None,
                "pii_redacted_types": list(pii_info.keys()) if pii_info else [],
            }
    def _assign_conflicts(self, new_memory_id: str, content: str) -> int:
        """2026-08-18（agent-memory-bench conflict 模式对齐）：写入后检测
        高相似但内容不同的旧记忆，分配相同 conflict_group_id（is_resolved=0）。

        相似度用 jieba 分词 token 集合重叠率（FTS5 BM25 对"语义相近但关键
        信息不同"的矛盾记忆给分过低，不适合做矛盾检测——如"端口是 5432"
        vs "端口是 5430" 只共享前缀词，BM25 分数 ~0）。

        Returns:
            分配的冲突组数量。
        """
        try:
            # 2026-08-21（性能修复）：召回查询截断——_search_fts 会把 query 逐词
            # 拼成 "词"* OR MATCH，超长中文 content 会切出数千词条导致单次 ingest
            # 冲突检测达分钟级（benchmark ingest 卡死根因）。冲突检测只需召回
            # 候选（token 重叠判断在下方用完整 content 计算），前缀截断语义不变；
            # TRINITY_CONFLICT_QUERY_MAX=0 可关闭召回（跳过冲突检测查询）。
            qmax = int(os.environ.get("TRINITY_CONFLICT_QUERY_MAX", "300"))
            recall_query = content if qmax <= 0 else content[:qmax]
            hits = self.search_memories(query=recall_query, top_k=10, touch=False)  # 候选召回（放宽）
        except Exception:
            return 0
        new_tokens = self._token_set(content)
        overlap_threshold = float(
            os.environ.get("TRINITY_CONFLICT_OVERLAP", CONFLICT_TOKEN_OVERLAP))
        assigned = 0
        for r in hits:
            mid = r.get("memory_id")
            if not mid or mid == new_memory_id:
                continue
            old_content = str(r.get("content", ""))
            if old_content == content:
                continue  # 完全相同内容（唯一约束已挡，防御）
            old_tokens = self._token_set(old_content)
            if not new_tokens or not old_tokens:
                continue
            inter = len(new_tokens & old_tokens)
            overlap = inter / max(len(new_tokens), len(old_tokens))
            if overlap >= overlap_threshold:
                group = "conf_" + hashlib.md5(
                    "|".join(sorted([new_memory_id, mid])).encode()
                ).hexdigest()[:12]
                with self._write_lock:
                    self._conn.execute(
                        "UPDATE memories SET conflict_group_id=?, is_resolved=0 "
                        "WHERE memory_id IN (?, ?)",
                        (group, new_memory_id, mid),
                    )
                    self._conn.commit()
                assigned += 1
        return assigned

    @staticmethod
    def _token_set(text: str):
        """jieba 分词 + 去空白，返回 token 集合（用于冲突相似度）。

        2026-08-21（性能修复）：只对前 TRINITY_CONFLICT_TOKEN_MAX（默认 2000）
        字符分词——超长文本（数万字会话）全量 jieba.cut 单次可达数秒，而
        冲突检测每次 ingest 要算 1 次新内容 + 每条候选（top_k=10），叠加成
        分钟级。冲突检测的语义是"主题级高重叠"，前 2000 字符已代表主题；
        短文本（<2000 字符）行为完全不变。
        """
        try:
            import jieba
            tmax = int(os.environ.get("TRINITY_CONFLICT_TOKEN_MAX", "2000"))
            src = str(text)
            if tmax > 0 and len(src) > tmax:
                src = src[:tmax]
            tokens = [t.strip() for t in jieba.cut(src) if t.strip()]
        except Exception:
            tokens = [t.strip() for t in re.split(r"[\s,，。；;：:、]+", str(text)) if t.strip()]
        return set(tokens)

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """查询单条记忆（2026-08-15 v2：线程本地只读连接）。"""
        conn = self._get_read_conn()
        if not conn:
            return None

        cursor = conn.execute(
            "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("content"):
            d["content"] = self._decrypt_content(d["content"])
        return d
    def get_memory_owners(self, memory_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量查询记忆的归属与状态（hybrid 检索隔离后过滤用）。

        返回 {memory_id: {status, agent_id, persona_id, tenant_id}}；
        不在库中的 id 不出现（调用方据此区分"池记忆/幽灵"）。

        2026-08-15（压测修复 v2）：线程本地只读连接（纯读，无锁）。
        """
        if not memory_ids:
            return {}
        conn = self._get_read_conn()
        if not conn:
            return {}
        placeholders = ",".join("?" * len(memory_ids))
        rows = conn.execute(
            f"SELECT memory_id, status, agent_id, persona_id, tenant_id "
            f"FROM memories WHERE memory_id IN ({placeholders})",
            list(memory_ids),
        ).fetchall()
        return {
            r["memory_id"]: {
                "status": r["status"],
                "agent_id": r["agent_id"],
                "persona_id": r["persona_id"],
                "tenant_id": r["tenant_id"],
            }
            for r in rows
        }
    def get_persona_memories(
        self, persona_id: str, agent_id: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        conn = self._conn
        if not conn:
            return []

        if agent_id:
            cursor = conn.execute("""
                SELECT * FROM memories
                WHERE persona_id = ? AND agent_id = ? AND status = 'active'
                ORDER BY created_at DESC
                LIMIT ?
            """, (persona_id, agent_id, limit))
        else:
            cursor = conn.execute("""
                SELECT * FROM memories
                WHERE persona_id = ? AND status = 'active'
                ORDER BY created_at DESC
                LIMIT ?
            """, (persona_id, limit))
        rows = [dict(row) for row in cursor.fetchall()]
        for r in rows:
            if r.get("content"):
                r["content"] = self._decrypt_content(r["content"])
        return rows
    @_safe_write
    def delete_memory(self, memory_id: str) -> bool:
        with self._write_lock:

            conn = self._conn
            if not conn:
                return False

            cursor = conn.execute(
                "SELECT memory_id, persona_id, sha256_hash FROM memories WHERE memory_id = ?",
                (memory_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False

            persona_id = row["persona_id"]
            content_hash = row["sha256_hash"]

            conn.execute(
                "UPDATE memories SET status = 'deleted', updated_at = datetime('now') WHERE memory_id = ?",
                (memory_id,)
            )
            conn.execute("""
                INSERT INTO memory_versions (version_id, memory_id, content, sha256_hash, operation, created_at)
                SELECT ? || '_del', memory_id, content, sha256_hash, 'DELETE', datetime('now')
                FROM memories WHERE memory_id = ?
            """, (memory_id, memory_id))

            # ── 审计日志 ────────────────────────────────────────────────
            self._write_audit_log(
                action="DELETE_MEMORY",
                memory_id=memory_id,
                persona_id=persona_id,
                content_hash=content_hash,
            )
            conn.commit()
            return True
    @_safe_write
    def purge_memory(self, memory_id: str, reason: str = "") -> Dict[str, Any]:
        """GDPR 硬擦除（2026-09-02, Fable 对照审计 P2-⑤⑦）。覆写销毁+行保留。"""
        with self._write_lock:
            conn = self._conn
            if not conn:
                return {"memory_id": memory_id, "purged": False, "error": "no conn"}
            row = conn.execute(
                "SELECT memory_id, persona_id, sha256_hash FROM memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if not row:
                return {"memory_id": memory_id, "purged": False, "error": "not_found"}
            prior_hash = row["sha256_hash"]
            persona_id = row["persona_id"]
            now = datetime.now(timezone.utc).isoformat()
            sentinel = "[HARD_PURGED %s] %s" % (now, memory_id)
            meta = json.dumps({"hard_purged": True, "purged_at": now,
                               "reason": (reason or "")[:200]}, ensure_ascii=False)
            enc = self._encrypt_content(sentinel)
            sent_hash = self._compute_sha256(sentinel)
            conn.execute(
                "UPDATE memories SET content = ?, tokenized_content = ?,"
                " status = 'gdpr_deleted', sha256_hash = ?, content_hash = ?,"
                " metadata = ?, importance = 0, updated_at = ? WHERE memory_id = ?",
                (enc, sentinel, sent_hash, sent_hash, meta, now, memory_id),
            )
            conn.execute(
                "UPDATE memory_versions SET content = ? WHERE memory_id = ?",
                (sentinel, memory_id),
            )
            try:
                conn.execute(
                    "DELETE FROM memory_links WHERE memory_id = ?"
                    " OR from_memory_id = ? OR to_memory_id = ?",
                    (memory_id, memory_id, memory_id),
                )
            except Exception:
                pass
            conn.commit()
            return {"memory_id": memory_id, "purged": True,
                    "prior_sha256": prior_hash, "status": "gdpr_deleted",
                    "persona_id": persona_id}
    @_safe_write
    def update_memory(
        self,
        memory_id: str,
        content: Optional[str] = None,
        importance: Optional[float] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update an existing memory with version tracking (conflict-preserving).

        Old version rows stay in memory_versions untouched; a new version row
        with operation 'UPDATE' is appended. The memories row is bumped to
        version + 1 with recomputed sha256/content_hash/tokenized_content.
        An UPDATE_MEMORY audit log entry is written.

        Returns:
            The updated memory row as a dict, or None if memory_id not found.
        """
        with self._write_lock:

            conn = self._conn
            if not conn:
                return None

            cursor = conn.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            current = dict(row)

            now = datetime.now(timezone.utc).isoformat()
            version_id = f"ver_{uuid.uuid4().hex[:12]}"

            new_content = content if content is not None else self._decrypt_content(current.get("content", ""))
            new_importance = (
                importance if importance is not None
                else float(current.get("importance", 0.5))
            )
            current_tags = current.get("tags") or "[]"
            new_tags = (
                tags if tags is not None
                else (json.loads(current_tags) if isinstance(current_tags, str) else current_tags)
            )
            new_category = (
                category if category is not None
                else current.get("category", "general")
            )

            sha256_hash = self._compute_sha256(new_content)
            tokenized = self._tokenize_content_for_fts(new_content)
            plain_content = new_content
            # B5 存储加密：content 列写密文；tokenized 明文；hash 基于明文
            new_content = self._encrypt_content(new_content)
            tokenized = self._tokenized_for_storage(plain_content, tokenized)

            conn.execute("""
                UPDATE memories
                SET content = ?, tokenized_content = ?, importance = ?, tags = ?,
                    category = ?, sha256_hash = ?, content_hash = ?,
                    version = version + 1, updated_at = ?
                WHERE memory_id = ?
            """, (new_content, tokenized, new_importance,
                  json.dumps(new_tags, ensure_ascii=False),
                  new_category, sha256_hash, sha256_hash, now, memory_id))

            conn.execute("""
                INSERT INTO memory_versions
                (version_id, memory_id, content, sha256_hash, operation, created_at)
                VALUES (?, ?, ?, ?, 'UPDATE', ?)
            """, (version_id, memory_id, new_content, sha256_hash, now))

            self._write_audit_log(
                action="UPDATE_MEMORY",
                memory_id=memory_id,
                persona_id=current.get("persona_id"),
                content_hash=sha256_hash,
                metadata={"old_version": current.get("version", 1)},
            )
            conn.commit()

            cursor = conn.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            )
            updated = cursor.fetchone()
            if not updated:
                return None
            d = dict(updated)
            if d.get("content"):
                d["content"] = self._decrypt_content(d["content"])
            return d
    @_safe_write
    def archive_memories(self, memory_ids: List[str]) -> int:
        """批量将记忆标记为 archived（衰减压缩回写；与 PostgreSQLAdapter 同接口）。"""
        if not memory_ids:
            return 0
        with self._write_lock:
            conn = self._conn
            if not conn:
                return 0
            now = datetime.now(timezone.utc).isoformat()
            placeholders = ",".join("?" * len(memory_ids))
            cur = conn.execute(
                f"UPDATE memories SET status = 'archived', updated_at = ? "
                f"WHERE memory_id IN ({placeholders})",
                [now] + list(memory_ids),
            )
            conn.commit()
            return cur.rowcount
    def get_version_chain(self, memory_id: str) -> List[Dict[str, Any]]:
        conn = self._conn
        if not conn:
            return []

        cursor = conn.execute("""
            SELECT * FROM memory_versions
            WHERE memory_id = ?
            ORDER BY created_at ASC
        """, (memory_id,))
        rows = [dict(row) for row in cursor.fetchall()]
        for r in rows:
            if r.get("content"):
                r["content"] = self._decrypt_content(r["content"])
        return rows
    def get_all_memories(self, agent_id: Optional[str] = None, limit: int = 200,
                          offset: int = 0) -> List[Dict[str, Any]]:
        """Get all active memories across all personas/tenants, optionally filtered by agent_id.

        2026-08-15（压测修复 v2）：线程本地只读连接（纯读，无锁）。
        2026-08-26（PageTree）：新增 offset 分页（页树全量建树用）。
        """
        conn = self._get_read_conn()
        if not conn:
            return []

        if agent_id:
            cursor = conn.execute("""
                SELECT * FROM memories
                WHERE status = 'active' AND agent_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (agent_id, limit, offset))
        else:
            cursor = conn.execute("""
                SELECT * FROM memories
                WHERE status = 'active'
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (limit, offset))
        rows = [dict(row) for row in cursor.fetchall()]
        for r in rows:
            if r.get("content"):
                r["content"] = self._decrypt_content(r["content"])
        return rows
    @_safe_write
    def touch_memory(self, memory_id: str) -> bool:
        """更新指定记忆的 last_accessed_at 和 access_count。

        Args:
            memory_id: 要触达的记忆 ID。

        Returns:
            是否成功更新。
        """
        with self._write_lock:

            conn = self._conn
            if not conn:
                return False

            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute("""
                UPDATE memories
                SET last_accessed_at = ?,
                    access_count = access_count + 1,
                    updated_at = ?
                WHERE memory_id = ?
            """, (now, now, memory_id))
            conn.commit()
            return cursor.rowcount > 0
    def _touch_batch(self, memory_ids: List[str]) -> None:
        """批量累积搜索命中的记忆访问（异步写，读路径零阻塞）。

        2026-08-15（压测修复）：原实现同步 UPDATE+commit（每次检索都写库，
        实测占读延迟 ~40%）。改为入内存队列，由 _touch_flush_loop 后台线程
        定期批量 flush（一次 UPDATE…IN + 一次 commit）。语义保持：
        access_count 按命中次数累加；last_accessed_at 取 flush 时刻。
        失败静默，不影响搜索主流程。
        """
        if not memory_ids:
            return
        with self._write_lock:
            for mid in memory_ids:
                self._touch_queue[mid] = self._touch_queue.get(mid, 0) + 1
        self._touch_pending.set()
    def _touch_flush_loop(self) -> None:
        """后台线程：周期 flush touch 队列（batch UPDATE + 一次 commit）。"""
        while not self._touch_stop.wait(1.0):
            try:
                self._flush_touch_queue()
            except Exception:
                pass  # 静默失败
    def _flush_touch_queue(self) -> None:
        """把累积的 touch 队列批量写入（幂等；空队列直接返回）。"""
        with self._write_lock:
            if not self._touch_queue:
                self._touch_pending.clear()
                return
            conn = self._conn
            if not conn:
                return
            queue = self._touch_queue
            self._touch_queue = {}
            self._touch_pending.clear()
            try:
                now = datetime.now(timezone.utc).isoformat()
                mids = list(queue.keys())
                counts = queue
                placeholders = ",".join("?" for _ in mids)
                # 单条 UPDATE 按计数累加（executemany + 一次 commit）
                conn.executemany(
                    "UPDATE memories SET access_count = access_count + ?, "
                    "last_accessed_at = ?, updated_at = ? WHERE memory_id = ?",
                    [(counts[mid], now, now, mid) for mid in mids],
                )
                conn.commit()
            except Exception:
                # flush 失败：回填队列避免丢失（下一轮重试）
                # 2026-08-16 修复:必须先 rollback——python sqlite3 在 execute 异常后
                # 连接留在未提交事务中(不自动回滚), 悬挂写事务会永久占 SQLite 写锁
                # (worker 超时/锁复发根因, 与 skill 坑 #9 同源)。
                try:
                    conn.rollback()
                except Exception:
                    pass
                for mid, cnt in queue.items():
                    self._touch_queue[mid] = self._touch_queue.get(mid, 0) + cnt
    def age_memories(self) -> Dict[str, Any]:
        """手动触发老化扫描，清理 TTL 过期的记忆（软删除）。

        Returns:
            Dict with aged_count and details.
        """
        # 2026-08-16 修复:加 _write_lock + 异常 rollback——此前无锁保护且
        # UPDATE 抛异常时不回滚, 会悬挂写事务占锁(与 touch flush 同源)。
        with self._write_lock:
            conn = self._conn
            if not conn:
                return {"aged_count": 0, "error": "Not connected"}

            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute("""
                SELECT memory_id FROM memories
                WHERE status = 'active'
                  AND ttl_seconds IS NOT NULL
                  AND created_at IS NOT NULL
                  AND datetime(created_at, '+' || ttl_seconds || ' seconds') < datetime(?)
            """, (now,))
            expired_ids = [row["memory_id"] for row in cursor.fetchall()]

            if not expired_ids:
                return {"aged_count": 0, "timestamp": now}

            try:
                placeholders = ",".join("?" for _ in expired_ids)
                conn.execute(f"""
                    UPDATE memories
                    SET status = 'expired', updated_at = ?
                    WHERE memory_id IN ({placeholders})
                """, [now] + expired_ids)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

            return {"aged_count": len(expired_ids), "timestamp": now, "expired_ids": expired_ids}
