"""
# status: reserve (2026-09 EXECUTION 163)
Trinity Second Brain — Selective Recall (Memory-as-a-Tool)
==========================================================
Mem0 2026 Production Best Practice · P0-02

核心问题：当前 Trinity 每轮都触发检索（"吸血鬼式检索"），导致延迟 +200-500ms、
注入 500+ 无关 token、无关记忆主动误导模型。正确做法是让 Agent 自己决定何时
需要召回，对标 Mem0 2026 "Memory-as-a-Tool" 设计——Agent 将记忆检索作为可选
工具而非强制管线步骤。

论文 / 最佳实践：
  Mem0 (2026). "Memory-as-a-Tool: Selective Recall for Agentic Systems."
  LangChain (2025). "Tool-Calling Memory Pattern."

设计要点：
  - RecallDecision 数据类：decision + confidence + reason + suggested_stores
  - SelectiveRecallRouter：意图分类快速路径 + 关键词触发 + LLM 辅助精确分类
  - SelectiveRecallManager：包装 TrinityRetrievalPipeline，先决策再检索
  - 统计信息：total_checks / skipped / recalled / tokens_saved
  - 与 _QueryRouter (engine_retrieval.py / retrieval.py) + BudgetMemRouter 集成

三元语：
  Retrieval: 选择性触发检索，大幅降低延迟与无关 token 注入
  Memory: suggested_stores 指明语义 / 情景 / 程序性子存储
  Guardian: 关键词触发列表防注入，LLM 辅助路径校验置信度阈值
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Configuration Constants
# ═══════════════════════════════════════════════════════════════════════════

SELECTIVE_RECALL_ENABLED: bool = True
"""全局开关：是否启用选择性召回。"""

SELECTIVE_RECALL_MAX_TOKENS: int = 500
"""检索结果上限（对标 Mem0 默认 500 tokens）。"""

SELECTIVE_RECALL_LLM_THRESHOLD: float = 0.5
"""置信度阈值：快速路径低于此值则触发 LLM 辅助分类。"""

SELECTIVE_RECALL_FORCE_KEYWORDS: List[str] = [
    # 中文强制触发词
    "之前说的", "上次提到", "还记得吗", "回忆一下", "之前聊过",
    "以前说过", "你记不记得", "以前讨论", "历史记录", "之前的对话",
    "参考之前", "你之前说过", "根据之前的", "查一下记忆", "查查记忆",
    "帮我回忆", "那天说的", "上次那个", "过去的记录", "你记错了",
    # 英文强制触发词
    "remember", "recall", "previous", "last time", "earlier",
    "do you remember", "what did I say", "before this", "in the past",
    "mentioned earlier", "as we discussed", "you said", "look back",
    "memory of", "what was that", "remind me", "previously discussed",
    "as mentioned", "per our last", "check your memory", "look up past",
]
"""强制触发关键词列表：命中任一即触发 recall（不经过分类器）。"""

# 闲聊类意图关键词（无需召回）
_CHITCHAT_KEYWORDS: Set[str] = {
    "你好", "谢谢", "再见", "好的", "ok", "thanks", "hi", "hello",
    "bye", "晚安", "早安", "午安", "哈哈", "嗯", "哦",
}

# 需要回忆的意图关键词
_RECALL_KEYWORDS: Set[str] = {
    "之前", "上次", "历史", "以前", "过去", "曾经", "记得",
    "previous", "before", "history", "past", "remember", "recall",
}

# 需要知识库的意图关键词
_KNOWLEDGE_KEYWORDS: Set[str] = {
    "是什么", "怎么", "为什么", "解释", "定义", "原理",
    "how", "what", "why", "explain", "define", "concept",
}


# ═══════════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════════

class RecallDecisionType(Enum):
    """选择性召回决策类型。"""
    RECALL = "recall"       # 需要检索记忆
    SKIP = "skip"           # 跳过检索
    DEFERRED = "deferred"   # 延后决策（先走当前轮，看结果再定）


class IntentClass(Enum):
    """意图分类——决定是否需要记忆检索。"""
    CHITCHAT = auto()        # 闲聊，无需召回
    NEEDS_HISTORY = auto()   # 需要对话历史 / 情景记忆
    NEEDS_KNOWLEDGE = auto() # 需要知识库 / 语义记忆
    NEEDS_BOTH = auto()      # 历史 + 知识库都需要
    AMBIGUOUS = auto()       # 模糊，需 LLM 辅助


class RecallStore(Enum):
    """可检索的记忆子存储类型。"""
    SEMANTIC = "semantic"       # 语义 / 知识记忆
    EPISODIC = "episodic"       # 情景 / 对话历史
    PROCEDURAL = "procedural"   # 程序性 / 工具使用记忆


# ═══════════════════════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class RecallDecision:
    """单次选择性召回决策。

    Attributes
    ----------
    decision : RecallDecisionType
        决策结果（recall / skip / deferred）。
    confidence : float
        置信度 [0, 1]。
    reason : str
        决策原因（可审计）。
    suggested_stores : List[RecallStore]
        建议检索的子存储列表。
    intent : IntentClass
        意图分类结果。
    metadata : Dict[str, Any]
        附加元数据（含触发关键词、快速/LLM路径标识等）。
    """
    decision: RecallDecisionType
    confidence: float = 0.0
    reason: str = ""
    suggested_stores: List[RecallStore] = field(default_factory=lambda: [RecallStore.EPISODIC])
    intent: IntentClass = IntentClass.AMBIGUOUS
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "suggested_stores": [s.value for s in self.suggested_stores],
            "intent": self.intent.name,
            "metadata": self.metadata,
        }


@dataclass
class SelectiveRecallStats:
    """SelectiveRecallManager 运行时统计。"""
    total_checks: int = 0
    total_skipped: int = 0
    total_recalled: int = 0
    total_deferred: int = 0
    tokens_saved: int = 0              # 跳过检索节省的 token 估计
    llm_assists: int = 0               # 触发 LLM 辅助分类次数
    fast_path_hits: int = 0            # 快速路径命中次数
    keyword_triggers: int = 0          # 关键词触发次数
    avg_decision_time_ms: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════
# SelectiveRecallRouter
# ═══════════════════════════════════════════════════════════════════════════

class SelectiveRecallRouter:
    """选择性召回路由器。

    两级决策：
      1. 快速路径（无 LLM）：关键词 + 规则分类，O(1) 判定
      2. LLM 辅助路径：快速路径置信度不足时由 LLM 精确分类

    Parameters
    ----------
    force_keywords : List[str]
        强制触发关键词列表。命中任一即直接 recall。
    llm_threshold : float
        快速路径置信度低于此值触发 LLM 辅助。
    llm_classify_fn : Optional[Callable]
        LLM 辅助分类函数，签名 `fn(query: str, context: dict) -> IntentClass`。
    """

    def __init__(
        self,
        force_keywords: Optional[List[str]] = None,
        llm_threshold: float = SELECTIVE_RECALL_LLM_THRESHOLD,
        llm_classify_fn: Optional[Callable[[str, Dict[str, Any]], IntentClass]] = None,
    ) -> None:
        self._force_keywords = force_keywords or list(SELECTIVE_RECALL_FORCE_KEYWORDS)
        self._llm_threshold = llm_threshold
        self._llm_classify = llm_classify_fn
        self._lock = threading.RLock()

        # 编译关键词正则（大小写不敏感）
        self._force_patterns = [
            re.compile(re.escape(kw), re.IGNORECASE)
            for kw in self._force_keywords
        ]

        logger.info(
            "SelectiveRecallRouter initialized [force_kw=%d llm_threshold=%.2f llm=%s]",
            len(self._force_keywords), llm_threshold, llm_classify_fn is not None,
        )

    # ── Public API ──────────────────────────────────────────────────────

    def should_recall(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> RecallDecision:
        """判断当前轮次是否需要检索记忆。

        Parameters
        ----------
        query : str
            用户查询文本。
        context : Optional[Dict[str, Any]]
            会话上下文（对话轮数、历史消息等）。

        Returns
        -------
        RecallDecision
        """
        ctx = context or {}
        start = time.perf_counter()

        with self._lock:
            # ── 第 0 层：关键词强制触发 ──
            hit_keyword = self._check_force_keywords(query)
            if hit_keyword:
                return RecallDecision(
                    decision=RecallDecisionType.RECALL,
                    confidence=1.0,
                    reason=f"Force keyword triggered: '{hit_keyword}'",
                    suggested_stores=[RecallStore.EPISODIC, RecallStore.SEMANTIC],
                    intent=IntentClass.NEEDS_HISTORY,
                    metadata={
                        "trigger": "keyword",
                        "keyword": hit_keyword,
                        "path": "fast",
                        "decision_time_ms": (time.perf_counter() - start) * 1000,
                    },
                )

            # ── 第 1 层：快速路径意图分类 ──
            intent, fast_confidence = self._fast_intent_classify(query, ctx)
            decision = self._intent_to_decision(intent)

            # 如果快速路径置信度足够，直接返回
            if fast_confidence >= self._llm_threshold:
                return RecallDecision(
                    decision=decision,
                    confidence=fast_confidence,
                    reason=f"Fast path: {intent.name} (conf={fast_confidence:.2f})",
                    suggested_stores=self._stores_for_intent(intent),
                    intent=intent,
                    metadata={
                        "trigger": "fast_path",
                        "path": "fast",
                        "decision_time_ms": (time.perf_counter() - start) * 1000,
                    },
                )

            # ── 第 2 层：LLM 辅助精确分类 ──
            if self._llm_classify:
                try:
                    llm_intent = self._llm_classify(query, ctx)
                    llm_confidence = 0.85  # LLM 辅助默认置信度
                    llm_decision = self._intent_to_decision(llm_intent)
                    return RecallDecision(
                        decision=llm_decision,
                        confidence=llm_confidence,
                        reason=f"LLM-assisted: {llm_intent.name}",
                        suggested_stores=self._stores_for_intent(llm_intent),
                        intent=llm_intent,
                        metadata={
                            "trigger": "llm_assist",
                            "path": "llm",
                            "fast_intent": intent.name,
                            "decision_time_ms": (time.perf_counter() - start) * 1000,
                        },
                    )
                except Exception as e:
                    logger.warning("LLM classification failed: %s, falling back to fast path", e)

            # ── 兜底：快速路径结果 ──
            return RecallDecision(
                decision=decision,
                confidence=fast_confidence,
                reason=f"Fast path (fallback): {intent.name}",
                suggested_stores=self._stores_for_intent(intent),
                intent=intent,
                metadata={
                    "trigger": "fast_path_fallback",
                    "path": "fast",
                    "decision_time_ms": (time.perf_counter() - start) * 1000,
                },
            )

    # ── Internal Methods ────────────────────────────────────────────────

    def _check_force_keywords(self, query: str) -> Optional[str]:
        """检查是否命中强制触发关键词。返回命中的关键词或 None。"""
        q_lower = query.lower()
        for i, pat in enumerate(self._force_patterns):
            if pat.search(q_lower):
                return self._force_keywords[i]
        return None

    def _fast_intent_classify(
        self, query: str, context: Dict[str, Any]
    ) -> Tuple[IntentClass, float]:
        """快速路径意图分类——无 LLM，O(1)。

        基于规则：
        - 极短句 / 纯感叹 / emoji → CHITCHAT
        - 含时间/历史/回忆关键词 → NEEDS_HISTORY
        - 含知识/解释/方法关键词 → NEEDS_KNOWLEDGE
        - 两者都有 → NEEDS_BOTH
        - 否则根据查询长度和上下文判断

        Returns
        -------
        (IntentClass, confidence)
        """
        q = query.strip()
        q_lower = q.lower()

        # 极短闲聊
        if len(q) <= 2 and any(kw == q_lower for kw in _CHITCHAT_KEYWORDS):
            return IntentClass.CHITCHAT, 0.95

        # 极短纯闲聊
        if len(q) <= 3 and not any(c.isalpha() for c in q):
            return IntentClass.CHITCHAT, 0.90

        # 闲聊关键词
        if q_lower in _CHITCHAT_KEYWORDS:
            return IntentClass.CHITCHAT, 0.92

        # 计算特征得分
        recall_score = sum(
            2.0 if kw in q_lower else 0.0
            for kw in _RECALL_KEYWORDS
        )

        knowledge_score = sum(
            2.0 if kw in q_lower else 0.0
            for kw in _KNOWLEDGE_KEYWORDS
        )

        # 长度特征
        q_len = len(q.split())
        if q_len >= 15:
            knowledge_score += 0.5  # 长查询更可能是知识类

        # 上下文特征：历史消息多更可能需要回忆
        ctx_turns = context.get("turn_count", 0)
        if ctx_turns > 5:
            recall_score += 0.3

        # 分类判定
        if recall_score >= 2.0 and knowledge_score >= 2.0:
            return IntentClass.NEEDS_BOTH, 0.85
        elif recall_score >= 2.0:
            return IntentClass.NEEDS_HISTORY, 0.82
        elif knowledge_score >= 2.0:
            return IntentClass.NEEDS_KNOWLEDGE, 0.80
        elif recall_score >= 0.5 or knowledge_score >= 0.5:
            return IntentClass.AMBIGUOUS, 0.45  # 低于阈值，触发 LLM 辅助
        else:
            # 默认：短查询 → 闲聊，长查询 → 知识
            if q_len <= 5:
                return IntentClass.CHITCHAT, 0.70
            else:
                return IntentClass.AMBIGUOUS, 0.40

    def _intent_to_decision(self, intent: IntentClass) -> RecallDecisionType:
        """意图 → 召回决策映射。"""
        mapping = {
            IntentClass.CHITCHAT: RecallDecisionType.SKIP,
            IntentClass.NEEDS_HISTORY: RecallDecisionType.RECALL,
            IntentClass.NEEDS_KNOWLEDGE: RecallDecisionType.RECALL,
            IntentClass.NEEDS_BOTH: RecallDecisionType.RECALL,
            IntentClass.AMBIGUOUS: RecallDecisionType.DEFERRED,
        }
        return mapping.get(intent, RecallDecisionType.DEFERRED)

    def _stores_for_intent(self, intent: IntentClass) -> List[RecallStore]:
        """意图 → 建议检索的子存储列表。"""
        mapping = {
            IntentClass.CHITCHAT: [],
            IntentClass.NEEDS_HISTORY: [RecallStore.EPISODIC],
            IntentClass.NEEDS_KNOWLEDGE: [RecallStore.SEMANTIC],
            IntentClass.NEEDS_BOTH: [RecallStore.EPISODIC, RecallStore.SEMANTIC],
            IntentClass.AMBIGUOUS: [RecallStore.EPISODIC, RecallStore.SEMANTIC, RecallStore.PROCEDURAL],
        }
        return mapping.get(intent, [])

    # ── Statistics ──────────────────────────────────────────────────────

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "force_keywords_count": len(self._force_keywords),
                "llm_threshold": self._llm_threshold,
                "llm_enabled": self._llm_classify is not None,
                "status": "ready",
            }


# ═══════════════════════════════════════════════════════════════════════════
# SelectiveRecallManager
# ═══════════════════════════════════════════════════════════════════════════

class SelectiveRecallManager:
    """选择性召回管理器——包装 TrinityRetrievalPipeline，在检索前插入决策。

    对标 Mem0 2026 "Memory-as-a-Tool"：Agent 将检索作为可选工具，
    由 SelectiveRecallRouter 动态决定是否触发。

    Parameters
    ----------
    pipeline : Optional[Any]
        TrinityRetrievalPipeline 实例（可延迟注入）。
    router : Optional[SelectiveRecallRouter]
        SelectiveRecallRouter 实例。
    max_result_tokens : int
        检索结果最大 token 数（对标 Mem0 500 tokens）。
    enabled : bool
        全局启用开关。
    """

    def __init__(
        self,
        pipeline: Optional[Any] = None,
        router: Optional[SelectiveRecallRouter] = None,
        max_result_tokens: int = SELECTIVE_RECALL_MAX_TOKENS,
        enabled: bool = SELECTIVE_RECALL_ENABLED,
    ) -> None:
        self._pipeline = pipeline
        self._router = router or SelectiveRecallRouter()
        self._max_result_tokens = max_result_tokens
        self._enabled = enabled
        self._lock = threading.RLock()

        # Stats
        self._stats = SelectiveRecallStats()

        logger.info(
            "SelectiveRecallManager initialized [enabled=%s max_tokens=%d]",
            enabled, max_result_tokens,
        )

    # ── Public API ──────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        with self._lock:
            self._enabled = value

    @property
    def pipeline(self):
        return self._pipeline

    @pipeline.setter
    def pipeline(self, value) -> None:
        with self._lock:
            self._pipeline = value

    @property
    def router(self) -> SelectiveRecallRouter:
        return self._router

    def selective_search(
        self,
        query: str,
        query_vector: Optional[np.ndarray] = None,
        top_k: int = 10,
        context: Optional[Dict[str, Any]] = None,
        use_reranker: bool = True,
    ) -> Dict[str, Any]:
        """先决策再检索——核心方法。

        Parameters
        ----------
        query : str
            用户查询。
        query_vector : Optional[np.ndarray]
            查询向量（传给底层 pipeline）。
        top_k : int
            检索结果数。
        context : Optional[Dict[str, Any]]
            会话上下文（轮数、历史等）。
        use_reranker : bool
            是否使用 reranker。

        Returns
        -------
        Dict[str, Any]
            {
                "decision": RecallDecision.to_dict(),
                "results": [...] or [],
                "recalled": bool,
                "tokens_estimated": int,
            }
        """
        start = time.perf_counter()

        with self._lock:
            self._stats.total_checks += 1

            # 未启用 → 直接检索
            if not self._enabled:
                results = self._do_recall(query, query_vector, top_k, use_reranker)
                self._stats.total_recalled += 1
                elapsed_ms = (time.perf_counter() - start) * 1000
                self._stats.avg_decision_time_ms = (
                    (self._stats.avg_decision_time_ms * (self._stats.total_checks - 1) + elapsed_ms)
                    / max(self._stats.total_checks, 1)
                )
                return {
                    "decision": {
                        "decision": "recall",
                        "reason": "Selective recall disabled — direct pass-through",
                        "confidence": 1.0,
                    },
                    "results": results,
                    "recalled": True,
                    "tokens_estimated": self._estimate_tokens(results),
                }

            # 决策
            decision = self._router.should_recall(query, context)

            # 更新统计
            self._update_stats_from_decision(decision, start)

            # 按决策执行
            if decision.decision == RecallDecisionType.SKIP:
                return {
                    "decision": decision.to_dict(),
                    "results": [],
                    "recalled": False,
                    "tokens_estimated": 0,
                }

            if decision.decision == RecallDecisionType.DEFERRED:
                # Deferred：先做轻量检索（episodic only, 低 top_k）
                light_results = self._do_recall(
                    query, query_vector,
                    top_k=min(top_k, 3),
                    use_reranker=False,
                )
                return {
                    "decision": decision.to_dict(),
                    "results": light_results,
                    "recalled": True,
                    "tokens_estimated": self._estimate_tokens(light_results),
                }

            # RECALL
            results = self._do_recall(query, query_vector, top_k, use_reranker)
            return {
                "decision": decision.to_dict(),
                "results": results,
                "recalled": True,
                "tokens_estimated": self._estimate_tokens(results),
            }

    def _do_recall(
        self,
        query: str,
        query_vector: Optional[np.ndarray],
        top_k: int,
        use_reranker: bool,
    ) -> List[Dict[str, Any]]:
        """执行实际检索。无 pipeline 时返回空。"""
        if self._pipeline is None:
            logger.warning("No pipeline configured, returning empty results")
            return []

        try:
            from trinity.modules.second_brain.retrieval import TrinityRetrievalPipeline

            if isinstance(self._pipeline, TrinityRetrievalPipeline):
                results = self._pipeline.search(
                    query=query,
                    query_vector=query_vector,
                    top_k=top_k,
                    use_reranker=use_reranker,
                )
                return results if isinstance(results, list) else []
            else:
                # Duck-typing: 尝试 search 方法
                results = self._pipeline.search(
                    query=query,
                    query_vector=query_vector,
                    top_k=top_k,
                    use_reranker=use_reranker,
                )
                return results if isinstance(results, list) else []
        except Exception as e:
            logger.warning("Recall failed: %s", e)
            return []

    def _estimate_tokens(self, results: List[Dict[str, Any]]) -> int:
        """估算检索结果的 token 数（1 token ≈ 4 chars）。"""
        if not results:
            return 0
        total_chars = 0
        for r in results:
            text = r.get("text", r.get("content", ""))
            if isinstance(text, str):
                total_chars += len(text)
        return max(1, total_chars // 4)

    def _update_stats_from_decision(
        self, decision: RecallDecision, start: float
    ) -> None:
        """更新运行时统计。"""
        if decision.decision == RecallDecisionType.RECALL:
            self._stats.total_recalled += 1
        elif decision.decision == RecallDecisionType.SKIP:
            self._stats.total_skipped += 1
            self._stats.tokens_saved += self._max_result_tokens
        elif decision.decision == RecallDecisionType.DEFERRED:
            self._stats.total_deferred += 1

        md = decision.metadata
        if md.get("trigger") == "keyword":
            self._stats.keyword_triggers += 1
        elif md.get("trigger") == "llm_assist":
            self._stats.llm_assists += 1
        elif md.get("trigger") in ("fast_path", "fast_path_fallback"):
            self._stats.fast_path_hits += 1

        elapsed_ms = (time.perf_counter() - start) * 1000
        n = max(self._stats.total_checks, 1)
        self._stats.avg_decision_time_ms = (
            (self._stats.avg_decision_time_ms * (n - 1) + elapsed_ms) / n
        )

    # ── Statistics ──────────────────────────────────────────────────────

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            s = self._stats
            total = max(s.total_checks, 1)
            return {
                "enabled": self._enabled,
                "max_result_tokens": self._max_result_tokens,
                "total_checks": s.total_checks,
                "total_skipped": s.total_skipped,
                "total_recalled": s.total_recalled,
                "total_deferred": s.total_deferred,
                "tokens_saved": s.tokens_saved,
                "skip_rate_pct": round(s.total_skipped / total * 100, 1),
                "recall_rate_pct": round(s.total_recalled / total * 100, 1),
                "deferred_rate_pct": round(s.total_deferred / total * 100, 1),
                "llm_assists": s.llm_assists,
                "fast_path_hits": s.fast_path_hits,
                "keyword_triggers": s.keyword_triggers,
                "avg_decision_time_ms": round(s.avg_decision_time_ms, 2),
                "router": self._router.statistics(),
                "pipeline_configured": self._pipeline is not None,
            }


# ═══════════════════════════════════════════════════════════════════════════
# Module-level selftest
# ═══════════════════════════════════════════════════════════════════════════

def self_test() -> Dict[str, Any]:
    """模块自检——验证核心类可实例化、关键路径可运行。"""
    results = {}

    # 1. RecallDecision 数据类
    rd = RecallDecision(decision=RecallDecisionType.RECALL, confidence=0.9, reason="test")
    d = rd.to_dict()
    assert d["decision"] == "recall"
    assert d["confidence"] == 0.9
    results["RecallDecision"] = "ok"

    # 2. SelectiveRecallRouter — fast path: chitchat
    router = SelectiveRecallRouter()
    decision = router.should_recall("你好")
    assert decision.decision == RecallDecisionType.SKIP, f"Expected SKIP, got {decision.decision}"
    results["Router_chitchat"] = f"SKIP conf={decision.confidence:.2f}"

    # 3. SelectiveRecallRouter — fast path: needs history (keyword)
    decision = router.should_recall("你还记得吗上周那个项目")
    assert decision.decision == RecallDecisionType.RECALL, f"Expected RECALL, got {decision.decision}"
    results["Router_history_keyword"] = f"RECALL conf={decision.confidence:.2f}"

    # 4. SelectiveRecallRouter — fast path: needs knowledge
    decision = router.should_recall("什么是transformer注意力机制原理")
    assert decision.decision == RecallDecisionType.RECALL
    results["Router_knowledge"] = f"RECALL conf={decision.confidence:.2f}"

    # 5. SelectiveRecallRouter — force keyword
    decision = router.should_recall("之前说的那个方案怎么样了")
    assert decision.decision == RecallDecisionType.RECALL
    assert decision.metadata.get("trigger") == "keyword"
    results["Router_force_keyword"] = f"RECALL keyword='{decision.metadata.get('keyword')}'"

    # 6. SelectiveRecallManager — without pipeline
    mgr = SelectiveRecallManager(enabled=True)
    result = mgr.selective_search("今天天气真好", context={"turn_count": 1})
    assert not result["recalled"]
    results["Manager_skip"] = f"SKIP (no pipeline)"

    # 7. SelectiveRecallManager — disabled mode (direct pass-through, no pipeline)
    mgr2 = SelectiveRecallManager(enabled=False)
    result2 = mgr2.selective_search("test query")
    assert result2["recalled"]
    assert result2["decision"]["reason"] == "Selective recall disabled — direct pass-through"
    results["Manager_disabled"] = "DIRECT"

    # 8. Statistics
    stats = mgr.statistics()
    assert stats["total_checks"] == 1
    assert stats["total_skipped"] == 1
    results["Stats"] = f"checks={stats['total_checks']} skip_rate={stats['skip_rate_pct']}%"

    # 9. Router statistics
    router_stats = router.statistics()
    results["Router_stats"] = f"force_kw={router_stats['force_keywords_count']} llm={router_stats['llm_enabled']}"

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Module entry
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    tr = self_test()
    for k, v in tr.items():
        print(f"  {k}: {v}")
    print("\n  ALL TESTS PASSED")
