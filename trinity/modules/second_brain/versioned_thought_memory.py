"""
# status: orphan (2026-08-15 audit, not in runtime path)
VersionedThoughtMemory — GitOfThoughts Reasoning Tree Versioning
=================================================================
arXiv 2606.14470 · P39-2

将思维链建模为 Git 风格的版本树: 每个推理节点存储为带 hash 的 commit,
支持 diff (版本差异) / merge (分支合并) / tag_thought + score_thought (质量标注)。

设计要点:
  - ThoughtCommit: 带 SHA-256 哈希的思维节点, 包含 parent 引用形成树
  - ThoughtBranch: 命名分支指针, 支持 checkout / branch / log
  - ThoughtDiff: 计算两版本间的内容差异 (块级 diff)
  - ThoughtMergeStrategy: 三路合并策略 (base / ours / theirs)
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DiffOperation(Enum):
    """差异操作类型。"""
    INSERT = auto()
    DELETE = auto()
    MODIFY = auto()
    UNCHANGED = auto()


class MergeStatus(Enum):
    """合并结果。"""
    SUCCESS = auto()
    CONFLICT = auto()
    ALREADY_UP_TO_DATE = auto()
    FAST_FORWARD = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ThoughtCommit:
    """思维链中的一个版本节点。

    Parameters
    ----------
    commit_hash : str
        SHA-256 哈希 (commit 唯一标识)。
    message : str
        提交信息。
    content : str
        思维内容。
    parent_hash : Optional[str]
        父 commit 哈希 (根节点为 None)。
    tags : List[str]
        标注标签。
    score : float
        质量评分 (0.0~1.0)。
    author : str
        创建者标识。
    timestamp : float
        创建时间。
    """
    commit_hash: str
    message: str
    content: str
    parent_hash: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    score: float = 0.0
    author: str = "system"
    timestamp: float = field(default_factory=time.time)


@dataclass
class DiffChunk:
    """差异块——描述两版本间的一个差异单元。"""
    operation: DiffOperation
    old_text: str = ""
    new_text: str = ""
    old_line_start: int = 0
    new_line_start: int = 0
    context: str = ""


@dataclass
class ThoughtBranch:
    """命名分支——指向某个 commit。"""
    name: str
    head_hash: str
    created_at: float = field(default_factory=time.time)
    description: str = ""


@dataclass
class ThoughtDiff:
    """版本差异结果。"""
    base_hash: str
    target_hash: str
    chunks: List[DiffChunk] = field(default_factory=list)
    total_insertions: int = 0
    total_deletions: int = 0
    total_modifications: int = 0


@dataclass
class ThoughtMerge:
    """合并结果。"""
    status: MergeStatus
    result_commit: Optional[ThoughtCommit] = None
    conflicts: List[str] = field(default_factory=list)
    merged_from: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ThoughtMergeStrategy
# ---------------------------------------------------------------------------

class ThoughtMergeStrategy:
    """思维三路合并策略: base=共同祖先, ours=当前分支, theirs=待合入分支。

    Parameters
    ----------
    conflict_marker : str
        冲突标记样式 (<<<<<<< / ======= / >>>>>>>)。
    """

    def __init__(self, conflict_marker: str = "---") -> None:
        self._conflict_marker = conflict_marker

    def three_way_merge(
        self,
        base: ThoughtCommit,
        ours: ThoughtCommit,
        theirs: ThoughtCommit,
    ) -> Tuple[str, List[str]]:
        """三路合并: 返回合并后内容 + 冲突列表。

        Returns
        -------
        Tuple[str, List[str]]
            (merged_content, conflict_sections)。
        """
        base_lines = base.content.splitlines()
        ours_lines = ours.content.splitlines()
        theirs_lines = theirs.content.splitlines()

        # Simplified LCS-based merge
        merged: List[str] = []
        conflicts: List[str] = []
        i, j = 0, 0
        # Use common prefix
        while i < min(len(ours_lines), len(theirs_lines)):
            o = ours_lines[i]
            t = theirs_lines[j] if j < len(theirs_lines) else ""
            if o == t:
                merged.append(o)
                i += 1
                j += 1
            elif o in set(base_lines) and t not in set(base_lines):
                merged.append(t)
                j += 1
            elif t in set(base_lines) and o not in set(base_lines):
                merged.append(o)
                i += 1
            else:
                # Conflict
                conflict_block = (
                    f"<<<<<<< ours\n{o}\n{self._conflict_marker}\n{t}\n>>>>>>> theirs"
                )
                merged.append(conflict_block)
                conflicts.append(conflict_block)
                i += 1
                j += 1

        # Append remaining
        merged.extend(ours_lines[i:])
        if j < len(theirs_lines):
            merged.extend(theirs_lines[j:])

        return "\n".join(merged), conflicts


# ---------------------------------------------------------------------------
# VersionedThoughtMemory
# ---------------------------------------------------------------------------

class VersionedThoughtMemory:
    """GitOfThoughts 版本化思维记忆系统。

    Parameters
    ----------
    repo_name : str
        仓库标识名。
    """

    def __init__(self, repo_name: str = "thoughts") -> None:
        self.repo_name = repo_name
        self._commits: Dict[str, ThoughtCommit] = {}
        self._branches: Dict[str, ThoughtBranch] = {}
        self._lock = threading.RLock()
        self._merge_strategy = ThoughtMergeStrategy()

        # Init main branch with root commit
        root = self._create_commit("Initial root commit", "", parent_hash=None)
        self._branches["main"] = ThoughtBranch(name="main", head_hash=root.commit_hash)

        logger.info("VersionedThoughtMemory repo '%s' initialized [commits=%d]", repo_name, len(self._commits))

    # ------------------------------------------------------------------
    # Commit API
    # ------------------------------------------------------------------

    def add_commit(
        self,
        content: str,
        message: str = "",
        parent_hash: Optional[str] = None,
        branch_name: str = "main",
    ) -> ThoughtCommit:
        """创建新 commit 并将分支 HEAD 指向它。

        Parameters
        ----------
        content : str
            思维内容。
        message : str
            提交信息。
        parent_hash : Optional[str]
            指定父 commit; None 则用当前分支 HEAD。
        branch_name : str
            目标分支。

        Returns
        -------
        ThoughtCommit
            新创建的 commit。
        """
        with self._lock:
            if parent_hash is None and branch_name in self._branches:
                parent_hash = self._branches[branch_name].head_hash

            commit = self._create_commit(content, message, parent_hash)

            # Update branch HEAD
            if branch_name in self._branches:
                self._branches[branch_name].head_hash = commit.commit_hash
            else:
                self._branches[branch_name] = ThoughtBranch(
                    name=branch_name, head_hash=commit.commit_hash,
                )

            logger.info("Commit %s on branch '%s': %s", commit.commit_hash[:8], branch_name, message)
            return commit

    def get_commit(self, commit_hash: str) -> Optional[ThoughtCommit]:
        return self._commits.get(commit_hash)

    def get_head(self, branch_name: str = "main") -> Optional[ThoughtCommit]:
        branch = self._branches.get(branch_name)
        if branch:
            return self._commits.get(branch.head_hash)
        return None

    def log(self, branch_name: str = "main", max_entries: int = 20) -> List[ThoughtCommit]:
        """获取分支历史 (HEAD 向前回溯)。"""
        branch = self._branches.get(branch_name)
        if not branch:
            return []
        result: List[ThoughtCommit] = []
        current_hash: Optional[str] = branch.head_hash
        while current_hash and len(result) < max_entries:
            commit = self._commits.get(current_hash)
            if not commit:
                break
            result.append(commit)
            current_hash = commit.parent_hash
        return result

    # ------------------------------------------------------------------
    # Branch API
    # ------------------------------------------------------------------

    def create_branch(self, name: str, from_branch: str = "main") -> ThoughtBranch:
        with self._lock:
            source = self._branches.get(from_branch)
            if not source:
                raise ValueError(f"Source branch '{from_branch}' not found")
            branch = ThoughtBranch(name=name, head_hash=source.head_hash)
            self._branches[name] = branch
            logger.info("Branch '%s' created from '%s'", name, from_branch)
            return branch

    def list_branches(self) -> List[ThoughtBranch]:
        return list(self._branches.values())

    def get_branch(self, name: str) -> Optional[ThoughtBranch]:
        return self._branches.get(name)

    # ------------------------------------------------------------------
    # Diff API
    # ------------------------------------------------------------------

    def diff(self, base_hash: str, target_hash: str) -> ThoughtDiff:
        """计算两个 commit 之间的内容差异。

        Parameters
        ----------
        base_hash : str
            基准 commit。
        target_hash : str
            目标 commit。

        Returns
        -------
        ThoughtDiff
            差异结果。
        """
        base = self._commits.get(base_hash)
        target = self._commits.get(target_hash)
        if not base or not target:
            return ThoughtDiff(base_hash=base_hash, target_hash=target_hash)

        result = ThoughtDiff(base_hash=base_hash, target_hash=target_hash)
        base_lines = base.content.splitlines()
        target_lines = target.content.splitlines()

        # Simple line-by-line diff
        max_len = max(len(base_lines), len(target_lines))
        for i in range(max_len):
            old = base_lines[i] if i < len(base_lines) else ""
            new = target_lines[i] if i < len(target_lines) else ""
            if old == "" and new != "":
                result.chunks.append(DiffChunk(DiffOperation.INSERT, "", new, old_line_start=i + 1, new_line_start=i + 1))
                result.total_insertions += 1
            elif old != "" and new == "":
                result.chunks.append(DiffChunk(DiffOperation.DELETE, old, "", old_line_start=i + 1, new_line_start=i + 1))
                result.total_deletions += 1
            elif old != new:
                result.chunks.append(DiffChunk(DiffOperation.MODIFY, old, new, old_line_start=i + 1, new_line_start=i + 1))
                result.total_modifications += 1

        return result

    # ------------------------------------------------------------------
    # Merge API
    # ------------------------------------------------------------------

    def merge(self, source_branch: str, into_branch: str = "main", message: str = "") -> ThoughtMerge:
        """将 source_branch 合并到 into_branch。

        使用三路合并策略: 基于最近共同祖先。

        Parameters
        ----------
        source_branch : str
            源分支名。
        into_branch : str
            目标分支名。
        message : str
            合并提交信息。

        Returns
        -------
        ThoughtMerge
            合并结果。
        """
        with self._lock:
            src = self._branches.get(source_branch)
            dst = self._branches.get(into_branch)
            if not src or not dst:
                return ThoughtMerge(status=MergeStatus.CONFLICT, conflicts=["branch not found"])

            src_commit = self._commits.get(src.head_hash)
            dst_commit = self._commits.get(dst.head_hash)
            if not src_commit or not dst_commit:
                return ThoughtMerge(status=MergeStatus.CONFLICT, conflicts=["commit not found"])

            # Find common ancestor
            ancestor = self._find_common_ancestor(src_commit, dst_commit)
            if not ancestor:
                return ThoughtMerge(status=MergeStatus.CONFLICT, conflicts=["no common ancestor"])

            if ancestor.commit_hash == dst.head_hash:
                # Fast-forward
                dst.head_hash = src.head_hash
                return ThoughtMerge(
                    status=MergeStatus.FAST_FORWARD,
                    result_commit=src_commit,
                    merged_from=[source_branch],
                )

            if ancestor.commit_hash == src.head_hash:
                # Already up-to-date
                return ThoughtMerge(status=MergeStatus.ALREADY_UP_TO_DATE)

            # Three-way merge
            merged_content, conflicts = self._merge_strategy.three_way_merge(ancestor, dst_commit, src_commit)

            merge_commit = self._create_commit(
                merged_content,
                message=message or f"Merge branch '{source_branch}' into '{into_branch}'",
                parent_hash=dst.head_hash,
            )
            dst.head_hash = merge_commit.commit_hash

            status = MergeStatus.CONFLICT if conflicts else MergeStatus.SUCCESS
            return ThoughtMerge(
                status=status,
                result_commit=merge_commit,
                conflicts=conflicts,
                merged_from=[source_branch],
            )

    # ------------------------------------------------------------------
    # Tag & Score API
    # ------------------------------------------------------------------

    def tag_thought(self, commit_hash: str, tag: str) -> bool:
        """为指定 commit 添加标签。"""
        commit = self._commits.get(commit_hash)
        if not commit:
            return False
        if tag not in commit.tags:
            commit.tags.append(tag)
        return True

    def score_thought(self, commit_hash: str, score: float) -> bool:
        """为指定 commit 设置质量评分 (0.0 ~ 1.0)。"""
        commit = self._commits.get(commit_hash)
        if not commit:
            return False
        commit.score = max(0.0, min(1.0, score))
        return True

    def search_by_tag(self, tag: str) -> List[ThoughtCommit]:
        """按标签搜索 commit。"""
        return [c for c in self._commits.values() if tag in c.tags]

    def top_scored(self, n: int = 10) -> List[ThoughtCommit]:
        """返回评分最高的 n 个 commit。"""
        scored = sorted(
            [c for c in self._commits.values() if c.score > 0],
            key=lambda c: c.score, reverse=True,
        )
        return scored[:n]

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            tagged = sum(1 for c in self._commits.values() if c.tags)
            scored = sum(1 for c in self._commits.values() if c.score > 0)
            return {
                "repo": self.repo_name,
                "total_commits": len(self._commits),
                "total_branches": len(self._branches),
                "tagged_commits": tagged,
                "scored_commits": scored,
                "branches": [{"name": b.name, "head": b.head_hash[:8]} for b in self._branches.values()],
            }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _create_commit(
        self, content: str, message: str, parent_hash: Optional[str] = None,
    ) -> ThoughtCommit:
        payload = f"{parent_hash or 'root'}:{message}:{content}:{time.time()}"
        h = hashlib.sha256(payload.encode()).hexdigest()
        commit = ThoughtCommit(
            commit_hash=h,
            message=message,
            content=content,
            parent_hash=parent_hash,
        )
        self._commits[h] = commit
        return commit

    def _find_common_ancestor(self, a: ThoughtCommit, b: ThoughtCommit) -> Optional[ThoughtCommit]:
        """找到两个 commit 的最近共同祖先 (BFS 从 a 出发)。"""
        ancestors: Set[str] = set()
        current: Optional[str] = a.commit_hash
        while current:
            ancestors.add(current)
            node = self._commits.get(current)
            current = node.parent_hash if node else None

        current = b.commit_hash
        while current:
            if current in ancestors:
                return self._commits[current]
            node = self._commits.get(current)
            current = node.parent_hash if node else None

        return None
