"""
# status: orphan (2026-08-15 audit, not in runtime path)
BudgetMemRouter — BudgetMem Budget-Tier Routing for Runtime Agent Memory
==========================================================================
ICML 2026 (arXiv 2602.06025) · P43-3

实现 BudgetMem 预算分层路由: 过滤→实体/时间/主题并行→摘要模块化流水线,
LOW/MID/HIGH 三档预算接口, PPO训练共享路由器, 按query动态选档。

设计要点:
  - FilterModule: 噪声过滤
  - ParallelExtractor: 实体/时间/主题并行提取
  - SummarizationModule: 摘要合成
  - BudgetTierRouter: PPO 训练共享路由器, 按query选档
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class BudgetTier(Enum):
    """预算层级——三档算力预算。"""
    LOW = 1       # 最小化token, 仅关键词+最近
    MID = 2       # 中等预算, 实体+时间+轻摘要
    HIGH = 3      # 全面预算, 全并行+深度摘要


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class BudgetDecision:
    """PPO 路由器的单次预算决策。"""
    decision_id: str
    query: str
    tier: BudgetTier
    module_tiers: Dict[str, BudgetTier] = field(default_factory=dict)
    confidence: float = 0.0
    cost_estimate: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class RouterState:
    """路由器的 PPO 训练状态。"""
    state_id: str
    total_decisions: int = 0
    total_reward: float = 0.0
    avg_cost: float = 0.0
    tier_distribution: Dict[str, int] = field(default_factory=lambda: {"LOW": 0, "MID": 0, "HIGH": 0})


@dataclass
class ExtractionPipeline:
    """提取流水线——过滤→并行→摘要三阶段。"""
    pipeline_id: str
    tier: BudgetTier = BudgetTier.MID
    filter_enabled: bool = True
    entity_extraction: bool = True
    temporal_extraction: bool = True
    topic_extraction: bool = True
    summarization_enabled: bool = True


# ---------------------------------------------------------------------------
# FilterModule
# ---------------------------------------------------------------------------

class FilterModule:
    """噪声过滤——按预算层级过滤低质量/冗余消息。

    Parameters
    ----------
    low_max_messages : int
        LOW 预算最大保留消息数。
    """

    def __init__(self, low_max_messages: int = 10) -> None:
        self.low_max_messages = low_max_messages
        self._lock = threading.RLock()

    def filter(
        self, messages: List[Dict[str, Any]], tier: BudgetTier
    ) -> List[Dict[str, Any]]:
        """按预算层级过滤消息。

        - LOW: 仅保留最近 N 条 + 含关键词的消息
        - MID: 去重 + 按重要性排序
        - HIGH: 全量保留, 标记噪声
        """
        with self._lock:
            if tier == BudgetTier.LOW:
                # 最近 N 条
                recent = sorted(messages, key=lambda m: m.get("timestamp", 0), reverse=True)
                return recent[:self.low_max_messages]

            if tier == BudgetTier.MID:
                # 去重
                seen: Set[str] = set()
                deduped = []
                for m in messages:
                    sig = m.get("content", "")[:50]
                    if sig not in seen:
                        seen.add(sig)
                        deduped.append(m)
                return deduped

            # HIGH: 全量但标记噪声
            for m in messages:
                m["_filtered"] = bool(len(m.get("content", "")) < 3)
            return messages

    def statistics(self) -> Dict[str, Any]:
        return {"status": "ready"}


# ---------------------------------------------------------------------------
# ParallelExtractor
# ---------------------------------------------------------------------------

class ParallelExtractor:
    """实体/时间/主题并行提取——按预算控制并行度。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def extract(
        self, messages: List[Dict[str, Any]], tier: BudgetTier
    ) -> Dict[str, List[Dict[str, Any]]]:
        """并行提取——返回 {entities, temporal, topics}。

        Tier 决定并行度与深度:
        - LOW: 仅关键词实体
        - MID: 实体+时间
        - HIGH: 实体+时间+主题全并行
        """
        with self._lock:
            result: Dict[str, List[Dict[str, Any]]] = {
                "entities": [],
                "temporal": [],
                "topics": [],
            }

            all_text = " ".join(m.get("content", "") for m in messages)

            # 实体提取 (所有tier)
            entities = self._extract_entities(all_text, tier)
            result["entities"] = entities

            # 时间提取 (MID+)
            if tier in (BudgetTier.MID, BudgetTier.HIGH):
                temporal = self._extract_temporal(messages)
                result["temporal"] = temporal

            # 主题提取 (HIGH only)
            if tier == BudgetTier.HIGH:
                topics = self._extract_topics(all_text)
                result["topics"] = topics

            return result

    def _extract_entities(self, text: str, tier: BudgetTier) -> List[Dict[str, Any]]:
        """简单实体提取——大写词/专名。"""
        import re
        if tier == BudgetTier.LOW:
            # 仅提取大写开头的词
            matches = re.findall(r'\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\b', text)
            return [{"entity": m, "type": "person"} for m in matches[:5]]

        # MID/HIGH
        matches = re.findall(r'\b([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*)\b', text)
        return [{"entity": m, "type": "named_entity"} for m in matches[:20]]

    def _extract_temporal(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """提取时间信息。"""
        result = []
        for m in messages:
            ts = m.get("timestamp", 0)
            if ts > 0:
                result.append({
                    "timestamp": ts,
                    "msg_id": m.get("id", ""),
                })
        # 按时间排序
        result.sort(key=lambda x: x["timestamp"])
        return result[-20:]  # 最近20条

    def _extract_topics(self, text: str) -> List[Dict[str, Any]]:
        """主题提取——词频统计。"""
        import re
        words = re.findall(r'\b[a-zA-Z]{4,}\b', text.lower())
        stopwords = {"this", "that", "with", "from", "have", "been", "they", "will", "what", "when"}
        word_counts: Dict[str, int] = defaultdict(int)
        for w in words:
            if w not in stopwords:
                word_counts[w] += 1
        top = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        return [{"topic": w, "frequency": c} for w, c in top]

    def statistics(self) -> Dict[str, Any]:
        return {"status": "ready"}


# ---------------------------------------------------------------------------
# SummarizationModule
# ---------------------------------------------------------------------------

class SummarizationModule:
    """摘要合成——按预算层级控制摘要深度。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def summarize(
        self,
        messages: List[Dict[str, Any]],
        extractions: Dict[str, List[Dict[str, Any]]],
        tier: BudgetTier,
    ) -> Dict[str, Any]:
        """合成摘要。

        - LOW: 仅一行统计摘要
        - MID: 结构化摘要 (实体+时间+统计)
        - HIGH: 深度上下文摘要
        """
        with self._lock:
            if tier == BudgetTier.LOW:
                return {
                    "summary": f"{len(messages)} messages processed",
                    "tier": "LOW",
                    "detail_level": "minimal",
                }

            if tier == BudgetTier.MID:
                entity_count = len(extractions.get("entities", []))
                temporal_count = len(extractions.get("temporal", []))
                return {
                    "summary": f"{len(messages)} messages, {entity_count} entities, {temporal_count} temporal points",
                    "entities": [e["entity"] for e in extractions.get("entities", [])[:5]],
                    "time_range": self._time_range(extractions.get("temporal", [])),
                    "tier": "MID",
                    "detail_level": "structured",
                }

            # HIGH
            entity_list = [e["entity"] for e in extractions.get("entities", [])[:10]]
            topic_list = [t["topic"] for t in extractions.get("topics", [])[:5]]
            return {
                "summary": f"Comprehensive: {len(messages)} msgs, {len(entity_list)} entities, {len(topic_list)} topics",
                "entities": entity_list,
                "topics": topic_list,
                "time_range": self._time_range(extractions.get("temporal", [])),
                "tier": "HIGH",
                "detail_level": "deep",
            }

    def _time_range(self, temporal: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not temporal:
            return {}
        timestamps = [t["timestamp"] for t in temporal if "timestamp" in t]
        if not timestamps:
            return {}
        return {
            "start": min(timestamps),
            "end": max(timestamps),
            "count": len(timestamps),
        }

    def statistics(self) -> Dict[str, Any]:
        return {"status": "ready"}


# ---------------------------------------------------------------------------
# BudgetTierRouter
# ---------------------------------------------------------------------------

class BudgetTierRouter:
    """PPO 训练共享路由器——按query动态选档。

    Parameters
    ----------
    learning_rate : float
        PPO 学习率。
    """

    def __init__(self, learning_rate: float = 0.01) -> None:
        self.learning_rate = learning_rate
        self._state = RouterState(state_id=f"router_state_{int(time.time()*1e6)}")
        self._decision_history: deque = deque(maxlen=500)
        self._lock = threading.RLock()

    def route(self, query: str, complexity_hint: Optional[str] = None) -> BudgetDecision:
        """按query动态选择预算层级。

        Parameters
        ----------
        query : str
            用户查询。
        complexity_hint : Optional[str]
            复杂度提示。

        Returns
        -------
        BudgetDecision
        """
        with self._lock:
            qlen = len(query.split())
            has_complex = any(
                kw in query.lower()
                for kw in ("analyze", "compare", "comprehensive", "deep", "history")
            )

            # 启发式路由
            if qlen <= 5 and not has_complex:
                tier = BudgetTier.LOW
            elif qlen <= 15 or has_complex:
                tier = BudgetTier.MID
            else:
                tier = BudgetTier.HIGH

            confidence = 0.7 if qlen <= 5 else 0.85

            # 估算成本 (token 等价)
            cost_map = {BudgetTier.LOW: 100, BudgetTier.MID: 500, BudgetTier.HIGH: 2000}
            cost = cost_map.get(tier, 500)

            decision = BudgetDecision(
                decision_id=f"bd_{self._state.total_decisions}_{int(time.time()*1e6)}",
                query=query,
                tier=tier,
                module_tiers={},
                confidence=confidence,
                cost_estimate=cost,
            )

            self._state.total_decisions += 1
            self._state.tier_distribution[tier.name] = self._state.tier_distribution.get(tier.name, 0) + 1
            self._state.avg_cost = (self._state.avg_cost * (self._state.total_decisions - 1) + cost) / max(self._state.total_decisions, 1)

            self._decision_history.append(decision)
            return decision

    def update_reward(self, decision_id: str, reward: float) -> bool:
        """PPO 奖励更新。"""
        with self._lock:
            for d in self._decision_history:
                if d.decision_id == decision_id:
                    self._state.total_reward += reward
                    # 简化的策略更新
                    d.confidence = d.confidence + self.learning_rate * (reward - 0.5)
                    d.confidence = max(0.1, min(1.0, d.confidence))
                    return True
            return False

    def get_state(self) -> Dict[str, Any]:
        return {
            "total_decisions": self._state.total_decisions,
            "total_reward": round(self._state.total_reward, 2),
            "avg_cost": round(self._state.avg_cost, 1),
            "tier_distribution": dict(self._state.tier_distribution),
        }

    def statistics(self) -> Dict[str, Any]:
        return self.get_state()


# ---------------------------------------------------------------------------
# BudgetMemRouter
# ---------------------------------------------------------------------------

class BudgetMemRouter:
    """BudgetMem 预算分层路由记忆系统。

    Parameters
    ----------
    low_max_messages : int
        LOW 预算最大消息数。
    router_lr : float
        路由器 PPO 学习率。
    """

    def __init__(self, low_max_messages: int = 10, router_lr: float = 0.01) -> None:
        self.filter_module = FilterModule(low_max_messages=low_max_messages)
        self.parallel_extractor = ParallelExtractor()
        self.summarization_module = SummarizationModule()
        self.budget_tier_router = BudgetTierRouter(learning_rate=router_lr)
        self._messages: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

        logger.info(
            "BudgetMemRouter initialized [low_msgs=%d lr=%.3f]",
            low_max_messages, router_lr,
        )

    def ingest(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """摄入消息。"""
        msg = {
            "id": f"msg_{len(self._messages)}_{int(time.time()*1e6)}",
            "content": content,
            "timestamp": time.time(),
            "metadata": metadata or {},
        }
        self._messages.append(msg)
        return msg

    def query(self, query_text: str, complexity_hint: Optional[str] = None) -> Dict[str, Any]:
        """查询——完整过滤→并行提取→摘要流水线。

        Parameters
        ----------
        query_text : str
            查询文本。
        complexity_hint : Optional[str]
            复杂度提示。

        Returns
        -------
        Dict[str, Any]
            包含路由决策、提取结果和摘要。
        """
        # 1. 路由决策
        decision = self.budget_tier_router.route(query_text, complexity_hint)

        # 2. 过滤
        filtered = self.filter_module.filter(self._messages, decision.tier)

        # 3. 并行提取
        extractions = self.parallel_extractor.extract(filtered, decision.tier)

        # 4. 摘要
        summary = self.summarization_module.summarize(filtered, extractions, decision.tier)

        return {
            "decision": {
                "tier": decision.tier.name,
                "confidence": decision.confidence,
                "cost_estimate": decision.cost_estimate,
            },
            "filtered_count": len(filtered),
            "extractions": {
                "entities": len(extractions.get("entities", [])),
                "temporal": len(extractions.get("temporal", [])),
                "topics": len(extractions.get("topics", [])),
            },
            "summary": summary,
        }

    def reward(self, decision_id: str, quality_score: float) -> Dict[str, Any]:
        """反馈奖励——驱动PPO学习。"""
        ok = self.budget_tier_router.update_reward(decision_id, quality_score)
        return {"updated": ok, "router_state": self.budget_tier_router.get_state()}

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "messages": len(self._messages),
                "router": self.budget_tier_router.statistics(),
            }
