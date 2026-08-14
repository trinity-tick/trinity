"""
Entity-Relation Extractor (P0.2)
=================================
LLM-driven entity and relation extraction pipeline for Trinity's memory graph.

Extracts named entities (person / project / file / tool / concept / tag /
decision) and relationship pairs from memory content, then writes them to
the entities and relations tables via the storage adapter.

Design:
  - Primary: LLM-based extraction via ``extract_from_memories()``.
  - Fallback: rule-based regex extraction when no LLM callback is provided.
  - Pluggable: ``llm_call`` is a callback ``(prompt) -> str`` injected at init.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Regex-based fallback patterns ─────────────────────────────────────
_PERSON_PATTERN = re.compile(
    r'(?:张三|李四|王五|赵六|陈七|周八|吴九|郑十|Alice|Bob|Charlie|David|Eve|Frank|Grace|Henry|Ivy|Jack|Kate|Leo|Mia|Nick|Olivia|Paul|Quinn|Rose|Sam|Tina|Uma|Victor|Wendy|Xander|Yara|Zoe)'
)
_PROJECT_PATTERN = re.compile(
    r'(?:Trinity|Marvis|[A-Z][a-z]+(?:[A-Z][a-z]+)+)(?:\s*(?:Project|计划|方案|平台|系统|引擎))?'
)
_FILE_PATTERN = re.compile(
    r'[A-Za-z]:\\[^\s,，。；]+\.\w+|(?:[\w/-]+/)+[\w.-]+\.\w{2,5}'
)
_TOOL_PATTERN = re.compile(
    r'(?:Python|Docker|Git|VSCode|PyCharm|Chrome|Postman|Kubernetes|Terraform|Ansible|Jenkins|Grafana|Prometheus|Elasticsearch|Kibana|Logstash|Redis|PostgreSQL|MySQL|MongoDB|Neo4j|RabbitMQ|Kafka|Nginx|Apache|Tomcat|FastAPI|Flask|Django|Spring|React|Vue|Angular|Node\.js|TypeScript|Rust|Go|Java|C\+\+|SQLite|FFmpeg|ImageMagick|Pandas|NumPy|PyTorch|TensorFlow|Jupyter|HuggingFace|LangChain|LlamaIndex)'
)
_DECISION_PATTERN = re.compile(
    r'(?:决定|确定|选择|采用|放弃|改为|切换到|迁移到|升级到|降级到|替换为)(?:\S+)?'
)


# ── LLM prompt templates ──────────────────────────────────────────────

_EXTRACTION_SYSTEM = """You are an entity-relation extractor for a personal memory graph.
Given one or more memory entries, extract:

1. **entities**: people, projects, files, tools, concepts, tags, and decisions mentioned.
   - type must be one of: person / project / file / tool / concept / tag / decision
   - name: brief canonical name (e.g. "Alice", "Trinity", "report.pdf")

2. **relations**: triplets between extracted entities.
   - predicate examples: works_on, uses, depends_on, created_by, part_of, follows_up, decided_by, references, conflicts_with, supersedes
   - subject and object must refer to entity names exactly as listed in entities.

Output ONLY valid JSON with this structure:
{
  "entities": [{"name": "...", "type": "..."}],
  "relations": [{"subject": "...", "predicate": "...", "object": "..."}]
}"""


def _build_extraction_prompt(memories: List[Dict[str, Any]]) -> str:
    lines = []
    for i, mem in enumerate(memories):
        cid = mem.get("memory_id", str(i))
        content = mem.get("content", "")
        lines.append(f"[{i}] {cid}: {content}")
    return "Extract entities and relations from these memories:\n\n" + "\n".join(lines)


# ── Main class ────────────────────────────────────────────────────────

class EntityRelationExtractor:
    """Extract named entities and relations from memory content.

    Parameters
    ----------
    adapter : object
        Connected Trinity storage adapter exposing ``upsert_entity``,
        ``create_relation``, ``search_entities``, and ``get_memory``.
    llm_call : callable, optional
        Async or sync callback ``(prompt: str) -> str`` that calls an LLM.
        If omitted, falls back to regex-based extraction.
    """

    _ENTITY_TYPES = [
        "person", "project", "file", "tool",
        "concept", "tag", "decision",
    ]

    def __init__(
        self,
        adapter,
        llm_call: Optional[Callable[[str], str]] = None,
    ):
        self._adapter = adapter
        self._llm = llm_call

    # ── Public API ──────────────────────────────────────────────────

    def extract_from_memories(
        self, memory_ids: List[str],
    ) -> Dict[str, Any]:
        """Extract entities and relations from a batch of memories.

        1. Fetch memory content for each memory_id.
        2. Run LLM extraction (or regex fallback).
        3. Upsert all entities and create all relations.
        4. Return summary of what was extracted and stored.

        Parameters
        ----------
        memory_ids : list of str
            Memory IDs to process.

        Returns
        -------
        dict with keys:
            entities_added : int
            relations_added : int
            entities : list of entity dicts
            relations : list of relation dicts
        """
        # 1. Fetch memory content
        memories = []
        for mid in memory_ids:
            try:
                mem = self._adapter.get_memory(mid)
                if mem:
                    memories.append(mem)
            except Exception:
                logger.warning("Failed to fetch memory %s, skipping", mid)

        if not memories:
            return {"entities_added": 0, "relations_added": 0,
                    "entities": [], "relations": []}

        # 2. Extract
        if self._llm:
            extracted = self._extract_with_llm(memories)
        else:
            extracted = self._extract_with_regex(memories)

        # 3. Write to storage
        entities_added = 0
        entity_map: Dict[str, str] = {}  # name -> entity_id

        for ent in extracted.get("entities", []):
            name = ent.get("name", "").strip()
            etype = ent.get("type", "concept").strip()
            if not name or etype not in self._ENTITY_TYPES:
                continue
            try:
                result = self._adapter.upsert_entity(name=name, etype=etype)
                if "id" in result:
                    entity_map[name] = result["id"]
                    entities_added += 1
            except Exception:
                logger.warning("Failed to upsert entity: %s (%s)", name, etype)

        relations_added = 0
        relations = []
        for rel in extracted.get("relations", []):
            subj = rel.get("subject", "").strip()
            pred = rel.get("predicate", "related_to").strip()
            obj = rel.get("object", "").strip()
            if not subj or not obj:
                continue
            subj_id = entity_map.get(subj)
            obj_id = entity_map.get(obj)
            if not subj_id or not obj_id:
                continue
            try:
                result = self._adapter.create_relation(subj_id, pred, obj_id)
                if "id" in result:
                    relations_added += 1
                    relations.append(result)
            except Exception:
                logger.warning("Failed to create relation: %s -[%s]-> %s",
                               subj, pred, obj)

        entities_out = [{"name": n, "id": eid} for n, eid in entity_map.items()]

        return {
            "entities_added": entities_added,
            "relations_added": relations_added,
            "entities": entities_out,
            "relations": relations,
        }

    # ── LLM extraction ──────────────────────────────────────────────

    def _extract_with_llm(
        self, memories: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        prompt = _EXTRACTION_SYSTEM + "\n\n" + _build_extraction_prompt(memories)
        try:
            response = self._llm(prompt)
            return json.loads(response)
        except Exception as exc:
            logger.warning("LLM extraction failed: %s, falling back to regex", exc)
            return self._extract_with_regex(memories)

    # ── Regex fallback ──────────────────────────────────────────────

    def _extract_with_regex(
        self, memories: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        entities: Dict[str, str] = {}  # name -> type
        relations: List[Dict[str, str]] = []
        seen_entity_pairs: set = set()

        for mem in memories:
            content = mem.get("content", "")
            # 每条记忆独立提取，关系仅在记忆内建立（避免跨记忆噪音）。
            mem_entities: Dict[str, str] = {}

            for match in _PERSON_PATTERN.finditer(content):
                mem_entities[match.group()] = "person"

            for match in _PROJECT_PATTERN.finditer(content):
                name = match.group().strip()
                if len(name) >= 3 and not name.isdigit():
                    mem_entities[name] = "project"

            for match in _FILE_PATTERN.finditer(content):
                fname = match.group().split("/")[-1].split("\\")[-1]
                if fname:
                    mem_entities[fname] = "file"

            for match in _TOOL_PATTERN.finditer(content):
                mem_entities[match.group()] = "tool"

            for match in _DECISION_PATTERN.finditer(content):
                decision = match.group().strip()
                if decision and len(decision) >= 2:
                    mem_entities[decision] = "decision"

            # 合并到全局实体表。
            for name, typ in mem_entities.items():
                entities.setdefault(name, typ)

            # Heuristic relations: if a person entity co-occurs with a
            # project/tool in the same content, create a "works_on" relation.
            persons = [n for n, t in mem_entities.items() if t == "person"]
            non_persons = [n for n, t in mem_entities.items() if t != "person"]
            for p in persons:
                for np_name in non_persons:
                    pair = (p, np_name)
                    if pair not in seen_entity_pairs:
                        seen_entity_pairs.add(pair)
                        relations.append({
                            "subject": p, "predicate": "works_on",
                            "object": np_name,
                        })

            # 兜底：无 person 实体时，同一记忆内共现的非 person 实体
            # 两两建立 related_to 关系，保证纯中文记忆也有关系产出。
            if not persons and len(non_persons) >= 2:
                for i in range(len(non_persons)):
                    for j in range(i + 1, len(non_persons)):
                        pair = (non_persons[i], non_persons[j])
                        if pair not in seen_entity_pairs:
                            seen_entity_pairs.add(pair)
                            relations.append({
                                "subject": non_persons[i],
                                "predicate": "related_to",
                                "object": non_persons[j],
                            })

        entity_list = [
            {"name": n, "type": t} for n, t in entities.items()
        ]
        return {"entities": entity_list, "relations": relations}


# ── Module-level self_test ────────────────────────────────────────────
def self_test() -> Dict[str, Any]:
    """Run a quick smoke test using regex fallback (no LLM required)."""
    import tempfile
    import os

    from trinity.adapters.sqlite import SQLiteAdapter

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_er.db")
        adapter = SQLiteAdapter(db_path=db_path)
        adapter.connect()

        # Inject test memories
        r1 = adapter.store_memory(
            "Alice works on Trinity project using Python. "
            "We decided to migrate to PostgreSQL.",
            persona_id="test_persona",
        )
        r2 = adapter.store_memory(
            "Bob uses Docker and FastAPI for the Marvis service. "
            "Referenced file: C:\\Users\\reports\\q3_report.pdf",
            persona_id="test_persona",
        )
        adapter._flush_batch()

        extractor = EntityRelationExtractor(adapter, llm_call=None)

        result = extractor.extract_from_memories([
            r1["memory_id"], r2["memory_id"],
        ])

        # Basic assertions
        assertions = [
            result["entities_added"] > 0,
            len(result["entities"]) > 0,
        ]

        entities = result.get("entities", [])
        entity_names = [e["name"] for e in entities]

        diag = adapter.diagnostics()
        adapter.disconnect()

        passed = all(assertions)
        return {
            "module": "trinity.memory.er_extractor",
            "result": "PASS" if passed else "FAIL",
            "entities_added": result["entities_added"],
            "relations_added": result["relations_added"],
            "entity_names": entity_names[:20],
            "db_entity_count": diag["entity_count"],
            "db_relation_count": diag["relation_count"],
        }
