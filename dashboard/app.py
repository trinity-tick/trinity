"""
Trinity Web Dashboard — Lightweight Flask monitor on port 3000.

API:
  GET /              Dashboard home
  GET /api/stats     Trinity runtime statistics
  GET /api/kgraph    Knowledge graph overview
  GET /api/memories  Memory list (paginated: ?page=1&per_page=20)
  GET /api/agents    Agent registry

Data sources:
  - trinity_store.db (SQLite)
  - data/kgraph/kgraph_data.jsonl
  - data/a2a_registry.json
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------------------
# Paths relative to this dashboard directory
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent  # dashboard/
TRINITY_DIR = BASE_DIR.parent               # trinity/
# 权威大库兜底（2026-08-15）：优先 ~/.trinity/store，回退项目根
_AUTH_DB = Path(os.path.expanduser("~/.trinity/store/trinity_store.db"))
DB_PATH = _AUTH_DB if _AUTH_DB.exists() else (TRINITY_DIR / "trinity_store.db")
KGRAPH_PATH = TRINITY_DIR / "data" / "kgraph" / "kgraph_data.jsonl"
A2A_PATH = TRINITY_DIR / "data" / "a2a_registry.json"
LEADERBOARD_PATH = TRINITY_DIR / "benchmark" / "leaderboard.html"

app = Flask(__name__,
            template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    """Get a fresh read-only connection to Trinity store."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_jsonl(path: Path) -> List[Dict]:
    """Load JSONL file into list of dicts."""
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def _load_json(path: Path) -> Dict:
    """Load single JSON file."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Dashboard homepage."""
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    """Aggregated Trinity runtime statistics."""
    try:
        db = _get_db()
        memory_count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        version_count = db.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0]
        audit_count = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        tenant_count = db.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]

        # Recent memory timestamp
        recent = db.execute(
            "SELECT memory_id FROM memories ORDER BY memory_id DESC LIMIT 1"
        ).fetchone()
        last_memory_id = recent["memory_id"] if recent else "none"

        db.close()
    except Exception:
        memory_count = 0
        version_count = 0
        audit_count = 0
        tenant_count = 0
        last_memory_id = "error"

    # KGraph stats
    kgraph_records = _load_jsonl(KGRAPH_PATH)
    entity_count = sum(1 for r in kgraph_records if r.get("type") == "entity")
    relation_count = sum(1 for r in kgraph_records if r.get("type") == "relation")

    # Agent stats
    a2a_data = _load_json(A2A_PATH)
    agents = a2a_data.get("agents", [])
    agent_count = len(agents)
    active_agents = sum(1 for a in agents if a.get("status") == "active")
    idle_agents = sum(1 for a in agents if a.get("status") == "idle")

    return jsonify({
        "timestamp": time.time(),
        "memory_count": memory_count,
        "version_count": version_count,
        "audit_count": audit_count,
        "tenant_count": tenant_count,
        "entity_count": entity_count,
        "relation_count": relation_count,
        "agent_count": agent_count,
        "active_agents": active_agents,
        "idle_agents": idle_agents,
        "last_memory_id": last_memory_id,
        "trinity_version": "6.37.0",
        "evolution_cycles": audit_count,
    })


@app.route("/api/kgraph")
def api_kgraph():
    """Knowledge graph overview: entities + relations."""
    records = _load_jsonl(KGRAPH_PATH)

    entities = []
    relations = []
    for rec in records:
        if rec.get("type") == "entity":
            entities.append({
                "id": rec.get("id"),
                "label": rec.get("properties", {}).get("name", rec.get("id", "?")),
                "entity_type": rec.get("entity_type", "unknown"),
                "group": rec.get("entity_type", "unknown"),
                "created_at": rec.get("created_at"),
            })
        elif rec.get("type") == "relation":
            relations.append({
                "id": rec.get("id"),
                "source": rec.get("source"),
                "target": rec.get("target"),
                "relation_type": rec.get("relation_type", "related_to"),
                "label": rec.get("relation_type", "related_to"),
            })

    # Add default labels for nodes missing the "label" property
    # vis.js uses `from` and `to` for edges
    edges = []
    for rel in relations:
        edges.append({
            "from": rel["source"],
            "to": rel["target"],
            "label": rel.get("relation_type", ""),
            "title": rel.get("relation_type", ""),
        })

    return jsonify({
        "nodes": entities,
        "edges": edges,
        "total_entities": len(entities),
        "total_relations": len(edges),
    })


@app.route("/api/memories")
def api_memories():
    """Paginated memory list."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    per_page = min(per_page, 100)

    try:
        db = _get_db()

        # Count total
        total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

        # Get page
        offset = (page - 1) * per_page
        rows = db.execute("""
            SELECT m.memory_id, m.content, m.role, m.session_id
            FROM memories m
            ORDER BY m.memory_id DESC
            LIMIT ? OFFSET ?
        """, (per_page, offset)).fetchall()

        # Get FTS data for tags/category
        memories = []
        for r in rows:
            mid = r["memory_id"]
            fts = db.execute(
                "SELECT category, tags FROM memories_fts WHERE content = ?",
                (r["content"],)
            ).fetchone()
            memories.append({
                "memory_id": mid,
                "content": r["content"][:500],
                "role": r["role"],
                "session_id": r["session_id"],
                "category": fts["category"] if fts else "",
                "tags": json.loads(fts["tags"]) if fts and fts["tags"] else [],
                "importance": (_importance_score(r["content"])),
            })

        db.close()
    except Exception:
        total = 0
        memories = []

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page) if total else 1,
        "memories": memories,
    })


def _importance_score(content: str) -> int:
    """Heuristic importance score (1-5) based on content length and keywords."""
    score = 1
    if not content:
        return score
    if len(content) > 200:
        score += 1
    if len(content) > 500:
        score += 1
    for kw in ["rule", "critical", "important", "evolution", "handoff", "safety", "error"]:
        if kw.lower() in content.lower():
            score += 1
            break
    return min(score, 5)


@app.route("/api/agents")
def api_agents():
    """Agent registry list."""
    a2a_data = _load_json(A2A_PATH)
    agents = a2a_data.get("agents", [])

    result = []
    for a in agents:
        caps = a.get("capabilities", [])
        hb = a.get("last_heartbeat", 0)
        hb_str = ""
        if hb:
            hb_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(hb))
        result.append({
            "agent_id": a.get("agent_id", ""),
            "name": a.get("name", ""),
            "version": a.get("version", ""),
            "capabilities": caps,
            "capability_count": len(caps),
            "endpoint": a.get("endpoint", ""),
            "status": a.get("status", "unknown"),
            "last_heartbeat": hb_str,
        })

    return jsonify({
        "total": len(result),
        "agents": result,
        "registry_version": a2a_data.get("registry_version", "1.0"),
    })


# ---------------------------------------------------------------------------
# Leaderboard（2026-08-15, V2 动作 A）：渲染 benchmark/leaderboard.html
# ---------------------------------------------------------------------------

@app.route("/leaderboard")
def leaderboard():
    """记忆基准榜单页（MemBench leaderboard）。"""
    if LEADERBOARD_PATH.exists():
        return LEADERBOARD_PATH.read_text(encoding="utf-8", errors="ignore")
    return "<h3>Leaderboard 未生成</h3><p>运行 benchmark 后生成 leaderboard.html</p>", 404


@app.route("/api/leaderboard")
def api_leaderboard():
    """Leaderboard 元数据（供外部引用）。"""
    return jsonify({
        "exists": LEADERBOARD_PATH.exists(),
        "path": str(LEADERBOARD_PATH),
        "generated": time.strftime("%Y-%m-%d %H:%M:%S",
                                    time.localtime(LEADERBOARD_PATH.stat().st_mtime))
        if LEADERBOARD_PATH.exists() else None,
    })


# ---------------------------------------------------------------------------
# 企业审计回放（2026-08-15, V2 动作 B）：/audit 页 + /api/audit 端点
# ---------------------------------------------------------------------------

@app.route("/audit")
def audit_page():
    """审计回放页（HTML，含过滤 UI）。"""
    return render_template("audit.html")


@app.route("/api/audit")
def api_audit():
    """审计日志查询：按 agent / persona / action / memory 过滤，可追溯。

    Query: ?agent=&persona=&action=&memory=&limit=100
    """
    agent = request.args.get("agent", "")
    persona = request.args.get("persona", "")
    action = request.args.get("action", "")
    memory = request.args.get("memory", "")
    limit = min(int(request.args.get("limit", 100)), 500)

    where = []
    params: list = []
    if agent:
        where.append("agent_id LIKE ?")
        params.append(f"%{agent}%")
    if persona:
        where.append("persona_id LIKE ?")
        params.append(f"%{persona}%")
    if action:
        where.append("action LIKE ?")
        params.append(f"%{action}%")
    if memory:
        where.append("memory_id = ?")
        params.append(memory)

    db = _get_db()
    sql = "SELECT id, memory_id, action, agent_id, persona_id, timestamp, checksum FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    rows = db.execute(sql, params).fetchall()
    entries = [
        {"id": r[0], "memory_id": r[1], "action": r[2], "agent_id": r[3],
         "persona_id": r[4], "timestamp": r[5],
         "checksum": (r[6] or "")[:16] + "..." if r[6] and len(r[6]) > 16 else (r[6] or "")}
        for r in rows
    ]
    return jsonify({"total": len(entries), "entries": entries,
                    "filters": {"agent": agent, "persona": persona,
                                "action": action, "memory": memory}})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 端口收敛（2026-08-15）：Docker trinity-dash 容器占用 :3000（IPv6 分占），
    # 本地 dashboard 改用 :3005 避免地址族分占冲突（DASH_PORT 可覆盖）。
    dash_port = int(os.environ.get("DASH_PORT", "3005"))
    print(f"Starting Trinity Dashboard on http://localhost:{dash_port}")
    app.run(host="0.0.0.0", port=dash_port, debug=False)
