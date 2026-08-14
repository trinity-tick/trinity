"""Light-Omni Reflex Video Controller (P34) — 对标 Light-Omni (arXiv 2607.05511)

实现"反射优先于推理"的 Agentic Video Understanding：

- ReflexVideoController: 快速通道检测已知模式，反射匹配 → 跳过深度推理
- PerceptFrameCache: 缓存关键感知帧，避免重复编码
- VideoMemoryQuickPath: 长期记忆索引 → 反射匹配 → 跳过 VLM 推理

设计要点：
- 反射通道（Reflex Channel）：毫秒级模式匹配，已知动作直接返回
- 推理通道（Reasoning Channel）：仅当反射未命中时触发 VLM 深度推理
- 帧缓存使用哈希键索引，基于 LRU + 重要性保留
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ReflexDecision(Enum):
    """反射/推理路径决策。"""
    DEEP_REASON = "deep_reason"       # 未命中模式 → 深度推理
    QUICK_REFLEX = "quick_reflex"     # 命中已知模式 → 快速反射
    CACHED = "cached"                 # 完全命中缓存 → 直接返回


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class PerceptFrame:
    """感知帧：视频关键帧 + 提取特征。"""
    frame_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    image_hash: str = ""
    key_features: list[str] = field(default_factory=list)
    timestamp_ms: float = 0.0
    importance: float = 1.0


@dataclass
class ReflexPattern:
    """反射模式：已知动作-帧映射。"""
    pattern_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    action_label: str = ""
    frame_hashes: list[str] = field(default_factory=list)
    match_score: float = 0.0
    hit_count: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class VideoContext:
    """视频上下文：连续帧序列 + 已识别模式。"""
    context_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    frames: list[PerceptFrame] = field(default_factory=list)
    timeline: list[float] = field(default_factory=list)
    identified_patterns: list[str] = field(default_factory=list)


@dataclass
class FrameCacheEntry:
    """帧缓存条目。"""
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    frame: PerceptFrame = field(default_factory=PerceptFrame)
    hash_key: str = ""
    access_count: int = 0
    cached_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# ReflexVideoController — Reflex Before Reasoning
# ---------------------------------------------------------------------------

class ReflexVideoController:
    """反射视频控制器：快速通道检测 → 未命中则降级深度推理。

    对标 Light-Omni：反射优先于推理（reflex over reasoning）。
    """

    def __init__(self, match_threshold: float = 0.75) -> None:
        self._lock = threading.RLock()
        self._threshold = match_threshold
        self._known_patterns: dict[str, ReflexPattern] = {}
        self._decision_log: list[tuple[str, ReflexDecision, float]] = []

    def process_frame(self, frame: PerceptFrame) -> tuple[ReflexDecision, Optional[str]]:
        """处理单帧：先尝试反射匹配，未命中返回 DEEP_REASON。"""
        with self._lock:
            for pid, pattern in self._known_patterns.items():
                score = self._match_score(frame, pattern)
                if score >= self._threshold:
                    pattern.hit_count += 1
                    pattern.match_score = (pattern.match_score * 0.9 + score * 0.1)
                    self._decision_log.append((frame.frame_id, ReflexDecision.QUICK_REFLEX, score))
                    return ReflexDecision.QUICK_REFLEX, pattern.action_label
            self._decision_log.append((frame.frame_id, ReflexDecision.DEEP_REASON, 0.0))
            return ReflexDecision.DEEP_REASON, None

    def register_pattern(self, action_label: str,
                         frames: list[PerceptFrame]) -> ReflexPattern:
        """从推理结果注册新反射模式。"""
        with self._lock:
            pattern = ReflexPattern(
                action_label=action_label,
                frame_hashes=[f.image_hash for f in frames],
            )
            self._known_patterns[pattern.pattern_id] = pattern
            logger.info("ReflexController registered pattern: %s", action_label)
            return pattern

    def _match_score(self, frame: PerceptFrame, pattern: ReflexPattern) -> float:
        """帧与模式的匹配分数（基于特征重叠）。"""
        if not frame.key_features or not pattern.frame_hashes:
            return 0.0
        hash_match = 1.0 if frame.image_hash in pattern.frame_hashes else 0.0
        feature_overlap = len(set(frame.key_features) & set(
            p.frame_hashes for p in self._known_patterns.values()
        )) / max(1, len(frame.key_features))
        return 0.7 * hash_match + 0.3 * feature_overlap

    def reflex_ratio(self) -> float:
        """反射命中率。"""
        with self._lock:
            if not self._decision_log:
                return 0.0
            quick = sum(1 for _, d, _ in self._decision_log
                        if d == ReflexDecision.QUICK_REFLEX)
            return quick / len(self._decision_log)

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "ReflexVideoController",
                "patterns": len(self._known_patterns),
                "decisions": len(self._decision_log),
                "reflex_ratio": round(self.reflex_ratio(), 3),
            }


# ---------------------------------------------------------------------------
# PerceptFrameCache — 感知帧缓存
# ---------------------------------------------------------------------------

class PerceptFrameCache:
    """感知帧缓存：基于 LRU + 重要性保留的帧缓存。

    避免对相同/相似帧重复编码处理。
    """

    def __init__(self, max_entries: int = 256) -> None:
        self._lock = threading.RLock()
        self._max_entries = max_entries
        self._cache: OrderedDict[str, FrameCacheEntry] = OrderedDict()

    def get(self, image_hash: str) -> Optional[PerceptFrame]:
        """按哈希获取缓存帧。"""
        with self._lock:
            entry = self._cache.get(image_hash)
            if entry:
                entry.access_count += 1
                self._cache.move_to_end(image_hash)
                return entry.frame
            return None

    def put(self, frame: PerceptFrame) -> str:
        """缓存一个感知帧。"""
        with self._lock:
            key = frame.image_hash or frame.frame_id
            if key in self._cache:
                self._cache[key].access_count += 1
                self._cache.move_to_end(key)
                return key
            if len(self._cache) >= self._max_entries:
                # Evict least important entry (lowest access_count × importance)
                evict_key = min(
                    self._cache.keys(),
                    key=lambda k: (self._cache[k].access_count *
                                   self._cache[k].frame.importance),
                )
                del self._cache[evict_key]
                logger.debug("FrameCache evicted: %s", evict_key)
            entry = FrameCacheEntry(frame=frame, hash_key=key)
            self._cache[key] = entry
            return key

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "PerceptFrameCache",
                "size": len(self._cache),
                "max_entries": self._max_entries,
                "utilization": round(len(self._cache) / self._max_entries, 3),
            }


# ---------------------------------------------------------------------------
# VideoMemoryQuickPath — 长期记忆快速路径
# ---------------------------------------------------------------------------

class VideoMemoryQuickPath:
    """视频记忆快速路径：长期记忆索引 → 反射匹配 → 跳过深度推理。

    对标 Light-Omni 的 long-term memory index + quick reflex path。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._controller = ReflexVideoController()
        self._cache = PerceptFrameCache()
        self._memory_index: dict[str, list[PerceptFrame]] = {}

    def index_video(self, video_id: str,
                    frames: list[PerceptFrame]) -> None:
        """将视频关键帧索引至长期记忆。"""
        with self._lock:
            self._memory_index[video_id] = frames
            for f in frames:
                self._cache.put(f)
            logger.info("QuickPath indexed video %s: %d frames", video_id, len(frames))

    def query(self, frame: PerceptFrame) -> tuple[ReflexDecision, Optional[str]]:
        """查询快速路径：先查缓存 → 再反射匹配。"""
        with self._lock:
            cached = self._cache.get(frame.image_hash)
            if cached:
                return ReflexDecision.CACHED, "cached_response"
            decision, action = self._controller.process_frame(frame)
            if decision == ReflexDecision.QUICK_REFLEX:
                self._cache.put(frame)
            return decision, action

    def learn_from_reasoning(self, action_label: str,
                             frames: list[PerceptFrame]) -> ReflexPattern:
        """从深度推理结果学习新反射模式。"""
        with self._lock:
            pattern = self._controller.register_pattern(action_label, frames)
            for f in frames:
                self._cache.put(f)
            logger.info("QuickPath learned pattern: %s from %d frames",
                        action_label, len(frames))
            return pattern

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "VideoMemoryQuickPath",
                "indexed_videos": len(self._memory_index),
                "controller": self._controller.statistics(),
                "cache": self._cache.statistics(),
            }


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

def quick_reflex(
    frame: PerceptFrame,
    quick_path: Optional[VideoMemoryQuickPath] = None,
) -> dict[str, Any]:
    """便捷函数：对单帧执行反射优先处理。

    Returns:
        dict with decision + action + cache status.
    """
    qp = quick_path or VideoMemoryQuickPath()
    decision, action = qp.query(frame)
    return {
        "decision": decision.value,
        "action": action,
        "controller_stats": qp.statistics(),
    }
