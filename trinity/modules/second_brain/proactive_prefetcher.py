"""
Proactive Prefetcher — P1-1 主动预取模块

基于会话上下文（用户、任务、历史话题）预测用户即将需要哪些记忆，
在查询到达前主动预取并预热缓存，降低首查询延迟。

设计要点:
    - LRU 缓存 + TTL 双重淘汰
    - 关键词扩展 + 高频实体关联生成预取查询词
    - 与 TrinityRetrievalPipeline 解耦，通过 duck-typing 适配任意 retriever
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ProactivePrefetcher:
    """主动预取器 — 预测用户查询意图，提前预热检索缓存。

    使用方式::

        from trinity.modules.second_brain.retrieval import TrinityRetrievalPipeline
        from trinity.modules.second_brain.proactive_prefetcher import ProactivePrefetcher

        pipeline = TrinityRetrievalPipeline()
        prefetcher = ProactivePrefetcher(retriever=pipeline, cache_size=200)
        prefetcher.on_session_start({
            "user_id": "u_42",
            "task": "分析 Q3 销售数据",
            "recent_topics": ["Q2 复盘", "竞品分析", "渠道策略"],
        })
        results = prefetcher.on_query("Q3 华东区毛利率")
    """

    # ── 构造函数 ──────────────────────────────────────────────────────

    def __init__(self, retriever, cache_size: int = 100):
        """初始化预取器。

        参数:
            retriever: 检索器实例，需实现 ``search(query, **kwargs) -> List[Dict]``。
            cache_size: LRU 缓存最大条目数。
        """
        self._retriever = retriever
        self._cache_size = max(cache_size, 1)

        # LRU 缓存: OrderedDict[query_key -> {"results": ..., "timestamp": ...}]
        self._cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()

        # 统计
        self._stats: Dict[str, int] = {
            "cache_hits": 0,
            "cache_misses": 0,
            "prefetch_count": 0,
            "total_queries": 0,
        }

        # 会话上下文
        self._context: Optional[Dict[str, Any]] = None
        self._prefetch_lock = threading.Lock()

        logger.info(
            "ProactivePrefetcher initialized (cache_size=%d)", cache_size,
        )

    # ── 会话生命周期 ──────────────────────────────────────────────────

    def on_session_start(self, context: Dict[str, Any]) -> None:
        """会话开始时：预测 + 预取 + 预热缓存。

        参数:
            context: 会话上下文，可包含:
                - user_id (str): 用户标识
                - task (str): 当前任务描述
                - recent_topics (List[str]): 近期讨论话题
                - entities (List[str]): 已知高频实体（可选）
                - session_id (str): 会话 ID（可选）
        """
        self._context = context
        logger.info(
            "Session start — user=%s task=%s",
            context.get("user_id", "?"),
            context.get("task", "?")[:60],
        )

        # 步骤 1: 生成预测查询词
        prefetch_queries = self.predict_relevance(context)
        if not prefetch_queries:
            logger.debug("No prefetch queries generated — skipping warmup")
            return

        # 步骤 2: 后台异步预取（避免阻塞会话启动）
        def _warmup():
            fetched = 0
            for q in prefetch_queries:
                try:
                    results = self._retriever.search(q)
                    self._set_cache(q, results)
                    fetched += 1
                except Exception:
                    logger.warning("Prefetch failed for query: %s", q[:80])
            with self._prefetch_lock:
                self._stats["prefetch_count"] += fetched
            logger.info("Prefetch warmup complete — %d/%d queries cached",
                        fetched, len(prefetch_queries))

        threading.Thread(target=_warmup, daemon=True).start()

    # ── 查询接口 ──────────────────────────────────────────────────────

    def on_query(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        """处理一次查询：先查缓存，未命中则走检索并更新缓存。

        参数:
            query: 查询字符串。
            **kwargs: 透传至 retriever.search（如 top_k / use_reranker）。

        返回:
            检索结果列表。
        """
        self._stats["total_queries"] += 1

        # 1) 查缓存
        cache_key = self._cache_key(query)
        cached = self._get_cache(cache_key)
        if cached is not None:
            self._stats["cache_hits"] += 1
            return cached

        # 2) 缓存未命中 — 走检索器
        self._stats["cache_misses"] += 1
        results = self._retriever.search(query, **kwargs)

        # 3) 更新缓存
        self._set_cache(cache_key, results)

        return results

    # ── 预测逻辑 ──────────────────────────────────────────────────────

    def predict_relevance(self, context: Dict[str, Any]) -> List[str]:
        """核心预测逻辑：基于会话上下文生成一组预取查询词。

        策略:
            1. 从 recent_topics 中提取关键词（TF 加权）
            2. 从 task 描述中提取名词短语
            3. 跨话题组合生成 conjunctive queries（如 "topic_A + topic_B"）
            4. 如果 context 提供 entities 列表，追加实体查询

        参数:
            context: 同 on_session_start 的 context。

        返回:
            预取查询词列表（已去重）。
        """
        queries: List[str] = []
        seen: set[str] = set()

        def _add(q: str):
            q = q.strip().lower()
            if q and q not in seen and len(q) >= 2:
                seen.add(q)
                queries.append(q)

        # ── 来源 1: recent_topics → 关键词扩展 ──
        topics: List[str] = context.get("recent_topics", [])
        if isinstance(topics, str):
            topics = [topics]

        for topic in topics:
            _add(topic)
            # 拆词: 提取 2-4 字的中文词和英文单词
            words = self._tokenize(topic)
            for w in words:
                _add(w)

        # ── 来源 2: task → 名词短语 ──
        task: str = context.get("task", "")
        if task:
            _add(task)
            for phrase in self._extract_phrases(task):
                _add(phrase)

        # ── 来源 3: 跨话题 conjunctive ──
        if len(topics) >= 2:
            for i in range(min(len(topics), 4)):
                for j in range(i + 1, min(len(topics), 5)):
                    combined = f"{topics[i]} {topics[j]}"
                    _add(combined)

        # ── 来源 4: 高频实体 ──
        entities: List[str] = context.get("entities", [])
        if isinstance(entities, list):
            for ent in entities[:10]:
                _add(ent)

        return queries

    # ── 统计接口 ──────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, int]:
        """返回缓存统计信息。

        返回:
            {"cache_hits": N, "cache_misses": N, "prefetch_count": N,
             "total_queries": N, "cache_size": N, "cache_max": N}
        """
        return {
            "cache_hits": self._stats["cache_hits"],
            "cache_misses": self._stats["cache_misses"],
            "prefetch_count": self._stats["prefetch_count"],
            "total_queries": self._stats["total_queries"],
            "cache_size": len(self._cache),
            "cache_max": self._cache_size,
        }

    # ── 缓存内部方法 ──────────────────────────────────────────────────

    def _cache_key(self, query: str) -> str:
        """生成缓存键（归一化后的 query 字符串）。"""
        return query.strip().lower()

    def _get_cache(self, key: str) -> Optional[List[Dict[str, Any]]]:
        """从 LRU 缓存读取。命中时将该条目移至末尾（最近使用）。"""
        entry = self._cache.get(key)
        if entry is not None:
            # 简单 TTL 检查（默认 300 秒）
            if time.time() - entry["timestamp"] > 300:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return entry["results"]
        return None

    def _set_cache(self, key: str, results: List[Dict[str, Any]]):
        """写入 LRU 缓存，超出容量时淘汰最久未用条目。"""
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = {
            "results": results,
            "timestamp": time.time(),
        }
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    # ── 关键词提取辅助方法 ───────────────────────────────────────────

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """简单分词：提取中文 2-4 字词组和英文单词。"""
        import re

        tokens: List[str] = []
        # 中文词组: 连续中文字 2-4 个
        for m in re.finditer(r"[\u4e00-\u9fff]{2,4}", text):
            tokens.append(m.group(0))
        # 英文单词: 3+ 字母
        for m in re.finditer(r"[A-Za-z]{3,}", text):
            tokens.append(m.group(0).lower())
        return tokens

    @staticmethod
    def _extract_phrases(text: str) -> List[str]:
        """从 task 描述中提取有意义的名词短语。"""
        import re

        phrases: List[str] = []
        # 中文: 连续中文字 + 可能的英文/数字组合
        for m in re.finditer(
            r"[\u4e00-\u9fffA-Za-z0-9]{2,}(?:[\u4e00-\u9fffA-Za-z0-9\s]{0,20}[\u4e00-\u9fffA-Za-z0-9]{2,})?",
            text,
        ):
            phrase = m.group(0).strip()
            if len(phrase) >= 3:
                phrases.append(phrase.lower())
        return phrases
