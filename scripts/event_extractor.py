#!/usr/bin/env python3
"""event_extractor.py — 事件中心时态图谱提取器（2026-09，EXECUTION 105.13）

对标 Graphiti（Zep）：实体+事件双层——事件是时态锚定的原子单元。
Trinity 落地为自包含事件图谱表 event_graph（actor/action/object 结构化）。

数据源：dsh_events（工具错误/目标完成）+ 感知记忆 + 决策/事故记忆。
提取：LLM 批量结构化（一次 5 条 → JSON 数组），幂等（source_id 唯一）。

用法:
  python scripts/event_extractor.py --dry-run --limit 5
  python scripts/event_extractor.py --limit 20
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trinity.brain.value_encoder import llm_chat  # noqa: E402

SQLITE_DB = os.path.expanduser("~/.trinity/store/trinity_store.db")
NL = chr(10)


def connect_pg():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
        dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
        user=os.environ.get("TRINITY_PG_USER", "trinity"),
        password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
    )


def collect_sources(limit):
    sources = []
    try:
        conn = sqlite3.connect(SQLITE_DB)
        cur = conn.cursor()
        cur.execute("SELECT type, payload, time FROM dsh_events ORDER BY time DESC LIMIT 3000")
        for etype, payload, ts in cur.fetchall():
            try:
                data = json.loads(payload)
            except Exception:
                continue
            text = None
            st = None
            if etype == "tool/result" and data.get("error"):
                text = "工具执行错误: " + str(data.get("error"))[:150]
                st = "tool_error"
            elif etype == "goal/write":
                stt = str(data.get("status") or data.get("state") or "")
                if "complete" in stt.lower() or stt.lower() in ("done", "completed"):
                    text = "目标完成: " + str(data.get("objective") or data.get("title") or "")[:150]
                    st = "goal_done"
            if text and st:
                sources.append((ts, st, "dsh:" + str(ts), text))
        conn.close()
    except Exception:
        pass
    try:
        # 2026-09：用 adapter 读取（存储加密默认开——content 密文落盘，
        # adapter 自动解密；直连 SQL 会读到 enc:v1: 密文）
        from trinity.adapters.postgresql import PostgreSQLAdapter
        ad = PostgreSQLAdapter(auto_connect=True)
        mems = ad.get_all_memories(limit=limit * 4)
        for m in mems or []:
            cat = str(m.get("category", ""))
            cid = str(m.get("memory_id", ""))
            content = str(m.get("content", ""))
            # 存储加密默认开：adapter 返回密文，需显式解密（enc:v1: 前缀）
            if content.startswith("enc:v1:"):
                try:
                    content = ad._decrypt_content(content)
                except Exception:
                    content = ""
            ts = str(m.get("created_at", ""))
            if cat == "perception" and content:
                sources.append((ts, "perception", cid, content[:200]))
            elif cat in ("decision", "incident") and content:
                sources.append((ts, cat, cid, content[:200]))
    except Exception:
        pass
    seen = set()
    uniq = []
    for ts, st, sid, text in sources:
        if sid in seen:
            continue
        seen.add(sid)
        uniq.append((ts, st, sid, text))
    uniq.sort(key=lambda x: str(x[0]), reverse=True)
    return uniq[:limit]


def rule_tool_event(text):
    """规则提取工具错误事件（不依赖 LLM）：从 '工具执行错误: {...}' 提取 name/code。"""
    t = str(text)
    name = ""
    code = ""
    try:
        if "name" in t:
            part = t.split("name", 1)[1].split(":", 1)[1].strip()
            name = part.strip("'\"}{ ,").split(",")[0].strip()
    except Exception:
        pass
    try:
        if "code" in t:
            part = t.split("code", 1)[1].split(":", 1)[1].strip()
            code = part.strip("'\"}{ ,").split("}")[0].strip()
    except Exception:
        pass
    if not name:
        name = "Tool"
    actor = name[:-5] if name.endswith("Error") else name
    return {
        "actor": actor[:64],
        "action": "执行失败",
        "object": code[:64] if code else name[:64],
        "summary": t[:200],
    }


def extract_events(items):
    if not items:
        return []
    batch = []
    for i, (_ts, st, sid, text) in enumerate(items):
        batch.append("[" + str(i) + "] (" + str(st) + ") " + str(text)[:150])
    prompt = (
        "你是记忆系统的事件提取器。从以下记录中提取【事件】（谁-做了什么-关于什么，一句话总结）。"
        + NL + "只输出 JSON 数组（长度与输入相同）："
        + NL + '[{"actor":"主体","action":"动作","object":"对象","summary":"一句话事件总结"}, ...]'
        + NL + "无法构成事件的输出 null。"
        + NL + "输入：" + NL + NL.join(batch)
    )
    raw = llm_chat(prompt, max_tokens=1200, temperature=0.2)
    if not raw:
        return [None] * len(items)
    try:
        s = raw.strip()
        fence = chr(96) * 3
        if s.startswith(fence):
            s = s.split(NL, 1)[-1]
            if s.endswith(fence):
                s = s[:-3]
        if s.startswith("json"):
            s = s[4:].lstrip()
        data = json.loads(s)
        if not isinstance(data, list):
            return [None] * len(items)
        out = []
        for entry in data[:len(items)]:
            if not isinstance(entry, dict) or not entry.get("action"):
                out.append(None)
            else:
                out.append({
                    "actor": str(entry.get("actor", "system"))[:64],
                    "action": str(entry.get("action", ""))[:64],
                    "object": str(entry.get("object", ""))[:128],
                    "summary": str(entry.get("summary", ""))[:300],
                })
        while len(out) < len(items):
            out.append(None)
        return out
    except Exception as e:  # noqa: BLE001
        print("extract parse failed: " + str(e)[:80])
        return [None] * len(items)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sources = collect_sources(args.limit)
    print("sources: " + str(len(sources)))
    if not sources:
        return 0
    events = extract_events(sources)
    conn = connect_pg()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS event_graph (
            event_id    SERIAL PRIMARY KEY,
            source_id   VARCHAR(128) UNIQUE,
            source_type VARCHAR(32),
            ts          TIMESTAMPTZ,
            actor       VARCHAR(64),
            action      VARCHAR(64),
            object      VARCHAR(128),
            summary     TEXT,
            entities    JSONB NOT NULL DEFAULT '[]',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    inserted = 0
    for (ts, st, sid, text), ev in zip(sources, events):
        if not ev and st == "tool_error":
            ev = rule_tool_event(str(text))  # 规则兜底（错误记录信息不足时 LLM 判 null）
        if not ev:
            continue
        # dsh_events.time 为 epoch 毫秒数值 → ISO（PG timestamptz 友好）
        if isinstance(ts, (int, float)):
            try:
                ts = datetime.fromtimestamp(float(ts) / 1000.0,
                                            tz=timezone.utc).isoformat()
            except Exception:
                ts = None
        if args.dry_run:
            print("  [dry] (" + str(st) + ") " + str(ev["actor"]) + " "
                  + str(ev["action"]) + " " + str(ev["object"])[:30] + " | "
                  + str(ev["summary"])[:50])
            inserted += 1
            continue
        cur.execute("""
            INSERT INTO event_graph (source_id, source_type, ts, actor, action, object, summary)
            VALUES (%s, %s, %s::timestamptz, %s, %s, %s, %s)
            ON CONFLICT (source_id) DO NOTHING
        """, (sid, st, ts, ev["actor"], ev["action"], ev["object"], ev["summary"]))
        if cur.rowcount > 0:
            inserted += 1
    conn.close()
    print("DONE: inserted=" + str(inserted))
    return 0


if __name__ == "__main__":
    sys.exit(main())
