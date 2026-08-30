"""
# status: reserve (2026-09 EXECUTION 163)
Memory Page Manager — P1-2 自主分页模块

MemGPT 风格的 OS 虚拟内存分页，将上下文窗口视为物理内存页帧，
按重要性 × 相关性决策记忆的换入/换出，并维护重要性衰减。

设计要点:
    - 虚拟分页: 记忆 = 页，上下文窗口 = 驻留集 (resident set)
    - 决策引擎: 相关性 (search score) × 重要性 (importance) 排序
    - 换出策略: 低重要性 + 长时间未访问 + 非当前主题三因子
    - 重要性衰减: 指数衰减，半衰期可配置
    - 与 memory_store 解耦，通过 duck-typing 适配
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class MemoryPage:
    """单条记忆页 — 带运行时元数据。"""

    memory_id: str
    content: str = ""
    importance: float = 1.0
    token_count: int = 0
    last_access: float = 0.0
    access_count: int = 0
    topic_tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── 默认 token 估算器（英文 ~4 char/token，中文 ~1.5 char/token）───

def _estimate_tokens(text: str) -> int:
    """简单 token 计数估计（混合中英文）。"""
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4.0) + 1


class MemoryPageManager:
    """自主记忆分页管理器 — MemGPT 风格虚拟内存。

    使用方式::

        from trinity.modules.second_brain.memory_page_manager import MemoryPageManager
        from trinity.modules.second_brain.retrieval import TrinityRetrievalPipeline

        pipeline = TrinityRetrievalPipeline()
        page_mgr = MemoryPageManager(memory_store=pipeline, token_budget=7000)

        # 根据查询决定加载哪些记忆
        to_load = page_mgr.decide_page_in("Q3 华东区毛利率", current_context_tokens=1200)

        # 上下文快满时决定换出
        current_mems = [m for m in page_mgr.get_resident_set()]
        to_evict = page_mgr.decide_page_out(current_mems, required_tokens=800)

        # 执行换入/换出
        page_mgr.evict([m.memory_id for m in to_evict])
        page_mgr.load([m.memory_id for m in to_load])
    """

    # ── 构造函数 ──────────────────────────────────────────────────────

    def __init__(
        self,
        memory_store,
        token_budget: int = 7000,
        half_life_seconds: float = 600.0,
        importance_decay_enabled: bool = True,
    ):
        """初始化分页管理器。

        参数:
            memory_store: 记忆存储适配器，需实现 ``search(query, **kwargs) -> List[Dict]``。
                          返回的 dict 预期包含: id / text / importance / token_count (可选)。
            token_budget: 上下文窗口 token 总预算。
            half_life_seconds: 重要性衰减半衰期（秒），默认 600s（10min）。
            importance_decay_enabled: 是否启用重要性衰减。
        """
        self._store = memory_store
        self._token_budget = max(token_budget, 100)
        self._half_life = max(half_life_seconds, 1.0)
        self._decay_enabled = importance_decay_enabled

        # 驻留集（当前在上下文窗口中的记忆）
        self._resident: OrderedDict[str, MemoryPage] = OrderedDict()

        # 所有已知记忆页的元数据缓存（包括已换出的）
        self._page_cache: Dict[str, MemoryPage] = {}

        # 当前 token 用量
        self._resident_tokens: int = 0

        # 统计
        self._stats: Dict[str, int] = {
            "page_ins": 0,
            "page_outs": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "decay_events": 0,
            "total_decisions": 0,
        }
        self._lock = threading.Lock()

        logger.info(
            "MemoryPageManager initialized (budget=%d tokens, half_life=%.0fs)",
            self._token_budget, self._half_life,
        )

    # ── 核心决策: 换入 ────────────────────────────────────────────────

    def decide_page_in(
        self, query: str, current_context_tokens: int = 0
    ) -> List[MemoryPage]:
        """决策哪些记忆应加载到上下文窗口。

        策略:
            1. 通过 memory_store.search(query) 检索候选记忆
            2. 排除已在 resident set 中的记忆
            3. 按 score × importance 排序
            4. 贪心选择直到总 token 不超过 budget - current_context_tokens

        参数:
            query: 当前查询文本。
            current_context_tokens: 上下文窗口已占用的 token 数。

        返回:
            建议换入的 MemoryPage 列表（已排序）。
        """
        with self._lock:
            self._stats["total_decisions"] += 1

        available = self._token_budget - current_context_tokens
        if available <= 0:
            logger.debug("No token budget available for page-in")
            return []

        # 1) 检索候选
        try:
            raw_results = self._store.search(query, top_k=50)
        except Exception:
            logger.warning("memory_store.search failed for query: %s", query[:80])
            raw_results = []

        # 2) 构建 MemoryPage 并排除 resident
        candidates: List[MemoryPage] = []
        resident_ids = set(self._resident.keys())

        for r in raw_results:
            mid = r.get("id", "")
            if not mid or mid in resident_ids:
                continue
            page = self._ensure_page(mid, r)
            candidates.append(page)

        # 3) 计算综合得分: relevance_score × importance
        for page in candidates:
            raw = next(
                (r for r in raw_results if r.get("id") == page.memory_id), {}
            )
            relevance = raw.get("score", raw.get("relevance", 0.5))
            page.metadata["_relevance"] = float(relevance)
            page.metadata["_composite"] = float(relevance) * page.importance

        candidates.sort(key=lambda p: -p.metadata.get("_composite", 0))

        # 4) 贪心选择
        selected: List[MemoryPage] = []
        used = 0
        for page in candidates:
            tokens = page.token_count or _estimate_tokens(page.content)
            if used + tokens > available:
                continue
            selected.append(page)
            used += tokens

        self._stats["cache_misses"] += 1
        logger.debug(
            "decide_page_in: %d candidates → %d selected (used=%d/%d tokens)",
            len(candidates), len(selected), used, available,
        )
        return selected

    # ── 核心决策: 换出 ────────────────────────────────────────────────

    def decide_page_out(
        self,
        current_memories: List[MemoryPage],
        required_tokens: int,
        current_topic: str = "",
    ) -> List[MemoryPage]:
        """决策哪些记忆应从上下文窗口换出。

        策略（三因子加权）:
            - 低重要性     → weight: 0.4
            - 长时间未访问 → weight: 0.35
            - 非当前主题   → weight: 0.25
            总分越低越优先被换出。

        参数:
            current_memories: 当前在上下文窗口中的记忆列表。
            required_tokens: 需要腾出的 token 数量。
            current_topic: 当前主题词（用于主题匹配）。

        返回:
            建议换出的 MemoryPage 列表。
        """
        with self._lock:
            self._stats["total_decisions"] += 1

        if not current_memories:
            return []

        now = time.time()
        max_age = max(
            (now - m.last_access for m in current_memories if m.last_access > 0),
            default=1.0,
        )

        # 提取当前主题关键词
        topic_words: Set[str] = set()
        if current_topic:
            import re
            topic_words = set(
                w.lower() for w in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}",
                                               current_topic)
            )

        # 计算每个记忆的 eviction 评分（越低越优先换出）
        scored: List[tuple[float, MemoryPage]] = []
        for mem in current_memories:
            # 因子 1: 低重要性 (0 = 最重要, 1 = 最不重要)
            imp_score = 1.0 - min(mem.importance, 1.0)

            # 因子 2: 长时间未访问 (归一化)
            age = now - mem.last_access if mem.last_access > 0 else max_age
            age_score = min(age / max(max_age, 1.0), 1.0)

            # 因子 3: 非当前主题
            topic_score = 1.0
            if topic_words and mem.topic_tags:
                overlap = topic_words & {t.lower() for t in mem.topic_tags}
                if overlap:
                    topic_score = 1.0 - len(overlap) / len(topic_words)

            eviction_score = 0.4 * imp_score + 0.35 * age_score + 0.25 * topic_score
            scored.append((eviction_score, mem))

        # 按 eviction_score 降序（分越高越该换出）
        scored.sort(key=lambda x: -x[0])

        # 贪心换出直到满足 required_tokens
        selected: List[MemoryPage] = []
        freed = 0
        for _, mem in scored:
            if freed >= required_tokens:
                break
            tokens = mem.token_count or _estimate_tokens(mem.content)
            selected.append(mem)
            freed += tokens

        logger.debug(
            "decide_page_out: %d candidates → %d evicted (freed=%d tokens, needed=%d)",
            len(current_memories), len(selected), freed, required_tokens,
        )
        return selected

    # ── 驻留集查询 ────────────────────────────────────────────────────

    def get_resident_set(self) -> List[MemoryPage]:
        """返回当前驻留（已在上下文窗口中）的记忆列表。"""
        with self._lock:
            return list(self._resident.values())

    @property
    def resident_tokens(self) -> int:
        """当前驻留集 token 用量。"""
        return self._resident_tokens

    @property
    def token_budget(self) -> int:
        """token 总预算。"""
        return self._token_budget

    # ── 换出操作 ──────────────────────────────────────────────────────

    def evict(self, memory_ids: List[str]) -> int:
        """将指定记忆从驻留集换出（移出上下文窗口）。

        参数:
            memory_ids: 要换出的记忆 ID 列表。

        返回:
            实际换出的数量。
        """
        with self._lock:
            count = 0
            for mid in memory_ids:
                page = self._resident.pop(mid, None)
                if page is not None:
                    tokens = page.token_count or _estimate_tokens(page.content)
                    self._resident_tokens = max(0, self._resident_tokens - tokens)
                    page.metadata["_evicted_at"] = time.time()
                    count += 1
            self._stats["page_outs"] += count
            if count:
                logger.debug("evict: %d pages, resident_tokens=%d",
                             count, self._resident_tokens)
            return count

    # ── 换入操作 ──────────────────────────────────────────────────────

    def load(self, memory_ids: List[str]) -> int:
        """将指定记忆从存储换入驻留集（加载到上下文窗口）。

        参数:
            memory_ids: 要换入的记忆 ID 列表。

        返回:
            实际换入的数量。
        """
        with self._lock:
            count = 0
            now = time.time()

            for mid in memory_ids:
                if mid in self._resident:
                    # 已在驻留集中 — 刷新 access
                    self._resident[mid].last_access = now
                    self._resident[mid].access_count += 1
                    self._resident.move_to_end(mid)
                    continue

                page = self._page_cache.get(mid)
                if page is None:
                    # 尝试从 store 加载
                    try:
                        raw = self._store.search(mid, top_k=1)
                        if raw and raw[0].get("id") == mid:
                            page = self._ensure_page(mid, raw[0])
                    except Exception:
                        pass
                if page is None:
                    continue

                tokens = page.token_count or _estimate_tokens(page.content)

                # 检查预算
                if self._resident_tokens + tokens > self._token_budget:
                    logger.warning(
                        "Cannot load %s: would exceed budget (%d + %d > %d)",
                        mid, self._resident_tokens, tokens, self._token_budget,
                    )
                    continue

                page.last_access = now
                page.access_count += 1
                self._resident[mid] = page
                self._page_cache[mid] = page
                self._resident_tokens += tokens
                count += 1

            self._stats["page_ins"] += count
            if count:
                logger.debug("load: %d pages, resident_tokens=%d",
                             count, self._resident_tokens)
            return count

    # ── 重要性衰减 ────────────────────────────────────────────────────

    def apply_importance_decay(self) -> int:
        """对所有驻留记忆应用指数衰减。

        衰减公式:
            new_importance = importance × exp(-λ × Δt)
            其中 λ = ln(2) / half_life

        返回:
            被衰减的记忆数量。
        """
        if not self._decay_enabled:
            return 0

        with self._lock:
            now = time.time()
            lam = math.log(2) / self._half_life
            count = 0

            for page in self._resident.values():
                if page.access_count > 0:
                    # 活跃记忆不受衰减（最近访问过的不衰减）
                    delta = now - page.last_access
                    if delta > self._half_life * 0.5:
                        old = page.importance
                        page.importance = max(
                            0.01,
                            page.importance * math.exp(-lam * delta),
                        )
                        if abs(page.importance - old) > 0.001:
                            count += 1
                else:
                    # 从未被访问的记忆直接衰减
                    delta = now - max(
                        page.last_access,
                        page.metadata.get("_evicted_at", now - self._half_life),
                    )
                    page.importance = max(
                        0.01,
                        page.importance * math.exp(-lam * delta),
                    )
                    count += 1

            self._stats["decay_events"] += count
            return count

    # ── 统计接口 ──────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, int]:
        """返回分页统计信息。

        返回:
            {"page_ins": N, "page_outs": N, "resident_count": N,
             "resident_tokens": N, "token_budget": N,
             "cache_hits": N, "cache_misses": N,
             "decay_events": N, "total_decisions": N}
        """
        with self._lock:
            return {
                "page_ins": self._stats["page_ins"],
                "page_outs": self._stats["page_outs"],
                "resident_count": len(self._resident),
                "resident_tokens": self._resident_tokens,
                "token_budget": self._token_budget,
                "cache_hits": self._stats["cache_hits"],
                "cache_misses": self._stats["cache_misses"],
                "decay_events": self._stats["decay_events"],
                "total_decisions": self._stats["total_decisions"],
            }

    # ── 内部辅助 ──────────────────────────────────────────────────────

    def _ensure_page(self, memory_id: str, raw: Dict[str, Any]) -> MemoryPage:
        """从原始结果构建或获取缓存的 MemoryPage。"""
        if memory_id in self._page_cache:
            self._stats["cache_hits"] += 1
            return self._page_cache[memory_id]

        content = raw.get("text", raw.get("content", ""))
        page = MemoryPage(
            memory_id=memory_id,
            content=str(content),
            importance=float(raw.get("importance", 1.0)),
            token_count=int(raw.get("token_count", 0)
                            or _estimate_tokens(content)),
            last_access=float(raw.get("last_access", 0.0)),
            access_count=int(raw.get("access_count", 0)),
            topic_tags=list(raw.get("topic_tags", raw.get("tags", []))),
            metadata=raw.get("metadata", {}),
        )
        self._page_cache[memory_id] = page
        return page

    def _touch(self, memory_id: str) -> None:
        """标记记忆为最近访问（更新 last_access 和 access_count）。"""
        page = self._resident.get(memory_id)
        if page is not None:
            page.last_access = time.time()
            page.access_count += 1
            self._resident.move_to_end(memory_id)
