"""Trinity client - links, entities & relations mixin (split from client.py, 2026-08-17).

Part of the Trinity client package decomposition. Behavior identical to
the pre-split single-file implementation.
"""

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from trinity.telemetry import traced
class _GraphMixin:
    def create_link(
        self, source_id: str, target_id: str,
        link_type: str = "semantic", strength: float = 0.5,
    ) -> Dict[str, Any]:
        """创建记忆关联链接。

        Args:
            source_id: 源记忆 ID。
            target_id: 目标记忆 ID。
            link_type: 链接类型（co_occurrence/semantic/causal/same_task）。
            strength: 关联强度 0-1。

        Returns:
            创建结果。
        """
        if self._adapter and hasattr(self._adapter, "create_memory_link"):
            return self._adapter.create_memory_link(
                source_id, target_id, link_type, strength,
            )
        return {"error": "Adapter does not support memory links"}
    def get_links(self, memory_id: str) -> Dict[str, Any]:
        """获取某记忆的完整关联网络（含双向链接）。

        Args:
            memory_id: 记忆 ID。

        Returns:
            Dict with outgoing/incoming lists.
        """
        if self._adapter and hasattr(self._adapter, "get_all_links"):
            return self._adapter.get_all_links(memory_id)
        return {"outgoing": [], "incoming": []}
    def delete_link(self, link_id: str) -> bool:
        """删除指定链接。

        Args:
            link_id: 链接 ID。

        Returns:
            是否删除成功。
        """
        if self._adapter and hasattr(self._adapter, "delete_memory_link"):
            return self._adapter.delete_memory_link(link_id)
        return False
    def adjust_link_strength(
        self, link_id: str, action: str = "strengthen", delta: float = 0.1,
    ) -> Dict[str, Any]:
        """调整链接强度。

        Args:
            link_id: 链接 ID。
            action: 'strengthen' 或 'weaken'。
            delta: 调整幅度（默认 0.1）。

        Returns:
            操作结果。
        """
        if not self._adapter:
            return {"error": "No adapter"}
        if action == "strengthen" and hasattr(self._adapter, "strengthen_link"):
            return self._adapter.strengthen_link(link_id, delta)
        elif action == "weaken" and hasattr(self._adapter, "weaken_link"):
            return self._adapter.weaken_link(link_id, delta)
        return {"error": f"Invalid action: {action}"}
    def upsert_entity(self, name: str, etype: str = "concept",
                      properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建或更新实体。

        Args:
            name: 实体名称。
            etype: 类型 (person/project/file/agent/task/concept/tag)。
            properties: 附加属性 JSON。

        Returns:
            Dict with id/name/type/properties/created_at.
        """
        if self._adapter and hasattr(self._adapter, "upsert_entity"):
            return self._adapter.upsert_entity(name, etype, properties)
        return self.bridge("upsert_entity", name=name, etype=etype, properties=properties)
    def get_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """查询实体详情（含关联关系）。"""
        if self._adapter and hasattr(self._adapter, "get_entity"):
            return self._adapter.get_entity(entity_id)
        return self.bridge("get_entity", entity_id=entity_id)
    def search_entities(self, name: Optional[str] = None,
                        etype: Optional[str] = None,
                        limit: int = 20) -> List[Dict[str, Any]]:
        """搜索实体。"""
        if self._adapter and hasattr(self._adapter, "search_entities"):
            return self._adapter.search_entities(name, etype, limit)
        return self.bridge("search_entities", name=name, etype=etype, limit=limit)
    def create_relation(self, subject_id: str, predicate: str,
                        object_id: str,
                        properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建关系（幂等去重）。"""
        if self._adapter and hasattr(self._adapter, "create_relation"):
            return self._adapter.create_relation(subject_id, predicate, object_id, properties)
        return self.bridge("create_relation",
                           subject_id=subject_id, predicate=predicate,
                           object_id=object_id, properties=properties)
    def query_relations(self, subject_id: Optional[str] = None,
                        predicate: Optional[str] = None,
                        object_id: Optional[str] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        """查询关系。"""
        if self._adapter and hasattr(self._adapter, "query_relations"):
            return self._adapter.query_relations(subject_id, predicate, object_id, limit)
        return self.bridge("query_relations",
                           subject_id=subject_id, predicate=predicate,
                           object_id=object_id, limit=limit)
    def traverse(self, start_id: str,
                 max_hops: int = 3) -> Dict[str, Any]:
        """多跳遍历子图。

        Returns:
            Dict with nodes/edges.
        """
        if self._adapter and hasattr(self._adapter, "traverse"):
            return self._adapter.traverse(start_id, max_hops)
        return self.bridge("traverse", start_id=start_id, max_hops=max_hops)
    def explore_topic(self, topic_name: str) -> Dict[str, Any]:
        """以主题词为入口，自动搜索实体 + 遍历关系 + 聚合知识卡片。

        流程：
        1. 搜索实体（匹配 topic_name）
        2. 对每个实体 expand 1 跳关系
        3. 聚合关联记忆
        4. 返回结构化知识卡片

        Returns:
            Dict with entities/relations/related_memories/summary.
        """
        entities = self.search_entities(name=topic_name, limit=5)

        all_entities: Dict[str, Dict] = {}
        all_relations: List[Dict] = []
        for ent in entities:
            eid = ent["id"]
            all_entities[eid] = ent
            sub_graph = self.traverse(eid, max_hops=1)
            for n in sub_graph.get("nodes", []):
                if n.get("id"):
                    all_entities.setdefault(n["id"], n)
            for e in sub_graph.get("edges", []):
                if e not in all_relations:
                    all_relations.append(e)

        # 聚合关联记忆
        related_memories: List[Dict] = []
        if self._adapter and hasattr(self._adapter, "search_memories"):
            for eid in all_entities:
                ent_data = all_entities.get(eid, {})
                ent_name = ent_data.get("name", "")
                if ent_name:
                    mems = self._adapter.search_memories(
                        query=ent_name, top_k=3,
                        persona_id=None, tenant_id=None, agent_id=None,
                    )
                    for m in mems:
                        mid = m.get("memory_id", "")
                        if mid and not any(
                            x.get("memory_id") == mid for x in related_memories
                        ):
                            related_memories.append(m)

        return {
            "topic": topic_name,
            "entities": list(all_entities.values()),
            "relations": all_relations,
            "related_memories": related_memories[:10],
            "summary": (
                f"知识图谱: {topic_name} — "
                f"{len(all_entities)} 个实体, "
                f"{len(all_relations)} 条关系, "
                f"{len(related_memories[:10])} 条关联记忆"
            ),
        }
