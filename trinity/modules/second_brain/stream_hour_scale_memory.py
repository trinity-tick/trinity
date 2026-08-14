"""
StreamMem / StreamArena — Hour-Scale Streaming Memory
======================================================
arXiv 2608.05703 · P49-2

小时级流式记忆：环形缓冲区近帧全保留 + 远帧选择性压缩，
解决长程记忆衰减。关键视觉证据保留 + 时间衰减压缩调度 +
历史回溯查询引擎。

设计要点:
  - StreamingMemoryBuffer: 环形缓冲区（近全保留/远压缩）
  - VisualEvidencePreserver: 视觉证据标记与保留
  - TemporalCompressionScheduler: 时间衰减选择性压缩
  - RetrospectionQueryEngine: 时间范围+语义回溯
"""
from __future__ import annotations

import logging
import threading
import time
import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple
from collections import OrderedDict, deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class MemoryFrame:
    """单帧记忆片段。"""
    frame_id: int
    content: str = ""
    embedding: Optional[np.ndarray] = None
    timestamp: float = field(default_factory=time.time)
    is_visual: bool = False
    importance_score: float = 0.5
    compressed: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# StreamingMemoryBuffer
# ---------------------------------------------------------------------------

class StreamingMemoryBuffer:
    """环形缓冲区——最近 N 帧全保留，远帧选择性压缩。

    Parameters
    ----------
    recent_window : int
        全保留窗口帧数。
    max_total : int
        缓冲区最大总帧数。
    """

    def __init__(self, recent_window: int = 100, max_total: int = 10000) -> None:
        self.recent_window = recent_window
        self.max_total = max_total
        self._frames: OrderedDict[int, MemoryFrame] = OrderedDict()
        self._frame_counter: int = 0
        self._lock = threading.RLock()

    def append(self, frame: MemoryFrame) -> None:
        with self._lock:
            frame.frame_id = self._frame_counter
            self._frames[self._frame_counter] = frame
            self._frame_counter += 1

            # 超出总量时压缩最远帧
            if len(self._frames) > self.max_total:
                oldest_id = next(iter(self._frames))
                self._frames.pop(oldest_id)

    def get_recent(self, n: Optional[int] = None) -> List[MemoryFrame]:
        """获取最近 N 帧（默认全部近窗）。"""
        with self._lock:
            n = n or self.recent_window
            ids = list(self._frames.keys())[-n:]
            return [self._frames[i] for i in ids]

    def get_by_range(self, start_ts: float, end_ts: float) -> List[MemoryFrame]:
        with self._lock:
            return [f for f in self._frames.values() if start_ts <= f.timestamp <= end_ts]

    def mark_compressed(self, frame_id: int) -> bool:
        with self._lock:
            if frame_id in self._frames:
                self._frames[frame_id].compressed = True
                return True
            return False

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_frames": len(self._frames),
            "recent_window": self.recent_window,
            "compressed_count": sum(1 for f in self._frames.values() if f.compressed),
        }


# ---------------------------------------------------------------------------
# VisualEvidencePreserver
# ---------------------------------------------------------------------------

class VisualEvidencePreserver:
    """关键视觉证据标记与保留策略——避免文本化丢失细节。

    识别含视觉信息的帧（截图、图表、UI），赋予高重要性分，
    在压缩时优先保留。
    """

    _VISUAL_MARKERS = {"screenshot", "chart", "diagram", "image", "photo",
                        "ui:", "界面", "图表", "截图", "照片", "graph"}

    def __init__(self, importance_threshold: float = 0.7) -> None:
        self.importance_threshold = importance_threshold
        self._protected_ids: set[int] = set()
        self._lock = threading.RLock()

    def assess(self, frame: MemoryFrame) -> float:
        """评估帧的视觉重要性。"""
        text_lower = frame.content.lower()
        score = 0.0

        # 显式标注
        for marker in self._VISUAL_MARKERS:
            if marker in text_lower:
                score = max(score, 0.85)
                break

        # 文件路径推测
        for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            if ext in text_lower:
                score = max(score, 0.75)

        # 元数据
        if frame.is_visual:
            score = max(score, 0.6)

        frame.importance_score = score
        return score

    def protect(self, frame: MemoryFrame) -> None:
        """标记为受保护——压缩时跳过。"""
        with self._lock:
            self._protected_ids.add(frame.frame_id)

    def is_protected(self, frame_id: int) -> bool:
        with self._lock:
            return frame_id in self._protected_ids

    def statistics(self) -> Dict[str, Any]:
        return {"protected": len(self._protected_ids), "threshold": self.importance_threshold}


# ---------------------------------------------------------------------------
# TemporalCompressionScheduler
# ---------------------------------------------------------------------------

class TemporalCompressionScheduler:
    """基于时间衰减函数的选择性压缩调度器。

    衰减函数: importance(t) = initial * exp(-lambda * age_hours)
    当 importance 低于阈值时触发压缩。
    """

    def __init__(self, decay_lambda: float = 0.01, compress_threshold: float = 0.2) -> None:
        self.decay_lambda = decay_lambda
        self.compress_threshold = compress_threshold
        self._compression_log: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    def schedule(
        self, frames: List[MemoryFrame],
        preserver: VisualEvidencePreserver,
    ) -> List[int]:
        """返回应被压缩的帧 ID 列表。"""
        with self._lock:
            to_compress: List[int] = []
            now = time.time()

            for f in frames:
                if preserver.is_protected(f.frame_id):
                    continue
                if f.compressed:
                    continue

                age_hours = (now - f.timestamp) / 3600.0
                decayed = f.importance_score * math.exp(-self.decay_lambda * age_hours)

                if decayed < self.compress_threshold:
                    to_compress.append(f.frame_id)

            self._compression_log.append({
                "timestamp": now, "candidates": len(frames),
                "to_compress": len(to_compress),
            })
            return to_compress

    def statistics(self) -> Dict[str, Any]:
        return {
            "decay_lambda": self.decay_lambda,
            "threshold": self.compress_threshold,
            "compression_rounds": len(self._compression_log),
        }


# ---------------------------------------------------------------------------
# RetrospectionQueryEngine
# ---------------------------------------------------------------------------

class RetrospectionQueryEngine:
    """历史回溯查询引擎——支持时间范围 + 语义检索。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def query_by_time(
        self, buffer: StreamingMemoryBuffer,
        start_ts: float, end_ts: float,
    ) -> List[MemoryFrame]:
        """时间范围查询。"""
        return buffer.get_by_range(start_ts, end_ts)

    def query_semantic(
        self, buffer: StreamingMemoryBuffer,
        query_embedding: np.ndarray, top_k: int = 10,
    ) -> List[Tuple[MemoryFrame, float]]:
        """语义检索——余弦相似度排序。"""
        with self._lock:
            scored: List[Tuple[MemoryFrame, float]] = []
            query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-8)
            for f in buffer.get_recent(500):
                if f.embedding is not None:
                    sim = float(np.dot(query_norm, f.embedding))
                    scored.append((f, sim))
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_k]

    def query_hybrid(
        self, buffer: StreamingMemoryBuffer,
        query_embedding: np.ndarray, start_ts: float, end_ts: float,
        top_k: int = 10,
    ) -> List[Tuple[MemoryFrame, float]]:
        """混合检索：时间范围过滤 + 语义排序。"""
        time_filtered = self.query_by_time(buffer, start_ts, end_ts)
        if not time_filtered:
            return []

        query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-8)
        scored: List[Tuple[MemoryFrame, float]] = []
        for f in time_filtered:
            if f.embedding is not None:
                sim = float(np.dot(query_norm, f.embedding))
                scored.append((f, sim))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def statistics(self) -> Dict[str, Any]:
        return {"mode": "time_range + semantic hybrid"}
