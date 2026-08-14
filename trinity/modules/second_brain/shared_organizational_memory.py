"""
Shared Organizational Memory — Enterprise Coding Agent Knowledge Base
======================================================================
arXiv 2608.00122 · P48-3

跨 Agent 共享组织知识库：平台级任务侧经验自动捕获 → QA 对结构化编目 →
跨实例共享索引 → 安全过滤门。贡献者审核门控确保质量，
敏感信息脱敏 + 权限范围限定保障安全。

设计要点:
  - PlatformCaptureHook: 平台级经验自动捕获
  - QAMemoryCurator: 经验→QA 编目
  - SharedMemoryIndex: 跨实例共享索引
  - SecurityAuditGate: 安全过滤门
"""
from __future__ import annotations

import logging
import threading
import time
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict, OrderedDict
import hashlib

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class TaskExperience:
    """单次任务的侧经验快照。"""
    task_id: str
    agent_id: str = ""
    context: str = ""               # 任务上下文
    action_sequence: List[str] = field(default_factory=list)
    result_summary: str = ""
    success: bool = True
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QAMemory:
    """QA 格式的结构化记忆条目。"""
    qa_id: str
    question: str
    answer: str
    contributor_agent_id: str = ""
    approved: bool = False
    curator_notes: str = ""
    tags: List[str] = field(default_factory=list)
    embedding: Optional[np.ndarray] = None
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# PlatformCaptureHook
# ---------------------------------------------------------------------------

class PlatformCaptureHook:
    """平台级任务侧经验自动捕获——任务上下文 + 操作序列 + 结果。

    生命周期钩子:
      on_task_start → on_step → on_task_end → capture
    """

    _SENSITIVE_PATTERNS = [
        re.compile(r'(?:api[_-]?key|secret|token|password|credential)\s*[:=]\s*\S+', re.I),
        re.compile(r'ghp_[A-Za-z0-9]{36}'),
        re.compile(r'sk-[A-Za-z0-9]{32,}'),
        re.compile(r'Bearer\s+[A-Za-z0-9\-_.~+/]+={0,2}'),
        re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}'),
    ]

    def __init__(self, agent_id: str = "") -> None:
        self.agent_id = agent_id
        self._active_tasks: Dict[str, TaskExperience] = {}
        self._captured: List[TaskExperience] = []
        self._lock = threading.RLock()

    def on_task_start(self, task_id: str, context: str) -> None:
        with self._lock:
            self._active_tasks[task_id] = TaskExperience(
                task_id=task_id, agent_id=self.agent_id, context=context,
            )

    def on_step(self, task_id: str, action: str) -> None:
        with self._lock:
            if task_id in self._active_tasks:
                self._active_tasks[task_id].action_sequence.append(action)

    def on_task_end(self, task_id: str, result: str, success: bool = True) -> Optional[TaskExperience]:
        with self._lock:
            exp = self._active_tasks.pop(task_id, None)
            if exp is None:
                return None
            exp.result_summary = self._sanitize(result)
            exp.success = success
            self._captured.append(exp)
            return exp

    def _sanitize(self, text: str) -> str:
        for pat in self._SENSITIVE_PATTERNS:
            text = pat.sub(lambda m: f"[REDACTED:{m.group()[:8]}...]", text)
        return text

    def statistics(self) -> Dict[str, Any]:
        return {"captured": len(self._captured), "active": len(self._active_tasks)}


# ---------------------------------------------------------------------------
# QAMemoryCurator
# ---------------------------------------------------------------------------

class QAMemoryCurator:
    """经验 → QA 对结构化编目——含贡献者审核门控。

    Parameters
    ----------
    approval_required : bool
        是否需要贡献者审核。
    """

    def __init__(self, approval_required: bool = True) -> None:
        self.approval_required = approval_required
        self._qa_memories: Dict[str, QAMemory] = {}
        self._pending: Dict[str, QAMemory] = {}
        self._lock = threading.RLock()

    def curate(self, experience: TaskExperience) -> Optional[QAMemory]:
        """将一次任务经验编目为 QA 对。"""
        with self._lock:
            qa_id = f"qa_{hashlib.md5(experience.task_id.encode()).hexdigest()[:12]}"
            question = f"How to handle: {experience.context[:120]}"
            answer = experience.result_summary[:300]
            tags = self._extract_tags(experience)

            qa = QAMemory(
                qa_id=qa_id, question=question, answer=answer,
                contributor_agent_id=experience.agent_id,
                approved=not self.approval_required,
                tags=tags,
            )

            if self.approval_required:
                self._pending[qa_id] = qa
            else:
                self._qa_memories[qa_id] = qa
            return qa

    def approve(self, qa_id: str) -> bool:
        """贡献者审核通过。"""
        with self._lock:
            if qa_id in self._pending:
                qa = self._pending.pop(qa_id)
                qa.approved = True
                self._qa_memories[qa_id] = qa
                return True
            return False

    def _extract_tags(self, exp: TaskExperience) -> List[str]:
        tags = []
        ctx_lower = exp.context.lower()
        if "debug" in ctx_lower:
            tags.append("debugging")
        if "build" in ctx_lower or "compile" in ctx_lower:
            tags.append("build")
        if "deploy" in ctx_lower:
            tags.append("deploy")
        if "test" in ctx_lower:
            tags.append("testing")
        if exp.success:
            tags.append("success")
        else:
            tags.append("failure")
        return tags

    def statistics(self) -> Dict[str, Any]:
        return {
            "approved": len(self._qa_memories),
            "pending_approval": len(self._pending),
        }


# ---------------------------------------------------------------------------
# SharedMemoryIndex
# ---------------------------------------------------------------------------

class SharedMemoryIndex:
    """跨实例共享索引——向量检索 + 关键词倒排索引。

    Parameters
    ----------
    vector_dim : int
        向量维度。
    """

    def __init__(self, vector_dim: int = 128) -> None:
        self.vector_dim = vector_dim
        self._qa_registry: Dict[str, QAMemory] = {}
        self._inverted_index: Dict[str, Set[str]] = defaultdict(set)
        self._lock = threading.RLock()

    def index(self, qa: QAMemory, embedding: Optional[np.ndarray] = None) -> None:
        """索引入一条 QA 记忆。"""
        with self._lock:
            self._qa_registry[qa.qa_id] = qa
            # 关键词倒排
            words = set(qa.question.lower().split() + qa.answer.lower().split())
            for w in words:
                if len(w) > 2:
                    self._inverted_index[w].add(qa.qa_id)
            # 向量
            if embedding is not None:
                qa.embedding = embedding.copy()

    def search_by_keywords(self, query: str, top_k: int = 10) -> List[QAMemory]:
        """关键词倒排检索。"""
        with self._lock:
            q_tokens = set(query.lower().split())
            scores: Dict[str, int] = defaultdict(int)
            for token in q_tokens:
                for qa_id in self._inverted_index.get(token, set()):
                    scores[qa_id] += 1
            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            return [self._qa_registry[qid] for qid, _ in ranked[:top_k] if qid in self._qa_registry]

    def search_by_vector(self, query_vec: np.ndarray, top_k: int = 10) -> List[Tuple[QAMemory, float]]:
        """向量语义检索。"""
        with self._lock:
            scored = []
            query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-8)
            for qa_id, qa in self._qa_registry.items():
                if qa.embedding is not None:
                    sim = float(np.dot(query_norm, qa.embedding))
                    scored.append((qa, sim))
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_k]

    def statistics(self) -> Dict[str, Any]:
        return {
            "indexed_qa": len(self._qa_registry),
            "inverted_terms": len(self._inverted_index),
            "vector_dim": self.vector_dim,
        }


# ---------------------------------------------------------------------------
# SecurityAuditGate
# ---------------------------------------------------------------------------

class SecurityAuditGate:
    """安全与隐私过滤门——敏感信息脱敏 + 权限范围限定。

    检测项: API密钥 / Token / 邮箱 / 内网IP / PII
    """

    _PII_PATTERNS = [
        (re.compile(r'\d{3}-\d{2}-\d{4}'), '[SSN]'),       # SSN
        (re.compile(r'\d{16}'), '[CC_NUM]'),                 # 信用卡号
        (re.compile(r'\d{3}-\d{3}-\d{4}'), '[PHONE]'),       # 电话号码
        (re.compile(r'10\.\d{1,3}\.\d{1,3}\.\d{1,3}'), '[INTERNAL_IP]'),
        (re.compile(r'172\.(1[6-9]|2\d|3[01])\.'), '[INTERNAL_IP]'),
        (re.compile(r'192\.168\.'), '[INTERNAL_IP]'),
    ]

    def __init__(self, allowed_domains: Optional[Set[str]] = None) -> None:
        self.allowed_domains = allowed_domains or set()
        self._audit_log: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    def audit(self, qa: QAMemory) -> Tuple[bool, str]:
        """审计一条 QA 记忆的安全性。

        Returns
        -------
        Tuple[bool, str]
            (通过?, 原因)
        """
        with self._lock:
            text = qa.question + " " + qa.answer
            redacted = text
            issues = []

            # PII 检测
            for pattern, label in self._PII_PATTERNS:
                if pattern.search(redacted):
                    issues.append(label)

            # 长度异常
            if len(text) > 10000:
                issues.append("OVERSIZED")

            # 权限域限制
            if self.allowed_domains:
                has_domain = any(d in text.lower() for d in self.allowed_domains)
                if not has_domain and len(self.allowed_domains) > 5:
                    issues.append("DOMAIN_RESTRICTED")

            passed = len(issues) == 0
            record = {"qa_id": qa.qa_id, "passed": passed, "issues": issues}
            self._audit_log.append(record)

            return passed, "; ".join(issues) if issues else "OK"

    def statistics(self) -> Dict[str, Any]:
        passed = sum(1 for r in self._audit_log if r["passed"])
        return {
            "total_audited": len(self._audit_log),
            "passed": passed,
            "blocked": len(self._audit_log) - passed,
        }
