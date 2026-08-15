"""
# status: orphan (2026-08-15 audit, not in runtime path)
P10-1: Codebase Typed Graph Memory Layer (对标 Cognee)

实现 AST 级代码实体提取、类型化关系图谱、影响面分析、CI 增量更新、
代码+文档联合摄入。

Reference: Cognee — Persistent Codebase Memory for Coding Agents (2026)
           https://www.cognee.ai/blog/tutorials/ai-coding-agent-persistent-codebase-memory
"""

import ast
import hashlib
import json
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


# ─── Enums ───────────────────────────────────────────────────────────────────

class EntityType(Enum):
    """代码实体类型"""
    MODULE = "module"
    FUNCTION = "function"
    CLASS = "class"
    INTERFACE = "interface"          # Protocol / ABC / abstract base
    METHOD = "method"
    IMPORT = "import"
    VARIABLE = "variable"
    DOCUMENT = "document"            # README / API docs / architecture docs


class RelationKind(Enum):
    """类型化关系种类（对标 Cognee typed relationships）"""
    CALLS = "calls"                  # f() 调用 g()
    INHERITS = "inherits"            # class B(A)
    IMPLEMENTS = "implements"        # class Impl(Protocol)
    IMPORTS = "imports"              # import / from ... import
    DEFINES = "defines"              # module defines class/function
    DEPENDS_ON = "depends_on"        # 语义依赖（非代码级 import，如文档→代码）
    DOCUMENTS = "documents"          # 文档描述某个实现
    OVERRIDES = "overrides"          # 方法覆写


# ─── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class CodeEntity:
    """代码实体节点"""
    id: str                           # 全局唯一 ID：module::qualname
    name: str                         # 短名称
    entity_type: EntityType
    file_path: str                    # 源文件绝对路径
    lineno: int                       # 定义所在行号（1-based）
    signature: str = ""               # 函数签名 / 类基类列表
    docstring: str = ""               # 文档字符串
    annotations: list[str] = field(default_factory=list)  # 类型注解
    properties: dict = field(default_factory=dict)
    content_hash: str = ""            # 用于增量变更检测


@dataclass
class TypedRelation:
    """类型化关系边"""
    subject: str                      # 主体实体 ID
    predicate: RelationKind           # 关系类型
    object: str                       # 客体实体 ID
    weight: float = 1.0
    metadata: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class ImpactReport:
    """影响面分析报告"""
    entity: str
    entity_type: EntityType
    explosion_radius: int             # 直接+间接受影响实体数
    direct_callers: list[str]         # 直接调用方
    transitive_callers: list[str]     # 传递调用方
    inheritors: list[str]             # 子类
    importers: list[str]              # 导入该模块的文件
    documentation_links: list[str]    # 关联文档
    estimated_change_cost: str        # "low" / "medium" / "high" / "critical"


@dataclass
class FileSnapshot:
    """文件快照，用于增量更新"""
    file_path: str
    mtime: float
    content_hash: str


@dataclass
class JointIngestionResult:
    """代码+文档联合摄入结果"""
    source_files: int
    doc_files: int
    entities_extracted: int
    relations_created: int
    cross_links: int                 # 文档→代码的 DEPENDS_ON 链接数


# ─── AST Extractor ──────────────────────────────────────────────────────────

class ASTEntityExtractor:
    """从 Python 源文件中提取 AST 级代码实体。

    提取：函数、类、接口(Protocol/ABC)、方法、导入、模块级变量。
    """

    def __init__(self, file_path: str, source_code: str):
        self.file_path = os.path.abspath(file_path)
        self.source = source_code
        self.module_name = self._derive_module_name()
        self.entities: list[CodeEntity] = []
        self._content_hash = hashlib.sha256(source_code.encode()).hexdigest()

    def _derive_module_name(self) -> str:
        base = os.path.splitext(os.path.basename(self.file_path))[0]
        return base

    def extract(self) -> list[CodeEntity]:
        """执行 AST 遍历并返回所有提取的实体。"""
        try:
            tree = ast.parse(self.source)
        except SyntaxError:
            return self.entities

        # 模块实体
        module_entity = CodeEntity(
            id=f"{self.module_name}::module",
            name=self.module_name,
            entity_type=EntityType.MODULE,
            file_path=self.file_path,
            lineno=1,
            signature=f"module:{self.module_name}",
            content_hash=self._content_hash,
        )
        self.entities.append(module_entity)

        # 遍历顶层节点
        for node in ast.iter_child_nodes(tree):
            self._visit_top_level(node, parent_id=module_entity.id, parent_cls=None)

        return self.entities

    def _visit_top_level(self, node: ast.AST, parent_id: str, parent_cls: Optional[str]):
        if isinstance(node, ast.FunctionDef):
            entity = self._make_function_entity(node, parent_id, parent_cls)
            self.entities.append(entity)

        elif isinstance(node, ast.AsyncFunctionDef):
            entity = self._make_function_entity(node, parent_id, parent_cls, is_async=True)
            self.entities.append(entity)

        elif isinstance(node, ast.ClassDef):
            entity = self._make_class_entity(node, parent_id)
            self.entities.append(entity)
            # 类内方法
            for body_node in node.body:
                if isinstance(body_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method = self._make_function_entity(
                        body_node, entity.id, parent_cls=entity.name
                    )
                    method.entity_type = EntityType.METHOD
                    self.entities.append(method)

        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            entity = self._make_import_entity(node, parent_id)
            self.entities.append(entity)

        elif isinstance(node, ast.Assign):
            # 模块级变量
            for target in node.targets:
                if isinstance(target, ast.Name):
                    entity = CodeEntity(
                        id=f"{self.module_name}::{target.id}",
                        name=target.id,
                        entity_type=EntityType.VARIABLE,
                        file_path=self.file_path,
                        lineno=node.lineno,
                        content_hash=self._content_hash,
                    )
                    self.entities.append(entity)

    def _make_function_entity(self, node, parent_id: str,
                               parent_cls: Optional[str],
                               is_async: bool = False) -> CodeEntity:
        qualname = f"{parent_cls}.{node.name}" if parent_cls else node.name
        args = [arg.arg for arg in node.args.args]
        prefix = "async " if is_async else ""
        signature = f"{prefix}def {node.name}({', '.join(args)})"
        doc = ast.get_docstring(node) or ""
        annotations = self._extract_annotations(node)

        return CodeEntity(
            id=f"{self.module_name}::{qualname}",
            name=node.name,
            entity_type=EntityType.FUNCTION,
            file_path=self.file_path,
            lineno=node.lineno,
            signature=signature,
            docstring=doc,
            annotations=annotations,
            content_hash=self._content_hash,
            properties={"parent_id": parent_id, "is_async": is_async},
        )

    def _make_class_entity(self, node: ast.ClassDef, parent_id: str) -> CodeEntity:
        bases = [self._unparse_base(b) for b in node.bases]
        signature = f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
        doc = ast.get_docstring(node) or ""

        # 判断是否为接口（Protocol / ABC）
        entity_type = EntityType.CLASS
        if any(b in ["Protocol", "ABC"] for b in bases):
            entity_type = EntityType.INTERFACE

        return CodeEntity(
            id=f"{self.module_name}::{node.name}",
            name=node.name,
            entity_type=entity_type,
            file_path=self.file_path,
            lineno=node.lineno,
            signature=signature,
            docstring=doc,
            content_hash=self._content_hash,
            properties={"parent_id": parent_id, "bases": bases},
        )

    def _make_import_entity(self, node, parent_id: str) -> CodeEntity:
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
            entity_name = f"import_{names[0]}"
            sig = f"import {', '.join(names)}"
        else:
            names = [alias.name for alias in node.names]
            entity_name = f"from_{node.module}_import_{names[0]}" if node.module else f"from_import_{names[0]}"
            sig = f"from {node.module or '...'} import {', '.join(names)}" if node.module else f"from ... import {', '.join(names)}"

        return CodeEntity(
            id=f"{self.module_name}::{entity_name}",
            name=entity_name,
            entity_type=EntityType.IMPORT,
            file_path=self.file_path,
            lineno=node.lineno,
            signature=sig,
            content_hash=self._content_hash,
            properties={"parent_id": parent_id, "imported_names": names},
        )

    def _extract_annotations(self, node) -> list[str]:
        anns = []
        for arg in node.args.args:
            if arg.annotation:
                anns.append(self._unparse_base(arg.annotation))
        if node.returns:
            anns.append(f"-> {self._unparse_base(node.returns)}")
        return anns

    @staticmethod
    def _unparse_base(node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return str(type(node).__name__)


# ─── Relation Extractor ─────────────────────────────────────────────────────

class RelationExtractor:
    """从已提取的实体集合中推断类型化关系。"""

    def __init__(self, entities: list[CodeEntity], source_code: str):
        self.entities = entities
        self.source = source_code
        self.entity_by_id: dict[str, CodeEntity] = {e.id: e for e in entities}
        self.relations: list[TypedRelation] = []

    def extract_all(self) -> list[TypedRelation]:
        """提取所有关系。"""
        try:
            tree = ast.parse(self.source)
        except SyntaxError:
            return self.relations

        # 1. DEFINES：module → 其下函数/类
        module_entity = self.entity_by_id.get(
            f"{os.path.splitext(os.path.basename(self.entities[0].file_path))[0]}::module"
        )
        if module_entity:
            for e in self.entities:
                if e.entity_type in (EntityType.FUNCTION, EntityType.CLASS,
                                     EntityType.INTERFACE, EntityType.METHOD,
                                     EntityType.VARIABLE):
                    if e.id != module_entity.id:
                        self.relations.append(TypedRelation(
                            subject=module_entity.id,
                            predicate=RelationKind.DEFINES,
                            object=e.id,
                        ))

        # 2. CALLS：函数体内的函数调用
        self._extract_calls(tree)

        # 3. INHERITS / IMPLEMENTS：类继承
        self._extract_inheritance()

        # 4. OVERRIDES：子类方法覆写
        self._extract_overrides()

        # 5. IMPORTS：import 语句 → 被导入模块
        self._extract_imports()

        return self.relations

    def _extract_calls(self, tree: ast.AST):
        """提取 CALLS 关系。"""
        # 建立当前模块内函数名→实体 ID 映射
        local_funcs: dict[str, str] = {}
        for e in self.entities:
            if e.entity_type in (EntityType.FUNCTION, EntityType.METHOD):
                local_funcs[e.name] = e.id

        # 遍历所有函数调用
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called_name = self._get_call_name(node)
                if called_name and called_name in local_funcs:
                    # 需要确定 caller——找到包含该调用的最近函数/方法
                    caller = self._find_enclosing_function(node, tree)
                    if caller and caller.id != local_funcs[called_name]:
                        self.relations.append(TypedRelation(
                            subject=caller.id,
                            predicate=RelationKind.CALLS,
                            object=local_funcs[called_name],
                            metadata={"lineno": node.lineno},
                        ))

    def _extract_inheritance(self):
        """提取 INHERITS / IMPLEMENTS 关系。"""
        for e in self.entities:
            if e.entity_type in (EntityType.CLASS, EntityType.INTERFACE):
                bases = e.properties.get("bases", [])
                for base in bases:
                    # 查找当前模块内是否有该基类
                    for other in self.entities:
                        if other.name == base and other.entity_type in (
                            EntityType.CLASS, EntityType.INTERFACE
                        ):
                            pred = RelationKind.IMPLEMENTS if (
                                other.entity_type == EntityType.INTERFACE
                            ) else RelationKind.INHERITS
                            self.relations.append(TypedRelation(
                                subject=e.id,
                                predicate=pred,
                                object=other.id,
                            ))
                            break
                    else:
                        # 跨模块继承 — 记录为 INHERITS 到外部实体
                        self.relations.append(TypedRelation(
                            subject=e.id,
                            predicate=RelationKind.INHERITS,
                            object=f"external::{base}",
                            metadata={"external": True},
                        ))

    def _extract_overrides(self):
        """提取 OVERRIDES 关系。"""
        # 按类分组方法
        class_methods: dict[str, dict[str, str]] = defaultdict(dict)
        for e in self.entities:
            if e.entity_type == EntityType.METHOD:
                parent_id = e.properties.get("parent_id", "")
                class_methods[parent_id][e.name] = e.id

        # 查找继承链中的同名方法
        for e in self.entities:
            if e.entity_type in (EntityType.CLASS, EntityType.INTERFACE):
                bases = e.properties.get("bases", [])
                for base in bases:
                    for other in self.entities:
                        if other.name == base:
                            # 比较方法
                            my_methods = class_methods.get(e.id, {})
                            base_methods = class_methods.get(other.id, {})
                            for mname in my_methods:
                                if mname in base_methods:
                                    self.relations.append(TypedRelation(
                                        subject=my_methods[mname],
                                        predicate=RelationKind.OVERRIDES,
                                        object=base_methods[mname],
                                    ))

    def _extract_imports(self):
        """提取 IMPORTS 关系。"""
        for e in self.entities:
            if e.entity_type == EntityType.IMPORT:
                imported = e.properties.get("imported_names", [])
                for name in imported:
                    # 查找本模块是否有同名定义（from-import 已引入）
                    for other in self.entities:
                        if other.name == name and other.id != e.id:
                            self.relations.append(TypedRelation(
                                subject=e.id,
                                predicate=RelationKind.IMPORTS,
                                object=other.id,
                            ))

    def _get_call_name(self, node: ast.Call) -> Optional[str]:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return None

    def _find_enclosing_function(self, node: ast.AST, tree: ast.AST) -> Optional[CodeEntity]:
        """找到包含给定节点的最近函数/方法实体。"""
        # 简化：用实体中 lineno 范围匹配
        candidates = [
            e for e in self.entities
            if e.entity_type in (EntityType.FUNCTION, EntityType.METHOD)
            and e.lineno <= node.lineno
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda e: e.lineno)


# ─── Graph Store ─────────────────────────────────────────────────────────────

class CodebaseGraphStore:
    """类型化代码图谱存储。

    维护实体索引、关系邻接表、文件快照表，支持影响面分析和增量更新。
    """

    def __init__(self, storage_path: Optional[str] = None):
        if storage_path is None:
            project_root = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."
            ))
            storage_path = os.path.join(project_root, "data", "codebase_graph.jsonl")
        self.storage_path = storage_path

        self.entities: dict[str, CodeEntity] = {}
        self.relations: list[TypedRelation] = []
        self._adjacency: dict[str, list[int]] = defaultdict(list)  # entity_id → relation indices
        self._reverse_adj: dict[str, list[int]] = defaultdict(list)  # object → relation indices
        self.file_snapshots: dict[str, FileSnapshot] = {}

        self._load()

    # ── CRUD ──

    def add_entity(self, entity: CodeEntity):
        self.entities[entity.id] = entity

    def add_relation(self, rel: TypedRelation):
        idx = len(self.relations)
        self.relations.append(rel)
        self._adjacency[rel.subject].append(idx)
        self._reverse_adj[rel.object].append(idx)

    def get_entity(self, entity_id: str) -> Optional[CodeEntity]:
        return self.entities.get(entity_id)

    def remove_entity(self, entity_id: str):
        """移除实体及其关联的所有关系。"""
        self.entities.pop(entity_id, None)
        for idx in self._adjacency.pop(entity_id, []):
            rel = self.relations[idx]
            self._reverse_adj.get(rel.object, []).remove(idx)
        for idx in self._reverse_adj.pop(entity_id, []):
            rel = self.relations[idx]
            self._adjacency.get(rel.subject, []).remove(idx)

    # ── Ingestion ──

    def ingest_file(self, file_path: str, source_code: str):
        """摄入单个源文件。"""
        # 清除旧实体
        old_entity_ids = [eid for eid, e in self.entities.items()
                          if e.file_path == os.path.abspath(file_path)]
        for eid in old_entity_ids:
            self.remove_entity(eid)

        # 提取实体
        extractor = ASTEntityExtractor(file_path, source_code)
        entities = extractor.extract()
        for e in entities:
            self.add_entity(e)

        # 提取关系
        rel_extractor = RelationExtractor(entities, source_code)
        for rel in rel_extractor.extract_all():
            self.add_relation(rel)

        # 更新文件快照
        abs_path = os.path.abspath(file_path)
        self.file_snapshots[abs_path] = FileSnapshot(
            file_path=abs_path,
            mtime=os.path.getmtime(abs_path) if os.path.exists(abs_path) else time.time(),
            content_hash=hashlib.sha256(source_code.encode()).hexdigest(),
        )

    def ingest_directory(self, dir_path: str, pattern: str = "*.py") -> JointIngestionResult:
        """摄入整个目录的 Python 源文件。

        Args:
            dir_path: 目录路径
            pattern: 文件 glob 模式
        """
        source_files = 0
        doc_files = 0
        for filepath in Path(dir_path).rglob(pattern):
            if filepath.is_file():
                try:
                    source_code = filepath.read_text(encoding="utf-8")
                    self.ingest_file(str(filepath), source_code)
                    source_files += 1
                except Exception:
                    pass

        entities_before = len(self.entities)
        relations_before = len(self.relations)
        cross_links = self._link_documentation(dir_path)

        return JointIngestionResult(
            source_files=source_files,
            doc_files=doc_files,
            entities_extracted=len(self.entities) - entities_before,
            relations_created=len(self.relations) - relations_before,
            cross_links=cross_links,
        )

    def _link_documentation(self, dir_path: str) -> int:
        """扫描文档文件并链接到对应代码实体。"""
        doc_patterns = ["*.md", "*.rst", "*.txt"]
        cross_links = 0
        doc_entities_added = 0

        for pattern in doc_patterns:
            for filepath in Path(dir_path).rglob(pattern):
                if filepath.is_file():
                    try:
                        content = filepath.read_text(encoding="utf-8")
                    except Exception:
                        continue
                    doc_hash = hashlib.sha256(content.encode()).hexdigest()

                    doc_entity = CodeEntity(
                        id=f"doc::{filepath.stem}",
                        name=filepath.name,
                        entity_type=EntityType.DOCUMENT,
                        file_path=str(filepath),
                        lineno=1,
                        signature=f"document:{filepath.name}",
                        content_hash=doc_hash,
                    )
                    self.add_entity(doc_entity)
                    doc_entities_added += 1

                    # 匹配文档中提到的代码实体
                    for eid, entity in self.entities.items():
                        if entity.entity_type != EntityType.DOCUMENT:
                            if entity.name in content:
                                self.add_relation(TypedRelation(
                                    subject=doc_entity.id,
                                    predicate=RelationKind.DOCUMENTS,
                                    object=eid,
                                ))
                                cross_links += 1

        return cross_links

    # ── Incremental Update (CI Trigger) ──

    def incremental_update(self, changed_files: list[str]):
        """CI 触发增量更新——仅处理变更文件。

        Args:
            changed_files: 变更文件路径列表
        """
        for fp in changed_files:
            abs_path = os.path.abspath(fp)
            if not os.path.exists(abs_path):
                # 文件已删除，清除相关实体
                old_ids = [eid for eid, e in self.entities.items()
                           if e.file_path == abs_path]
                for eid in old_ids:
                    self.remove_entity(eid)
                self.file_snapshots.pop(abs_path, None)
                continue

            current_hash = hashlib.sha256(
                Path(abs_path).read_text(encoding="utf-8").encode()
            ).hexdigest()

            snapshot = self.file_snapshots.get(abs_path)
            if snapshot and snapshot.content_hash == current_hash:
                continue  # 未变更

            try:
                source_code = Path(abs_path).read_text(encoding="utf-8")
                self.ingest_file(abs_path, source_code)
            except Exception:
                pass

    # ── Impact Surface Analysis ──

    def impact_surface(self, entity_id: str, max_depth: int = 5) -> ImpactReport:
        """计算指定实体的变更爆炸半径。

        使用多路径 BFS 遍历：CALLS 反向→调用方、INHERITS 反向→子类、
        IMPORTS 反向→导入方、DOCUMENTS 反向→关联文档。

        Args:
            entity_id: 实体 ID
            max_depth: 最大传递深度

        Returns:
            ImpactReport 包含爆炸半径与受影响实体清单
        """
        entity = self.entities.get(entity_id)
        if not entity:
            return ImpactReport(
                entity=entity_id,
                entity_type=EntityType.FUNCTION,
                explosion_radius=0,
                direct_callers=[],
                transitive_callers=[],
                inheritors=[],
                importers=[],
                documentation_links=[],
                estimated_change_cost="low",
            )

        direct_callers = []
        transitive_callers = []
        inheritors = []
        importers = []
        doc_links = []

        # BFS
        visited = {entity_id}
        queue = deque([(entity_id, 0)])

        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue

            # 反向查找：谁引用了 current？
            for rel_idx in self._reverse_adj.get(current, []):
                rel = self.relations[rel_idx]
                caller_id = rel.subject

                if caller_id in visited:
                    continue
                visited.add(caller_id)

                if rel.predicate == RelationKind.CALLS:
                    if depth == 0:
                        direct_callers.append(caller_id)
                    else:
                        transitive_callers.append(caller_id)
                elif rel.predicate == RelationKind.INHERITS:
                    inheritors.append(caller_id)
                elif rel.predicate == RelationKind.IMPLEMENTS:
                    inheritors.append(caller_id)
                elif rel.predicate == RelationKind.IMPORTS:
                    importers.append(caller_id)
                elif rel.predicate == RelationKind.DOCUMENTS:
                    doc_links.append(caller_id)

                queue.append((caller_id, depth + 1))

        explosion_radius = len(direct_callers) + len(transitive_callers) + len(inheritors)

        # 估算变更成本
        if explosion_radius == 0:
            cost = "low"
        elif explosion_radius <= 5:
            cost = "medium"
        elif explosion_radius <= 20:
            cost = "high"
        else:
            cost = "critical"

        return ImpactReport(
            entity=entity_id,
            entity_type=entity.entity_type,
            explosion_radius=explosion_radius,
            direct_callers=direct_callers,
            transitive_callers=transitive_callers,
            inheritors=inheritors,
            importers=importers,
            documentation_links=doc_links,
            estimated_change_cost=cost,
        )

    # ── Query ──

    def query_by_relation(self, predicate: RelationKind) -> list[TypedRelation]:
        return [r for r in self.relations if r.predicate == predicate]

    def get_callers(self, entity_id: str) -> list[str]:
        """获取调用该实体的所有调用方。"""
        report = self.impact_surface(entity_id)
        return report.direct_callers + report.transitive_callers

    def get_subgraph(self, entity_id: str, depth: int = 2) -> dict:
        """提取以 entity_id 为中心的子图。"""
        nodes = {entity_id: self._entity_to_dict(entity_id)}
        edges = []

        visited = {entity_id}
        queue = deque([(entity_id, 0)])

        while queue:
            current, d = queue.popleft()
            if d >= depth:
                continue
            for rel_idx in self._adjacency.get(current, []) + self._reverse_adj.get(current, []):
                rel = self.relations[rel_idx]
                other = rel.object if rel.subject == current else rel.subject
                edges.append({
                    "subject": rel.subject,
                    "predicate": rel.predicate.value,
                    "object": rel.object,
                    "weight": rel.weight,
                })
                if other not in visited:
                    visited.add(other)
                    nodes[other] = self._entity_to_dict(other)
                    queue.append((other, d + 1))

        return {"nodes": list(nodes.values()), "edges": edges}

    def _entity_to_dict(self, entity_id: str) -> dict:
        e = self.entities.get(entity_id)
        if not e:
            return {"id": entity_id, "name": entity_id, "type": "unknown"}
        return {
            "id": e.id,
            "name": e.name,
            "type": e.entity_type.value,
            "file_path": e.file_path,
            "lineno": e.lineno,
            "signature": e.signature,
        }

    def stats(self) -> dict:
        return {
            "total_entities": len(self.entities),
            "total_relations": len(self.relations),
            "by_type": {
                t.value: sum(1 for e in self.entities.values() if e.entity_type == t)
                for t in EntityType
            },
            "by_relation": {
                rk.value: sum(1 for r in self.relations if r.predicate == rk)
                for rk in RelationKind
            },
            "files_tracked": len(self.file_snapshots),
        }

    # ── Persistence ──

    def _load(self):
        if not os.path.exists(self.storage_path):
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    if record.get("type") == "entity":
                        self.entities[record["id"]] = CodeEntity(**record["data"])
                    elif record.get("type") == "relation":
                        rel = TypedRelation(
                            subject=record["subject"],
                            predicate=RelationKind(record["predicate"]),
                            object=record["object"],
                            weight=record.get("weight", 1.0),
                            metadata=record.get("metadata", {}),
                            created_at=record.get("created_at", time.time()),
                        )
                        idx = len(self.relations)
                        self.relations.append(rel)
                        self._adjacency[rel.subject].append(idx)
                        self._reverse_adj[rel.object].append(idx)
                    elif record.get("type") == "snapshot":
                        self.file_snapshots[record["file_path"]] = FileSnapshot(**record["data"])
        except Exception:
            pass

    def save(self):
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        with open(self.storage_path, "w", encoding="utf-8") as f:
            for eid, entity in self.entities.items():
                record = {
                    "type": "entity",
                    "id": eid,
                    "data": {
                        "id": entity.id,
                        "name": entity.name,
                        "entity_type": entity.entity_type.value,
                        "file_path": entity.file_path,
                        "lineno": entity.lineno,
                        "signature": entity.signature,
                        "docstring": entity.docstring,
                        "annotations": entity.annotations,
                        "properties": entity.properties,
                        "content_hash": entity.content_hash,
                    },
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            for rel in self.relations:
                record = {
                    "type": "relation",
                    "subject": rel.subject,
                    "predicate": rel.predicate.value,
                    "object": rel.object,
                    "weight": rel.weight,
                    "metadata": rel.metadata,
                    "created_at": rel.created_at,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            for fp, snap in self.file_snapshots.items():
                record = {
                    "type": "snapshot",
                    "file_path": fp,
                    "data": {
                        "file_path": snap.file_path,
                        "mtime": snap.mtime,
                        "content_hash": snap.content_hash,
                    },
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ─── Convenience API ─────────────────────────────────────────────────────────

def create_codebase_graph(storage_path: Optional[str] = None) -> CodebaseGraphStore:
    """创建代码库类型化图谱存储实例。"""
    return CodebaseGraphStore(storage_path)
