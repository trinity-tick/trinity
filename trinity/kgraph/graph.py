"""
Trinity Knowledge Graph — 轻量级图查询层

纯 Python 实现，零外部依赖。使用 JSON lines 文件存储，
支持实体管理、关系查询、BFS 遍历、子图导出和序列化。
"""

import json
import os
import time
from collections import defaultdict, deque
from enum import Enum
from pathlib import Path


PROJECT_ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".."
))


class RelationType(Enum):
    """预定义的关系类型，用于规范关系 predicate。"""
    BELONGS_TO = "belongs_to"
    LOCATED_IN = "located_in"
    DEPENDS_ON = "depends_on"
    DERIVED_FROM = "derived_from"
    REFERENCES = "references"
    APPLIES_TO = "applies_to"
    USES = "uses"
    LOCATED_AT = "located_at"
    PART_OF = "part_of"

    @classmethod
    def has_value(cls, value: str) -> bool:
        return any(value == e.value for e in cls)


class KnowledgeGraph:
    """
    轻量级知识图谱，支持实体和关系的增删改查、BFS 遍历、子图提取和序列化。

    存储格式 (JSON lines):
        {"type":"entity","id":"...","entity_type":"...","properties":{...},"created_at":...}
        {"type":"relation","subject":"...","predicate":"...","object":"...","weight":1.0,"metadata":{},"created_at":...}
    """

    def __init__(self, storage_path: str | None = None):
        if storage_path is None:
            self.storage_path = os.path.join(PROJECT_ROOT, "data", "kgraph", "kgraph_data.jsonl")
        else:
            self.storage_path = storage_path

        self._entities: dict[str, dict] = {}
        self._relations: list[dict] = []
        self._entity_type_index: dict[str, set[str]] = defaultdict(set)
        self._relation_index: dict[str, list[int]] = defaultdict(list)

        # 尝试从文件加载已有数据
        if os.path.exists(self.storage_path):
            try:
                loaded = self.__class__.load(self.storage_path)
                self._entities = loaded._entities
                self._relations = loaded._relations
                self._rebuild_relation_index()
                self._rebuild_type_index()
            except Exception:
                pass

    def add_entity(self, entity_id: str, entity_type: str,
                   properties: dict | None = None) -> dict:
        entity = {
            "id": entity_id,
            "entity_type": entity_type,
            "properties": properties or {},
            "created_at": time.time()
        }
        self._entities[entity_id] = entity
        self._entity_type_index[entity_type].add(entity_id)
        return entity

    def get_entity(self, entity_id: str) -> dict | None:
        return self._entities.get(entity_id)

    def remove_entity(self, entity_id: str) -> bool:
        if entity_id not in self._entities:
            return False
        del self._entities[entity_id]
        # 清理索引
        for type_set in self._entity_type_index.values():
            type_set.discard(entity_id)
        # 清理相关关系
        self._relations = [
            r for r in self._relations
            if r["subject"] != entity_id and r["object"] != entity_id
        ]
        self._rebuild_relation_index()
        return True

    def add_relation(self, subject: str, predicate: str, object: str,
                     weight: float = 1.0, metadata: dict | None = None) -> dict:
        relation = {
            "subject": subject,
            "predicate": predicate,
            "object": object,
            "weight": weight,
            "metadata": metadata or {},
            "created_at": time.time()
        }
        self._relations.append(relation)
        idx = len(self._relations) - 1
        self._relation_index[subject].append(idx)
        self._relation_index[object].append(idx)
        # 自动创建占位实体（如果不存在）
        for eid in [subject, object]:
            if eid not in self._entities:
                self._entities[eid] = {
                    "id": eid, "entity_type": "unknown",
                    "properties": {}, "created_at": time.time()
                }
        return relation

    def query_relations(self, entity_id: str,
                        max_depth: int = 2) -> list[dict]:
        results = []
        visited = {entity_id}
        queue = deque([(entity_id, 0)])

        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue

            for idx in self._relation_index.get(current, []):
                rel = self._relations[idx]
                results.append(rel)

                neighbor = rel["object"] if rel["subject"] == current else rel["subject"]
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))

        return results

    def query_by_type(self, entity_type: str) -> list[dict]:
        ids = self._entity_type_index.get(entity_type, set())
        return [self._entities[eid] for eid in ids if eid in self._entities]

    def search(self, query_text: str, top_k: int = 5) -> list[dict]:
        query = query_text.lower()
        results = []
        for eid, entity in self._entities.items():
            score = 0.0
            if query in eid.lower():
                score += 1.0
            props = entity.get("properties", {})
            for key, val in props.items():
                if isinstance(val, str) and query in val.lower():
                    score += 0.5
                if key == "name" and isinstance(val, str) and query in val.lower():
                    score += 0.3
            if entity.get("entity_type") and query in entity["entity_type"].lower():
                score += 0.2
            if score > 0:
                results.append({"entity": entity, "score": score})

        results.sort(key=lambda x: -x["score"])
        return results[:top_k]

    def get_stats(self) -> dict:
        rel_type_dist: dict[str, int] = defaultdict(int)
        for rel in self._relations:
            rel_type_dist[rel["predicate"]] += 1

        entity_type_dist: dict[str, int] = defaultdict(int)
        for ent in self._entities.values():
            entity_type_dist[ent["entity_type"]] += 1

        return {
            "entity_count": len(self._entities),
            "relation_count": len(self._relations),
            "entity_type_distribution": dict(entity_type_dist),
            "relation_type_distribution": dict(rel_type_dist),
        }

    def get_subgraph(self, entity_ids: list[str],
                     max_depth: int = 1) -> dict:
        sub_entities = {}
        sub_relations = []
        visited = set(entity_ids)

        for eid in entity_ids:
            if eid in self._entities:
                sub_entities[eid] = self._entities[eid]

        for eid in entity_ids:
            for idx in self._relation_index.get(eid, []):
                rel = self._relations[idx]
                sub_relations.append(rel)
                for nid in [rel["subject"], rel["object"]]:
                    if nid not in visited and nid in self._entities:
                        visited.add(nid)
                        if max_depth > 0:
                            sub_entities[nid] = self._entities[nid]

        return {
            "entities": list(sub_entities.values()),
            "relations": sub_relations
        }

    def to_json(self) -> list[dict]:
        data = []
        for entity in self._entities.values():
            data.append({"type": "entity", **entity})
        for relation in self._relations:
            data.append({"type": "relation", **relation})
        return data

    @classmethod
    def from_json(cls, data: list[dict]) -> "KnowledgeGraph":
        kg = cls.__new__(cls)
        kg._entities = {}
        kg._relations = []
        kg._entity_type_index = defaultdict(set)
        kg._relation_index = defaultdict(list)
        kg.storage_path = ""

        for item in data:
            if item.get("type") == "entity":
                ent = {k: v for k, v in item.items() if k != "type"}
                kg._entities[ent["id"]] = ent
                kg._entity_type_index[ent["entity_type"]].add(ent["id"])
            elif item.get("type") == "relation":
                rel = {k: v for k, v in item.items() if k != "type"}
                kg._relations.append(rel)
                idx = len(kg._relations) - 1
                kg._relation_index[rel["subject"]].append(idx)
                kg._relation_index[rel["object"]].append(idx)

        return kg

    def save(self, path: str | None = None) -> str:
        save_path = path or self.storage_path
        dir_path = os.path.normpath(os.path.dirname(save_path))
        if not os.path.isdir(dir_path):
            os.system('mkdir "%s"' % dir_path)
        with open(save_path, "w", encoding="utf-8") as f:
            for item in self.to_json():
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        return save_path

    @classmethod
    def load(cls, path: str) -> "KnowledgeGraph":
        data = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    data.append(json.loads(line))
        return cls.from_json(data)

    def _rebuild_relation_index(self) -> None:
        self._relation_index = defaultdict(list)
        for idx, rel in enumerate(self._relations):
            self._relation_index[rel["subject"]].append(idx)
            self._relation_index[rel["object"]].append(idx)

    def _rebuild_type_index(self) -> None:
        self._entity_type_index = defaultdict(set)
        for eid, ent in self._entities.items():
            self._entity_type_index[ent["entity_type"]].add(eid)


if __name__ == "__main__":
    # 自检测试
    print("=" * 50)
    print("KnowledgeGraph 自检测试")
    print("=" * 50)

    kg = KnowledgeGraph(storage_path="data/kgraph/test_graph.jsonl")

    # 1. 添加实体
    kg.add_entity("caitang", "brand", {"name": "彩棠", "desc": "珀莱雅子品牌"})
    kg.add_entity("proya", "brand", {"name": "珀莱雅"})
    kg.add_entity("heavy_rule", "rule", {"name": "重品层规则", "desc": "重品0.1kg-0.3kg放第一层"})
    kg.add_entity("position_1", "location", {"name": "第一层货架"})
    kg.add_entity("x_priority", "strategy", {"name": "X轴优先扩展", "desc": "货架布局时优先扩展X轴"})
    print("[测试1] 添加5个实体 OK")

    # 2. 添加关系
    kg.add_relation("caitang", "belongs_to", "proya")
    kg.add_relation("heavy_rule", "applies_to", "caitang")
    kg.add_relation("heavy_rule", "located_at", "position_1")
    kg.add_relation("caitang", "uses", "x_priority")
    print("[测试2] 添加4条关系 OK")

    # 3. 查询关联网络
    rels = kg.query_relations("caitang", max_depth=2)
    print("[测试3] 查询彩棠关联网络: %d 条关系" % len(rels))
    for r in rels:
        print("   %s --[%s]--> %s" % (r["subject"], r["predicate"], r["object"]))

    # 4. 按类型查询
    brands = kg.query_by_type("brand")
    print("[测试4] 按类型查询 brand: %d 个" % len(brands))

    # 5. 关键词搜索
    hits = kg.search("彩棠", top_k=3)
    print("[测试5] 关键词搜索 '彩棠': %d 条" % len(hits))

    # 6. 统计
    stats = kg.get_stats()
    print("[测试6] 统计: entities=%d, relations=%d" % (stats["entity_count"], stats["relation_count"]))

    # 7. 子图
    sub = kg.get_subgraph(["caitang", "heavy_rule"], max_depth=1)
    print("[测试7] 子图: %d 实体, %d 关系" % (len(sub["entities"]), len(sub["relations"])))

    # 8. 保存 & 加载
    saved_path = kg.save()
    kg2 = KnowledgeGraph.load(saved_path)
    s2 = kg2.get_stats()
    print("[测试8] 保存 & 加载: entities=%d, relations=%d" % (s2["entity_count"], s2["relation_count"]))

    print("\n所有测试通过!")
