"""
SkillProceduralMemory — Skill Documents as Procedural Memory
=============================================================
clawxiv 2604.00009 · P43-2

实现 Skill Documents 过程性记忆: 结构化 SKILL.md 格式存储已验证工作流,
6.4x 优于向量RAG, 零幻觉步骤, git版本控制兼容, 可组合链接。

设计要点:
  - SkillDocument: 结构化 SKILL.md 格式
  - SkillValidator: 验证步骤完整性
  - SkillChainComposer: 技能链组合
  - GitVersionedSkill: 兼容 git 版本控制
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque
import re

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SkillDocumentFormat(Enum):
    """技能文档格式。"""
    SKILL_MD = auto()
    MARKDOWN = auto()
    YAML_FRONTMATTER = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class SkillStep:
    """技能步骤——编号步骤含确切命令。"""
    step_number: int
    action: str
    command: str = ""
    expected_output: str = ""
    timeout_seconds: int = 30
    retry_on_failure: bool = True


@dataclass
class SkillTrigger:
    """触发条件——何时激活此技能。"""
    keywords: List[str] = field(default_factory=list)
    context_patterns: List[str] = field(default_factory=list)
    required_inputs: Dict[str, str] = field(default_factory=dict)


@dataclass
class SkillPitfall:
    """已知陷阱——常见失败模式与规避。"""
    description: str
    mitigation: str
    severity: str = "medium"


@dataclass
class SkillVerification:
    """验证标准——确认技能执行成功。"""
    check: str
    success_criteria: str
    failure_indicator: str = ""


@dataclass
class SkillDocument:
    """结构化 SKILL.md 技能文档。

    对标 clawxiv 2604.00009 的 skill documents 格式。
    """
    skill_id: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    trigger: SkillTrigger = field(default_factory=SkillTrigger)
    steps: List[SkillStep] = field(default_factory=list)
    pitfalls: List[SkillPitfall] = field(default_factory=list)
    verification: Optional[SkillVerification] = None
    dependencies: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def token_count(self) -> int:
        """估算令牌数 (参考: 平均800 vs 向量RAG 5100)。"""
        text = self.to_skill_md()
        return len(text.split())

    def to_skill_md(self) -> str:
        """导出为 SKILL.md 格式。"""
        lines = [
            f"# {self.name}",
            f"",
            f"**Version:** {self.version}",
            f"**ID:** {self.skill_id}",
            f"",
            f"## Description",
            f"{self.description}",
            f"",
            f"## Trigger Conditions",
        ]
        if self.trigger.keywords:
            lines.append(f"- Keywords: {', '.join(self.trigger.keywords)}")
        if self.trigger.context_patterns:
            lines.append(f"- Context: {', '.join(self.trigger.context_patterns)}")
        lines.append(f"")
        lines.append(f"## Steps")
        for step in self.steps:
            lines.append(f"{step.step_number}. **{step.action}**")
            if step.command:
                lines.append(f"   ```bash\n   {step.command}\n   ```")
            if step.expected_output:
                lines.append(f"   Expected: {step.expected_output}")
        if self.pitfalls:
            lines.append(f"")
            lines.append(f"## Known Pitfalls")
            for p in self.pitfalls:
                lines.append(f"- **{p.description}** ({p.severity}): {p.mitigation}")
        if self.verification:
            lines.append(f"")
            lines.append(f"## Verification")
            lines.append(f"- Check: {self.verification.check}")
            lines.append(f"- Success: {self.verification.success_criteria}")
        if self.dependencies:
            lines.append(f"")
            lines.append(f"## Dependencies")
            for dep in self.dependencies:
                lines.append(f"- {dep}")

        return "\n".join(lines)


@dataclass
class SkillChain:
    """技能链——多个技能的编排序列。"""
    chain_id: str
    name: str
    description: str = ""
    skill_ids: List[str] = field(default_factory=list)
    sequential: bool = True  # True=顺序, False=并行
    error_handling: str = "stop"  # stop / skip / retry
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# SkillValidator
# ---------------------------------------------------------------------------

class SkillValidator:
    """验证技能文档的完整性和可执行性。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def validate(self, skill: SkillDocument) -> Tuple[bool, List[str]]:
        """验证技能文档——返回 (是否通过, 问题列表)。"""
        issues: List[str] = []

        # 1. 必须字段
        if not skill.name:
            issues.append("Missing skill name")
        if not skill.description:
            issues.append("Missing description")
        if not skill.steps:
            issues.append("No steps defined")

        # 2. 步骤完整性
        for step in skill.steps:
            if not step.action:
                issues.append(f"Step {step.step_number}: missing action description")
            if not step.command and not step.action.lower().startswith("check"):
                issues.append(f"Step {step.step_number}: missing command for '{step.action}'")

        # 3. 验证标准
        if not skill.verification:
            issues.append("No verification criteria defined")

        # 4. 陷阱覆盖
        if not skill.pitfalls and skill.steps:
            issues.append("No pitfalls documented (recommended for production)")

        passed = len(issues) == 0
        if not passed:
            logger.warning("Skill '%s' validation: %d issues", skill.name, len(issues))

        return passed, issues

    def statistics(self) -> Dict[str, Any]:
        return {"status": "ready"}


# ---------------------------------------------------------------------------
# SkillChainComposer
# ---------------------------------------------------------------------------

class SkillChainComposer:
    """技能链组合——从原子技能组合出链式调用。

    Parameters
    ----------
    max_chain_length : int
        最大链长度。
    """

    def __init__(self, max_chain_length: int = 10) -> None:
        self.max_chain_length = max_chain_length
        self._chains: Dict[str, SkillChain] = {}
        self._lock = threading.RLock()
        self._chain_count: int = 0

    def compose(
        self, name: str, skills: List[SkillDocument], sequential: bool = True, description: str = ""
    ) -> SkillChain:
        """组合技能为链。"""
        with self._lock:
            if len(skills) > self.max_chain_length:
                skills = skills[:self.max_chain_length]
                logger.warning("Chain truncated to %d skills", self.max_chain_length)

            self._chain_count += 1
            chain = SkillChain(
                chain_id=f"chain_{self._chain_count}_{int(time.time()*1e6)}",
                name=name,
                description=description,
                skill_ids=[s.skill_id for s in skills],
                sequential=sequential,
            )
            self._chains[chain.chain_id] = chain

            # 自动建立依赖关系
            if sequential and len(skills) > 1:
                for i in range(1, len(skills)):
                    prev_id = skills[i - 1].skill_id
                    if prev_id not in skills[i].dependencies:
                        skills[i].dependencies.append(prev_id)

            return chain

    def get_chain(self, chain_id: str) -> Optional[SkillChain]:
        return self._chains.get(chain_id)

    def statistics(self) -> Dict[str, Any]:
        return {"total_chains": len(self._chains)}


# ---------------------------------------------------------------------------
# GitVersionedSkill
# ---------------------------------------------------------------------------

class GitVersionedSkill:
    """git 版本控制兼容的技能管理。

    Parameters
    ----------
    repo_path : str
        git 仓库路径。
    """

    def __init__(self, repo_path: str = ".") -> None:
        self.repo_path = repo_path
        self._version_history: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._lock = threading.RLock()

    def commit_skill(self, skill: SkillDocument, message: str = "") -> Dict[str, Any]:
        """提交技能变更 (模拟 git commit)。"""
        with self._lock:
            version_entry = {
                "version": skill.version,
                "skill_id": skill.skill_id,
                "name": skill.name,
                "steps_count": len(skill.steps),
                "message": message or f"Update {skill.name} v{skill.version}",
                "timestamp": time.time(),
                "hash": f"git_{int(time.time()*1e6):x}",
            }
            self._version_history[skill.skill_id].append(version_entry)
            logger.info("Committed skill: %s v%s (%d steps)", skill.name, skill.version, len(skill.steps))
            return version_entry

    def get_history(self, skill_id: str) -> List[Dict[str, Any]]:
        return self._version_history.get(skill_id, [])

    def diff_versions(self, skill_id: str, v1: str, v2: str) -> Dict[str, Any]:
        """模拟 git diff 两个版本。"""
        history = self._version_history.get(skill_id, [])
        h1 = next((h for h in history if h["version"] == v1), None)
        h2 = next((h for h in history if h["version"] == v2), None)
        if not h1 or not h2:
            return {"error": "Version not found"}
        return {
            "v1": {"version": v1, "steps": h1["steps_count"]},
            "v2": {"version": v2, "steps": h2["steps_count"]},
            "steps_changed": h2["steps_count"] - h1["steps_count"],
        }

    def statistics(self) -> Dict[str, Any]:
        return {"tracked_skills": len(self._version_history)}


# ---------------------------------------------------------------------------
# SkillProceduralMemory
# ---------------------------------------------------------------------------

class SkillProceduralMemory:
    """Skill Documents 过程性记忆系统。

    Parameters
    ----------
    repo_path : str
        git 仓库路径。
    """

    def __init__(self, repo_path: str = ".") -> None:
        self._skills: Dict[str, SkillDocument] = {}
        self.skill_validator = SkillValidator()
        self.skill_chain_composer = SkillChainComposer()
        self.git_versioned_skill = GitVersionedSkill(repo_path=repo_path)
        self._lock = threading.RLock()
        self._skill_count: int = 0

        logger.info("SkillProceduralMemory initialized [repo=%s]", repo_path)

    def create_skill(
        self,
        name: str,
        description: str,
        steps: List[SkillStep],
        trigger_keywords: Optional[List[str]] = None,
        pitfalls: Optional[List[SkillPitfall]] = None,
        verification: Optional[SkillVerification] = None,
        tags: Optional[List[str]] = None,
    ) -> Tuple[SkillDocument, bool, List[str]]:
        """创建技能文档——创建即验证。"""
        with self._lock:
            self._skill_count += 1
            skill = SkillDocument(
                skill_id=f"skill_{self._skill_count}_{int(time.time()*1e6)}",
                name=name,
                description=description,
                trigger=SkillTrigger(keywords=trigger_keywords or []),
                steps=steps,
                pitfalls=pitfalls or [],
                verification=verification,
                tags=tags or [],
            )

            # 创建即验证
            passed, issues = self.skill_validator.validate(skill)

            self._skills[skill.skill_id] = skill

            # Git 版本控制
            if passed:
                self.git_versioned_skill.commit_skill(skill, f"Created {name} v{skill.version}")

            return skill, passed, issues

    def compose_chain(self, name: str, skill_ids: List[str], description: str = "") -> Optional[SkillChain]:
        """组合技能链。"""
        skills = [self._skills[sid] for sid in skill_ids if sid in self._skills]
        if len(skills) != len(skill_ids):
            return None
        return self.skill_chain_composer.compose(name, skills, description=description)

    def export_skill_md(self, skill_id: str) -> Optional[str]:
        """导出为 SKILL.md 格式。"""
        skill = self._skills.get(skill_id)
        if not skill:
            return None
        return skill.to_skill_md()

    def get_skill(self, skill_id: str) -> Optional[SkillDocument]:
        return self._skills.get(skill_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total_tokens = sum(s.token_count() for s in self._skills.values())
            return {
                "total_skills": len(self._skills),
                "chains": self.skill_chain_composer.statistics()["total_chains"],
                "git_tracked": self.git_versioned_skill.statistics()["tracked_skills"],
                "avg_tokens": total_tokens // max(len(self._skills), 1),
            }
