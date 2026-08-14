"""
ProjectMemEventSourced — PROJECTMEM Event-Sourced Memory & Judgment Layer
==========================================================================
arXiv 2606.12329 · P44-3

实现事件溯源记忆与判断层: 本地优先, 按文件/类/函数维度记录失败修复事件。
确定性门控在 commit 边界拦截重复失败。预动作钩子在编辑前拦截。
可选语义检索补充, 零读时模型成本。

设计要点:
  - EventLog: 事件溯源事件存储
  - DeterministicGate: commit 边界确定性门控
  - PreActionHook: 编辑前拦截
  - FailureRepairEvent: 失败修复事件
  - SemanticRetrievalBridge: 可选语义检索桥
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque
import hashlib
import json

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EventSourceType(Enum):
    """事件源类型。"""
    FILE = auto()
    CLASS = auto()
    FUNCTION = auto()
    PROJECT = auto()


class FileEventScope(Enum):
    """文件事件范围。"""
    SINGLE_FILE = auto()
    DIRECTORY = auto()
    PROJECT_WIDE = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class FailureRepairEvent:
    """失败修复事件——记录失败的修复尝试与成功方案。"""
    event_id: str
    source_type: EventSourceType
    scope: FileEventScope
    file_path: str = ""
    class_name: str = ""
    function_name: str = ""
    failure_signature: str = ""  # 失败特征哈希
    error_message: str = ""
    fix_description: str = ""
    successful_fix: bool = False
    fix_commit_hash: str = ""
    tags: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class CommitBoundary:
    """Commit 边界——事件分组边界。"""
    boundary_id: str
    events: List[str] = field(default_factory=list)  # event_ids
    commit_message: str = ""
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# FailureSigner
# ---------------------------------------------------------------------------

class FailureSigner:
    """失败签名生成器——确定性计算失败特征。"""

    @staticmethod
    def sign(error_message: str, file_path: str, class_name: str = "", function_name: str = "") -> str:
        """生成确定性失败签名。"""
        content = f"{error_message}|{file_path}|{class_name}|{function_name}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    @staticmethod
    def sign_hunk(hunk_content: str) -> str:
        """对代码块生成签名——用于预动作钩子。"""
        # 归一化: 去除空白差异
        normalized = " ".join(hunk_content.split())
        return hashlib.sha256(normalized.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# EventLog
# ---------------------------------------------------------------------------

class EventLog:
    """事件溯源事件存储——时间线不可变追加日志。"""

    def __init__(self) -> None:
        self._events: List[FailureRepairEvent] = []
        self._by_file: Dict[str, List[int]] = defaultdict(list)
        self._by_signature: Dict[str, List[int]] = defaultdict(list)
        self._lock = threading.RLock()

    def append(self, event: FailureRepairEvent) -> int:
        """追加事件 (不可变追加)。"""
        with self._lock:
            idx = len(self._events)
            self._events.append(event)
            self._by_file[event.file_path].append(idx)
            self._by_signature[event.failure_signature].append(idx)
            logger.debug("EventLog: appended event %s (sig=%s)", event.event_id, event.failure_signature[:8])
            return idx

    def get_by_signature(self, signature: str) -> List[FailureRepairEvent]:
        """按失败签名检索历史。"""
        indices = self._by_signature.get(signature, [])
        return [self._events[i] for i in indices]

    def get_by_file(self, file_path: str) -> List[FailureRepairEvent]:
        """按文件检索历史。"""
        indices = self._by_file.get(file_path, [])
        return [self._events[i] for i in indices]

    def get_recent(self, n: int = 20) -> List[FailureRepairEvent]:
        return self._events[-n:]

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_events": len(self._events),
            "unique_signatures": len(self._by_signature),
            "tracked_files": len(self._by_file),
        }


# ---------------------------------------------------------------------------
# DeterministicGate
# ---------------------------------------------------------------------------

class DeterministicGate:
    """确定性门控——在 commit 边界拦截重复失败。

    核心策略: 签名(signature)是否命中历史失败 → 阻断 commit。
    """

    def __init__(self) -> None:
        self._blocked_signatures: Set[str] = set()
        self._lock = threading.RLock()

    def check(self, event_log: EventLog, file_path: str, error_message: str,
              class_name: str = "", function_name: str = "") -> Dict[str, Any]:
        """检查是否应阻断。"""
        with self._lock:
            signature = FailureSigner.sign(error_message, file_path, class_name, function_name)

            # 查历史
            past_events = event_log.get_by_signature(signature)
            past_failures = [e for e in past_events if not e.successful_fix]

            if past_failures:
                self._blocked_signatures.add(signature)
                return {
                    "blocked": True,
                    "signature": signature,
                    "reason": f"Matched {len(past_failures)} previous failure(s)",
                    "past_events": [
                        {"event_id": e.event_id, "error": e.error_message[:80],
                         "timestamp": e.timestamp}
                        for e in past_failures[-3:]
                    ],
                }

            # 检查是否已有成功修复
            past_successes = [e for e in past_events if e.successful_fix]
            if past_successes:
                return {
                    "blocked": False,
                    "signature": signature,
                    "warning": f"Previous fix succeeded: {past_successes[0].fix_description[:100]}",
                    "successful_fix": past_successes[0].fix_description,
                }

            return {"blocked": False, "signature": signature, "is_novel": True}

    def statistics(self) -> Dict[str, Any]:
        return {"blocked_signatures": len(self._blocked_signatures)}


# ---------------------------------------------------------------------------
# PreActionHook
# ---------------------------------------------------------------------------

class PreActionHook:
    """预动作钩子——在编辑前拦截。"""

    def __init__(self) -> None:
        self._hook_registry: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def register(self, hook_name: str, hook_fn: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        """注册钩子。"""
        with self._lock:
            self._hook_registry[hook_name] = hook_fn

    def execute(self, hook_name: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """执行钩子。"""
        hook_fn = self._hook_registry.get(hook_name)
        if not hook_fn:
            return {"executed": False, "reason": f"Hook '{hook_name}' not registered"}

        result = hook_fn(context)
        return {"executed": True, "hook_name": hook_name, "result": result}

    def pre_edit_check(
        self, event_log: EventLog, file_path: str, hunk_content: str, class_name: str = "", function_name: str = ""
    ) -> Dict[str, Any]:
        """编辑前检查——hunk签名匹配历史失败。"""
        hunk_sig = FailureSigner.sign_hunk(hunk_content)

        # 查找与当前文件相关的历史失败
        file_events = event_log.get_by_file(file_path)
        matched = [
            e for e in file_events
            if e.failure_signature[:8] in hunk_sig  # 前缀近似匹配
        ]

        if matched:
            return {
                "warn": True,
                "message": f"{len(matched)} similar past failure(s) in {file_path}",
                "suggestions": [e.fix_description for e in matched if e.successful_fix][:3],
            }

        return {"warn": False}

    def statistics(self) -> Dict[str, Any]:
        return {"registered_hooks": len(self._hook_registry)}


# ---------------------------------------------------------------------------
# SemanticRetrievalBridge
# ---------------------------------------------------------------------------

class SemanticRetrievalBridge:
    """可选语义检索桥——与确定性门控互补。

    Parameters
    ----------
    enabled : bool
        是否启用语义检索。
    """

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self._lock = threading.RLock()

    def search(self, query: str, event_log: EventLog, top_k: int = 5) -> List[FailureRepairEvent]:
        """语义检索——如果不启用则返回空。"""
        if not self.enabled:
            return []

        # 基于关键词的简单检索 (零模型成本)
        query_keywords = set(query.lower().split())
        scored: List[Tuple[FailureRepairEvent, int]] = []

        for event in event_log.get_recent(500):
            text = f"{event.error_message} {event.fix_description}".lower()
            score = sum(1 for kw in query_keywords if kw in text)
            if score > 0:
                scored.append((event, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [e for e, _ in scored[:top_k]]

    def statistics(self) -> Dict[str, Any]:
        return {"enabled": self.enabled}


# ---------------------------------------------------------------------------
# JudgmentLayer
# ---------------------------------------------------------------------------

class JudgmentLayer:
    """判断层——整合门控+钩子, 统一判断接口。"""

    def __init__(self, gate: DeterministicGate, hook: PreActionHook) -> None:
        self.gate = gate
        self.hook = hook

    def judge_commit(
        self, event_log: EventLog, file_path: str, error_message: str,
        class_name: str = "", function_name: str = ""
    ) -> Dict[str, Any]:
        """Commit 边界判断。"""
        return self.gate.check(event_log, file_path, error_message, class_name, function_name)

    def judge_pre_edit(
        self, event_log: EventLog, file_path: str, hunk_content: str,
        class_name: str = "", function_name: str = ""
    ) -> Dict[str, Any]:
        """编辑前判断。"""
        return self.hook.pre_edit_check(event_log, file_path, hunk_content, class_name, function_name)

    def statistics(self) -> Dict[str, Any]:
        return {
            "gate": self.gate.statistics(),
            "hook": self.hook.statistics(),
        }


# ---------------------------------------------------------------------------
# ProjectMemEventSourced
# ---------------------------------------------------------------------------

class ProjectMemEventSourced:
    """PROJECTMEM 事件溯源记忆与判断层。

    本地优先, 按文件/类/函数维度记录失败修复事件。
    确定性门控在 commit 边界拦截重复失败, 预动作钩子在编辑前拦截。

    Parameters
    ----------
    enable_semantic_retrieval : bool
        是否启用可选语义检索。
    """

    def __init__(self, enable_semantic_retrieval: bool = False) -> None:
        self.event_log = EventLog()
        self.deterministic_gate = DeterministicGate()
        self.pre_action_hook = PreActionHook()
        self.judgment_layer = JudgmentLayer(self.deterministic_gate, self.pre_action_hook)
        self.semantic_retrieval_bridge = SemanticRetrievalBridge(
            enabled=enable_semantic_retrieval,
        )
        self._commit_boundaries: List[CommitBoundary] = []
        self._lock = threading.RLock()
        self._event_count: int = 0

        logger.info(
            "ProjectMemEventSourced initialized [semantic=%s]",
            enable_semantic_retrieval,
        )

    def record_failure(
        self,
        file_path: str,
        error_message: str,
        fix_description: str = "",
        class_name: str = "",
        function_name: str = "",
        source_type: EventSourceType = EventSourceType.FILE,
    ) -> FailureRepairEvent:
        """记录失败事件。"""
        with self._lock:
            self._event_count += 1
            signature = FailureSigner.sign(error_message, file_path, class_name, function_name)

            event = FailureRepairEvent(
                event_id=f"evt_{self._event_count}_{int(time.time()*1e6)}",
                source_type=source_type,
                scope=FileEventScope.SINGLE_FILE,
                file_path=file_path,
                class_name=class_name,
                function_name=function_name,
                failure_signature=signature,
                error_message=error_message,
                fix_description=fix_description,
                successful_fix=bool(fix_description),
                tags=[source_type.name.lower(), "failure"],
            )

            self.event_log.append(event)
            return event

    def record_success(self, event: FailureRepairEvent, fix_commit_hash: str = "") -> FailureRepairEvent:
        """标记失败事件为已修复。"""
        event.successful_fix = True
        event.fix_commit_hash = fix_commit_hash
        event.timestamp = time.time()
        return event

    def commit_check(
        self, file_path: str, error_message: str, class_name: str = "", function_name: str = ""
    ) -> Dict[str, Any]:
        """Commit 边界检查——门控判断。"""
        return self.judgment_layer.judge_commit(
            self.event_log, file_path, error_message, class_name, function_name,
        )

    def pre_edit_check(self, file_path: str, hunk_content: str, class_name: str = "", function_name: str = "") -> Dict[str, Any]:
        """编辑前检查——预动作钩子。"""
        return self.judgment_layer.judge_pre_edit(
            self.event_log, file_path, hunk_content, class_name, function_name,
        )

    def search_relevant(self, query: str) -> List[Dict[str, Any]]:
        """语义检索——可选补充。"""
        events = self.semantic_retrieval_bridge.search(query, self.event_log)
        return [
            {"event_id": e.event_id, "file": e.file_path, "error": e.error_message[:80],
             "fix": e.fix_description[:100]}
            for e in events
        ]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "events": self.event_log.statistics(),
                "judgment": self.judgment_layer.statistics(),
                "commits_tracked": len(self._commit_boundaries),
            }
