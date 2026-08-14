"""
P17-3: Hierarchical Summarization Chain — 层次化摘要链

对标: Agent Context Engineering 2026 — 滑动窗口 + 层次摘要 + 记忆卸载
三元语: 即时摘要 → 会话摘要 → 长期摘要 → 图注入 → 预算管理 → 压缩策略

设计要点:
- InstantSummarizer: 对话结束后立即生成本轮关键要点 (即时层)
- SessionSummarizer: 累积多轮即时摘要后压缩为会话级概括 (会话层)
- LongTermSummarizer: 多个会话摘要进一步合并为长期知识 (长期层)
- SummaryGraphIntegrator: 将各层摘要注入知识图谱节点, 与图检索联动
- ContextBudgetManager: 根据 token 预算动态选择检索粒度 (全量→摘要→仅图)
- CompactionPolicy: 何时压缩/压缩粒度/保留哪些关键细节的策略引擎
- 与 P2 compression.py / P11 intent_compression.py 互补——compression 做向量压缩, 本模块做三层文本摘要链
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class SummaryLevel(Enum):
    """摘要层次"""
    INSTANT = "instant"       # 即时层 (单轮)
    SESSION = "session"       # 会话层 (多轮)
    LONG_TERM = "long_term"   # 长期层 (跨会话)


class RetrievalGranularity(Enum):
    """检索粒度级别"""
    FULL_CONTEXT = "full_context"       # 全量上下文
    SUMMARY_ONLY = "summary_only"       # 仅摘要
    GRAPH_ONLY = "graph_only"           # 仅知识图谱
    HYBRID = "hybrid"                   # 混合 (摘要+关键原文)


class CompactionTrigger(Enum):
    """压缩触发条件"""
    TOKEN_THRESHOLD = auto()     # Token 超过阈值
    TIME_WINDOW = auto()         # 时间窗口到期
    SESSION_END = auto()         # 会话结束
    MANUAL = auto()              # 手动触发
    IMPORTANCE_DECAY = auto()    # 重要性衰减


class SummaryQuality(Enum):
    """摘要质量等级"""
    DRAFT = "draft"
    REVIEWED = "reviewed"
    FINAL = "final"
    ARCHIVED = "archived"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class InstantSummary:
    """即时摘要 (单轮对话)"""
    summary_id: str
    session_id: str
    turn_index: int
    content: str                      # 摘要文本
    key_points: List[str]             # 关键要点
    entities: List[str]               # 涉及的实体
    decisions: List[str]              # 做出的决策
    action_items: List[str]           # 待办事项
    token_count: int
    importance_score: float           # [0, 1]
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionSummary:
    """会话摘要 (多轮压缩)"""
    summary_id: str
    session_id: str
    instant_summary_ids: List[str]    # 源即时摘要 ID 列表
    content: str
    major_themes: List[str]           # 主要主题
    resolved_topics: List[str]        # 已解决话题
    pending_topics: List[str]         # 待解决话题
    key_outcomes: List[str]           # 关键成果
    token_count: int
    compression_ratio: float          # 压缩比 = output/input
    quality: SummaryQuality = SummaryQuality.DRAFT
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LongTermSummary:
    """长期摘要 (跨会话)"""
    summary_id: str
    session_summary_ids: List[str]
    content: str
    knowledge_nuggets: List[str]      # 可复用的知识片段
    learned_patterns: List[str]       # 学到的模式
    deprecated_info: List[str]        # 已过时信息
    confidence_decayed: List[str]     # 置信度衰减条目
    token_count: int
    retention_priority: float         # 保留优先级 [0, 1]
    last_updated: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphNode:
    """知识图谱节点 (摘要注入用)"""
    node_id: str
    label: str
    summary_ref: Optional[str]        # 关联摘要 ID
    properties: Dict[str, Any] = field(default_factory=dict)
    edges: List[str] = field(default_factory=list)  # 邻接节点 ID
    importance: float = 0.5
    timestamp: float = field(default_factory=time.time)


@dataclass
class BudgetConfig:
    """上下文预算配置"""
    max_tokens: int = 8000
    current_usage: int = 0
    granularity: RetrievalGranularity = RetrievalGranularity.FULL_CONTEXT
    reserved_for_response: int = 2000
    overhead_per_item: int = 50


@dataclass
class CompactionRule:
    """压缩规则"""
    rule_id: str
    trigger: CompactionTrigger
    source_level: SummaryLevel
    target_level: SummaryLevel
    threshold_value: float            # 触发阈值
    preserve_ratio: float = 0.3       # 保留比 (保留多少关键细节)
    priority: int = 5                 # 1-10, 越高越优先
    enabled: bool = True


# ============================================================================
# InstantSummarizer — 即时摘要生成器
# ============================================================================

class InstantSummarizer:
    """
    对话结束后立即生成本轮关键要点。

    从原始对话中提取: 关键要点/实体/决策/待办事项。
    使用轻量级规则+统计方法, 不依赖外部模型。
    """

    def __init__(self, max_summary_length: int = 500):
        self.max_summary_length = max_summary_length
        self._lock = threading.RLock()
        self._summaries: OrderedDict[str, InstantSummary] = OrderedDict()
        self._total_summaries: int = 0
        self._session_turns: Dict[str, int] = defaultdict(int)

    def summarize(
        self,
        session_id: str,
        turn_content: str,
        entities: Optional[List[str]] = None,
        decisions: Optional[List[str]] = None,
        action_items: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> InstantSummary:
        """为一轮对话生成即时摘要"""
        with self._lock:
            self._session_turns[session_id] += 1
            turn_index = self._session_turns[session_id]

        # 提取关键要点
        key_points = self._extract_key_points(turn_content)
        entities = entities or self._extract_entities(turn_content)

        # 生成摘要文本
        content = self._generate_summary_text(turn_context=turn_content, key_points=key_points)

        # 计算重要性 (基于内容长度/关键点数/决策数)
        importance = min(1.0, (
            0.3 * min(1.0, len(turn_content) / 500) +
            0.3 * min(1.0, len(key_points) / 5) +
            0.4 * min(1.0, len(decisions or []) / 3)
        ))

        summary = InstantSummary(
            summary_id=f"inst_{self._total_summaries:08d}",
            session_id=session_id,
            turn_index=turn_index,
            content=content,
            key_points=key_points,
            entities=entities,
            decisions=decisions or [],
            action_items=action_items or [],
            token_count=len(content.split()),
            importance_score=importance,
            metadata=metadata or {},
        )

        with self._lock:
            if len(self._summaries) >= 2048:
                self._summaries.popitem(last=False)
            self._summaries[summary.summary_id] = summary
            self._total_summaries += 1

        return summary

    def _extract_key_points(self, text: str) -> List[str]:
        """从文本中提取关键要点 (基于规则)"""
        points = []
        sentences = [s.strip() for s in text.replace("。", ".").split(".") if len(s.strip()) > 10]
        # 取首尾各2句 + 中间最长的1句
        if sentences:
            if len(sentences) <= 5:
                points = sentences[:3]
            else:
                points = sentences[:2] + [max(sentences[2:-2], key=len)] if len(sentences) > 4 else sentences[:3]
        return [p[:200] for p in points]

    def _extract_entities(self, text: str) -> List[str]:
        """简单实体提取"""
        entities = []
        # 基于启发式规则提取: 大写词/引号内词/URL 模式
        import re
        quoted = re.findall(r'["\']([^"\']{2,30})["\']', text)
        entities.extend(quoted[:5])
        # 驼峰词
        camel = re.findall(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b', text)
        entities.extend(camel[:3])
        # 全大写缩写
        upper = re.findall(r'\b[A-Z]{2,8}\b', text)
        entities.extend(upper[:3])
        return list(set(entities))[:10]

    def _generate_summary_text(
        self, turn_context: str, key_points: List[str]
    ) -> str:
        if not key_points:
            return turn_context[:self.max_summary_length]
        return " ".join(key_points)[:self.max_summary_length]

    def get_session_summaries(self, session_id: str) -> List[InstantSummary]:
        with self._lock:
            return [
                s for s in self._summaries.values()
                if s.session_id == session_id
            ]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_summaries": self._total_summaries,
                "active_sessions": len(self._session_turns),
                "avg_key_points": (
                    sum(len(s.key_points) for s in self._summaries.values()) / max(1, len(self._summaries))
                    if self._summaries else 0.0
                ),
                "avg_importance": (
                    sum(s.importance_score for s in self._summaries.values()) / max(1, len(self._summaries))
                    if self._summaries else 0.0
                ),
            }


# ============================================================================
# SessionSummarizer — 会话级摘要
# ============================================================================

class SessionSummarizer:
    """
    累积多轮即时摘要后压缩为会话级概括。

    合并多轮的关键要点, 去除冗余, 识别主题和成果。
    """

    def __init__(self, max_session_summary_length: int = 1000):
        self.max_session_summary_length = max_session_summary_length
        self._lock = threading.RLock()
        self._summaries: OrderedDict[str, SessionSummary] = OrderedDict()
        self._total_summaries: int = 0

    def summarize_session(
        self,
        session_id: str,
        instant_summaries: List[InstantSummary],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SessionSummary:
        """将会话内所有即时摘要压缩为会话摘要"""
        if not instant_summaries:
            raise ValueError("No instant summaries to compress")

        all_key_points: List[str] = []
        all_entities: Set[str] = set()
        all_decisions: List[str] = []
        all_actions: List[str] = []

        for s in instant_summaries:
            all_key_points.extend(s.key_points)
            all_entities.update(s.entities)
            all_decisions.extend(s.decisions)
            all_actions.extend(s.action_items)

        # 去重关键要点
        unique_points = self._deduplicate_points(all_key_points)[:20]

        # 识别主题
        themes = self._cluster_themes(unique_points)

        # 区分已解决/待解决
        resolved, pending = self._classify_topics(unique_points, all_decisions)

        # 生成压缩摘要
        content = self._build_session_content(
            themes=themes,
            outcomes=all_decisions[:10],
            pending=pending[:5],
        )

        input_tokens = sum(s.token_count for s in instant_summaries)
        compression_ratio = len(content.split()) / max(1, input_tokens)

        summary = SessionSummary(
            summary_id=f"sess_{self._total_summaries:08d}",
            session_id=session_id,
            instant_summary_ids=[s.summary_id for s in instant_summaries],
            content=content,
            major_themes=themes[:8],
            resolved_topics=resolved[:10],
            pending_topics=pending[:5],
            key_outcomes=all_decisions[:10],
            token_count=len(content.split()),
            compression_ratio=compression_ratio,
            metadata=metadata or {},
        )

        with self._lock:
            if len(self._summaries) >= 1024:
                self._summaries.popitem(last=False)
            self._summaries[summary.summary_id] = summary
            self._total_summaries += 1

        return summary

    def _deduplicate_points(self, points: List[str]) -> List[str]:
        """基于 Jaccard 相似度去重"""
        seen: List[str] = []
        for p in points:
            is_dup = False
            for s in seen:
                if self._jaccard(p.split(), s.split()) > 0.6:
                    is_dup = True
                    break
            if not is_dup:
                seen.append(p)
        return seen

    def _jaccard(self, a: List[str], b: List[str]) -> float:
        set_a, set_b = set(a), set(b)
        intersect = len(set_a & set_b)
        union = len(set_a | set_b)
        return intersect / union if union > 0 else 0.0

    def _cluster_themes(self, points: List[str]) -> List[str]:
        """基于关键词聚类识别主题"""
        # 简单频率统计
        word_freq: Dict[str, int] = defaultdict(int)
        for p in points:
            for w in p.lower().split():
                if len(w) > 2:
                    word_freq[w] += 1
        return [w for w, _ in sorted(word_freq.items(), key=lambda x: -x[1])[:12]]

    def _classify_topics(
        self, points: List[str], decisions: List[str]
    ) -> Tuple[List[str], List[str]]:
        resolved = [d for d in decisions if d]
        # 有决策 = 已解决
        pending = [p for p in points[:10] if p not in resolved]
        return resolved, pending

    def _build_session_content(
        self, themes: List[str], outcomes: List[str], pending: List[str]
    ) -> str:
        parts = []
        if themes:
            parts.append(f"主题: {', '.join(themes[:5])}。")
        if outcomes:
            parts.append(f"成果: {', '.join(outcomes[:5])}。")
        if pending:
            parts.append(f"待处理: {', '.join(pending[:3])}。")
        return " ".join(parts)[:self.max_session_summary_length]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_session_summaries": self._total_summaries,
                "cached": len(self._summaries),
                "avg_compression_ratio": (
                    sum(s.compression_ratio for s in self._summaries.values()) / max(1, len(self._summaries))
                    if self._summaries else 0.0
                ),
            }


# ============================================================================
# LongTermSummarizer — 长期层摘要
# ============================================================================

class LongTermSummarizer:
    """
    多个会话摘要进一步合并为长期知识。

    提取可复用知识片段, 记录学到的模式, 标记过时信息。
    """

    def __init__(self, max_knowledge_nuggets: int = 200, decay_threshold: float = 0.2):
        self.max_knowledge_nuggets = max_knowledge_nuggets
        self.decay_threshold = decay_threshold
        self._lock = threading.RLock()
        self._summaries: OrderedDict[str, LongTermSummary] = OrderedDict()
        self._knowledge_index: Dict[str, float] = {}  # knowledge → confidence
        self._total_summaries: int = 0

    def summarize(
        self,
        session_summaries: List[SessionSummary],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LongTermSummary:
        """合并多个会话摘要为长期知识"""
        all_themes: List[str] = []
        all_outcomes: List[str] = []
        all_content: List[str] = []

        for s in session_summaries:
            all_themes.extend(s.major_themes)
            all_outcomes.extend(s.key_outcomes)
            all_content.append(s.content)

        # 提取知识片段
        nuggets = self._extract_knowledge_nuggets(all_themes, all_outcomes)
        patterns = self._detect_patterns(session_summaries)
        deprecated = self._identify_deprecated(all_themes, session_summaries)
        confidence_decayed = self._decay_old_confidence(session_summaries)

        retention_priority = self._compute_retention_priority(nuggets, patterns)

        summary = LongTermSummary(
            summary_id=f"lt_{self._total_summaries:08d}",
            session_summary_ids=[s.summary_id for s in session_summaries],
            content=" ".join(all_content)[:2000],
            knowledge_nuggets=nuggets[:self.max_knowledge_nuggets],
            learned_patterns=patterns[:20],
            deprecated_info=deprecated[:10],
            confidence_decayed=confidence_decayed[:10],
            token_count=sum(len(c.split()) for c in all_content),
            retention_priority=retention_priority,
            metadata=metadata or {},
        )

        with self._lock:
            for nugget in nuggets:
                self._knowledge_index[nugget] = self._knowledge_index.get(nugget, 0.5) * 0.9 + 0.1
            if len(self._summaries) >= 512:
                self._summaries.popitem(last=False)
            self._summaries[summary.summary_id] = summary
            self._total_summaries += 1

        return summary

    def _extract_knowledge_nuggets(
        self, themes: List[str], outcomes: List[str]
    ) -> List[str]:
        """提取可复用知识片段"""
        nuggets = set()
        for t in themes:
            if len(t) > 3:
                nuggets.add(t)
        for o in outcomes:
            if len(o) > 5:
                nuggets.add(o)
        return list(nuggets)

    def _detect_patterns(self, session_summaries: List[SessionSummary]) -> List[str]:
        """检测跨会话的重复模式"""
        theme_count: Dict[str, int] = defaultdict(int)
        for s in session_summaries:
            for t in s.major_themes:
                theme_count[t] += 1

        patterns = [t for t, c in theme_count.items() if c >= 2]
        return patterns

    def _identify_deprecated(
        self, themes: List[str], session_summaries: List[SessionSummary]
    ) -> List[str]:
        """识别可能过时的信息"""
        deprecated = []
        now = time.time()
        for s in session_summaries:
            age_days = (now - s.timestamp) / 86400
            if age_days > 30:
                deprecated.extend(s.major_themes[:3])
        return list(set(deprecated))[:10]

    def _decay_old_confidence(
        self, session_summaries: List[SessionSummary]
    ) -> List[str]:
        decayed = []
        for s in session_summaries:
            if s.quality == SummaryQuality.DRAFT:
                decayed.append(f"Session {s.session_id} (DRAFT) confidence decayed")
        return decayed

    def _compute_retention_priority(
        self, nuggets: List[str], patterns: List[str]
    ) -> float:
        return min(1.0, (len(nuggets) * 0.05 + len(patterns) * 0.15))

    def query_knowledge(self, keyword: str) -> List[str]:
        with self._lock:
            return [k for k in self._knowledge_index if keyword.lower() in k.lower()][:10]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_long_term_summaries": self._total_summaries,
                "knowledge_nuggets": len(self._knowledge_index),
                "cached": len(self._summaries),
            }


# ============================================================================
# SummaryGraphIntegrator — 摘要→图注入器
# ============================================================================

class SummaryGraphIntegrator:
    """
    将各层摘要注入知识图谱节点, 与图检索联动。

    构建三层图结构: 即时层节点→会话层节点→长期层节点, 层间有边相连。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._nodes: Dict[str, GraphNode] = {}
        self._level_index: Dict[SummaryLevel, List[str]] = defaultdict(list)
        self._total_injections: int = 0

    def inject_instant(self, summary: InstantSummary) -> List[GraphNode]:
        """注入即时摘要到图"""
        nodes = []
        for kp in summary.key_points[:5]:
            node = GraphNode(
                node_id=f"node_inst_{hashlib.md5(kp.encode()).hexdigest()[:12]}",
                label=kp[:80],
                summary_ref=summary.summary_id,
                importance=summary.importance_score,
                properties={"level": SummaryLevel.INSTANT.value, "turn": summary.turn_index},
            )
            nodes.append(node)
            with self._lock:
                self._nodes[node.node_id] = node
                self._level_index[SummaryLevel.INSTANT].append(node.node_id)
                self._total_injections += 1
        return nodes

    def inject_session(self, summary: SessionSummary) -> List[GraphNode]:
        """注入会话摘要到图, 连接即时层节点"""
        node = GraphNode(
            node_id=f"node_sess_{summary.summary_id}",
            label=summary.content[:80],
            summary_ref=summary.summary_id,
            importance=0.7,
            edges=list(summary.instant_summary_ids),
            properties={
                "level": SummaryLevel.SESSION.value,
                "themes": summary.major_themes,
            },
        )
        with self._lock:
            self._nodes[node.node_id] = node
            self._level_index[SummaryLevel.SESSION].append(node.node_id)
            self._total_injections += 1
        return [node]

    def inject_long_term(self, summary: LongTermSummary) -> List[GraphNode]:
        """注入长期摘要, 连接会话层节点"""
        nodes = []
        for nugget in summary.knowledge_nuggets[:10]:
            node = GraphNode(
                node_id=f"node_lt_{hashlib.md5(nugget.encode()).hexdigest()[:12]}",
                label=nugget[:80],
                summary_ref=summary.summary_id,
                importance=summary.retention_priority,
                edges=list(summary.session_summary_ids),
                properties={
                    "level": SummaryLevel.LONG_TERM.value,
                    "retention_priority": summary.retention_priority,
                },
            )
            nodes.append(node)
            with self._lock:
                self._nodes[node.node_id] = node
                self._level_index[SummaryLevel.LONG_TERM].append(node.node_id)
                self._total_injections += 1
        return nodes

    def query_graph(
        self, keyword: str, level: Optional[SummaryLevel] = None, top_k: int = 10
    ) -> List[GraphNode]:
        with self._lock:
            candidates = (
                [self._nodes[nid] for nid in self._level_index.get(level, [])]
                if level
                else list(self._nodes.values())
            )
            return [n for n in candidates if keyword.lower() in n.label.lower()][:top_k]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_nodes": len(self._nodes),
                "total_injections": self._total_injections,
                "by_level": {
                    lvl.value: len(ids)
                    for lvl, ids in self._level_index.items()
                },
            }


# ============================================================================
# ContextBudgetManager — 上下文预算管理
# ============================================================================

class ContextBudgetManager:
    """
    根据 token 预算动态选择检索粒度。

    - 预算充裕 → 全量上下文 (FULL_CONTEXT)
    - 预算紧张 → 仅摘要 (SUMMARY_ONLY)
    - 预算极低 → 仅知识图谱 (GRAPH_ONLY)
    - 中等 → 混合模式 (HYBRID)
    """

    def __init__(self, config: Optional[BudgetConfig] = None):
        self.config = config or BudgetConfig()
        self._lock = threading.RLock()
        self._usage_history: List[Dict[str, Any]] = []
        self._granularity_switches: int = 0

    def assess(self, required_tokens: int) -> RetrievalGranularity:
        """评估当前最佳检索粒度"""
        available = self.config.max_tokens - self.config.current_usage
        available -= self.config.reserved_for_response

        if required_tokens <= available:
            return RetrievalGranularity.FULL_CONTEXT
        elif required_tokens * 0.4 <= available:
            return RetrievalGranularity.HYBRID
        elif required_tokens * 0.15 <= available:
            return RetrievalGranularity.SUMMARY_ONLY
        else:
            return RetrievalGranularity.GRAPH_ONLY

    def allocate(self, tokens: int) -> bool:
        """分配 token 预算"""
        with self._lock:
            if self.config.current_usage + tokens > self.config.max_tokens:
                return False
            self.config.current_usage += tokens
            return True

    def release(self, tokens: int) -> None:
        with self._lock:
            self.config.current_usage = max(0, self.config.current_usage - tokens)

    def update_granularity(self, granularity: RetrievalGranularity) -> None:
        with self._lock:
            if self.config.granularity != granularity:
                self.config.granularity = granularity
                self._granularity_switches += 1

    def reset(self) -> None:
        with self._lock:
            self.config.current_usage = 0
            self.config.granularity = RetrievalGranularity.FULL_CONTEXT

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "max_tokens": self.config.max_tokens,
                "current_usage": self.config.current_usage,
                "usage_pct": self.config.current_usage / max(1, self.config.max_tokens) * 100,
                "granularity": self.config.granularity.value,
                "granularity_switches": self._granularity_switches,
            }


# ============================================================================
# CompactionPolicy — 压缩策略引擎
# ============================================================================

class CompactionPolicy:
    """
    压缩策略: 何时压缩、压缩到什么粒度、保留哪些关键细节。

    基于多维度触发条件 (token / 时间 / 会话结束 / 重要性衰减),
    定义可配置的压缩规则集合。
    """

    def __init__(self, default_preserve_ratio: float = 0.3):
        self.default_preserve_ratio = default_preserve_ratio
        self._lock = threading.RLock()
        self._rules: Dict[str, CompactionRule] = {}
        self._compaction_count: int = 0
        self._compaction_history: List[Dict[str, Any]] = []
        self._init_default_rules()

    def _init_default_rules(self):
        """初始化默认压缩规则"""
        defaults = [
            CompactionRule(
                rule_id="token_overload",
                trigger=CompactionTrigger.TOKEN_THRESHOLD,
                source_level=SummaryLevel.INSTANT,
                target_level=SummaryLevel.SESSION,
                threshold_value=6000,
                preserve_ratio=0.3,
                priority=9,
            ),
            CompactionRule(
                rule_id="session_end",
                trigger=CompactionTrigger.SESSION_END,
                source_level=SummaryLevel.INSTANT,
                target_level=SummaryLevel.SESSION,
                threshold_value=0,
                preserve_ratio=0.4,
                priority=10,
            ),
            CompactionRule(
                rule_id="cross_session_merge",
                trigger=CompactionTrigger.TIME_WINDOW,
                source_level=SummaryLevel.SESSION,
                target_level=SummaryLevel.LONG_TERM,
                threshold_value=86400,  # 24h
                preserve_ratio=0.25,
                priority=7,
            ),
            CompactionRule(
                rule_id="importance_decay",
                trigger=CompactionTrigger.IMPORTANCE_DECAY,
                source_level=SummaryLevel.SESSION,
                target_level=SummaryLevel.LONG_TERM,
                threshold_value=0.3,
                preserve_ratio=0.1,
                priority=5,
            ),
            CompactionRule(
                rule_id="token_critical",
                trigger=CompactionTrigger.TOKEN_THRESHOLD,
                source_level=SummaryLevel.SESSION,
                target_level=SummaryLevel.LONG_TERM,
                threshold_value=3000,
                preserve_ratio=0.15,
                priority=8,
            ),
        ]
        for rule in defaults:
            self._rules[rule.rule_id] = rule

    def add_rule(self, rule: CompactionRule) -> None:
        with self._lock:
            self._rules[rule.rule_id] = rule

    def remove_rule(self, rule_id: str):
        with self._lock:
            self._rules.pop(rule_id, None)

    def evaluate_triggers(
        self,
        current_tokens: int,
        session_age_seconds: float,
        importance_scores: List[float],
        is_session_end: bool = False,
    ) -> List[CompactionRule]:
        """
        评估当前状态触发了哪些压缩规则。

        Returns:
            按优先级降序排列的触发规则列表
        """
        triggered = []
        avg_importance = sum(importance_scores) / max(1, len(importance_scores))

        for rule in self._rules.values():
            if not rule.enabled:
                continue
            if rule.trigger == CompactionTrigger.TOKEN_THRESHOLD:
                if current_tokens >= rule.threshold_value:
                    triggered.append(rule)
            elif rule.trigger == CompactionTrigger.TIME_WINDOW:
                if session_age_seconds >= rule.threshold_value:
                    triggered.append(rule)
            elif rule.trigger == CompactionTrigger.SESSION_END:
                if is_session_end:
                    triggered.append(rule)
            elif rule.trigger == CompactionTrigger.IMPORTANCE_DECAY:
                if avg_importance <= rule.threshold_value:
                    triggered.append(rule)

        triggered.sort(key=lambda r: r.priority, reverse=True)

        with self._lock:
            self._compaction_count += len(triggered)
            self._compaction_history.append({
                "timestamp": time.time(),
                "current_tokens": current_tokens,
                "session_age_s": session_age_seconds,
                "avg_importance": avg_importance,
                "triggered_rules": [r.rule_id for r in triggered],
            })

        return triggered

    def get_preserve_details(
        self, keys: List[str], rule: CompactionRule
    ) -> List[str]:
        """
        根据规则决定保留哪些关键细节。

        Args:
            keys: 候选项列表
            rule: 压缩规则
        Returns:
            应保留的项
        """
        preserve_count = max(1, int(len(keys) * rule.preserve_ratio))
        return keys[:preserve_count]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_rules": len(self._rules),
                "enabled_rules": sum(1 for r in self._rules.values() if r.enabled),
                "total_compactions": self._compaction_count,
                "rules": {
                    rid: {"priority": r.priority, "enabled": r.enabled, "trigger": r.trigger.name}
                    for rid, r in self._rules.items()
                },
            }
