# -*- coding: utf-8 -*-
"""memory_stream_server.py — 记忆流 Web UI（表达伙伴，2026-08-27）。

Trinity 记忆的可见入口：最近记忆流 + 检索 + 热门查询统计。
用法:
    python scripts/memory_stream_server.py [--port 8010]
"""
import json
import os
import sys
import time
import sqlite3
import argparse
from datetime import datetime

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

DB = os.path.expanduser("~/.trinity/store/trinity_store.db")

PAGE = chr(60) + "!DOCTYPE html" + chr(62) + chr(10) + """
<html lang="zh"><head><meta charset="utf-8"><title>Trinity 记忆流</title>
<style>
body{font-family:system-ui;max-width:900px;margin:0 auto;padding:20px;background:#fafafa;color:#222}
h1{font-size:20px} .card{background:#fff;border:1px solid #e2e2e2;border-radius:8px;padding:12px;margin:8px 0}
.meta{color:#888;font-size:12px;margin-top:6px} .tag{background:#eef;border-radius:4px;padding:2px 6px;font-size:11px;margin-right:4px}
input{width:70%;padding:8px} button{padding:8px 16px} .query{background:#f5f5f5;border-radius:6px;padding:6px 10px;margin:4px 0;font-size:13px}
</style></head><body>
<h1>🧠 Trinity 记忆流</h1>
<form method="get" action="/"><input name="q" placeholder="检索记忆…" value="__Q__"><button>检索</button></form>
__SEARCH__
<h2>最近记忆流</h2>
__STREAM__
</body></html>
"""


def _conn():
    c = sqlite3.connect(DB, timeout=15)
    return c


def _mem_card(row):
    mid, cat, tags, created, content = row
    c = (content or "")[:220]
    tags_html = "".join(f'<span class="tag">{t}</span>' for t in (tags or [])[:4])
    created_s = (created or "")[:19].replace("T", " ")
    return (f'<div class="card"><div>{c}</div>'
            f'<div class="meta">{mid[:14]} · {cat} · {created_s} {tags_html}</div></div>')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8010)
    args = ap.parse_args()

    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse
    import uvicorn

    app = FastAPI(title="Trinity Memory Stream")

    @app.get("/", response_class=HTMLResponse)
    async def index(q: str = ""):
        conn = _conn()
        search_html = ""
        if q:
            rows = conn.execute(
                "SELECT memory_id, category, tags, created_at, substr(content,1,240) FROM memories "
                "WHERE status='active' AND content LIKE ? ORDER BY created_at DESC LIMIT 10",
                ("%" + q + "%",)).fetchall()
            search_html = "<h2>检索结果</h2>" + "".join(_mem_card(r) for r in rows) if rows else "<h2>检索结果</h2><p>无命中</p>"
        rows = conn.execute(
            "SELECT memory_id, category, tags, created_at, substr(content,1,240) FROM memories "
            "WHERE status='active' ORDER BY created_at DESC LIMIT 30").fetchall()
        stream = "".join(_mem_card(r) for r in rows)
        conn.close()
        page = PAGE.replace("__SEARCH__", search_html).replace("__STREAM__", stream)
        return HTMLResponse(page.replace("__Q__", q.replace('"', "&quot;")))

    @app.get("/api/stream")
    async def api_stream(limit: int = 20):
        conn = _conn()
        rows = conn.execute(
            "SELECT memory_id, category, tags, created_at, substr(content,1,300) FROM memories "
            "WHERE status='active' ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return {"memories": [{"id": r[0], "category": r[1], "tags": r[2], "created_at": r[3], "content": r[4]} for r in rows]}

    @app.get("/api/hot-queries")
    async def api_hot(days: int = 7):
        conn = _conn()
        since = (datetime.utcnow() - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = conn.execute(
            "SELECT details FROM audit_log WHERE action IN ('search','search_hybrid') AND timestamp >= ? LIMIT 500",
            (since,)).fetchall()
        qmap = {}
        for (d,) in rows:
            try:
                dd = json.loads(d)
                q2 = str(dd.get("query", ""))[:60]
                if q2:
                    qmap[q2] = qmap.get(q2, 0) + 1
            except Exception:
                pass
        conn.close()
        return {"hot": sorted(qmap.items(), key=lambda x: -x[1])[:10]}

    print(f"Trinity memory stream on http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
