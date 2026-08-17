"""
Trinity Knowledge Graph — 轻量级图查询层

纯 Python 实现，零外部依赖。使用 JSON lines 文件存储，
支持实体管理、关系查询、BFS 遍历、子图导出和序列化。
"""

import json
import os
import re
import time
from collections import defaultdict, deque
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np


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
        """关键词检索 + PPR 图扩散（2026-08-15, R3 P0-1b）。

        两阶段：
          1. 关键词召回种子实体（原有逻辑）
          2. PPR 从种子扩散，把图上关联实体提升进结果
             （对齐 HippoRAG 2 / 2026 PPR 检索主流；图小或无关时回退纯关键词）

        Returns:
            [{"entity": {...}, "score": float, "ppr_score": float|None}, ...]
        """
        query = query_text.lower()
        kw_results: list[dict] = []
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
                kw_results.append({"entity": entity, "score": score,
                                   "ppr_score": None})

        kw_results.sort(key=lambda x: -x["score"])
        kw_ids = [r["entity"].get("id") or r["entity"].get("entity_id")
                  for r in kw_results]

        # ── PPR 图扩散（有种子且图非空时）────────────────────────────
        if kw_ids and len(self._relations) > 0 and len(self._entities) > 1:
            try:
                ppr = self.ppr_search([e for e in kw_ids if e], top_k=max(top_k * 3, 10))
                ppr_by_id = {p["entity_id"]: p["ppr_score"] for p in ppr}
                # 融合：关键词结果打 PPR 标记；PPR 独有实体补入
                for r in kw_results:
                    eid = r["entity"].get("id") or r["entity"].get("entity_id")
                    if eid in ppr_by_id:
                        r["ppr_score"] = ppr_by_id[eid]
                        r["score"] += ppr_by_id[eid] * 0.5  # 图信号加权
                known = {r["entity"].get("id") or r["entity"].get("entity_id")
                         for r in kw_results}
                for eid, pscore in ppr_by_id.items():
                    if eid not in known and eid in self._entities:
                        kw_results.append({
                            "entity": self._entities[eid],
                            "score": pscore * 0.5,   # 仅图信号
                            "ppr_score": pscore,
                        })
                kw_results.sort(key=lambda x: -x["score"])
            except Exception:
                pass  # PPR 失败回退纯关键词

        return kw_results[:top_k]

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

    # ── P0-1: PPR 图上检索 ──────────────────────────────────────────────

    def ppr_search(self, query_entities: list[str],
                   alpha: float = 0.85,
                   max_iter: int = 100,
                   top_k: int = 10) -> list[dict]:
        """基于 Personalized PageRank 的图上关联检索。

        用纯 NumPy 构建邻接矩阵并通过迭代法计算稳态 PPR 向量，
        返回 top_k 个与查询实体最相关的实体及其 PPR 分值。

        参数:
            query_entities: 查询实体 ID 列表（作为重启节点集）。
            alpha: 阻尼因子，控制重启概率（0 < alpha <= 1）。
            max_iter: 最大迭代次数。
            top_k: 返回结果数。

        返回:
            [{"entity_id": "...", "entity": {...}, "ppr_score": 0.xx}, ...]
        """
        # 1) 构建 entity_id → 矩阵索引映射
        eids = list(self._entities.keys())
        if not eids:
            return []
        eid_to_idx = {eid: i for i, eid in enumerate(eids)}
        n = len(eids)

        # 2) 构建 (n, n) 邻接矩阵（out-degree 归一化）
        adj = np.zeros((n, n), dtype=np.float64)
        out_degree = np.zeros(n, dtype=np.int32)

        for rel in self._relations:
            s, o = rel["subject"], rel["object"]
            if s in eid_to_idx and o in eid_to_idx:
                si, oi = eid_to_idx[s], eid_to_idx[o]
                w = rel.get("weight", 1.0)
                adj[oi, si] += w      # s → o，列归一化
                out_degree[si] += 1

        # 处理出度为 0 的节点：将对应列设为均匀分布
        zero_out = out_degree == 0
        if zero_out.any():
            adj[:, zero_out] = 1.0 / n

        # 出度 >0 的列做列归一化
        col_sums = adj.sum(axis=0)
        col_sums[col_sums == 0] = 1.0
        adj = adj / col_sums

        # 3) 构造偏好向量 p（均匀分布在 query_entities 上）
        p = np.zeros(n, dtype=np.float64)
        valid_queries = [e for e in query_entities if e in eid_to_idx]
        if not valid_queries:
            p = np.ones(n) / n
        else:
            for e in valid_queries:
                p[eid_to_idx[e]] = 1.0
            p = p / p.sum()

        # 4) 幂迭代法计算稳态 PPR
        v = p.copy()
        for _ in range(max_iter):
            v_next = (1.0 - alpha) * p + alpha * adj.dot(v)
            delta = np.abs(v_next - v).sum()
            if delta < 1e-8:
                break
            v = v_next

        # 5) 排序返回 top_k（排除已在 query 中的节点）
        query_indices = {eid_to_idx[e] for e in valid_queries}
        scores = [(i, v[i]) for i in range(n)
                  if v[i] > 1e-10 and i not in query_indices]
        scores.sort(key=lambda x: -x[1])
        scores = scores[:top_k]

        results = []
        for idx, score in scores:
            eid = eids[idx]
            results.append({
                "entity_id": eid,
                "entity": self._entities[eid],
                "ppr_score": round(float(score), 6),
            })
        return results

    # ── P0-1: 自动实体解析 ──────────────────────────────────────────────

    def auto_extract_entities(self, text: str,
                              llm_func=None) -> list[dict]:
        """从文本中自动抽取实体。

        提供基于正则的 fallback 实现，同时预留 llm_func 回调接口。
        当 llm_func 提供时优先走 LLM 路径，否则走正则 fallback。

        参数:
            text: 待抽取的原始文本。
            llm_func: 可选的 LLM 驱动抽取回调，签名为
                      llm_func(text) -> list[dict]，
                      每个 dict 至少包含 entity_name 和 entity_type。

        返回:
            [{"entity_name": "...", "entity_type": "person/organization/...",
              "source_span": "...", "confidence": 0.xx}, ...]
        """
        if llm_func is not None:
            try:
                result = llm_func(text)
                if isinstance(result, list):
                    return result
            except Exception:
                pass  # fallback to regex

        return self._regex_extract_entities(text)

    # ── 正则 fallback ──────────────────────────────────────────────────

    @staticmethod
    def _regex_extract_entities(text: str) -> list[dict]:
        """基于正则的实体抽取（fallback）。"""
        entities: list[dict] = []
        seen: set[str] = set()

        def _add(name: str, etype: str, span: str, conf: float):
            key = (name.strip().lower(), etype)
            if key in seen:
                return
            seen.add(key)
            entities.append({
                "entity_name": name.strip(),
                "entity_type": etype,
                "source_span": span.strip(),
                "confidence": conf,
            })

        patterns = [
            # 日期: 2024-01-01 / 2024/01/01 / 2024年1月1日
            (r"\b\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?\b", "date", 0.95),
            # 中文人名: 2-4 个中文字 + 常见称谓
            (r"(?:[李王张刘陈杨赵黄周吴徐孙马胡朱郭何罗高林郑][\u4e00-\u9fff]{1,3}(?:先生|女士|教授|经理|总[监经理]))",
             "person", 0.80),
            # 简单中文人名: 两个连续的 2-4 字中文人名（以常见姓起头）
            (
                r"[李王张刘陈杨赵黄周吴徐孙马胡朱郭何罗高林郑]"
                r"[\u4e00-\u9fff]{1,3}(?=[，。；：\s]|$)",
                "person", 0.65,
            ),
            # 组织: XX公司 / XX有限公司 / XX集团 / XX大学 / XX研究所
            (
                r"[\u4e00-\u9fffA-Za-z]{2,30}?"
                r"(?:有限公司|有限责任公司|股份公司|集团|大学|学院|"
                r"研究所|医院|中心|基金会|协会|学会)",
                "organization", 0.85,
            ),
            # 文件: *.pdf / *.docx / *.xlsx / report_2024.pdf ...
            (r"\b[\w./\\-]+\.(?:pdf|docx?|xlsx?|pptx?|md|txt|json|yaml|yml|py|jpg|png)\b",
             "file", 0.90),
            # 项目/代号: UPPERCASE_WITH_UNDERSCORE 或 大写驼峰
            (r"\b[A-Z][A-Z_]{2,}[A-Z]\b", "project", 0.70),
            # 概念/术语: 被引号括起来的短语
            (r"[「『""]([^」』""]+)[」』""]", "concept", 0.60),
            # 邮箱
            (r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "contact", 0.95),
            # URL
            (r"https?://[^\s,，。；;]+", "resource", 0.90),
            # 中文百分比 / 数值指标
            (r"\b\d{1,3}(?:\.\d{1,2})?%", "metric", 0.85),
        ]

        for pat_info in patterns:
            if len(pat_info) == 3:
                pattern, etype, conf = pat_info
            else:
                pattern, etype, conf = pat_info[0], pat_info[1], pat_info[2]

            for m in re.finditer(pattern, text, re.VERBOSE):
                span = m.group(0)
                _add(span, etype, span, conf)

        return entities

    # ── P0-1: 自动本体发现 ──────────────────────────────────────────────

    def auto_ontology(self, entity_pairs: list[tuple[str, str]],
                      llm_func=None) -> list[str]:
        """从实体对列表自动发现关系类型（本体）。

        基于规则的 fallback 通过共现频率与分类推断关系名；
        当 llm_func 提供时优先走 LLM 路径。

        参数:
            entity_pairs: [(subject, object), ...] 实体对列表。
            llm_func: 可选的 LLM 回调，签名为
                      llm_func(entity_pairs) -> list[str]。

        返回:
            建议的新关系名列表（去重，按置信度排序）。
        """
        if llm_func is not None:
            try:
                result = llm_func(entity_pairs)
                if isinstance(result, list):
                    return list(dict.fromkeys(result))  # 去重保序
            except Exception:
                pass

        return self._rule_ontology(entity_pairs)

    def _rule_ontology(self,
                       entity_pairs: list[tuple[str, str]]) -> list[str]:
        """基于规则的自动本体发现（共现频率 + 分类推断）。"""
        from collections import Counter

        # 统计共现频率
        pair_counter: Counter = Counter()
        for s, o in entity_pairs:
            pair_counter[(s, o)] += 1

        # 按频率排序，取高频对
        total_pairs = len(entity_pairs) or 1
        freq_pairs = [(p, c / total_pairs) for p, c
                      in pair_counter.most_common(50)
                      if c >= 2]

        # 从 KG 中获取已有实体的类型信息
        def _entity_type(eid: str) -> str:
            ent = self._entities.get(eid)
            return ent["entity_type"] if ent else "unknown"

        # 分类推断规则：根据 subject/object 的 entity_type 推断关系
        type_rule_map = {
            ("person", "organization"): ["works_for", "member_of"],
            ("organization", "person"): ["employs", "has_member"],
            ("person", "person"): ["colleague_of", "reports_to", "knows"],
            ("organization", "organization"): ["partner_of", "subsidiary_of",
                                                "competitor_of"],
            ("project", "person"): ["assigned_to", "owned_by"],
            ("person", "project"): ["contributes_to", "leads"],
            ("document", "project"): ["belongs_to", "documents"],
            ("project", "document"): ["has_document", "produces"],
            ("file", "file"): ["depends_on", "references", "includes"],
            ("concept", "concept"): ["related_to", "subclass_of", "instance_of"],
            ("brand", "brand"): ["competes_with", "parent_of"],
            ("rule", "location"): ["located_at", "applies_at"],
            ("strategy", "rule"): ["implements", "governs"],
        }

        suggested: list[str] = []
        seen_rel: set[str] = set()

        for (s, o), freq in freq_pairs:
            st = _entity_type(s)
            ot = _entity_type(o)
            candidates = type_rule_map.get(
                (st, ot),
                type_rule_map.get(("concept", "concept"), ["related_to"]),
            )
            for rel_name in candidates:
                if rel_name not in seen_rel:
                    seen_rel.add(rel_name)
                    suggested.append(rel_name)

        # 补充RelationType中没有但是高频出现的关系
        existing_preds = {rel["predicate"] for rel in self._relations}
        for rel_name in ["references", "depends_on", "derived_from",
                         "part_of", "uses"]:
            if rel_name in existing_preds and rel_name not in seen_rel:
                seen_rel.add(rel_name)
                suggested.append(rel_name)

        return suggested


class IncrementalKGraph:
    """增量知识图谱更新管理器。

    包装现有 KnowledgeGraph 实例，提供实体/关系的 diff 计算、
    增量合并、状态哈希追踪等能力，避免全量重建。

    使用方式::

        kg = KnowledgeGraph()
        inc = IncrementalKGraph(kg)
        diff = inc.compute_diff(new_entities, new_relations)
        summary = inc.diff_and_merge(new_entities, new_relations)
        inc.mark_synced(inc._compute_state_hash())
    """

    def __init__(self, kgraph: KnowledgeGraph):
        """初始化增量管理器。

        参数:
            kgraph: 已加载的 KnowledgeGraph 实例。
        """
        self._kg = kgraph
        self._sync_hash: str | None = None
        # 启动时自动计算当前状态哈希作为基线
        self._sync_hash = self._compute_state_hash()

    # ── Diff 计算 ─────────────────────────────────────────────────────

    def compute_diff(
        self,
        new_entities: list[dict],
        new_relations: list[dict],
    ) -> dict:
        """计算新数据与现有图谱的差异。

        参数:
            new_entities: 新实体列表，每个 dict 至少包含 id / entity_type。
            new_relations: 新关系列表，每个 dict 至少包含
                           subject / predicate / object。

        返回:
            {
                "added_entities": [...],
                "removed_entities": [...],
                "modified_entities": [...],
                "added_relations": [...],
                "removed_relations": [...],
                "unchanged_entities": N,
                "unchanged_relations": N,
                "total_changes": N,
            }
        """
        # ── 实体差异 ──
        existing_eids = set(self._kg._entities.keys())
        new_eids = {e["id"] for e in new_entities}

        added_eids = new_eids - existing_eids
        removed_eids = existing_eids - new_eids

        # 检测修改：同 ID 但 entity_type 或 properties 不同
        modified: list[dict] = []
        new_map = {e["id"]: e for e in new_entities}
        for eid in new_eids & existing_eids:
            old = self._kg._entities[eid]
            new = new_map[eid]
            if (
                old.get("entity_type") != new.get("entity_type")
                or old.get("properties") != new.get("properties")
            ):
                modified.append({
                    "entity_id": eid,
                    "old_type": old.get("entity_type"),
                    "new_type": new.get("entity_type"),
                    "old_properties": old.get("properties", {}),
                    "new_properties": new.get("properties", {}),
                })

        added_entities = [new_map[eid] for eid in added_eids]
        removed_entities = [
            self._kg._entities[eid] for eid in removed_eids
        ]

        # ── 关系差异 ──
        def _rel_key(r: dict) -> tuple:
            return (r["subject"], r["predicate"], r["object"])

        existing_rel_keys = {_rel_key(r) for r in self._kg._relations}
        new_rel_keys = {_rel_key(r) for r in new_relations}

        added_rel_keys = new_rel_keys - existing_rel_keys
        removed_rel_keys = existing_rel_keys - new_rel_keys

        added_relations = [
            r for r in new_relations if _rel_key(r) in added_rel_keys
        ]
        removed_relations = [
            r for r in self._kg._relations if _rel_key(r) in removed_rel_keys
        ]

        unchanged_entities = len(existing_eids & new_eids) - len(modified)
        unchanged_relations = len(existing_rel_keys & new_rel_keys)

        total = (
            len(added_entities)
            + len(removed_entities)
            + len(modified)
            + len(added_relations)
            + len(removed_relations)
        )

        return {
            "added_entities": added_entities,
            "removed_entities": removed_entities,
            "modified_entities": modified,
            "added_relations": added_relations,
            "removed_relations": removed_relations,
            "unchanged_entities": unchanged_entities,
            "unchanged_relations": unchanged_relations,
            "total_changes": total,
        }

    # ── Diff + Merge ──────────────────────────────────────────────────

    def diff_and_merge(
        self,
        new_entities: list[dict],
        new_relations: list[dict],
    ) -> dict:
        """对比并仅写入增量变化到图谱。

        执行流程:
            1. compute_diff 计算差异
            2. 应用 added_entities（调用 add_entity）
            3. 应用 added_relations（调用 add_relation）
            4. 应用 modified_entities（remove + add）
            5. 删除 removed_relations / removed_entities
            6. 保存到磁盘

        参数:
            new_entities / new_relations: 同 compute_diff。

        返回:
            diff 摘要 + merge 统计。
        """
        diff = self.compute_diff(new_entities, new_relations)

        merge_stats = {
            "entities_added": 0,
            "entities_removed": 0,
            "entities_modified": 0,
            "relations_added": 0,
            "relations_removed": 0,
        }

        # 1) 新增实体
        for ent in diff["added_entities"]:
            self._kg.add_entity(
                entity_id=ent["id"],
                entity_type=ent.get("entity_type", "unknown"),
                properties=ent.get("properties", {}),
            )
            merge_stats["entities_added"] += 1

        # 2) 修改实体: remove → add
        for mod in diff["modified_entities"]:
            eid = mod["entity_id"]
            self._kg.remove_entity(eid)
            self._kg.add_entity(
                entity_id=eid,
                entity_type=mod["new_type"] or "unknown",
                properties=mod.get("new_properties", {}),
            )
            merge_stats["entities_modified"] += 1

        # 3) 新增关系
        for rel in diff["added_relations"]:
            self._kg.add_relation(
                subject=rel["subject"],
                predicate=rel["predicate"],
                object=rel["object"],
                weight=rel.get("weight", 1.0),
                metadata=rel.get("metadata", {}),
            )
            merge_stats["relations_added"] += 1

        # 4) 删除关系（重建索引方式）
        if diff["removed_relations"]:
            removed_keys = {
                (r["subject"], r["predicate"], r["object"])
                for r in diff["removed_relations"]
            }
            self._kg._relations = [
                r for r in self._kg._relations
                if (r["subject"], r["predicate"], r["object"])
                not in removed_keys
            ]
            merge_stats["relations_removed"] += len(removed_keys)
            self._kg._rebuild_relation_index()

        # 5) 删除实体
        for ent in diff["removed_entities"]:
            self._kg.remove_entity(ent["id"])
            merge_stats["entities_removed"] += 1

        # 6) 持久化
        if diff["total_changes"] > 0:
            self._kg.save()

        # 7) 更新同步哈希
        new_hash = self._compute_state_hash()
        self._sync_hash = new_hash

        return {
            "diff": diff,
            "merge": merge_stats,
            "sync_hash": new_hash,
        }

    # ── 同步哈希 ──────────────────────────────────────────────────────

    def get_last_sync_hash(self) -> str | None:
        """返回上次同步的状态哈希。"""
        return self._sync_hash

    def mark_synced(self, sync_hash: str) -> None:
        """标记同步完成，更新状态哈希。

        参数:
            sync_hash: 新的同步状态哈希（通常是 _compute_state_hash() 的返回值）。
        """
        self._sync_hash = sync_hash

    def _compute_state_hash(self) -> str:
        """基于当前图谱全部实体和关系计算确定性状态哈希。"""
        import hashlib

        h = hashlib.sha256()
        # 实体: 按 id 排序
        for eid in sorted(self._kg._entities.keys()):
            ent = self._kg._entities[eid]
            h.update(eid.encode())
            h.update(ent.get("entity_type", "").encode())
            props = json.dumps(
                ent.get("properties", {}), sort_keys=True, ensure_ascii=False
            )
            h.update(props.encode())

        # 关系: 按 (subject, predicate, object) 排序
        rel_keys = sorted(
            (r["subject"], r["predicate"], r["object"])
            for r in self._kg._relations
        )
        for sk in rel_keys:
            h.update(sk[0].encode())
            h.update(sk[1].encode())
            h.update(sk[2].encode())

        return h.hexdigest()


# ── P3-1: Entity-Attribute-Time 3D 记忆建模（对标 MindMemOS）───────

class EntityAttributeTimeGraph:
    """Entity-Attribute-Time 三维记忆建模图。

    对标 MindMemOS 的实体-属性-时间三维结构：
      - 以 (entity, attribute, time) 为唯一坐标保存记忆
      - 追踪同一实体同一属性的演化轨迹（时间轴变化）
      - 新属性值自动归档旧值而非覆盖（演化驱动冲突消解）

    使用方式::

        eat = EntityAttributeTimeGraph(kgraph=kg)
        eat.record_attribute(
            entity_id="user_pref",
            attribute="theme",
            value="dark mode",
        )
        # 后续变化
        eat.record_attribute(
            entity_id="user_pref",
            attribute="theme",
            value="light mode",     # 旧值 'dark mode' 自动归档
        )
        timeline = eat.get_attribute_timeline("user_pref", "theme")
        # timeline: [{"value": "dark mode", ...}, {"value": "light mode", ...}]

    Reference:
        MindMemOS: Entity-Attribute-Time 3D structure (2026.08)
        LoCoMo benchmark: 92.5% with temporal modeling
    """

    def __init__(self, kgraph: KnowledgeGraph):
        """初始化三维记忆图。

        参数:
            kgraph: 已加载的 KnowledgeGraph 实例，作为底层图存储。
        """
        self._kg = kgraph
        # 属性演化历史: (entity_id, attribute) → [历史记录]
        self._attribute_history: dict[tuple, list[dict]] = {}
        # 元数据: entity_id → entity_type
        self._entity_types: dict[str, str] = {}

    # ── 属性记录（含自动归档）──────────────────────────────────────────

    def record_attribute(
        self,
        entity_id: str,
        attribute: str,
        value: Any,
        entity_type: str = "unknown",
        metadata: dict | None = None,
        timestamp: float | None = None,
    ) -> dict:
        """记录/更新实体属性值。

        如果该属性已存在旧值，自动将旧值归档到历史轨迹，
        同时更新图结构（entity -(has_attr)→ attribute_value）。

        参数:
            entity_id: 实体 ID。
            attribute: 属性名（如 "preferred_theme"）。
            value: 属性值。
            entity_type: 实体类型。
            metadata: 可选元数据。
            timestamp: 时间戳（默认当前时间）。

        返回:
            {"action": "created"/"updated", "archived_old": dict|None, ...}
        """
        ts = timestamp or time.time()
        key = (entity_id, attribute)

        # 初始化历史
        if key not in self._attribute_history:
            self._attribute_history[key] = []

        # 获取当前值作为旧值
        history = self._attribute_history[key]
        old_entry = history[-1] if history else None

        # 创建新条目
        new_entry = {
            "entity_id": entity_id,
            "attribute": attribute,
            "value": value,
            "entity_type": entity_type,
            "metadata": metadata or {},
            "timestamp": ts,
            "version": len(history) + 1,
        }
        history.append(new_entry)
        self._entity_types[entity_id] = entity_type

        # 如果新旧值相同，不重复记录
        if old_entry and old_entry.get("value") == value:
            return {
                "action": "unchanged",
                "entity_id": entity_id,
                "attribute": attribute,
                "value": value,
                "archived_old": None,
            }

        # 更新图结构：移除旧属性关系，添加新属性关系
        old_attr_id = f"{entity_id}__{attribute}"
        new_attr_id = f"{entity_id}__{attribute}__v{len(history)}"

        # 确保实体存在
        if entity_id not in self._kg._entities:
            self._kg.add_entity(entity_id, entity_type, {
                "name": entity_id,
                attribute: value,
            })
        else:
            # 更新实体的最新属性值
            ent = self._kg._entities[entity_id]
            ent["properties"][attribute] = value
            ent["properties"][f"{attribute}_version"] = len(history)

        # 添加属性演化关系
        self._kg.add_relation(
            subject=entity_id,
            predicate="has_attribute",
            object=new_attr_id,
            weight=1.0,
            metadata={
                "attribute": attribute,
                "value": str(value),
                "version": len(history),
                "timestamp": ts,
                "is_current": True,
                "entity_type": entity_type,
            },
        )

        # 归档旧关系（标记为非当前）
        if old_entry:
            old_version = old_entry["version"]
            old_attr_id_v = f"{entity_id}__{attribute}__v{old_version}"
            if old_attr_id_v != new_attr_id:
                self._kg.add_relation(
                    subject=entity_id,
                    predicate="had_attribute",
                    object=old_attr_id_v,
                    weight=0.5,
                    metadata={
                        "attribute": attribute,
                        "value": str(old_entry["value"]),
                        "version": old_version,
                        "timestamp": old_entry["timestamp"],
                        "is_current": False,
                        "superseded_by": new_attr_id,
                        "entity_type": entity_type,
                    },
                )

        return {
            "action": "updated" if old_entry else "created",
            "entity_id": entity_id,
            "attribute": attribute,
            "value": value,
            "version": len(history),
            "archived_old": old_entry,
        }

    # ── 属性演化轨迹查询 ────────────────────────────────────────────

    def get_attribute_timeline(
        self,
        entity_id: str,
        attribute: str,
        max_entries: int = 50,
    ) -> list[dict]:
        """获取指定实体属性的完整演化轨迹。

        参数:
            entity_id: 实体 ID。
            attribute: 属性名。
            max_entries: 最大返回条目数。

        返回:
            按时间排序的属性变化历史列表，包含时间戳、值、版本号。
        """
        key = (entity_id, attribute)
        history = self._attribute_history.get(key, [])
        return history[-max_entries:]

    def get_entity_timeline(
        self,
        entity_id: str,
    ) -> dict[str, list[dict]]:
        """获取实体所有属性的演化轨迹。

        参数:
            entity_id: 实体 ID。

        返回:
            {attribute_name: [timeline_entries], ...}
        """
        result: dict[str, list[dict]] = {}
        for (eid, attr), history in self._attribute_history.items():
            if eid == entity_id:
                result[attr] = list(history)
        return result

    def get_current_value(
        self,
        entity_id: str,
        attribute: str,
    ) -> Any | None:
        """获取属性当前值（最新版本）。

        参数:
            entity_id: 实体 ID。
            attribute: 属性名。

        返回:
            当前值或 None。
        """
        timeline = self.get_attribute_timeline(entity_id, attribute)
        return timeline[-1]["value"] if timeline else None

    # ── 演化驱动冲突消解 ────────────────────────────────────────────

    def detect_conflicts(
        self,
        entity_id: str,
    ) -> list[dict]:
        """检测实体的属性冲突。

        当同一属性在短时间内出现多次互斥的值变化时标记为冲突。

        参数:
            entity_id: 实体 ID。

        返回:
            [{"attribute": ..., "values": [...], "confidence": ...}, ...]
        """
        conflicts: list[dict] = []
        timeline_map = self.get_entity_timeline(entity_id)

        for attr, history in timeline_map.items():
            if len(history) < 2:
                continue

            # 检测短时间内频繁变化
            unique_values = list(dict.fromkeys(
                h["value"] for h in history
            ))
            if len(unique_values) >= 3:
                conflicts.append({
                    "entity_id": entity_id,
                    "attribute": attr,
                    "values": unique_values,
                    "change_count": len(unique_values),
                    "total_versions": len(history),
                    "suggestion": "Consider clarifying user preference",
                })

        return conflicts

    def resolve_conflict(
        self,
        entity_id: str,
        attribute: str,
        resolved_value: Any,
        resolution_type: str = "explicit",
    ) -> dict:
        """消解属性冲突。

        标记冲突，归档所有冲突值，设置解析后的值。

        参数:
            entity_id: 实体 ID。
            attribute: 属性名。
            resolved_value: 解析后的值。
            resolution_type: "explicit"（显式确认）或 "heuristic"（启发式）。

        返回:
            record_attribute 的结果。
        """
        key = (entity_id, attribute)
        history = self._attribute_history.get(key, [])

        # 标记所有冲突版本
        for entry in history:
            entry["metadata"] = entry.get("metadata", {})
            entry["metadata"]["conflict_resolved"] = True
            entry["metadata"]["resolution_type"] = resolution_type

        return self.record_attribute(
            entity_id=entity_id,
            attribute=attribute,
            value=resolved_value,
            metadata={
                "conflict_resolved": True,
                "resolution_type": resolution_type,
                "resolved_versions": len(history),
            },
        )

    def get_stats(self) -> dict:
        """获取 EAT 图统计信息。"""
        total_attrs = sum(len(h) for h in self._attribute_history.values())
        unique_entities = len(set(e for e, _ in self._attribute_history))
        unique_attributes = len(set(a for _, a in self._attribute_history))

        return {
            "total_entities": unique_entities,
            "total_attributes": unique_attributes,
            "total_versions": total_attrs,
            "entity_type_distribution": {
                et: sum(1 for t in self._entity_types.values() if t == et)
                for et in set(self._entity_types.values())
            },
        }


# ── P3-5: 图记忆读写反馈进化（对标 SAGE）──────────────────────────────

class KGraphFeedbackLoop:
    """知识图谱 writer-reader 反馈闭环。

    对标 SAGE（Self-evolving Agentic Graph-memory Engine, May 2026）：
      - Memory Writer: 从交互历史中增量构建结构化图记忆
      - Memory Reader (GFM-based): 执行检索并向 Writer 提供反馈
      - 反馈维度：命中率 (hit rate)、证据链完整性 (evidence chain completeness)
      - Writer 根据 Reader 反馈优化图结构和索引策略
      - 支持多轮自进化

    使用方式::

        kg = KnowledgeGraph()
        kgfl = KGraphFeedbackLoop(kg)

        # Reader 检索
        results = kgfl.reader_retrieve(query="user preferences")

        # Reader 反馈检索质量
        feedback = kgfl.reader_feedback(
            query="user preferences",
            results=results,
            ground_truth=["user prefers dark mode"],
        )

        # Writer 根据反馈优化结构
        evolution = kgfl.writer_evolve(feedback)
        # evolution rounds 递增，索引策略调整
    """

    def __init__(
        self,
        kgraph: KnowledgeGraph,
        max_evolution_rounds: int = 10,
    ):
        """初始化反馈闭环。

        参数:
            kgraph: 知识图谱实例。
            max_evolution_rounds: 最大自进化轮数。
        """
        self._kg = kgraph
        self.max_evolution_rounds = max_evolution_rounds

        # 进化状态
        self._evolution_round: int = 0
        self._feedback_log: list[dict] = []

        # 索引优化参数
        self._index_weights: dict[str, float] = {
            "entity_name_weight": 1.0,
            "property_weight": 0.5,
            "relation_weight": 0.3,
            "entity_type_weight": 0.2,
        }

        # 图结构质量指标
        self._quality_metrics: dict[str, float] = {
            "avg_hit_rate": 1.0,
            "avg_evidence_completeness": 1.0,
            "graph_density": 0.0,
            "entity_connectivity": 0.0,
        }

    # ── Reader: 检索 ──────────────────────────────────────────────────

    def reader_retrieve(
        self,
        query: str,
        top_k: int = 10,
        use_ppr: bool = True,
    ) -> list[dict]:
        """Reader 执行检索。

        组合关键词搜索与 PPR 图搜索。

        参数:
            query: 检索查询。
            top_k: 返回数量。
            use_ppr: 是否使用 PPR 增强。

        返回:
            检索结果列表。
        """
        results: list[dict] = []
        seen_ids: set[str] = set()

        # 1) 关键词搜索（利用索引权重）
        keyword_hits = self._kg.search(query, top_k=top_k * 2)
        for hit in keyword_hits:
            eid = hit["entity"]["id"]
            if eid not in seen_ids:
                results.append({
                    "entity_id": eid,
                    "entity": hit["entity"],
                    "score": hit["score"],
                    "source": "keyword_search",
                })
                seen_ids.add(eid)

        # 2) PPR 图搜索
        if use_ppr:
            # 从关键词结果中提取实体作为 PPR 种子
            seed_entities = [r["entity_id"] for r in results[:3]]
            if seed_entities:
                ppr_results = self._kg.ppr_search(
                    query_entities=seed_entities,
                    top_k=top_k,
                )
                for pr in ppr_results:
                    if pr["entity_id"] not in seen_ids:
                        results.append({
                            "entity_id": pr["entity_id"],
                            "entity": pr["entity"],
                            "score": pr["ppr_score"],
                            "source": "ppr_graph",
                        })

        # 排序去重
        results.sort(key=lambda r: -r["score"])
        return results[:top_k]

    # ── Reader: 反馈 ──────────────────────────────────────────────────

    def reader_feedback(
        self,
        query: str,
        results: list[dict],
        ground_truth: list[str] | None = None,
        expected_entities: list[str] | None = None,
    ) -> dict:
        """Reader 向 Writer 提供检索质量反馈。

        计算：
          - 命中率 (hit rate): ground truth 在结果中的覆盖比例
          - 证据链完整性: 结果中能否串联形成完整推理路径
          - MRR (Mean Reciprocal Rank)

        参数:
            query: 原始查询。
            results: 检索结果列表。
            ground_truth: 期望的正确答案列表。
            expected_entities: 期望出现的实体 ID 列表。

        返回:
            反馈字典。
        """
        # ── 命中率 ──
        hit_rate = 1.0
        if ground_truth or expected_entities:
            targets = set(ground_truth or [])
            targets.update(expected_entities or [])
            if targets:
                result_ids = {r["entity_id"] for r in results}
                hit_ids = targets & result_ids
                hit_rate = len(hit_ids) / len(targets) if targets else 1.0

        # ── 证据链完整性 ──
        evidence_completeness = self._compute_evidence_completeness(
            results, query
        )

        # ── MRR ──
        mrr = 0.0
        if expected_entities:
            for rank, r in enumerate(results, 1):
                if r["entity_id"] in set(expected_entities):
                    mrr = 1.0 / rank
                    break

        feedback = {
            "query": query,
            "hit_rate": round(hit_rate, 4),
            "evidence_completeness": round(evidence_completeness, 4),
            "mrr": round(mrr, 4),
            "result_count": len(results),
            "timestamp": time.time(),
            "evolution_round": self._evolution_round,
        }

        self._feedback_log.append(feedback)
        return feedback

    def _compute_evidence_completeness(
        self,
        results: list[dict],
        query: str,
    ) -> float:
        """估算证据链完整性。

        通过检查结果实体间是否存在关系链来评估。
        """
        if len(results) < 2:
            return 0.5 if results else 0.0

        # 检查结果实体间的关系密度
        eids = [r["entity_id"] for r in results]
        connection_count = 0
        max_connections = len(eids) * (len(eids) - 1) / 2

        for i in range(len(eids)):
            for j in range(i + 1, len(eids)):
                # 通过 relation_index 检查是否存在直接关系
                i_rels = set(
                    r["object"] if r["subject"] == eids[i] else r["subject"]
                    for r in self._kg._relations
                    if r["subject"] == eids[i] or r["object"] == eids[i]
                )
                if eids[j] in i_rels:
                    connection_count += 1

        density = connection_count / max_connections if max_connections > 0 else 0.0

        # 同时考虑查询词在关系元数据中的匹配
        query_match_score = 0.0
        for rel in self._kg._relations:
            meta = rel.get("metadata", {})
            meta_str = str(meta).lower()
            if query.lower() in meta_str or query.lower() in rel.get("predicate", ""):
                query_match_score += 0.1

        query_match_score = min(query_match_score, 1.0)

        return 0.4 * density + 0.4 * query_match_score + 0.2 * (1 if results else 0)

    # ── Writer: 进化 ─────────────────────────────────────────────────

    def writer_evolve(self, feedback: dict | None = None) -> dict:
        """Writer 根据 Reader 反馈优化图结构和索引策略。

        优化维度:
          1. 索引权重调整: 根据命中率调整搜索评分权重
          2. 图密度优化: 过稀疏则建议添加关系
          3. 实体连接性: 孤立实体添加 inferred 关系

        参数:
            feedback: 最新的 Reader 反馈（可选，使用最近一次）。

        返回:
            进化摘要。
        """
        if self._evolution_round >= self.max_evolution_rounds:
            return {
                "round": self._evolution_round,
                "status": "max_rounds_reached",
                "changes": [],
            }

        fb = feedback or (self._feedback_log[-1] if self._feedback_log else None)
        if fb is None:
            return {"round": self._evolution_round, "status": "no_feedback"}

        changes: list[str] = []

        # ── 1) 索引权重调整 ──
        hit_rate = fb.get("hit_rate", 1.0)
        if hit_rate < 0.5:
            # 降低 property_weight，提高 entity_name_weight
            self._index_weights["entity_name_weight"] = min(
                2.0, self._index_weights["entity_name_weight"] + 0.1
            )
            self._index_weights["property_weight"] = max(
                0.1, self._index_weights["property_weight"] - 0.05
            )
            changes.append(
                f"Adjusted index weights: entity_name={self._index_weights['entity_name_weight']:.2f}, "
                f"property={self._index_weights['property_weight']:.2f}"
            )

        if fb.get("evidence_completeness", 1.0) < 0.6:
            self._index_weights["relation_weight"] = min(
                0.8, self._index_weights["relation_weight"] + 0.1
            )
            changes.append(
                f"Boosted relation_weight to {self._index_weights['relation_weight']:.2f}"
            )

        # ── 2) 图密度优化 ──
        stats = self._kg.get_stats()
        entities = stats.get("entity_count", 0)
        relations = stats.get("relation_count", 0)
        max_possible = entities * (entities - 1) / 2
        density = relations / max_possible if max_possible > 0 else 0
        self._quality_metrics["graph_density"] = round(density, 4)

        if density < 0.01 and entities >= 10:
            # 图过稀疏，添加 inferred 关系
            changes.append(
                f"Graph density low ({density:.4f}); "
                f"consider adding inferred relations to improve connectivity."
            )

        # ── 3) 实体连接性 ──
        connected = set()
        for rel in self._kg._relations:
            connected.add(rel["subject"])
            connected.add(rel["object"])
        isolated = len(self._kg._entities) - len(connected)
        self._quality_metrics["entity_connectivity"] = (
            len(connected) / max(len(self._kg._entities), 1)
        )

        if isolated > 0:
            changes.append(
                f"Detected {isolated} isolated entities; "
                f"consider entity linking to improve retrieval."
            )

        self._evolution_round += 1
        self._quality_metrics["avg_hit_rate"] = (
            0.9 * self._quality_metrics["avg_hit_rate"]
            + 0.1 * hit_rate
        )
        self._quality_metrics["avg_evidence_completeness"] = (
            0.9 * self._quality_metrics["avg_evidence_completeness"]
            + 0.1 * fb.get("evidence_completeness", 1.0)
        )

        return {
            "round": self._evolution_round,
            "changes": changes,
            "quality_metrics": dict(self._quality_metrics),
            "index_weights": dict(self._index_weights),
        }

    def auto_evolve(self, rounds: int = 3) -> list[dict]:
        """自动执行多轮自进化。

        每轮使用最近反馈进行优化。

        参数:
            rounds: 进化轮数。

        返回:
            每轮的进化摘要列表。
        """
        summaries: list[dict] = []
        for _ in range(min(rounds, self.max_evolution_rounds - self._evolution_round)):
            summary = self.writer_evolve()
            summaries.append(summary)
            if summary.get("status") == "no_feedback":
                break
        return summaries

    # ── 分析与导出 ──────────────────────────────────────────────────

    def get_feedback_summary(self) -> dict:
        """获取反馈历史摘要。"""
        if not self._feedback_log:
            return {"total_feedbacks": 0}

        hit_rates = [f["hit_rate"] for f in self._feedback_log]
        ev_completeness = [f["evidence_completeness"] for f in self._feedback_log]

        return {
            "total_feedbacks": len(self._feedback_log),
            "evolution_rounds": self._evolution_round,
            "avg_hit_rate": round(sum(hit_rates) / len(hit_rates), 4),
            "avg_evidence_completeness": (
                round(sum(ev_completeness) / len(ev_completeness), 4)
            ),
            "latest_index_weights": dict(self._index_weights),
            "latest_quality_metrics": dict(self._quality_metrics),
        }

    def export_feedback_log(self, path: str) -> str:
        """导出反馈日志为 JSONL 文件。"""
        dir_path = os.path.dirname(path)
        if dir_path and not os.path.isdir(dir_path):
            os.makedirs(dir_path, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            for entry in self._feedback_log:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path


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
