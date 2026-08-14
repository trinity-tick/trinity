"""
P7-1: Idle-Time Proactive Anticipation Engine (对标 ProAct)
=============================================================

核心设计（基于 ProAct 论文 arXiv:2605.25971）：
  - 需求预测器（NeedPredictor）：分析对话历史+持久记忆，基于历史模式推断下一步需求
  - 信息预获取（InformationPreFetcher）：Agent 空闲时后台静默搜索/检索，提前消解知识缺口
  - 预判结果缓存（PrejudgmentCache）：缓存预测结果，减少重复计算

ProActEval 评测框架：
  - 200 场景 × 40 领域
  - 可预测需求链（predictable need chains）
  - 多样化用户认知画像（diverse user cognitive profiles）

关键指标：
  - 任务轮次减少 14.8%
  - 用户努力降低 11.7%
  - 幻觉率下降 28.1%

Reference: Hu et al., "Anticipate and Learn: Unleashing Idle-Time Compute
           in Proactive Agents", arXiv:2605.25971, 2026.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections import defaultdict, deque, OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)

# ── 预判来源与状态 ────────────────────────────────────────────────────


class PredictionSource(Enum):
    """需求预测来源。"""
    DIALOGUE_PATTERN = "dialogue_pattern"       # 对话模式推断
    TOPIC_TRANSITION = "topic_transition"        # 话题转移预测
    TASK_DEPENDENCY = "task_dependency"          # 任务依赖链
    USER_PROFILE = "user_profile"                # 用户画像匹配
    CONTEXT_GAP = "context_gap"                  # 上下文缺口检测
    EXTERNAL_EVENT = "external_event"            # 外部事件触发


class PredictionState(Enum):
    """预测状态。"""
    PENDING = "pending"          # 待处理（已预测、未消解）
    PRE_FETCHING = "pre_fetching"  # 预获取中
    RESOLVED = "resolved"        # 已消解（信息已就绪）
    CANCELLED = "cancelled"      # 已取消（不再需要）
    EXPIRED = "expired"          # 已过期（超时）


class PreFetchStatus(Enum):
    """预获取状态。"""
    IDLE = "idle"               # 空闲
    SEARCHING = "searching"     # 搜索中
    RETRIEVING = "retrieving"   # 检索中
    SYNTHESIZING = "synthesizing"  # 合成中
    COMPLETED = "completed"     # 完成
    FAILED = "failed"           # 失败


class CacheStrategy(Enum):
    """缓存策略。"""
    LRU = "lru"                         # 最近最少使用
    IMPORTANCE_WEIGHTED = "importance_weighted"  # 重要性加权
    TIME_DECAY = "time_decay"           # 时间衰减
    HYBRID = "hybrid"                   # 混合策略


# ── 数据结构 ──────────────────────────────────────────────────────────


@dataclass
class PredictedNeed:
    """预测的下一需求。

    Args:
        need_id: 唯一标识
        description: 需求描述
        source: 预测来源
        confidence: 预测置信度 [0,1]
        prerequisite_knowledge: 前提知识域列表
        estimated_urgency: 预估紧急度 [0,1]
        dependency_chain: 依赖链（前置预测ID列表）
        predicted_at: 预测时间
        expires_at: 过期时间
        resolved_at: 消解时间
    """
    need_id: str = field(default_factory=lambda: f"pnd_{uuid.uuid4().hex[:12]}")
    description: str = ""
    source: PredictionSource = PredictionSource.DIALOGUE_PATTERN
    confidence: float = 0.5
    prerequisite_knowledge: List[str] = field(default_factory=list)
    estimated_urgency: float = 0.5
    dependency_chain: List[str] = field(default_factory=list)
    predicted_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 86400)
    resolved_at: Optional[float] = None


@dataclass
class PreFetchResult:
    """预获取结果。

    Args:
        fetch_id: 唯一标识
        need_id: 关联的需求ID
        query: 原始查询
        retrieved_documents: 检索到的文档摘要列表
        knowledge_gap_resolved: 已消解的知识缺口
        confidence_gain: 置信度提升
        fetch_latency_s: 预获取耗时（秒）
        cache_key: 缓存键
    """
    fetch_id: str = field(default_factory=lambda: f"pf_{uuid.uuid4().hex[:12]}")
    need_id: str = ""
    query: str = ""
    retrieved_documents: List[Dict[str, str]] = field(default_factory=list)
    knowledge_gap_resolved: bool = False
    confidence_gain: float = 0.0
    fetch_latency_s: float = 0.0
    cache_key: str = ""


@dataclass
class NeedChain:
    """需求链：一组按依赖关系排列的预测需求。

    Args:
        chain_id: 唯一标识
        domain: 所属领域
        needs: 有序需求列表
        chain_confidence: 整体链置信度
        created_at: 创建时间
    """
    chain_id: str = field(default_factory=lambda: f"nch_{uuid.uuid4().hex[:12]}")
    domain: str = ""
    needs: List[PredictedNeed] = field(default_factory=list)
    chain_confidence: float = 0.5
    created_at: float = field(default_factory=time.time)


@dataclass
class UserCognitiveProfile:
    """用户认知画像（ProActEval 特征）。

    Args:
        profile_id: 唯一标识
        preferred_depth: 信息偏好深度（shallow/moderate/deep）
        expertise_domains: 专业领域列表
        query_style: 查询风格（concise/detailed/exploratory）
        recall_preference: 记忆回溯偏好（近期/远期/综合）
        interaction_frequency: 交互频率估算
        recent_topics: 近期话题列表
    """
    profile_id: str = field(default_factory=lambda: f"ucp_{uuid.uuid4().hex[:12]}")
    preferred_depth: str = "moderate"
    expertise_domains: List[str] = field(default_factory=list)
    query_style: str = "detailed"
    recall_preference: str = "balanced"
    interaction_frequency: float = 1.0
    recent_topics: List[str] = field(default_factory=list)


@dataclass
class AnticipatorStats:
    """预判引擎统计快照。"""
    total_predictions: int = 0
    active_predictions: int = 0
    resolved_predictions: int = 0
    expired_predictions: int = 0
    total_pre_fetches: int = 0
    successful_pre_fetches: int = 0
    avg_fetch_latency_s: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_size: int = 0
    total_knowledge_gaps_resolved: int = 0
    idle_cycles_utilized: int = 0


# ── 需求预测器 ────────────────────────────────────────────────────────


class NeedPredictor:
    """基于历史模式推断下一需求的需求预测器。

    分析对话历史 + 持久记忆，识别可预测需求链中的下一需求。
    支持 40 个领域的需求链建模。
    """

    DOMAIN_WEIGHTS: Dict[str, float] = {
        "programming": 0.92,
        "data_analysis": 0.88,
        "writing": 0.85,
        "research": 0.90,
        "planning": 0.83,
        "debugging": 0.91,
        "documentation": 0.80,
        "design": 0.78,
    }

    def __init__(
        self,
        history_window: int = 50,
        min_confidence: float = 0.3,
        domain_weight_decay: float = 0.95,
    ):
        self.history_window = history_window
        self.min_confidence = min_confidence
        self.domain_weight_decay = domain_weight_decay

        self._domain_transition: Dict[str, Dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._topic_cooccurrence: Dict[str, Dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self._interaction_count: int = 0
        self._lock = threading.RLock()

    def observe_turn(
        self,
        current_topic: str,
        user_intent: str,
        previous_topic: Optional[str] = None,
        context_keywords: Optional[List[str]] = None,
    ) -> None:
        """记录一轮对话，更新领域转移矩阵。

        Args:
            current_topic: 当前话题/领域
            user_intent: 用户意图描述
            previous_topic: 前一轮话题
            context_keywords: 上下文关键词
        """
        with self._lock:
            self._interaction_count += 1
            if previous_topic and current_topic:
                self._domain_transition[previous_topic][current_topic] += 1

            if context_keywords:
                for kw in context_keywords:
                    self._topic_cooccurrence[current_topic][kw] = (
                        self._topic_cooccurrence[current_topic].get(kw, 0.0)
                        * 0.9 + 0.1
                    )

    def predict_next_needs(
        self,
        current_topic: str,
        recent_dialogue: Optional[List[Dict[str, Any]]] = None,
        top_k: int = 5,
    ) -> List[PredictedNeed]:
        """基于当前话题和近期对话预测下一需求。

        Args:
            current_topic: 当前话题
            recent_dialogue: 近期对话记录
            top_k: 返回前K个预测

        Returns:
            预测需求列表（按置信度降序）
        """
        with self._lock:
            transitions = self._domain_transition.get(current_topic, {})
            if not transitions:
                return []

            total = sum(transitions.values())
            results: List[PredictedNeed] = []

            for next_topic, count in transitions.items():
                base = count / max(total, 1)
                domain_w = self.DOMAIN_WEIGHTS.get(next_topic, 0.7)
                confidence = base * domain_w

                if confidence < self.min_confidence:
                    continue

                if recent_dialogue:
                    for turn in recent_dialogue[-5:]:
                        text = turn.get("content", "")
                        keywords = self._topic_cooccurrence.get(
                            next_topic, {}
                        )
                        for kw, weight in keywords.items():
                            if kw.lower() in text.lower():
                                confidence += 0.05 * weight

                confidence = min(confidence, 1.0)

                need = PredictedNeed(
                    description=f"User may need assistance with: {next_topic}",
                    source=PredictionSource.TOPIC_TRANSITION,
                    confidence=round(confidence, 4),
                    prerequisite_knowledge=[next_topic],
                    estimated_urgency=round(confidence * 0.8, 4),
                )
                results.append(need)

            results.sort(key=lambda n: n.confidence, reverse=True)
            return results[:top_k]

    def build_need_chain(
        self, seed_topic: str, max_depth: int = 4,
    ) -> Optional[NeedChain]:
        """构建从 seed_topic 出发的需求链。

        Args:
            seed_topic: 起始话题
            max_depth: 最大链深度

        Returns:
            NeedChain 或 None
        """
        with self._lock:
            needs: List[PredictedNeed] = []
            current = seed_topic
            visited: Set[str] = set()
            confidence = 1.0

            for _ in range(max_depth):
                if current in visited:
                    break
                visited.add(current)

                predictions = self.predict_next_needs(current, top_k=1)
                if not predictions:
                    break

                n = predictions[0]
                n.dependency_chain = [p.need_id for p in needs]
                needs.append(n)
                confidence *= n.confidence
                current = n.prerequisite_knowledge[0] if n.prerequisite_knowledge else ""

            if not needs:
                return None

            domain = seed_topic
            return NeedChain(
                domain=domain,
                needs=needs,
                chain_confidence=round(confidence, 4),
            )

    def statistics(self) -> Dict[str, Any]:
        """返回预测器运行时指标。"""
        with self._lock:
            return {
                "interactions_observed": self._interaction_count,
                "domain_transitions_tracked": sum(
                    len(v) for v in self._domain_transition.values()
                ),
                "unique_domains": len(self._domain_transition),
                "history_window": self.history_window,
                "min_confidence": self.min_confidence,
                "domain_weight_decay": self.domain_weight_decay,
            }


# ── 信息预获取器 ──────────────────────────────────────────────────────


class InformationPreFetcher:
    """Agent 空闲时后台静默搜索/检索，提前消解知识缺口。

    利用空闲算力（idle-time compute）对已预测需求进行信息预获取。
    支持多种检索后端：内部存储 / 向量索引 / 外部 API。
    """

    def __init__(
        self,
        max_concurrent_fetches: int = 3,
        fetch_timeout_s: float = 30.0,
        retry_limit: int = 2,
    ):
        self.max_concurrent_fetches = max_concurrent_fetches
        self.fetch_timeout_s = fetch_timeout_s
        self.retry_limit = retry_limit

        self._active_fetches: Dict[str, PreFetchResult] = {}
        self._completed_fetches: deque = deque(maxlen=500)
        self._idle_cycles: int = 0
        self._lock = threading.RLock()

        logger.info(
            "InformationPreFetcher initialized (max_concurrent=%d, timeout=%.1fs)",
            max_concurrent_fetches,
            fetch_timeout_s,
        )

    def fetch_for_need(
        self,
        need: PredictedNeed,
        knowledge_base: Optional[Dict[str, Any]] = None,
        on_complete: Optional[Callable[[PreFetchResult], None]] = None,
    ) -> Optional[PreFetchResult]:
        """为预测需求执行后台信息预获取。

        Args:
            need: 预测需求
            knowledge_base: 可用的知识库（可选）
            on_complete: 完成回调（可选）

        Returns:
            PreFetchResult 或 None（若并发数已满）
        """
        with self._lock:
            if len(self._active_fetches) >= self.max_concurrent_fetches:
                return None

            self._idle_cycles += 1

        fetch_id = f"pf_{uuid.uuid4().hex[:12]}"
        cache_key = hashlib.sha256(
            f"{need.description}:{','.join(need.prerequisite_knowledge)}".encode()
        ).hexdigest()[:16]

        t_start = time.time()

        # 模拟信息检索（实际部署时替换为真实检索后端）
        docs: List[Dict[str, str]] = []
        knowledge_gap = False

        # 内部知识库检索
        if knowledge_base:
            for domain in need.prerequisite_knowledge:
                if domain in knowledge_base:
                    kb_entry = knowledge_base[domain]
                    docs.append({
                        "source": "internal_kb",
                        "domain": domain,
                        "content": str(kb_entry)[:500],
                    })
                else:
                    knowledge_gap = True

        confidence_gain = 0.15 if docs else 0.0

        latency = time.time() - t_start
        result = PreFetchResult(
            fetch_id=fetch_id,
            need_id=need.need_id,
            query=need.description,
            retrieved_documents=docs,
            knowledge_gap_resolved=bool(docs) and not knowledge_gap,
            confidence_gain=confidence_gain,
            fetch_latency_s=round(latency, 4),
            cache_key=cache_key,
        )

        with self._lock:
            self._completed_fetches.append(result)
            if need.need_id in self._active_fetches:
                del self._active_fetches[need.need_id]

        if on_complete:
            try:
                on_complete(result)
            except Exception as exc:
                logger.warning("PreFetch callback failed: %s", exc)

        return result

    def enqueue_background_fetch(
        self,
        needs: List[PredictedNeed],
        knowledge_base: Optional[Dict[str, Any]] = None,
    ) -> int:
        """批量入队后台预获取任务。

        Args:
            needs: 预测需求列表
            knowledge_base: 知识库

        Returns:
            成功入队的任务数
        """
        count = 0
        for need in sorted(needs, key=lambda n: -n.estimated_urgency):
            result = self.fetch_for_need(need, knowledge_base)
            if result is not None:
                count += 1
        return count

    @property
    def idle_cycles(self) -> int:
        """返回累计空闲周期数。"""
        return self._idle_cycles

    def statistics(self) -> Dict[str, Any]:
        """返回预获取器运行时指标。"""
        with self._lock:
            total = len(self._completed_fetches)
            successful = sum(
                1 for f in self._completed_fetches if f.knowledge_gap_resolved
            )
            latencies = [
                f.fetch_latency_s
                for f in self._completed_fetches
                if f.fetch_latency_s > 0
            ]
            return {
                "active_fetches": len(self._active_fetches),
                "completed_fetches": total,
                "successful_fetches": successful,
                "success_rate": round(successful / max(total, 1), 4),
                "avg_latency_s": round(
                    sum(latencies) / max(len(latencies), 1), 4
                ),
                "idle_cycles_utilized": self._idle_cycles,
                "max_concurrent": self.max_concurrent_fetches,
            }


# ── 预判结果缓存 ──────────────────────────────────────────────────────


class PrejudgmentCache:
    """预测结果缓存，避免重复预判和预获取。

    支持 LRU / 重要性加权 / 时间衰减 / 混合四种淘汰策略。
    """

    def __init__(
        self,
        max_cache_size: int = 200,
        strategy: CacheStrategy = CacheStrategy.HYBRID,
        ttl_seconds: float = 3600.0,
    ):
        self.max_cache_size = max_cache_size
        self.strategy = strategy
        self.ttl_seconds = ttl_seconds

        self._cache: OrderedDict[str, Tuple[PredictedNeed, PreFetchResult, float]] = (
            OrderedDict()
        )
        self._hits: int = 0
        self._misses: int = 0
        self._lock = threading.RLock()

    def get(
        self, cache_key: str,
    ) -> Optional[Tuple[PredictedNeed, PreFetchResult]]:
        """从缓存获取预判结果。

        Args:
            cache_key: 缓存键

        Returns:
            (PredictedNeed, PreFetchResult) 或 None
        """
        with self._lock:
            if cache_key not in self._cache:
                self._misses += 1
                return None

            need, result, cached_at = self._cache[cache_key]

            # TTL 检查
            if time.time() - cached_at > self.ttl_seconds:
                del self._cache[cache_key]
                self._misses += 1
                return None

            # LRU: 移到末尾
            self._cache.move_to_end(cache_key)
            self._hits += 1
            return (need, result)

    def put(
        self,
        cache_key: str,
        need: PredictedNeed,
        result: PreFetchResult,
    ) -> None:
        """将预判结果存入缓存。

        Args:
            cache_key: 缓存键
            need: 预测需求
            result: 预获取结果
        """
        with self._lock:
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)

            self._cache[cache_key] = (need, result, time.time())

            if len(self._cache) > self.max_cache_size:
                self._evict()

    def _evict(self) -> None:
        """按当前策略淘汰一个条目。"""
        if not self._cache:
            return

        if self.strategy == CacheStrategy.LRU:
            self._cache.popitem(last=False)

        elif self.strategy == CacheStrategy.IMPORTANCE_WEIGHTED:
            min_key = min(
                self._cache.keys(),
                key=lambda k: self._cache[k][0].estimated_urgency,
                default=None,
            )
            if min_key:
                del self._cache[min_key]

        elif self.strategy == CacheStrategy.TIME_DECAY:
            now = time.time()
            min_key = min(
                self._cache.keys(),
                key=lambda k: now - self._cache[k][2],
                default=None,
            )
            if min_key:
                del self._cache[min_key]

        else:  # HYBRID: 兼顾时间和重要性
            now = time.time()
            best_key = min(
                self._cache.keys(),
                key=lambda k: (
                    self._cache[k][0].estimated_urgency
                    * np.exp(-0.001 * (now - self._cache[k][2]))
                ),
                default=None,
            )
            if best_key:
                del self._cache[best_key]

    def clear(self) -> None:
        """清空缓存。"""
        with self._lock:
            self._cache.clear()

    @property
    def hit_rate(self) -> float:
        """缓存命中率。"""
        total = self._hits + self._misses
        return self._hits / max(total, 1)

    def revalidate(self) -> int:
        """重新验证缓存，移除过期条目。"""
        with self._lock:
            now = time.time()
            expired = [
                k
                for k, (_, _, cached_at) in self._cache.items()
                if now - cached_at > self.ttl_seconds
            ]
            for k in expired:
                del self._cache[k]
            return len(expired)

    def statistics(self) -> Dict[str, Any]:
        """返回缓存运行时指标。"""
        with self._lock:
            return {
                "cache_size": len(self._cache),
                "max_size": self.max_cache_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self.hit_rate, 4),
                "strategy": self.strategy.value,
                "ttl_seconds": self.ttl_seconds,
            }


# ── 主动预判引擎 ──────────────────────────────────────────────────────


class ProactiveAnticipator:
    """ProAct 风格的空闲算力主动预判引擎。

    组合需求预测器、信息预获取器和预判结果缓存，
    在 Agent 空闲时间自动分析对话历史和持久记忆，
    预测未来需求链并提前消解知识缺口。

    ProActEval 设计：
      - 200 场景 × 40 领域评测
      - 可预测需求链 + 多样化用户认知画像
    """

    # 40 领域覆盖表（ProActEval 对齐）
    SUPPORTED_DOMAINS: Set[str] = {
        "programming", "data_science", "machine_learning", "web_development",
        "database", "devops", "cloud_computing", "cybersecurity", "mobile_dev",
        "game_development", "nlp", "computer_vision", "robotics", "iot",
        "blockchain", "quantum_computing", "scientific_computing", "mathematics",
        "statistics", "physics", "chemistry", "biology", "medicine", "finance",
        "economics", "business", "marketing", "design", "writing", "translation",
        "education", "legal", "history", "philosophy", "psychology", "sociology",
        "geography", "music", "art", "engineering",
    }

    def __init__(
        self,
        history_window: int = 50,
        min_confidence: float = 0.3,
        cache_size: int = 200,
        cache_strategy: CacheStrategy = CacheStrategy.HYBRID,
        prefetch_timeout_s: float = 30.0,
        auto_prefetch: bool = True,
        profiled_user_count: int = 3,
    ):
        self.predictor = NeedPredictor(
            history_window=history_window,
            min_confidence=min_confidence,
        )
        self.prefetcher = InformationPreFetcher(
            fetch_timeout_s=prefetch_timeout_s,
        )
        self.cache = PrejudgmentCache(
            max_cache_size=cache_size,
            strategy=cache_strategy,
        )

        self.auto_prefetch = auto_prefetch
        self.profiled_user_count = profiled_user_count

        # 用户认知画像存储
        self._profiles: Dict[str, UserCognitiveProfile] = {}
        self._active_needs: Dict[str, PredictedNeed] = {}
        self._resolved_needs: deque = deque(maxlen=1000)
        self._need_chains: Dict[str, NeedChain] = {}
        self._knowledge_base: Dict[str, Any] = {}

        self._lock = threading.RLock()

        logger.info(
            "ProactiveAnticipator initialized (window=%d, min_conf=%.2f, "
            "cache=%d, domains=%d)",
            history_window,
            min_confidence,
            cache_size,
            len(self.SUPPORTED_DOMAINS),
        )

    # ── 对话观察与记录 ────────────────────────────────────────────

    def observe_interaction(
        self,
        role: str,
        content: str,
        topic: Optional[str] = None,
        intent: Optional[str] = None,
    ) -> None:
        """观察一轮对话交互。

        Args:
            role: 角色（user/assistant/system）
            content: 对话内容
            topic: 话题/领域
            intent: 用户意图
        """
        if role != "user" or not content.strip():
            return

        detected_topic = topic or self._detect_topic(content)
        user_intent = intent or self._extract_intent(content)

        with self._lock:
            # 收集上下文关键词
            keywords = self._extract_keywords(content)
            self.predictor.observe_turn(
                current_topic=detected_topic,
                user_intent=user_intent,
                context_keywords=keywords,
            )

            # 若开启了自动预获取，则触发预测
            if self.auto_prefetch:
                needs = self.predictor.predict_next_needs(detected_topic)
                for need in needs:
                    self._active_needs[need.need_id] = need
                self.prefetcher.enqueue_background_fetch(
                    needs, self._knowledge_base
                )

    def idle_cycle(self) -> int:
        """执行一个空闲周期：后台分析 + 预获取。

        Returns:
            本周期完成的预获取数
        """
        count = 0
        with self._lock:
            # 对所有活跃预测执行预获取
            unresolved = [
                n
                for n in self._active_needs.values()
                if time.time() < n.expires_at
            ]
            unresolved.sort(key=lambda n: -n.estimated_urgency)

            count = self.prefetcher.enqueue_background_fetch(
                unresolved, self._knowledge_base
            )

        return count

    # ── 预测查询 ──────────────────────────────────────────────────

    def predict(
        self,
        current_topic: str,
        top_k: int = 5,
        use_cache: bool = True,
    ) -> List[PredictedNeed]:
        """预测下一步需求。

        Args:
            current_topic: 当前话题
            top_k: 返回数
            use_cache: 是否使用缓存

        Returns:
            预测需求列表
        """
        if use_cache:
            cache_key = hashlib.sha256(
                f"pred:{current_topic}:{top_k}".encode()
            ).hexdigest()[:16]
            cached = self.cache.get(cache_key)
            if cached:
                need, result = cached
                need.confidence *= (
                    1.0 + max(0, result.confidence_gain / 5)
                )
                return [need]

        with self._lock:
            needs = self.predictor.predict_next_needs(current_topic, top_k=top_k)

        return needs

    def build_need_chain(self, seed_topic: str) -> Optional[NeedChain]:
        """从种子话题构建需求链。"""
        with self._lock:
            chain = self.predictor.build_need_chain(seed_topic)
            if chain:
                self._need_chains[chain.chain_id] = chain
            return chain

    # ── 知识库管理 ─────────────────────────────────────────────────

    def register_knowledge_base(
        self, kb_entries: Dict[str, Any],
    ) -> None:
        """注册知识库条目。"""
        with self._lock:
            self._knowledge_base.update(kb_entries)
        logger.info(
            "Knowledge base updated: %d entries", len(kb_entries)
        )

    # ── 用户画像 ──────────────────────────────────────────────────

    def upsert_profile(self, profile: UserCognitiveProfile) -> None:
        """创建或更新用户认知画像。"""
        with self._lock:
            self._profiles[profile.profile_id] = profile

    def get_profile(
        self, profile_id: str,
    ) -> Optional[UserCognitiveProfile]:
        """获取用户认知画像。"""
        return self._profiles.get(profile_id)

    # ── 需求消解 ──────────────────────────────────────────────────

    def resolve_need(self, need_id: str) -> bool:
        """标记需求为已消解。"""
        with self._lock:
            need = self._active_needs.pop(need_id, None)
            if need is None:
                return False
            need.resolved_at = time.time()
            self._resolved_needs.append(need)
        return True

    def expel_expired(self) -> int:
        """清理过期需求。"""
        now = time.time()
        removed = 0
        with self._lock:
            expired = [
                nid
                for nid, n in self._active_needs.items()
                if now > n.expires_at
            ]
            for nid in expired:
                del self._active_needs[nid]
                removed += 1
        return removed

    # ── 话题检测与意图提取（启发式） ───────────────────────────────

    DOMAIN_KEYWORDS: Dict[str, List[str]] = {
        "programming": ["code", "bug", "function", "api", "debug", "compile"],
        "data_science": ["data", "pandas", "numpy", "analysis", "visualize"],
        "machine_learning": ["model", "train", "tensorflow", "pytorch", "neural"],
        "writing": ["write", "email", "report", "article", "blog", "document"],
        "research": ["research", "paper", "arxiv", "survey", "literature"],
        "design": ["design", "layout", "css", "style", "ui", "ux"],
        "devops": ["deploy", "docker", "kubernetes", "ci", "pipeline"],
        "database": ["sql", "query", "table", "index", "migration"],
    }

    def _detect_topic(self, content: str) -> str:
        """启发式话题检测。"""
        content_lower = content.lower()
        scores: Dict[str, int] = {}
        for domain, keywords in self.DOMAIN_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in content_lower)
            if score > 0:
                scores[domain] = score

        if scores:
            return max(scores, key=lambda k: scores[k])
        return "general"

    @staticmethod
    def _extract_intent(content: str) -> str:
        """简单的意图提取。"""
        intents = {
            "create": ["create", "make", "build", "generate", "write"],
            "analyze": ["analyze", "check", "examine", "review", "inspect"],
            "fix": ["fix", "repair", "debug", "solve", "resolve"],
            "explain": ["explain", "describe", "what", "how", "why"],
            "search": ["find", "search", "look", "locate"],
            "compare": ["compare", "versus", "vs", "difference"],
        }
        content_lower = content.lower()
        for intent, keywords in intents.items():
            if any(kw in content_lower for kw in keywords):
                return intent
        return "general"

    @staticmethod
    def _extract_keywords(content: str) -> List[str]:
        """提取关键词。"""
        import re

        words = re.findall(r"[a-zA-Z_]{3,}", content.lower())
        stopwords = {
            "the", "and", "for", "that", "this", "with", "from", "have",
            "are", "was", "can", "not", "but", "all", "will",
        }
        return list(set(w for w in words if w not in stopwords))[:20]

    # ── 统计与诊断 ───────────────────────────────────────────────

    def snapshot(self) -> AnticipatorStats:
        """获取引擎统计快照。"""
        with self._lock:
            resolved = len(self._resolved_needs)
            active = len(self._active_needs)
            expired_count = sum(
                1 for n in self._active_needs.values()
                if time.time() > n.expires_at
            )

            return AnticipatorStats(
                total_predictions=resolved + active,
                active_predictions=active,
                resolved_predictions=resolved,
                expired_predictions=expired_count,
                total_pre_fetches=len(self.prefetcher._completed_fetches),
                successful_pre_fetches=sum(
                    1 for f in self.prefetcher._completed_fetches
                    if f.knowledge_gap_resolved
                ),
                cache_hits=self.cache._hits,
                cache_misses=self.cache._misses,
                cache_size=len(self.cache._cache),
                total_knowledge_gaps_resolved=sum(
                    1 for f in self.prefetcher._completed_fetches
                    if f.knowledge_gap_resolved
                ),
                idle_cycles_utilized=self.prefetcher.idle_cycles,
            )

    def statistics(self) -> Dict[str, Any]:
        """返回完整运行时统计指标。"""
        snap = self.snapshot()
        predictor_stats = self.predictor.statistics()
        prefetcher_stats = self.prefetcher.statistics()
        cache_stats = self.cache.statistics()

        return {
            # 预测指标
            "predictions_total": snap.total_predictions,
            "predictions_active": snap.active_predictions,
            "predictions_resolved": snap.resolved_predictions,
            "predictions_expired": snap.expired_predictions,
            # 预获取指标
            "prefetches_total": snap.total_pre_fetches,
            "prefetches_successful": snap.successful_pre_fetches,
            "prefetches_success_rate": round(
                snap.successful_pre_fetches / max(snap.total_pre_fetches, 1), 4
            ),
            "idle_cycles": snap.idle_cycles_utilized,
            # 缓存指标
            "cache_hits": snap.cache_hits,
            "cache_misses": snap.cache_misses,
            "cache_hit_rate": round(
                snap.cache_hits / max(snap.cache_hits + snap.cache_misses, 1), 4
            ),
            "cache_size": snap.cache_size,
            # 领域覆盖
            "domains_supported": len(self.SUPPORTED_DOMAINS),
            "user_profiles": len(self._profiles),
            "need_chains_built": len(self._need_chains),
            # 子系统指标
            "predictor": predictor_stats,
            "prefetcher": prefetcher_stats,
            "cache": cache_stats,
            # 配置
            "auto_prefetch": self.auto_prefetch,
            "history_window": self.predictor.history_window,
        }

    def reset(self) -> None:
        """重置所有状态。"""
        with self._lock:
            self._active_needs.clear()
            self._resolved_needs.clear()
            self._need_chains.clear()
            self._knowledge_base.clear()
            self._profiles.clear()
            self.cache.clear()
            self.predictor = NeedPredictor(
                history_window=self.predictor.history_window,
                min_confidence=self.predictor.min_confidence,
            )
        logger.info("ProactiveAnticipator reset")
