"""
# status: orphan (2026-08-15 audit, not in runtime path)
SuperLocalMemory V3.3 — Ebbinghaus Forgetting Curve + Cognitive Quantization
=============================================================================
arXiv 2604.04514 · P49-4

艾宾浩斯遗忘曲线 + 认知量化 + 隐式参数化：基于遗忘曲线做衰减调度，
认知量化器合并冗余记忆为抽象知识节点，隐式→显式参数化桥接，
四层记忆体系（Sensory→STM→LTM→Implicit）协调。

设计要点:
  - EbbinghausForgettingSchedule: 艾宾浩斯遗忘曲线衰减+复习调度
  - CognitiveQuantizer: 冗余记忆合并为抽象知识节点
  - ImplicitParameterizationBridge: 隐式模式→显式行为规则
  - FourTierMemoryCoordinator: 四层协调
"""
from __future__ import annotations

import logging
import threading
import time
import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict, OrderedDict

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MemoryTier(Enum):
    SENSORY = auto()
    STM = auto()
    LTM = auto()
    IMPLICIT = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class MemoryItem:
    """通用记忆条目。"""
    item_id: str
    content: str = ""
    tier: MemoryTier = MemoryTier.STM
    retention: float = 1.0            # 记忆保留率 0~1
    repetitions: int = 0
    last_reviewed: float = 0.0
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CognitiveNode:
    """认知量化节点——多个相似记忆合并后的抽象表示。"""
    node_id: str
    abstract_content: str = ""
    source_count: int = 0
    confidence: float = 0.5
    source_ids: List[str] = field(default_factory=list)


@dataclass
class BehaviorRule:
    """隐式模式 → 显式行为规则。"""
    rule_id: str
    condition: str = ""
    action: str = ""
    confidence: float = 0.5
    activation_count: int = 0
    source_patterns: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# EbbinghausForgettingSchedule
# ---------------------------------------------------------------------------

class EbbinghausForgettingSchedule:
    """基于艾宾浩斯遗忘曲线的记忆衰减与复习调度。

    遗忘曲线: R(t) = exp(-t / S)
    复习间隔: 1天 → 2天 → 4天 → 7天 → 15天 → 30天（指数递增）
    """

    REVIEW_INTERVALS_HOURS = [24, 48, 96, 168, 360, 720]

    def __init__(self, stability_factor: float = 1.0) -> None:
        self.stability_factor = stability_factor
        self._lock = threading.RLock()

    def retention(self, hours_since_review: float, item: MemoryItem) -> float:
        """计算当前记忆保留率 R(t) = exp(-t / (S * stability))。"""
        with self._lock:
            S = self.stability_factor * (1.0 + 0.1 * item.repetitions)
            return math.exp(-hours_since_review / max(S, 1.0))

    def next_review_interval(self, item: MemoryItem) -> float:
        """计算下次复习间隔（小时）。"""
        idx = min(item.repetitions, len(self.REVIEW_INTERVALS_HOURS) - 1)
        return self.REVIEW_INTERVALS_HOURS[idx]

    def is_due(self, item: MemoryItem) -> bool:
        """判断是否需要复习。"""
        now = time.time()
        hours_since = (now - item.last_reviewed) / 3600.0 if item.last_reviewed > 0 else float("inf")
        return hours_since >= self.next_review_interval(item)

    def review(self, item: MemoryItem, quality: float = 1.0) -> MemoryItem:
        """执行一次复习更新。"""
        with self._lock:
            item.repetitions += 1
            item.last_reviewed = time.time()
            item.retention = min(1.0, item.retention + 0.1 * quality)
            self.stability_factor += 0.05 * quality
            return item

    def schedule_reviews(
        self, items: List[MemoryItem], top_k: int = 20,
    ) -> List[Tuple[MemoryItem, float]]:
        """批量调度：返回按紧迫度排序的待复习项。"""
        with self._lock:
            now = time.time()
            scored = []
            for item in items:
                if item.last_reviewed == 0:
                    urgency = 1.0
                else:
                    hours_since = (now - item.last_reviewed) / 3600.0
                    interval = self.next_review_interval(item)
                    urgency = max(0.0, hours_since / interval)

                r = self.retention(hours_since, item)
                scored.append((item, urgency * (1.0 - r)))

            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_k]

    def statistics(self) -> Dict[str, Any]:
        return {"stability_factor": round(self.stability_factor, 4)}


# ---------------------------------------------------------------------------
# CognitiveQuantizer
# ---------------------------------------------------------------------------

class CognitiveQuantizer:
    """认知量化器——将冗余相似记忆合并为抽象知识节点。

    策略: 相似度 > 阈值 → 合并为一条抽象总结 + 保留关键差异。
    """

    def __init__(self, similarity_threshold: float = 0.75) -> None:
        self.similarity_threshold = similarity_threshold
        self._nodes: List[CognitiveNode] = []
        self._lock = threading.RLock()

    def quantize(self, items: List[MemoryItem]) -> List[CognitiveNode]:
        """对一组记忆进行认知量化合并。"""
        with self._lock:
            clusters: List[List[MemoryItem]] = []
            used: Set[int] = set()

            for i, item_a in enumerate(items):
                if i in used:
                    continue
                cluster = [item_a]
                used.add(i)
                for j, item_b in enumerate(items):
                    if j in used:
                        continue
                    if self._text_similarity(item_a.content, item_b.content) >= self.similarity_threshold:
                        cluster.append(item_b)
                        used.add(j)
                clusters.append(cluster)

            nodes = []
            for cluster in clusters:
                if len(cluster) == 1:
                    node = CognitiveNode(
                        node_id=f"cog_{cluster[0].item_id}",
                        abstract_content=cluster[0].content[:200],
                        source_count=1,
                        source_ids=[cluster[0].item_id],
                    )
                else:
                    # 合并: 提取公共片段
                    contents = [c.content for c in cluster]
                    abstract = self._merge_contents(contents)
                    node = CognitiveNode(
                        node_id=f"cog_{cluster[0].item_id}_merged",
                        abstract_content=abstract,
                        source_count=len(cluster),
                        confidence=0.5 + 0.1 * min(len(cluster), 5),
                        source_ids=[c.item_id for c in cluster],
                    )
                nodes.append(node)

            self._nodes.extend(nodes)
            return nodes

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        a_words = set(a.lower().split())
        b_words = set(b.lower().split())
        if not a_words or not b_words:
            return 0.0
        intersection = a_words & b_words
        union = a_words | b_words
        return len(intersection) / len(union)

    @staticmethod
    def _merge_contents(contents: List[str]) -> str:
        words: Dict[str, int] = defaultdict(int)
        for c in contents:
            for w in c.lower().split():
                if len(w) > 3:
                    words[w] += 1
        common = sorted(words.items(), key=lambda x: x[1], reverse=True)[:10]
        return " | ".join(w for w, _ in common)

    def statistics(self) -> Dict[str, Any]:
        return {"nodes": len(self._nodes), "threshold": self.similarity_threshold}


# ---------------------------------------------------------------------------
# ImplicitParameterizationBridge
# ---------------------------------------------------------------------------

class ImplicitParameterizationBridge:
    """隐式→显式记忆参数化桥接——频繁模式 → 行为规则。

    检测高频率共现模式，提炼为 if-then 行为规则。
    """

    def __init__(self, min_activation: int = 3, confidence_threshold: float = 0.6) -> None:
        self.min_activation = min_activation
        self.confidence_threshold = confidence_threshold
        self._rules: Dict[str, BehaviorRule] = {}
        self._pattern_counts: Dict[str, int] = defaultdict(int)
        self._lock = threading.RLock()

    def ingest(self, condition: str, action: str) -> None:
        """注入一条条件→动作观察。"""
        with self._lock:
            pattern_key = f"{condition[:80]} -> {action[:80]}"
            self._pattern_counts[pattern_key] += 1

    def bridge(self) -> List[BehaviorRule]:
        """桥接：将频繁模式参数化为显式规则。"""
        with self._lock:
            new_rules = []
            for pattern, count in self._pattern_counts.items():
                if count < self.min_activation:
                    continue

                parts = pattern.split(" -> ", 1)
                condition, action = parts[0], parts[1] if len(parts) > 1 else ""

                rule_id = f"rule_{hash(pattern) & 0xFFFFFFFF:08x}"
                if rule_id in self._rules:
                    self._rules[rule_id].activation_count += 1
                    continue

                confidence = min(0.5 + 0.1 * (count - self.min_activation), 0.95)
                if confidence < self.confidence_threshold:
                    continue

                rule = BehaviorRule(
                    rule_id=rule_id, condition=condition, action=action,
                    confidence=confidence, activation_count=count,
                    source_patterns=[pattern],
                )
                self._rules[rule_id] = rule
                new_rules.append(rule)

            return new_rules

    def get_rules(self) -> List[BehaviorRule]:
        with self._lock:
            return list(self._rules.values())

    def statistics(self) -> Dict[str, Any]:
        return {"rules": len(self._rules), "patterns": len(self._pattern_counts)}


# ---------------------------------------------------------------------------
# FourTierMemoryCoordinator
# ---------------------------------------------------------------------------

class FourTierMemoryCoordinator:
    """四层记忆体系协调器——Sensory → STM → LTM → Implicit。

    流动规则:
      Sensory: 所有新输入，保留 ≤5分钟
      STM: 经注意力筛选，保留 ≤30分钟
      LTM: 经复习巩固，长期保留
      Implicit: 频繁模式的隐性知识
    """

    _TIER_CAPACITIES: Dict[MemoryTier, int] = {
        MemoryTier.SENSORY: 50,
        MemoryTier.STM: 200,
        MemoryTier.LTM: 5000,
        MemoryTier.IMPLICIT: 500,
    }

    def __init__(self) -> None:
        self._tiers: Dict[MemoryTier, OrderedDict[str, MemoryItem]] = {
            t: OrderedDict() for t in MemoryTier
        }
        self._item_counter: int = 0
        self._lock = threading.RLock()

    def ingest(self, content: str) -> MemoryItem:
        """新输入进入 Sensory。"""
        with self._lock:
            item = MemoryItem(
                item_id=f"mem_{self._item_counter}",
                content=content, tier=MemoryTier.SENSORY,
            )
            self._item_counter += 1
            self._tiers[MemoryTier.SENSORY][item.item_id] = item
            self._enforce_capacity(MemoryTier.SENSORY)
            return item

    def promote(self, item_id: str, schedule: EbbinghausForgettingSchedule) -> Optional[MemoryItem]:
        """晋升：Sensory→STM 或 STM→LTM。"""
        with self._lock:
            for src_tier, dst_tier in [(MemoryTier.SENSORY, MemoryTier.STM),
                                        (MemoryTier.STM, MemoryTier.LTM)]:
                if item_id in self._tiers[src_tier]:
                    item = self._tiers[src_tier].pop(item_id)
                    item.tier = dst_tier
                    self._tiers[dst_tier][item_id] = item
                    schedule.review(item, quality=0.8)
                    self._enforce_capacity(dst_tier)
                    return item
            return None

    def consolidate_to_implicit(
        self, bridge: ImplicitParameterizationBridge,
        quantizer: CognitiveQuantizer,
    ) -> List[BehaviorRule]:
        """LTM → Implicit 固化：高频模式 → 行为规则。"""
        with self._lock:
            ltm_items = list(self._tiers[MemoryTier.LTM].values())
            if len(ltm_items) < 10:
                return []

            nodes = quantizer.quantize(ltm_items[-50:])
            for node in nodes:
                bridge.ingest(node.abstract_content, "recall_pattern")

            rules = bridge.bridge()
            for rule in rules:
                item = MemoryItem(
                    item_id=rule.rule_id,
                    content=f"{rule.condition} -> {rule.action}",
                    tier=MemoryTier.IMPLICIT,
                    metadata={"confidence": rule.confidence},
                )
                self._tiers[MemoryTier.IMPLICIT][item.item_id] = item

            self._enforce_capacity(MemoryTier.IMPLICIT)
            return rules

    def _enforce_capacity(self, tier: MemoryTier) -> None:
        cap = self._TIER_CAPACITIES[tier]
        tier_dict = self._tiers[tier]
        while len(tier_dict) > cap:
            tier_dict.popitem(last=False)

    def statistics(self) -> Dict[str, Any]:
        return {
            "tiers": {t.name: len(self._tiers[t]) for t in MemoryTier},
            "total": sum(len(self._tiers[t]) for t in MemoryTier),
        }
