"""
P14-7: Code Agent Memory (对标 MemCoder · SWE-bench +9.4%)
==============================================================

核心设计（MemCoder: Intent-to-Code Mapping + Verification-Driven Self-Correction）：
  - IntentToCodeMapping：从 git commit 历史提取 intent → code 映射
  - SelfCorrectionEngine：验证反馈驱动的自修正机制（verify → feedback → amend）
  - ExperienceInternalizer：人工验证方案 → 长期知识固化
  - RepositoryMemory：仓库级代码记忆检索（跨文件/跨模块语义搜索）

兼容性：
  - 与 meta_learning_codegen.py（MetaAgent）代码变异/交叉接口兼容
  - 与 codebase_graph_memory.py（CodebaseGraphStore）仓库索引兼容
  - 与 aml_protocol_adapter.py（P14-1）评测接口兼容

Reference:
  - MemCoder: Memory-Driven Code Agent (SWE-bench +9.4%)
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ────────────────────────────────────────────────────

class ChangeType(Enum):
    """代码变更类型。"""
    ADD = "add"               # 新增文件/函数
    MODIFY = "modify"         # 修改已有代码
    DELETE = "delete"         # 删除代码
    REFACTOR = "refactor"     # 重构（功能不变）
    FIX = "fix"               # 修复 bug
    OPTIMIZE = "optimize"     # 性能优化


class VerificationStatus(Enum):
    """验证状态。"""
    PENDING = "pending"           # 待验证
    VERIFIED = "verified"         # 已验证通过
    REJECTED = "rejected"         # 验证不通过
    AMENDED = "amended"           # 已修正
    INTERNALIZED = "internalized" # 已固化为长期知识


class KnowledgeTier(Enum):
    """知识层级。"""
    TRANSIENT = "transient"        # 临时——仅当前会话有效
    SESSION = "session"            # 会话级——跨任务有效
    REPOSITORY = "repository"      # 仓库级——全局有效
    VERIFIED = "verified"          # 人工验证——不可自动删除


class CorrectionType(Enum):
    """修正类型。"""
    SYNTAX = "syntax"             # 语法错误修正
    LOGIC = "logic"               # 逻辑错误修正
    STYLE = "style"               # 风格/规范修正
    PERFORMANCE = "performance"   # 性能优化
    SECURITY = "security"         # 安全漏洞修正


# ── 数据结构 ──────────────────────────────────────────────────────

@dataclass
class CommitChange:
    """单个 commit 变更记录。"""
    commit_id: str
    commit_hash: str
    message: str
    author: str = "unknown"
    files_changed: List[str] = field(default_factory=list)
    change_type: ChangeType = ChangeType.MODIFY
    additions: int = 0
    deletions: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tags: List[str] = field(default_factory=list)


@dataclass
class IntentToCodeMapping:
    """Intent → Code 映射记录。"""
    mapping_id: str
    intent: str                              # 自然语言意图描述
    code_snippet: str                        # 对应代码片段
    language: str = "python"
    file_path: Optional[str] = None
    function_name: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)  # 上下文信息
    verification: VerificationStatus = VerificationStatus.PENDING
    confidence: float = 0.5
    usage_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_used: Optional[datetime] = None


@dataclass
class VerificationFeedback:
    """验证反馈记录。"""
    feedback_id: str
    mapping_id: str
    verifier: str                    # 验证者（human / linter / test-suite）
    passed: bool
    errors: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    corrected_snippet: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SelfCorrectionLog:
    """自修正日志。"""
    log_id: str
    mapping_id: str
    original_snippet: str
    feedback: VerificationFeedback
    correction_type: CorrectionType
    corrected_snippet: Optional[str] = None
    status: VerificationStatus = VerificationStatus.AMENDED
    corrected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CodeSearchResult:
    """代码搜索命中结果。"""
    mapping: IntentToCodeMapping
    relevance_score: float
    match_type: str = "semantic"     # semantic / keyword / structural
    snippet_context: str = ""


@dataclass
class RepositoryStats:
    """仓库级统计。"""
    total_mappings: int = 0
    verified_count: int = 0
    internalized_count: int = 0
    languages: Dict[str, int] = field(default_factory=dict)
    change_types: Dict[str, int] = field(default_factory=dict)
    avg_confidence: float = 0.0


# ── 仓库记忆 ──────────────────────────────────────────────────────

class RepositoryMemory:
    """仓库级代码记忆索引与检索。"""

    def __init__(self, repo_name: str = "default"):
        self._repo_name = repo_name
        self._mappings: Dict[str, IntentToCodeMapping] = {}
        self._commits: List[CommitChange] = []
        self._file_index: Dict[str, List[str]] = defaultdict(list)  # file_path → mapping_ids
        self._function_index: Dict[str, List[str]] = defaultdict(list)
        self._intent_index: Dict[str, str] = {}                     # intent_hash → mapping_id
        self._lock = threading.RLock()
        logger.info("RepositoryMemory initialized (repo=%s)", repo_name)

    def index_mapping(self, mapping: IntentToCodeMapping) -> str:
        with self._lock:
            self._mappings[mapping.mapping_id] = mapping
            if mapping.file_path:
                self._file_index[mapping.file_path].append(mapping.mapping_id)
            if mapping.function_name:
                self._function_index[mapping.function_name].append(mapping.mapping_id)
            intent_hash = hashlib.md5(mapping.intent.encode()).hexdigest()[:16]
            self._intent_index[intent_hash] = mapping.mapping_id
            return mapping.mapping_id

    def index_commit(self, commit: CommitChange) -> str:
        with self._lock:
            self._commits.append(commit)
            return commit.commit_id

    def search_by_intent(
        self,
        query: str,
        top_k: int = 10,
    ) -> List[CodeSearchResult]:
        results: List[CodeSearchResult] = []
        with self._lock:
            for mapping in self._mappings.values():
                # Simple keyword overlap as relevance
                query_words = set(query.lower().split())
                intent_words = set(mapping.intent.lower().split())
                overlap = len(query_words & intent_words)
                if overlap > 0:
                    relevance = overlap / max(len(query_words), len(intent_words))
                    results.append(CodeSearchResult(
                        mapping=mapping,
                        relevance_score=relevance,
                        match_type="keyword",
                    ))
        results.sort(key=lambda r: r.relevance_score, reverse=True)
        return results[:top_k]

    def search_by_file(self, file_path: str) -> List[IntentToCodeMapping]:
        with self._lock:
            mapping_ids = self._file_index.get(file_path, [])
            return [self._mappings[mid] for mid in mapping_ids if mid in self._mappings]

    def search_by_function(self, function_name: str) -> List[IntentToCodeMapping]:
        with self._lock:
            mapping_ids = self._function_index.get(function_name, [])
            return [self._mappings[mid] for mid in mapping_ids if mid in self._mappings]

    def get_mapping(self, mapping_id: str) -> Optional[IntentToCodeMapping]:
        with self._lock:
            return self._mappings.get(mapping_id)

    def update_verification(self, mapping_id: str, status: VerificationStatus):
        with self._lock:
            mapping = self._mappings.get(mapping_id)
            if mapping:
                mapping.verification = status

    def statistics(self) -> RepositoryStats:
        with self._lock:
            lang_dist: Dict[str, int] = defaultdict(int)
            type_dist: Dict[str, int] = defaultdict(int)
            confidences: List[float] = []
            for m in self._mappings.values():
                lang_dist[m.language] = lang_dist.get(m.language, 0) + 1
                confidences.append(m.confidence)
            for c in self._commits:
                type_dist[c.change_type.value] = type_dist.get(c.change_type.value, 0) + 1
            return RepositoryStats(
                total_mappings=len(self._mappings),
                verified_count=sum(1 for m in self._mappings.values() if m.verification == VerificationStatus.VERIFIED),
                internalized_count=sum(1 for m in self._mappings.values() if m.verification == VerificationStatus.INTERNALIZED),
                languages=dict(lang_dist),
                change_types=dict(type_dist),
                avg_confidence=float(np.mean(confidences)) if confidences else 0.0,
            )


# ── 自修正引擎 ───────────────────────────────────────────────────

class SelfCorrectionEngine:
    """验证反馈驱动的自修正机制。"""

    _MAX_CORRECTION_ATTEMPTS = 3

    def __init__(self):
        self._correction_logs: List[SelfCorrectionLog] = []
        self._feedbacks: List[VerificationFeedback] = []
        self._lock = threading.RLock()
        logger.info("SelfCorrectionEngine initialized")

    def submit_feedback(self, feedback: VerificationFeedback) -> str:
        with self._lock:
            self._feedbacks.append(feedback)
            return feedback.feedback_id

    def verify_and_correct(
        self,
        mapping: IntentToCodeMapping,
        verifier: str = "linter",
    ) -> SelfCorrectionLog:
        """验证一个 mapping 并在失败时尝试自修正。"""
        with self._lock:
            # Check previous attempts
            previous_attempts = [
                log for log in self._correction_logs
                if log.mapping_id == mapping.mapping_id
            ]
            if len(previous_attempts) >= self._MAX_CORRECTION_ATTEMPTS:
                feedback = VerificationFeedback(
                    feedback_id=f"fb_{uuid.uuid4().hex[:12]}",
                    mapping_id=mapping.mapping_id,
                    verifier=verifier,
                    passed=False,
                    errors=["Max correction attempts reached"],
                )
                self._feedbacks.append(feedback)
                return SelfCorrectionLog(
                    log_id=f"log_{uuid.uuid4().hex[:12]}",
                    mapping_id=mapping.mapping_id,
                    original_snippet=mapping.code_snippet,
                    feedback=feedback,
                    correction_type=CorrectionType.LOGIC,
                    status=VerificationStatus.REJECTED,
                )

            # Simulate verification — check for basic syntax
            errors = self._basic_check(mapping)
            passed = len(errors) == 0

            feedback = VerificationFeedback(
                feedback_id=f"fb_{uuid.uuid4().hex[:12]}",
                mapping_id=mapping.mapping_id,
                verifier=verifier,
                passed=passed,
                errors=errors,
            )
            self._feedbacks.append(feedback)

            if passed:
                mapping.verification = VerificationStatus.VERIFIED
                log = SelfCorrectionLog(
                    log_id=f"log_{uuid.uuid4().hex[:12]}",
                    mapping_id=mapping.mapping_id,
                    original_snippet=mapping.code_snippet,
                    feedback=feedback,
                    correction_type=CorrectionType.SYNTAX,
                    status=VerificationStatus.VERIFIED,
                )
            else:
                # Attempt correction
                corrected = self._attempt_correction(mapping, errors)
                mapping.verification = VerificationStatus.AMENDED
                mapping.code_snippet = corrected
                mapping.confidence = max(0.1, mapping.confidence - 0.1)
                log = SelfCorrectionLog(
                    log_id=f"log_{uuid.uuid4().hex[:12]}",
                    mapping_id=mapping.mapping_id,
                    original_snippet=mapping.code_snippet,
                    feedback=feedback,
                    correction_type=CorrectionType.SYNTAX,
                    corrected_snippet=corrected,
                    status=VerificationStatus.AMENDED,
                )

            self._correction_logs.append(log)
            return log

    def _basic_check(self, mapping: IntentToCodeMapping) -> List[str]:
        errors: List[str] = []
        code = mapping.code_snippet
        if not code.strip():
            errors.append("Empty code snippet")
        # Check bracket balance
        if code.count('(') != code.count(')'):
            errors.append("Unbalanced parentheses")
        if code.count('{') != code.count('}'):
            errors.append("Unbalanced braces")
        if code.count('[') != code.count(']'):
            errors.append("Unbalanced brackets")
        return errors

    def _attempt_correction(self, mapping: IntentToCodeMapping, errors: List[str]) -> str:
        """尝试基于错误信息修正代码。"""
        code = mapping.code_snippet
        for err in errors:
            if "parentheses" in err.lower():
                open_count = code.count('(')
                close_count = code.count(')')
                if open_count > close_count:
                    code += ')' * (open_count - close_count)
                elif close_count > open_count:
                    code = '(' * (close_count - open_count) + code
            if "braces" in err.lower():
                open_count = code.count('{')
                close_count = code.count('}')
                if open_count > close_count:
                    code += '}' * (open_count - close_count)
        return code

    def get_correction_history(
        self,
        mapping_id: str,
    ) -> List[SelfCorrectionLog]:
        with self._lock:
            return [log for log in self._correction_logs if log.mapping_id == mapping_id]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            verified = sum(1 for log in self._correction_logs if log.status == VerificationStatus.VERIFIED)
            amended = sum(1 for log in self._correction_logs if log.status == VerificationStatus.AMENDED)
            return {
                "total_corrections": len(self._correction_logs),
                "total_feedbacks": len(self._feedbacks),
                "verified": verified,
                "amended": amended,
                "success_rate": verified / max(1, len(self._correction_logs)),
            }


# ── 经验内化器 ────────────────────────────────────────────────────

class ExperienceInternalizer:
    """人工验证方案 → 长期知识固化。"""

    def __init__(self, repo_memory: RepositoryMemory):
        self._repo_memory = repo_memory
        self._internalized: Set[str] = set()
        self._lock = threading.RLock()
        logger.info("ExperienceInternalizer initialized")

    def internalize(
        self,
        mapping_id: str,
        human_verified: bool = True,
    ) -> bool:
        """将人工验证通过的 mapping 固化为长期知识。"""
        with self._lock:
            mapping = self._repo_memory.get_mapping(mapping_id)
            if not mapping:
                logger.warning("Mapping %s not found for internalization", mapping_id)
                return False
            if not human_verified:
                return False
            mapping.verification = VerificationStatus.INTERNALIZED
            mapping.confidence = 1.0  # 固化后置信度设为 1.0
            self._internalized.add(mapping_id)
            logger.info("Internalized mapping %s: %s", mapping_id, mapping.intent[:60])
            return True

    def batch_internalize(
        self,
        mapping_ids: List[str],
        require_verified: bool = True,
    ) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        for mid in mapping_ids:
            mapping = self._repo_memory.get_mapping(mid)
            if not mapping:
                results[mid] = False
                continue
            if require_verified and mapping.verification != VerificationStatus.VERIFIED:
                results[mid] = False
                continue
            results[mid] = self.internalize(mid, human_verified=True)
        return results

    def get_internalized(self) -> List[IntentToCodeMapping]:
        with self._lock:
            return [
                self._repo_memory.get_mapping(mid)
                for mid in self._internalized
                if self._repo_memory.get_mapping(mid)
            ]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "internalized_count": len(self._internalized),
                "knowledge_tier": KnowledgeTier.VERIFIED.value,
            }


# ── 代码 Agent 记忆（顶层调度器）─────────────────────────────────

class CodeAgentMemory:
    """代码 Agent 记忆——意图提取、修正、内化、检索。"""

    _VERSION = "1.0.0"

    def __init__(self, repo_name: str = "default"):
        self._repo_memory = RepositoryMemory(repo_name=repo_name)
        self._correction_engine = SelfCorrectionEngine()
        self._internalizer = ExperienceInternalizer(repo_memory=self._repo_memory)
        self._lock = threading.RLock()
        self._version = self._VERSION
        logger.info("CodeAgentMemory v%s initialized (repo=%s)", self._version, repo_name)

    # ── 提取 Intent → Code 映射 ──────────────────────────────────

    def extract_from_commit(
        self,
        commit_hash: str,
        message: str,
        files: List[str],
        intent: str,
        code_snippet: str,
        language: str = "python",
        file_path: Optional[str] = None,
        function_name: Optional[str] = None,
        change_type: ChangeType = ChangeType.MODIFY,
    ) -> Dict[str, str]:
        with self._lock:
            # Create commit record
            commit_id = f"commit_{uuid.uuid4().hex[:12]}"
            commit = CommitChange(
                commit_id=commit_id,
                commit_hash=commit_hash,
                message=message,
                files_changed=files,
                change_type=change_type,
            )
            self._repo_memory.index_commit(commit)

            # Create mapping
            mapping_id = f"map_{uuid.uuid4().hex[:12]}"
            mapping = IntentToCodeMapping(
                mapping_id=mapping_id,
                intent=intent,
                code_snippet=code_snippet,
                language=language,
                file_path=file_path or (files[0] if files else None),
                function_name=function_name,
            )
            self._repo_memory.index_mapping(mapping)

            return {"commit_id": commit_id, "mapping_id": mapping_id}

    # ── 自修正 ───────────────────────────────────────────────────

    def self_correct(self, mapping_id: str) -> Optional[SelfCorrectionLog]:
        mapping = self._repo_memory.get_mapping(mapping_id)
        if not mapping:
            return None
        return self._correction_engine.verify_and_correct(mapping)

    # ── 内化 ──────────────────────────────────────────────────────

    def internalize(self, mapping_id: str, human_verified: bool = True) -> bool:
        return self._internalizer.internalize(mapping_id, human_verified)

    # ── 检索 ─────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 10) -> List[CodeSearchResult]:
        return self._repo_memory.search_by_intent(query, top_k=top_k)

    def search_by_file(self, file_path: str) -> List[IntentToCodeMapping]:
        return self._repo_memory.search_by_file(file_path)

    def get_mapping(self, mapping_id: str) -> Optional[IntentToCodeMapping]:
        return self._repo_memory.get_mapping(mapping_id)

    # ── 属性 ───────────────────────────────────────────────────────

    @property
    def repository(self) -> RepositoryMemory:
        return self._repo_memory

    @property
    def correction_engine(self) -> SelfCorrectionEngine:
        return self._correction_engine

    @property
    def internalizer(self) -> ExperienceInternalizer:
        return self._internalizer

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            repo_stats = self._repo_memory.statistics()
            return {
                "version": self._version,
                "repo_name": self._repo_memory._repo_name,
                "repository": {
                    "total_mappings": repo_stats.total_mappings,
                    "verified": repo_stats.verified_count,
                    "internalized": repo_stats.internalized_count,
                    "languages": repo_stats.languages,
                    "avg_confidence": repo_stats.avg_confidence,
                },
                "correction_engine": self._correction_engine.statistics(),
                "internalizer": self._internalizer.statistics(),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    """返回模块级统计信息。"""
    return {
        "module": "P14-7 Code Agent Memory",
        "benchmark": "MemCoder (SWE-bench +9.4%)",
        "classes": 4,
        "enums": 4,
        "dataclasses": 6,
        "key_metric": "Intent-to-Code mapping / Self-correction / Verified knowledge internalization",
        "thread_safe": True,
    }
