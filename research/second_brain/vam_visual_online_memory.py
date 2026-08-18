"""
# status: orphan (2026-08-15 audit, not in runtime path)
VAM — Visual Online Memory for Agents
======================================
arXiv 2606.01435 · P46-2

视觉记忆编码器将截图/图片转为视觉记忆向量, 支持以图搜图和文本搜图。
每会话视觉记忆缓冲区按时间衰减。

设计要点:
  - VAMVisualMemoryEncoder: 视觉记忆向量编码 (哈希+维度映射)
  - VisualMemoryIndex: 在线索引, 支持 image-to-image / text-to-image 检索
  - VisualContextInjector: 相关视觉记忆注入当前上下文
  - VAMSessionMemory: 会话级缓冲区, 按时间衰减
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class VisualMemoryRecord:
    """视觉记忆记录——单条视觉记忆条目。"""
    record_id: str
    image_hash: str          # 图片感知哈希
    visual_vector: np.ndarray  # 视觉向量 (dim,)
    text_description: str = ""
    source: str = ""          # 截图/上传/视频帧
    timestamp: float = field(default_factory=time.time)
    relevance: float = 1.0     # 当前相关性 (衰减)


# ---------------------------------------------------------------------------
# VAMVisualMemoryEncoder
# ---------------------------------------------------------------------------

class VAMVisualMemoryEncoder:
    """视觉记忆编码器——将截图/图片转为视觉记忆向量。

    使用感知哈希 (phash) + 位置敏感散列将图片特征映射到固定维向量。
    """

    def __init__(self, vector_dim: int = 128) -> None:
        self.vector_dim = vector_dim
        self._lock = threading.RLock()

    def encode(self, image_data: bytes, description: str = "") -> Tuple[str, np.ndarray]:
        """编码图片数据为 (hash, vector)。

        Parameters
        ----------
        image_data : bytes
            图片原始字节。
        description : str
            图片文本描述 (可选)。

        Returns
        -------
        Tuple[str, np.ndarray]
            (感知哈希, 视觉向量)
        """
        with self._lock:
            # 感知哈希: 对字节做滚动散列
            phash = self._compute_phash(image_data)
            # 将 hash 映射到固定维向量
            vec = self._hash_to_vector(phash)
            # 注入文本描述信号
            if description:
                text_signal = self._text_to_signal(description)
                vec = 0.85 * vec + 0.15 * text_signal
                norm = float(np.linalg.norm(vec))
                if norm > 0:
                    vec /= norm
            return phash, vec.astype(np.float32)

    def _compute_phash(self, data: bytes) -> str:
        """简单感知哈希——分块取均值的符号。"""
        chunks = [data[i:i+64] for i in range(0, min(len(data), 4096), 64)]
        hash_vals = []
        for ch in chunks:
            if ch:
                hash_vals.append(str(sum(ch) % 256))
        # 不足部分补零
        while len(hash_vals) < 64:
            hash_vals.append("0")
        return ":".join(hash_vals[:64])

    def _hash_to_vector(self, phash: str) -> np.ndarray:
        """将感知哈希转换为向量。"""
        parts = phash.split(":")
        vec = np.zeros(self.vector_dim, dtype=np.float32)
        for i, p in enumerate(parts):
            idx = (int(p) * 31) % self.vector_dim
            vec[idx] += float(int(p)) / 255.0
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec

    def _text_to_signal(self, text: str) -> np.ndarray:
        """文本描述 → 向量信号。"""
        vec = np.zeros(self.vector_dim, dtype=np.float32)
        for i, ch in enumerate(text[:self.vector_dim]):
            idx = (ord(ch) * 7) % self.vector_dim
            vec[idx] += 1.0 / (i + 1)
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec

    def statistics(self) -> Dict[str, Any]:
        return {"vector_dim": self.vector_dim}


# ---------------------------------------------------------------------------
# VisualMemoryIndex
# ---------------------------------------------------------------------------

class VisualMemoryIndex:
    """在线视觉记忆索引——支持以图搜图和文本搜图。"""

    def __init__(self) -> None:
        self._records: List[VisualMemoryRecord] = []
        self._text_index: Dict[str, List[int]] = {}  # keyword → record indices
        self._lock = threading.RLock()

    def add(self, record: VisualMemoryRecord) -> None:
        """添加视觉记忆记录。"""
        with self._lock:
            idx = len(self._records)
            self._records.append(record)

            # 文本索引
            keywords = self._extract_keywords(record.text_description)
            for kw in keywords:
                self._text_index.setdefault(kw, []).append(idx)

    def _extract_keywords(self, text: str) -> List[str]:
        return [w.lower() for w in text.split() if len(w) > 2]

    def search_by_image(self, query_vec: np.ndarray, top_k: int = 10) -> List[Dict[str, Any]]:
        """以图搜图——余弦相似度。"""
        scored = []
        for i, rec in enumerate(self._records):
            sim = float(np.dot(query_vec, rec.visual_vector))
            scored.append((sim, i))
        scored.sort(key=lambda x: x[0], reverse=True)

        results = []
        for sim, i in scored[:top_k]:
            r = self._records[i]
            results.append({
                "record_id": r.record_id, "similarity": round(sim, 4),
                "description": r.text_description, "source": r.source,
            })
        return results

    def search_by_text(self, text: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """文本搜图——关键词匹配 + 向量相似度。"""
        keywords = self._extract_keywords(text)
        candidate_indices: Dict[int, float] = {}
        for kw in keywords:
            for idx in self._text_index.get(kw, []):
                candidate_indices[idx] = candidate_indices.get(idx, 0) + 1

        ranked = sorted(candidate_indices.items(), key=lambda x: x[1], reverse=True)
        results = []
        for idx, score in ranked[:top_k]:
            r = self._records[idx]
            results.append({
                "record_id": r.record_id, "match_score": score,
                "description": r.text_description, "source": r.source,
            })
        return results

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_records": len(self._records),
            "text_keywords": len(self._text_index),
        }


# ---------------------------------------------------------------------------
# VAMSessionMemory
# ---------------------------------------------------------------------------

class VAMSessionMemory:
    """每会话的视觉记忆缓冲区——按时间衰减。

    Parameters
    ----------
    decay_half_life : float
        衰减半衰期 (秒), 默认 600 (10分钟)。
    max_records : int
        缓冲区最大记录数。
    """

    def __init__(self, decay_half_life: float = 600.0, max_records: int = 50) -> None:
        self.decay_half_life = decay_half_life
        self.max_records = max_records
        self._buffer: List[VisualMemoryRecord] = []
        self._lock = threading.RLock()

    def push(self, record: VisualMemoryRecord) -> None:
        """推入缓冲区, 超过上限则淘汰最旧的。"""
        with self._lock:
            self._buffer.append(record)
            # 淘汰: 按 relevance 排序, 保留 top N
            if len(self._buffer) > self.max_records:
                self._decay_all()
                self._buffer.sort(key=lambda r: r.relevance, reverse=True)
                self._buffer = self._buffer[:self.max_records]

    def _decay_all(self) -> None:
        now = time.time()
        for r in self._buffer:
            age = max(0.0, now - r.timestamp)
            r.relevance = 2.0 ** (-age / self.decay_half_life)

    def get_active(self, min_relevance: float = 0.1) -> List[VisualMemoryRecord]:
        """获取当前活跃 (relevance > threshold) 的记录。"""
        with self._lock:
            self._decay_all()
            return [r for r in self._buffer if r.relevance >= min_relevance]

    def statistics(self) -> Dict[str, Any]:
        return {
            "buffer_size": len(self._buffer),
            "max_records": self.max_records,
            "decay_half_life": self.decay_half_life,
        }


# ---------------------------------------------------------------------------
# VisualContextInjector
# ---------------------------------------------------------------------------

class VisualContextInjector:
    """视觉上下文注入器——将相关视觉记忆注入当前 agent 上下文。

    Parameters
    ----------
    max_context_items : int
        每次注入的最大视觉记忆条目数。
    """

    def __init__(self, max_context_items: int = 5) -> None:
        self.max_context_items = max_context_items
        self._lock = threading.RLock()

    def inject(
        self, session_memory: VAMSessionMemory, query_vec: np.ndarray,
        index: Optional[VisualMemoryIndex] = None,
    ) -> List[Dict[str, Any]]:
        """注入视觉上下文——从会话记忆和索引中取最相关的视觉记忆。

        Returns
        -------
        List[Dict]
            注入的视觉上下文条目列表。
        """
        with self._lock:
            context_items: List[Dict[str, Any]] = []

            # 从会话缓冲区获取活跃记录
            active = session_memory.get_active()
            for r in active:
                sim = float(np.dot(query_vec, r.visual_vector))
                context_items.append({
                    "record_id": r.record_id,
                    "similarity": round(sim, 4),
                    "description": r.text_description,
                    "source": "session",
                })

            # 从索引搜索补充
            if index is not None:
                index_results = index.search_by_image(query_vec, self.max_context_items)
                for ir in index_results:
                    if ir["record_id"] not in {c["record_id"] for c in context_items}:
                        ir["source"] = "index"
                        context_items.append(ir)

            # 按相似度排序, 截断
            context_items.sort(key=lambda x: x.get("similarity", 0), reverse=True)
            return context_items[:self.max_context_items]

    def statistics(self) -> Dict[str, Any]:
        return {"max_context_items": self.max_context_items}
