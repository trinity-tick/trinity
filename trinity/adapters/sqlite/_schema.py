"""SQLite adapter - table & FTS5 schema mixin (split from sqlite.py, 2026-08-17).

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


class _SchemaMixin:
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
