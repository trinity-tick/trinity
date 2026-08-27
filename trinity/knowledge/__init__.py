# -*- coding: utf-8 -*-
"""Trinity Knowledge Layer — 借鉴 Context7 的知识源治理（Phase 1-3）。

Context7 借鉴（2026-08-26）：知识源注册表 + 健康度（freshness/coverage/usage/health）
+ 独立知识检索工具 + 查询别名展开。

机制：
  - build_sources()   从 doc:*/kb_harvested/web/video/knowledge 等类目聚合知识源
                      （source_id = source_uri 或 category；freshness=最近同步、
                       coverage=条目数、usage=access_count 汇总、health=综合评分）
  - knowledge_search() 独立知识检索（doc 层，源过滤 + 健康度元数据）
  - expand_query()     查询别名展开（~/.trinity/aliases.yaml）
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trinity.knowledge")

_HOME = os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity"))
_SOURCES_FILE = os.path.join(_HOME, "knowledge_sources.json")
_ALIASES_FILE = os.path.join(_HOME, "aliases.yaml")

# 知识类目（doc 层 + 采集/领域知识）
KNOWLEDGE_CATEGORIES = (
    "doc:general", "doc:summary", "doc:plan", "doc:ops", "doc:benchmark",
    "doc:protocol", "doc:general", "doc_summary",
    "kb_harvested", "web_harvested", "video_harvested",
    "knowledge", "wms_knowledge", "ai_knowledge", "insight",
)
# 过时阈值（天）
STALE_DAYS = 30.0
# 2026-08-27（价值驱动动态阈值）：TRINITY_STALE_DAYS 覆盖；
# 高价值源建议放宽（memory_value 0.7+ 可配 45-60）
_STALE_DAYS_EFFECTIVE = float(os.environ.get("TRINITY_STALE_DAYS", "30"))

_LOCK = threading.RLock()
_SOURCES_CACHE: Optional[Dict[str, Any]] = None
_ALIASES_CACHE: Optional[Dict[str, List[str]]] = None
_ALIASES_MTIME = 0.0


def _sources_file() -> str:
    return os.path.join(os.environ.get("TRINITY_HOME", _HOME), "knowledge_sources.json")


# ── Phase 1: 源注册表 + 健康度 ──────────────────────────────────────

def _source_id(rec: Dict[str, Any]) -> str:
    uri = (rec.get("source_uri") or "").strip()
    if uri:
        return uri.replace("\\", "/")
    return "cat:" + (rec.get("category") or "unknown")


def _health(freshness_days: float, usage: int, coverage: int) -> float:
    """健康度 0-1：freshness 分（30 天内线性衰减）+ usage 分（log 归一化）。"""
    fresh = max(0.0, 1.0 - freshness_days / _STALE_DAYS_EFFECTIVE)
    use = min(1.0, math.log10(usage + 1) / 2.0)
    cov = min(1.0, coverage / 20.0)
    return round(0.5 * fresh + 0.3 * use + 0.2 * cov, 3)


def _parse_ts(ts: Any) -> float:
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            from datetime import datetime, timezone
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            continue
    return 0.0


def _rows_from_db() -> List[Dict[str, Any]]:
    """直连生产库只读取知识类目元数据（category/source_uri/created_at/access_count——
    均明文列，无需解密 content）；不依赖全局 _TRINITY_STORE（eval 等环境无干扰）。"""
    import sqlite3
    store_dir = os.environ.get("TRINITY_STORE") or str(Path.home() / ".trinity" / "store")
    db = os.path.join(store_dir, "trinity_store.db")
    if not os.path.exists(db):
        return []
    rows = []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
        try:
            ph = ",".join("?" for _ in KNOWLEDGE_CATEGORIES)
            sql = ("SELECT memory_id, category, source_uri, created_at, access_count "
                   "FROM memories WHERE status = 'active' AND category IN (" + ph + ")")
            cur = conn.execute(sql, list(KNOWLEDGE_CATEGORIES))
            rows = [dict(zip(["memory_id", "category", "source_uri", "created_at", "access_count"], r))
                     for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("knowledge db read failed: %s", exc)
    return rows

def build_sources(client: Any = None, emit_stale: bool = False) -> Dict[str, Any]:
    """聚合知识源注册表（freshness/coverage/usage/health）。

    client: 可传 Trinity()；缺省懒建（TRINITY_MEMORY_ENABLED=0）。
    emit_stale=True：对过时源 emit("knowledge.stale", ...)（automation 规则可响应）。
    """
    global _SOURCES_CACHE
    # 2026-08-26：缺省直连生产库只读（不懒建 Trinity——避免全局 _TRINITY_STORE
    # 被其他进程/测试污染时连到空库；知识源聚合只需明文元数据列）。
    if client is not None and getattr(client, "_adapter", None) is not None:
        rows: List[Dict[str, Any]] = []
        offset = 0
        while True:
            batch = client._adapter.get_all_memories(limit=1000, offset=offset)
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000
    else:
        rows = _rows_from_db()
    now = time.time()
    agg: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        cat = (r.get("category") or "")
        if cat not in KNOWLEDGE_CATEGORIES:
            continue
        sid = _source_id(r)
        a = agg.setdefault(sid, {
            "source_id": sid,
            "category": cat,
            "count": 0,
            "access_sum": 0,
            "newest_ts": 0.0,
        })
        a["count"] += 1
        a["access_sum"] += int(r.get("access_count") or 0)
        ts = _parse_ts(r.get("created_at"))
        if ts > a["newest_ts"]:
            a["newest_ts"] = ts
    sources = []
    for sid, a in sorted(agg.items(), key=lambda kv: -kv[1]["count"]):
        freshness_days = max(0.0, (now - a["newest_ts"]) / 86400.0) if a["newest_ts"] else STALE_DAYS
        sources.append({
            "source_id": sid,
            "category": a["category"],
            "count": a["count"],
            "access_sum": a["access_sum"],
            "newest_ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(a["newest_ts"])) if a["newest_ts"] else "",
            "freshness_days": round(freshness_days, 1),
            "health": _health(freshness_days, a["access_sum"], a["count"]),
            "stale": freshness_days > _STALE_DAYS_EFFECTIVE,
        })
    out = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "total": len(sources),
        "stale_count": sum(1 for s in sources if s["stale"]),
        "sources": sources,
    }
    try:
        with open(_sources_file(), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    except Exception as exc:
        logger.warning("knowledge sources save failed: %s", exc)
    _SOURCES_CACHE = out
    if emit_stale:
        for s in sources:
            if s["stale"]:
                try:
                    from trinity.automation import emit as _emit
                    _emit("knowledge.stale", {
                        "source_id": s["source_id"],
                        "category": s["category"],
                        "freshness_days": s["freshness_days"],
                        "count": s["count"],
                    })
                except Exception:
                    pass
    return out


def sources() -> Dict[str, Any]:
    """读源注册表（缓存；无缓存时构建）。"""
    global _SOURCES_CACHE
    if _SOURCES_CACHE is not None:
        return _SOURCES_CACHE
    if os.path.exists(_sources_file()):
        try:
            with open(_sources_file(), "r", encoding="utf-8") as f:
                _SOURCES_CACHE = json.load(f)
                return _SOURCES_CACHE
        except Exception:
            pass
    return build_sources()


# ── Phase 2: 独立知识检索 ───────────────────────────────────────────

def knowledge_search(
    client: Any = None,
    query: str = "",
    source: Optional[str] = None,
    top_k: int = 10,
) -> Dict[str, Any]:
    """独立知识检索（doc 层）：源过滤 + 健康度元数据。

    source: 源过滤（source_id 子串匹配，如文件名/类目）。
    """
    if client is None:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
        from trinity import Trinity
        client = Trinity(adapter="sqlite")
    _q = expand_query(query) if query else query
    result = client.search(query=_q, mode="keyword", top_k=top_k,
                           include_docs=True)
    results = result.get("results", [])
    if source:
        wanted = source.lower()
        results = [r for r in results
                   if wanted in str(r.get("source_uri") or r.get("category") or "").lower()]
    # 附健康度元数据（source_uri 反斜杠 → 正斜杠归一，与注册表一致）
    src_map = {s["source_id"]: s for s in sources().get("sources", [])}
    for r in results:
        _uri = (r.get("source_uri") or "").replace("\\", "/")
        sid = _uri or ("cat:" + str(r.get("category") or ""))
        meta = src_map.get(sid)
        if meta:
            r["source_health"] = {
                "freshness_days": meta.get("freshness_days"),
                "health": meta.get("health"),
                "stale": meta.get("stale"),
                "count": meta.get("count"),
            }
    return {"results": results, "query": _q, "expanded": _q != query,
            "source_filter": source or ""}


# ── Phase 3: 查询别名展开 ───────────────────────────────────────────

def _aliases_file() -> str:
    return os.path.join(os.environ.get("TRINITY_HOME", _HOME), "aliases.yaml")


def load_aliases(force: bool = False) -> Dict[str, List[str]]:
    """加载 ~/.trinity/aliases.yaml（mtime 缓存）：aliases: {词: [展开词...]}。"""
    global _ALIASES_CACHE, _ALIASES_MTIME
    path = _aliases_file()
    if not os.path.exists(path):
        return {}
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = 0.0
    if not force and _ALIASES_CACHE is not None and abs(mtime - _ALIASES_MTIME) < 1e-6:
        return _ALIASES_CACHE
    with _LOCK:
        try:
            import yaml
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            aliases = {}
            for k, v in (data.get("aliases") or {}).items():
                if isinstance(v, list):
                    aliases[str(k).lower()] = [str(x) for x in v]
                elif isinstance(v, str):
                    aliases[str(k).lower()] = [v]
            _ALIASES_CACHE = aliases
            _ALIASES_MTIME = mtime
            return aliases
        except Exception as exc:
            logger.warning("aliases load failed: %s", exc)
            return {}


def expand_query(query: str) -> str:
    """查询别名展开：query 中命中别名的词，追加其展开词（去重）。"""
    if not query:
        return query
    aliases = load_aliases()
    if not aliases:
        return query
    low = query.lower()
    additions = []
    for alias, targets in aliases.items():
        if alias and alias in low:
            additions.extend(t for t in targets if t.lower() not in low)
    if not additions:
        return query
    return query + " " + " ".join(additions)
