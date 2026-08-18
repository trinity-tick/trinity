"""
# status: orphan (2026-08-15 audit, not in runtime path)
SleepWakeConsolidator — Language Models Need Sleep (Google)
============================================================
Google Research, Jul 2026 · P44-4

实现睡眠-觉醒巩固周期: 离线睡眠阶段主动回放近期经验,
蒸馏关键知识到持久存储。梦-回放用于对抗灾难性遗忘。
与已有 FadeMem/Ebbinghaus 遗忘曲线协同。

设计要点:
  - WakePhase: 清醒阶段——接收信息、处理任务
  - SleepPhase: 睡眠阶段——主动回放+知识蒸馏
  - DreamReplay: 梦-回放——对抗灾难性遗忘
  - KnowledgeDistiller: 蒸馏关键知识到持久存储
  - ExperienceReplayBuffer: 经验回放缓存
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque
import random

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SLC_ConsolidationPhase(Enum):
    """巩固阶段。"""
    AWAKE = auto()         # 清醒——接收信息
    NREM_LIGHT = auto()    # 浅睡——初步整理
    NREM_DEEP = auto()     # 深睡——知识蒸馏
    REM_DREAM = auto()     # 快速眼动——梦回放


class SLC_ConsolidationResult(Enum):
    """巩固结果。"""
    CONSOLIDATED = auto()
    FORGOTTEN = auto()
    MERGED = auto()
    REFRESHED = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ExperienceReplayBuffer:
    """经验回放缓存——存储近期经验。"""
    buffer_id: str
    experiences: List[Dict[str, Any]] = field(default_factory=list)
    max_size: int = 100
    importance_weights: Dict[str, float] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def add(self, experience: Dict[str, Any]) -> bool:
        if len(self.experiences) >= self.max_size:
            # 按重要性权重淘汰最低
            if self.importance_weights:
                remove_idx = min(range(len(self.experiences)),
                                 key=lambda i: self.importance_weights.get(self.experiences[i].get("id", ""), 0))
                self.experiences.pop(remove_idx)
            else:
                return False

        self.experiences.append(experience)
        return True


@dataclass
class ConsolidationCycle:
    """一次睡眠-觉醒巩固周期。"""
    cycle_id: str
    phase: SLC_ConsolidationPhase = SLC_ConsolidationPhase.AWAKE
    experiences_processed: int = 0
    knowledge_distilled: int = 0
    dreams_generated: int = 0
    forgetting_prevented: int = 0
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0


@dataclass
class SleepWakeRhythm:
    """睡眠-觉醒节律配置。"""
    wake_duration_seconds: float = 600.0     # 10分钟清醒
    sleep_duration_seconds: float = 120.0     # 2分钟睡眠
    dream_ratio: float = 0.25                # 25% 时间用于REM
    consolidation_batch_size: int = 20


# ---------------------------------------------------------------------------
# KnowledgeDistiller
# ---------------------------------------------------------------------------

class KnowledgeDistiller:
    """知识蒸馏器——将经验蒸馏为持久知识。

    Parameters
    ----------
    compression_ratio : float
        压缩比 (持久知识 / 原始经验)。
    """

    def __init__(self, compression_ratio: float = 0.1) -> None:
        self.compression_ratio = compression_ratio
        self._persistent_knowledge: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def distill(self, experiences: List[Dict[str, Any]]) -> Dict[str, Any]:
        """从经验中蒸馏知识。

        Returns
        -------
        Dict[str, Any]
            {knowledge_entries, compressed_size}
        """
        with self._lock:
            if not experiences:
                return {"knowledge_entries": 0, "compressed_size": 0}

            # 聚合相似经验
            clusters: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for exp in experiences:
                content = str(exp.get("content", ""))
                if not content.strip():
                    continue
                # 简单聚类: 按前10字符
                key = content[:15].strip()
                clusters[key].append(exp)

            distilled_count = 0
            for key, cluster in clusters.items():
                if len(cluster) >= 2:
                    # 合并同类经验
                    knowledge = {
                        "summary": f"Merged {len(cluster)} experiences: {key}",
                        "source_count": len(cluster),
                        "key_facts": [exp.get("content", "")[:80] for exp in cluster[:3]],
                        "importance": min(1.0, len(cluster) * 0.1),
                        "distilled_at": time.time(),
                    }
                    self._persistent_knowledge[f"k_{int(time.time()*1e6)}_{distilled_count}"] = knowledge
                    distilled_count += 1

            return {
                "knowledge_entries": distilled_count,
                "compressed_size": max(1, distilled_count),
                "compression_ratio": round(distilled_count / max(len(experiences), 1), 3),
            }

    def retrieve_knowledge(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """检索持久知识。"""
        q_words = set(query.lower().split())
        scored = []
        for kid, knowledge in self._persistent_knowledge.items():
            summary = knowledge.get("summary", "").lower()
            score = sum(1 for w in q_words if w in summary)
            if score > 0:
                scored.append((knowledge, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [k for k, _ in scored[:top_k]]

    def statistics(self) -> Dict[str, Any]:
        return {"persistent_knowledge_entries": len(self._persistent_knowledge)}


# ---------------------------------------------------------------------------
# DreamReplay
# ---------------------------------------------------------------------------

class DreamReplay:
    """梦-回放——对抗灾难性遗忘。

    在 REM 睡眠阶段, 随机回放持久知识以强化记忆。
    与 FadeMem/Ebbinghaus 遗忘曲线协同作用。

    Parameters
    ----------
    replay_ratio : float
        回放比例——持久知识中回放的比例。
    """

    def __init__(self, replay_ratio: float = 0.1) -> None:
        self.replay_ratio = replay_ratio
        self._dream_log: deque = deque(maxlen=100)
        self._lock = threading.RLock()

    def generate_dreams(
        self, distiller: KnowledgeDistiller, n_dreams: int = 5
    ) -> List[Dict[str, Any]]:
        """生成梦回放序列——随机采样持久知识进行扰动回放。

        对抗遗忘策略:
        - 随机采样持久知识
        - 轻微扰动 (noise injection)
        - 以变体形式重放, 强化泛化
        """
        with self._lock:
            all_knowledge = list(distiller._persistent_knowledge.items())
            if not all_knowledge:
                return []

            # 采样
            sample_size = min(n_dreams, int(len(all_knowledge) * self.replay_ratio), len(all_knowledge))
            if sample_size == 0:
                return []

            sampled = random.sample(all_knowledge, sample_size)

            dreams = []
            for kid, knowledge in sampled:
                # 扰动回放——轻微改变以增强泛化
                dream = {
                    "dream_id": f"dream_{int(time.time()*1e6)}_{len(dreams)}",
                    "source_knowledge_id": kid,
                    "original_summary": knowledge.get("summary", ""),
                    "variation": self._perturb(knowledge),
                    "importance": knowledge.get("importance", 0.5),
                    "timestamp": time.time(),
                }
                dreams.append(dream)
                self._dream_log.append(dream)

            logger.info("DreamReplay: generated %d dreams from %d knowledge entries", len(dreams), len(all_knowledge))
            return dreams

    def _perturb(self, knowledge: Dict[str, Any]) -> str:
        """轻微扰动——模拟记忆变体。"""
        summary = knowledge.get("summary", "")
        if not summary:
            return ""

        words = summary.split()
        if len(words) <= 1:
            return summary

        # 随机替换或重排1-2个词
        modified = list(words)
        if len(modified) >= 3:
            swap_idx = random.randint(0, len(modified) - 2)
            modified[swap_idx], modified[swap_idx + 1] = modified[swap_idx + 1], modified[swap_idx]

        return " ".join(modified)

    def statistics(self) -> Dict[str, Any]:
        return {"total_dreams": len(self._dream_log)}


# ---------------------------------------------------------------------------
# WakePhase
# ---------------------------------------------------------------------------

class WakePhase:
    """清醒阶段——接收信息、处理任务。"""

    def __init__(self) -> None:
        self._active_experiences: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        self._exp_count: int = 0

    def receive(self, content: str, importance: float = 0.5, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """接收新信息。"""
        with self._lock:
            self._exp_count += 1
            exp = {
                "id": f"exp_{self._exp_count}_{int(time.time()*1e6)}",
                "content": content,
                "importance": importance,
                "metadata": metadata or {},
                "received_at": time.time(),
            }
            self._active_experiences.append(exp)
            return exp

    def flush_experiences(self) -> List[Dict[str, Any]]:
        """清空并返回所有活跃经验。"""
        with self._lock:
            exps = list(self._active_experiences)
            self._active_experiences.clear()
            return exps

    def statistics(self) -> Dict[str, Any]:
        return {"active_experiences": len(self._active_experiences)}


# ---------------------------------------------------------------------------
# SleepPhase
# ---------------------------------------------------------------------------

class SleepPhase:
    """睡眠阶段——主动回放+知识蒸馏。

    分阶段:
    - NREM_LIGHT: 初步整理, 按重要性排序
    - NREM_DEEP: 知识蒸馏到持久存储
    - REM_DREAM: 梦回放对抗遗忘
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def nrem_light(self, experiences: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """NREM Light——按重要性排序并筛选。"""
        with self._lock:
            # 按重要性降序
            sorted_exp = sorted(experiences, key=lambda e: e.get("importance", 0.5), reverse=True)

            # 重要性 < 0.2 的标记为遗忘候选
            forgotten = [e for e in sorted_exp if e.get("importance", 0.5) < 0.2]
            kept = [e for e in sorted_exp if e.get("importance", 0.5) >= 0.2]

            logger.info("NREM Light: %d kept, %d candidate for forgetting", len(kept), len(forgotten))
            return kept

    def nrem_deep(self, experiences: List[Dict[str, Any]], distiller: KnowledgeDistiller) -> Dict[str, Any]:
        """NREM Deep——知识蒸馏。"""
        with self._lock:
            result = distiller.distill(experiences)
            logger.info("NREM Deep: distilled %d knowledge entries", result["knowledge_entries"])
            return result

    def rem_dream(self, distiller: KnowledgeDistiller, dream_replay: DreamReplay) -> List[Dict[str, Any]]:
        """REM Dream——梦回放。"""
        with self._lock:
            dreams = dream_replay.generate_dreams(distiller)
            logger.info("REM Dream: %d dreams generated", len(dreams))
            return dreams

    def statistics(self) -> Dict[str, Any]:
        return {"status": "ready"}


# ---------------------------------------------------------------------------
# SleepWakeConsolidator
# ---------------------------------------------------------------------------

class SleepWakeConsolidator:
    """睡眠-觉醒巩固周期系统 (Google: Language Models Need Sleep)。

    Parameters
    ----------
    wake_duration_seconds : float
        清醒阶段持续时间 (秒)。
    sleep_duration_seconds : float
        睡眠阶段持续时间 (秒)。
    compression_ratio : float
        知识蒸馏压缩比。
    """

    def __init__(
        self,
        wake_duration_seconds: float = 600.0,
        sleep_duration_seconds: float = 120.0,
        compression_ratio: float = 0.1,
    ) -> None:
        self.rhythm = SleepWakeRhythm(
            wake_duration_seconds=wake_duration_seconds,
            sleep_duration_seconds=sleep_duration_seconds,
        )
        self.wake_phase = WakePhase()
        self.sleep_phase = SleepPhase()
        self.knowledge_distiller = KnowledgeDistiller(compression_ratio=compression_ratio)
        self.dream_replay = DreamReplay()
        self._current_phase = SLC_ConsolidationPhase.AWAKE
        self._cycles: List[ConsolidationCycle] = []
        self._cycle_count: int = 0
        self._lock = threading.RLock()

        logger.info(
            "SleepWakeConsolidator initialized [wake=%.0fs sleep=%.0fs cr=%.2f]",
            wake_duration_seconds, sleep_duration_seconds, compression_ratio,
        )

    def receive(self, content: str, importance: float = 0.5) -> Dict[str, Any]:
        """清醒阶段——接收信息。"""
        return self.wake_phase.receive(content, importance)

    def sleep(self) -> Dict[str, Any]:
        """触发睡眠巩固周期。"""
        with self._lock:
            self._cycle_count += 1
            cycle = ConsolidationCycle(
                cycle_id=f"cycle_{self._cycle_count}_{int(time.time()*1e6)}",
            )

            # 1. NREM Light: 整理
            self._current_phase = SLC_ConsolidationPhase.NREM_LIGHT
            experiences = self.wake_phase.flush_experiences()
            if not experiences:
                cycle.ended_at = time.time()
                self._current_phase = SLC_ConsolidationPhase.AWAKE
                return {"cycle": cycle.cycle_id, "skipped": True, "reason": "No experiences to consolidate"}

            kept = self.sleep_phase.nrem_light(experiences)
            cycle.experiences_processed = len(kept)

            # 2. NREM Deep: 蒸馏
            self._current_phase = SLC_ConsolidationPhase.NREM_DEEP
            distillation = self.sleep_phase.nrem_deep(kept, self.knowledge_distiller)
            cycle.knowledge_distilled = distillation["knowledge_entries"]

            # 3. REM Dream: 梦回放
            self._current_phase = SLC_ConsolidationPhase.REM_DREAM
            dreams = self.sleep_phase.rem_dream(self.knowledge_distiller, self.dream_replay)
            cycle.dreams_generated = len(dreams)

            # 遗忘计数
            cycle.forgetting_prevented = len(kept) - distillation["knowledge_entries"]

            # 回到清醒
            self._current_phase = SLC_ConsolidationPhase.AWAKE
            cycle.ended_at = time.time()
            cycle.phase = SLC_ConsolidationPhase.AWAKE
            self._cycles.append(cycle)

            return {
                "cycle_id": cycle.cycle_id,
                "experiences_processed": cycle.experiences_processed,
                "knowledge_distilled": cycle.knowledge_distilled,
                "dreams_generated": cycle.dreams_generated,
                "forgetting_prevented": cycle.forgetting_prevented,
                "duration_seconds": round(cycle.ended_at - cycle.started_at, 2),
            }

    def phase(self) -> str:
        return self._current_phase.name

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_cycles": len(self._cycles),
                "current_phase": self._current_phase.name,
                "knowledge": self.knowledge_distiller.statistics(),
                "dreams": self.dream_replay.statistics(),
                "active_experiences": self.wake_phase.statistics()["active_experiences"],
            }
