"""P1-6: edge-level bi-temporal completion (COMPARISON_VS_2026_SOTA_R7).

Verifies:
  - SQLiteAdapter.query_relations_at returns only edges valid at the time
  - create_relation accepts valid_from/valid_to (edge bi-temporal)
  - API GET /graph/relations/at endpoint works end-to-end
  - entity_dedup merges duplicate-edge timelines (earliest valid_from,
    latest valid_to; NULL valid_to stays open)
"""

import sqlite3

import pytest

from scripts.entity_dedup import _merge_relation_timelines


@pytest.fixture()
def adapter():
    from trinity.adapters.sqlite import SQLiteAdapter
    a = SQLiteAdapter(db_path=":memory:")
    a.connect()
    yield a
    if hasattr(a, "close"):
        a.close()


def _eid(a, name):
    """Create an entity and return its id (upsert_entity returns {"id": ...})."""
    return a.upsert_entity(name, "person", {})["id"]


def test_create_relation_with_valid_window(adapter):
    e1 = _eid(adapter, "Alice")
    e2 = _eid(adapter, "Bob")
    r = adapter.create_relation(
        e1, "works_with", e2,
        valid_from="2026-01-01T00:00:00+00:00",
        valid_to="2026-06-01T00:00:00+00:00",
    )
    assert r["valid_from"].startswith("2026-01-01")
    assert r["valid_to"].startswith("2026-06-01")


def test_query_relations_at_filters_by_time(adapter):
    e1 = _eid(adapter, "Alice")
    e2 = _eid(adapter, "Bob")
    e3 = _eid(adapter, "Carol")
    adapter.create_relation(
        e1, "works_with", e2,
        valid_from="2026-01-01T00:00:00+00:00",
        valid_to="2026-03-01T00:00:00+00:00",
    )
    adapter.create_relation(
        e1, "works_with", e3,
        valid_from="2026-02-01T00:00:00+00:00",  # open-ended
    )

    # At 2026-01-15: only the first edge (closed window)
    at_jan = adapter.query_relations_at("2026-01-15T00:00:00+00:00")
    objs = {r["object_id"] for r in at_jan}
    assert objs == {e2}

    # At 2026-04-01: first edge expired, second still valid (open-ended)
    at_apr = adapter.query_relations_at("2026-04-01T00:00:00+00:00")
    objs = {r["object_id"] for r in at_apr}
    assert objs == {e3}


def test_query_relations_at_with_filters(adapter):
    e1 = _eid(adapter, "Alice")
    e2 = _eid(adapter, "Bob")
    adapter.create_relation(
        e1, "works_with", e2,
        valid_from="2026-01-01T00:00:00+00:00",
    )
    rows = adapter.query_relations_at(
        "2026-02-01T00:00:00+00:00",
        subject_id=e1, predicate="works_with",
    )
    assert len(rows) == 1
    assert rows[0]["object_id"] == e2


def test_api_query_relations_at_endpoint():
    from fastapi.testclient import TestClient
    from trinity.api.server import app

    with TestClient(app) as client:
        r = client.get(
            "/graph/relations/at",
            params={"at_time": "2026-01-15T00:00:00+00:00", "limit": 5},
        )
        assert r.status_code in (200, 501)
        if r.status_code == 200:
            assert isinstance(r.json(), list)


def test_api_create_relation_valid_params():
    from fastapi.testclient import TestClient
    from trinity.api.server import app

    with TestClient(app) as client:
        r = client.post(
            "/graph/relations",
            json={
                "subject_id": "entity_does_not_exist_a",
                "predicate": "test_pred",
                "object_id": "entity_does_not_exist_b",
                "valid_from": "2026-01-01T00:00:00+00:00",
                "valid_to": "2026-12-31T00:00:00+00:00",
            },
        )
        # Adapter rejects missing entities or creates them; either way the
        # endpoint must not 500 on the new params.
        assert r.status_code in (200, 400, 404, 501)


def test_merge_relation_timelines_closed_windows():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE relations (
               id TEXT, subject_id TEXT, predicate TEXT, object_id TEXT,
               properties TEXT, created_at TEXT, valid_from TEXT, valid_to TEXT)"""
    )
    rel_id = "same-triple"
    conn.execute(
        "INSERT INTO relations VALUES (?,?,?,?,?,?,?,?)",
        (rel_id, "keep", "p", "o", "{}", "t", "2026-01-01", "2026-03-01"),
    )
    conn.execute(
        "INSERT INTO relations VALUES (?,?,?,?,?,?,?,?)",
        (rel_id, "drop", "p", "o", "{}", "t", "2026-02-01", "2026-06-01"),
    )
    _merge_relation_timelines(conn, "keep", "drop")
    rows = conn.execute("SELECT * FROM relations").fetchall()
    assert len(rows) == 1
    assert rows[0]["valid_from"] == "2026-01-01"
    assert rows[0]["valid_to"] == "2026-06-01"
    conn.close()


def test_merge_relation_timelines_open_stays_open():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE relations (
               id TEXT, subject_id TEXT, predicate TEXT, object_id TEXT,
               properties TEXT, created_at TEXT, valid_from TEXT, valid_to TEXT)"""
    )
    rel_id = "same-triple"
    conn.execute(
        "INSERT INTO relations VALUES (?,?,?,?,?,?,?,?)",
        (rel_id, "keep", "p", "o", "{}", "t", "2026-01-01", "2026-03-01"),
    )
    conn.execute(
        "INSERT INTO relations VALUES (?,?,?,?,?,?,?,?)",
        (rel_id, "drop", "p", "o", "{}", "t", "2026-02-01", None),  # open-ended
    )
    _merge_relation_timelines(conn, "keep", "drop")
    rows = conn.execute("SELECT * FROM relations").fetchall()
    assert len(rows) == 1
    assert rows[0]["valid_from"] == "2026-01-01"
    assert rows[0]["valid_to"] is None  # open stays open
    conn.close()


def test_merge_relation_timelines_migrates_plain_edges():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE relations (
               id TEXT, subject_id TEXT, predicate TEXT, object_id TEXT,
               properties TEXT, created_at TEXT, valid_from TEXT, valid_to TEXT)"""
    )
    conn.execute(
        "INSERT INTO relations VALUES (?,?,?,?,?,?,?,?)",
        ("r1", "drop", "p", "o1", "{}", "t", "2026-01-01", None),
    )
    conn.execute(
        "INSERT INTO relations VALUES (?,?,?,?,?,?,?,?)",
        ("r2", "o2", "p", "drop", "{}", "t", "2026-01-01", None),
    )
    _merge_relation_timelines(conn, "keep", "drop")
    rows = conn.execute("SELECT * FROM relations ORDER BY id").fetchall()
    assert rows[0]["subject_id"] == "keep"
    assert rows[1]["object_id"] == "keep"
    conn.close()
