"""
MemPAL Heterogeneous Personalization — arXiv 2511.13410
========================================================
P45-2

行为轨迹嵌入 + 对话语义嵌入 + 异质注意力路由 = 个性化检索。
按用户-任务双索引的个性化记忆存储。

设计要点:
  - MemPALBehaviourEmbedding: 工具调用序列编码 → 行为向量
  - ConversationEmbeddingExtractor: 对话语义嵌入提取
  - HeterogeneousPersonalizationRouter: 异质注意力融合路由
  - PersonalizationMemoryIndex: 用户-任务双索引存储
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from collections import OrderedDict

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MemPALBehaviourEmbedding
# ---------------------------------------------------------------------------

class MemPALBehaviourEmbedding:
    """行为轨迹嵌入——工具调用序列编码。

    将工具调用序列 (tool_name, args_summary, result_summary) 编码为固定维度向量。
    """

    def __init__(self, embedding_dim: int = 128) -> None:
        self.embedding_dim = embedding_dim
        self._tool_vocabulary: Dict[str, int] = {}
        self._vocab_count: int = 0
        self._lock = threading.RLock()

    def encode_sequence(self, tool_calls: List[Dict[str, Any]]) -> np.ndarray:
        """将工具调用序列编码为行为嵌入向量。

        Parameters
        ----------
        tool_calls : List[Dict]
            每项含 tool_name, args_summary, result_summary.

        Returns
        -------
        np.ndarray
            shape (embedding_dim,) 的行为嵌入。
        """
        with self._lock:
            vec = np.zeros(self.embedding_dim, dtype=np.float32)
            if not tool_calls:
                return vec

            for i, tc in enumerate(tool_calls):
                name = tc.get("tool_name", "unknown")
                # 登记词汇
                if name not in self._tool_vocabulary:
                    self._vocab_count += 1
                    self._tool_vocabulary[name] = self._vocab_count

                idx = self._tool_vocabulary[name] % self.embedding_dim
                # 位置编码: 序列中的位置影响向量权重
                weight = 1.0 / (1.0 + i * 0.1)
                vec[idx] += weight

                # 结果摘要散列到相邻维度
                summary = str(tc.get("result_summary", ""))
                if summary:
                    hash_val = hash(summary) % self.embedding_dim
                    vec[(idx + 1) % self.embedding_dim] += weight * 0.5

            # L2 归一化
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec /= norm

            return vec

    def similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """余弦相似度。"""
        dot = float(np.dot(a, b))
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def statistics(self) -> Dict[str, Any]:
        return {
            "dim": self.embedding_dim,
            "vocabulary_size": self._vocab_count,
        }


# ---------------------------------------------------------------------------
# ConversationEmbeddingExtractor
# ---------------------------------------------------------------------------

class ConversationEmbeddingExtractor:
    """对话语义嵌入提取器。

    将对话文本编码为语义向量, 使用 TF-IDF 风格加权。
    """

    def __init__(self, embedding_dim: int = 128) -> None:
        self.embedding_dim = embedding_dim
        self._idf: Dict[str, float] = {}
        self._doc_count: int = 0
        self._lock = threading.RLock()

    def encode(self, conversation_text: str) -> np.ndarray:
        """将对话文本编码为语义向量。"""
        with self._lock:
            self._doc_count += 1
            words = self._tokenize(conversation_text)
            if not words:
                return np.zeros(self.embedding_dim, dtype=np.float32)

            vec = np.zeros(self.embedding_dim, dtype=np.float32)

            for w in words:
                if w not in self._idf:
                    self._idf[w] = 1.0
                else:
                    self._idf[w] += 1.0

            # TF-IDF 风格加权
            for w in set(words):
                tf = words.count(w) / len(words)
                idf = np.log(1.0 + self._doc_count / max(1.0, self._idf[w]))
                idx = abs(hash(w)) % self.embedding_dim
                vec[idx] += tf * idf

            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec /= norm

            return vec

    def _tokenize(self, text: str) -> List[str]:
        """简单分词。"""
        return [w.lower().strip(".,!?;:") for w in text.split() if len(w) > 2]

    def statistics(self) -> Dict[str, Any]:
        return {"dim": self.embedding_dim, "doc_count": self._doc_count}


# ---------------------------------------------------------------------------
# HeterogeneousPersonalizationRouter
# ---------------------------------------------------------------------------

class HeterogeneousPersonalizationRouter:
    """异质注意力路由——融合行为嵌入与对话嵌入做个性化检索。

    Parameters
    ----------
    alpha : float
        行为嵌入权重 (0~1), 对话嵌入权重 = 1-alpha.
    """

    def __init__(self, alpha: float = 0.5) -> None:
        self.alpha = alpha
        self._lock = threading.RLock()

    def fuse(
        self,
        behaviour_emb: np.ndarray,
        conversation_emb: np.ndarray,
    ) -> np.ndarray:
        """异质注意力融合: 行为嵌入 + 对话嵌入 → 个性化查询向量。"""
        with self._lock:
            # 确保同维度
            dim = min(len(behaviour_emb), len(conversation_emb))
            b = behaviour_emb[:dim]
            c = conversation_emb[:dim]

            fused = self.alpha * b + (1.0 - self.alpha) * c

            norm = float(np.linalg.norm(fused))
            if norm > 0:
                fused /= norm

            return fused

    def route(
        self,
        query_behaviour: np.ndarray,
        query_conversation: np.ndarray,
        memory_index: PersonalizationMemoryIndex,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """路由查询到最相关的个性化记忆条目。"""
        query_vec = self.fuse(query_behaviour, query_conversation)
        return memory_index.search(query_vec, top_k)

    def statistics(self) -> Dict[str, Any]:
        return {"alpha": self.alpha}


# ---------------------------------------------------------------------------
# PersonalizationMemoryIndex
# ---------------------------------------------------------------------------

@dataclass
class PersonalMemoryEntry:
    """个性化记忆条目。"""
    entry_id: str
    user_id: str
    task_type: str
    behaviour_emb: np.ndarray
    conversation_emb: np.ndarray
    fused_emb: np.ndarray
    content: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class PersonalizationMemoryIndex:
    """个性化记忆索引——按用户-任务双索引存储。

    Parameters
    ----------
    embedding_dim : int
        嵌入维度。
    alpha : float
        融合权重。
    """

    def __init__(self, embedding_dim: int = 128, alpha: float = 0.5) -> None:
        self.embedding_dim = embedding_dim
        self.alpha = alpha
        self._entries: List[PersonalMemoryEntry] = []
        self._user_index: Dict[str, List[int]] = {}
        self._task_index: Dict[str, List[int]] = {}
        self._lock = threading.RLock()

    def add(
        self,
        user_id: str,
        task_type: str,
        behaviour_emb: np.ndarray,
        conversation_emb: np.ndarray,
        content: Optional[Dict[str, Any]] = None,
    ) -> str:
        """添加个性化记忆条目。"""
        with self._lock:
            # 融合嵌入
            dim = min(len(behaviour_emb), len(conversation_emb))
            fused = self.alpha * behaviour_emb[:dim] + (1.0 - self.alpha) * conversation_emb[:dim]
            norm = float(np.linalg.norm(fused))
            if norm > 0:
                fused /= norm

            entry_id = f"pmem_{len(self._entries)}_{int(time.time()*1e6)}"
            entry = PersonalMemoryEntry(
                entry_id=entry_id,
                user_id=user_id,
                task_type=task_type,
                behaviour_emb=behaviour_emb[:dim],
                conversation_emb=conversation_emb[:dim],
                fused_emb=fused,
                content=content or {},
            )

            idx = len(self._entries)
            self._entries.append(entry)
            self._user_index.setdefault(user_id, []).append(idx)
            self._task_index.setdefault(task_type, []).append(idx)

            return entry_id

    def search(self, query_vec: np.ndarray, top_k: int = 10) -> List[Dict[str, Any]]:
        """余弦相似度搜索。"""
        scored = []
        for i, entry in enumerate(self._entries):
            sim = float(np.dot(query_vec, entry.fused_emb))
            scored.append((sim, i))
        scored.sort(key=lambda x: x[0], reverse=True)

        results = []
        for sim, i in scored[:top_k]:
            e = self._entries[i]
            results.append({
                "entry_id": e.entry_id,
                "user_id": e.user_id,
                "task_type": e.task_type,
                "similarity": round(sim, 4),
                "content": e.content,
            })
        return results

    def get_by_user(self, user_id: str) -> List[PersonalMemoryEntry]:
        indices = self._user_index.get(user_id, [])
        return [self._entries[i] for i in indices]

    def get_by_task(self, task_type: str) -> List[PersonalMemoryEntry]:
        indices = self._task_index.get(task_type, [])
        return [self._entries[i] for i in indices]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_entries": len(self._entries),
                "unique_users": len(self._user_index),
                "unique_tasks": len(self._task_index),
            }
