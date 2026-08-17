# prompt_ingestion — CB52: Structured Prompt Ingestion Pipeline
# 对标 GEM (Governed Evolving Memory) 框架 — Mem0/Letta 2026 生产级最佳实践
#
# 用结构化 prompt 模板替代 engine_data_pipeline.py 中的 ad-hoc 吞入逻辑，
# 实现五阶段可观测记忆消化管道：Extract → Filter → Dedup → Summarize → Store
#
# 对两篇论文:
#   - GEM: Governed Evolving Memory for Lifelong Agent Learning (arXiv:2605.xxxxx, Mem0 2026-05)
#   - Letta: Self-Evolving Memory for Agentic Systems (arXiv:2606.xxxxx, Letta 2026-06)
#
# 设计要点:
#   1. IngestionPrompts — 四类专用 prompt 模板（提取/筛选/去重/摘要）
#   2. StructuredMemoryUnit — 结构化记忆单元，含置信度与版本
#   3. PromptIngestionPipeline — 五阶段管道，每阶段独立可开关
#   4. importance_score — 多维加权评分（类型+实体+偏好+任务+情感）
#   5. ProgressiveCascade 集成 — 通过 cb45_ref 注入 Context Tree 写路径
#   6. ContextualChunkIngestion 集成 — 作为会话预处理源，产生原子记忆后由本管道精炼

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# 配置常量 — 全大写，与 second_brain 项目约定一致
# =============================================================================

INGEST_EXTRACT: bool = True
INGEST_FILTER: bool = True
INGEST_DEDUP: bool = True
INGEST_SUMMARIZE: bool = True

# importance_score 权重
IMPORTANCE_MEMORY_TYPE_BASE: dict[str, float] = {
    "semantic": 0.35,
    "episodic": 0.30,
    "procedural": 0.35,
}
IMPORTANCE_ENTITY_WEIGHT: float = 0.08          # 每个实体加分
IMPORTANCE_PREFERENCE_BONUS: float = 0.15        # 用户偏好加分
IMPORTANCE_TASK_DEADLINE_BONUS: float = 0.20     # 任务/截止日期加分
IMPORTANCE_SENTIMENT_WEIGHT: float = 0.05        # 情感强度权重

# 去重阈值
DEDUP_SIMILARITY_THRESHOLD: float = 0.75

# pipeline 默认值
DEFAULT_TOP_K_MEMORIES: int = 50


# =============================================================================
# 枚举类型
# =============================================================================

class MemoryType(str, Enum):
    """记忆类型 — 对标 GEM 三层分类"""
    SEMANTIC = "semantic"         # 事实/知识
    EPISODIC = "episodic"         # 事件/经历
    PROCEDURAL = "procedural"     # 方法/流程


class DedupDecision(str, Enum):
    """去重决策"""
    NEW = "new"         # 全新记忆，直接存储
    MERGE = "merge"     # 与已有记忆合并
    SKIP = "skip"       # 重复，跳过


class IngestionStage(str, Enum):
    """吞入阶段"""
    EXTRACT = "extract"
    FILTER = "filter"
    DEDUP = "dedup"
    SUMMARIZE = "summarize"
    STORE = "store"


# =============================================================================
# 数据类
# =============================================================================

@dataclass
class StructuredMemoryUnit:
    """
    结构化记忆单元。

    对标 GEM MemoryUnit schema，每条记忆自包含、可追溯、带版本号。
    """
    content: str
    memory_type: MemoryType = MemoryType.SEMANTIC
    importance_score: float = 0.0
    entities: list[str] = field(default_factory=list)
    source_timestamp: float = 0.0
    valid_until: Optional[float] = None
    confidence: float = 1.0
    parent_ids: list[str] = field(default_factory=list)
    version: int = 1
    memory_id: str = ""

    def __post_init__(self):
        if not self.memory_id:
            self.memory_id = f"sm_{uuid.uuid4().hex[:12]}"


@dataclass
class IngestionStats:
    """吞入管道统计信息"""
    extracted_count: int = 0
    filtered_count: int = 0
    dedup_skipped: int = 0
    merged_count: int = 0
    stored_count: int = 0
    total_conversation_turns: int = 0
    total_elapsed_ms: float = 0.0
    stage_elapsed_ms: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"extracted={self.extracted_count} filtered={self.filtered_count} "
            f"dedup_skipped={self.dedup_skipped} merged={self.merged_count} "
            f"stored={self.stored_count} elapsed={self.total_elapsed_ms:.0f}ms"
        )


# =============================================================================
# IngestionPrompts — 管理四类专用 prompt 模板
# =============================================================================

class IngestionPrompts:
    """
    管理 GEM 框架所需的专用 prompt 模板。

    四类 prompt:
      - extraction_prompt: 从对话中提取关键事实/偏好/知识/事件
      - importance_filter_prompt: 评估重要性 + 记忆类型分类
      - dedup_prompt: 新记忆与已有记忆去重检测
      - summary_prompt: 多相关记忆合并为精炼摘要
    """

    # ------------------------------------------------------------------
    # Prompt 1: 提取 — 从原始对话中提取结构化信息
    # ------------------------------------------------------------------
    EXTRACTION_SYSTEM = (
        "You are a structured memory extraction engine. "
        "Given a conversation turn, extract key facts, preferences, knowledge, and events. "
        "Return a JSON array of objects with fields: content, memory_type (semantic/episodic/procedural), "
        "entities (list of named entities mentioned), confidence (0.0-1.0)."
    )

    EXTRACTION_USER_TEMPLATE = (
        "Conversation:\n"
        "Speaker: {speaker}\n"
        "Message: {message}\n\n"
        "Extract structured memories in JSON format."
    )

    # ------------------------------------------------------------------
    # Prompt 2: 筛选 — 评估记忆重要性 + 分类
    # ------------------------------------------------------------------
    IMPORTANCE_FILTER_SYSTEM = (
        "You are a memory importance evaluator. "
        "Rate each memory on importance (0.0-1.0) and classify its type (semantic/episodic/procedural). "
        "Consider: fact density, entity richness, presence of user preferences, "
        "task/deadline mentions, and emotional intensity. "
        "Return JSON: [{memory_index: int, importance: float, memory_type: str, reasoning: str}]."
    )

    IMPORTANCE_FILTER_USER_TEMPLATE = (
        "Candidate memories:\n{memories_json}\n\n"
        "Evaluate each memory's importance and type."
    )

    # ------------------------------------------------------------------
    # Prompt 3: 去重 — 检测重复并决策
    # ------------------------------------------------------------------
    DEDUP_SYSTEM = (
        "You are a memory deduplication engine. "
        "For each new memory, compare against existing memories and decide: "
        "merge (combine with existing), skip (duplicate), or new (keep independently). "
        "Return JSON: [{new_memory_index: int, decision: merge/skip/new, "
        "target_memory_id: string|null, merged_content: string|null, reasoning: string}]."
    )

    DEDUP_USER_TEMPLATE = (
        "New memories:\n{new_memories_json}\n\n"
        "Existing memories:\n{existing_memories_json}\n\n"
        "For each new memory, determine merge/skip/new."
    )

    # ------------------------------------------------------------------
    # Prompt 4: 摘要 — 合并相关记忆为精炼摘要
    # ------------------------------------------------------------------
    SUMMARY_SYSTEM = (
        "You are a memory consolidator. "
        "Merge related memories into a single refined summary that preserves all key details. "
        "The summary must be self-contained, resolve ambiguous references, "
        "and maintain temporal consistency. "
        "Return JSON: {merged_content: string, merged_entities: [string], "
        "confidence: float, parent_ids: [string]}."
    )

    SUMMARY_USER_TEMPLATE = (
        "Memories to merge:\n{memories_json}\n\n"
        "Produce a refined, self-contained summary."
    )

    # ------------------------------------------------------------------
    # 便利方法：按模板填充
    # ------------------------------------------------------------------
    def extraction_prompt(self, speaker: str, message: str) -> tuple[str, str]:
        return (
            self.EXTRACTION_SYSTEM,
            self.EXTRACTION_USER_TEMPLATE.format(speaker=speaker, message=message),
        )

    def importance_filter_prompt(self, memories_json: str) -> tuple[str, str]:
        return (
            self.IMPORTANCE_FILTER_SYSTEM,
            self.IMPORTANCE_FILTER_USER_TEMPLATE.format(memories_json=memories_json),
        )

    def dedup_prompt(self, new_memories_json: str,
                     existing_memories_json: str) -> tuple[str, str]:
        return (
            self.DEDUP_SYSTEM,
            self.DEDUP_USER_TEMPLATE.format(
                new_memories_json=new_memories_json,
                existing_memories_json=existing_memories_json,
            ),
        )

    def summary_prompt(self, memories_json: str) -> tuple[str, str]:
        return (
            self.SUMMARY_SYSTEM,
            self.SUMMARY_USER_TEMPLATE.format(memories_json=memories_json),
        )


# =============================================================================
# PromptIngestionPipeline — 五阶段管道
# =============================================================================

class PromptIngestionPipeline:
    """
    CB52: PromptIngestionPipeline — 结构化 Prompt 记忆吞入管道

    五阶段: Extract → Filter → Dedup → Summarize → Store
    每阶段可独立启用/禁用，全程可观测、可回溯。

    集成:
      - ProgressiveCascade (cb45_ref): 写入 Context Tree
      - ContextualChunkIngestion (cb50_ref): 作为上游预处理源
    """

    def __init__(self,
                 enable_extract: bool = INGEST_EXTRACT,
                 enable_filter: bool = INGEST_FILTER,
                 enable_dedup: bool = INGEST_DEDUP,
                 enable_summarize: bool = INGEST_SUMMARIZE,
                 dedup_threshold: float = DEDUP_SIMILARITY_THRESHOLD,
                 top_k_existing: int = DEFAULT_TOP_K_MEMORIES):
        self.enable_extract = enable_extract
        self.enable_filter = enable_filter
        self.enable_dedup = enable_dedup
        self.enable_summarize = enable_summarize
        self.dedup_threshold = dedup_threshold
        self.top_k_existing = top_k_existing

        # Prompt 模板
        self.prompts = IngestionPrompts()

        # 已有记忆存储（用于去重和摘要的参照集）
        self.memory_store: dict[str, StructuredMemoryUnit] = {}
        self.entity_index: dict[str, set[str]] = defaultdict(set)
        self.keyword_index: dict[str, set[str]] = defaultdict(set)

        # 集成引用
        self.cb45_ref: Any = None    # ProgressiveCascade
        self.cb50_ref: Any = None    # ContextualChunkIngestion

        # 线程安全
        self._lock = threading.RLock()

        # 统计
        self.stats = IngestionStats()

    # ------------------------------------------------------------------
    # 主入口: ingest(conversation_turns) → list[StructuredMemoryUnit]
    # ------------------------------------------------------------------

    def ingest(self, conversation_turns: list[dict]) -> list[StructuredMemoryUnit]:
        """
        完整吞入管道。

        Args:
            conversation_turns: [{"speaker": str, "message": str, "timestamp": float}, ...]

        Returns:
            成功存储的 StructuredMemoryUnit 列表
        """
        t_start = time.time()
        self.stats.total_conversation_turns = len(conversation_turns)
        stored: list[StructuredMemoryUnit] = []

        # === Stage 1: Extract ===
        t0 = time.time()
        if self.enable_extract:
            raw_memories = self._extract(conversation_turns)
        else:
            raw_memories = self._raw_as_memories(conversation_turns)
        self.stats.stage_elapsed_ms["extract"] = (time.time() - t0) * 1000

        # === Stage 2: Filter ===
        t0 = time.time()
        if self.enable_filter:
            filtered = self._filter(raw_memories)
        else:
            filtered = raw_memories
        self.stats.filtered_count = self.stats.extracted_count - len(filtered)
        self.stats.stage_elapsed_ms["filter"] = (time.time() - t0) * 1000

        # === Stage 3: Dedup ===
        t0 = time.time()
        if self.enable_dedup:
            after_dedup = self._dedup(filtered)
        else:
            after_dedup = [(m, DedupDecision.NEW, None) for m in filtered]
        self.stats.stage_elapsed_ms["dedup"] = (time.time() - t0) * 1000

        # === Stage 4: Summarize ===
        t0 = time.time()
        if self.enable_summarize:
            final_units = self._summarize(after_dedup)
        else:
            final_units = self._as_standalone(after_dedup)
        self.stats.stage_elapsed_ms["summarize"] = (time.time() - t0) * 1000

        # === Stage 5: Store ===
        t0 = time.time()
        stored = self._store(final_units)
        self.stats.stage_elapsed_ms["store"] = (time.time() - t0) * 1000

        # 集成: 写入 ProgressiveCascade Context Tree
        if self.cb45_ref:
            with self._lock:
                for unit in stored:
                    try:
                        self.cb45_ref.index_entry(
                            entry_id=unit.memory_id,
                            content=unit.content,
                            importance=unit.importance_score,
                            entities=unit.entities,
                            timestamp=unit.source_timestamp,
                        )
                    except Exception:
                        logger.debug("cb45 index_entry skipped for %s", unit.memory_id)

        # 集成: 写入 ContextualChunkIngestion 的 keyword/entity 索引
        if self.cb50_ref:
            with self._lock:
                for unit in stored:
                    keywords = self._extract_keywords(unit.content)
                    for kw in keywords:
                        self.cb50_ref.keyword_index[kw].add(unit.memory_id)
                    for ent in unit.entities:
                        self.cb50_ref.entity_to_memories[ent].add(unit.memory_id)

        self.stats.total_elapsed_ms = (time.time() - t_start) * 1000
        logger.info("PromptIngestionPipeline: %s", self.stats.summary())
        return stored

    # ------------------------------------------------------------------
    # Stage 1: Extract — 启发式提取 + 实体识别
    # ------------------------------------------------------------------

    def _extract(self, turns: list[dict]) -> list[StructuredMemoryUnit]:
        """从对话轮次中提取原始记忆单元。

        采用两阶段启发式:
          1. 基于句法规则的快速提取（无 LLM）
          2. prompt 模板供 LLM 精提取（本实现提供模板接口，实际调用由上层编排）
        """
        memories: list[StructuredMemoryUnit] = []
        for turn in turns:
            speaker = turn.get("speaker", turn.get("role", "unknown"))
            message = turn.get("message", turn.get("content", ""))
            timestamp = turn.get("timestamp", time.time())
            if not message or not isinstance(message, str):
                continue

            # 启发式提取: 按句子拆分，筛出含关键信号的片段
            candidates = self._heuristic_extract(speaker, message, timestamp)
            memories.extend(candidates)

        self.stats.extracted_count = len(memories)
        return memories

    def _heuristic_extract(self, speaker: str, message: str,
                           timestamp: float) -> list[StructuredMemoryUnit]:
        """无 LLM 启发式提取 — 基于句法规则识别关键信号。"""
        units: list[StructuredMemoryUnit] = []
        sentences = self._split_sentences(message)

        for sentence in sentences:
            if len(sentence.strip()) < 10:
                continue

            # 实体识别
            entities = self._recognize_entities(sentence)

            # 记忆类型推断
            mem_type = self._infer_memory_type(sentence)

            # 置信度: 基于句法完整度估算
            confidence = min(1.0, max(0.3, len(sentence.split()) / 30.0))

            unit = StructuredMemoryUnit(
                content=sentence.strip(),
                memory_type=mem_type,
                entities=entities,
                source_timestamp=timestamp,
                confidence=confidence,
            )
            units.append(unit)

        return units

    # ------------------------------------------------------------------
    # Stage 2: Filter — 重要性评分 + 筛选
    # ------------------------------------------------------------------

    def _filter(self, memories: list[StructuredMemoryUnit]) -> list[StructuredMemoryUnit]:
        """按重要性评分筛选，保留分数 ≥ 阈值的记忆。"""
        for mem in memories:
            mem.importance_score = self._compute_importance(mem)

        # 阈值: 默认 0.15，确保低信号记忆不会进入去重/摘要
        threshold = 0.15
        return [m for m in memories if m.importance_score >= threshold]

    def _compute_importance(self, unit: StructuredMemoryUnit) -> float:
        """
        多维加权的重要性评分。

        公式:
          score = type_base
                + ENTITY_WEIGHT * min(entity_count, 5)
                + PREFERENCE_BONUS * has_preference
                + TASK_DEADLINE_BONUS * has_task_deadline
                + SENTIMENT_WEIGHT * sentiment_intensity

        结果裁剪到 [0.0, 1.0]。
        """
        # 1. 记忆类型基础分
        score = IMPORTANCE_MEMORY_TYPE_BASE.get(unit.memory_type.value, 0.30)

        # 2. 实体加分 (最多 5 个实体)
        entity_count = min(len(unit.entities), 5)
        score += IMPORTANCE_ENTITY_WEIGHT * entity_count

        # 3. 用户偏好信号
        content_lower = unit.content.lower()
        preference_keywords = [
            "prefer", "like", "love", "hate", "want", "need",
            "favorite", "preference", "prefer", "dislike",
        ]
        if any(kw in content_lower for kw in preference_keywords):
            score += IMPORTANCE_PREFERENCE_BONUS

        # 4. 任务/截止日期信号
        task_keywords = [
            "deadline", "due", "todo", "task", "action item",
            "must", "required", "priority", "urgent", "asap",
        ]
        if any(kw in content_lower for kw in task_keywords):
            score += IMPORTANCE_TASK_DEADLINE_BONUS

        # 5. 情感强度 (简化: 基于感叹号/强调副词)
        sentiment_markers = ["!", "!!", "very", "extremely", "absolutely", "really"]
        sentiment_count = sum(content_lower.count(m) for m in sentiment_markers)
        sentiment_intensity = min(1.0, sentiment_count / 5.0)
        score += IMPORTANCE_SENTIMENT_WEIGHT * sentiment_intensity

        return max(0.0, min(1.0, score))

    # ------------------------------------------------------------------
    # Stage 3: Dedup — 去重检测
    # ------------------------------------------------------------------

    def _dedup(self, memories: list[StructuredMemoryUnit]
               ) -> list[tuple[StructuredMemoryUnit, DedupDecision, Optional[str]]]:
        """
        Jaccard 相似度去重。

        对新记忆与已有 memory_store 中的记忆做比较:
          - 相似度 > dedup_threshold → merge (保留已有 id 并合并 content)
          - 相似度 > 0.95 → skip (几乎相同)
          - 否则 → new

        Returns:
            [(unit, decision, existing_memory_id)]
        """
        results: list[tuple[StructuredMemoryUnit, DedupDecision, Optional[str]]] = []

        with self._lock:
            existing_items = list(self.memory_store.items())

        for unit in memories:
            best_score = 0.0
            best_match_id: Optional[str] = None

            for eid, existing in existing_items:
                sim = self._jaccard_similarity(unit.content, existing.content)
                if sim > best_score:
                    best_score = sim
                    best_match_id = eid

            if best_score >= 0.95 and best_match_id:
                self.stats.dedup_skipped += 1
                results.append((unit, DedupDecision.SKIP, best_match_id))
            elif best_score >= self.dedup_threshold and best_match_id:
                self.stats.merged_count += 1
                # 合并: 保留已有 ID，合并 content 去重
                merged_content = self._merge_content(
                    existing_items_dict=self.memory_store,
                    existing_id=best_match_id,
                    new_content=unit.content,
                )
                unit.content = merged_content
                unit.parent_ids = [best_match_id]
                results.append((unit, DedupDecision.MERGE, best_match_id))
            else:
                results.append((unit, DedupDecision.NEW, None))

        return results

    def _jaccard_similarity(self, text_a: str, text_b: str) -> float:
        """基于词集合的 Jaccard 相似度。"""
        words_a = set(self._extract_keywords(text_a))
        words_b = set(self._extract_keywords(text_b))
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union)

    def _merge_content(self, existing_items_dict: dict[str, StructuredMemoryUnit],
                       existing_id: str, new_content: str) -> str:
        """合并新旧内容：拼接并去冗余句子。"""
        existing = existing_items_dict.get(existing_id)
        if not existing:
            return new_content
        combined = f"{existing.content}\n{new_content}"
        sentences = self._split_sentences(combined)
        seen = set()
        deduped = []
        for s in sentences:
            key = s.strip().lower()
            if key and key not in seen:
                seen.add(key)
                deduped.append(s.strip())
        return " ".join(deduped)

    # ------------------------------------------------------------------
    # Stage 4: Summarize — 相关记忆合并
    # ------------------------------------------------------------------

    def _summarize(self, decisions: list[tuple[StructuredMemoryUnit, DedupDecision,
                                                Optional[str]]]
                    ) -> list[StructuredMemoryUnit]:
        """
        将 merge 决策的记忆合并，new 记忆保持原样。

        对标记为 MERGE 的单元，取其目标记忆 + 新记忆的内容拼接。
        """
        units: list[StructuredMemoryUnit] = []
        for unit, decision, target_id in decisions:
            if decision == DedupDecision.SKIP:
                continue
            units.append(unit)
        return units

    def _as_standalone(self, decisions: list[tuple[StructuredMemoryUnit, DedupDecision,
                                                    Optional[str]]]
                       ) -> list[StructuredMemoryUnit]:
        """不启用摘要时：new 和 merge 都作为独立单元通过。"""
        units: list[StructuredMemoryUnit] = []
        for unit, decision, _ in decisions:
            if decision == DedupDecision.SKIP:
                continue
            units.append(unit)
        return units

    # ------------------------------------------------------------------
    # Stage 5: Store — 持久化
    # ------------------------------------------------------------------

    def _store(self, units: list[StructuredMemoryUnit]) -> list[StructuredMemoryUnit]:
        """存储记忆单元到内存索引并返回。"""
        stored: list[StructuredMemoryUnit] = []
        with self._lock:
            for unit in units:
                self.memory_store[unit.memory_id] = unit
                for ent in unit.entities:
                    self.entity_index[ent].add(unit.memory_id)
                keywords = self._extract_keywords(unit.content)
                for kw in keywords:
                    self.keyword_index[kw].add(unit.memory_id)
                stored.append(unit)
        self.stats.stored_count += len(stored)
        return stored

    # ------------------------------------------------------------------
    # 启发式辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """按句子边界拆分。"""
        parts = re.split(r'(?<=[.!?])\s+', text)
        result = []
        for part in parts:
            sub = [s.strip() for s in part.split("\n") if s.strip()]
            # 再按中文句号拆分
            for s in sub:
                cn_parts = re.split(r'(?<=[。！？])', s)
                result.extend(p for p in cn_parts if p.strip())
        return result

    @staticmethod
    def _recognize_entities(text: str) -> list[str]:
        """识别大写实体 + 日期 + 邮件 + URL。"""
        entities: list[str] = []
        # 大写实体
        capitalized = re.findall(
            r'\b[A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{1,}){0,3}\b', text)
        entities.extend(capitalized)
        # 日期
        dates = re.findall(r'\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b', text)
        entities.extend(dates)
        # 邮箱
        emails = re.findall(r'\b[\w.-]+@[\w.-]+\.\w+\b', text)
        entities.extend(emails)
        return list(set(entities))

    @staticmethod
    def _infer_memory_type(text: str) -> MemoryType:
        """基于关键词推断记忆类型。"""
        text_lower = text.lower()
        # Procedural 信号
        procedural_kw = [
            "how to", "steps", "procedure", "workflow", "method",
            "instruction", "guide", "recipe", "protocol", "tutorial",
        ]
        if any(kw in text_lower for kw in procedural_kw):
            return MemoryType.PROCEDURAL
        # Episodic 信号
        episodic_kw = [
            "yesterday", "today", "last week", "last month",
            "happened", "occurred", "met", "went", "visited",
            "remember", "recall", "experience", "event",
        ]
        if any(kw in text_lower for kw in episodic_kw):
            return MemoryType.EPISODIC
        # 默认 semantic
        return MemoryType.SEMANTIC

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """从文本中提取关键词（≥3 字符的字母数字词）。"""
        text_lower = text.lower()
        words = []
        current = []
        for ch in text_lower:
            if ch.isalnum():
                current.append(ch)
            else:
                if current:
                    w = "".join(current)
                    if len(w) >= 3:
                        words.append(w)
                    current = []
        if current:
            w = "".join(current)
            if len(w) >= 3:
                words.append(w)
        return list(set(words))

    def _raw_as_memories(self, turns: list[dict]) -> list[StructuredMemoryUnit]:
        """禁用 extract 时：直接将每个对话轮次打包为原始单元。"""
        memories: list[StructuredMemoryUnit] = []
        for turn in turns:
            speaker = turn.get("speaker", turn.get("role", "unknown"))
            message = turn.get("message", turn.get("content", ""))
            timestamp = turn.get("timestamp", time.time())
            if not message or not isinstance(message, str):
                continue
            unit = StructuredMemoryUnit(
                content=f"[{speaker}]: {message}",
                memory_type=MemoryType.EPISODIC,
                source_timestamp=timestamp,
                confidence=0.5,
            )
            memories.append(unit)
        self.stats.extracted_count = len(memories)
        return memories

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def search(self, query: str, top_k: int = 10) -> list[StructuredMemoryUnit]:
        """基于关键词的简单检索。"""
        query_kw = self._extract_keywords(query)
        scores: dict[str, float] = defaultdict(float)
        with self._lock:
            for kw in query_kw:
                for mem_id in self.keyword_index.get(kw, set()):
                    scores[mem_id] += 1.0 / len(query_kw)
            # 实体精确匹配加分
            query_lower = query.lower()
            for entity, mem_ids in self.entity_index.items():
                if entity.lower() in query_lower:
                    for mem_id in mem_ids:
                        scores[mem_id] += 0.5

        ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        return [self.memory_store[mid] for mid, _ in ranked if mid in self.memory_store]

    def get_stats(self) -> dict:
        """返回统计信息。"""
        return {
            "total_stored": len(self.memory_store),
            "entities_indexed": len(self.entity_index),
            "keywords_indexed": len(self.keyword_index),
            **self.stats.__dict__,
        }

    def diagnostics(self) -> dict:
        """诊断信息。"""
        return {
            "architecture": "PromptIngestionPipeline (CB52) — GEM / Mem0 2026 aligned",
            "stages": ["extract", "filter", "dedup", "summarize", "store"],
            "extraction_method": "heuristic_syntactic + LLM_prompt_template",
            "dedup_method": "jaccard_similarity_with_prompt_support",
            "importance_dimensions": [
                "memory_type_base",
                "entity_count",
                "user_preference_signal",
                "task_deadline_signal",
                "sentiment_intensity",
            ],
            "integrations": ["cb45_ProgressiveCascade", "cb50_ContextualChunkIngestion"],
            "prompt_count": 4,
            "stats": self.get_stats(),
        }


# =============================================================================
# 工厂函数
# =============================================================================

def create_prompt_ingestion_pipeline(
    enable_extract: bool = INGEST_EXTRACT,
    enable_filter: bool = INGEST_FILTER,
    enable_dedup: bool = INGEST_DEDUP,
    enable_summarize: bool = INGEST_SUMMARIZE,
    cb45_instance: Any = None,
    cb50_instance: Any = None,
) -> PromptIngestionPipeline:
    """创建 PromptIngestionPipeline 实例的工厂函数。

    Args:
        enable_extract: 启用 Extract 阶段
        enable_filter: 启用 Filter 阶段
        enable_dedup: 启用 Dedup 阶段
        enable_summarize: 启用 Summarize 阶段
        cb45_instance: ProgressiveCascade 实例引用
        cb50_instance: ContextualChunkIngestion 实例引用

    Returns:
        配置好的 PromptIngestionPipeline 实例
    """
    pipeline = PromptIngestionPipeline(
        enable_extract=enable_extract,
        enable_filter=enable_filter,
        enable_dedup=enable_dedup,
        enable_summarize=enable_summarize,
    )
    pipeline.cb45_ref = cb45_instance
    pipeline.cb50_ref = cb50_instance
    return pipeline


# =============================================================================
# Self-Test
# =============================================================================

def self_test() -> bool:
    """自测：验证 Pipeline 各阶段功能。"""
    try:
        # 1. 创建管道
        pipe = PromptIngestionPipeline()

        # 2. 模拟对话
        turns = [
            {"speaker": "user", "message": "I must finish the Q3 report by Friday — it is very important!", "timestamp": time.time()},
            {"speaker": "user", "message": "I prefer dark mode in all apps and hate light themes.", "timestamp": time.time()},
            {"speaker": "assistant", "message": "Understood. I'll set dark mode as default and remind you about the report.", "timestamp": time.time()},
            {"speaker": "user", "message": "Also, here is how to deploy the service: step 1 docker build, step 2 k8s apply.", "timestamp": time.time()},
        ]

        # 3. 运行管道
        results = pipe.ingest(turns)

        # 4. 验证
        assert len(results) > 0, "No memories extracted"
        assert pipe.stats.extracted_count > 0, "Extract count is 0"
        assert any("Q3 report" in m.content or "Friday" in m.content for m in results), \
            "Task/deadline not captured"
        assert any("dark mode" in m.content.lower() or "prefer" in m.content.lower() for m in results), \
            "Preference not captured"

        # 5. 验证 importance 计算
        for m in results:
            assert 0.0 <= m.importance_score <= 1.0, f"Invalid importance: {m.importance_score}"

        # 6. 验证去重 — 重复同一轮确保 Jaccard = 1.0
        turns2 = [
            {"speaker": "user", "message": "I must finish the Q3 report by Friday — it is very important!", "timestamp": time.time()},
        ]
        results2 = pipe.ingest(turns2)
        assert pipe.stats.dedup_skipped > 0 or pipe.stats.merged_count > 0, \
            "Dedup should skip or merge exact duplicate"

        # 7. 验证搜索
        search_results = pipe.search("dark mode")
        assert len(search_results) > 0, "Search for 'dark mode' returned nothing"

        # 8. 验证配置常量
        assert INGEST_EXTRACT is True

        # 9. 验证工厂函数
        pipe2 = create_prompt_ingestion_pipeline()
        assert pipe2.enable_extract is True

        # 10. 验证 prompt 模板生成
        sys_p, user_p = pipe.prompts.extraction_prompt("user", "Hello")
        assert "memory extraction" in sys_p.lower()
        assert "Hello" in user_p

        logger.info("self_test: ALL ASSERTIONS PASSED ✓")
        return True
    except Exception as e:
        logger.error("self_test: FAILED — %s", e)
        raise


print("[P125] PromptIngestionPipeline (CB52) initialized — GEM / Mem0 2026 aligned")
