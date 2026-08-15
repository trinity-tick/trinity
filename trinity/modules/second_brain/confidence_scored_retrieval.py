"""
# status: orphan (2026-08-15 audit, not in runtime path)
P14-2: Confidence-Scored Memory Retrieval (对标 AgentPrizm 2026.07.09)
========================================================================

核心设计（基于 AgentPrizm Confidence Scored Retrieval with Fact Validity Windows）：
  - ConfidenceScorer：为每条记忆检索结果分配四维置信度分数
    （来源权威性 / 引用一致性 / 时间新鲜度 / 语义匹配度）
  - FactValidityWindow：事实有效期窗口机制
    （金融数据/h 级、法规/月级、个人偏好/周级），过期自动降权
  - ContradictionResolver：记忆矛盾自动裁决
    （基于置信度 + 时效性 + 来源权威性三方加权）
  - AuditableRecallReceipt：可审计召回回执
    （检索时间 / 查询意图 / 命中记忆 ID / 置信度 / 时效性 / 来源链）

兼容性：
  - 与 staleness_detector.py 的 StalenessDetector 接口兼容
  - 与 audit_trail.py 的 AuditTrail 接口兼容

Reference:
  - AgentPrizm: Confidence-Scored Retrieval with Fact Validity Windows (2026.07.09)
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ──────────────────────────────────────────────────

class ValidityCategory(Enum):
    """事实有效期类别。"""
    FINANCIAL = "financial"          # 金融数据：小时级（4h）
    REGULATORY = "regulatory"        # 法规：月级（30d）
    PERSONAL_PREFERENCE = "personal"  # 个人偏好：周级（7d）
    GENERAL_KNOWLEDGE = "general"    # 通用知识：年级（365d）
    NEWS = "news"                     # 新闻：日级（24h）


class ConfidenceDimension(Enum):
    """置信度评分维度。"""
    SOURCE_AUTHORITY = "source_authority"        # 来源权威性
    CITATION_CONSISTENCY = "citation_consistency"  # 引用一致性
    TEMPORAL_FRESHNESS = "temporal_freshness"     # 时间新鲜度
    SEMANTIC_MATCH = "semantic_match"              # 语义匹配度


class ContradictionVerdict(Enum):
    """矛盾裁决结果。"""
    PREFER_A = "prefer_memory_a"
    PREFER_B = "prefer_memory_b"
    MERGE = "merge"
    DISCARD_BOTH = "discard_both"
    FLAG_FOR_REVIEW = "flag_for_review"


class SourceType(Enum):
    """来源类型分级（权威性排序）。"""
    OFFICIAL_DOCUMENT = 5    # 官方文档 / 法律文书
    PEER_REVIEWED = 4        # 同行评审论文
    VERIFIED_DATABASE = 4    # 经验证的数据库
    USER_CONFIRMED = 3       # 用户确认过的记忆
    LLM_GENERATED = 2        # LLM 生成的记忆
    UNVERIFIED = 1           # 未验证来源
    ANONYMOUS = 0            # 匿名/未知来源


class FreshnessTier(Enum):
    """新鲜度等级。"""
    FRESH = "fresh"              # 有效期内
    STALE = "stale"              # 刚过期
    EXPIRED = "expired"          # 严重过期
    DEPRECATED = "deprecated"    # 已废弃


# ── 数据结构 ──────────────────────────────────────────────────────

@dataclass
class ConfidenceScore:
    """四维置信度分数。"""
    source_authority: float = 0.0
    citation_consistency: float = 0.0
    temporal_freshness: float = 0.0
    semantic_match: float = 0.0

    @property
    def overall(self) -> float:
        """加权综合置信度。"""
        weights = np.array([0.30, 0.25, 0.25, 0.20])
        scores = np.array([
            self.source_authority,
            self.citation_consistency,
            self.temporal_freshness,
            self.semantic_match,
        ])
        return float(np.dot(weights, scores))

    def to_dict(self) -> Dict[str, float]:
        return {
            "source_authority": round(self.source_authority, 4),
            "citation_consistency": round(self.citation_consistency, 4),
            "temporal_freshness": round(self.temporal_freshness, 4),
            "semantic_match": round(self.semantic_match, 4),
            "overall": round(self.overall, 4),
        }


@dataclass
class ValidityWindow:
    """事实有效期窗口。"""
    category: ValidityCategory
    created_at: float  # Unix timestamp
    expires_at: float  # Unix timestamp
    last_validated_at: float = 0.0

    @property
    def time_to_live_hours(self) -> float:
        return max(0.0, (self.expires_at - time.time()) / 3600.0)

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @classmethod
    def for_category(cls, category: ValidityCategory, created_at: Optional[float] = None) -> ValidityWindow:
        """按类别创建有效期窗口。"""
        now = created_at or time.time()
        ttl_map = {
            ValidityCategory.FINANCIAL: 4 * 3600,            # 4 小时
            ValidityCategory.REGULATORY: 30 * 86400,         # 30 天
            ValidityCategory.PERSONAL_PREFERENCE: 7 * 86400,  # 7 天
            ValidityCategory.GENERAL_KNOWLEDGE: 365 * 86400,  # 365 天
            ValidityCategory.NEWS: 24 * 3600,                 # 24 小时
        }
        ttl = ttl_map.get(category, 30 * 86400)
        return cls(category=category, created_at=now, expires_at=now + ttl)


@dataclass
class MemoryFact:
    """记忆事实条目（用于矛盾裁决）。"""
    fact_id: str
    content: str
    confidence: ConfidenceScore = field(default_factory=ConfidenceScore)
    validity: Optional[ValidityWindow] = None
    source_type: SourceType = SourceType.UNVERIFIED
    source_chain: List[str] = field(default_factory=list)  # 来源溯源链
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditableRecallReceipt:
    """可审计召回回执。"""
    receipt_id: str
    timestamp: str  # ISO 8601
    query_intent: str
    hit_memory_ids: List[str]
    confidence_scores: Dict[str, ConfidenceScore]
    validity_windows: Dict[str, ValidityWindow]
    source_chains: Dict[str, List[str]]
    retrieval_latency_ms: float
    version: str = "AgentPrizm-1.0"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "timestamp": self.timestamp,
            "query_intent": self.query_intent,
            "hit_memory_ids": self.hit_memory_ids,
            "confidence_scores": {
                mid: cs.to_dict() for mid, cs in self.confidence_scores.items()
            },
            "validity_windows": {
                mid: {
                    "category": vw.category.value,
                    "ttl_hours": round(vw.time_to_live_hours, 2),
                    "is_expired": vw.is_expired,
                }
                for mid, vw in self.validity_windows.items()
            },
            "source_chains": self.source_chains,
            "retrieval_latency_ms": self.retrieval_latency_ms,
            "version": self.version,
        }


# ── 核心类 ─────────────────────────────────────────────────────────

class ConfidenceScorer:
    """置信度评分器

    为每条记忆检索结果分配四维置信度分数，加权聚合为总体置信度。
    """

    # 来源权威性基础分映射
    AUTHORITY_BASE: Dict[SourceType, float] = {
        SourceType.OFFICIAL_DOCUMENT: 0.95,
        SourceType.PEER_REVIEWED: 0.90,
        SourceType.VERIFIED_DATABASE: 0.85,
        SourceType.USER_CONFIRMED: 0.75,
        SourceType.LLM_GENERATED: 0.50,
        SourceType.UNVERIFIED: 0.25,
        SourceType.ANONYMOUS: 0.10,
    }

    def __init__(self):
        self._lock = threading.RLock()
        self._score_history: List[ConfidenceScore] = []
        self._scored_count: int = 0

    def score(
        self,
        source_type: SourceType,
        citation_count: int = 0,
        citation_agreement: float = 0.5,
        created_at: Optional[float] = None,
        validity_category: ValidityCategory = ValidityCategory.GENERAL_KNOWLEDGE,
        semantic_similarity: float = 0.5,
    ) -> ConfidenceScore:
        """对单条记忆计算四维置信度。

        Args:
            source_type: 来源类型
            citation_count: 引用次数
            citation_agreement: 引用一致性 (0~1)
            created_at: 创建时间戳
            validity_category: 时效类别
            semantic_similarity: 语义相似度 (0~1)
        """
        with self._lock:
            # 1. 来源权威性
            authority = self.AUTHORITY_BASE.get(source_type, 0.1)
            authority = np.clip(authority, 0.0, 1.0)

            # 2. 引用一致性：基于引用次数和一致性
            citation_weight = min(citation_count / 10.0, 1.0)
            consistency = citation_agreement * citation_weight
            consistency = np.clip(consistency, 0.0, 1.0)

            # 3. 时间新鲜度：基于有效期剩余
            now = time.time()
            window = ValidityWindow.for_category(validity_category, created_at)
            if not window.is_expired:
                total_ttl = window.expires_at - window.created_at
                remaining = window.expires_at - now
                freshness = np.clip(remaining / max(total_ttl, 1.0), 0.0, 1.0)
            else:
                # 过期后指数衰减
                hours_since_expiry = (now - window.expires_at) / 3600.0
                freshness = np.exp(-0.05 * hours_since_expiry)

            # 4. 语义匹配度
            semantic = np.clip(semantic_similarity, 0.0, 1.0)

            score = ConfidenceScore(
                source_authority=round(authority, 4),
                citation_consistency=round(consistency, 4),
                temporal_freshness=round(freshness, 4),
                semantic_match=round(semantic, 4),
            )
            self._score_history.append(score)
            self._scored_count += 1
            return score

    def score_batch(
        self,
        facts: List[MemoryFact],
        semantic_scores: Optional[List[float]] = None,
    ) -> List[ConfidenceScore]:
        """批量评分。"""
        scores = []
        for i, fact in enumerate(facts):
            sem = semantic_scores[i] if semantic_scores else 0.5
            s = self.score(
                source_type=fact.source_type,
                citation_agreement=fact.confidence.citation_consistency,
                created_at=fact.validity.created_at if fact.validity else None,
                validity_category=fact.validity.category if fact.validity else ValidityCategory.GENERAL_KNOWLEDGE,
                semantic_similarity=sem,
            )
            scores.append(s)
            fact.confidence = s
        return scores

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            if self._score_history:
                avg_overall = float(np.mean([s.overall for s in self._score_history[-100:]]))
            else:
                avg_overall = 0.0
            return {
                "scored_count": self._scored_count,
                "recent_avg_overall": round(avg_overall, 4),
            }


class FactValidityWindow:
    """事实有效期窗口管理器

    核心机制：
      - 金融数据/h 级、法规/月级、个人偏好/周级
      - 过期自动降权（指数衰减）
      - 支持批量校验与自动废弃
    """

    DECAY_RATES = {
        FreshnessTier.STALE: 0.7,       # 刚过期：保留 70% 权重
        FreshnessTier.EXPIRED: 0.3,      # 严重过期：保留 30% 权重
        FreshnessTier.DEPRECATED: 0.05,  # 废弃：保留 5% 权重
    }

    def __init__(self):
        self._lock = threading.RLock()
        self._windows: Dict[str, ValidityWindow] = {}  # fact_id -> window
        self._expired_ids: Set[str] = set()

    def register(
        self,
        fact_id: str,
        category: ValidityCategory,
        created_at: Optional[float] = None,
    ) -> ValidityWindow:
        """注册有效期窗口。"""
        with self._lock:
            window = ValidityWindow.for_category(category, created_at)
            self._windows[fact_id] = window
            return window

    def check(self, fact_id: str) -> Tuple[FreshnessTier, float]:
        """检查记忆新鲜度，返回 (等级, 降权系数)。

        过期越久，衰减越大。
        """
        with self._lock:
            window = self._windows.get(fact_id)
            if window is None:
                return (FreshnessTier.FRESH, 1.0)

            if not window.is_expired:
                return (FreshnessTier.FRESH, 1.0)

            self._expired_ids.add(fact_id)
            hours_past = (time.time() - window.expires_at) / 3600.0

            if hours_past < 24:
                tier = FreshnessTier.STALE
            elif hours_past < 720:  # 30 天
                tier = FreshnessTier.EXPIRED
            else:
                tier = FreshnessTier.DEPRECATED

            weight = self.DECAY_RATES[tier] * np.exp(-0.01 * max(hours_past - 24, 0))
            return (tier, round(weight, 4))

    def check_batch(self, fact_ids: List[str]) -> Dict[str, Tuple[FreshnessTier, float]]:
        """批量新鲜度检查。"""
        return {fid: self.check(fid) for fid in fact_ids}

    def extend(self, fact_id: str, hours: float) -> None:
        """延长有效期。"""
        with self._lock:
            if fact_id in self._windows:
                self._windows[fact_id].expires_at += hours * 3600
                self._windows[fact_id].last_validated_at = time.time()
                self._expired_ids.discard(fact_id)

    def purge_expired(self) -> List[str]:
        """清除严重过期的窗口（返回已清除 ID 列表）。"""
        with self._lock:
            to_remove = [
                fid for fid, w in self._windows.items()
                if w.is_expired and (time.time() - w.expires_at) > 90 * 86400
            ]
            for fid in to_remove:
                del self._windows[fid]
            return to_remove

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            total = len(self._windows)
            active = sum(1 for w in self._windows.values() if not w.is_expired)
            return {
                "total_windows": total,
                "active_windows": active,
                "expired_windows": total - active,
                "expired_ids_count": len(self._expired_ids),
                "category_distribution": {
                    cat.value: sum(1 for w in self._windows.values() if w.category == cat)
                    for cat in ValidityCategory
                },
            }


class ContradictionResolver:
    """记忆矛盾自动裁决器

    裁决策略：
      1. 置信度对比：置信度高的优先
      2. 时效性对比：较新的优先
      3. 来源权威性对比：官方 > 用户确认 > LLM 生成
      4. 平局时标记人工审核
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._resolution_log: List[Dict[str, Any]] = []

    def resolve(
        self, fact_a: MemoryFact, fact_b: MemoryFact
    ) -> ContradictionVerdict:
        """裁决两条矛盾记忆，返回裁决结果。"""
        with self._lock:
            ca = fact_a.confidence.overall
            cb = fact_b.confidence.overall

            # 1. 置信度差距显著 → 高置信度获胜
            if abs(ca - cb) > 0.2:
                verdict = ContradictionVerdict.PREFER_A if ca > cb else ContradictionVerdict.PREFER_B
            else:
                # 2. 置信度接近 → 对比时效性
                va = fact_a.validity
                vb = fact_b.validity
                ta = va.created_at if va else 0.0
                tb = vb.created_at if vb else 0.0

                if abs(ta - tb) > 86400:  # 超过一天差距
                    verdict = ContradictionVerdict.PREFER_A if ta > tb else ContradictionVerdict.PREFER_B
                else:
                    # 3. 来源权威性对比
                    sa = ConfidenceScorer.AUTHORITY_BASE.get(fact_a.source_type, 0.0)
                    sb = ConfidenceScorer.AUTHORITY_BASE.get(fact_b.source_type, 0.0)

                    if abs(sa - sb) > 0.1:
                        verdict = ContradictionVerdict.PREFER_A if sa > sb else ContradictionVerdict.PREFER_B
                    else:
                        # 4. 内容相似度判决 → 可合并
                        content_similarity = self._compute_text_similarity(
                            fact_a.content, fact_b.content
                        )
                        if content_similarity > 0.7:
                            verdict = ContradictionVerdict.MERGE
                        else:
                            verdict = ContradictionVerdict.FLAG_FOR_REVIEW

            self._log_resolution(fact_a, fact_b, verdict)
            return verdict

    def resolve_batch(
        self, conflicts: List[Tuple[MemoryFact, MemoryFact]]
    ) -> List[Dict[str, Any]]:
        """批量裁决。"""
        results = []
        for a, b in conflicts:
            verdict = self.resolve(a, b)
            results.append({
                "fact_a_id": a.fact_id,
                "fact_b_id": b.fact_id,
                "verdict": verdict.value,
            })
        return results

    def _compute_text_similarity(self, text_a: str, text_b: str) -> float:
        """简单文本相似度（Jaccard token overlap）。"""
        tokens_a = set(text_a.lower().split())
        tokens_b = set(text_b.lower().split())
        if not tokens_a or not tokens_b:
            return 0.0
        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b
        return len(intersection) / len(union)

    def _log_resolution(
        self, fact_a: MemoryFact, fact_b: MemoryFact, verdict: ContradictionVerdict
    ) -> None:
        self._resolution_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "fact_a_id": fact_a.fact_id,
            "fact_b_id": fact_b.fact_id,
            "verdict": verdict.value,
            "confidence_a": fact_a.confidence.to_dict(),
            "confidence_b": fact_b.confidence.to_dict(),
        })

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            verdict_counts = defaultdict(int)
            for entry in self._resolution_log:
                verdict_counts[entry["verdict"]] += 1
            return {
                "total_resolutions": len(self._resolution_log),
                "verdict_distribution": dict(verdict_counts),
            }


class AuditableRecallReceipt:
    """可审计召回回执生成器

    每次召回生成包含以下内容的可审计回执：
      - 检索时间 / 查询意图
      - 命中记忆 ID 列表
      - 每条记忆的四维置信度
      - 有效期状态
      - 完整来源链
    """

    def __init__(self, confidence_scorer: Optional[ConfidenceScorer] = None):
        self._lock = threading.RLock()
        self._scorer = confidence_scorer or ConfidenceScorer()
        self._receipts: List[AuditableRecallReceipt] = []

    def generate(
        self,
        query_intent: str,
        hit_facts: List[MemoryFact],
        latency_ms: float = 0.0,
    ) -> AuditableRecallReceipt:
        """生成一条可审计回执。"""
        with self._lock:
            receipt = AuditableRecallReceipt(
                receipt_id=str(uuid.uuid4())[:16],
                timestamp=datetime.now(timezone.utc).isoformat(),
                query_intent=query_intent,
                hit_memory_ids=[f.fact_id for f in hit_facts],
                confidence_scores={f.fact_id: f.confidence for f in hit_facts},
                validity_windows={
                    f.fact_id: f.validity for f in hit_facts if f.validity
                },
                source_chains={f.fact_id: f.source_chain for f in hit_facts},
                retrieval_latency_ms=latency_ms,
            )
            self._receipts.append(receipt)
            return receipt

    def get_receipt(self, receipt_id: str) -> Optional[AuditableRecallReceipt]:
        """按 ID 查询历史回执。"""
        with self._lock:
            for r in self._receipts:
                if r.receipt_id == receipt_id:
                    return r
            return None

    def export_receipts(self, limit: int = 100) -> List[Dict[str, Any]]:
        """导出最近回执列表。"""
        with self._lock:
            return [r.to_dict() for r in self._receipts[-limit:]]

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            avg_hits = 0.0
            if self._receipts:
                avg_hits = float(np.mean([
                    len(r.hit_memory_ids) for r in self._receipts
                ]))
            return {
                "total_receipts": len(self._receipts),
                "avg_hits_per_recall": round(avg_hits, 2),
                "scorer_stats": self._scorer.statistics(),
            }
