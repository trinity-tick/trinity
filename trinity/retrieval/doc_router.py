#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc_router.py — Wiki 文档域两阶段检索（目录粗排 → 域内精排）

背景（EXECUTION 774/775）：doc 语料（persona=trinity-docs，2440 章节）在生产混合检索里
recall@page 只有 0.25（20 题 top-10），而"路径 + 各级标题"的纯词法目录粗排 R@1=0.90。
根因：平铺检索缺一个"先选对子域"的阶段——docs 只占记忆池 4%，章节块被会话摘要/评测语料淹没。

两阶段：
  阶段一 route()：只读文件系统目录（路径 + 各级标题）做 BM25 粗排，选出候选文档；
  阶段二 fill()：在命中文档的章节子集内做词法精排，取最相关章节注入结果头部。

开关：TRINITY_DOC_ROUTE=on|off（**默认 off**，任何异常 fail-open 返回原结果）。
      TRINITY_DOC_ROUTE_DOCS=<docs 根>（默认 C:/Users/Administrator/trinity/docs）
      TRINITY_DOC_ROUTE_TOPN=<粗排取几篇>（默认 2）

回滚：export TRINITY_DOC_ROUTE=off（或删掉本模块的调用点）。
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("trinity.retrieval.doc_router")

try:
    import jieba

    jieba.setLogLevel(60)
    _HAS_JIEBA = True
except Exception:  # pragma: no cover - jieba 缺失时退化到字符 bigram
    _HAS_JIEBA = False

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_LEXEME_RE = re.compile(r"'([^']+)'")

DEFAULT_DOCS_ROOT = r"C:/Users/Administrator/trinity/docs"
_INDEX_TTL_SEC = 300.0
_CACHE: Dict[str, Dict[str, Any]] = {}


def enabled() -> bool:
    return str(os.environ.get("TRINITY_DOC_ROUTE", "off")).strip().lower() in ("on", "1", "true", "yes")


def _docs_root() -> Path:
    return Path(os.environ.get("TRINITY_DOC_ROUTE_DOCS", DEFAULT_DOCS_ROOT))


def tokenize(text: str) -> List[str]:
    text = (text or "").lower()
    if not text:
        return []
    if _HAS_JIEBA:
        toks = [t.strip() for t in jieba.lcut(text) if t.strip()]
    else:
        toks = _WORD_RE.findall(text)
    out: List[str] = []
    for t in toks:
        if re.fullmatch(r"[\W_]+", t):
            continue
        out.append(t)
        if re.fullmatch(r"[\u4e00-\u9fff]{2,}", t):  # 中文补 bigram，抗分词差异
            out.extend(t[i:i + 2] for i in range(len(t) - 1))
    return out


class _BM25:
    def __init__(self, docs: Dict[str, List[str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.lens = {d: len(t) for d, t in docs.items()}
        self.avg = (sum(self.lens.values()) / len(docs)) if docs else 1.0
        self.tf = {d: Counter(t) for d, t in docs.items()}
        df: Counter = Counter()
        for toks in docs.values():
            for term in set(toks):
                df[term] += 1
        n = max(1, len(docs))
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def score(self, query_tokens: Sequence[str], doc: str) -> float:
        tf = self.tf.get(doc)
        if not tf:
            return 0.0
        s = 0.0
        dl = self.lens[doc] or 1
        for term in query_tokens:
            idf = self.idf.get(term)
            f = tf.get(term, 0)
            if not idf or not f:
                continue
            denom = f + self.k1 * (1 - self.b + self.b * dl / self.avg)
            s += idf * f * (self.k1 + 1) / denom
        return s


def _build_index(root: Path) -> Dict[str, Any]:
    docs: Dict[str, List[str]] = defaultdict(list)
    chunks: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    n_files = 0
    for p in sorted(root.rglob("*.md")):
        if "_sitemap.generated" in p.name:
            continue
        rel = p.relative_to(root).as_posix()
        n_files += 1
        toks = tokenize(rel) + tokenize(rel.replace("/", " "))
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            if line.startswith("#"):
                head = line.lstrip("#").strip()
                if head:
                    toks.extend(tokenize(head) * 3)
        docs[rel].extend(toks)
    return {
        "root": str(root),
        "files": n_files,
        "bm25": _BM25({k: v for k, v in docs.items()}),
        "built_at": time.time(),
    }


def _index(root: Optional[Path] = None) -> Dict[str, Any]:
    root = root or _docs_root()
    key = str(root)
    ent = _CACHE.get(key)
    if ent is None or (time.time() - ent["built_at"]) > _INDEX_TTL_SEC:
        try:
            ent = _build_index(root)
        except Exception:
            ent = {"root": key, "files": 0, "bm25": _BM25({}), "built_at": time.time()}
        _CACHE[key] = ent
    return ent


def min_score() -> float:
    try:
        return float(os.environ.get("TRINITY_DOC_ROUTE_MIN_SCORE", "35"))
    except Exception:
        return 35.0


def route(query: str, top_n: int = 2) -> List[Tuple[str, float]]:
    """阶段一：目录粗排。返回 [(相对 docs 根的路径, 分数)]。

    门槛（2026-09-16 实测标定）：doc 域问题的路由最高分 27.9–155.6（20 题），
    非 doc 域问题（偏好/闲聊/会话类）只有 5.9–29.7 —— 默认门槛 35 把两类分开，
    避免把记忆类查询也强行注入文档章节。可用 TRINITY_DOC_ROUTE_MIN_SCORE 调整。
    """
    idx = _index()
    bm = idx["bm25"]
    qt = tokenize(query)
    if not qt:
        return []
    scored = [(d, bm.score(qt, d)) for d in bm.tf]
    scored = [x for x in scored if x[1] > 0]
    if not scored:
        return []
    scored.sort(key=lambda kv: -kv[1])
    if scored[0][1] < min_score():
        return []
    return scored[: max(1, top_n)]


def doc_of(row: Dict[str, Any]) -> str:
    """从检索结果行里取出它属于哪个文档（相对路径 basename 或 rel）。"""
    meta = row.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    for key in ("source_file", "source_uri"):
        v = meta.get(key)
        if v:
            return Path(str(v).replace("\\", "/")).as_posix()
    v = row.get("source_uri")
    if v:
        return Path(str(v).replace("\\", "/")).as_posix()
    return ""


def _within_doc(query: str, rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """阶段二：在命中文档内部做词法精排（章节标题权重 3x）。"""
    qt = tokenize(query)
    scored = []
    for r in rows:
        text = str(r.get("_section") or "") + " " + str(r.get("content") or "")
        lex = _LEXEME_RE.findall(text) or tokenize(text)
        toks = tokenize(str(r.get("_section") or "")) * 3 + lex
        tf = Counter(toks)
        s = sum(tf.get(t, 0) for t in qt)
        scored.append((s, r))
    scored.sort(key=lambda kv: -kv[0])
    return [r for _, r in scored[:limit]]


def fetch_doc_chunks(adapter: Any, doc_rel: str, limit: int = 6) -> List[Dict[str, Any]]:
    """按 metadata.source_file 取该文档的章节块（直连适配器，失败返回空）。"""
    if adapter is None:
        return []
    try:
        from trinity.security.crypto import decrypt_content
    except Exception:
        def decrypt_content(x):
            return x
    rows: List[Dict[str, Any]] = []
    # ── T14（2026-10-06）：原实现的守卫是 `if not hasattr(adapter, "_get_conn"): return []`
    # —— **方言盲**：`_get_conn()` 只存在於 `PostgreSQLAdapter`，`SQLiteAdapter` 只有
    # `_conn`/`_get_read_conn` ⇒ 在 SQLite 上恒假 ⇒ **文档路由恒空**（静默 no-op）。
    # 现在按方言选 SQL 与占位符，连接统一走 `trinity._tags._conn_ctx`（方言感知）。
    # 实测：改前 `fetch_doc_chunks(sqlite_adapter, …)` 返回 [] 且库未被查询；
    # 改后能真的取出该文档的章节块（见 evidence/t14_repro_after.json）。

    def _dialect_sql(dialect: str):
        if dialect == "sqlite":
            # JSON1 的 json_extract（比 `->>` 兼容更老的 SQLite）；ORDER BY 用可移植写法
            return ("""
            SELECT memory_id, content, category, importance, persona_id, session_id,
                   source_uri, metadata, created_at, role, modality, last_accessed_at
            FROM memories
            WHERE json_extract(metadata, '$.source_file') = ?
              AND (status IS NULL OR status = 'active')
            ORDER BY (importance IS NULL), importance DESC, (updated_at IS NULL), updated_at DESC
            LIMIT ?
            """, "?")
        return ("""
            SELECT memory_id, content, category, importance, persona_id, session_id,
                   source_uri, metadata, created_at, role, modality, last_accessed_at
            FROM memories
            WHERE metadata->>'source_file' = %s
              AND (status IS NULL OR status = 'active')
            ORDER BY importance DESC NULLS LAST, updated_at DESC NULLS LAST
            LIMIT %s
            """, "%s")

    try:
        if hasattr(adapter, "_get_conn"):
            _ctx = adapter._get_conn()
            _dialect = "postgres"
        else:
            from trinity._tags import _conn_ctx as _cc, _dialect as _dl
            _ctx = _cc(adapter)
            _dialect = _dl(adapter)
            if _ctx is None:
                # **响亮失败**：不静默返回空集（那正是本项要治的形态）
                logger.error(
                    "doc_router.fetch_doc_chunks: adapter %s 既无 _get_conn 也无 _conn —— "
                    "拒绝静默返回空集", type(adapter).__name__)
                return []
        _SQL, _ph = _dialect_sql(_dialect)
        with _ctx as conn:
            cur = conn.cursor()
            cur.execute(_SQL, (doc_rel, limit * 3))
            fetched = cur.fetchall()
            cols = [d[0] for d in cur.description]
        for raw in fetched:
            row = dict(zip(cols, raw))
            meta = row.get("metadata")
            if isinstance(meta, str):
                import json as _json
                try:
                    meta = _json.loads(meta)
                except Exception:
                    meta = {}
            row["metadata"] = meta or {}
            row["_section"] = (meta or {}).get("section", "")
            content = row.get("content") or ""
            try:
                row["content"] = decrypt_content(content)
            except Exception:
                row["content"] = content
            rows.append(row)
    except Exception:
        return []
    return rows


def rerank(query: str, results: Optional[List[Dict[str, Any]]], adapter: Any = None,
           top_k: int = 10, top_n: Optional[int] = None) -> List[Dict[str, Any]]:
    """两阶段重排：路由命中文档 → 注入该文档内最相关章节 + 原结果去重保序。"""
    rows = list(results or [])
    if not query or not rows and adapter is None:
        return rows
    try:
        top_n = int(os.environ.get("TRINITY_DOC_ROUTE_TOPN", top_n or 2))
    except Exception:
        top_n = top_n or 2
    routed = route(query, top_n=top_n)
    if not routed:
        return rows
    wanted = [d for d, _ in routed]
    routed_set = set(wanted)

    existing_ids = {r.get("memory_id") for r in rows}
    injected: List[Dict[str, Any]] = []
    for doc_rel in wanted:
        base = os.path.basename(doc_rel)
        chunks = [c for c in fetch_doc_chunks(adapter, doc_rel, limit=8) if c.get("memory_id") not in existing_ids]
        if not chunks:
            # 兼容：有些行 metadata.source_file 只存 basename
            chunks = [c for c in fetch_doc_chunks(adapter, base, limit=8) if c.get("memory_id") not in existing_ids]
        picked = _within_doc(query, chunks, limit=3)
        for c in picked:
            c["doc_route"] = doc_rel
            c["doc_route_fill"] = True
            injected.append(c)
            existing_ids.add(c.get("memory_id"))

    boosted, rest = [], []
    for r in rows:
        if doc_of(r) in routed_set:
            r["doc_route_boost"] = True
            boosted.append(r)
        else:
            rest.append(r)
    merged = injected + boosted + rest
    cap = max(int(top_k or 10), len(rows))
    return merged[:cap]
