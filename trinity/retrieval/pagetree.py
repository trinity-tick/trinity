# -*- coding: utf-8 -*-
"""MemoryPageTree — PageIndex 启发的记忆空间"主题页树"检索（Phase 1：纯元数据，零 LLM）。

借鉴 VectifyAI/PageIndex（Vectorless, Reasoning-based RAG）的三个机制，适配 Trinity 的记忆形态：

  1. 物化层级索引: 记忆空间 → 主题页树（category → 簇(cluster) → 记忆），
     簇轴 = persona（非 default 时）否则主标签（首个非噪音 tag）否则 untagged。
     节点带词表（jieba 高频词）与样例——"为 LLM 和 agent 优化的目录"。
  2. 先定位页、再读页内: 检索先按查询词对"页"打分（词重叠 + 基础召回命中率），
     选中 top page_k 页后再在页内排序取记忆——页内天然低噪音（呼应 GEN-3 剪枝实验）。
  3. 页路径可溯源: 每条结果附带 page_path / page_title，对齐 PageIndex 的显式引用。

Phase 2（维护链）将用 LLM 补节点摘要（summary 字段已预留）；Phase 3（mode=reason）
将把活跃 goal / 会话上下文接入检索决策。本模块默认关闭，由调用方显式启用。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import jieba
    _JIEBA = True
except Exception:  # pragma: no cover
    _JIEBA = False

# ── 默认噪音标签：不参与簇轴选择（基准语料/内部标记）───────────────
DEFAULT_EXCLUDE_TAGS = {"lme", "stress", "stress-test", "locktest", "test", "sync"}
# ── 默认排除类目（生产建树时由脚本传入，库内默认不排除）────────────
DEFAULT_EXCLUDE_CATEGORIES: set = set()

_STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "or", "in", "on", "at", "to", "is",
    "are", "was", "were", "what", "how", "did", "do", "does", "with", "from",
    "this", "that", "their", "his", "her", "its", "about", "over", "since",
    "before", "after", "year", "first", "main", "focus", "work", "person",
    "have", "has", "had", "been", "being", "who", "whom", "which", "where",
    "when", "why", "three", "most", "significant", "changes", "half",
    "也", "了", "的", "是", "在", "与", "和", "及", "对", "就", "把", "被",
    "一个", "我们", "你们", "他们", "这个", "那个", "什么", "如何", "怎么",
}


def _tokenize(text: str) -> List[str]:
    """切词：jieba（中文）+ 拉丁词元，去停用词、去单字/单字母。"""
    if not text:
        return []
    text = str(text).lower()
    terms: List[str] = []
    if _JIEBA:
        try:
            terms += [t.strip() for t in jieba.cut(text) if t.strip()]
        except Exception:
            pass
    terms += re.findall(r"[a-z0-9]{2,}", text)
    out = []
    seen = set()
    for t in terms:
        if len(t) < 2:
            continue
        if t in _STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _term_freqs(texts: List[str], top_n: int = 40) -> List[str]:
    """对文本集合做词频统计，返回高频词列表（簇词表）。

    2026-08-26（五轮归因实证）：≥2 词频阈值会掏空小簇（2-3 条记忆）
    的词表（只剩人名/标签）→ 页打分退化。按样本数自适应：
    样本 >= 6 才用 ≥2 过滤（大簇降噪），小簇保留单现词。
    """
    cnt: Counter = Counter()
    for t in texts:
        for w in _tokenize(t):
            cnt[w] += 1
    min_df = 2 if len(texts) >= 6 else 1
    return [w for w, _ in cnt.most_common(top_n * 2) if cnt[w] >= min_df][:top_n]


def _overlap_ratio(query_terms: List[str], node_terms: List[str]) -> float:
    """查询词与节点词表的重叠率（sqrt 归一，防长节点词表稀释）。"""
    if not query_terms:
        return 0.0
    qset = set(query_terms)
    nset = set(node_terms or [])
    if not nset:
        return 0.0
    hit = sum(1 for t in qset if t in nset)
    return hit / math.sqrt(len(qset))


def _parse_ts(ts: Any) -> Optional[float]:
    """时间戳 → unix 秒（容忍多种格式）。"""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s[:26], fmt[:26] if len(s) >= 26 else fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            continue
    return None


def _cluster_key(rec: Dict[str, Any], exclude_tags: set) -> str:
    """簇轴：persona（非 default）→ 主标签（非噪音）→ untagged。"""
    persona = (rec.get("persona_id") or "").strip()
    if persona and persona != "default":
        return "persona:" + persona
    tags = rec.get("tags") or []
    for t in tags:
        ts = str(t).strip()
        if ts and ts.lower() not in exclude_tags:
            return "tag:" + ts
    return "untagged"


class MemoryPageTree:
    """记忆空间主题页树。

    用法::

        tree = MemoryPageTree()
        tree.build(records)
        tree.save(path)
        tree2 = MemoryPageTree.load(path)
        out = tree2.search(query, top_k=10, page_k=2, base_fn=adapter_search)
    """

    VERSION = 1

    def __init__(self) -> None:
        self.records: Dict[str, Dict[str, Any]] = {}      # memory_id → record
        self.categories: Dict[str, Dict[str, Any]] = {}   # category → node
        self.clusters: Dict[str, Dict[str, Any]] = {}     # cluster_node_id → node
        self.memory_index: Dict[str, str] = {}            # memory_id → cluster_node_id
        self.stats: Dict[str, Any] = {}
        self.built_at: Optional[str] = None
        self._term_df: Dict[str, int] = {}                # term → 含该词的簇数（IDF）
        self._node_vectors: Dict[str, List[float]] = {}   # 节点摘要向量（2026-08-26 遗留处理）

    # ── build ───────────────────────────────────────────────────────

    def build(
        self,
        records: List[Dict[str, Any]],
        exclude_categories: Optional[set] = None,
        exclude_tags: Optional[set] = None,
        top_terms: int = 40,
        sample_mems: int = 3,
        sample_chars: int = 240,
        with_vectors: bool = False,
    ) -> "MemoryPageTree":
        """建树。

        with_vectors（2026-08-26 遗留处理）：为簇节点生成摘要向量（本地
        embedding 引擎），页检索可用语义相似度定位（近义改写查询收益）。
        """
        exclude_categories = set(exclude_categories or DEFAULT_EXCLUDE_CATEGORIES)
        exclude_tags = set(exclude_tags or DEFAULT_EXCLUDE_TAGS)
        self.records = {}
        self.categories = {}
        self.clusters = {}
        self.memory_index = {}
        self._node_vectors = {}

        # 1) 规范化记录 + 按 category 分组
        by_cat: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for rec in records or []:
            mid = str(rec.get("memory_id") or rec.get("id") or "").strip()
            content = (rec.get("content") or "").strip()
            if not mid or not content:
                continue
            cat = (rec.get("category") or "general").strip() or "general"
            if cat in exclude_categories:
                continue
            tags = rec.get("tags") or []
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            if not isinstance(tags, list):
                tags = []
            norm = {
                "memory_id": mid,
                "content": content,
                "category": cat,
                "tags": [str(t) for t in tags if str(t).strip()],
                "persona_id": (rec.get("persona_id") or "").strip(),
                "agent_id": (rec.get("agent_id") or "").strip() or "default",
                "session_id": str(rec.get("session_id") or ""),
                "importance": float(rec.get("importance") or rec.get("importance_score") or 0.5),
                "created_at": rec.get("created_at") or "",
                "_ts": _parse_ts(rec.get("created_at")),
            }
            self.records[mid] = norm
            by_cat[cat].append(norm)

        # 2) category → cluster 节点
        for cat, mems in sorted(by_cat.items()):
            cat_node_id = "cat:" + cat
            clusters: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for m in mems:
                clusters[_cluster_key(m, exclude_tags)].append(m)

            cat_node = {
                "node_id": cat_node_id,
                "kind": "category",
                "title": cat,
                "parent": None,
                "cluster_ids": [],
                "memory_count": len(mems),
                "stats": {"count": len(mems)},
            }
            for ckey, cmems in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
                cid = "clu:%s/%s" % (cat, ckey)
                cluster_node = self._make_cluster(cid, cat, ckey, cmems, top_terms, sample_mems, sample_chars)
                self.clusters[cid] = cluster_node
                cat_node["cluster_ids"].append(cid)
                for m in cmems:
                    self.memory_index[m["memory_id"]] = cid
            cat_node["stats"]["clusters"] = len(cat_node["cluster_ids"])
            self.categories[cat] = cat_node

        # 词项跨簇文档频率（IDF 页打分用；人名/通用词 df 高 → 权重低）
        self._term_df = {}
        for node in self.clusters.values():
            for t in set(node.get("terms") or []):
                self._term_df[t] = self._term_df.get(t, 0) + 1

        self.built_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        self.stats = {
            "records": len(self.records),
            "categories": len(self.categories),
            "clusters": len(self.clusters),
            "excluded_categories": sorted(exclude_categories),
        }
        if with_vectors:
            self._embed_node_vectors()
        logger.info("MemoryPageTree built: %d records → %d categories / %d clusters (vectors=%d)",
                    len(self.records), len(self.categories), len(self.clusters),
                    len(self._node_vectors))
        return self

    def restore_summaries(self, old_tree: Optional["MemoryPageTree"]) -> None:
        """从旧树恢复节点摘要（重建不丢 LLM 摘要，2026-08-26 遗留处理）。"""
        if old_tree is None:
            return
        for cid, node in self.clusters.items():
            old = old_tree.clusters.get(cid)
            if old and (old.get("summary") or "").strip():
                node["summary"] = old.get("summary", "")
                node["summary_ts"] = old.get("summary_ts", "")
                node["summary_model"] = old.get("summary_model", "")

    def embed_node_vectors(self) -> None:
        """公开：为簇节点生成摘要向量（摘要优先，缺摘要用样例文本）。失败静默降级。"""
        self._embed_node_vectors()

    def _embed_node_vectors(self) -> None:
        """为簇节点生成摘要向量（摘要优先，缺摘要用样例文本）。失败静默降级。"""
        try:
            from trinity.embeddings.engine import create_engine
            engine = create_engine(backend="auto", use_cache=True)
            texts = []
            nodes = list(self.clusters.values())
            for node in nodes:
                summary = (node.get("summary") or "").strip()
                if summary:
                    texts.append("[" + str(node.get("category", "")) + "] " + summary)
                else:
                    sample = " ".join(node.get("sample", []))[:300]
                    texts.append("[" + str(node.get("category", "")) + "] " + (sample or node.get("title", "")))
            vectors = engine.embed_batch(texts) if texts else []
            for node, vec in zip(nodes, vectors):
                # numpy.float32 不能 JSON 序列化 → 显式转 Python float
                self._node_vectors[node["node_id"]] = [float(x) for x in vec]
        except Exception as exc:
            logger.warning("pagetree node vector embedding skipped: %s", exc)
            self._node_vectors = {}

    def _make_cluster(self, cid: str, cat: str, ckey: str, mems: List[Dict[str, Any]],
                      top_terms: int, sample_mems: int, sample_chars: int) -> Dict[str, Any]:
        """构造簇节点：词表（标题+tags+样例词频）+ 样例内容 + 统计。"""
        mems_sorted = sorted(
            mems,
            key=lambda m: (float(m.get("importance") or 0.0), m.get("_ts") or 0.0),
            reverse=True,
        )
        contents = [m["content"][:sample_chars] for m in mems_sorted[:sample_mems]]
        all_tags: List[str] = []
        for m in mems:
            all_tags.extend(m.get("tags") or [])
        tag_counter = Counter(all_tags)
        title = ckey.split(":", 1)[-1] if ":" in ckey else ckey
        terms = _term_freqs(contents, top_n=top_terms)
        # 簇词表 = 高频词 + 标签（标签保证主题词直配）
        vocab = terms
        for t, _ in tag_counter.most_common(10):
            if t.lower() not in DEFAULT_EXCLUDE_TAGS and t not in vocab:
                vocab.append(t)
        return {
            "node_id": cid,
            "kind": "cluster",
            "title": title,
            "parent": "cat:" + cat,
            "category": cat,
            "memory_ids": [m["memory_id"] for m in mems],
            "tags": tag_counter.most_common(20),
            "terms": vocab,
            "sample": contents,
            "summary": "",  # Phase 2: 维护链 LLM 摘要
            "stats": {
                "count": len(mems),
                "avg_importance": round(sum(float(m.get("importance") or 0) for m in mems) / max(1, len(mems)), 3),
                "newest_ts": max((m.get("_ts") or 0) for m in mems) or None,
            },
        }

    # ── persistence ─────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.VERSION,
            "built_at": self.built_at,
            "stats": self.stats,
            "records": self.records,
            "categories": self.categories,
            "clusters": self.clusters,
            "memory_index": self.memory_index,
            "term_df": self._term_df,
            "node_vectors": self._node_vectors,
        }

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False)
        logger.info("MemoryPageTree saved -> %s (%.1f KB)", path, os.path.getsize(path) / 1024)

    @classmethod
    def load(cls, path: str) -> Optional["MemoryPageTree"]:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tree = cls()
            tree.records = data.get("records", {})
            tree.categories = data.get("categories", {})
            tree.clusters = data.get("clusters", {})
            tree.memory_index = data.get("memory_index", {})
            tree.stats = data.get("stats", {})
            tree.built_at = data.get("built_at")
            tree._term_df = data.get("term_df", {}) or {}
            tree._node_vectors = data.get("node_vectors", {}) or {}
            return tree
        except Exception as exc:
            logger.warning("MemoryPageTree load failed (%s): %s", path, exc)
            return None

    # ── search ──────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 10,
        page_k: int = 3,
        base_fn: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None,
        page_mem_cap: int = 300,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """页优先检索：定位页 → 页内排序 → 基础召回兜底填充。

        filters（2026-08-26 隔离修复）：候选记忆按 persona_id / agent_id /
        session_id / category 过滤（与 search() 的过滤契约一致，多租户隔离）。

        返回::

            {
              "results": [ {memory_id, content, score, page_path, page_title, ...} ],
              "pages_used": [cluster_node_id, ...],
              "page_hits": {cluster_node_id: base_hit_count},
              "filled_by_base": count,
            }
        """
        q_terms = _tokenize(query)
        base_hits: Dict[str, int] = defaultdict(int)
        base_by_id: Dict[str, Dict[str, Any]] = {}
        if base_fn is not None:
            try:
                for r in (base_fn(query, max(top_k * 2, 20)) or []):
                    mid = str(r.get("memory_id") or r.get("id") or "")
                    if mid:
                        base_by_id[mid] = r
                        cid = self.memory_index.get(mid)
                        if cid:
                            base_hits[cid] += 1
            except Exception as exc:
                logger.warning("pagetree base_fn failed: %s", exc)

        # ── 退化短查询守卫（2026-08-26 四轮归因实证）────────────
        #   ≤2 个内容词的查询（如 "What does Henry have?"）在页定位上
        #   无区分度（所有相关页同分），页排序反而挤掉基础召回——
        #   直接返回基础召回（对齐自适应路由：短查询走轻通道）。
        if len(q_terms) <= 2:
            results = []
            for mid, r in base_by_id.items():
                rec = dict(r)
                rec.setdefault("source_channel", "base")
                results.append(rec)
                if len(results) >= top_k:
                    break
            return {
                "results": results,
                "pages_used": [],
                "page_hits": {},
                "filled_by_base": 0,
                "guard": "short_query",
            }

        # ── 页打分：IDF 加权词重叠 0.75 + 基础召回命中率 0.25 ──
        # 词重叠按查询词 IDF 加权（跨簇文档频率低 = 区分力强）：
        #   人名/高频词（df 高）权重低，主题词（df 低）权重高——
        # 防止"样例恰好含查询词"的小页挤掉真正装着答案的大页。
        # 2026-08-26 三轮归因实证：sqrt 归一 + 密度因子均不如 IDF 加权。
        n_clu = max(1, len(self.clusters))
        q_idf = {
            t: math.log(1.0 + n_clu / max(1, self._term_df.get(t, 0)))
            for t in q_terms
        }
        idf_sum = sum(q_idf.values()) or 1.0
        scored: List[tuple] = []
        # 2026-08-26（二轮优化，holdout 实证）：节点摘要参与页打分——
        # 近义改写查询与原文词重叠≈0，但 LLM 摘要用词不同（主题性描述），
        # 摘要词表能接住改写查询（生产 holdout 页树 R@10 0.137 → 待测）。
        _summary_terms: Dict[str, set] = {}
        # 2026-08-26（遗留处理）：摘要向量化页定位——query 语义 vs 节点向量
        # 余弦相似度（本地 embedding 引擎），近义改写查询不再依赖表层词。
        _q_vec: Optional[Any] = None
        _vec_sims: Optional[Dict[str, float]] = None
        if self._node_vectors:
            try:
                from trinity.embeddings.engine import create_engine
                _eng = create_engine(backend="auto", use_cache=True)
                _q_vec = _eng.embed(query)
                _vec_sims = {}
                for cid, vec in self._node_vectors.items():
                    import numpy as _np
                    _vec_sims[cid] = float(_np.dot(_q_vec, _np.asarray(vec, dtype=_np.float32)))
            except Exception:
                _vec_sims = None
        _vec_min = min(_vec_sims.values()) if _vec_sims else 0.0
        _vec_max = max(_vec_sims.values()) if _vec_sims else 1.0
        _vec_span = (_vec_max - _vec_min) or 1.0
        for cid, node in self.clusters.items():
            node_set = set(node.get("terms") or [])
            _summ = node.get("summary") or ""
            if _summ:
                _st = _summary_terms.get(cid)
                if _st is None:
                    _st = set(_tokenize(_summ))
                    _summary_terms[cid] = _st
                node_set |= _st
            w_overlap = sum(
                q_idf[t] for t in q_terms if t in node_set
            ) / idf_sum
            base_ratio = min(1.0, base_hits.get(cid, 0) / max(1, len(base_by_id)))
            if _vec_sims is not None:
                vec_norm = (_vec_sims.get(cid, _vec_min) - _vec_min) / _vec_span
                score = 0.4 * vec_norm + 0.35 * w_overlap + 0.25 * base_ratio
            else:
                score = 0.75 * w_overlap + 0.25 * base_ratio
            if score > 0:
                scored.append((score, cid, node))
        scored.sort(key=lambda x: -x[0])

        pages_used = [cid for _, cid, _ in scored[:page_k]]
        page_hits = dict(base_hits)

        results: List[Dict[str, Any]] = []
        seen: set = set()

        # ── 页内候选：选中页的记忆（按 importance+recency 截断到 cap）──
        _filters = filters or {}
        _fp = (_filters.get("persona_id") or "").strip()
        _fa = (_filters.get("agent_id") or "").strip()
        _fs = (_filters.get("session_id") or "").strip()
        _fc = (_filters.get("category") or "").strip()
        for score, cid, node in scored[:page_k]:
            mems = []
            for mid in node.get("memory_ids") or []:
                rec = self.records.get(mid)
                if not rec:
                    continue
                if _fp and rec.get("persona_id") != _fp:
                    continue
                if _fa and (rec.get("agent_id") or "default") != _fa:
                    continue
                if _fs and rec.get("session_id") != _fs:
                    continue
                if _fc and rec.get("category") != _fc:
                    continue
                mems.append(rec)
            mems.sort(key=lambda m: (float(m.get("importance") or 0), m.get("_ts") or 0), reverse=True)
            for rec in mems[:page_mem_cap]:
                mid = rec["memory_id"]
                if mid in seen:
                    continue
                seen.add(mid)
                c_terms = _tokenize(rec["content"])
                mem_score = _overlap_ratio(q_terms, c_terms)
                mem_score += 0.15 * float(rec.get("importance") or 0.5)
                if rec.get("_ts"):
                    days = max(0.0, (time.time() - rec["_ts"]) / 86400.0)
                    mem_score += 0.05 * math.exp(-days / 365.0)
                results.append({
                    "memory_id": mid,
                    "content": rec["content"],
                    "score": round(mem_score, 6),
                    "category": rec["category"],
                    "tags": rec["tags"],
                    "persona_id": rec["persona_id"],
                    "session_id": rec["session_id"],
                    "created_at": rec["created_at"],
                    "importance": rec["importance"],
                    "page_path": node.get("parent", "") + " / " + node.get("title", ""),
                    "page_title": node.get("title", ""),
                    "page_node": cid,
                    "source_channel": "pagetree",
                    "in_base": mid in base_by_id,
                })
        results.sort(key=lambda r: -r["score"])

        # ── 基础召回兜底填充（保召回，页树未覆盖的命中不丢）──
        filled = 0
        for mid, r in base_by_id.items():
            if len(results) >= top_k:
                break
            if mid in seen:
                continue
            seen.add(mid)
            rec = dict(r)
            rec.setdefault("source_channel", "base")
            rec.setdefault("page_path", "")
            rec.setdefault("page_title", "")
            rec.setdefault("page_node", "")
            results.append(rec)
            filled += 1

        return {
            "results": results[:top_k],
            "pages_used": pages_used,
            "page_hits": page_hits,
            "filled_by_base": filled,
        }
