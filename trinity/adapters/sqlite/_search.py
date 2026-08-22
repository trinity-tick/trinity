"""SQLite adapter - search & FTS mixin (split from sqlite.py, 2026-08-17).

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


class _SearchMixin:
    _CJK_PATTERN = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\uff00-\uffef]')

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
        touch: bool = True,
    ) -> List[Dict[str, Any]]:
        """搜索记忆。

        优先使用 FTS5 全文搜索，如果不可用则回退到 LIKE 模糊搜索。
        支持 agent_id / persona_id / session_id / app_id / category 的任意 AND 组合。

        touch（默认 True）：命中记忆异步入队 touch（access_count+1）。
        内部维护操作（如写路径的冲突检测检索）应传 touch=False，避免把
        "写入时自碰"误记为真实访问（污染 access_count 语义）。

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
        # touch=False：内部维护检索（如写路径冲突检测）不把命中记作访问，
        # 避免刚写入的记忆被自身冲突检索 touch 成 access_count=1。
        if touch and results:
            memory_ids = [r["memory_id"] for r in results]
            self._touch_batch(memory_ids)

        return results
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
        if not _SearchMixin._CJK_PATTERN.search(query):
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
        if not _SearchMixin._CJK_PATTERN.search(content):
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
        # 2026-08-21（性能防御）：OR 词条上限——超长查询（如写路径冲突检测的
        # 全文召回）会切出数千词条导致 FTS5 MATCH 分钟级。截断到前 64 词，
        # 召回语义不变（FTS 本就是近似召回；正常用户短查询不受影响）。
        terms = terms[:64]
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
