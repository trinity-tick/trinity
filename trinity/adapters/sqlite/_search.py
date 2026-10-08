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

from ._base import _SQLiteMixinBase
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13, AST）
except Exception:
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(site, exc, detail=detail)
        except Exception:
            return None

logger = logging.getLogger("trinity.adapters.sqlite")


class _SearchMixin(_SQLiteMixinBase):
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
        exclude_categories: Optional[list] = None,
        top_k: int = 10,
        touch: bool = True,
        include_docs: bool = False,
        visibility_rule: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """搜索记忆。

        优先使用 FTS5 全文搜索，如果不可用则回退到 LIKE 模糊搜索。
        支持 agent_id / persona_id / session_id / app_id / category 的任意 AND 组合。

        visibility_rule（2026-08-26 Budibase 借鉴 Phase 3）：行级可见性规则
        表达式（白名单字段+参数化，防注入），解析失败时忽略该规则（不阻断检索）。

        include_docs（默认 False，2026-08-24 R6 P0-①）：doc:* 类（文档 dump/
        知识库内容）默认排除在交互记忆检索面之外——对齐 2026"记忆与知识
        分层而非混同"共识（本地实测 doc 类占 active 24%，Raft 查询 top-10
        中 70% 被文档污染）。include_docs=True 时包含（知识检索面）。

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
        # 2026-10-08（G12/t155）：**检索排除口径下推**——原先只在 PG 侧实现
        # （`adapters/_pg_search.py` 的 `category != ALL(%s)`），SQLite 侧 0 处 ⇒
        # 换后端即静默改变语义（`perception` 会重新进入检索，影响面 4,513 条）。
        # ⭐ 语义与 PG 侧**等价**（含 NULL 行为：两侧对 NULL category 都会排除该行）；
        # ⭐ 本块在 `where` 之前 ⇒ 进 WHERE ⇒ **在取 top-k（LIMIT）之前生效**。
        if exclude_categories:
            conditions.append("category NOT IN (%s)"
                              % ",".join("?" * len(exclude_categories)))
            params.extend([str(c) for c in exclude_categories])
        # 2026-08-24（R6 P0-①）：doc 类分层隔离——默认排除知识库内容
        if not include_docs:
            conditions.append("(category NOT LIKE 'doc:%' AND category NOT LIKE 'doc_%')")

        # 2026-08-26（Budibase 借鉴 Phase 3）：行级可见性规则（白名单+参数化）
        if visibility_rule:
            try:
                from trinity.security.visibility import to_sql as _vis_to_sql
                _vis_where, _vis_params = _vis_to_sql(visibility_rule)
                if _vis_where:
                    conditions.append("(" + _vis_where + ")")
                    params.extend(_vis_params)
            except Exception as _vis_exc:
                logger.debug("visibility rule ignored: %s", _vis_exc)

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
            except Exception as _e:
                # FTS5 搜索失败，回退到 LIKE
                swallow(__name__, _e)

        # 2026-09-30 修复（外部审计 P0）：FTS 命中不足时用按词 LIKE **补齐**，
        # 而不是仅在"FTS 完全为 0 条"时才整体替换。
        # 原因：写入侧把 CJK 内容经 jieba 分词后建索引，查询侧一旦分词边界不同，
        # FTS 的 `"词"*` 前缀匹配就跨不过 token 边界 ⇒ 表现为**部分漏检而非零检**
        # （实测本机库：`'向量索引'` 子串真值 10 条，FTS 只召回 1 条；
        #  `'记忆分层'` 真值 1 条，FTS 召回 0 条）。旧逻辑只在 FTS 恰好为 0 时
        # 才回退，这种"漏了一部分"的情况永远不会被补上。
        if len(results) < top_k:
            seen_ids = {r["memory_id"] for r in results}
            try:
                extra = self._search_like(query, params, where, top_k)
            except Exception as _e:  # noqa: BLE001 — 补齐失败不得破坏已有 FTS 结果
                swallow(__name__, _e)
                extra = []
            for _row in extra:
                if _row["memory_id"] in seen_ids:
                    continue
                seen_ids.add(_row["memory_id"])
                results.append(_row)
                if len(results) >= top_k:
                    break

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

        # 2026-10-08（G9R-8/t122）：**并列分确定性次级键**。原因（实测，见
        # G9R-8-NEWROW-TOPK-VISIBILITY.md）：下面的 `norm_score` 是 min-max 归一化，
        # 同分/近似同分语料下会把**大批行压成同一个分数**（临时库实测 200 条命中
        # `distinct score = 1`）⇒ 只按 `score` 排序时，"谁活过 LIMIT 截断"**是未定义的**
        # ⇒ 同一查询两次可能给出**不同结果集**，一切评测/回归基线随之悬空。
        # 次级键选 `m.memory_id`（**与时间无关**）：不引入"新优先"偏好（那是另一案），
        # 也不依赖 rowid 的物理分配顺序 ⇒ 结果对同一库同一查询**逐位可复现**。
        sql = f"""
            SELECT m.memory_id, m.content, m.persona_id, m.session_id, m.role,
                   m.importance, m.tags, m.category, m.modality, m.created_at,
                   m.source_uri, m.memory_layer, m.access_count, m.last_accessed_at,
                   m.metadata, fts.rank as score
            FROM memories m
            INNER JOIN (
                SELECT rowid, rank
                FROM memories_fts
                WHERE memories_fts MATCH ?
            ) fts ON m.rowid = fts.rowid
            WHERE {where}
            ORDER BY score, m.memory_id
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
            content = self._decrypt_text_resilient(
                row["content"], memory_id=row["memory_id"],
                tokenized=(row["tokenized_content"]
                           if "tokenized_content" in row.keys() else None),
                where="search_memories/fts")
            _md_raw = row["metadata"] if "metadata" in row.keys() else None
            _md = {}
            if isinstance(_md_raw, str):
                try:
                    _md = json.loads(_md_raw or "{}")
                except Exception:
                    _md = {}
            elif isinstance(_md_raw, dict):
                _md = _md_raw
            # 2026-09-02（Fable 对照审计 P0-②）：检索输出带 provenance_role
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
                "source_uri": row["source_uri"] if "source_uri" in row.keys() else None,
                "memory_layer": row["memory_layer"] if "memory_layer" in row.keys() else None,
                "access_count": row["access_count"] if "access_count" in row.keys() else 0,
                "last_accessed_at": row["last_accessed_at"] if "last_accessed_at" in row.keys() else None,
                "metadata": _md,
                "provenance_role": _md.get("provenance_role"),
                "score": round(norm_score, 4),
            })
        # 2026-09-02（Fable 对照审计 P2-⑥）：读侧 untrusted 标注
        from trinity.security.readside import annotate_readside
        for _d in results:
            annotate_readside(_d)

        return results
    def _search_like(
        self, query: str, params: List[Any], where: str, top_k: int
    ) -> List[Dict[str, Any]]:
        """回退到 LIKE 模糊搜索（2026-08-15 v2：线程本地只读连接）。

        2026-09-30 修复（外部审计 P0 —— 中文查询确定性 0 命中）：
        旧实现用**整条查询串**做子串匹配（``like_term = f"%{query}%"``）。
        因此 "向量索引 持久化" 会去匹配**含空格的完整字面串** "向量索引 持久化"，
        而任何文档都不含该串 ⇒ 多词查询在 FTS 失配之后**连回退也必然 0 条**。
        实测：``'向量索引'`` 的 `LIKE '%向量索引%'` 真值 10 条，"记忆分层" 1 条，
        线上整句查询却返回 0 条。

        现改为**按词匹配**，并且匹配**正确的列**。这里有两个各自独立的缺陷，
        只修一个都不够（本机生产库实测）：

        缺陷 1 —— 匹配整串：``like_term = f"%{query}%"`` 让 "向量索引 持久化"
        去匹配**含空格的完整字面串**，任何文档都不含该串 ⇒ 必然 0 条。

        缺陷 2 —— 匹配了密文列：``content LIKE ?`` 在**开启存储加密**的库上等于
        永远不匹配（本机 ``content`` 111,484 行中 106,864 行是 ``enc:v1:`` 密文，
        active 行 24,862 中 22,571 是密文）。明文只存在于 ``tokenized_content``
        这一 jieba 分词影子列里 —— 而它**词元之间插了空格**，"向量索引" 被写成
        ``向量 索引``，因此 ``tokenized_content LIKE '%向量索引%'`` 同样是 0 条
        （实测 0 条；把空格去掉后 45 条）。所以必须对**去掉空格的**影子列做匹配。

        最终匹配式（每词）：content（明文行） ∪ REPLACE(tokenized_content,' ','')
        （密文行） ∪ tags；按命中词数排序，词数上限 8 以免退化为全表多条件扫描。
        """
        conn = self._get_read_conn()
        if not conn:
            return []

        terms = [t for t in self._tokenize_fts_query(query)[:8] if t.strip()]
        if not terms:
            return []

        # SQLite 的 `?` 按**在 SQL 文本中出现的先后**绑定，因此参数顺序必须是：
        # SELECT 里的打分占位符 → WHERE 的 params → MATCH 占位符 → LIMIT。
        # 打分每词 2 个占位符（content / 去空格影子列）；匹配每词 4 个
        # （content / tokenized_content / 去空格影子列 / tags）。
        score_sql = " + ".join(
            "(CASE WHEN content LIKE ? THEN 2 ELSE 0 END)"
            " + (CASE WHEN REPLACE(tokenized_content,' ','') LIKE ? THEN 1 ELSE 0 END)"
            for _ in terms
        )
        match_sql = " OR ".join(
            "(content LIKE ? OR tokenized_content LIKE ?"
            " OR REPLACE(tokenized_content,' ','') LIKE ? OR tags LIKE ?)"
            for _ in terms
        )
        score_params = [p for t in terms for p in (f"%{t}%", f"%{t}%")]
        match_params = [p for t in terms for p in (f"%{t}%",) * 4]

        cursor = conn.execute(f"""
            SELECT memory_id, content, persona_id, session_id, role,
                   importance, tags, category, modality, created_at,
                   source_uri, memory_layer, access_count, last_accessed_at,
                   metadata,
                   ({score_sql}) AS matched
            FROM memories
            WHERE {where}
              AND ({match_sql})
            ORDER BY matched DESC, importance DESC, created_at DESC
            LIMIT ?
        """, score_params + params + match_params + [top_k])

        results = []
        for row in cursor.fetchall():
            content = self._decrypt_text_resilient(
                row["content"], memory_id=row["memory_id"],
                tokenized=(row["tokenized_content"]
                           if "tokenized_content" in row.keys() else None),
                where="search_memories/like")
            _md_raw = row["metadata"] if "metadata" in row.keys() else None
            _md = {}
            if isinstance(_md_raw, str):
                try:
                    _md = json.loads(_md_raw or "{}")
                except Exception:
                    _md = {}
            elif isinstance(_md_raw, dict):
                _md = _md_raw
            # 打分：命中词数占比 × 0.5 ⇒ 取值 (0, 0.5]。每词理论上限 3
            # （content 2 + 去空格影子列 1）。**刻意低于** FTS 归一化分数的常见
            # 区间，使回退结果只做"补齐召回"，不会抢占 FTS 的精确排序。
            _matched = int(row["matched"] or 0)
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
                "memory_layer": row["memory_layer"] if "memory_layer" in row.keys() else None,
                "metadata": _md,
                "provenance_role": _md.get("provenance_role"),
                "score": round(0.5 * _matched / (3 * len(terms)), 4),
            })
        # 2026-09-02（Fable 对照审计 P2-⑥）：读侧 untrusted 标注
        from trinity.security.readside import annotate_readside
        for _d in results:
            annotate_readside(_d)

        return results
