"""SQLite adapter - links, entities, relations mixin (split from sqlite.py, 2026-08-17).

Part of the SQLiteAdapter package decomposition. Behavior identical to the
pre-split single-file implementation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import functools
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...security.crypto import get_storage_cipher, StorageCipher  # type: ignore[attr-defined]
from .._util import _safe_write

logger = logging.getLogger("trinity.adapters.sqlite")


class _GraphMixin:
    def create_memory_link(self, source_id: str, target_id: str,
                           link_type: str = "semantic",
                           strength: float = 0.5) -> Dict[str, Any]:
        """创建记忆关联链接。

        Args:
            source_id: 源记忆 ID。
            target_id: 目标记忆 ID。
            link_type: 链接类型（co_occurrence/semantic/causal/same_task）。
            strength: 关联强度 0-1。

        Returns:
            创建结果。
        """
        with self._write_lock:

            conn = self._conn
            if not conn:
                return {"error": "Not connected"}
            if source_id == target_id:
                return {"error": "Cannot link memory with itself"}
            link_id = hashlib.sha256(
                f"{source_id}:{target_id}:{link_type}".encode()
            ).hexdigest()[:32]
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("""
                INSERT OR IGNORE INTO memory_links
                    (id, source_id, target_id, link_type, strength, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (link_id, source_id, target_id, link_type, strength, now))
            conn.commit()
            return {
                "id": link_id, "source_id": source_id, "target_id": target_id,
                "link_type": link_type, "strength": strength, "created_at": now,
            }
    def get_linked_memories(self, memory_id: str,
                            min_strength: float = 0.0) -> List[Dict[str, Any]]:
        """获取与指定记忆关联的所有链接（按强度降序）。

        Args:
            memory_id: 记忆 ID。
            min_strength: 最低关联强度阈值。

        Returns:
            链接列表。
        """
        conn = self._conn
        if not conn:
            return []
        cursor = conn.execute("""
            SELECT ml.*, m.content AS target_content
            FROM memory_links ml
            LEFT JOIN memories m ON m.memory_id = ml.target_id
            WHERE ml.source_id = ?
              AND ml.strength >= ?
            ORDER BY ml.strength DESC
        """, (memory_id, min_strength))
        return [dict(row) for row in cursor.fetchall()]
    def strengthen_link(self, link_id: str,
                        increment: float = 0.1) -> Dict[str, Any]:
        """增强链接强度（上限 1.0）。

        Args:
            link_id: 链接 ID。
            increment: 增量值。

        Returns:
            操作结果。
        """
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}
        conn.execute("""
            UPDATE memory_links
            SET strength = MIN(strength + ?, 1.0)
            WHERE id = ?
        """, (increment, link_id))
        conn.commit()
        cursor = conn.execute(
            "SELECT * FROM memory_links WHERE id = ?", (link_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else {"error": "Link not found"}
    def weaken_link(self, link_id: str,
                    decrement: float = 0.1) -> Dict[str, Any]:
        """削弱链接强度（下限 0.0）。

        Args:
            link_id: 链接 ID。
            decrement: 减量值。

        Returns:
            操作结果。
        """
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}
        conn.execute("""
            UPDATE memory_links
            SET strength = MAX(strength - ?, 0.0)
            WHERE id = ?
        """, (decrement, link_id))
        conn.commit()
        cursor = conn.execute(
            "SELECT * FROM memory_links WHERE id = ?", (link_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else {"error": "Link not found"}
    def delete_memory_link(self, link_id: str) -> bool:
        """删除指定链接。

        Args:
            link_id: 链接 ID。

        Returns:
            是否删除成功。
        """
        conn = self._conn
        if not conn:
            return False
        cursor = conn.execute(
            "DELETE FROM memory_links WHERE id = ?", (link_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
    def get_all_links(self, memory_id: str) -> List[Dict[str, Any]]:
        """获取某记忆和其所有关联链接和反向链接。

        Args:
            memory_id: 记忆 ID。

        Returns:
            包含 outgoing/incoming 的字典。
        """
        conn = self._conn
        if not conn:
            return {"outgoing": [], "incoming": []}
        outgoing = conn.execute(
            "SELECT * FROM memory_links WHERE source_id = ?", (memory_id,)
        ).fetchall()
        incoming = conn.execute(
            "SELECT * FROM memory_links WHERE target_id = ?", (memory_id,)
        ).fetchall()
        return {
            "outgoing": [dict(r) for r in outgoing],
            "incoming": [dict(r) for r in incoming],
        }
    @staticmethod
    def _parse_entity_properties(summary: Optional[str]) -> Dict[str, Any]:
        """安全解析 entities.summary：合法 JSON 视为 properties，否则视为摘要文本。"""
        if not summary:
            return {}
        try:
            parsed = json.loads(summary)
            if isinstance(parsed, dict):
                return parsed
            return {"summary": summary}
        except (ValueError, TypeError):
            return {"summary": summary}
    def upsert_entity(self, name: str, etype: str = "concept",
                      properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建或更新实体（幂等：按 name + type 去重）。

        Args:
            name: 实体名称。
            etype: 实体类型（person/project/file/agent/task/concept/tag）。
            properties: 附加属性 JSON。

        Returns:
            实体字典。
        """
        with self._write_lock:

            conn = self._conn
            if not conn:
                return {"error": "Not connected"}
            props_json = json.dumps(properties or {}, ensure_ascii=False)
            now = datetime.now(timezone.utc).isoformat()

            # 按 name + type 查找已有（entities 表主键为 entity_id）
            cursor = conn.execute(
                "SELECT entity_id FROM entities WHERE name = ? AND type = ?", (name, etype)
            )
            row = cursor.fetchone()
            if row:
                entity_id = row["entity_id"]
                conn.execute(
                    "UPDATE entities SET summary = ?, first_seen = ? WHERE entity_id = ?",
                    (props_json, now, entity_id),
                )
            else:
                entity_id = f"ent_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    "INSERT INTO entities (entity_id, name, type, summary, first_seen) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (entity_id, name, etype, props_json, now),
                )
            conn.commit()
            return {"id": entity_id, "name": name, "type": etype,
                    "properties": (properties or {}), "created_at": now}
    def get_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """查询实体详情（含关联记忆与关系）。

        Args:
            entity_id: 实体 ID。

        Returns:
            实体详情字典，无则 None。
        """
        conn = self._conn
        if not conn:
            return None
        cursor = conn.execute("SELECT * FROM entities WHERE entity_id = ?", (entity_id,))
        row = cursor.fetchone()
        if not row:
            return None
        entity = dict(row)
        entity.pop("embedding", None)
        entity["id"] = entity.get("entity_id")
        entity["properties"] = self._parse_entity_properties(entity.get("summary"))
        entity["created_at"] = entity.get("first_seen")

        # 关联关系
        rel_out = conn.execute(
            "SELECT * FROM relations WHERE subject_id = ?", (entity_id,)
        ).fetchall()
        rel_in = conn.execute(
            "SELECT * FROM relations WHERE object_id = ?", (entity_id,)
        ).fetchall()
        entity["relations_outgoing"] = [dict(r) for r in rel_out]
        entity["relations_incoming"] = [dict(r) for r in rel_in]
        return entity
    def search_entities(self, name: Optional[str] = None,
                        etype: Optional[str] = None,
                        limit: int = 20) -> List[Dict[str, Any]]:
        """搜索实体（按名称模糊 + 类型精确）。

        Args:
            name: 名称关键词（LIKE 模糊匹配，可选）。
            etype: 实体类型精确过滤（可选）。
            limit: 返回数量。

        Returns:
            实体列表。

        2026-08-15（压测修复 v2）：线程本地只读连接（纯读，无锁）。
        """
        conn = self._get_read_conn()
        if not conn:
            return []
        sql = "SELECT * FROM entities WHERE 1=1"
        params: list = []
        if name:
            sql += " AND name LIKE ?"
            params.append(f"%{name}%")
        if etype:
            sql += " AND type = ?"
            params.append(etype)
        sql += " ORDER BY first_seen DESC LIMIT ?"
        params.append(limit)
        cursor = conn.execute(sql, params)
        results = []
        for row in cursor.fetchall():
            d = dict(row)
            d.pop("embedding", None)
            d["id"] = d.get("entity_id")
            d["properties"] = self._parse_entity_properties(d.get("summary"))
            d["created_at"] = d.get("first_seen")
            results.append(d)
        return results
    def create_relation(self, subject_id: str, predicate: str,
                        object_id: str,
                        properties: Optional[Dict] = None,
                        valid_from: Optional[str] = None,
                        valid_to: Optional[str] = None) -> Dict[str, Any]:
        """创建关系（幂等：按 subject+predicate+object 去重）。

        Args:
            subject_id: 主体实体 ID。
            predicate: 谓词。
            object_id: 客体实体 ID。
            properties: 附加属性 JSON。
            valid_from: 边生效起始时间（ISO8601，缺省=now；edge bi-temporal）。
            valid_to: 边失效时间（ISO8601，缺省 None=仍有效）。

        Returns:
            关系字典。
        """
        with self._write_lock:

            conn = self._conn
            if not conn:
                return {"error": "Not connected"}
            if subject_id == object_id:
                return {"error": "Cannot create self-referencing relation"}
            props_json = json.dumps(properties or {}, ensure_ascii=False)
            now = datetime.now(timezone.utc).isoformat()
            rel_id = hashlib.sha256(
                f"{subject_id}:{predicate}:{object_id}".encode()
            ).hexdigest()[:32]
            conn.execute("""
                INSERT OR IGNORE INTO relations
                    (id, subject_id, predicate, object_id, properties, created_at,
                     valid_from, valid_to)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (rel_id, subject_id, predicate, object_id, props_json, now,
                  valid_from or now, valid_to))
            conn.commit()
            return {"id": rel_id, "subject_id": subject_id, "predicate": predicate,
                    "object_id": object_id, "properties": (properties or {}),
                    "created_at": now, "valid_from": valid_from or now,
                    "valid_to": valid_to}
    def query_relations(self, subject_id: Optional[str] = None,
                        predicate: Optional[str] = None,
                        object_id: Optional[str] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        """查询关系（任意组合过滤条件）。

        Args:
            subject_id: 主体 ID（可选）。
            predicate: 谓词（可选）。
            object_id: 客体 ID（可选）。
            limit: 返回数量。

        Returns:
            关系列表。

        2026-08-15（压测修复 v2）：线程本地只读连接（纯读，无锁）。
        """
        conn = self._get_read_conn()
        if not conn:
            return []
        sql = "SELECT * FROM relations WHERE 1=1"
        params: list = []
        if subject_id:
            sql += " AND subject_id = ?"
            params.append(subject_id)
        if predicate:
            sql += " AND predicate = ?"
            params.append(predicate)
        if object_id:
            sql += " AND object_id = ?"
            params.append(object_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]
    def query_relations_at(self, at_time: str,
                           subject_id: Optional[str] = None,
                           predicate: Optional[str] = None,
                           object_id: Optional[str] = None,
                           limit: int = 50) -> List[Dict[str, Any]]:
        """时点查询：返回指定时间点有效的边（edge bi-temporal）。

        valid_from <= at_time AND (valid_to IS NULL OR valid_to > at_time)。

        Args:
            at_time: 查询时间点（ISO8601）。
            subject_id / predicate / object_id: 可选过滤。
            limit: 返回数量。

        Returns:
            该时点有效的关系列表。
        """
        conn = self._conn
        if not conn:
            return []
        sql = ("SELECT * FROM relations WHERE valid_from <= ? "
               "AND (valid_to IS NULL OR valid_to > ?)")
        params: list = [at_time, at_time]
        if subject_id:
            sql += " AND subject_id = ?"
            params.append(subject_id)
        if predicate:
            sql += " AND predicate = ?"
            params.append(predicate)
        if object_id:
            sql += " AND object_id = ?"
            params.append(object_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]
    def traverse(self, start_id: str,
                 max_hops: int = 3) -> Dict[str, Any]:
        """多跳遍历，返回以 start_id 为起点的子图。

        Args:
            start_id: 起始实体 ID。
            max_hops: 最大跳数（1-5，默认 3）。

        Returns:
            {"nodes": [...], "edges": [...]}

        2026-08-15（压测修复 v2）：线程本地只读连接（纯读，无锁；
        query_graph 内调用，每线程独立连接天然安全）。
        """
        conn = self._get_read_conn()
        if not conn:
            return {"nodes": [], "edges": []}
        max_hops = max(1, min(max_hops, 5))

        visited: set = set()
        node_ids: set = {start_id}
        edges: list = []

        for hop in range(max_hops):
            if not node_ids:
                break
            visited |= node_ids
            next_ids: set = set()
            for nid in node_ids:
                for direction in ("subject", "object"):
                    col = f"{direction}_id"
                    cursor = conn.execute(
                        f"SELECT * FROM relations WHERE {col} = ?", (nid,)
                    )
                    for row in cursor.fetchall():
                        r = dict(row)
                        other = r["object_id"] if direction == "subject" else r["subject_id"]
                        edges.append(r)
                        if other not in visited:
                            next_ids.add(other)
            node_ids = next_ids - visited

        all_nodes = set()
        for e in edges:
            all_nodes.add(e["subject_id"])
            all_nodes.add(e["object_id"])
        all_nodes.add(start_id)

        # 批量查询实体
        node_list: list = []
        for nid in all_nodes:
            cursor = conn.execute("SELECT * FROM entities WHERE entity_id = ?", (nid,))
            row = cursor.fetchone()
            if row:
                n = dict(row)
                n.pop("embedding", None)
                n["id"] = n.get("entity_id")
                n["properties"] = self._parse_entity_properties(n.get("summary"))
                n["created_at"] = n.get("first_seen")
                node_list.append(n)

        return {"nodes": node_list, "edges": edges}
    @_safe_write
    def create_entity(self, name: str, etype: str = "concept",
                      properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建新实体（非幂等，实体已存在时返回错误）。

        Args:
            name: 实体名称。
            etype: 实体类型。
            properties: 附加属性 JSON。

        Returns:
            实体字典；若实体已存在则返回 {"error": "Entity exists", "entity_id": "..."}。
        """
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}
        cursor = conn.execute(
            "SELECT entity_id FROM entities WHERE name = ? AND type = ?", (name, etype)
        )
        row = cursor.fetchone()
        if row:
            return {"error": "Entity exists", "entity_id": row["entity_id"]}
        return self.upsert_entity(name=name, etype=etype, properties=properties)
    def get_entity_by_name(self, name: str,
                           etype: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """按名称精确匹配单个实体。

        Args:
            name: 实体名称（精确匹配）。
            etype: 实体类型过滤（可选）。

        Returns:
            实体详情字典，无则 None。
        """
        conn = self._conn
        if not conn:
            return None
        sql = "SELECT entity_id FROM entities WHERE name = ?"
        params: list = [name]
        if etype:
            sql += " AND type = ?"
            params.append(etype)
        sql += " LIMIT 1"
        cursor = conn.execute(sql, params)
        row = cursor.fetchone()
        if not row:
            return None
        return self.get_entity(row["entity_id"])
    def get_neighbors(self, entity_id: str) -> Dict[str, Any]:
        """获取实体的 1-hop 邻居。

        Args:
            entity_id: 实体 ID。

        Returns:
            {"entity": ..., "neighbors": [实体列表], "relations": [关系列表]}
        """
        subgraph = self.traverse(entity_id, max_hops=1)
        entity = self.get_entity(entity_id)
        neighbors = []
        nodes_seen = {entity_id}
        if entity:
            entity.pop("relations_outgoing", None)
            entity.pop("relations_incoming", None)
        for node in subgraph.get("nodes", []):
            nid = node.get("entity_id", "") or node.get("id", "")
            if nid != entity_id and nid not in nodes_seen:
                nodes_seen.add(nid)
                neighbors.append(node)
        return {
            "entity": entity,
            "neighbors": neighbors,
            "relations": subgraph.get("edges", []),
        }
    def query_graph(self, query: str,
                    limit: int = 20) -> Dict[str, Any]:
        """通过关键词搜索实体，返回以匹配实体为中心的子图。

        Args:
            query: 实体名称关键词。
            limit: 匹配实体数量上限。

        Returns:
            {"match_entities": [...], "nodes": [...], "edges": [...]}
            所有匹配实体及其 1-hop 邻居合并去重的完整子图。

        2026-08-15（压测修复 v2）：线程本地只读连接（纯读，无锁；
        内部 traverse/search_entities 各取本线程读连接，安全）。
        """
        matches = self.search_entities(name=query, limit=limit)
        if not matches:
            return {"match_entities": [], "nodes": [], "edges": []}

        all_nodes: dict = {}
        all_edges: dict = {}
        match_entities = []

        for ent in matches:
            ent_copy = dict(ent)
            ent_copy.pop("relations_outgoing", None)
            ent_copy.pop("relations_incoming", None)
            match_entities.append(ent_copy)
            eid = ent.get("entity_id") or ent.get("id")
            all_nodes[eid] = ent_copy

            sub = self.traverse(eid, max_hops=1)
            for node in sub.get("nodes", []):
                nid = node.get("entity_id", "") or node.get("id", "")
                if nid not in all_nodes:
                    all_nodes[nid] = node
            for edge in sub.get("edges", []):
                eid2 = edge.get("id", "")
                if eid2 not in all_edges:
                    all_edges[eid2] = edge

        return {
            "match_entities": match_entities,
            "nodes": list(all_nodes.values()),
            "edges": list(all_edges.values()),
        }
