"""
Graph Retriever — Knowledge-Graph Traversal for Memory Retrieval.

Leverages Trinity's existing entities + relations + memory_links tables
to perform entity-anchored search, relation filtering, seed expansion,
and subgraph extraction.

All methods are read-only and operate through the SQLiteAdapter.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set


class GraphRetriever:
    """Entity / relation / subgraph retrieval backed by Trinity adapter.

    Parameters
    ----------
    adapter : object
        A connected Trinity storage adapter (SQLiteAdapter or
        PostgreSQLAdapter) exposing ``search_entities``,
        ``query_relations``, ``traverse``, ``get_entity``, and
        ``get_all_links``.
    """

    def __init__(self, adapter):
        self._adapter = adapter

    # ── Public API ──────────────────────────────────────────────────

    def search_by_entity(
        self, entity_name: str, top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """Find memories linked to a named entity.

        1. Look up entities matching ``entity_name`` (LIKE fuzzy).
        2. For each entity, traverse 1 hop to find related entities.
        3. Walk ``memory_links`` (outgoing + incoming) anchored on
           those entity IDs to collect linked memory IDs.
        4. Fetch full memory records for those IDs.

        Parameters
        ----------
        entity_name : str
            Entity name substring (fuzzy match).
        top_k : int
            Max memories to return.

        Returns
        -------
        list of memory dicts, each with a ``graph_score`` field.
        """
        entities = self._adapter.search_entities(
            name=entity_name, etype=None, limit=10,
        )
        if not entities:
            return []

        # Collect entity IDs + 1-hop neighbours
        entity_ids: Set[str] = set()
        for ent in entities:
            entity_ids.add(ent["id"])
            try:
                sub = self._adapter.traverse(ent["id"], max_hops=1)
                for node in sub.get("nodes", []):
                    entity_ids.add(node.get("id", ""))
            except Exception:
                pass

        return self._memories_for_entities(entity_ids, top_k)

    def search_by_relation(
        self,
        source_entity: str,
        relation_type: str,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """Find memories connected via a specific relation type.

        1. Find entities matching ``source_entity``.
        2. Query outgoing relations for those entities filtered by
           ``predicate == relation_type``.
        3. Collect the object entities from matched relations.
        4. Fetch linked memories.

        Parameters
        ----------
        source_entity : str
            Entity name substring.
        relation_type : str
            Relation predicate to filter on.
        top_k : int
            Max memories.

        Returns
        -------
        list of memory dicts with ``graph_score``.
        """
        entities = self._adapter.search_entities(
            name=source_entity, etype=None, limit=10,
        )
        if not entities:
            return []

        object_ids: Set[str] = set()
        for ent in entities:
            rels = self._adapter.query_relations(
                subject_id=ent["id"],
                predicate=relation_type,
                limit=20,
            )
            for rel in rels:
                object_ids.add(rel.get("object_id", ""))

        if not object_ids:
            return []

        return self._memories_for_entities(object_ids, top_k)

    def expand_from_seed(
        self,
        memory_id: str,
        depth: int = 2,
        top_k: int = 20,
    ) -> List[Dict[str, Any]]:
        """Expand from a seed memory via the link graph.

        1. Start from ``memory_id``.
        2. BFS through ``memory_links`` up to ``depth`` hops.
        3. Return all reachable memories scored by proximity
           (closer = higher).

        Parameters
        ----------
        memory_id : str
            Starting memory ID.
        depth : int
            Max BFS depth (1-3).
        top_k : int
            Max memories returned.

        Returns
        -------
        list of memory dicts with ``graph_score`` = 1.0 / (depth + 1).
        """
        depth = max(1, min(depth, 3))
        visited: Set[str] = {memory_id}
        frontier: Set[str] = {memory_id}
        results: Dict[str, Dict[str, Any]] = {}

        for d in range(1, depth + 1):
            next_frontier: Set[str] = set()
            for mid in frontier:
                try:
                    links = self._adapter.get_all_links(mid)
                except Exception:
                    continue
                for link in links.get("outgoing", []):
                    target = link.get("target_id", "")
                    if target and target not in visited:
                        visited.add(target)
                        next_frontier.add(target)
                        results[target] = {
                            "memory_id": target,
                            "content": link.get("target_content", ""),
                            "link_type": link.get("link_type", "semantic"),
                            "strength": float(link.get("strength", 0.5)),
                            "graph_score": round(1.0 / (d + 1), 4),
                            "graph_hops": d,
                        }
                for link in links.get("incoming", []):
                    source = link.get("source_id", "")
                    if source and source not in visited:
                        visited.add(source)
                        next_frontier.add(source)
                        results[source] = {
                            "memory_id": source,
                            "content": link.get("source_content",
                                              link.get("target_content", "")),
                            "link_type": link.get("link_type", "semantic"),
                            "strength": float(link.get("strength", 0.5)),
                            "graph_score": round(1.0 / (d + 1), 4),
                            "graph_hops": d,
                        }
            frontier = next_frontier
            if not frontier:
                break

        # Enrich with full memory records where possible
        enriched: List[Dict[str, Any]] = []
        for mid, info in list(results.items())[:top_k * 2]:
            try:
                mem = self._adapter.get_memory(mid)
                if mem:
                    mem["graph_score"] = info["graph_score"]
                    mem["graph_hops"] = info["graph_hops"]
                    mem["graph_source"] = "expand_from_seed"
                    enriched.append(mem)
                    continue
            except Exception:
                pass
            enriched.append(info)

        enriched.sort(key=lambda x: x.get("graph_score", 0), reverse=True)
        return enriched[:top_k]

    def get_subgraph(self, memory_ids: List[str]) -> Dict[str, Any]:
        """Extract the connected subgraph spanning a set of memory IDs.

        Finds all ``memory_links`` where both source and target are
        within ``memory_ids``, plus entities linked to any of those
        memories.

        Parameters
        ----------
        memory_ids : list of str
            Memory IDs to include in the subgraph.

        Returns
        -------
        dict with keys:
            nodes : list of memory dicts
            edges : list of link dicts (only within-set links)
            entities : list of entity dicts
        """
        id_set = set(memory_ids)
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []

        for mid in memory_ids:
            try:
                mem = self._adapter.get_memory(mid)
                if mem:
                    nodes.append(mem)
            except Exception:
                pass
            try:
                links = self._adapter.get_all_links(mid)
                for link in links.get("outgoing", []):
                    if link.get("target_id") in id_set:
                        edges.append(link)
            except Exception:
                pass

        # Gather entities — search for any entities whose name appears
        # in memory content (simple substring approach for now).
        entity_set: Dict[str, Dict[str, Any]] = {}
        for node in nodes:
            content = node.get("content", "")
            try:
                ents = self._adapter.search_entities(limit=50)
                for ent in ents:
                    ename = ent.get("name", "")
                    if ename and ename.lower() in content.lower():
                        entity_set[ent["id"]] = ent
            except Exception:
                pass

        return {
            "nodes": nodes,
            "edges": edges,
            "entities": list(entity_set.values()),
        }

    # ── helpers ─────────────────────────────────────────────────────

    def _memories_for_entities(
        self, entity_ids: Set[str], top_k: int,
    ) -> List[Dict[str, Any]]:
        """Look up memories linked (outgoing or incoming) to entity IDs."""
        memory_ids: Dict[str, float] = {}
        for eid in entity_ids:
            try:
                links = self._adapter.get_linked_memories(
                    eid, min_strength=0.0,
                )
                for link in links:
                    target = link.get("target_id", "")
                    strength = float(link.get("strength", 0.5))
                    if target and strength > memory_ids.get(target, -1):
                        memory_ids[target] = strength
            except Exception:
                pass

        # Fetch full records and attach graph_score
        results: List[Dict[str, Any]] = []
        for mid, strength in sorted(
            memory_ids.items(), key=lambda x: x[1], reverse=True,
        ):
            try:
                mem = self._adapter.get_memory(mid)
                if mem:
                    mem["graph_score"] = round(strength, 4)
                    mem["graph_source"] = "entity_search"
                    results.append(mem)
            except Exception:
                pass

        return results[:top_k]
