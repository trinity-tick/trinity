"""
# status: orphan (2026-08-15 audit, not in runtime path)
CB65: FilesystemMemoryOrganizer — 文件系统记忆组织器
=====================================================

对标 arXiv 2607.26637（Filesystem-Based Memory）。将记忆存储为目录树形式的
Markdown 文件，Agent 自行读写重组。管理/搜索/执行三角色协作。

设计要点：
  - 目录树存储：category/subcategory/.../memory_id.md
  - 三角色协作：Manager（CRUD+索引）、Searcher（全文+元数据检索）、
    Executor（批量操作+重组）
  - Markdown 格式：每条记忆为独立 .md 文件，含 YAML frontmatter 元数据
  - 统一陈述性记忆与技能存储：同一目录结构承载 facts + skills
  - 与 FilesystemMemoryOrganizer API 解耦：通过虚拟树操作，不直接写磁盘
    （实际落地由 external adapter 完成）

Reference:
  - arXiv 2607.26637 "Filesystem-Based Memory for AI Agents"
  - Filesystem-as-memory: Markdown + YAML frontmatter + directory tree
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import threading
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class FSOperationType(Enum):
    """文件系统操作类型。"""
    CREATE = "create"        # 创建记忆文件
    READ = "read"             # 读取记忆文件
    UPDATE = "update"         # 更新记忆内容
    DELETE = "delete"         # 删除（软删除，标记 archived）
    MOVE = "move"             # 移动到其他目录
    SEARCH = "search"         # 搜索操作
    REORGANIZE = "reorganize"  # 重组目录结构


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class MemoryFile:
    """一条记忆的 Markdown 文件表示。

    Attributes:
        file_id: 唯一文件 ID（基于路径哈希）。
        path: 虚拟路径（如 /knowledge/work/meeting_001.md）。
        content: Markdown 正文内容。
        frontmatter: YAML 元数据字典。
        tags: 标签列表。
        created_at: 创建时间戳。
        updated_at: 最后更新时间戳。
        archived: 是否已归档（软删除）。
        file_size: 文件大小（字节）。
    """
    file_id: str
    path: str
    content: str = ""
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=_time.time)
    updated_at: float = field(default_factory=_time.time)
    archived: bool = False
    file_size: int = 0

    def __post_init__(self):
        if not self.file_size:
            self.file_size = len(self.content.encode("utf-8"))


@dataclass
class DirectoryTree:
    """虚拟目录树节点。

    Attributes:
        dir_id: 目录唯一 ID。
        path: 目录虚拟路径。
        parent: 父目录 ID（根为 None）。
        children: 子目录 ID 列表。
        files: 本目录下的文件 ID 列表。
    """
    dir_id: str
    path: str = "/"
    parent: Optional[str] = None
    children: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)


@dataclass
class FSOperation:
    """文件系统操作记录。

    Attributes:
        op_type: 操作类型。
        target_path: 目标路径。
        timestamp: 操作时间。
        detail: 附加信息。
    """
    op_type: FSOperationType
    target_path: str
    timestamp: float = field(default_factory=_time.time)
    detail: str = ""


@dataclass
class FSOConfig:
    """文件系统记忆组织器配置。

    Attributes:
        root_path: 虚拟根路径。
        max_tags_per_file: 每个文件最大标签数。
        max_search_results: 搜索最大返回数。
    """
    root_path: str = "/memory"
    max_tags_per_file: int = 20
    max_search_results: int = 50


# ============================================================================
# Main Class
# ============================================================================

class FilesystemMemoryOrganizer:
    """文件系统记忆组织器 (CB65)。

    三角色协作架构：
      - Manager: create / read / update / delete
      - Searcher: search_by_tags / search_by_content / search_by_metadata
      - Executor: reorganize / move / batch operations

    Usage:
        fmo = FilesystemMemoryOrganizer()
        f1 = fmo.create("/knowledge/algorithms/sort.md", "# Sorting\n...", tags=["algo"])
        results = fmo.search_by_tags(["algo"])
        fmo.reorganize("/knowledge/algorithms/", "/tech/algorithms/")
    """

    def __init__(self, config: Optional[FSOConfig] = None):
        self.config = config or FSOConfig()
        self._lock = threading.RLock()
        self._files: Dict[str, MemoryFile] = {}
        self._dirs: Dict[str, DirectoryTree] = {}
        self._op_log: List[FSOperation] = []
        self._start_time: float = _time.time()

        # Initialize root
        root_id = self._dir_id("/")
        self._dirs[root_id] = DirectoryTree(dir_id=root_id, path="/")

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _file_id(self, path: str) -> str:
        return hashlib.md5(path.encode()).hexdigest()[:12]

    def _dir_id(self, path: str) -> str:
        return "dir_" + hashlib.md5(path.encode()).hexdigest()[:12]

    def _ensure_dir(self, dir_path: str) -> str:
        """确保目录存在，返回 dir_id。"""
        dir_id = self._dir_id(dir_path)
        if dir_id not in self._dirs:
            parent_path = "/".join(dir_path.rstrip("/").split("/")[:-1]) or "/"
            parent_id = self._ensure_dir(parent_path)
            self._dirs[dir_id] = DirectoryTree(
                dir_id=dir_id, path=dir_path, parent=parent_id,
            )
            self._dirs[parent_id].children.append(dir_id)
        return dir_id

    def _log_op(self, op_type: FSOperationType, path: str, detail: str = ""):
        self._op_log.append(FSOperation(
            op_type=op_type, target_path=path, detail=detail,
        ))

    # ------------------------------------------------------------------
    # Role 1: Manager (CRUD)
    # ------------------------------------------------------------------

    def create(
        self,
        path: str,
        content: str,
        tags: Optional[List[str]] = None,
        frontmatter: Optional[Dict[str, Any]] = None,
    ) -> MemoryFile:
        """创建新记忆文件。

        Args:
            path: 虚拟文件路径（如 /knowledge/notes.md）。
            content: Markdown 正文。
            tags: 标签列表。
            frontmatter: YAML 元数据。

        Returns:
            MemoryFile: 创建的文件对象。
        """
        with self._lock:
            file_id = self._file_id(path)
            if file_id in self._files:
                raise FileExistsError(f"Memory file already exists: {path}")

            dir_path = "/".join(path.rstrip("/").split("/")[:-1]) or "/"
            dir_id = self._ensure_dir(dir_path)

            mf = MemoryFile(
                file_id=file_id,
                path=path,
                content=content,
                frontmatter=frontmatter or {},
                tags=tags[:self.config.max_tags_per_file] if tags else [],
            )
            self._files[file_id] = mf
            self._dirs[dir_id].files.append(file_id)
            self._log_op(FSOperationType.CREATE, path, f"tags={mf.tags}")
            return mf

    def read(self, path: str) -> Optional[MemoryFile]:
        """读取记忆文件。"""
        with self._lock:
            file_id = self._file_id(path)
            mf = self._files.get(file_id)
            if mf and not mf.archived:
                return mf
            return None

    def update(
        self,
        path: str,
        content: Optional[str] = None,
        tags: Optional[List[str]] = None,
        frontmatter: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemoryFile]:
        """更新记忆文件内容/标签/元数据。"""
        with self._lock:
            mf = self.read(path)
            if mf is None:
                return None
            if content is not None:
                mf.content = content
                mf.file_size = len(content.encode("utf-8"))
            if tags is not None:
                mf.tags = tags[:self.config.max_tags_per_file]
            if frontmatter is not None:
                mf.frontmatter.update(frontmatter)
            mf.updated_at = _time.time()
            self._log_op(FSOperationType.UPDATE, path, f"tags={mf.tags}")
            return mf

    def delete(self, path: str) -> bool:
        """软删除（归档）记忆文件。"""
        with self._lock:
            mf = self.read(path)
            if mf is None:
                return False
            mf.archived = True
            mf.updated_at = _time.time()
            self._log_op(FSOperationType.DELETE, path)
            return True

    def move(self, source: str, target: str) -> bool:
        """移动文件到新路径。"""
        with self._lock:
            mf = self.read(source)
            if mf is None:
                return False

            new_id = self._file_id(target)
            if new_id in self._files:
                return False  # target exists

            # Remove from old dir
            old_dir = "/".join(source.rstrip("/").split("/")[:-1]) or "/"
            old_dir_id = self._dir_id(old_dir)
            if old_dir_id in self._dirs:
                self._dirs[old_dir_id].files = [
                    f for f in self._dirs[old_dir_id].files if f != mf.file_id
                ]

            # Add to new dir
            new_dir = "/".join(target.rstrip("/").split("/")[:-1]) or "/"
            new_dir_id = self._ensure_dir(new_dir)

            # Update file identity
            del self._files[mf.file_id]
            mf.file_id = new_id
            mf.path = target
            mf.updated_at = _time.time()
            self._files[new_id] = mf
            self._dirs[new_dir_id].files.append(new_id)

            self._log_op(FSOperationType.MOVE, f"{source} -> {target}")
            return True

    # ------------------------------------------------------------------
    # Role 2: Searcher
    # ------------------------------------------------------------------

    def search_by_tags(self, tags: List[str]) -> List[MemoryFile]:
        """按标签搜索（AND 逻辑）。"""
        with self._lock:
            results = []
            for mf in self._files.values():
                if mf.archived:
                    continue
                if all(t in mf.tags for t in tags):
                    results.append(mf)
            return results[:self.config.max_search_results]

    def search_by_content(self, keyword: str) -> List[MemoryFile]:
        """全文关键词搜索（大小写不敏感）。"""
        with self._lock:
            kw = keyword.lower()
            results = []
            for mf in self._files.values():
                if mf.archived:
                    continue
                if kw in mf.content.lower():
                    results.append(mf)
            return results[:self.config.max_search_results]

    def search_by_metadata(self, key: str, value: Any) -> List[MemoryFile]:
        """按 frontmatter 元数据字段搜索。"""
        with self._lock:
            results = []
            for mf in self._files.values():
                if mf.archived:
                    continue
                if key in mf.frontmatter and mf.frontmatter[key] == value:
                    results.append(mf)
            return results[:self.config.max_search_results]

    # ------------------------------------------------------------------
    # Role 3: Executor (batch + reorganize)
    # ------------------------------------------------------------------

    def reorganize(
        self,
        source_dir: str,
        target_dir: str,
        tag_filter: Optional[List[str]] = None,
    ) -> int:
        """重组目录：将 source_dir 下的文件移动到 target_dir。

        Args:
            source_dir: 源目录虚拟路径。
            target_dir: 目标目录虚拟路径。
            tag_filter: 仅移动含这些标签的文件（None=全部）。

        Returns:
            int: 移动的文件数。
        """
        with self._lock:
            src_dir_id = self._dir_id(source_dir)
            if src_dir_id not in self._dirs:
                return 0

            moved = 0
            file_ids = list(self._dirs[src_dir_id].files)
            for fid in file_ids:
                mf = self._files.get(fid)
                if mf is None or mf.archived:
                    continue
                if tag_filter and not all(t in mf.tags for t in tag_filter):
                    continue

                new_path = target_dir.rstrip("/") + "/" + mf.path.split("/")[-1]
                if self.move(mf.path, new_path):
                    moved += 1

            self._log_op(FSOperationType.REORGANIZE,
                        f"{source_dir} -> {target_dir}",
                        f"moved={moved}")
            return moved

    def list_dir(self, dir_path: str = "/") -> List[MemoryFile]:
        """列出目录下所有文件。"""
        with self._lock:
            dir_id = self._dir_id(dir_path)
            if dir_id not in self._dirs:
                return []
            return [
                self._files[fid] for fid in self._dirs[dir_id].files
                if fid in self._files and not self._files[fid].archived
            ]

    def tree_stats(self) -> Dict[str, int]:
        """目录树统计。"""
        with self._lock:
            total = sum(1 for mf in self._files.values() if not mf.archived)
            archived = sum(1 for mf in self._files.values() if mf.archived)
            return {
                "total_dirs": len(self._dirs),
                "total_files": total,
                "archived_files": archived,
                "total_operations": len(self._op_log),
            }

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            ts = self.tree_stats()
            return {
                "class": "FilesystemMemoryOrganizer (CB65)",
                **ts,
                "total_tags": len(set(
                    t for mf in self._files.values() for t in mf.tags
                )),
                "total_content_kb": round(
                    sum(mf.file_size for mf in self._files.values()) / 1024, 2
                ),
                "uptime_seconds": round(_time.time() - self._start_time, 3),
            }
