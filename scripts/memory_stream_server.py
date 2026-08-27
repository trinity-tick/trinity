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
__STATS__
<h2>热门查询（近 7 天）</h2>
<div>__HOT__</div>
<form method="get" action="/"><input name="q" placeholder="检索记忆…" value="__Q__"><select name="cat">__CATOPTIONS__</select><button>检索/过滤</button></form>
__SEARCH__
<h2>最近记忆流</h2>
__STREAM__
</body></html>
"""


def _conn():
    c = sqlite3.connect(DB, timeout=15)
    return c


def _snippet(text, hl, radius=80):
    """2026-08-27 UI 增强：命中词周围片段化 + 高亮（命中在深处时可见）。"""
    try:
        idx = text.find(hl)
        if idx < 0:
            return _safe(text[:220], "")
        start = max(0, idx - radius)
        end = min(len(text), idx + len(hl) + radius)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        return prefix + _safe(text[start:end], hl) + suffix
    except Exception:
        return _safe(text[:220], "")


def _safe(text, hl):
    try:
        # 2026-08-27: 先转义再高亮（否则 <mark> 被 &lt; 吃掉）
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if hl and hl in text:
            text = text.replace(hl, "<mark>" + hl + "</mark>")
        return text
    except Exception:
        return str(text)[:220]


def _mem_card(row, hl: str = ""):
    mid, cat, tags, created, content = row
    c = (content or "")[:220]
    if hl:
        try:
            c = c.replace(hl, '<mark>' + hl + '</mark>')
        except Exception:
            pass
    tags_html = "".join(f'<span class="tag">{t}</span>' for t in (tags or [])[:4])
    created_s = (created or "")[:19].replace("T", " ")
    return (f'<div class="card"><div>{c}</div>'
            f'<div class="meta">{mid[:14]} · {cat} · {created_s} {tags_html}</div></div>')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8010)
    args = ap.parse_args()

    from fastapi import FastAPI, Request
    sys.path.insert(0, _TRINITY_ROOT)
    from trinity import Trinity
    _mem = Trinity(adapter="sqlite")  # 2026-08-27: 引擎读取（解密 content，检索语义）
    from fastapi.responses import HTMLResponse
    import uvicorn

    app = FastAPI(title="Trinity Memory Stream")

    @app.get("/", response_class=HTMLResponse)
    async def index(q: str = "", cat: str = ""):
        conn = _conn()
        search_html = ""
        if q:
            # 2026-08-27: 引擎检索（解密 + 语义）——SQL LIKE 对密文 content 无效
            _sr = _mem.search(query=q, mode="keyword", top_k=10)
            _srows = _sr.get("results", []) if isinstance(_sr, dict) else []
            _scards = "".join(
                f'<div class="card"><div>{_snippet(r.get("content") or "", q)}</div>'
                f'<div class="meta">{str(r.get("memory_id", ""))[:14]} · {r.get("category")} · {str(r.get("created_at", ""))[:19].replace("T", " ")}</div></div>'
                for r in _srows)
            search_html = "<h2>检索结果</h2>" + _scards if _srows else "<h2>检索结果</h2><p>无命中</p>"
        # 类别下拉选项（2026-08-27 UI 增强）
        _cats = conn.execute("SELECT DISTINCT category FROM memories WHERE status='active' ORDER BY category LIMIT 40").fetchall()
        cat_opts = '<option value="">全部类别</option>' + "".join(
            f'<option value="{c}"{" selected" if c == cat else ""}>{c}</option>' for (c,) in _cats)
        # 统计区块（2026-08-27 UI 增强）
        total = conn.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0]
        cats = conn.execute("SELECT category, count(*) FROM memories WHERE status='active' GROUP BY category ORDER BY 2 DESC LIMIT 5").fetchall()
        stats_html = "<p>活跃记忆 <b>" + str(total) + "</b> 条 · 类别: " + " · ".join(f"{c}({n})" for c, n in cats) + "</p>"
        # 热门查询
        hot_rows = conn.execute(
            "SELECT details FROM audit_log WHERE action IN ('search','search_hybrid') AND timestamp >= ? LIMIT 400",
            ((datetime.utcnow() - __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S"),)).fetchall()
        qmap = {}
        for (dd,) in hot_rows:
            try:
                ddd = json.loads(dd)
                qq = str(ddd.get("query", ""))[:50]
                if qq:
                    qmap[qq] = qmap.get(qq, 0) + 1
            except Exception:
                pass
        hot_html = "".join(f'<div class="query">{q} <b>x{n}</b></div>' for q, n in sorted(qmap.items(), key=lambda x: -x[1])[:8]) or "<p>无</p>"
        # 时间线分组（按天）+ 类别过滤（2026-08-27 UI 增强）
        _cat_where = "AND category = ?" if cat else ""
        _cat_params = (cat,) if cat else ()
        # 2026-08-27: 引擎读取（get_all_memories 解密）
        _all = _mem._adapter.get_all_memories(limit=200, offset=0)
        if cat:
            _all = [r for r in _all if r.get("category") == cat]
        groups = {}
        for r in _all:
            day = str(r.get("created_at") or "")[:10]
            groups.setdefault(day, []).append(r)
        stream = ""
        for day in sorted(groups, reverse=True):
            stream += f'<h3>{day}</h3>'
            stream += "".join(
                f'<div class="card"><div>{_safe((r.get("content") or "")[:220], "")}</div>'
                f'<div class="meta">{str(r.get("memory_id", ""))[:14]} · {r.get("category")} · {str(r.get("created_at", ""))[:19].replace("T", " ")} · {("".join(t + " " for t in (r.get("tags") or [])[:3]))}</div></div>'
                for r in groups[day][:40])
        conn.close()
        page = PAGE.replace("__STATS__", stats_html).replace("__HOT__", hot_html).replace("__CATOPTIONS__", cat_opts)
        page = page.replace("__SEARCH__", search_html).replace("__STREAM__", stream)
        return HTMLResponse(page.replace("__Q__", q.replace('"', "&quot;")).replace("__CAT__", cat.replace('"', "&quot;")))

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
