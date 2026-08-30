"""
# status: active (2026-09 EXECUTION 172: 大脑方向激活) (2026-09 EXECUTION 163: 保留待激活)
P22-4: Structured Distillation Compressor — 11x 蒸馏压缩 + 96% 召回 MRR

对标论文: Structured Distillation (对话记忆结构化蒸馏, 2026.08)
核心发现: 每次对话压缩为 4 字段复合对象（exchange_core / specific_context /
        thematic_room_assignments / files_touched），实现 11x 压缩比，
        同时保留 96% 召回 MRR（Mean Reciprocal Rank）。
三元语: exchange_core → specific_context → thematic_room → files_touched → 复合对象 → MRR评估

设计要点:
- ExchangeCoreExtractor: 从对话中提取核心交换语义（问题→回答→决策链条）
- SpecificContextTagger: 标记对话的具体上下文（时间/环境/前置条件/约束）
- ThematicRoomAssigner: 将对话分配到一个或多个主题房间（语义空间分区）
- FilesTouchedTracker: 追踪对话中涉及的文件操作（读/写/改/删）
- CompositeObjectBuilder: 构建 4 字段复合压缩对象，11x 压缩
- RecallMRREvaluator: 评估蒸馏后检索的召回 MRR，确保 ≥ 96%
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================


class ExchangeRole(Enum):
    """对话角色"""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class FileOperation(Enum):
    """文件操作类型"""
    READ = "read"
    WRITE = "write"
    MODIFY = "modify"
    DELETE = "delete"
    CREATE = "create"
    COPY = "copy"
    MOVE = "move"


class ThematicRoomType(Enum):
    """主题房间类型"""
    CODING = "coding"
    ANALYSIS = "analysis"
    PLANNING = "planning"
    DEBUGGING = "debugging"
    REVIEW = "review"
    DOCUMENTATION = "documentation"
    CASUAL = "casual"
    RESEARCH = "research"


class DistillationPhase(Enum):
    """蒸馏阶段"""
    EXTRACT_CORE = "extract_core"
    TAG_CONTEXT = "tag_context"
    ASSIGN_ROOMS = "assign_rooms"
    TRACK_FILES = "track_files"
    BUILD_COMPOSITE = "build_composite"
    VALIDATE_MRR = "validate_mrr"


class MRRMetricType(Enum):
    """MRR 评估指标"""
    RECIPROCAL_RANK = "reciprocal_rank"
    PRECISION_AT_K = "precision_at_k"
    RECALL_AT_K = "recall_at_k"
    NDCG = "ndcg"


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass
class ExchangeTurn:
    """对话轮次"""
    turn_id: int
    role: ExchangeRole
    content: str
    timestamp: float = field(default_factory=time.time)
    token_count: int = 0

    def __post_init__(self) -> None:
        if not self.token_count:
            self.token_count = len(self.content.split())


@dataclass
class ExchangeCore:
    """核心交换语义"""
    core_id: str
    user_intent: str
    assistant_response_summary: str
    decision_chain: List[str] = field(default_factory=list)
    outcome: str = ""
    confidence: float = 0.9
    source_turn_ids: List[int] = field(default_factory=list)


@dataclass
class SpecificContext:
    """具体上下文标记"""
    context_id: str
    temporal_context: str = ""          # 时间上下文
    environmental_context: str = ""     # 环境上下文
    preconditions: List[str] = field(default_factory=list)   # 前置条件
    constraints: List[str] = field(default_factory=list)     # 约束条件
    active_window: str = ""             # 当前活跃窗口/应用
    source_turn_ids: List[int] = field(default_factory=list)


@dataclass
class ThematicRoomAssignment:
    """主题房间分配"""
    assignment_id: str
    room_type: ThematicRoomType
    confidence: float = 0.8
    keywords: List[str] = field(default_factory=list)
    sub_topics: List[str] = field(default_factory=list)
    parent_room: Optional[str] = None


@dataclass
class FileTouchRecord:
    """文件接触记录"""
    touch_id: str
    file_path: str
    operation: FileOperation
    description: str = ""
    before_hash: str = ""
    after_hash: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class CompositeDistillate:
    """4 字段复合蒸馏对象

    exchange_core: 核心交换语义
    specific_context: 具体上下文
    thematic_room_assignments: 主题房间分配列表
    files_touched: 文件接触记录列表
    """
    distillate_id: str
    exchange_core: ExchangeCore
    specific_context: SpecificContext
    thematic_room_assignments: List[ThematicRoomAssignment] = field(default_factory=list)
    files_touched: List[FileTouchRecord] = field(default_factory=list)
    original_size_bytes: int = 0
    compressed_size_bytes: int = 0
    compression_ratio: float = 1.0
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.compressed_size_bytes > 0 and self.original_size_bytes > 0:
            self.compression_ratio = self.original_size_bytes / max(self.compressed_size_bytes, 1)


@dataclass
class MRREvaluationResult:
    """MRR 评估结果"""
    eval_id: str
    metric_type: MRRMetricType
    score: float
    threshold: float
    passed: bool
    query_count: int = 0
    detail_scores: List[float] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class DistillationStats:
    """蒸馏统计"""
    turns_processed: int = 0
    distillates_created: int = 0
    total_original_bytes: int = 0
    total_compressed_bytes: int = 0
    mrr_evaluations: int = 0
    mrr_passed: int = 0
    files_tracked: int = 0

    def summary(self) -> Dict[str, Any]:
        ratio = self.total_original_bytes / max(self.total_compressed_bytes, 1)
        return {
            "turns": self.turns_processed,
            "distillates": self.distillates_created,
            "original_bytes": self.total_original_bytes,
            "compressed_bytes": self.total_compressed_bytes,
            "avg_compression_ratio": round(ratio, 1),
            "mrr_evals": self.mrr_evaluations,
            "mrr_passed": self.mrr_passed,
            "files_tracked": self.files_tracked,
        }


# ============================================================================
# Core Classes
# ============================================================================


class ExchangeCoreExtractor:
    """核心交换语义提取器

    从对话轮次中提取 (意图, 回答摘要, 决策链, 结果)，
    构成一条完整的信息交换记录。
    """

    def __init__(self, max_decision_depth: int = 5) -> None:
        self._max_depth = max_decision_depth
        self._lock = threading.RLock()
        self._counter = 0
        self._cores: Dict[str, ExchangeCore] = {}

    def extract(self, turns: List[ExchangeTurn]) -> ExchangeCore:
        """从对话轮次提取核心交换语义"""
        user_turns = [t for t in turns if t.role == ExchangeRole.USER]
        assistant_turns = [t for t in turns if t.role == ExchangeRole.ASSISTANT]

        intent = user_turns[-1].content[:80] if user_turns else "unknown"
        summary = assistant_turns[-1].content[:80] if assistant_turns else "no response"

        # 决策链：简化提取 final answer 前的中间步骤
        decisions: List[str] = []
        for turn in turns[:self._max_depth]:
            snippet = turn.content[:40]
            decisions.append(snippet)

        with self._lock:
            self._counter += 1
        core = ExchangeCore(
            core_id=f"core_{self._counter}",
            user_intent=intent,
            assistant_response_summary=summary,
            decision_chain=decisions,
            outcome=summary,
            source_turn_ids=[t.turn_id for t in turns],
        )
        with self._lock:
            self._cores[core.core_id] = core
        return core

    @property
    def core_count(self) -> int:
        return len(self._cores)


class SpecificContextTagger:
    """具体上下文标记器

    提取对话的时空环境、前置条件、约束条件，
    为后续检索提供精确的上下文过滤维度。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counter = 0
        self._contexts: Dict[str, SpecificContext] = {}

    def tag(self, turns: List[ExchangeTurn], active_window: str = "") -> SpecificContext:
        """标记对话上下文"""
        # 从对话内容推断时间/环境/前置/约束
        all_text = " ".join(t.content for t in turns)

        # 时间上下文
        temporal = "session"
        if "昨天" in all_text or "yesterday" in all_text:
            temporal = "cross-day"
        elif "刚才" in all_text or "just now" in all_text:
            temporal = "same-session"

        # 前置条件
        preconditions: List[str] = []
        for turn in turns:
            if "需要" in turn.content or "先" in turn.content:
                preconditions.append(turn.content[:30])

        # 约束
        constraints: List[str] = []
        for turn in turns:
            if "不能" in turn.content or "限制" in turn.content or "必须" in turn.content:
                constraints.append(turn.content[:30])

        with self._lock:
            self._counter += 1
        ctx = SpecificContext(
            context_id=f"ctx_{self._counter}",
            temporal_context=temporal,
            environmental_context=f"active:{active_window}" if active_window else "default",
            preconditions=preconditions[:5],
            constraints=constraints[:5],
            active_window=active_window,
            source_turn_ids=[t.turn_id for t in turns],
        )
        with self._lock:
            self._contexts[ctx.context_id] = ctx
        return ctx

    @property
    def context_count(self) -> int:
        return len(self._contexts)


class ThematicRoomAssigner:
    """主题房间分配器

    将对话按语义分配到主题房间（如 coding/analysis/planning），
    支持多房间分配（一条对话可属于多个主题）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counter = 0
        self._assignments: Dict[str, List[ThematicRoomAssignment]] = defaultdict(list)
        # 房间关键词映射
        self._room_keywords: Dict[ThematicRoomType, List[str]] = {
            ThematicRoomType.CODING: ["代码", "code", "函数", "class", "def", "import", "bug", "error"],
            ThematicRoomType.ANALYSIS: ["分析", "analysis", "数据", "data", "统计", "图表"],
            ThematicRoomType.PLANNING: ["计划", "plan", "方案", "设计", "架构", "流程"],
            ThematicRoomType.DEBUGGING: ["调试", "debug", "修复", "fix", "报错", "异常"],
            ThematicRoomType.REVIEW: ["审查", "review", "检查", "评估", "评价"],
            ThematicRoomType.DOCUMENTATION: ["文档", "doc", "说明", "README", "注释"],
            ThematicRoomType.RESEARCH: ["研究", "research", "论文", "paper", "实验"],
            ThematicRoomType.CASUAL: [],
        }

    def assign(self, turns: List[ExchangeTurn]) -> List[ThematicRoomAssignment]:
        """分配对话到主题房间"""
        all_text = " ".join(t.content.lower() for t in turns)
        assignments: List[ThematicRoomAssignment] = []
        with self._lock:
            self._counter += 1

        for room_type, keywords in self._room_keywords.items():
            if not keywords:
                continue
            matches = [kw for kw in keywords if kw in all_text]
            if matches:
                confidence = min(1.0, len(matches) / max(len(keywords), 1) * 2)
                assignment = ThematicRoomAssignment(
                    assignment_id=f"assign_{self._counter}_{room_type.value}",
                    room_type=room_type,
                    confidence=confidence,
                    keywords=matches,
                    sub_topics=matches[:3],
                )
                assignments.append(assignment)

        # 如果没有匹配，默认 CASUAL
        if not assignments:
            assignments.append(ThematicRoomAssignment(
                assignment_id=f"assign_{self._counter}_casual",
                room_type=ThematicRoomType.CASUAL,
                confidence=0.5,
            ))

        with self._lock:
            for a in assignments:
                self._assignments[a.assignment_id.split("_")[1]].append(a)
        return assignments


class FilesTouchedTracker:
    """文件接触追踪器

    追踪对话中涉及的所有文件操作（读/写/改/删/创建/复制/移动），
    记录操作类型、文件路径和前后哈希。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counter = 0
        self._records: List[FileTouchRecord] = []

    def track(
        self,
        file_path: str,
        operation: FileOperation,
        description: str = "",
        before_hash: str = "",
        after_hash: str = "",
    ) -> FileTouchRecord:
        """记录一次文件操作"""
        with self._lock:
            self._counter += 1
        record = FileTouchRecord(
            touch_id=f"touch_{self._counter}",
            file_path=file_path,
            operation=operation,
            description=description,
            before_hash=before_hash,
            after_hash=after_hash,
        )
        with self._lock:
            self._records.append(record)
        return record

    def get_touches_by_file(self, file_path: str) -> List[FileTouchRecord]:
        with self._lock:
            return [r for r in self._records if r.file_path == file_path]

    def get_touches_by_operation(self, operation: FileOperation) -> List[FileTouchRecord]:
        with self._lock:
            return [r for r in self._records if r.operation == operation]

    @property
    def record_count(self) -> int:
        return len(self._records)


class CompositeObjectBuilder:
    """复合对象构建器

    将 exchange_core / specific_context / thematic_room_assignments /
    files_touched 四字段组装为 CompositeDistillate 对象，
    计算压缩比（目标 11x）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counter = 0
        self._distillates: Dict[str, CompositeDistillate] = {}

    def build(
        self,
        core: ExchangeCore,
        context: SpecificContext,
        rooms: List[ThematicRoomAssignment],
        files: List[FileTouchRecord],
        original_turns: List[ExchangeTurn],
    ) -> CompositeDistillate:
        """构建 4 字段复合蒸馏对象"""
        original_bytes = sum(t.token_count * 4 for t in original_turns)  # 估算：4 bytes/token

        # 压缩后大小估算
        compressed_bytes = (
            len(core.user_intent) * 4 +
            len(core.assistant_response_summary) * 4 +
            len(context.temporal_context) * 4 +
            len("|".join(context.preconditions)) * 4 +
            len("|".join(r.room_type.value for r in rooms)) * 4 +
            len("|".join(f.file_path for f in files)) * 4
        )

        with self._lock:
            self._counter += 1
        distillate = CompositeDistillate(
            distillate_id=f"distill_{self._counter}",
            exchange_core=core,
            specific_context=context,
            thematic_room_assignments=rooms,
            files_touched=files,
            original_size_bytes=original_bytes,
            compressed_size_bytes=compressed_bytes,
        )
        with self._lock:
            self._distillates[distillate.distillate_id] = distillate
        return distillate

    def get_distillate(self, distillate_id: str) -> Optional[CompositeDistillate]:
        with self._lock:
            return self._distillates.get(distillate_id)

    @property
    def distillate_count(self) -> int:
        return len(self._distillates)

    def average_compression_ratio(self) -> float:
        with self._lock:
            if not self._distillates:
                return 1.0
            ratios = [d.compression_ratio for d in self._distillates.values()]
            return sum(ratios) / len(ratios)


class RecallMRREvaluator:
    """召回 MRR 评估器

    对蒸馏后的检索质量进行评估，确保 Mean Reciprocal Rank ≥ 0.96
    使用标准 MRR 公式：MRR = (1/|Q|) * Σ(1/rank_i)
    """

    def __init__(self, target_mrr: float = 0.96, min_queries: int = 10) -> None:
        self._target = target_mrr
        self._min_queries = min_queries
        self._lock = threading.RLock()
        self._counter = 0
        self._results: List[MRREvaluationResult] = []

    def evaluate(self, query_results: List[Tuple[str, int, bool]]) -> MRREvaluationResult:
        """评估 MRR

        Args:
            query_results: [(query_id, rank_of_correct_answer, is_relevant), ...]
        Returns:
            MRREvaluationResult with MRR score
        """
        if len(query_results) < self._min_queries:
            raise ValueError(f"Need at least {self._min_queries} queries, got {len(query_results)}")

        reciprocal_ranks: List[float] = []
        for _, rank, is_relevant in query_results:
            if is_relevant and rank > 0:
                reciprocal_ranks.append(1.0 / rank)
            else:
                reciprocal_ranks.append(0.0)

        mrr = sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0.0
        passed = mrr >= self._target

        with self._lock:
            self._counter += 1
        result = MRREvaluationResult(
            eval_id=f"mrr_{self._counter}",
            metric_type=MRRMetricType.RECIPROCAL_RANK,
            score=mrr,
            threshold=self._target,
            passed=passed,
            query_count=len(query_results),
            detail_scores=reciprocal_ranks,
        )
        with self._lock:
            self._results.append(result)
        return result

    def precision_at_k(self, relevant_count: int, total_retrieved: int, k: int) -> float:
        """Precision@K"""
        if total_retrieved == 0:
            return 0.0
        return min(relevant_count, k) / min(total_retrieved, k)

    def recall_at_k(self, relevant_found: int, total_relevant: int) -> float:
        """Recall@K"""
        if total_relevant == 0:
            return 0.0
        return min(relevant_found, total_relevant) / total_relevant

    @property
    def current_mrr(self) -> float:
        with self._lock:
            if not self._results:
                return 0.0
            return self._results[-1].score

    @property
    def is_above_threshold(self) -> bool:
        return self.current_mrr >= self._target


class StructuredDistillationCompressor:
    """结构化蒸馏压缩器 — 顶层编排器

    完整蒸馏管线：核心提取 → 上下文标记 → 主题分配 →
    文件追踪 → 复合对象构建 → MRR 评估。
    目标：11x 压缩比 + 96% 召回 MRR。
    """

    def __init__(
        self,
        core_extractor: Optional[ExchangeCoreExtractor] = None,
        context_tagger: Optional[SpecificContextTagger] = None,
        room_assigner: Optional[ThematicRoomAssigner] = None,
        file_tracker: Optional[FilesTouchedTracker] = None,
        builder: Optional[CompositeObjectBuilder] = None,
        mrr_evaluator: Optional[RecallMRREvaluator] = None,
    ) -> None:
        self.core = core_extractor or ExchangeCoreExtractor()
        self.context = context_tagger or SpecificContextTagger()
        self.rooms = room_assigner or ThematicRoomAssigner()
        self.files = file_tracker or FilesTouchedTracker()
        self.builder = builder or CompositeObjectBuilder()
        self.mrr = mrr_evaluator or RecallMRREvaluator()
        self._lock = threading.RLock()
        self._stats = DistillationStats()

    def distill(
        self,
        turns: List[ExchangeTurn],
        active_window: str = "",
        file_operations: Optional[List[Tuple[str, FileOperation, str]]] = None,
    ) -> CompositeDistillate:
        """执行完整蒸馏管线"""
        # Phase 1: 核心交换提取
        core = self.core.extract(turns)

        # Phase 2: 上下文标记
        ctx = self.context.tag(turns, active_window=active_window)

        # Phase 3: 主题房间分配
        assignments = self.rooms.assign(turns)

        # Phase 4: 文件追踪
        file_records: List[FileTouchRecord] = []
        if file_operations:
            for path, op, desc in file_operations:
                record = self.files.track(path, op, description=desc)
                file_records.append(record)

        # Phase 5: 复合对象构建
        distillate = self.builder.build(core, ctx, assignments, file_records, turns)

        # 更新统计
        with self._lock:
            self._stats.turns_processed += len(turns)
            self._stats.distillates_created += 1
            self._stats.total_original_bytes += distillate.original_size_bytes
            self._stats.total_compressed_bytes += distillate.compressed_size_bytes
            self._stats.files_tracked += len(file_records)

        return distillate

    def evaluate_quality(self, query_results: List[Tuple[str, int, bool]]) -> MRREvaluationResult:
        """评估蒸馏质量（MRR）"""
        result = self.mrr.evaluate(query_results)
        with self._lock:
            self._stats.mrr_evaluations += 1
            if result.passed:
                self._stats.mrr_passed += 1
        return result

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标"""
        return {
            "module": "Structured_Distillation",
            "turns_processed": self._stats.turns_processed,
            "distillates": self._stats.distillates_created,
            "avg_compression": self.builder.average_compression_ratio(),
            "target_compression": "11x",
            "current_mrr": self.mrr.current_mrr,
            "target_mrr": "0.96",
            "mrr_above_threshold": self.mrr.is_above_threshold,
            "stats": self._stats.summary(),
        }


# ============================================================================
# Module-level statistics
# ============================================================================


def statistics() -> Dict[str, Any]:
    """模块级运行时指标"""
    return {
        "module": "structured_distillation_compressor",
        "class_count": 6,
        "target_compression_ratio": "11x",
        "target_recall_mrr": "0.96",
        "distillate_fields": ["exchange_core", "specific_context", "thematic_room_assignments", "files_touched"],
        "supported_file_operations": [op.value for op in FileOperation],
        "thematic_rooms": [r.value for r in ThematicRoomType],
    }
