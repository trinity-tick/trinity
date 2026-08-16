"""SQLite storage adapter — single-tenant default backend.

支持 WAL 模式、FTS5 全文搜索、批量写入。
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import functools


def _safe_write(fn):
    """写路径统一异常保护(2026-08-16):任何异常立即回滚,避免悬挂未提交
    写事务长期占用 SQLite 写锁(锁复发根因)。"""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise
    return wrapper



import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


from .base import StorageAdapter

from ..security.crypto import get_storage_cipher, StorageCipher  # type: ignore[attr-defined]

logger = logging.getLogger("trinity.adapters.sqlite")


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

        # ── 存储加密（B5, 2026-08-15）──────────────────────────────────
        # TRINITY_STORAGE_ENCRYPTION=on 时启用 AES-256-GCM：
        #   - memories.content / memory_versions.content 落盘为密文
        #   - tokenized_content 保持明文 → FTS5 检索不受影响
        #   - sha256_hash/content_hash 基于明文计算 → 去重/一致性链不变
        self._cipher: Optional[StorageCipher] = get_storage_cipher()
        if self._cipher:
            logger.info("storage encryption ON (AES-256-GCM) for %s", db_path)

        # ── 批量写入缓冲区 ──────────────────────────────────────────
        self._batch_buffer: List[Dict[str, Any]] = []
        self._batch_last_flush = time.time()

        # ── 写锁（2026-08-15，根治 mcp-stdio 持锁）──────────────────
        # SQLite 单连接同一时刻只能有一个写事务；ingest 主线程与
        # _postprocess_memory 后台线程并发写同一 conn，交错会导致
        # 未提交写事务悬挂、永久占库锁（database is locked）。
        # RLock：同一线程可重入（嵌套写方法），跨线程 serialize。
        self._write_lock = threading.RLock()

        # ── 线程本地只读连接池（2026-08-15 压测修复）──────────────
        # 读路径（search/get/query）每线程独立只读连接：WAL 下多读
        # 并行、零锁竞争（实测 8 线程 p50 115ms→25ms）。写路径仍走
        # 主连接 self._conn + _write_lock（WAL 单写者语义）。
        # 生命周期管理（2026-08-15 二轮）：注册表 + 上限 + 超限回退，
        # 防线程频繁重建时连接句柄滞留（见 _get_read_conn / disconnect）。
        self._read_local = threading.local()
        self._read_conns: set = set()          # 全部线程本地读连接（强引用）
        self._read_conns_lock = threading.Lock()
        self._read_conn_max = 64               # 读连接上限（防无界增长）
        self._read_conn_overflow = 0           # 超限回退计数（诊断）

        # ── 异步 touch 队列（2026-08-15，压测修复）────────────────
        # 检索命中即 touch 是隐藏写放大（实测占读延迟 ~40%）：每次
        # search 同步 UPDATE+commit。改为内存累积 + 后台线程定期批量
        # flush（一次 UPDATE…IN + 一次 commit），读路径零写阻塞。
        self._touch_queue: Dict[str, int] = {}  # memory_id -> 命中次数
        self._touch_pending = threading.Event()
        self._touch_stop = threading.Event()

    # ── 连接 / 断开 ──────────────────────────────────────────────────

    def connect(self) -> None:
        """Connect to SQLite database and create tables if needed."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        # check_same_thread=False：允许 FastAPI/HTTP 线程池等跨线程复用同一连接
        # （SQLite 连接默认线程绑定，多线程服务调用 search/store 会抛
        #  "SQLite objects created in a thread can only be used in that same thread"）
        self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._create_tables()
        # 性能（2026-08-15）：jieba 词典冷启动约 1.4s（首查被拖慢）——
        # 后台线程预热，把开销移到进程启动而非首次搜索。
        threading.Thread(target=self._prewarm_tokenizer, daemon=True).start()
        # 异步 touch flush 线程（2026-08-15 压测修复）
        threading.Thread(target=self._touch_flush_loop, daemon=True,
                         name="touch-flush").start()

    def _prewarm_tokenizer(self) -> None:
        """后台预热 jieba 分词词典（非阻塞；失败静默）。"""
        try:
            import jieba
            jieba.initialize()
            list(jieba.cut("Trinity 记忆系统分词预热"))
        except Exception:  # noqa: BLE001
            pass

    def _apply_pragmas(self) -> None:
        """应用性能优化 PRAGMA。"""
        cursor = self._conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA cache_size=-8000;")   # 8MB cache
        cursor.execute("PRAGMA busy_timeout=15000;")  # 15s 写锁等待，避免并发锁冲突
        self._conn.commit()

    def disconnect(self) -> None:
        # 断开前 flush 批处理缓冲区 + touch 队列
        self._flush_batch()
        self._touch_stop.set()
        self._flush_touch_queue()
        if self._conn:
            self._conn.close()
            self._conn = None
        # 关闭全部线程本地只读连接（注册表强引用保证不滞留）
        with self._read_conns_lock:
            conns = list(self._read_conns)
            self._read_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        # 当前线程的 thread-local 引用清空
        try:
            self._read_local.conn = None
        except Exception:
            pass

    def _get_read_conn(self) -> Optional[sqlite3.Connection]:
        """返回当前线程的只读连接（WAL 多读并行，2026-08-15）。

        读路径用 per-thread 只读连接：WAL 下并发读不阻塞、无锁竞争，
        替代此前"单连接 + RLock 串行化"（8 线程 p50 115ms→25ms）。
        只读模式（mode=ro）保证不会误写；写路径仍走主连接+_write_lock。

        生命周期（2026-08-15 二轮）：连接注册进 _read_conns（disconnect
        全量关闭）；超过 _read_conn_max 时创建临时只读连接（不注册、不缓存，
        GC 兜底关闭）并计 _read_conn_overflow——防线程频繁重建时句柄滞留、
        防无界增长，同时避免回退主连接导致调用方锁外 execute 的游标竞态。
        """
        conn = getattr(self._read_local, "conn", None)
        if conn is not None:
            return conn
        # 上限检查：满则用临时只读连接（不注册，本线程下次仍走主路径）
        with self._read_conns_lock:
            over = len(self._read_conns) >= self._read_conn_max
        if over:
            self._read_conn_overflow += 1
            try:
                conn = sqlite3.connect(
                    f"file:{self.db_path}?mode=ro", uri=True, timeout=10.0,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=15000;")
                return conn
            except Exception:
                return None
        try:
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, timeout=10.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=15000;")
            self._read_local.conn = conn
            with self._read_conns_lock:
                self._read_conns.add(conn)
            return conn
        except Exception:
            return None

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
            else:
                # P0.6 migration: backfill any missing columns from the
                # canonical schema (multi-scope ACL app_id, agent_id, and the
                # extended memory-metadata columns introduced after the legacy
                # 14-column schema).  This covers DBs created at intermediate
                # versions that already have persona_id but still lack the
                # columns referenced by the INSERT statement.
                _column_migrations = [
                    ("agent_id", "TEXT DEFAULT 'default'"),
                    ("app_id", "TEXT"),
                    ("tokenized_content", "TEXT"),
                    ("ttl_seconds", "INTEGER"),
                    ("last_accessed_at", "TEXT"),
                    ("access_count", "INTEGER DEFAULT 0"),
                    ("importance_score", "REAL DEFAULT 0.0"),
                    ("content_hash", "TEXT"),
                    ("conflict_group_id", "TEXT"),
                    ("is_resolved", "INTEGER DEFAULT 0"),
                    ("modality", "TEXT DEFAULT 'text'"),
                    ("metadata", "TEXT DEFAULT '{}'"),
                    ("source_uri", "TEXT"),
                    ("memory_layer", "TEXT"),
                ]
                for _col, _decl in _column_migrations:
                    if _col not in cols:
                        cursor.execute(
                            "ALTER TABLE memories ADD COLUMN %s %s" % (_col, _decl)
                        )
                        cols.append(_col)

        # P0.6 migration: audit_log may still use the legacy schema whose id is
        # INTEGER PRIMARY KEY AUTOINCREMENT (the canonical schema uses a TEXT
        # UUID id written by write_audit_log).  ALTER TABLE cannot change the
        # id column type, so the table must be rebuilt while preserving rows.
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
        )
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(audit_log)")
            audit_info = {row[1]: row for row in cursor.fetchall()}
            _id_type = (audit_info.get("id") or [None, None, ""])[2] or ""
            if "INT" in _id_type.upper():
                cursor.executescript("""
                    ALTER TABLE audit_log RENAME TO audit_log_legacy;
                    CREATE TABLE audit_log (
                        id           TEXT PRIMARY KEY,
                        memory_id    TEXT,
                        action       TEXT NOT NULL,
                        agent_id     TEXT,
                        persona_id   TEXT,
                        timestamp    TEXT DEFAULT (datetime('now')),
                        details      TEXT DEFAULT '{}',
                        checksum     TEXT
                    );
                    INSERT INTO audit_log
                        (id, memory_id, action, persona_id, timestamp, details)
                        SELECT CAST(id AS TEXT), memory_id, action,
                               persona_id, timestamp, COALESCE(metadata, '{}')
                        FROM audit_log_legacy;
                    DROP TABLE audit_log_legacy;
                """)
            else:
                for _col, _decl in [
                    ("agent_id", "TEXT"),
                    ("details", "TEXT DEFAULT '{}'"),
                    ("checksum", "TEXT"),
                ]:
                    if _col not in audit_info:
                        cursor.execute(
                            "ALTER TABLE audit_log ADD COLUMN %s %s" % (_col, _decl)
                        )

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
                agent_id    TEXT DEFAULT 'default',
                app_id      TEXT,
                content     TEXT NOT NULL,
                tokenized_content TEXT,
                role        TEXT DEFAULT 'user',
                importance  REAL DEFAULT 0.5,
                tags        TEXT DEFAULT '[]',
                category    TEXT DEFAULT 'general',
                sha256_hash TEXT,
                status      TEXT DEFAULT 'active',
                version     INTEGER DEFAULT 1,
                ttl_seconds INTEGER,
                last_accessed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                access_count INTEGER DEFAULT 0,
                importance_score REAL DEFAULT 0.0,
                content_hash TEXT,
                conflict_group_id TEXT,
                is_resolved INTEGER DEFAULT 0,
                modality    TEXT DEFAULT 'text',
                metadata    TEXT DEFAULT '{}',
                source_uri  TEXT,
                memory_layer TEXT,
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
                id           TEXT PRIMARY KEY,
                memory_id    TEXT,
                action       TEXT NOT NULL,
                agent_id     TEXT,
                persona_id   TEXT,
                timestamp    TEXT DEFAULT (datetime('now')),
                details      TEXT DEFAULT '{}',
                checksum     TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_audit_memory ON audit_log(memory_id);
            CREATE INDEX IF NOT EXISTS idx_audit_agent_time ON audit_log(agent_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_persona_time ON audit_log(persona_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_action_time ON audit_log(action, timestamp);

            CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(agent_id);
            CREATE INDEX IF NOT EXISTS idx_memories_app ON memories(app_id);
            CREATE INDEX IF NOT EXISTS idx_memories_ttl ON memories(ttl_seconds, created_at);
            CREATE INDEX IF NOT EXISTS idx_memories_last_access ON memories(last_accessed_at);
            CREATE INDEX IF NOT EXISTS idx_memories_modality ON memories(modality);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash
                ON memories(persona_id, agent_id, content_hash)
                WHERE content_hash IS NOT NULL;
        """)
        self._conn.commit()

        # ── Agent 权重表 ────────────────────────────────────────────
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS agent_weights (
                agent_id   TEXT PRIMARY KEY,
                weight     REAL NOT NULL DEFAULT 1.0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self._conn.commit()

        # ── 记忆关联表 ────────────────────────────────────────────
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS memory_links (
                id         TEXT PRIMARY KEY,
                source_id  TEXT NOT NULL,
                target_id  TEXT NOT NULL,
                link_type  TEXT NOT NULL DEFAULT 'semantic',
                strength   REAL NOT NULL DEFAULT 0.5,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_memory_links_source
                ON memory_links(source_id);
            CREATE INDEX IF NOT EXISTS idx_memory_links_target
                ON memory_links(target_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_links_pair_type
                ON memory_links(source_id, target_id, link_type);
        """)
        self._conn.commit()

        # ── 记忆图谱（entities + relations）──────────────────────
        # P1 migration: entities 表统一为 entity_id 主键结构（与 ER 提取器
        # 写入的表结构一致）。旧库 entities 表为 id 主键
        # （id/name/type/properties/created_at），检测到无 entity_id 列时
        # 重建并保留数据。
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entities'"
        )
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(entities)")
            ent_cols = [row[1] for row in cursor.fetchall()]
            if "entity_id" not in ent_cols:
                cursor.executescript("""
                    ALTER TABLE entities RENAME TO entities_legacy;
                    CREATE TABLE entities (
                        entity_id  TEXT PRIMARY KEY,
                        name       TEXT NOT NULL UNIQUE,
                        type       TEXT NOT NULL DEFAULT 'concept',
                        frequency  INTEGER DEFAULT 1,
                        first_seen TEXT,
                        embedding  BLOB,
                        summary    TEXT
                    );
                    INSERT INTO entities (entity_id, name, type, summary, first_seen)
                        SELECT id, name, type, properties, created_at
                        FROM entities_legacy;
                    DROP TABLE entities_legacy;
                """)
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                entity_id  TEXT PRIMARY KEY,
                name       TEXT NOT NULL UNIQUE,
                type       TEXT NOT NULL DEFAULT 'concept',
                frequency  INTEGER DEFAULT 1,
                first_seen TEXT,
                embedding  BLOB,
                summary    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_entities_name
                ON entities(name);
            CREATE INDEX IF NOT EXISTS idx_entities_type
                ON entities(type);

            CREATE TABLE IF NOT EXISTS relations (
                id          TEXT PRIMARY KEY,
                subject_id  TEXT NOT NULL,
                predicate   TEXT NOT NULL DEFAULT 'related_to',
                object_id   TEXT NOT NULL,
                properties  TEXT DEFAULT '{}',
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                valid_from  TEXT,
                valid_to    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_relations_subject
                ON relations(subject_id);
            CREATE INDEX IF NOT EXISTS idx_relations_object
                ON relations(object_id);
            CREATE INDEX IF NOT EXISTS idx_relations_predicate
                ON relations(predicate);
        """)
        self._conn.commit()

        # ── relations 时序列迁移（2026-08-15, R2 edge bi-temporal）──
        # 旧库 relations 无 valid_from/valid_to，幂等补列。
        # 注意：索引必须在补列之后创建（旧库 executescript 内建索引会
        # 因列不存在报 OperationalError）。
        try:
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(relations)").fetchall()]
            if "valid_from" not in cols:
                self._conn.execute("ALTER TABLE relations ADD COLUMN valid_from TEXT")
            if "valid_to" not in cols:
                self._conn.execute("ALTER TABLE relations ADD COLUMN valid_to TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_relations_valid "
                "ON relations(valid_from, valid_to)"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # ── 身份锚点表 ─────────────────────────────────────────────
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS identity_anchors (
                id          TEXT PRIMARY KEY,
                agent_id    TEXT NOT NULL,
                anchor_type TEXT NOT NULL,
                content     TEXT NOT NULL DEFAULT '{}',
                version     INTEGER NOT NULL DEFAULT 1,
                checksum    TEXT,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_identity_anchors_agent
                ON identity_anchors(agent_id);
            CREATE INDEX IF NOT EXISTS idx_identity_anchors_type
                ON identity_anchors(agent_id, anchor_type);

            -- DCSA-EJP: 双循环宪法自审计
            CREATE TABLE IF NOT EXISTS audit_runs (
                run_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                task TEXT DEFAULT '',
                executor_result TEXT DEFAULT '{}',
                auditor_result TEXT DEFAULT '{}',
                disagreement_flag INTEGER DEFAULT 0,
                packet_json TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_audit_runs_agent ON audit_runs(agent_id);
            CREATE INDEX IF NOT EXISTS idx_audit_runs_time ON audit_runs(created_at);

            CREATE TABLE IF NOT EXISTS constitutional_violations (
                violation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                invariant TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'medium',
                context TEXT DEFAULT '{}',
                timestamp TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (run_id) REFERENCES audit_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_violations_run ON constitutional_violations(run_id);
            CREATE INDEX IF NOT EXISTS idx_violations_invariant ON constitutional_violations(invariant);

            -- A2A Protocol: cross-agent task tracking
            CREATE TABLE IF NOT EXISTS a2a_tasks (
                task_id     TEXT PRIMARY KEY,
                from_agent  TEXT NOT NULL,
                to_agent    TEXT NOT NULL,
                payload     TEXT DEFAULT '{}',
                status      TEXT DEFAULT 'pending',
                result      TEXT,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_a2a_tasks_from ON a2a_tasks(from_agent);
            CREATE INDEX IF NOT EXISTS idx_a2a_tasks_to ON a2a_tasks(to_agent);
            CREATE INDEX IF NOT EXISTS idx_a2a_tasks_status ON a2a_tasks(status);

            CREATE TABLE IF NOT EXISTS agent_registry (
                agent_id        TEXT PRIMARY KEY,
                card_json       TEXT NOT NULL DEFAULT '{}',
                registered_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                last_heartbeat  TEXT DEFAULT CURRENT_TIMESTAMP,
                status          TEXT DEFAULT 'active'
            );
            CREATE INDEX IF NOT EXISTS idx_agent_registry_status ON agent_registry(status);
        """)
        self._conn.commit()

        # ── FTS5 全文搜索虚拟表 ─────────────────────────────────────
        self._create_fts5()

        # Ensure default tenant exists
        cursor.execute("INSERT OR IGNORE INTO tenants (tenant_id, name) VALUES (?, ?)",
                      ("default", "default"))
        self._conn.commit()  # 立即提交，避免未提交写事务长期占用锁（多进程共享大库时导致 database is locked）

    def _create_fts5(self) -> None:
        """创建 FTS5 虚拟表及同步触发器。

        使用 tokenized_content 列进行索引：
        - CJK 内容会通过 Python 端 jieba 分词后写入 tokenized_content
        - 非 CJK 内容 tokenized_content 为 NULL，触发器回退到原始 content
        """
        cursor = self._conn.cursor()
        try:
            # 兼容旧数据库：如果缺少 tokenized_content 列则添加
            try:
                cursor.execute(
                    "ALTER TABLE memories ADD COLUMN tokenized_content TEXT"
                )
            except sqlite3.OperationalError:
                pass  # 列已存在

            # B5 迁移（2026-08-15）：旧库 FTS5 表是 external content
            # （content='memories'），会忽略触发器写入值、直接索引
            # memories.content —— 加密后该列是密文 → 检索失效。
            # 检测到旧结构则 DROP 重建为独立表，并回填索引。
            try:
                fts_def = cursor.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
                ).fetchone()
                if fts_def and "content='memories'" in (fts_def["sql"] or ""):
                    cursor.execute("DROP TABLE memories_fts")
                    for trg in ("memories_ai", "memories_ad", "memories_au"):
                        cursor.execute(f"DROP TRIGGER IF EXISTS {trg}")
                    cursor.execute("""
                        CREATE VIRTUAL TABLE memories_fts
                        USING fts5(content, category, tags)
                    """)
                    cursor.execute("""
                        INSERT INTO memories_fts(rowid, content, category, tags)
                        SELECT rowid, COALESCE(tokenized_content, content), category, tags
                        FROM memories
                    """)
                    logger.info("B5: external-content memories_fts migrated to standalone")
            except sqlite3.OperationalError:
                pass  # 表尚不存在（全新库）

            cursor.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(content, category, tags);

                -- INSERT 触发器：使用 tokenized_content（带 content 兜底）
                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories
                BEGIN
                    INSERT INTO memories_fts(rowid, content, category, tags)
                    VALUES (new.rowid,
                            COALESCE(new.tokenized_content, new.content),
                            new.category, new.tags);
                END;

                -- DELETE 触发器：同步删除 FTS 索引（独立表用 DELETE 语法）
                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories
                BEGIN
                    DELETE FROM memories_fts WHERE rowid = old.rowid;
                END;

                -- UPDATE 触发器：同步更新 FTS 索引
                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories
                BEGIN
                    DELETE FROM memories_fts WHERE rowid = old.rowid;
                    INSERT INTO memories_fts(rowid, content, category, tags)
                    VALUES (new.rowid,
                            COALESCE(new.tokenized_content, new.content),
                            new.category, new.tags);
                END;
            """)
            self._conn.commit()
        except sqlite3.OperationalError:
            # FTS5 可能不可用（旧版 SQLite），静默回退
            pass

    # ── 辅助方法 ─────────────────────────────────────────────────────

    def _compute_sha256(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # ── 存储加密辅助（B5, 2026-08-15）───────────────────────────────

    def _encrypt_content(self, content: str) -> str:
        """写入前加密（未启用时原样返回）。"""
        if self._cipher is None:
            return content
        return self._cipher.encrypt(content)

    def _decrypt_content(self, content: str) -> str:
        """读取后解密（未加密的历史数据原样返回）。"""
        if self._cipher is None or not content:
            return content
        return self._cipher.decrypt(content)

    def _tokenized_for_storage(self, plain_content: str, tokenized: Optional[str]) -> Optional[str]:
        """确定写入 tokenized_content 列的值。

        - 未加密：保持原逻辑（CJK 分词，非 CJK 为 None 由触发器回退 content）
        - 加密后：content 列是密文，FTS 触发器 COALESCE(tokenized, content)
          会回退到密文 → 检索失效。因此加密模式下非 CJK 内容也写入
          明文 content 作为 tokenized_content，保证 FTS 可搜。
        """
        if self._cipher is not None and not tokenized:
            return plain_content
        return tokenized

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

    # ── 向后兼容：旧调用者转发 ───────────────────────────────────
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
        """检查 FTS5 是否可用（2026-08-15 v2：线程本地只读连接）。"""
        try:
            conn = self._get_read_conn()
            if not conn:
                return False
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
            )
            return cursor.fetchone() is not None
        except Exception:
            return False

    # ── 批量写入 ─────────────────────────────────────────────────────

    @_safe_write
    def ingest_batch(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """批量写入记忆记录。

        攒够 100 条或 5 秒后统一 commit。如果 records 为空，
        仅做 flush 检查。

        Args:
            records: 要写入的记录列表，每项包含 store_memory 参数。

        Returns:
            每条记录的写入结果列表。
        """
        with self._write_lock:

            results = []
            for rec in records:
                content = rec.get("content", "")
                result = self.store_memory(
                    content=content,
                    persona_id=rec.get("persona_id", "default"),
                    session_id=rec.get("session_id"),
                    tenant_id=rec.get("tenant_id", "default"),
                    agent_id=rec.get("agent_id", "default"),
                    role=rec.get("role", "user"),
                    importance=rec.get("importance", 0.5),
                    tags=rec.get("tags"),
                    category=rec.get("category", "general"),
                    ttl_seconds=rec.get("ttl_seconds"),
                    modality=rec.get("modality", "text"),
                    metadata=rec.get("metadata"),
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
            tokenized = self._tokenize_content_for_fts(stored_content)
            plain_content = stored_content
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

    # ── 搜索 ─────────────────────────────────────────────────────────

    def search_memories(
        self,
        query: str,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        app_id: Optional[str] = None,
        session_id: Optional[str] = None,
        category: Optional[str] = None,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """搜索记忆。

        优先使用 FTS5 全文搜索，如果不可用则回退到 LIKE 模糊搜索。
        支持 agent_id / persona_id / session_id / app_id / category 的任意 AND 组合。

        2026-08-15（压测修复 v2）：改用线程本地只读连接（_get_read_conn）——
        WAL 下多读并行、零锁竞争；touch 已异步化（入队），读路径无写。
        不再需要 _write_lock 串行化（每线程独立连接，无游标共享）。
        """
        conn = self._get_read_conn()
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
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if app_id:
            conditions.append("app_id = ?")
            params.append(app_id)
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if category:
            conditions.append("category = ?")
            params.append(category)

        where = " AND ".join(conditions)

        results: List[Dict[str, Any]] = []

        # 尝试 FTS5 搜索
        if self._fts_available():
            try:
                fts_results = self._search_fts(query, params, where, top_k)
                # FTS5 可能对 CJK 文字分词不完整，返回空结果，
                # 此时仍应回退到 LIKE 搜索
                if fts_results:
                    results = fts_results
            except Exception:
                # FTS5 搜索失败，回退到 LIKE
                pass

        if not results:
            # 回退：LIKE 模糊搜索
            results = self._search_like(query, params, where, top_k)

        # ── 自动 touch：异步入队（2026-08-15 起读路径零写阻塞）────
        if results:
            memory_ids = [r["memory_id"] for r in results]
            self._touch_batch(memory_ids)

        return results

    # ── 中英混合分词辅助方法 ───────────────────────────────────────

    _CJK_PATTERN = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\uff00-\uffef]')

    @staticmethod
    def _tokenize_fts_query(query: str) -> List[str]:
        """将查询拆分为 FTS5 词组。

        CJK 文字使用 jieba 分词后直接作为词组（如 "机密 记忆" 的查询
        切为 ["机密", "记忆"]）。注意：unicode61 tokenizer 把连续 CJK
        字符当作单个 token（如 "机密记忆" 是一个 token），因此不能在
        字间插入空格（"机 密 记 忆" 会变成 4 个单字 token 永远匹配
        不到索引里的整词 token）。
        非 CJK 文本保持原始空格分词。
        """
        if not SQLiteAdapter._CJK_PATTERN.search(query):
            return query.strip().split()

        try:
            import jieba
        except ImportError:
            return query.strip().split()

        tokens = list(jieba.cut(query))
        result: List[str] = []
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            result.append(token)
        return result

    @staticmethod
    def _tokenize_content_for_fts(content: str) -> Optional[str]:
        """对写入内容做 jieba 分词，返回用于 FTS5 索引的文本。

        - CJK 内容：jieba 分词后空格连接，供 FTS5 unicode61 正确索引
        - 纯非 CJK 内容：返回 None，由触发器回退到原始 content
        """
        if not SQLiteAdapter._CJK_PATTERN.search(content):
            return None

        try:
            import jieba
        except ImportError:
            return None

        tokens = list(jieba.cut(content))
        return ' '.join(token for token in tokens if token.strip())

    def _search_fts(
        self, query: str, params: List[Any], where: str, top_k: int
    ) -> List[Dict[str, Any]]:
        """使用 FTS5 全文搜索（支持词间空格分词和 jieba 中文分词）。

        2026-08-15（压测修复 v2）：用线程本地只读连接（调用方 search_memories
        已取读连接；本方法自取，兼容独立调用）。
        """
        terms = self._tokenize_fts_query(query)
        # 2026-08-15（压测修复）：转义 FTS5 查询特殊字符（" 引号等），
        # 防止 MATCH 语法错误导致 "bad parameter or other API misuse"。
        safe_terms = [t.replace('"', '""') for t in terms if t.strip()]
        fts_query = " OR ".join(f'"{t}"*' for t in safe_terms)
        if not fts_query:
            return []

        sql = f"""
            SELECT m.memory_id, m.content, m.persona_id, m.session_id, m.role,
                   m.importance, m.tags, m.category, m.modality, m.created_at,
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
        conn = self._get_read_conn()
        if not conn:
            return []

        full_params = [fts_query] + params + [top_k]
        cursor = conn.execute(sql, full_params)

        results = []
        # 先收集，再 min-max 归一化分数
        rows = cursor.fetchall()
        if not rows:
            return []

        # 提取 rank 值用于归一化（防御：并发错位/异常数据时 rank 可能为 None）
        raw_scores = [r for r in (row["score"] for row in rows) if r is not None]
        min_rank = min(raw_scores) if raw_scores else 0
        max_rank = max(raw_scores) if raw_scores else 1
        rank_range = max_rank - min_rank if max_rank != min_rank else 1.0

        for i, row in enumerate(rows):
            # FTS5 rank 是负值（越负越相关），我们翻转成 0-1 分数
            rank = row["score"] if row["score"] is not None else min_rank
            norm_score = 1.0 - (rank - min_rank) / rank_range
            content = self._decrypt_content(row["content"])
            results.append({
                "memory_id": row["memory_id"],
                "content": content,
                "content_preview": content[:100],
                "persona_id": row["persona_id"],
                "session_id": row["session_id"],
                "role": row["role"],
                "importance": row["importance"],
                "tags": json.loads(row["tags"]),
                "category": row["category"],
                "modality": row["modality"],
                "created_at": row["created_at"],
                "score": round(norm_score, 4),
            })

        return results

    def _search_like(
        self, query: str, params: List[Any], where: str, top_k: int
    ) -> List[Dict[str, Any]]:
        """回退到 LIKE 模糊搜索（2026-08-15 v2：线程本地只读连接）。"""
        conn = self._get_read_conn()
        if not conn:
            return []

        like_term = f"%{query}%"
        # params 已经包含 WHERE 中的 ?，只需要加上 LIKE 参数和 LIMIT
        full_params = params + [like_term, top_k]

        # params 来自 WHERE 条件，like_term * 2 用于 content+tags 过滤，top_k 用于 LIMIT
        cursor = conn.execute(f"""
            SELECT memory_id, content, persona_id, session_id, role,
                   importance, tags, category, modality, created_at,
                   0.8 as score
            FROM memories
            WHERE {where}
              AND (content LIKE ? OR tags LIKE ?)
            ORDER BY importance DESC, created_at DESC
            LIMIT ?
        """, params + [like_term, like_term, top_k])

        results = []
        for row in cursor.fetchall():
            content = self._decrypt_content(row["content"])
            results.append({
                "memory_id": row["memory_id"],
                "content": content,
                "content_preview": content[:100],
                "persona_id": row["persona_id"],
                "session_id": row["session_id"],
                "role": row["role"],
                "importance": row["importance"],
                "tags": json.loads(row["tags"]),
                "category": row["category"],
                "modality": row["modality"],
                "created_at": row["created_at"],
                "score": row["score"],
            })

        return results

    # ── 单条查询 ─────────────────────────────────────────────────────

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

    def get_all_memories(self, agent_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        """Get all active memories across all personas/tenants, optionally filtered by agent_id.

        2026-08-15（压测修复 v2）：线程本地只读连接（纯读，无锁）。
        """
        conn = self._get_read_conn()
        if not conn:
            return []

        if agent_id:
            cursor = conn.execute("""
                SELECT * FROM memories
                WHERE status = 'active' AND agent_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (agent_id, limit))
        else:
            cursor = conn.execute("""
                SELECT * FROM memories
                WHERE status = 'active'
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,))
        rows = [dict(row) for row in cursor.fetchall()]
        for r in rows:
            if r.get("content"):
                r["content"] = self._decrypt_content(r["content"])
        return rows

    # ── TTL & 自动老化 ────────────────────────────────────────────────

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

    # ── 去重与冲突解决 ─────────────────────────────────────────────

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

    # ── Agent 权重管理 ─────────────────────────────────────────────

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

    # ── 记忆关联（memory_links）───────────────────────────────────

    def create_memory_link(self, source_id: str, target_id: str,
                           link_type: str = "semantic",
                           strength: float = 0.5) -> Dict[str, Any]:
        """创建记忆关联链接。

        Args:
            source_id: 源记忆 ID。
            target_id: 目标记忆 ID。
            link_type: 链接类型（co_occurrence/semantic/causal/same_task）。
            strength: 关联强度 0-1。

        Returns:
            创建结果。
        """
        with self._write_lock:

            conn = self._conn
            if not conn:
                return {"error": "Not connected"}
            if source_id == target_id:
                return {"error": "Cannot link memory with itself"}
            link_id = hashlib.sha256(
                f"{source_id}:{target_id}:{link_type}".encode()
            ).hexdigest()[:32]
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("""
                INSERT OR IGNORE INTO memory_links
                    (id, source_id, target_id, link_type, strength, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (link_id, source_id, target_id, link_type, strength, now))
            conn.commit()
            return {
                "id": link_id, "source_id": source_id, "target_id": target_id,
                "link_type": link_type, "strength": strength, "created_at": now,
            }

    def get_linked_memories(self, memory_id: str,
                            min_strength: float = 0.0) -> List[Dict[str, Any]]:
        """获取与指定记忆关联的所有链接（按强度降序）。

        Args:
            memory_id: 记忆 ID。
            min_strength: 最低关联强度阈值。

        Returns:
            链接列表。
        """
        conn = self._conn
        if not conn:
            return []
        cursor = conn.execute("""
            SELECT ml.*, m.content AS target_content
            FROM memory_links ml
            LEFT JOIN memories m ON m.memory_id = ml.target_id
            WHERE ml.source_id = ?
              AND ml.strength >= ?
            ORDER BY ml.strength DESC
        """, (memory_id, min_strength))
        return [dict(row) for row in cursor.fetchall()]

    def strengthen_link(self, link_id: str,
                        increment: float = 0.1) -> Dict[str, Any]:
        """增强链接强度（上限 1.0）。

        Args:
            link_id: 链接 ID。
            increment: 增量值。

        Returns:
            操作结果。
        """
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}
        conn.execute("""
            UPDATE memory_links
            SET strength = MIN(strength + ?, 1.0)
            WHERE id = ?
        """, (increment, link_id))
        conn.commit()
        cursor = conn.execute(
            "SELECT * FROM memory_links WHERE id = ?", (link_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else {"error": "Link not found"}

    def weaken_link(self, link_id: str,
                    decrement: float = 0.1) -> Dict[str, Any]:
        """削弱链接强度（下限 0.0）。

        Args:
            link_id: 链接 ID。
            decrement: 减量值。

        Returns:
            操作结果。
        """
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}
        conn.execute("""
            UPDATE memory_links
            SET strength = MAX(strength - ?, 0.0)
            WHERE id = ?
        """, (decrement, link_id))
        conn.commit()
        cursor = conn.execute(
            "SELECT * FROM memory_links WHERE id = ?", (link_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else {"error": "Link not found"}

    def delete_memory_link(self, link_id: str) -> bool:
        """删除指定链接。

        Args:
            link_id: 链接 ID。

        Returns:
            是否删除成功。
        """
        conn = self._conn
        if not conn:
            return False
        cursor = conn.execute(
            "DELETE FROM memory_links WHERE id = ?", (link_id,)
        )
        conn.commit()
        return cursor.rowcount > 0

    def get_all_links(self, memory_id: str) -> List[Dict[str, Any]]:
        """获取某记忆和其所有关联链接和反向链接。

        Args:
            memory_id: 记忆 ID。

        Returns:
            包含 outgoing/incoming 的字典。
        """
        conn = self._conn
        if not conn:
            return {"outgoing": [], "incoming": []}
        outgoing = conn.execute(
            "SELECT * FROM memory_links WHERE source_id = ?", (memory_id,)
        ).fetchall()
        incoming = conn.execute(
            "SELECT * FROM memory_links WHERE target_id = ?", (memory_id,)
        ).fetchall()
        return {
            "outgoing": [dict(r) for r in outgoing],
            "incoming": [dict(r) for r in incoming],
        }

    # ── 记忆图谱（entities + relations）───────────────────────────

    @staticmethod
    def _parse_entity_properties(summary: Optional[str]) -> Dict[str, Any]:
        """安全解析 entities.summary：合法 JSON 视为 properties，否则视为摘要文本。"""
        if not summary:
            return {}
        try:
            parsed = json.loads(summary)
            if isinstance(parsed, dict):
                return parsed
            return {"summary": summary}
        except (ValueError, TypeError):
            return {"summary": summary}

    def upsert_entity(self, name: str, etype: str = "concept",
                      properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建或更新实体（幂等：按 name + type 去重）。

        Args:
            name: 实体名称。
            etype: 实体类型（person/project/file/agent/task/concept/tag）。
            properties: 附加属性 JSON。

        Returns:
            实体字典。
        """
        with self._write_lock:

            conn = self._conn
            if not conn:
                return {"error": "Not connected"}
            props_json = json.dumps(properties or {}, ensure_ascii=False)
            now = datetime.now(timezone.utc).isoformat()

            # 按 name + type 查找已有（entities 表主键为 entity_id）
            cursor = conn.execute(
                "SELECT entity_id FROM entities WHERE name = ? AND type = ?", (name, etype)
            )
            row = cursor.fetchone()
            if row:
                entity_id = row["entity_id"]
                conn.execute(
                    "UPDATE entities SET summary = ?, first_seen = ? WHERE entity_id = ?",
                    (props_json, now, entity_id),
                )
            else:
                entity_id = f"ent_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    "INSERT INTO entities (entity_id, name, type, summary, first_seen) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (entity_id, name, etype, props_json, now),
                )
            conn.commit()
            return {"id": entity_id, "name": name, "type": etype,
                    "properties": (properties or {}), "created_at": now}

    def get_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """查询实体详情（含关联记忆与关系）。

        Args:
            entity_id: 实体 ID。

        Returns:
            实体详情字典，无则 None。
        """
        conn = self._conn
        if not conn:
            return None
        cursor = conn.execute("SELECT * FROM entities WHERE entity_id = ?", (entity_id,))
        row = cursor.fetchone()
        if not row:
            return None
        entity = dict(row)
        entity.pop("embedding", None)
        entity["id"] = entity.get("entity_id")
        entity["properties"] = self._parse_entity_properties(entity.get("summary"))
        entity["created_at"] = entity.get("first_seen")

        # 关联关系
        rel_out = conn.execute(
            "SELECT * FROM relations WHERE subject_id = ?", (entity_id,)
        ).fetchall()
        rel_in = conn.execute(
            "SELECT * FROM relations WHERE object_id = ?", (entity_id,)
        ).fetchall()
        entity["relations_outgoing"] = [dict(r) for r in rel_out]
        entity["relations_incoming"] = [dict(r) for r in rel_in]
        return entity

    def search_entities(self, name: Optional[str] = None,
                        etype: Optional[str] = None,
                        limit: int = 20) -> List[Dict[str, Any]]:
        """搜索实体（按名称模糊 + 类型精确）。

        Args:
            name: 名称关键词（LIKE 模糊匹配，可选）。
            etype: 实体类型精确过滤（可选）。
            limit: 返回数量。

        Returns:
            实体列表。

        2026-08-15（压测修复 v2）：线程本地只读连接（纯读，无锁）。
        """
        conn = self._get_read_conn()
        if not conn:
            return []
        sql = "SELECT * FROM entities WHERE 1=1"
        params: list = []
        if name:
            sql += " AND name LIKE ?"
            params.append(f"%{name}%")
        if etype:
            sql += " AND type = ?"
            params.append(etype)
        sql += " ORDER BY first_seen DESC LIMIT ?"
        params.append(limit)
        cursor = conn.execute(sql, params)
        results = []
        for row in cursor.fetchall():
            d = dict(row)
            d.pop("embedding", None)
            d["id"] = d.get("entity_id")
            d["properties"] = self._parse_entity_properties(d.get("summary"))
            d["created_at"] = d.get("first_seen")
            results.append(d)
        return results

    def create_relation(self, subject_id: str, predicate: str,
                        object_id: str,
                        properties: Optional[Dict] = None,
                        valid_from: Optional[str] = None,
                        valid_to: Optional[str] = None) -> Dict[str, Any]:
        """创建关系（幂等：按 subject+predicate+object 去重）。

        Args:
            subject_id: 主体实体 ID。
            predicate: 谓词。
            object_id: 客体实体 ID。
            properties: 附加属性 JSON。
            valid_from: 边生效起始时间（ISO8601，缺省=now；edge bi-temporal）。
            valid_to: 边失效时间（ISO8601，缺省 None=仍有效）。

        Returns:
            关系字典。
        """
        with self._write_lock:

            conn = self._conn
            if not conn:
                return {"error": "Not connected"}
            if subject_id == object_id:
                return {"error": "Cannot create self-referencing relation"}
            props_json = json.dumps(properties or {}, ensure_ascii=False)
            now = datetime.now(timezone.utc).isoformat()
            rel_id = hashlib.sha256(
                f"{subject_id}:{predicate}:{object_id}".encode()
            ).hexdigest()[:32]
            conn.execute("""
                INSERT OR IGNORE INTO relations
                    (id, subject_id, predicate, object_id, properties, created_at,
                     valid_from, valid_to)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (rel_id, subject_id, predicate, object_id, props_json, now,
                  valid_from or now, valid_to))
            conn.commit()
            return {"id": rel_id, "subject_id": subject_id, "predicate": predicate,
                    "object_id": object_id, "properties": (properties or {}),
                    "created_at": now, "valid_from": valid_from or now,
                    "valid_to": valid_to}

    def query_relations(self, subject_id: Optional[str] = None,
                        predicate: Optional[str] = None,
                        object_id: Optional[str] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        """查询关系（任意组合过滤条件）。

        Args:
            subject_id: 主体 ID（可选）。
            predicate: 谓词（可选）。
            object_id: 客体 ID（可选）。
            limit: 返回数量。

        Returns:
            关系列表。

        2026-08-15（压测修复 v2）：线程本地只读连接（纯读，无锁）。
        """
        conn = self._get_read_conn()
        if not conn:
            return []
        sql = "SELECT * FROM relations WHERE 1=1"
        params: list = []
        if subject_id:
            sql += " AND subject_id = ?"
            params.append(subject_id)
        if predicate:
            sql += " AND predicate = ?"
            params.append(predicate)
        if object_id:
            sql += " AND object_id = ?"
            params.append(object_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def query_relations_at(self, at_time: str,
                           subject_id: Optional[str] = None,
                           predicate: Optional[str] = None,
                           object_id: Optional[str] = None,
                           limit: int = 50) -> List[Dict[str, Any]]:
        """时点查询：返回指定时间点有效的边（edge bi-temporal）。

        valid_from <= at_time AND (valid_to IS NULL OR valid_to > at_time)。

        Args:
            at_time: 查询时间点（ISO8601）。
            subject_id / predicate / object_id: 可选过滤。
            limit: 返回数量。

        Returns:
            该时点有效的关系列表。
        """
        conn = self._conn
        if not conn:
            return []
        sql = ("SELECT * FROM relations WHERE valid_from <= ? "
               "AND (valid_to IS NULL OR valid_to > ?)")
        params: list = [at_time, at_time]
        if subject_id:
            sql += " AND subject_id = ?"
            params.append(subject_id)
        if predicate:
            sql += " AND predicate = ?"
            params.append(predicate)
        if object_id:
            sql += " AND object_id = ?"
            params.append(object_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def traverse(self, start_id: str,
                 max_hops: int = 3) -> Dict[str, Any]:
        """多跳遍历，返回以 start_id 为起点的子图。

        Args:
            start_id: 起始实体 ID。
            max_hops: 最大跳数（1-5，默认 3）。

        Returns:
            {"nodes": [...], "edges": [...]}

        2026-08-15（压测修复 v2）：线程本地只读连接（纯读，无锁；
        query_graph 内调用，每线程独立连接天然安全）。
        """
        conn = self._get_read_conn()
        if not conn:
            return {"nodes": [], "edges": []}
        max_hops = max(1, min(max_hops, 5))

        visited: set = set()
        node_ids: set = {start_id}
        edges: list = []

        for hop in range(max_hops):
            if not node_ids:
                break
            visited |= node_ids
            next_ids: set = set()
            for nid in node_ids:
                for direction in ("subject", "object"):
                    col = f"{direction}_id"
                    cursor = conn.execute(
                        f"SELECT * FROM relations WHERE {col} = ?", (nid,)
                    )
                    for row in cursor.fetchall():
                        r = dict(row)
                        other = r["object_id"] if direction == "subject" else r["subject_id"]
                        edges.append(r)
                        if other not in visited:
                            next_ids.add(other)
            node_ids = next_ids - visited

        all_nodes = set()
        for e in edges:
            all_nodes.add(e["subject_id"])
            all_nodes.add(e["object_id"])
        all_nodes.add(start_id)

        # 批量查询实体
        node_list: list = []
        for nid in all_nodes:
            cursor = conn.execute("SELECT * FROM entities WHERE entity_id = ?", (nid,))
            row = cursor.fetchone()
            if row:
                n = dict(row)
                n.pop("embedding", None)
                n["id"] = n.get("entity_id")
                n["properties"] = self._parse_entity_properties(n.get("summary"))
                n["created_at"] = n.get("first_seen")
                node_list.append(n)

        return {"nodes": node_list, "edges": edges}

    @_safe_write
    def create_entity(self, name: str, etype: str = "concept",
                      properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建新实体（非幂等，实体已存在时返回错误）。

        Args:
            name: 实体名称。
            etype: 实体类型。
            properties: 附加属性 JSON。

        Returns:
            实体字典；若实体已存在则返回 {"error": "Entity exists", "entity_id": "..."}。
        """
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}
        cursor = conn.execute(
            "SELECT entity_id FROM entities WHERE name = ? AND type = ?", (name, etype)
        )
        row = cursor.fetchone()
        if row:
            return {"error": "Entity exists", "entity_id": row["entity_id"]}
        return self.upsert_entity(name=name, etype=etype, properties=properties)

    def get_entity_by_name(self, name: str,
                           etype: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """按名称精确匹配单个实体。

        Args:
            name: 实体名称（精确匹配）。
            etype: 实体类型过滤（可选）。

        Returns:
            实体详情字典，无则 None。
        """
        conn = self._conn
        if not conn:
            return None
        sql = "SELECT entity_id FROM entities WHERE name = ?"
        params: list = [name]
        if etype:
            sql += " AND type = ?"
            params.append(etype)
        sql += " LIMIT 1"
        cursor = conn.execute(sql, params)
        row = cursor.fetchone()
        if not row:
            return None
        return self.get_entity(row["entity_id"])

    def get_neighbors(self, entity_id: str) -> Dict[str, Any]:
        """获取实体的 1-hop 邻居。

        Args:
            entity_id: 实体 ID。

        Returns:
            {"entity": ..., "neighbors": [实体列表], "relations": [关系列表]}
        """
        subgraph = self.traverse(entity_id, max_hops=1)
        entity = self.get_entity(entity_id)
        neighbors = []
        nodes_seen = {entity_id}
        if entity:
            entity.pop("relations_outgoing", None)
            entity.pop("relations_incoming", None)
        for node in subgraph.get("nodes", []):
            nid = node.get("entity_id", "") or node.get("id", "")
            if nid != entity_id and nid not in nodes_seen:
                nodes_seen.add(nid)
                neighbors.append(node)
        return {
            "entity": entity,
            "neighbors": neighbors,
            "relations": subgraph.get("edges", []),
        }

    def query_graph(self, query: str,
                    limit: int = 20) -> Dict[str, Any]:
        """通过关键词搜索实体，返回以匹配实体为中心的子图。

        Args:
            query: 实体名称关键词。
            limit: 匹配实体数量上限。

        Returns:
            {"match_entities": [...], "nodes": [...], "edges": [...]}
            所有匹配实体及其 1-hop 邻居合并去重的完整子图。

        2026-08-15（压测修复 v2）：线程本地只读连接（纯读，无锁；
        内部 traverse/search_entities 各取本线程读连接，安全）。
        """
        matches = self.search_entities(name=query, limit=limit)
        if not matches:
            return {"match_entities": [], "nodes": [], "edges": []}

        all_nodes: dict = {}
        all_edges: dict = {}
        match_entities = []

        for ent in matches:
            ent_copy = dict(ent)
            ent_copy.pop("relations_outgoing", None)
            ent_copy.pop("relations_incoming", None)
            match_entities.append(ent_copy)
            eid = ent.get("entity_id") or ent.get("id")
            all_nodes[eid] = ent_copy

            sub = self.traverse(eid, max_hops=1)
            for node in sub.get("nodes", []):
                nid = node.get("entity_id", "") or node.get("id", "")
                if nid not in all_nodes:
                    all_nodes[nid] = node
            for edge in sub.get("edges", []):
                eid2 = edge.get("id", "")
                if eid2 not in all_edges:
                    all_edges[eid2] = edge

        return {
            "match_entities": match_entities,
            "nodes": list(all_nodes.values()),
            "edges": list(all_edges.values()),
        }

    # ── 审计日志方法 ───────────────────────────────────────────────────

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

    # ── 身份锚点 CRUD ───────────────────────────────────────────

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

    # ── DCSA-EJP 审计 CRUD ──────────────────────────────────────────

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

    # ── A2A Protocol: Task Management ──────────────────────────────

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

        cursor = conn.execute("SELECT COUNT(DISTINCT agent_id) as c FROM memories")
        agents = cursor.fetchone()["c"]

        cursor = conn.execute("SELECT COUNT(*) as c FROM memory_versions")
        versions = cursor.fetchone()["c"]

        # 检查 FTS5 状态
        fts_ok = self._fts_available()

        # TTL 统计
        cursor = conn.execute("SELECT COUNT(*) as c FROM memories WHERE status = 'expired'")
        expired = cursor.fetchone()["c"]

        cursor = conn.execute("SELECT AVG(access_count) as a FROM memories WHERE status = 'active'")
        avg_access = cursor.fetchone()["a"] or 0

        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        return {
            "adapter": "sqlite",
            "db_path": self.db_path,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "total_memories": total,
            "active_memories": active,
            "expired_memories": expired,
            "total_personas": personas,
            "total_agents": agents,
            "total_versions": versions,
            "fts5_enabled": fts_ok,
            "avg_access_count": round(avg_access, 2),
            "journal_mode": "wal",
            "read_conn_pool": {
                "active": len(self._read_conns),
                "max": self._read_conn_max,
                "overflow": self._read_conn_overflow,
            },
            "agent_weights_configured": len(self.get_agent_weights()),
            "memory_links_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM memory_links"
            ).fetchone()["c"],
            "entity_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM entities"
            ).fetchone()["c"],
            "relation_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM relations"
            ).fetchone()["c"],
            "audit_log_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM audit_log"
            ).fetchone()["c"],
            "identity_anchor_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM identity_anchors"
            ).fetchone()["c"],
            "audit_run_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM audit_runs"
            ).fetchone()["c"],
            "violation_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM constitutional_violations"
            ).fetchone()["c"],
            "a2a_task_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM a2a_tasks"
            ).fetchone()["c"],
            "agent_registry_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM agent_registry"
            ).fetchone()["c"],
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

        # ── 测试12-20：记忆图谱 CRUD（P0.1）─────────────────────────
        print("\n[测试12] create_entity / upsert_entity:")
        e1 = adapter.create_entity("Alice", etype="person")
        assert "id" in e1, "create_entity 失败"
        print(f"  创建实体 Alice: id={e1['id']}")
        e1_dup = adapter.create_entity("Alice", etype="person")
        assert e1_dup.get("error") == "Entity exists", "create_entity 未检测到重复"
        print(f"  重复创建 Alice: error={e1_dup.get('error')}, entity_id={e1_dup.get('entity_id')}")
        e1b = adapter.upsert_entity("Alice", etype="person",
                                     properties={"role": "admin"})
        assert e1b["id"] == e1["id"], "upsert_entity 未复用已有 ID"
        print(f"  upsert Alice (幂等): id={e1b['id']} (应同上)")
        print("  [通过] create_entity / upsert_entity 通过")

        print("\n[测试13] get_entity / get_entity_by_name:")
        e1_detail = adapter.get_entity(e1["id"])
        assert e1_detail["name"] == "Alice", "get_entity 返回名称不对"
        print(f"  get_entity({e1['id']}): name={e1_detail['name']}, type={e1_detail['type']}")
        e1_byname = adapter.get_entity_by_name("Alice")
        assert e1_byname is not None, "get_entity_by_name 未找到"
        assert e1_byname["id"] == e1["id"], "get_entity_by_name 返回错误实体"
        print(f"  get_entity_by_name('Alice'): id={e1_byname['id']}")
        e_none = adapter.get_entity_by_name("NonExistentEntity")
        assert e_none is None, "get_entity_by_name 应返回 None"
        print(f"  get_entity_by_name('NonExistentEntity'): {e_none}")
        print("  [通过] get_entity / get_entity_by_name 通过")

        print("\n[测试14] search_entities:")
        e2 = adapter.upsert_entity("Bob", etype="person")
        adapter.upsert_entity("alice_project", etype="project")
        res = adapter.search_entities(name="ali", limit=10)
        assert len(res) >= 1, "search_entities 应找到至少 1 个结果"
        print(f"  search_entities('ali'): {len(res)} 个结果")
        res_person = adapter.search_entities(etype="person", limit=10)
        assert len(res_person) >= 2, "search_entities type=person 应 ≥ 2"
        print(f"  search_entities(type='person'): {len(res_person)} 个结果")
        print("  [通过] search_entities 通过")

        print("\n[测试15] create_relation:")
        r1 = adapter.create_relation(e1["id"], "works_on", e2["id"],
                                      properties={"role": "collaborator"})
        assert "id" in r1, "create_relation 失败"
        print(f"  创建关系: {r1['subject_id']} -[{r1['predicate']}]-> {r1['object_id']}")
        r1_dup = adapter.create_relation(e1["id"], "works_on", e2["id"])
        assert r1_dup["id"] == r1["id"], "create_relation 未幂等去重"
        print(f"  重复创建: id={r1_dup['id']} (应同上)")
        print("  [通过] create_relation 通过")

        print("\n[测试16] query_relations:")
        rels_subj = adapter.query_relations(subject_id=e1["id"])
        assert len(rels_subj) >= 1, "query_relations 未找到关系"
        print(f"  query_relations(subject_id={e1['id']}): {len(rels_subj)} 条")
        rels_pred = adapter.query_relations(predicate="works_on")
        assert len(rels_pred) >= 1, "query_relations by predicate 未找到"
        print(f"  query_relations(predicate='works_on'): {len(rels_pred)} 条")
        print("  [通过] query_relations 通过")

        print("\n[测试17] traverse:")
        sub = adapter.traverse(e1["id"], max_hops=2)
        assert "nodes" in sub and "edges" in sub, "traverse 返回格式错误"
        assert len(sub["nodes"]) >= 2, f"traverse 节点不足: {len(sub['nodes'])}"
        print(f"  traverse({e1['id']}, max_hops=2): nodes={len(sub['nodes'])}, edges={len(sub['edges'])}")
        print("  [通过] traverse 通过")

        print("\n[测试18] get_neighbors:")
        nb = adapter.get_neighbors(e1["id"])
        assert nb.get("entity") is not None, "get_neighbors 缺少 entity"
        assert len(nb.get("neighbors", [])) >= 1, "get_neighbors 邻居不足"
        print(f"  get_neighbors({e1['id']}): entity={nb['entity']['name']}, "
              f"neighbors={len(nb['neighbors'])}, relations={len(nb['relations'])}")
        print("  [通过] get_neighbors 通过")

        print("\n[测试19] query_graph:")
        qg = adapter.query_graph("Alice", limit=5)
        assert len(qg["match_entities"]) >= 1, "query_graph 匹配实体为空"
        assert len(qg["nodes"]) >= 2, "query_graph 子图节点不足"
        print(f"  query_graph('Alice'): match={len(qg['match_entities'])}, "
              f"nodes={len(qg['nodes'])}, edges={len(qg['edges'])}")
        print("  [通过] query_graph 通过")

        print("\n[测试20] diagnostics 图统计:")
        diag = adapter.diagnostics()
        assert diag["entity_count"] >= 3, f"entity_count 应为 ≥3，实际 {diag['entity_count']}"
        assert diag["relation_count"] >= 1, f"relation_count 应为 ≥1，实际 {diag['relation_count']}"
        print(f"  entity_count={diag['entity_count']}, relation_count={diag['relation_count']}")
        print("  [通过] diagnostics 图统计通过")

        # ── 最终 diagnostics ─────────────────────────────────────────
        print(f"\n  总记忆数: {adapter.diagnostics()['total_memories']}")

        adapter.disconnect()

    print("\n" + "=" * 60)
    print("  [PASS] Trinity 安全与合规升级全部通过！")
    print("  功能: audit_log | PII 检测 | GDPR 导出 | GDPR 遗忘")
    print("=" * 60)
