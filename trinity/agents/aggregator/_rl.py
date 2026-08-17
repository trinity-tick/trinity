"""MemoryAggregator - RL feedback / scoring mixin (split from aggregator.py).
"""

from __future__ import annotations

import json
import logging
import math
import os
import pickle
import threading
import time
from collections import Counter, deque
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# ── v7.1.0: Observability & Tracing ──
from trinity.agents.observability import ObservabilityManager, RequestTracer

import numpy as np

from trinity.agents.dimensions import (
    DEFAULT_CONFIDENCE,
    CONFIDENCE_BOOST_PER_AGENT,
    MAX_CONFIDENCE,
    TOPIC_MAX_TOPICS,
    DimensionEngine,
    DimensionVector,
    MemoryCategory,
    MemoryScope,
    RelationType,
)

from ._constants import logger


class _RLMixin:

    def _apply_scoring_calibration(
        self,
        merged: List[DimensionVector],
        query_text: str,
    ) -> List[DimensionVector]:
        """RRF 融合后的评分校准（全部 env 门控，默认 off）。

        1) TRINITY_CONFIDENCE_SCORER=on → 四维置信度校准（AgentPrizm 对齐）：
           priority × (0.6 + 0.4 × confidence.overall)，旧记忆/低权威来源降权。
        2) TRINITY_IMPORTANCE_BOOST=on → importance 动态微调（写时定值→查询时加权）：
           priority += (importance - 0.5) × 0.2，±0.1 有界。
        3) TRINITY_RERANK=on → Cross-Encoder 重排（本地缓存模型，加载失败静默降级）。
        """
        if not merged:
            return merged

        # 1) 四维置信度校准
        if os.environ.get("TRINITY_CONFIDENCE_SCORER", "off").strip().lower() == "on":
            try:
                from trinity.modules.second_brain.confidence_scored_retrieval import (
                    ConfidenceScorer,
                    SourceType,
                    ValidityCategory,
                )

                scorer = ConfidenceScorer()

                def _validity(cat: str) -> ValidityCategory:
                    c = (cat or "").lower()
                    if "pref" in c or "preference" in c:
                        return ValidityCategory.PERSONAL_PREFERENCE
                    if "news" in c:
                        return ValidityCategory.NEWS
                    if "financ" in c or "market" in c:
                        return ValidityCategory.FINANCIAL
                    if "regul" in c or "policy" in c or "law" in c:
                        return ValidityCategory.REGULATORY
                    return ValidityCategory.GENERAL_KNOWLEDGE

                for dv in merged:
                    src = (
                        SourceType.USER_CONFIRMED
                        if getattr(dv, "source_count", 0) >= 1
                        else SourceType.LLM_GENERATED
                    )
                    conf = scorer.score(
                        source_type=src,
                        citation_count=getattr(dv, "access_count", 0),
                        citation_agreement=min(
                            1.0, 0.5 + 0.05 * getattr(dv, "access_count", 0)
                        ),
                        created_at=getattr(dv, "created_at", 0.0) or time.time(),
                        validity_category=_validity(getattr(dv, "category", "")),
                        semantic_similarity=max(0.0, min(1.0, float(dv.priority))),
                    )
                    dv.priority = float(dv.priority) * (0.6 + 0.4 * conf.overall)
            except Exception as exc:
                logger.debug("Confidence calibration skipped: %s", exc)

        # 2) importance 动态微调（动态记忆评分对齐）
        if os.environ.get("TRINITY_IMPORTANCE_BOOST", "off").strip().lower() == "on":
            try:
                for dv in merged:
                    imp = self.importance_score(dv.memory_id)
                    boost = (imp - 0.5) * 0.2  # ±0.1 有界
                    dv.priority = min(1.0, max(0.0, float(dv.priority) + boost))
            except Exception as exc:
                logger.debug("Importance boost skipped: %s", exc)

        # 3) Cross-Encoder 重排（本地缓存模型；加载失败静默降级为原序）
        if os.environ.get("TRINITY_RERANK", "off").strip().lower() == "on":
            try:
                from trinity.vector_index.reranker import CrossEncoderReranker

                rk = CrossEncoderReranker(model_name="fast")
                items = [{"id": d.memory_id, "text": d.content, "_dv": d} for d in merged]
                reranked = rk.rerank(query_text, items, top_k=len(items), id_key="id")
                # 重排顺序写回 priority（映射 [0.5,1.0] 保序），
                # 否则后续 RL 步骤按 priority 重排会把重排结果冲掉。
                order = {r["id"]: i for i, r in enumerate(reranked)}
                for d in merged:
                    if d.memory_id in order:
                        rank = order[d.memory_id]
                        d.priority = 0.5 + 0.5 * (1.0 - rank / max(1, len(reranked)))
                merged.sort(key=lambda d: order.get(d.memory_id, 1 << 30))
            except Exception as exc:
                logger.debug("Reranker skipped: %s", exc)

        return merged

    def rl_feedback(self, memory_id: str, positive: bool = True) -> Dict[str, Any]:
        """记录 RL 强化信号（用户确认/纠正 → 更新 Q 值）。

        Args:
            memory_id: 目标记忆。
            positive: True=用户确认（TASK_SUCCESS），False=纠正（TASK_FAILURE）。

        Returns:
            {"rl": bool, "q_value": float}
        """
        if self._rl_scorer is None:
            return {"rl": False, "q_value": 0.5}
        try:
            from trinity.modules.second_brain.episodic_rl import FeedbackSignal
            # 冷启动兜底（2026-08-17, RL 反馈闭环）: 未注册记忆先注册，
            # 使 API/MCP/DSH 对引擎侧（非聚合池）记忆也能直接反馈不崩溃。
            if memory_id not in self._rl_scorer._states:
                self._rl_scorer.register_memory(memory_id, semantic_score=0.5)
            signal = FeedbackSignal.TASK_SUCCESS if positive else FeedbackSignal.TASK_FAILURE
            self._rl_scorer.record_feedback(memory_id, signal)
            self._rl_scorer.update_q_values()
            self._save_rl_state()
            q = self._rl_scorer.score_memory(memory_id)
            return {"rl": True, "q_value": round(q, 4)}
        except Exception as exc:
            logger.debug("rl_feedback failed: %s", exc)
            return {"rl": False, "q_value": 0.5}

    def _save_rl_state(self) -> None:
        """RL 状态独立落盘（2026-08-17 评分测试修复）。

        聚合池 _save 只在写操作时触发，而 RL 状态由 query（读操作）的
        rl_implicit_use / API 的 rl_feedback 更新——不主动落盘则 RL 奖励
        永远滞留内存、重启清零（学完即忘）。此处直接写小文件 rl_state.json，
        不触发整池保存。
        """
        if self._rl_scorer is None or not self._persist_path:
            return
        try:
            import os as _os
            rl_path = _os.path.join(_os.path.dirname(self._persist_path), "rl_state.json")
            self._rl_scorer.save(rl_path)
        except Exception:
            pass

    def rl_implicit_use(self, memory_ids: List[str], limit: int = 3) -> int:
        """RL 闭环自动喂食源（2026-08-17）: 检索命中即视为"使用"。

        给 top 结果打 IMPLICIT_USE 小奖励（reward_implicit=0.05），使
        检索→使用→反馈→Q 值 闭环无需人工/agent 显式调用。每记忆每进程
        只奖励一次（_rl_implicit_rewarded 防通胀，集合 >10w 时清理）。
        强信号仍走 rl_feedback（TASK_SUCCESS/FAILURE）。

        Returns:
            实际奖励的记忆条数。
        """
        if self._rl_scorer is None:
            return 0
        from trinity.modules.second_brain.episodic_rl import FeedbackSignal

        rewarded = 0
        for mid in (memory_ids or [])[:limit]:
            if not mid or mid in self._rl_implicit_rewarded:
                continue
            try:
                if mid not in self._rl_scorer._states:
                    self._rl_scorer.register_memory(mid, semantic_score=0.5)
                self._rl_scorer.record_feedback(mid, FeedbackSignal.IMPLICIT_USE)
                self._rl_implicit_rewarded.add(mid)
                rewarded += 1
            except Exception:
                continue
        if len(self._rl_implicit_rewarded) > 100000:
            self._rl_implicit_rewarded.clear()
        if rewarded:
            self._rl_scorer.update_q_values()
            self._save_rl_state()
        return rewarded

    def importance_score(self, memory_id: str) -> float:
        """Auto-score memory importance (Mem0/Supermemory aligned).

        Factors: access frequency, recency, cross-agent references, content
        length (proxy for information density), and priority dimension.

        Returns float in [0.0, 1.0].
        """
        if memory_id not in self._pool:
            return 0.0
        dv = self._pool[memory_id]
        score = 0.0
        # 1. Access frequency bonus (30% weight)
        access_count = getattr(dv, 'access_count', 0)
        score += min(access_count / 10.0, 1.0) * 0.3
        # 2. Content length — proxy for information density (20% weight)
        content_len = len(dv.content) if dv.content else 0
        score += min(content_len / 500.0, 1.0) * 0.2
        # 3. Cross-agent reference bonus — topic shared by 2+ agents (30% weight)
        dv_topics = getattr(dv, 'topics', [])
        topic_agents: Set[str] = set()
        for other in self._pool.values():
            other_topics = getattr(other, 'topics', [])
            if set(dv_topics) & set(other_topics):
                topic_agents.update(other.source_agents)
        if len(topic_agents) >= 2:
            score += 0.3
        # 4. Priority dimension bonus (20% weight)
        priority = getattr(dv, 'priority', 0.5)
        score += priority * 0.2
        return round(min(score, 1.0), 4)
