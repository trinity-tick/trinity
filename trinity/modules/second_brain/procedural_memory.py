"""
CB60: ProceduralMemory — 程序性记忆
====================================

对标 LangMem procedural memory。Agent 根据用户反馈和历史执行结果，
自主更新自身 system prompt / 行为规则 / 工具使用策略。
记忆内容为可执行规则对 (condition → action)，带版本追踪。

设计要点：
  - 可执行规则对：condition → action，Agent 在运行时评估条件并执行动作
  - 自主更新：基于用户反馈（显式纠正/隐式偏好）和任务结果自动调整规则
  - 版本追踪：每条规则有独立版本号，支持回滚到历史版本
  - 规则优先级：High > Medium > Low，同优先级按创建时间排序
  - 与 memory_version_control 集成：规则变更记录 COW 快照

Reference:
  - LangMem: LangChain procedural memory — agent self-updates behavior
  - LangMem LoCoMo: 58.1%, LongMemEval temporal: 23.4%
  - LangMem 核心：Agent 根据反馈更新 system prompt / 行为规则 / 工具策略
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 安全内置函数白名单（禁止 __import__ / eval / exec 等危险操作）
_SAFE_BUILTINS = {
    "__builtins__": {
        "True": True, "False": False, "None": None,
        "len": len, "str": str, "int": int, "float": float, "bool": bool,
        "list": list, "dict": dict, "tuple": tuple, "set": set,
        "min": min, "max": max, "sum": sum, "abs": abs,
        "any": any, "all": all, "sorted": sorted, "reversed": reversed,
        "enumerate": enumerate, "zip": zip, "range": range,
        "isinstance": isinstance, "type": type,
        "round": round, "pow": pow,
    },
}


# ============================================================================
# Enums
# ============================================================================

class RulePriority(Enum):
    """规则优先级。"""
    HIGH = "high"       # 安全/合规类规则，不可覆写
    MEDIUM = "medium"   # 标准行为规则
    LOW = "low"         # 偏好/便利类规则


class RuleTrigger(Enum):
    """规则触发来源。"""
    EXPLICIT_FEEDBACK = auto()   # 用户显式纠正
    IMPLICIT_SIGNAL = auto()     # 隐式偏好信号（重复否定/跳过）
    TASK_FAILURE = auto()        # 任务执行失败后自动生成
    TASK_SUCCESS = auto()        # 成功模式固化
    USER_PREFERENCE = auto()     # 用户偏好声明
    SYSTEM_INJECTED = auto()     # 系统注入的默认规则


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class RuleCondition:
    """规则的触发条件。

    condition 为可执行 Python 表达式字符串，运行时 eval 求值，
    可使用变量：query, context, history, tools, session。
    """
    condition: str              # 可执行条件表达式，如 "len(context) < 100"
    description: str = ""       # 人类可读的条件描述
    required_context_keys: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class RuleAction:
    """规则触发后的执行动作。

    action 为动作类型；payload 为动作参数。
    动作类型：prepend_prompt（在 System Prompt 前添加）、
    modify_behavior（修改行为指令）、prefer_tool（优先使用某工具）、
    avoid_tool（避免使用某工具）。
    """
    action: str                 # prepend_prompt / modify_behavior / prefer_tool / avoid_tool
    payload: str                # 动作参数（文本指令或工具名）
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class ProceduralRule:
    """可执行规则对 (condition → action)。"""
    rule_id: str
    condition: RuleCondition
    action: RuleAction
    priority: RulePriority = RulePriority.MEDIUM
    trigger: RuleTrigger = RuleTrigger.EXPLICIT_FEEDBACK
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    hit_count: int = 0          # 命中次数
    success_count: int = 0      # 执行后任务成功次数
    enabled: bool = True
    source_session: str = ""    # 产生该规则的会话 ID
    tags: List[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.hit_count == 0:
            return 0.0
        return self.success_count / self.hit_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "condition": self.condition.to_dict(),
            "action": self.action.to_dict(),
            "priority": self.priority.value,
            "trigger": self.trigger.name,
            "version": self.version,
            "hit_count": self.hit_count,
            "success_rate": round(self.success_rate, 3),
            "enabled": self.enabled,
        }


# ============================================================================
# Main Class
# ============================================================================

class ProceduralMemory:
    """程序性记忆管理器。

    存储和管理 Agent 的可执行规则对，支持自主更新和版本追踪。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._rules: Dict[str, ProceduralRule] = {}
        self._rule_versions: Dict[str, List[ProceduralRule]] = {}  # base_id → [v1, v2, ...]
        self._rule_count: int = 0
        self._feedback_count: int = 0
        self._created_at: float = time.time()

    @staticmethod
    def _make_rule_id(condition: RuleCondition, action: RuleAction) -> str:
        raw = f"{condition.condition}::{action.action}::{action.payload}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    # -- CRUD --

    def add_rule(self, condition: RuleCondition, action: RuleAction,
                 priority: RulePriority = RulePriority.MEDIUM,
                 trigger: RuleTrigger = RuleTrigger.EXPLICIT_FEEDBACK,
                 source_session: str = "",
                 tags: Optional[List[str]] = None) -> ProceduralRule:
        """添加新规则或更新已有规则（版本递增）。"""
        with self._lock:
            base_id = self._make_rule_id(condition, action)
            now = time.time()

            if base_id in self._rule_versions:
                # 已有规则：版本递增
                prev = self._rule_versions[base_id][-1]
                new_version = prev.version + 1
            else:
                new_version = 1

            rule_id = f"{base_id}:v{new_version}"
            rule = ProceduralRule(
                rule_id=rule_id,
                condition=condition,
                action=action,
                priority=priority,
                trigger=trigger,
                version=new_version,
                created_at=now,
                updated_at=now,
                source_session=source_session,
                tags=tags or [],
            )
            self._rules[rule_id] = rule
            self._rule_versions.setdefault(base_id, []).append(rule)
            self._rule_count += 1
            return rule

    def get_applicable_rules(self, context: Dict[str, Any]) -> List[ProceduralRule]:
        """获取当前上下文下适用的规则，按优先级排序。"""
        with self._lock:
            rules = []
            for rule in self._rules.values():
                if not rule.enabled:
                    continue
                try:
                    if eval(rule.condition.condition, _SAFE_BUILTINS, context):
                        rules.append(rule)
                except Exception:
                    logger.debug("ProceduralMemory: eval failed for %s", rule.rule_id)
            rules.sort(key=lambda r: (0 if r.priority == RulePriority.HIGH else
                                      1 if r.priority == RulePriority.MEDIUM else 2,
                                      -r.updated_at))
            return rules

    def record_feedback(self, rule_id: str, success: bool):
        """记录规则执行反馈。"""
        with self._lock:
            self._feedback_count += 1
            rule = self._rules.get(rule_id)
            if rule:
                rule.hit_count += 1
                if success:
                    rule.success_count += 1
                    rule.updated_at = time.time()
                    logger.debug(
                        "ProceduralMemory: rule %s success rate → %.3f",
                        rule_id, rule.success_rate,
                    )

    def rollback_rule(self, base_id: str, target_version: int) -> Optional[ProceduralRule]:
        """回滚规则到指定版本。"""
        with self._lock:
            versions = self._rule_versions.get(base_id, [])
            for v in versions:
                if v.version == target_version:
                    for r in versions:
                        r.enabled = False
                    v.enabled = True
                    return v
            return None

    def get_rules_by_trigger(self, trigger: RuleTrigger) -> List[ProceduralRule]:
        with self._lock:
            return [r for r in self._rules.values() if r.trigger == trigger]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self._rules)
            enabled = sum(1 for r in self._rules.values() if r.enabled)
            by_priority = {}
            for p in RulePriority:
                by_priority[p.value] = sum(
                    1 for r in self._rules.values() if r.priority == p
                )
            return {
                "class": "ProceduralMemory (CB60)",
                "total_rules": total,
                "enabled_rules": enabled,
                "rule_groups": len(self._rule_versions),
                "total_versions": sum(len(v) for v in self._rule_versions.values()),
                "by_priority": by_priority,
                "total_feedback": self._feedback_count,
                "uptime_seconds": time.time() - self._created_at,
            }
