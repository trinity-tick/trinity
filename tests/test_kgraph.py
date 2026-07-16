"""Tests for Trinity Knowledge Graph (kgraph.graph)."""

import os
import sys
import json
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.kgraph.graph import KnowledgeGraph, RelationType


# ====================================================================
# Fixtures
# ====================================================================

@pytest.fixture
def temp_storage_path():
    """Provide a temporary JSONL path for the knowledge graph."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "kgraph_test.jsonl")


@pytest.fixture
def empty_kg(temp_storage_path):
    """Return an empty KnowledgeGraph instance."""
    kg = KnowledgeGraph(storage_path=temp_storage_path)
    yield kg


@pytest.fixture
def populated_kg(empty_kg):
    """Return a KnowledgeGraph pre-populated with entities and relations."""
    kg = empty_kg
    # Entities
    kg.add_entity("alice", "person", {"name": "Alice", "age": 30, "city": "NYC"})
    kg.add_entity("bob", "person", {"name": "Bob", "age": 25, "city": "SF"})
    kg.add_entity("charlie", "person", {"name": "Charlie", "age": 35, "city": "NYC"})
    kg.add_entity("project_x", "project", {"name": "Project X", "status": "active"})
    kg.add_entity("project_y", "project", {"name": "Project Y", "status": "completed"})
    kg.add_entity("team_alpha", "team", {"name": "Team Alpha"})

    # Relations
    kg.add_relation("alice", "belongs_to", "team_alpha")
    kg.add_relation("bob", "belongs_to", "team_alpha")
    kg.add_relation("charlie", "belongs_to", "team_alpha")
    kg.add_relation("alice", "works_on", "project_x", weight=0.8)
    kg.add_relation("bob", "works_on", "project_y", weight=0.9)
    kg.add_relation("project_x", "depends_on", "project_y")
    kg.add_relation("charlie", "manages", "project_x", weight=1.0)

    yield kg


# ====================================================================
# Tests: Node / Entity Addition
# ====================================================================

class TestEntityAddition:
    """Test adding entities to the knowledge graph."""

    def test_add_entity_returns_entity(self, empty_kg):
        """add_entity should return the created entity dict."""
        entity = empty_kg.add_entity("test_1", "test_type", {"key": "val"})
        assert entity["id"] == "test_1"
        assert entity["entity_type"] == "test_type"
        assert entity["properties"]["key"] == "val"
        assert "created_at" in entity

    def test_get_entity_after_add(self, empty_kg):
        """get_entity should retrieve the entity after add."""
        empty_kg.add_entity("e1", "widget", {"color": "red"})
        entity = empty_kg.get_entity("e1")
        assert entity is not None
        assert entity["properties"]["color"] == "red"

    def test_get_nonexistent_entity(self, empty_kg):
        """get_entity should return None for nonexistent IDs."""
        assert empty_kg.get_entity("no_such_entity") is None

    def test_add_entity_without_properties(self, empty_kg):
        """add_entity should work without properties."""
        entity = empty_kg.add_entity("bare_entity", "simple")
        assert entity["properties"] == {}

    def test_add_duplicate_entity_overwrites(self, empty_kg):
        """Adding the same ID twice should overwrite the first."""
        empty_kg.add_entity("dup", "type_a", {"data": "first"})
        empty_kg.add_entity("dup", "type_b", {"data": "second"})
        entity = empty_kg.get_entity("dup")
        assert entity["entity_type"] == "type_b"
        assert entity["properties"]["data"] == "second"


class TestEntityRemoval:
    """Test removing entities from the graph."""

    def test_remove_entity(self, empty_kg):
        """remove_entity should delete the entity and its relations."""
        empty_kg.add_entity("removable", "test", {})
        empty_kg.add_relation("removable", "references", "other")
        assert empty_kg.remove_entity("removable") is True
        assert empty_kg.get_entity("removable") is None

    def test_remove_nonexistent_entity(self, empty_kg):
        """remove_entity should return False for nonexistent ID."""
        assert empty_kg.remove_entity("ghost") is False

    def test_remove_entity_cleans_relation_index(self, empty_kg):
        """Removing an entity should also remove its relations."""
        empty_kg.add_entity("a", "test", {})
        empty_kg.add_entity("b", "test", {})
        empty_kg.add_relation("a", "references", "b")
        empty_kg.remove_entity("a")
        # b should still exist, but the relation is gone
        stats = empty_kg.get_stats()
        assert stats["relation_count"] == 0


# ====================================================================
# Tests: Relation Addition
# ====================================================================

class TestRelationAddition:
    """Test adding relations to the knowledge graph."""

    def test_add_relation_returns_relation(self, empty_kg):
        """add_relation should return the created relation dict."""
        empty_kg.add_entity("src", "type", {})
        empty_kg.add_entity("tgt", "type", {})
        rel = empty_kg.add_relation("src", "references", "tgt", weight=0.5)
        assert rel["subject"] == "src"
        assert rel["predicate"] == "references"
        assert rel["object"] == "tgt"
        assert rel["weight"] == 0.5

    def test_add_relation_auto_creates_entities(self, empty_kg):
        """Adding a relation with nonexistent IDs should create placeholder entities."""
        rel = empty_kg.add_relation("auto_src", "uses", "auto_tgt")
        # Both should now exist as "unknown" type entities
        assert empty_kg.get_entity("auto_src") is not None
        assert empty_kg.get_entity("auto_tgt") is not None
        assert empty_kg.get_entity("auto_src")["entity_type"] == "unknown"

    def test_add_relation_default_weight(self, empty_kg):
        """add_relation should default weight to 1.0."""
        empty_kg.add_entity("a", "type", {})
        empty_kg.add_entity("b", "type", {})
        rel = empty_kg.add_relation("a", "connects", "b")
        assert rel["weight"] == 1.0

    def test_add_relation_with_metadata(self, empty_kg):
        """add_relation should accept and store metadata."""
        empty_kg.add_entity("a", "type", {})
        empty_kg.add_entity("b", "type", {})
        rel = empty_kg.add_relation(
            "a", "connects", "b",
            metadata={"source": "user_input", "timestamp": "2025-01-01"}
        )
        assert rel["metadata"]["source"] == "user_input"

    def test_add_multiple_relations_same_pair(self, empty_kg):
        """Adding multiple relations between the same entities should work."""
        empty_kg.add_entity("x", "type", {})
        empty_kg.add_entity("y", "type", {})
        empty_kg.add_relation("x", "references", "y")
        empty_kg.add_relation("x", "depends_on", "y")
        stats = empty_kg.get_stats()
        assert stats["relation_count"] == 2


# ====================================================================
# Tests: Query / Traversal
# ====================================================================

class TestQueryRelations:
    """Test querying relations by entity (BFS traversal)."""

    def test_query_relations_depth_1(self, populated_kg):
        """query_relations should return direct relations at depth 1."""
        rels = populated_kg.query_relations("alice", max_depth=1)
        predicates = {r["predicate"] for r in rels}
        assert "belongs_to" in predicates
        assert "works_on" in predicates

    def test_query_relations_depth_2(self, populated_kg):
        """query_relations should reach indirect relations at depth 2."""
        rels = populated_kg.query_relations("alice", max_depth=2)
        # At depth 2, should reach depends_on via project_x
        predicates = {r["predicate"] for r in rels}
        assert "depends_on" in predicates
        assert "manages" in predicates

    def test_query_relations_isolated_entity(self, empty_kg):
        """Isolated entity should return empty relation list."""
        empty_kg.add_entity("lonely", "isolated", {})
        rels = empty_kg.query_relations("lonely")
        assert rels == []

    def test_query_by_type(self, populated_kg):
        """query_by_type should return all entities of a given type."""
        persons = populated_kg.query_by_type("person")
        assert len(persons) == 3
        ids = {e["id"] for e in persons}
        assert ids == {"alice", "bob", "charlie"}

    def test_query_by_type_nonexistent(self, empty_kg):
        """query_by_type for nonexistent type should return empty list."""
        assert empty_kg.query_by_type("dinosaur") == []


# ====================================================================
# Tests: Keyword Search
# ====================================================================

class TestSearch:
    """Test keyword search functionality."""

    def test_search_by_entity_id(self, populated_kg):
        """Search should find entities by ID."""
        results = populated_kg.search("alice", top_k=5)
        assert len(results) >= 1
        assert results[0]["entity"]["id"] == "alice"

    def test_search_by_property_value(self, populated_kg):
        """Search should find entities by property value."""
        results = populated_kg.search("NYC", top_k=5)
        ids = {r["entity"]["id"] for r in results}
        assert "alice" in ids
        assert "charlie" in ids

    def test_search_by_name_property(self, populated_kg):
        """Search should give a bonus to name property matches."""
        results = populated_kg.search("Team Alpha", top_k=5)
        # Should find team_alpha by name
        ids = {r["entity"]["id"] for r in results}
        assert "team_alpha" in ids

    def test_search_case_insensitive(self, populated_kg):
        """Search should be case-insensitive."""
        results_lower = populated_kg.search("alice", top_k=5)
        results_upper = populated_kg.search("ALICE", top_k=5)
        assert len(results_lower) == len(results_upper)

    def test_search_top_k_limit(self, populated_kg):
        """Search should respect the top_k limit."""
        results = populated_kg.search("a", top_k=2)
        assert len(results) <= 2

    def test_search_no_match(self, empty_kg):
        """Search with no matches should return empty list."""
        results = empty_kg.search("nonexistent")
        assert results == []

    def test_search_empty_query(self, populated_kg):
        """Empty query triggers in-match on all entities (actual behavior)."""
        results = populated_kg.search("")
        # The search treats "" as in all strings, matching every entity
        assert len(results) > 0


# ====================================================================
# Tests: Stats & Subgraph
# ====================================================================

class TestStatsAndSubgraph:
    """Test get_stats() and get_subgraph()."""

    def test_get_stats_counts(self, populated_kg):
        """get_stats should return correct entity/relation counts."""
        stats = populated_kg.get_stats()
        assert stats["entity_count"] == 6
        assert stats["relation_count"] == 7

    def test_get_stats_type_distribution(self, populated_kg):
        """get_stats should show entity type distribution."""
        stats = populated_kg.get_stats()
        assert stats["entity_type_distribution"]["person"] == 3
        assert stats["entity_type_distribution"]["project"] == 2

    def test_get_stats_relation_distribution(self, populated_kg):
        """get_stats should show relation type distribution."""
        stats = populated_kg.get_stats()
        assert stats["relation_type_distribution"]["belongs_to"] == 3
        assert stats["relation_type_distribution"]["works_on"] == 2

    def test_get_subgraph(self, populated_kg):
        """get_subgraph should extract entities and relations."""
        sub = populated_kg.get_subgraph(["alice", "bob"], max_depth=1)
        assert len(sub["entities"]) >= 2
        assert len(sub["relations"]) >= 2

    def test_get_subgraph_with_depth(self, populated_kg):
        """get_subgraph with depth 0 should include only seed entities."""
        sub = populated_kg.get_subgraph(["alice"], max_depth=0)
        entity_ids = {e["id"] for e in sub["entities"]}
        assert entity_ids == {"alice"}

    def test_get_subgraph_includes_indirect(self, populated_kg):
        """get_subgraph should include indirectly connected entities."""
        sub = populated_kg.get_subgraph(["alice"], max_depth=1)
        # Should include team_alpha and project_x (direct neighbors)
        entity_ids = {e["id"] for e in sub["entities"]}
        assert "team_alpha" in entity_ids
        assert "project_x" in entity_ids

    def test_empty_graph_stats(self, empty_kg):
        """Empty graph should return zero counts."""
        stats = empty_kg.get_stats()
        assert stats["entity_count"] == 0
        assert stats["relation_count"] == 0


# ====================================================================
# Tests: Serialization (to_json, from_json, save, load)
# ====================================================================

class TestSerialization:
    """Test JSON serialization and file persistence."""

    def test_to_json_structure(self, populated_kg):
        """to_json should return a list of entity and relation dicts."""
        data = populated_kg.to_json()
        types = {item["type"] for item in data}
        assert "entity" in types
        assert "relation" in types
        assert len(data) > 0

    def test_from_json_roundtrip(self, populated_kg):
        """from_json(to_json()) should produce an equivalent graph."""
        data = populated_kg.to_json()
        kg2 = KnowledgeGraph.from_json(data)
        stats1 = populated_kg.get_stats()
        stats2 = kg2.get_stats()
        assert stats1["entity_count"] == stats2["entity_count"]
        assert stats1["relation_count"] == stats2["relation_count"]

    def test_save_creates_jsonl(self, populated_kg):
        """save() should write a valid JSON lines file."""
        saved_path = populated_kg.save()
        assert os.path.exists(saved_path)
        with open(saved_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        # Each line should be valid JSON
        for line in lines:
            obj = json.loads(line)
            assert "type" in obj

    def test_load_roundtrip(self, populated_kg):
        """save() then load() should reconstruct the graph."""
        saved_path = populated_kg.save()
        kg2 = KnowledgeGraph.load(saved_path)
        stats1 = populated_kg.get_stats()
        stats2 = kg2.get_stats()
        assert stats1["entity_count"] == stats2["entity_count"]
        assert stats1["relation_count"] == stats2["relation_count"]

    def test_load_missing_file_raises(self, temp_storage_path):
        """Loading from a nonexistent path should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            KnowledgeGraph.load(temp_storage_path)


# ====================================================================
# Tests: RelationType 枚举
# ====================================================================

class TestRelationType:
    """Test RelationType enum values."""

    def test_has_value_valid(self):
        """has_value should return True for valid relation types."""
        assert RelationType.has_value("belongs_to") is True
        assert RelationType.has_value("depends_on") is True

    def test_has_value_invalid(self):
        """has_value should return False for undefined types."""
        assert RelationType.has_value("invalid_type") is False
        assert RelationType.has_value("") is False

    def test_enum_values(self):
        """All enum members should have non-empty values."""
        for rt in RelationType:
            assert len(rt.value) > 0

    def test_part_of_present(self):
        """PART_OF should be a defined relation type."""
        assert RelationType.PART_OF.value == "part_of"


# ====================================================================
# Tests: Edge Cases
# ====================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_graph_with_many_entities(self, empty_kg):
        """Graph should handle many entities."""
        for i in range(100):
            empty_kg.add_entity(f"e{i}", "bulk", {"index": i})
        stats = empty_kg.get_stats()
        assert stats["entity_count"] == 100

    def test_graph_with_circular_relations(self, empty_kg):
        """Graph should handle circular relations without infinite loops."""
        empty_kg.add_entity("a", "node")
        empty_kg.add_entity("b", "node")
        empty_kg.add_entity("c", "node")
        empty_kg.add_relation("a", "references", "b")
        empty_kg.add_relation("b", "references", "c")
        empty_kg.add_relation("c", "references", "a")
        # BFS query should handle cycles gracefully
        rels = empty_kg.query_relations("a", max_depth=3)
        assert len(rels) >= 2

    def test_remove_all_entities(self, populated_kg):
        """Removing all entities should produce an empty graph."""
        entities = list(populated_kg._entities.keys())
        for eid in entities:
            populated_kg.remove_entity(eid)
        stats = populated_kg.get_stats()
        assert stats["entity_count"] == 0
        assert stats["relation_count"] == 0
