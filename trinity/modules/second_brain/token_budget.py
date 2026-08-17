"""
P4-4: Token Budget + Ebbinghaus Decay (对标 Mem0 May 2026)
============================================================

硬 Token 上限控制 + 艾宾浩斯遗忘曲线过滤 + 分层摘要压缩。
确保记忆注入 Token 成本可预测且不随记忆库增长而膨胀。

Mem0 Token Budgeting (May 2026) 设计要点：
  - 硬 Token 上限: 设置上下文记忆注入的最大 token 数，严格控制成本
  - 分层摘要压缩: 将检索到的记忆按重要性分层压缩（完整→摘要→标题→丢弃）
  - 艾宾浩斯遗忘曲线: 根据时间间隔衰减记忆权重
    R = e^(-t/S) 其中 S 为记忆强度（相对强度）
  - 短间隔记忆（< 1h）：保留率 ~100%
  - 中间隔记忆（1h-1d）：保留率 58% → 44%
  - 长间隔记忆（1d-31d）：保留率 33% → 21%
  - 超长间隔（>31d）：保留率 ~13%

效果指标（对标 Mem0）:
  - Token 预算: Prompt token 减少 75%
  - 分层摘要: 平均节省 60% token 同时保持 95% 信息量
  - 艾宾浩斯: +3.8 时序推理准确率

Reference: mem0.ai/blog/6-techniques-to-cut-ai-agent-memory-cost (May 2026)
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────

class CompressionLevel(Enum):
    """分层摘要压缩级别。"""
    FULL = 0        # 完整内容（0% 压缩）
    DETAILED = 1    # 详细摘要（~50% 压缩）
    COMPACT = 2     # 紧凑摘要（~80% 压缩）
    HEADLINE = 3    # 标题级（~95% 压缩）
    DROPPED = 4     # 丢弃（不注入）


# 艾宾浩斯遗忘曲线默认参数
EBBINGHAUS_STRENGTH_MAP = {
    "critical": 365.0,     # 关键知识（~1 年半衰期）
    "important": 90.0,     # 重要知识（~3 个月半衰期）
    "normal": 30.0,        # 一般知识（~1 个月半衰期）
    "transient": 1.0,      # 临时信息（~1 天半衰期）
}


@dataclass
class TokenBudgetConfig:
    """Token 预算配置。

    Args:
        hard_token_limit: 硬 Token 上限（默认为 7,000）
        ebbinghaus_enabled: 是否启用艾宾浩斯衰减
        compression_enabled: 是否启用分层摘要压缩
        summary_budget_ratio: 摘要分配的 token 比例（剩余给完整记忆）
        full_min_importance: 完整保留的最低重要性阈值
        compact_below_importance: 低于此值使用紧凑摘要
        headline_below_importance: 低于此值仅保留标题
        drop_below_importance: 低于此值直接丢弃
    """

    hard_token_limit: int = 7000
    ebbinghaus_enabled: bool = True
    compression_enabled: bool = True
    summary_budget_ratio: float = 0.15           # 15% 给摘要层
    full_min_importance: float = 0.7
    compact_below_importance: float = 0.5
    headline_below_importance: float = 0.2
    drop_below_importance: float = 0.05


@dataclass
class MemoryTokenEntry:
    """记忆条目的 Token 预算元数据。

    Args:
        memory_id: 记忆唯一标识
        full_text: 完整内容
        token_count: 完整内容 token 数
        importance: 重要性 [0, 1]
        ebbinghaus_strength: 记忆强度类别
        created_at: 创建时间戳
        last_reviewed: 最近复习时间（用于艾宾浩斯曲线）
        review_count: 复习次数（每次复习重置曲线）
        compressed_text: 压缩后文本（分层摘要）
        compression_level: 当前压缩级别
    """

    memory_id: str
    full_text: str = ""
    token_count: int = 0
    importance: float = 0.5
    ebbinghaus_strength: str = "normal"
    created_at: float = field(default_factory=time.time)
    last_reviewed: float = field(default_factory=time.time)
    review_count: int = 0
    compressed_text: str = ""
    compression_level: CompressionLevel = CompressionLevel.FULL


# ── Token 估算 ────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """混合中英文 token 估算。

    英文 ~4 char/token，中文 ~1.5 char/token。
    """
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4.0) + 1


# ── 艾宾浩斯衰减 ────────────────────────────────────────────────

def ebbinghaus_retention(
    elapsed_seconds: float,
    strength_days: float = 30.0,
    review_count: int = 0,
) -> float:
    """计算艾宾浩斯遗忘曲线的保留率。

    R = e^(-t / S)
    其中 t 为经过时间（天），S 为记忆强度（天）。

    每次复习后强度翻倍：S' = S * 2^review_count
    """
    if elapsed_seconds <= 0:
        return 1.0

    elapsed_days = elapsed_seconds / 86400.0
    effective_strength = strength_days * (2 ** review_count)
    retention = math.exp(-elapsed_days / effective_strength)
    return max(0.0, min(1.0, retention))


# ── 分层摘要压缩 ──────────────────────────────────────────────────

def _compress_to_level(text: str, level: CompressionLevel) -> str:
    """将文本压缩到指定级别（简化版 — 实际使用应接入 LLM 摘要）。

    当前实现：
    - DETAILED: 取前 60% 句子
    - COMPACT: 取前 30% 句子 + "..."
    - HEADLINE: 取第一句作为标题
    - DROPPED: 空字符串
    """
    if not text or level == CompressionLevel.FULL:
        return text
    if level == CompressionLevel.DROPPED:
        return ""

    sentences = [s.strip() for s in text.replace("\n", " ").split(".") if s.strip()]
    if not sentences:
        return text

    if level == CompressionLevel.HEADLINE:
        return sentences[0] + "."
    elif level == CompressionLevel.COMPACT:
        n = max(1, len(sentences) // 3)
        return ". ".join(sentences[:n]) + "."
    elif level == CompressionLevel.DETAILED:
        n = max(1, len(sentences) * 3 // 5)
        return ". ".join(sentences[:n]) + "."
    return text


# ── Token 预算管理器 ──────────────────────────────────────────────

class TokenBudgetManager:
    """Token 预算 + 艾宾浩斯衰减 + 分层摘要压缩管理器。

    使用方式::

        from trinity.modules.second_brain.token_budget import TokenBudgetManager

        tbm = TokenBudgetManager(hard_token_limit=7000)

        # 注册记忆
        tbm.register(
            "mem_001", full_text="Q3 华东区毛利率为 22.3%...",
            importance=0.85, strength="important",
        )

        # 获取预算约束下的注入上下文
        result = tbm.build_context(["mem_001", "mem_002", "mem_003"])
        # → {
        #     "context": "..."  (≤ hard_token_limit),
        #     "total_tokens": 2340,
        #     "memories_included": ["mem_001", "mem_002"],
        #     "memories_dropped": ["mem_003"],
        #     "compression_applied": {"mem_002": "compact"},
        #   }
    """

    # ── 构造函数 ──────────────────────────────────────────────────

    def __init__(
        self,
        hard_token_limit: int = 7000,
        ebbinghaus_enabled: bool = True,
        compression_enabled: bool = True,
        summary_budget_ratio: float = 0.15,
    ):
        self.config = TokenBudgetConfig(
            hard_token_limit=hard_token_limit,
            ebbinghaus_enabled=ebbinghaus_enabled,
            compression_enabled=compression_enabled,
            summary_budget_ratio=summary_budget_ratio,
        )

        self._entries: Dict[str, MemoryTokenEntry] = {}

    # ── 注册 ──────────────────────────────────────────────────────

    def register(
        self,
        memory_id: str,
        full_text: str = "",
        importance: float = 0.5,
        strength: str = "normal",
        created_at: Optional[float] = None,
    ) -> MemoryTokenEntry:
        """注册一条记忆到 Token 预算系统。

        Args:
            memory_id: 记忆 ID
            full_text: 完整内容
            importance: 重要性 [0, 1]
            strength: 记忆强度类别 ("critical"/"important"/"normal"/"transient")
            created_at: 创建时间戳
        """
        token_count = estimate_tokens(full_text)
        entry = MemoryTokenEntry(
            memory_id=memory_id,
            full_text=full_text,
            token_count=token_count,
            importance=importance,
            ebbinghaus_strength=strength,
            created_at=created_at or time.time(),
        )
        self._entries[memory_id] = entry
        logger.debug(
            "TokenBudget registered: %s (tokens=%d, importance=%.2f, strength=%s)",
            memory_id, token_count, importance, strength,
        )
        return entry

    def review(self, memory_id: str) -> bool:
        """记录一次复习（重置艾宾浩斯曲线）。"""
        entry = self._entries.get(memory_id)
        if entry is None:
            return False
        entry.last_reviewed = time.time()
        entry.review_count += 1
        return True

    # ── 核心：构建预算约束上下文 ──────────────────────────────────

    def build_context(
        self, memory_ids: List[str], header: str = ""
    ) -> Dict[str, Any]:
        """构建 Token 预算约束下的记忆注入上下文。

        流程：
        1. 按 (重要性 × 艾宾浩斯保留率) 排序
        2. 分层压缩：高重要性完整、中重要性紧凑、低重要性标题
        3. 硬截断：总 token 超过上限后丢弃低优先级
        4. 追加预算说明（告知模型有多少记忆被省略）

        Returns:
            {
                "context": str,               # 最终注入文本
                "total_tokens": int,          # 实际消耗 token
                "budget_limit": int,          # 硬上限
                "memories_included": [...],   # 包含的记忆 ID
                "memories_dropped": [...],    # 被丢弃的记忆 ID
                "compression_applied": {...},  # {memory_id: compression_level}
                "ebbinghaus_scores": {...},   # {memory_id: retention_score}
            }
        """
        if not memory_ids:
            return {
                "context": header,
                "total_tokens": estimate_tokens(header),
                "budget_limit": self.config.hard_token_limit,
                "memories_included": [],
                "memories_dropped": [],
                "compression_applied": {},
                "ebbinghaus_scores": {},
            }

        now = time.time()
        scored: List[Tuple[str, float]] = []

        # Step 1: 计算综合得分 = importance × ebbinghaus_retention
        for mid in memory_ids:
            entry = self._entries.get(mid)
            if entry is None:
                # 未知记忆：默认得分
                scored.append((mid, 0.5))
                continue

            if self.config.ebbinghaus_enabled:
                strength_days = EBBINGHAUS_STRENGTH_MAP.get(
                    entry.ebbinghaus_strength, 30.0
                )
                elapsed = now - entry.last_reviewed
                retention = ebbinghaus_retention(elapsed, strength_days, entry.review_count)
            else:
                retention = 1.0

            score = entry.importance * retention
            scored.append((mid, score))

        # 按得分降序
        scored.sort(key=lambda x: x[1], reverse=True)

        # Step 2: 分层压缩 + 硬截断
        context_parts: List[str] = []
        if header:
            context_parts.append(header)

        included: List[str] = []
        dropped: List[str] = []
        compression_applied: Dict[str, str] = {}
        ebbinghaus_scores: Dict[str, float] = {}

        current_tokens = estimate_tokens(header) if header else 0
        budget = self.config.hard_token_limit

        for mid, score in scored:
            entry = self._entries.get(mid)
            if entry is None:
                continue

            ebbinghaus_scores[mid] = round(score, 4)

            if self.config.compression_enabled:
                level = self._determine_compression(entry.importance)
                text = _compress_to_level(entry.full_text, level)
                compression_applied[mid] = level.name.lower()
            else:
                text = entry.full_text
                compression_applied[mid] = "full"

            text_tokens = estimate_tokens(text)
            if current_tokens + text_tokens <= budget and text:
                context_parts.append(text)
                current_tokens += text_tokens
                included.append(mid)
            else:
                dropped.append(mid)

        # Step 3: 预算说明
        omitted_count = len(dropped)
        if omitted_count > 0:
            note = f"\n\n[Token Budget: {omitted_count} lower-priority memories omitted. "
            note += f"Budget={budget}, Used={current_tokens}]"
            context_parts.append(note)
            current_tokens += estimate_tokens(note)

        context = "\n\n---\n\n".join(context_parts)

        logger.info(
            "Context built: %d memories included, %d dropped, %d tokens / %d budget",
            len(included), len(dropped), current_tokens, budget,
        )

        return {
            "context": context,
            "total_tokens": current_tokens,
            "budget_limit": budget,
            "memories_included": included,
            "memories_dropped": dropped,
            "compression_applied": compression_applied,
            "ebbinghaus_scores": ebbinghaus_scores,
        }

    def _determine_compression(self, importance: float) -> CompressionLevel:
        """根据重要性决定压缩级别。"""
        if importance >= self.config.full_min_importance:
            return CompressionLevel.FULL
        elif importance >= self.config.compact_below_importance:
            return CompressionLevel.DETAILED
        elif importance >= self.config.headline_below_importance:
            return CompressionLevel.COMPACT
        elif importance >= self.config.drop_below_importance:
            return CompressionLevel.HEADLINE
        else:
            return CompressionLevel.DROPPED

    # ── 查询 ──────────────────────────────────────────────────────

    def get_entry(self, memory_id: str) -> Optional[MemoryTokenEntry]:
        """查询记忆条目。"""
        return self._entries.get(memory_id)

    def get_retention(self, memory_id: str) -> Optional[float]:
        """查询记忆当前的艾宾浩斯保留率。"""
        entry = self._entries.get(memory_id)
        if entry is None:
            return None
        strength_days = EBBINGHAUS_STRENGTH_MAP.get(
            entry.ebbinghaus_strength, 30.0
        )
        elapsed = time.time() - entry.last_reviewed
        return ebbinghaus_retention(elapsed, strength_days, entry.review_count)

    def statistics(self) -> Dict[str, Any]:
        """返回预算管理器运行时统计。"""
        total = len(self._entries)
        if total == 0:
            return {"total_entries": 0, "total_tokens": 0}

        total_tokens = sum(e.token_count for e in self._entries.values())
        return {
            "total_entries": total,
            "total_tokens": total_tokens,
            "hard_token_limit": self.config.hard_token_limit,
            "ebbinghaus_enabled": self.config.ebbinghaus_enabled,
            "compression_enabled": self.config.compression_enabled,
            "avg_importance": round(
                sum(e.importance for e in self._entries.values()) / total, 4
            ),
        }
