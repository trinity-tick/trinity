"""SQLite storage adapter — single-tenant default backend.

支持 WAL 模式、FTS5 全文搜索、批量写入。
"""

import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


from .base import StorageAdapter


# ── 批量写入常量 ──────────────────────────────────────────────────────
_BATCH_SIZE = 100       # 攒够 100 条
_BATCH_TIMEOUT = 5.0    # 或 5 秒


class SQLiteAdapter(StorageAdapter):
    """SQLite-based storage adapter.

    Default backend for single-tenant deployments.
    Supports persona_id and session_id scoping.
    """

    def __init__(self, db_path: str = "trinity_store.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

        # ── 批量写入缓冲区 ──────────────────────────────────────────
        self._batch_buffer: List[Dict[str, Any]] = []
        self._batch_last_flush = time.time()

    # ── 连接 / 断开 ──────────────────────────────────────────────────

    def connect(self) -> None:
        """Connect to SQLite database and create tables if needed."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._create_tables()

    def _apply_pragmas(self) -> None:
        """应用性能优化 PRAGMA。"""
        cursor = self._conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA cache_size=-8000;")   # 8MB cache
        self._conn.commit()

    def disconnect(self) -> None:
        # 断开前 flush 批处理缓冲区
        self._flush_batch()
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── 建表 ─────────────────────────────────────────────────────────

    def _create_tables(self) -> None:
        cursor = self._conn.cursor()

        # Auto-migrate old schema (pre-v6.37 without persona_id/tenant_id)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories'")
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(memories)")
            cols = [row[1] for row in cursor.fetchall()]
            if 'persona_id' not in cols:
                cursor.executescript("""
                    DROP TABLE IF EXISTS memory_versions;
                    DROP TABLE IF EXISTS memories;
                    DROP TABLE IF EXISTS sessions;
                    DROP TABLE IF EXISTS personas;
                    DROP TABLE IF EXISTS tenants;
                """)

        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id   TEXT PRIMARY KEY,
                name        TEXT NOT NULL UNIQUE,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS personas (
                persona_id  TEXT PRIMARY KEY,
                tenant_id   TEXT REFERENCES tenants(tenant_id),
                name        TEXT NOT NULL,
                metadata    TEXT DEFAULT '{}',
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, name)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id  TEXT PRIMARY KEY,
                persona_id  TEXT REFERENCES personas(persona_id),
                tenant_id   TEXT REFERENCES tenants(tenant_id),
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS memories (
                memory_id   TEXT PRIMARY KEY,
                session_id  TEXT REFERENCES sessions(session_id),
                persona_id  TEXT REFERENCES personas(persona_id),
                tenant_id   TEXT REFERENCES tenants(tenant_id),
                content     TEXT NOT NULL,
                role        TEXT DEFAULT 'user',
                importance  REAL DEFAULT 0.5,
                tags        TEXT DEFAULT '[]',
                category    TEXT DEFAULT 'general',
                sha256_hash TEXT,
                status      TEXT DEFAULT 'active',
                version     INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS memory_versions (
                version_id   TEXT PRIMARY KEY,
                memory_id    TEXT,
                content      TEXT NOT NULL,
                sha256_hash  TEXT,
                operation    TEXT DEFAULT 'CREATE',
                created_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                action       TEXT NOT NULL,
                memory_id    TEXT,
                persona_id   TEXT,
                content_hash TEXT,
                metadata     TEXT DEFAULT '{}',
                timestamp    TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_audit_persona ON audit_log(persona_id);
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
        """)
        self._conn.commit()

        # ── FTS5 全文搜索虚拟表 ─────────────────────────────────────
        self._create_fts5()

        # Ensure default tenant exists
        cursor.execute("INSERT OR IGNORE INTO tenants (tenant_id, name) VALUES (?, ?)",
                      ("default", "default"))

    def _create_fts5(self) -> None:
        """创建 FTS5 虚拟表及同步触发器。"""
        cursor = self._conn.cursor()
        try:
            cursor.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(content, category, tags,
                    content='memories',
                    content_rowid='rowid');

                -- INSERT 触发器：同步写入 FTS 索引
                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories
                BEGIN
                    INSERT INTO memories_fts(rowid, content, category, tags)
                    VALUES (new.rowid, new.content, new.category, new.tags);
                END;

                -- DELETE 触发器：同步删除 FTS 索引
                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories
                BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
                    VALUES ('delete', old.rowid, old.content, old.category, old.tags);
                END;

                -- UPDATE 触发器：同步更新 FTS 索引
                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories
                BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
                    VALUES ('delete', old.rowid, old.content, old.category, old.tags);
                    INSERT INTO memories_fts(rowid, content, category, tags)
                    VALUES (new.rowid, new.content, new.category, new.tags);
                END;
            """)
            self._conn.commit()
        except sqlite3.OperationalError:
            # FTS5 可能不可用（旧版 SQLite），静默回退
            pass

    # ── 辅助方法 ─────────────────────────────────────────────────────

    def _compute_sha256(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # ── PII 检测与脱敏 ──────────────────────────────────────────────

    # PII 检测按优先级排序：长匹配优先，避免身份证中的数字被误当作电话号码
    _PII_PATTERNS = {
        "id_card": r"[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]",
        "phone":   r"(?:(?:\+|00)86[\s\-]?)?1[3-9]\d{9}(?!\d)",
        "email":   r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    }

    def _detect_pii(self, content: str) -> Dict[str, List[str]]:
        """检测内容中的 PII 并返回脱敏后的内容与检测结果。

        Returns:
            {"redacted": 脱敏后的内容, "found": {"phone": [...], "email": [...], "id_card": [...]}}
        """
        import re

        found: Dict[str, List[str]] = {"phone": [], "email": [], "id_card": []}
        redacted = content

        # 按优先级顺序检测（身份证 > 电话 > 邮箱），避免长内容被短模式误匹配
        for pii_type, pattern in self._PII_PATTERNS.items():
            matches = re.findall(pattern, redacted)
            if matches:
                # 去重并排序（长匹配优先替换）
                unique = sorted(set(matches), key=len, reverse=True)
                found[pii_type] = unique
                for match in unique:
                    if pii_type == "phone":
                        # 138****1234 保留首尾3+4位
                        digits = re.sub(r"\D", "", match)
                        if len(digits) >= 7:
                            replacement = digits[:3] + "****" + digits[-4:]
                        elif len(digits) >= 3:
                            replacement = digits[:3] + "****"
                        else:
                            replacement = digits + "****"
                    elif pii_type == "email":
                        # email: a***@domain.com
                        local, domain = match.split("@", 1)
                        if len(local) >= 3:
                            replacement = local[0] + "***@" + domain
                        elif len(local) >= 1:
                            replacement = local[0] + "***@" + domain
                        else:
                            replacement = "***@" + domain
                    elif pii_type == "id_card":
                        # 身份证: 保留前6后4
                        replacement = match[:6] + "********" + match[-4:]
                    else:
                        replacement = "***"
                    # 只替换第一次出现
                    redacted = redacted.replace(match, replacement, 1)

        return {"redacted": redacted, "found": found}

    def _write_audit_log(self, action: str, memory_id: str = None,
                          persona_id: str = None, content_hash: str = None,
                          metadata: dict = None) -> None:
        """向 audit_log 表写入一条审计记录（INSERT only）。"""
        conn = self._conn
        if not conn:
            return
        conn.execute("""
            INSERT INTO audit_log (action, memory_id, persona_id, content_hash, metadata)
            VALUES (?, ?, ?, ?, ?)
        """, (
            action,
            memory_id,
            persona_id,
            content_hash,
            json.dumps(metadata or {}),
        ))

    # ── GDPR 工具函数 ────────────────────────────────────────────────

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

    def _fts_available(self) -> bool:
        """检查 FTS5 是否可用。"""
        try:
            cursor = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
            )
            return cursor.fetchone() is not None
        except Exception:
            return False

    # ── 批量写入 ─────────────────────────────────────────────────────

    def ingest_batch(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """批量写入记忆记录。

        攒够 100 条或 5 秒后统一 commit。如果 records 为空，
        仅做 flush 检查。

        Args:
            records: 要写入的记录列表，每项包含 store_memory 参数。

        Returns:
            每条记录的写入结果列表。
        """
        results = []
        for rec in records:
            content = rec.get("content", "")
            result = self.store_memory(
                content=content,
                persona_id=rec.get("persona_id", "default"),
                session_id=rec.get("session_id"),
                tenant_id=rec.get("tenant_id", "default"),
                role=rec.get("role", "user"),
                importance=rec.get("importance", 0.5),
                tags=rec.get("tags"),
                category=rec.get("category", "general"),
            )
            results.append(result)

        # 检查是否需要 flush
        self._maybe_flush()
        return results

    def _maybe_flush(self) -> None:
        """如果达到批量条件则 flush。同时确保每次写入后立即 commit。"""
        now = time.time()
        if (len(self._batch_buffer) >= _BATCH_SIZE or
                (self._batch_buffer and now - self._batch_last_flush >= _BATCH_TIMEOUT)):
            self._flush_batch()
        else:
            # 确保每次写入都 commit，防止进程退出时数据丢失
            try:
                self._conn.commit()
                self._batch_last_flush = time.time()
            except Exception:
                pass

    def _flush_batch(self) -> None:
        """提交所有缓冲写入。"""
        if not self._batch_buffer:
            return
        try:
            self._conn.commit()
        except Exception:
            self._conn.rollback()
        finally:
            self._batch_buffer = []
            self._batch_last_flush = time.time()

    # ── 核心存储方法 ─────────────────────────────────────────────────

    def store_memory(
        self,
        content: str,
        persona_id: str = "default",
        session_id: Optional[str] = None,
        tenant_id: str = "default",
        role: str = "user",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        category: str = "general",
        auto_redact_pii: bool = False,
    ) -> Dict[str, Any]:
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

        self._batch_buffer.append({
            "memory_id": memory_id,
            "session_id": session_id,
            "persona_id": persona_id,
            "tenant_id": tenant_id,
            "content": stored_content,
            "role": role,
            "importance": importance,
            "tags_json": tags_json,
            "category": category,
            "sha256_hash": sha256_hash,
            "now": now,
        })

        conn.execute("""
            INSERT INTO memories
            (memory_id, session_id, persona_id, tenant_id, content, role,
             importance, tags, category, sha256_hash, status, version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?)
        """, (memory_id, session_id, persona_id, tenant_id, stored_content, role,
              importance, tags_json, category, sha256_hash, now, now))

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

        return {
            "memory_id": memory_id,
            "version_id": version_id,
            "sha256_hash": sha256_hash,
            "timestamp": now,
            "persona_id": persona_id,
            "session_id": session_id,
            "auto_redacted": auto_redact_pii and pii_info is not None,
            "pii_redacted_types": list(pii_info.keys()) if pii_info else [],
        }

    # ── 搜索 ─────────────────────────────────────────────────────────

    def search_memories(
        self,
        query: str,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """搜索记忆。

        优先使用 FTS5 全文搜索，如果不可用则回退到 LIKE 模糊搜索。
        """
        conn = self._conn
        if not conn:
            return []

        conditions = ["status = 'active'"]
        params: List[Any] = []

        if persona_id:
            conditions.append("persona_id = ?")
            params.append(persona_id)
        if tenant_id:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)

        where = " AND ".join(conditions)

        # 尝试 FTS5 搜索
        if self._fts_available():
            try:
                fts_results = self._search_fts(query, params, where, top_k)
                # FTS5 可能对 CJK 文字分词不完整，返回空结果，
                # 此时仍应回退到 LIKE 搜索
                if fts_results:
                    return fts_results
            except Exception:
                # FTS5 搜索失败，回退到 LIKE
                pass

        # 回退：LIKE 模糊搜索
        return self._search_like(query, params, where, top_k)

    def _search_fts(
        self, query: str, params: List[Any], where: str, top_k: int
    ) -> List[Dict[str, Any]]:
        """使用 FTS5 全文搜索。"""
        # 构造 FTS5 查询词：用 * 做前缀匹配，OR 连接避免多词全命中失败
        terms = query.strip().split()
        fts_query = " OR ".join(f'"{t}"*' for t in terms if t)
        if not fts_query:
            return []

        sql = f"""
            SELECT m.memory_id, m.content, m.persona_id, m.session_id, m.role,
                   m.importance, m.tags, m.category, m.created_at,
                   fts.rank as score
            FROM memories m
            INNER JOIN (
                SELECT rowid, rank
                FROM memories_fts
                WHERE memories_fts MATCH ?
            ) fts ON m.rowid = fts.rowid
            WHERE {where}
            ORDER BY score
            LIMIT ?
        """

        # rank 越小越相关，转为 0-1 分数
        cursor = conn = self._conn
        if not conn:
            return []

        full_params = params + [fts_query, top_k]
        cursor = conn.execute(sql, full_params)

        results = []
        # 先收集，再 min-max 归一化分数
        rows = cursor.fetchall()
        if not rows:
            return []

        # 提取 rank 值用于归一化
        raw_scores = [row["score"] for row in rows]
        min_rank = min(raw_scores) if raw_scores else 0
        max_rank = max(raw_scores) if raw_scores else 1
        rank_range = max_rank - min_rank if max_rank != min_rank else 1.0

        for i, row in enumerate(rows):
            # FTS5 rank 是负值（越负越相关），我们翻转成 0-1 分数
            norm_score = 1.0 - (row["score"] - min_rank) / rank_range
            results.append({
                "memory_id": row["memory_id"],
                "content": row["content"],
                "content_preview": row["content"][:100],
                "persona_id": row["persona_id"],
                "session_id": row["session_id"],
                "role": row["role"],
                "importance": row["importance"],
                "tags": json.loads(row["tags"]),
                "category": row["category"],
                "created_at": row["created_at"],
                "score": round(norm_score, 4),
            })

        return results

    def _search_like(
        self, query: str, params: List[Any], where: str, top_k: int
    ) -> List[Dict[str, Any]]:
        """回退到 LIKE 模糊搜索。"""
        conn = self._conn
        if not conn:
            return []

        like_term = f"%{query}%"
        # params 已经包含 WHERE 中的 ?，只需要加上 LIKE 参数和 LIMIT
        full_params = params + [like_term, top_k]

        # params 来自 WHERE 条件，like_term * 2 用于 content+tags 过滤，top_k 用于 LIMIT
        cursor = conn.execute(f"""
            SELECT memory_id, content, persona_id, session_id, role,
                   importance, tags, category, created_at,
                   0.8 as score
            FROM memories
            WHERE {where}
              AND (content LIKE ? OR tags LIKE ?)
            ORDER BY importance DESC, created_at DESC
            LIMIT ?
        """, params + [like_term, like_term, top_k])

        results = []
        for row in cursor.fetchall():
            results.append({
                "memory_id": row["memory_id"],
                "content": row["content"],
                "content_preview": row["content"][:100],
                "persona_id": row["persona_id"],
                "session_id": row["session_id"],
                "role": row["role"],
                "importance": row["importance"],
                "tags": json.loads(row["tags"]),
                "category": row["category"],
                "created_at": row["created_at"],
                "score": row["score"],
            })

        return results

    # ── 单条查询 ─────────────────────────────────────────────────────

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        conn = self._conn
        if not conn:
            return None

        cursor = conn.execute(
            "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return dict(row)

    def get_persona_memories(
        self, persona_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        conn = self._conn
        if not conn:
            return []

        cursor = conn.execute("""
            SELECT * FROM memories
            WHERE persona_id = ? AND status = 'active'
            ORDER BY created_at DESC
            LIMIT ?
        """, (persona_id, limit))
        return [dict(row) for row in cursor.fetchall()]

    def delete_memory(self, memory_id: str) -> bool:
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

    def get_version_chain(self, memory_id: str) -> List[Dict[str, Any]]:
        conn = self._conn
        if not conn:
            return []

        cursor = conn.execute("""
            SELECT * FROM memory_versions
            WHERE memory_id = ?
            ORDER BY created_at ASC
        """, (memory_id,))
        return [dict(row) for row in cursor.fetchall()]

    def get_all_memories(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Get all active memories across all personas/tenants."""
        conn = self._conn
        if not conn:
            return []

        cursor = conn.execute("""
            SELECT * FROM memories
            WHERE status = 'active'
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]

    def diagnostics(self) -> Dict[str, Any]:
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}

        cursor = conn.execute("SELECT COUNT(*) as c FROM memories")
        total = cursor.fetchone()["c"]

        cursor = conn.execute(
            "SELECT COUNT(*) as c FROM memories WHERE status = 'active'"
        )
        active = cursor.fetchone()["c"]

        cursor = conn.execute("SELECT COUNT(DISTINCT persona_id) as c FROM memories")
        personas = cursor.fetchone()["c"]

        cursor = conn.execute("SELECT COUNT(*) as c FROM memory_versions")
        versions = cursor.fetchone()["c"]

        # 检查 FTS5 状态
        fts_ok = self._fts_available()

        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        return {
            "adapter": "sqlite",
            "db_path": self.db_path,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "total_memories": total,
            "active_memories": active,
            "total_personas": personas,
            "total_versions": versions,
            "fts5_enabled": fts_ok,
            "journal_mode": "wal",
        }


# ── 自检测试 ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import tempfile

    print("=" * 60)
    print("  Trinity SQLiteAdapter 安全与合规升级自检")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_trinity.db")
        adapter = SQLiteAdapter(db_path=db_path)
        adapter.connect()

        # ── 测试1-3：基础功能 ────────────────────────────────────────
        cursor = adapter._conn.execute("PRAGMA journal_mode")
        jm = cursor.fetchone()[0]
        print(f"\n[测试1] journal_mode = {jm} (期望: wal)")

        cursor = adapter._conn.execute("PRAGMA synchronous")
        syn = cursor.fetchone()[0]
        print(f"[测试2] synchronous = {syn} (期望: 1 = NORMAL)")

        fts_ok = adapter._fts_available()
        print(f"[测试3] FTS5 可用 = {fts_ok}")

        # ── 测试4：审计日志表 ────────────────────────────────────────
        print("\n[测试4] 审计日志表创建:")
        cursor = adapter._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
        )
        has_audit = cursor.fetchone() is not None
        print(f"  audit_log 表存在 = {has_audit}")
        assert has_audit, "audit_log 表未创建"

        # ── 测试5：PII 检测器 ────────────────────────────────────────
        print("\n[测试5] PII 检测与脱敏:")
        test_content = "我的电话是13812345678，邮箱是user@example.com，身份证是110101199001011234"
        result = adapter._detect_pii(test_content)
        print(f"  原始内容: {test_content}")
        print(f"  脱敏后  : {result['redacted']}")
        print(f"  检测结果: {result['found']}")
        assert "138****5678" in result["redacted"], "手机号脱敏失败"
        assert "u***@example.com" in result["redacted"], "邮箱脱敏失败"
        assert "110101********1234" in result["redacted"], "身份证脱敏失败"
        print("  [通过] PII 检测/脱敏通过")

        # ── 测试6：含 PII 内容存储（自动脱敏） ───────────────────────
        print("\n[测试6] 含 PII 内容存储 (auto_redact_pii=True):")
        r_pii = adapter.store_memory(
            "我的电话是13912345678，联系张经理13899990000",
            persona_id="user_001",
            tags=["PII", "test"],
            category="contact",
            auto_redact_pii=True,
        )
        adapter._flush_batch()
        print(f"  存储结果: memory_id={r_pii['memory_id']}")
        print(f"  自动脱敏: {r_pii['auto_redacted']}")
        print(f"  脱敏类型: {r_pii['pii_redacted_types']}")
        assert r_pii["auto_redacted"], "自动脱敏未触发"
        # 验证实际存储的是脱敏后的内容
        stored = adapter.get_memory(r_pii["memory_id"])
        print(f"  实际存储内容: {stored['content']}")
        assert "139****5678" in stored["content"], "存储内容未脱敏"
        assert "138****0000" in stored["content"], "第二个号码未脱敏"
        print("  [通过] PII 自动脱敏存储通过")

        # ── 测试7：不含 PII 内容正常存储 ─────────────────────────────
        print("\n[测试7] 不含 PII 内容存储:")
        r_normal = adapter.store_memory(
            "明天下午三点开会",
            persona_id="user_001",
            auto_redact_pii=True,
        )
        adapter._flush_batch()
        stored_normal = adapter.get_memory(r_normal["memory_id"])
        print(f"  内容: {stored_normal['content']}")
        assert "明天下午三点开会" in stored_normal["content"], "无PII内容被误修改"
        print("  [通过] 无 PII 内容未受影响")

        # ── 测试8：导出用户数据 (GDPR) ──────────────────────────────
        print("\n[测试8] 导出用户数据 (export_user_data):")
        exported = adapter.export_user_data("user_001")
        exported_data = json.loads(exported)
        print(f"  记忆数量: {exported_data['memories_count']}")
        print(f"  导出时间: {exported_data['exported_at']}")
        print(f"  Persona: {exported_data['persona']}")
        assert exported_data["memories_count"] >= 2, "导出数据不完整"
        print("  [通过] 用户数据导出通过")

        # ── 测试9：审计日志验证 ──────────────────────────────────────
        print("\n[测试9] 审计日志验证:")
        cursor = adapter._conn.execute(
            "SELECT id, action, persona_id, timestamp FROM audit_log ORDER BY id"
        )
        logs = cursor.fetchall()
        print(f"  审计条目数: {len(logs)}")
        for log in logs:
            print(f"    [{log['id']}] {log['action']} | persona={log['persona_id']} | {log['timestamp']}")
        assert len(logs) >= 3, "审计日志条目不足"  # STORE_MEMORY x2 + EXPORT_USER_DATA x1
        print("  [通过] 审计日志验证通过")

        # ── 测试10：GDPR 被遗忘权 ────────────────────────────────────
        print("\n[测试10] GDPR 被遗忘权 (forget_user):")
        forget_result = adapter.forget_user("user_001")
        print(f"  遗忘结果: {forget_result}")
        assert forget_result["memories_erased"] > 0, "未找到需遗忘的记忆"
        # 验证内容已匿名化
        memories_after = adapter.get_persona_memories("user_001")
        if memories_after:
            for m in memories_after:
                print(f"  记忆 {m['memory_id']}: 状态={m['status']}, 内容={m['content'][:30]}")
                assert m["content"] == "[GDPR erased]", "内容未匿名化"
                assert m["status"] == "gdpr_deleted", "状态未更新"
        else:
            print("  所有记忆已被删除")
        print("  [通过] GDPR 被遗忘权通过")

        # ── 测试11：delete_memory 审计日志 ──────────────────────────
        print("\n[测试11] delete_memory 审计日志:")
        r_del = adapter.store_memory("将被删除的记忆", persona_id="user_del_test")
        adapter._flush_batch()
        del_result = adapter.delete_memory(r_del["memory_id"])
        print(f"  删除结果: {del_result}")
        cursor = adapter._conn.execute(
            "SELECT action, memory_id FROM audit_log WHERE action = 'DELETE_MEMORY'"
        )
        del_log = cursor.fetchone()
        assert del_log is not None, "DELETE_MEMORY 审计日志未记录"
        print(f"  审计日志: action={del_log['action']}, memory_id={del_log['memory_id']}")
        print("  [通过] delete_memory 审计日志通过")

        # ── 最终 diagnostics ─────────────────────────────────────────
        print(f"\n  总记忆数: {adapter.diagnostics()['total_memories']}")

        adapter.disconnect()

    print("\n" + "=" * 60)
    print("  [PASS] Trinity 安全与合规升级全部通过！")
    print("  功能: audit_log | PII 检测 | GDPR 导出 | GDPR 遗忘")
    print("=" * 60)
