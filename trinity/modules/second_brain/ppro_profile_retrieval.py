"""
PPRO — User Profile-Guided Personalized Retrieval Optimization
================================================================
arXiv 2607.00017 · P49-3

用户画像引导的个性化检索优化：自动提炼用户画像三元组，
画像条件化检索重排 + GRPO 查询重写 + 个性化召回评分。

设计要点:
  - UserProfileDeriver: 自动提炼用户画像
  - ProfileConditionedRanker: 画像条件化重排
  - QueryRewriterGRPO: GRPO 查询重写器
  - PersonalizedRecallScorer: 个性化召回评分
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ProfileTriple:
    """用户画像三元组——属性/偏好/关系。"""
    subject: str
    predicate: str
    obj: str
    confidence: float = 0.5
    source: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class UserProfile:
    """聚合用户画像。"""
    user_id: str = ""
    attributes: Dict[str, str] = field(default_factory=dict)
    preferences: Dict[str, float] = field(default_factory=dict)
    relations: List[ProfileTriple] = field(default_factory=list)
    embedding: Optional[np.ndarray] = None


@dataclass
class RetrievalResult:
    """单条检索结果。"""
    doc_id: str
    content: str = ""
    base_score: float = 0.0
    personalized_score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# UserProfileDeriver
# ---------------------------------------------------------------------------

class UserProfileDeriver:
    """从累积记忆中自动提炼用户画像——属性、偏好、关系三元组。

    通过启发式规则从记忆文本中提取结构化画像信息。
    """

    _ATTRIBUTE_PATTERNS = [
        ("role is", "role"), ("occupation", "occupation"),
        ("prefer", "preference"), ("use ", "tool"),
        ("work on", "project"), ("skill", "skill"),
    ]

    def __init__(self) -> None:
        self._profiles: Dict[str, UserProfile] = {}
        self._lock = threading.RLock()

    def derive(self, user_id: str, memories: List[str]) -> UserProfile:
        """从记忆列表中提炼用户画像。"""
        with self._lock:
            profile = self._profiles.get(user_id, UserProfile(user_id=user_id))
            attrs = profile.attributes
            prefs = profile.preferences

            for mem in memories:
                mem_lower = mem.lower()
                for pattern, key in self._ATTRIBUTE_PATTERNS:
                    idx = mem_lower.find(pattern)
                    if idx >= 0:
                        value = mem[idx + len(pattern):].strip()[:50]
                        if " " in value:
                            value = value.split()[0]
                        if key in {"preference", "role", "occupation"}:
                            attrs[key] = value
                        else:
                            prefs[value] = prefs.get(value, 0.0) + 1.0

                # 抽取关系三元组
                for sep in (" -> ", " likes ", " uses ", " works with "):
                    if sep in mem:
                        parts = mem.split(sep, 1)
                        triple = ProfileTriple(
                            subject=user_id,
                            predicate=sep.strip(),
                            obj=parts[1][:60],
                            confidence=0.6,
                            source="memory",
                        )
                        profile.relations.append(triple)

            # 归一化偏好
            if prefs:
                total = sum(prefs.values())
                for k in prefs:
                    prefs[k] /= total

            self._profiles[user_id] = profile
            return profile

    def get_profile(self, user_id: str) -> Optional[UserProfile]:
        with self._lock:
            return self._profiles.get(user_id)

    def statistics(self) -> Dict[str, Any]:
        return {"users": len(self._profiles)}


# ---------------------------------------------------------------------------
# ProfileConditionedRanker
# ---------------------------------------------------------------------------

class ProfileConditionedRanker:
    """画像条件化检索重排——profile 作为先验注入排序。

    排序分数 = α * base_score + β * profile_match_score
    """

    def __init__(self, alpha: float = 0.6, beta: float = 0.4) -> None:
        self.alpha = alpha
        self.beta = beta
        self._lock = threading.RLock()

    def rerank(
        self, results: List[RetrievalResult], profile: UserProfile,
    ) -> List[RetrievalResult]:
        """画像条件化重排。"""
        with self._lock:
            for r in results:
                profile_score = self._compute_profile_match(r, profile)
                r.personalized_score = self.alpha * r.base_score + self.beta * profile_score

            ranked = sorted(results, key=lambda r: r.personalized_score, reverse=True)
            return ranked

    def _compute_profile_match(self, result: RetrievalResult, profile: UserProfile) -> float:
        score = 0.0
        content_lower = result.content.lower()

        # 属性匹配
        for attr_val in profile.attributes.values():
            if attr_val.lower() in content_lower:
                score += 0.15

        # 偏好匹配
        for pref_val in profile.preferences:
            if pref_val.lower() in content_lower:
                score += profile.preferences[pref_val] * 0.2

        # 关系匹配
        for rel in profile.relations:
            if rel.obj.lower() in content_lower:
                score += rel.confidence * 0.1

        return min(1.0, score)

    def statistics(self) -> Dict[str, Any]:
        return {"alpha": self.alpha, "beta": self.beta}


# ---------------------------------------------------------------------------
# QueryRewriterGRPO
# ---------------------------------------------------------------------------

class QueryRewriterGRPO:
    """GRPO 优化的查询重写器——根据检索质量+下游答案质量联合优化。

    维护多候选重写模板，通过 GRPO 步骤在线调整模板选择权重。
    """

    _TEMPLATES = [
        "{query}",                                             # 原始
        "{query} for user with preferences: {profile}",        # 画像注入
        "{query} in context of {preferences}",                 # 偏好上下文
        "personalized: {query} given {attributes}",            # 属性注入
        "{query} (user likes {likes})",                       # 偏好提示
    ]

    def __init__(self) -> None:
        self._weights = np.ones(len(self._TEMPLATES), dtype=np.float32) / len(self._TEMPLATES)
        self._usage: Dict[int, int] = defaultdict(int)
        self._rewards: Dict[int, List[float]] = defaultdict(list)
        self._lock = threading.RLock()

    def rewrite(self, query: str, profile: UserProfile) -> Tuple[str, int]:
        """按当前策略权重选择重写模板。"""
        with self._lock:
            probs = self._weights / self._weights.sum()
            tpl_idx = int(np.random.choice(len(self._TEMPLATES), p=probs))
            self._usage[tpl_idx] += 1

            likes = list(profile.preferences.keys())[:3] if profile.preferences else ["general"]
            attrs = {k: v for k, v in profile.attributes.items()}

            rewritten = self._TEMPLATES[tpl_idx].format(
                query=query,
                profile=", ".join(f"{k}:{v}" for k, v in attrs.items()),
                preferences=", ".join(likes),
                attributes=", ".join(f"{k}={v}" for k, v in attrs.items()),
                likes=", ".join(likes),
            )
            return rewritten, tpl_idx

    def update(
        self, template_idx: int, retrieval_quality: float, answer_quality: float,
        lr: float = 0.1,
    ) -> None:
        """GRPO 步骤更新：联合优化检索质量 + 答案质量。"""
        with self._lock:
            reward = 0.5 * retrieval_quality + 0.5 * answer_quality
            self._rewards[template_idx].append(reward)

            # 策略梯度更新
            advantage = reward - 0.5
            grad = np.zeros_like(self._weights)
            grad[template_idx] = advantage
            self._weights += lr * grad
            self._weights = np.maximum(0.01, self._weights)
            self._weights /= self._weights.sum()

    def statistics(self) -> Dict[str, Any]:
        return {
            "template_weights": {i: round(float(w), 3) for i, w in enumerate(self._weights)},
            "usage": dict(self._usage),
        }


# ---------------------------------------------------------------------------
# PersonalizedRecallScorer
# ---------------------------------------------------------------------------

class PersonalizedRecallScorer:
    """个性化召回评分器——评估检索结果的用户相关性。

    评分维度: 内容匹配度、偏好对齐度、历史采纳率。
    """

    def __init__(self) -> None:
        self._score_history: List[Dict[str, float]] = []
        self._lock = threading.RLock()

    def score(
        self, results: List[RetrievalResult], profile: UserProfile,
        feedback_history: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """批量评分。"""
        with self._lock:
            scores = {}

            for r in results:
                content_score = self._content_affinity(r, profile)
                preference_score = self._preference_alignment(r, profile)
                history_score = self._history_adoption(r, feedback_history or [])

                composite = 0.4 * content_score + 0.4 * preference_score + 0.2 * history_score
                scores[r.doc_id] = round(composite, 4)

            return {
                "per_doc": scores,
                "mean": round(float(np.mean(list(scores.values()))) if scores else 0.0, 4),
                "count": len(scores),
            }

    def _content_affinity(self, result: RetrievalResult, profile: UserProfile) -> float:
        text = result.content.lower()
        matches = sum(1 for v in profile.attributes.values() if v.lower() in text)
        return min(1.0, matches * 0.25)

    def _preference_alignment(self, result: RetrievalResult, profile: UserProfile) -> float:
        text = result.content.lower()
        score = sum(profile.preferences.get(w, 0.0) for w in text.split() if w in profile.preferences)
        return min(1.0, score)

    @staticmethod
    def _history_adoption(result: RetrievalResult, feedback: List[Dict[str, Any]]) -> float:
        if not feedback:
            return 0.5
        relevant = [f for f in feedback if f.get("doc_id") == result.doc_id]
        if not relevant:
            return 0.3
        adopted = sum(1 for f in relevant if f.get("adopted", False))
        return adopted / len(relevant)

    def statistics(self) -> Dict[str, Any]:
        return {"scored_batches": len(self._score_history)}
