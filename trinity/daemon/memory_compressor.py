#!/usr/bin/env python3
"""
Memory Compressor Module — LLM 记忆压缩引擎 (Layer 6c)
========================================================
基于 LLM 调用将多条低分衰减记忆摘要压缩为一条精简记忆。
压缩时保留：实体名、日期、关键决策、数字/金额等关键信息。

压缩流程：
  1. 收集 DecayEngine 产出的 pending_compression 记忆批次
  2. 调用 LLM 逐批次生成摘要记忆
  3. 将摘要作为新记忆写入，原始记忆标记为 archived
  4. 生成压缩审计记录

Design principles:
  - 保留关键信息（实体/日期/决策）
  - 去重冗余描述
  - 保留引用链（parent_memory_ids）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Data Structures
# ============================================================================


class CompressionStatus(Enum):
    """压缩操作状态"""
    SUCCESS = "success"              # 压缩成功
    PARTIAL = "partial"              # 部分成功（部分记忆未压缩）
    SKIPPED = "skipped"              # 跳过（批次太小/LLM 不可用）
    FAILED = "failed"                # 压缩失败


@dataclass
class CompressedMemory:
    """压缩后的摘要记忆"""
    summary_id: str                  # 新记忆 UUID
    content: str                     # 摘要内容（LLM 生成）
    parent_memory_ids: List[str]     # 原始记忆 ID 列表
    memory_type: str                 # 记忆类型
    importance: float                # 继承的重要性（取加权平均）
    entity_names: List[str]          # 保留的实体名
    key_dates: List[str]             # 保留的关键日期
    key_decisions: List[str]         # 保留的关键决策
    original_count: int              # 压缩前条数
    compressed_at: str               # 压缩时间


@dataclass
class CompressionBatchResult:
    """单批次压缩结果"""
    batch_id: str
    status: CompressionStatus
    compressed: Optional[CompressedMemory] = None
    archived_ids: List[str] = field(default_factory=list)
    failed_ids: List[str] = field(default_factory=list)
    error_message: str = ""
    elapsed_seconds: float = 0.0
    # 2026-08-24（R5 P0-②）：压缩前注入守卫命中（投毒内容进压缩的风险标记）
    guard_hits: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class CompressionReport:
    """完整压缩报告"""
    report_id: str
    timestamp: float = field(default_factory=time.time)
    total_batches: int = 0
    success_batches: int = 0
    partial_batches: int = 0
    failed_batches: int = 0
    total_archived: int = 0
    total_summaries: int = 0
    batch_results: List[CompressionBatchResult] = field(default_factory=list)


# ============================================================================
# Compression Prompt Templates
# ============================================================================


_COMPRESSION_SYSTEM_PROMPT = """\
You are a memory compression agent. Your task is to distill multiple memory entries
into a single concise summary that preserves all critical information.

Rules:
1. Preserve ALL entity names (people, organizations, products, tools)
2. Preserve ALL dates and time references
3. Preserve ALL key decisions, conclusions, and action items
4. Preserve ALL numbers, amounts, metrics mentioned
5. Remove redundant descriptions and repetitive content
6. Output ONLY the compressed summary text — no preamble, no markdown headings
7. Keep the summary under 500 words
8. Use a factual, neutral tone

Output format (plain text only):
<summary text here>
"""

_COMPRESSION_USER_TEMPLATE = """\
Compress the following {count} memory entries of type "{memory_type}" into one
concise summary. Preserve entities, dates, decisions, and numbers.

Memory entries:
{entries}

Compressed summary:"""


# ============================================================================
# MemoryCompressor
# ============================================================================


class MemoryCompressor:
    """LLM 记忆压缩器

    客户端需提供 PostgreSQL 适配器实例和 LLM 调用函数。

    Usage:
        compressor = MemoryCompressor(
            pg_adapter=adapter,
            llm_callable=lambda prompt: openai_response,
        )
        report = compressor.compress_batch(decay_results)
    """

    def __init__(
        self,
        pg_adapter: Any = None,
        llm_callable: Optional[callable] = None,
        default_importance: float = 0.3,
        max_content_length: int = 4000,
    ):
        """
        Args:
            pg_adapter: PostgreSQLAdapter 实例，用于读写记忆
            llm_callable: 调用 LLM 的函数，签名 (system_prompt, user_prompt) -> str
            default_importance: 压缩摘要的默认重要性
            max_content_length: 单次压缩输入的最大总字符数
        """
        self.pg_adapter = pg_adapter
        self.llm_callable = llm_callable
        self.default_importance = default_importance
        self.max_content_length = max_content_length

    # ── Compression ────────────────────────────────────────────

    def build_compression_prompt(
        self,
        memories: List[Dict[str, Any]],
        memory_type: str,
    ) -> Tuple[str, str]:
        """构建压缩提示词。

        Returns:
            (system_prompt, user_prompt)
        """
        entries = []
        total_length = 0

        for i, mem in enumerate(memories, 1):
            content = str(mem.get("content", ""))
            created = str(mem.get("created_at", ""))[:19]
            entry = f"[{i}] ({created}) {content}"
            entries.append(entry)
            total_length += len(entry)
            if total_length > self.max_content_length:
                entries.append(f"... (truncated, {len(memories) - i} more)")
                break

        user_prompt = _COMPRESSION_USER_TEMPLATE.format(
            count=len(memories),
            memory_type=memory_type,
            entries="\n\n".join(entries),
        )

        return _COMPRESSION_SYSTEM_PROMPT, user_prompt

    def compress_batch(
        self,
        memories: List[Dict[str, Any]],
        memory_type: str = "general",
    ) -> CompressionBatchResult:
        """压缩单批记忆。

        Args:
            memories: 待压缩记忆字典列表
            memory_type: 记忆类型

        Returns:
            CompressionBatchResult
        """
        batch_id = f"batch_{uuid.uuid4().hex[:12]}"
        result = CompressionBatchResult(
            batch_id=batch_id,
            status=CompressionStatus.SKIPPED,
        )
        start = time.time()

        # 2026-09-01 (P5 修复): 允许单条记忆压缩——此前 len<2 直接 SKIP，
        # 而 create_compression_batches 会把某类型仅剩 1 条的 pending 也成批，
        # 导致孤条 pending 永远悬置（2026-08-31 实测 Pending=2 恒 Failures=2）。
        # 单条压缩语义安全：对 1 条记忆生成浓缩摘要并归档原条目（original_count=1）。
        if len(memories) < 1:
            result.error_message = "Batch empty"
            result.elapsed_seconds = time.time() - start
            return result

        if not self.llm_callable:
            result.error_message = "No LLM callable provided"
            result.elapsed_seconds = time.time() - start
            return result

        try:
            # ── 2026-08-24（R5 P0-②）：压缩前注入守卫——压缩会跨多条记忆
            # 聚合摘要，投毒内容可能被"洗白"进压缩结果；压缩前对每条原始
            # 记忆做注入扫描（复用 trinity.security.injection，与写路径
            # 同语义），命中高危标记并在结果中报告（不阻断压缩——治理
            # 决策由上层按 risk 处理；TRINITY_COMPRESS_GUARD=off 关闭）。
            guard_on = os.environ.get("TRINITY_COMPRESS_GUARD", "on").lower() not in ("off", "0", "false")
            guard_hits = []
            if guard_on:
                try:
                    from trinity.security.injection import scan_injection
                    for m in memories:
                        content = str(m.get("content") or "")
                        if not content.strip():
                            continue
                        rep = scan_injection(content)
                        if rep.get("flagged"):
                            guard_hits.append({
                                "memory_id": str(m.get("memory_id", "")),
                                "severity": rep.get("severity"),
                                "patterns": [h["pattern"] for h in rep.get("hits", [])],
                            })
                except Exception as exc:
                    logger.warning("compression injection guard skipped: %s", exc)

            system_prompt, user_prompt = self.build_compression_prompt(
                memories, memory_type,
            )
            summary_text = self.llm_callable(system_prompt, user_prompt)

            if not summary_text or len(summary_text.strip()) < 10:
                result.status = CompressionStatus.FAILED
                result.error_message = "LLM returned empty or too-short summary"
                result.elapsed_seconds = time.time() - start
                return result

            # Extract preserved entities (simple heuristic)
            entity_names = self._extract_entities(summary_text)
            key_dates = self._extract_dates(summary_text)
            key_decisions = self._extract_decisions(summary_text)

            # Calculate weighted importance
            importances = [float(m.get("importance", 0.5)) for m in memories]
            avg_importance = sum(importances) / len(importances) if importances else self.default_importance
            # Clamp to [0, 1]
            avg_importance = max(0.0, min(1.0, avg_importance))

            parent_ids = [str(m.get("memory_id", "")) for m in memories]

            compressed = CompressedMemory(
                summary_id=str(uuid.uuid4()),
                content=summary_text.strip(),
                parent_memory_ids=parent_ids,
                memory_type=memory_type,
                importance=round(avg_importance, 4),
                entity_names=entity_names,
                key_dates=key_dates,
                key_decisions=key_decisions,
                original_count=len(memories),
                compressed_at=datetime.now(timezone.utc).isoformat(),
            )

            # Persist if adapter is available
            if self.pg_adapter:
                self._persist_compressed(compressed)
                self._archive_originals(parent_ids)

            result.status = CompressionStatus.SUCCESS
            result.compressed = compressed
            result.archived_ids = parent_ids
            result.guard_hits = guard_hits  # R5 P0-②：注入守卫命中回填
            if guard_hits:
                logger.warning(
                    "Compression batch %s: %d injection guard hits (high=%d) — "
                    "compressed summary may contain poisoned content",
                    batch_id, len(guard_hits),
                    sum(1 for h in guard_hits if h.get("severity") == "high"),
                )

        except Exception as e:
            logger.error("Compression batch %s failed: %s", batch_id, e)
            result.status = CompressionStatus.FAILED
            result.error_message = str(e)

        result.elapsed_seconds = round(time.time() - start, 3)
        return result

    # ── Full Compression Pipeline ──────────────────────────────

    # ── Intent-Aware Pre-Clustering（R3 P0-1c, 2026-08-15）──────────
    # 对齐 SimpleMem (ICML 2026)：压缩前按意图层次聚类，同意图记忆
    # 一起压缩（摘要更聚焦）。env 开关 TRINITY_INTENT_CLUSTER=on 启用；
    # 失败/关闭时原样返回批次（行为不变）。

    def intent_cluster_batch(
        self,
        memories: List[Dict[str, Any]],
        min_cluster: int = 2,
    ) -> List[List[Dict[str, Any]]]:
        """按意图把一批记忆聚类为多个子批（供逐簇压缩）。

        Args:
            memories: 待压缩记忆列表。
            min_cluster: 子批最小记忆数（小于则并入相邻）。

        Returns:
            子批列表；意图聚类不可用时返回 [memories]（原样）。
        """
        if len(memories) < min_cluster * 2:
            return [memories]
        try:
            from trinity.modules.second_brain.intent_compression import (
                InteractionFragment, HierarchicalClustering,
            )
            frags = [
                InteractionFragment(
                    fragment_id=str(m.get("memory_id", i)),
                    text=str(m.get("content", "")),
                    task_labels=[str(m.get("category", "general"))],
                    importance=float(m.get("importance", 0.5)),
                )
                for i, m in enumerate(memories)
            ]
            clusterer = HierarchicalClustering()
            labels = clusterer.fit_predict(frags)
            groups: Dict[int, List[Dict[str, Any]]] = {}
            for mem, label in zip(memories, labels.values()):
                groups.setdefault(int(label), []).append(mem)
            sub_batches = [g for g in groups.values() if len(g) >= 1]
            # 过小的簇并入最近的大簇
            merged: List[List[Dict[str, Any]]] = []
            small: List[Dict[str, Any]] = []
            for g in sub_batches:
                if len(g) >= min_cluster:
                    merged.append(g)
                else:
                    small.extend(g)
            if small:
                if merged:
                    merged[0].extend(small)
                else:
                    merged = [small]
            return merged or [memories]
        except Exception as exc:
            logger.debug("intent clustering skipped: %s", exc)
            return [memories]

    # ── Structured Distillation Compress（R4, 2026-08-15）────────────
    # 对接 ICML 2026 Structured Distillation（11x 压缩 + 96% MRR 保留）：
    # 把记忆批转 ExchangeTurn → distill 复合对象 → 摘要文本。
    # env TRINITY_DISTILL_COMPRESS=on 启用；失败/关闭返回 None（调用方回退 LLM）。

    def distill_compress(
        self,
        memories: List[Dict[str, Any]],
    ) -> Optional[str]:
        """用结构化蒸馏压缩一批记忆为摘要文本（11x 目标）。

        Args:
            memories: 待压缩记忆列表。

        Returns:
            摘要文本；不可用/失败返回 None。
        """
        if not memories:
            return None
        try:
            from trinity.modules.second_brain.structured_distillation_compressor import (
                ExchangeRole, ExchangeTurn, StructuredDistillationCompressor,
            )
            turns = [
                ExchangeTurn(
                    turn_id=i,
                    role=ExchangeRole.USER if m.get("role", "user") == "user"
                    else ExchangeRole.ASSISTANT,
                    content=str(m.get("content", "")),
                )
                for i, m in enumerate(memories)
            ]
            compressor = StructuredDistillationCompressor()
            distillate = compressor.distill(turns)
            core = distillate.exchange_core
            lines = [
                f"[DISTILLED - {distillate.compression_ratio:.1f}x]",
                f"Intent: {core.user_intent}",
                f"Summary: {core.assistant_response_summary}",
                f"Outcome: {core.outcome or 'n/a'}",
            ]
            rooms = distillate.thematic_room_assignments
            if rooms:
                themes = ", ".join(
                    (getattr(r, "room_type", None).value
                     if hasattr(getattr(r, "room_type", None), "value")
                     else (getattr(r, "room", "") or ""))
                    for r in rooms[:3]
                )
                lines.append(f"Themes: {themes}")
            return "\n".join(l for l in lines if l and not l.endswith(": "))
        except Exception as exc:
            logger.debug("structured distillation skipped: %s", exc)
            return None

    def compress_all_batches(
        self,
        batches: List[List[Dict[str, Any]]],
        memory_type: str = "general",
    ) -> CompressionReport:
        """运行完整压缩流水线。

        Args:
            batches: 待压缩批次列表
            memory_type: 记忆类型

        Returns:
            CompressionReport
        """
        report = CompressionReport(
            report_id=f"report_{uuid.uuid4().hex[:12]}",
            total_batches=len(batches),
        )

        for batch in batches:
            batch_result = self.compress_batch(batch, memory_type)
            report.batch_results.append(batch_result)

            if batch_result.status == CompressionStatus.SUCCESS:
                report.success_batches += 1
                report.total_summaries += 1
                report.total_archived += len(batch_result.archived_ids)
            elif batch_result.status == CompressionStatus.PARTIAL:
                report.partial_batches += 1
                report.total_archived += len(batch_result.archived_ids)
            else:
                report.failed_batches += 1

        return report

    # ── Persistence ────────────────────────────────────────────

    def _persist_compressed(self, compressed: CompressedMemory) -> Optional[str]:
        """将压缩摘要写入 PostgreSQL 并返回 memory_id。"""
        if not self.pg_adapter:
            return None

        try:
            import psycopg2.extras

            now = datetime.now(timezone.utc).isoformat()
            tags = [
                "compressed",
                f"parent_count:{compressed.original_count}",
            ]

            # Build rich content with metadata
            full_content = (
                f"[COMPRESSED SUMMARY — {compressed.original_count} memories]\n"
                f"Type: {compressed.memory_type}\n"
                f"Entities: {', '.join(compressed.entity_names) if compressed.entity_names else 'N/A'}\n"
                f"Dates: {', '.join(compressed.key_dates) if compressed.key_dates else 'N/A'}\n"
                f"Decisions: {', '.join(compressed.key_decisions) if compressed.key_decisions else 'N/A'}\n"
                f"---\n"
                f"{compressed.content}"
            )

            result = self.pg_adapter.store_memory(
                content=full_content,
                persona_id="system",
                importance=compressed.importance,
                tags=tags,
                category=f"compressed_{compressed.memory_type}",
                role="system",
            )
            if result:
                compressed.summary_id = result.get("memory_id", compressed.summary_id)
                logger.info(
                    "Persisted compressed summary %s (from %d memories)",
                    compressed.summary_id, compressed.original_count,
                )
            return result.get("memory_id") if result else None

        except Exception as e:
            logger.error("Failed to persist compressed memory: %s", e)
            return None

    def _archive_originals(self, memory_ids: List[str]) -> int:
        """将原始记忆标记为 archived 状态（存储无关：adapter 提供 archive_memories）。"""
        if not self.pg_adapter:
            return 0

        count = 0
        for mem_id in memory_ids:
            try:
                self.pg_adapter.update_memory(
                    memory_id=mem_id,
                    tags=["archived", "compressed"],
                )
                # 状态翻转走 adapter 统一接口（SQLite/PostgreSQL 各自实现），
                # 不再依赖 PG 专属裸 SQL。
                count += self.pg_adapter.archive_memories([mem_id]) or 0
            except Exception as e:
                logger.error("Failed to archive memory %s: %s", mem_id, e)

        logger.info("Archived %d original memories", count)
        return count

    # ── Entity Extraction Helpers ──────────────────────────────

    @staticmethod
    def _extract_entities(text: str) -> List[str]:
        """从摘要中提取实体名（简单启发式）。"""
        entities = []
        # Look for patterns like "EntityName (organization)", names in quotes, etc.
        import re
        # Capitalized multi-word phrases (simple proper noun heuristic)
        proper_nouns = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b', text)
        entities.extend(proper_nouns)
        return list(dict.fromkeys(entities))[:10]  # dedup, max 10

    @staticmethod
    def _extract_dates(text: str) -> List[str]:
        """从摘要中提取日期。"""
        import re
        date_patterns = [
            r'\d{4}-\d{2}-\d{2}',          # 2026-08-07
            r'\d{2}/\d{2}/\d{4}',          # 08/07/2026
            r'\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日',  # 2026年8月7日
        ]
        dates = []
        for pat in date_patterns:
            dates.extend(re.findall(pat, text))
        return list(dict.fromkeys(dates))[:10]

    @staticmethod
    def _extract_decisions(text: str) -> List[str]:
        """从摘要中提取关键决策表述。"""
        import re
        decision_keywords = [
            r'(?:决定|决策|选择|采用|确定|确认|选定|批准|通过)[^。\n]{10,60}',
            r'(?:decided|chose|selected|determined|approved|confirmed)[^.\n]{10,60}',
        ]
        decisions = []
        for pat in decision_keywords:
            decisions.extend(re.findall(pat, text, re.IGNORECASE))
        return decisions[:5]


# ============================================================================
# Mock LLM callable for testing / offline fallback
# ============================================================================


def mock_llm_compress(system_prompt: str, user_prompt: str) -> str:
    """Mock LLM 压缩函数（无 LLM 时的降级方案）。

    使用简单的抽取式摘要：连接所有记忆的前 100 字符。
    """
    lines = user_prompt.split("\n")
    entries = [l.strip() for l in lines if l.strip().startswith("[")]
    if not entries:
        return "Compressed summary not available (no entries)."

    snippets = []
    for entry in entries:
        # Extract content after the closing bracket
        if "] " in entry:
            text = entry.split("] ", 1)[1]
            snippets.append(text[:120])

    combined = " | ".join(snippets[:5])
    return (
        f"[AUTO-COMPRESSED] {len(entries)} memories merged: "
        f"{combined[:1500]}"
    )


# ============================================================================
# Real LLM callable (OpenAI-compatible chat completions, stdlib-only)
# ============================================================================


def create_llm_compress_callable(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 60.0,
) -> Callable[[str, str], str]:
    """创建真实 LLM 压缩 callable（OpenAI 兼容 /chat/completions）。

    参数缺省时从环境变量读取：
        TRINITY_LLM_BASE_URL  默认 https://api.openai.com/v1
        TRINITY_LLM_API_KEY   （必填，缺失抛 ValueError）
        TRINITY_LLM_MODEL     默认 gpt-4o-mini

    Returns:
        callable (system_prompt, user_prompt) -> str
    """
    import os
    import urllib.request

    base_url = (base_url or os.environ.get("TRINITY_LLM_BASE_URL")
                or "https://api.openai.com/v1").rstrip("/")
    api_key = api_key or os.environ.get("TRINITY_LLM_API_KEY")
    model = model or os.environ.get("TRINITY_LLM_MODEL") or "gpt-4o-mini"

    if not api_key:
        raise ValueError(
            "TRINITY_LLM_API_KEY 未设置：真实 LLM 压缩需要 API key。"
            "可设置环境变量 TRINITY_LLM_API_KEY / TRINITY_LLM_BASE_URL / "
            "TRINITY_LLM_MODEL，或继续使用 mock 压缩（--llm mock）。"
        )

    endpoint = f"{base_url}/chat/completions"

    def _call(system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            # 压缩 token 预算可调（P1-2）：TRINITY_LLM_MAX_TOKENS，默认 800
            "max_tokens": int(os.environ.get("TRINITY_LLM_MAX_TOKENS", "800")),
        }
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        try:
            return body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as e:
            raise RuntimeError(f"LLM 响应格式异常: {body}") from e

    return _call
