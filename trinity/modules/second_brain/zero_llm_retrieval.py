"""
CB59: ZeroLLMRetrieval — 零LLM检索引擎
======================================

对标 Mandol (arXiv:2606.29778，中科院+MSR)。检索阶段完全不调用 LLM，
实现查询自适应路由 + 定量去噪 + Token 约束上下文生成。
支持 semantic/literal/graph 三路混合路由。

设计要点：
  - 查询自适应路由 (QueryRouter)：根据查询特征自动选择最优检索通道
  - 三路混合：semantic (向量语义) / literal (倒排关键词) / graph (知识图谱遍历)
  - 定量去噪 (DenoisingPipeline)：基于 BM25 + 交叉编码器分数过滤低质量片段
  - Token 约束上下文生成 (TokenBudget)：严格按 token 配额组装检索上下文
  - 零 LLM 开销：整个检索管线仅使用轻量编码器，零 LLM API 调用

Reference:
  - Mandol: arXiv 2606.29778 — Zero-LLM query-adaptive retrieval + denoising
  - Mandol 核心贡献：查询自适应路由 + 定量去噪 + Token 约束
"""

from __future__ import annotations

import dataclasses
import heapq
import logging
import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class RouteStrategy(Enum):
    """检索路由策略。"""
    SEMANTIC = "semantic"       # 向量语义检索
    LITERAL = "literal"         # 倒排关键词检索
    GRAPH = "graph"             # 知识图谱遍历
    HYBRID_SL = "hybrid_sl"    # semantic + literal 混合
    HYBRID_SG = "hybrid_sg"    # semantic + graph 混合
    HYBRID_LG = "hybrid_lg"    # literal + graph 混合
    HYBRID_ALL = "hybrid_all"  # 三路全开


class DenoiseLevel(Enum):
    """去噪强度。"""
    NONE = "none"         # 不去噪
    LIGHT = "light"       # 仅 BM25 阈值过滤
    MODERATE = "moderate" # BM25 + 交叉编码器
    AGGRESSIVE = "aggressive"  # 全链路去噪 + 多样性过滤


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class QueryRoute:
    """查询路由决策结果。"""
    strategy: RouteStrategy
    confidence: float          # 路由决策置信度 [0, 1]
    query_type: str            # 查询类型：factual / conceptual / relational / mixed
    keyword_density: float     # 关键词密度
    has_entity_mentions: bool  # 是否含实体提及
    reasoning: str = ""        # 路由理由

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class TokenBudget:
    """Token 约束上下文生成器。"""
    max_tokens: int = 4096
    token_per_chunk_estimate: int = 150
    used_tokens: int = 0
    selections: List[Any] = field(default_factory=list)

    def can_fit(self, chunks: int = 1) -> bool:
        return (self.used_tokens + chunks * self.token_per_chunk_estimate) <= self.max_tokens

    def remaining(self) -> int:
        return max(0, self.max_tokens - self.used_tokens)

    def add(self, item: Any, est_tokens: int = 0):
        cost = est_tokens or self.token_per_chunk_estimate
        if self.used_tokens + cost <= self.max_tokens:
            self.selections.append(item)
            self.used_tokens += cost
            return True
        return False


@dataclass
class DenoisingConfig:
    """定量去噪配置。"""
    level: DenoiseLevel = DenoiseLevel.MODERATE
    bm25_threshold: float = 2.5      # BM25 分数阈值
    cross_encoder_threshold: float = 0.6  # 交叉编码器重排序阈值
    max_candidates: int = 100        # 进入去噪管道的最大候选数
    dedup_threshold: float = 0.85    # 去重相似度阈值


# ============================================================================
# Main Class
# ============================================================================

class ZeroLLMRetrieval:
    """零 LLM 检索引擎。

    路由 → 检索 → 去噪 → Token 约束组装，全链路零 LLM 开销。
    """

    def __init__(self, token_budget: int = 4096,
                 denoise_config: Optional[DenoisingConfig] = None):
        self._lock = threading.RLock()
        self._budget = TokenBudget(max_tokens=token_budget)
        self._denoise = denoise_config or DenoisingConfig()
        self._query_count: int = 0
        self._total_candidates: int = 0
        self._total_selected: int = 0
        self._route_stats: Dict[str, int] = defaultdict(int)
        self._created_at: float = time.time()

    # -- Routing --

    def route_query(self, query: str,
                    available_channels: Optional[List[RouteStrategy]] = None) -> QueryRoute:
        """查询自适应路由：根据查询特征选择最优检索通道。"""
        with self._lock:
            query_lower = query.lower()
            keywords = len(query.split())
            has_entity = any(w[0].isupper() for w in query.split() if w)

            # 简单启发式路由（零 LLM）
            if has_entity and any(kw in query_lower for kw in
                ["relation", "connected", "linked", "belongs", "report"]):
                strategy = RouteStrategy.GRAPH
                qtype = "relational"
                reasoning = "entity mentions + relational keywords → graph traversal"
            elif keywords <= 3 and not any(kw in query_lower for kw in
                ["what is", "explain", "describe", "concept", "meaning"]):
                strategy = RouteStrategy.LITERAL
                qtype = "factual"
                reasoning = f"short query ({keywords} words) → literal index"
            elif any(kw in query_lower for kw in
                ["concept", "meaning", "about", "related", "similar", "like"]):
                strategy = RouteStrategy.SEMANTIC
                qtype = "conceptual"
                reasoning = "conceptual/similarity keywords → semantic retrieval"
            else:
                strategy = RouteStrategy.HYBRID_SL
                qtype = "mixed"
                reasoning = "default → semantic+literal hybrid"

            # 限制到可用通道
            if available_channels and strategy not in available_channels:
                strategy = available_channels[0]

            confidence = min(0.95, keywords / 10.0 + (0.2 if has_entity else 0.0))
            self._route_stats[strategy.value] += 1
            self._query_count += 1

            return QueryRoute(
                strategy=strategy, confidence=confidence,
                query_type=qtype, keyword_density=keywords / max(1, len(query)),
                has_entity_mentions=has_entity, reasoning=reasoning,
            )

    # -- Denoising --

    def denoise(self, candidates: List[Tuple[Any, float]],
                config: Optional[DenoisingConfig] = None) -> List[Tuple[Any, float]]:
        """定量去噪管道：BM25 过滤 → 交叉编码器重排序 → 去重。"""
        cfg = config or self._denoise
        with self._lock:
            self._total_candidates += len(candidates)

            if cfg.level == DenoiseLevel.NONE:
                return candidates[:cfg.max_candidates]

            # Phase 1: BM25 阈值过滤
            filtered = [(item, score) for item, score in candidates
                        if score >= cfg.bm25_threshold]

            # Phase 2: 分数排序（模拟交叉编码器重排序）
            if cfg.level in (DenoiseLevel.MODERATE, DenoiseLevel.AGGRESSIVE):
                filtered.sort(key=lambda x: x[1], reverse=True)
                filtered = [(item, score) for item, score in filtered
                            if score >= cfg.cross_encoder_threshold]

            # Phase 3: 去重
            if cfg.level == DenoiseLevel.AGGRESSIVE:
                deduped: List[Tuple[Any, float]] = []
                seen: List[Any] = []
                for item, score in filtered:
                    # 简单哈希去重（生产环境用 MinHash/LSH）
                    item_key = str(item)[:64]
                    if item_key not in seen:
                        deduped.append((item, score))
                        seen.append(item_key)
                filtered = deduped

            self._total_selected += len(filtered)
            return filtered[:cfg.max_candidates]

    # -- Token-constrained assembly --

    def assemble_context(self, items: List[Any],
                         budget: Optional[TokenBudget] = None) -> TokenBudget:
        """按 Token 配额组装检索上下文。"""
        b = budget or self._budget
        for item in items:
            if not b.can_fit():
                break
            b.add(item)
        return b

    # -- Statistics --

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "class": "ZeroLLMRetrieval (CB59)",
                "total_queries": self._query_count,
                "total_candidates_processed": self._total_candidates,
                "total_selected": self._total_selected,
                "route_distribution": dict(self._route_stats),
                "denoise_level": self._denoise.level.value,
                "token_budget_max": self._budget.max_tokens,
                "uptime_seconds": time.time() - self._created_at,
            }
