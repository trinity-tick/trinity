# -*- coding: utf-8 -*-
"""usage_feedback.py — 使用伙伴闭环（2026-08-27）：使用统计反馈给进化引擎。

聚合审计/命中数据，生成使用反馈报告（analysis 类目记忆）：
  - 近 N 天使用概况（search 查询数 / write 数 / 平均命中）
  - 热门查询 TOP（query + hits）
  - 高频记忆 TOP（access_count 最高）
  - 闲置记忆（active、0 访问、超 D 天）
反馈报告可被 evolution ANALYZE 检索到（自进化获得'使用'输入——打破自转）。

用法:
    python scripts/usage_feedback.py [--days 7] [--top 10] [--idle-days 30] [--no-ingest]
"""
import json
import os
import sys
import time
import argparse
from datetime import datetime, timedelta

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--idle-days", type=int, default=30)
    ap.add_argument("--no-ingest", action="store_true")
    args = ap.parse_args()

    import sqlite3
    db = os.path.expanduser("~/.trinity/store/trinity_store.db")
    if not os.path.exists(db):
        print("store missing:", db)
        return 1
    conn = sqlite3.connect(db, timeout=15)
    # 2026-08-27：审计 created_at 是 ISO 文本（如 2026-08-26T18:05:25），用 ISO 比较
    since_iso = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%S")

    # 1) 审计概况（action=search 的 details 是 JSON）
    try:
        rows = conn.execute(
            "SELECT action, count(*) FROM audit_log WHERE timestamp >= ? GROUP BY action ORDER BY 2 DESC",
            (since_iso,)).fetchall()
        actions = {a: c for a, c in rows}
    except Exception:
        actions = {}
    n_search = actions.get("search", 0) + actions.get("search_hybrid", 0)
    n_write = actions.get("memory.write", 0) + actions.get("write", 0) + actions.get("ingest", 0)

    # 2) 热门查询（从 search 审计 details JSON 解析 query）
    hot_q = []
    try:
        qrows = conn.execute(
            "SELECT details FROM audit_log WHERE action IN ('search','search_hybrid') AND timestamp >= ? LIMIT 500",
            (since_iso,)).fetchall()
        qmap = {}
        for (d,) in qrows:
            try:
                dd = json.loads(d)
                q = str(dd.get("query", ""))[:60]
                if q:
                    qmap[q] = qmap.get(q, 0) + 1
            except Exception:
                pass
        hot_q = sorted(qmap.items(), key=lambda x: -x[1])[: args.top]
    except Exception:
        pass

    # 3) 高频记忆
    try:
        hot_mem = conn.execute(
            "SELECT memory_id, access_count, substr(content,1,80) FROM memories "
            "WHERE status='active' AND access_count > 0 ORDER BY access_count DESC LIMIT ?",
            (args.top,)).fetchall()
    except Exception:
        hot_mem = []

    # 4) 闲置记忆
    try:
        idle = conn.execute(
            "SELECT count(*) FROM memories WHERE status='active' AND (access_count=0 OR access_count IS NULL) "
            "AND (last_accessed_at IS NULL OR last_accessed_at < ?)",
            (since - (args.idle_days - args.days) * 86400,)).fetchone()[0]
    except Exception:
        idle = 0
    total_active = conn.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0]
    conn.close()

    report = (
        f"[analysis] {datetime.now().strftime('%Y-%m-%d')} 使用反馈（近 {args.days} 天）" + chr(10) +
        f"- 使用概况: search={n_search} 次 / write={n_write} 次 / 活跃记忆 {total_active}" + chr(10) +
        f"- 热门查询 TOP{args.top}: " + ("; ".join(f"{q}(x{c})" for q, c in hot_q) if hot_q else "无") + chr(10) +
        f"- 高频记忆 TOP{args.top}: " + ("; ".join(f"{m[:10]}..({c}次)" for m, c, _ in hot_mem) if hot_mem else "无") + chr(10) +
        f"- 闲置记忆(>{args.idle_days}天未访问): {idle} 条（占 active {round(idle / max(1, total_active) * 100, 1)}%）" + chr(10) +
        f"- 洞察: " + (
            "使用活跃，反馈闭环运转" if n_search > 10 else
            "使用偏少——建议接入更多 agent 工作流（引擎超前于使用）") + chr(10) +
        f"- 建议: 高频记忆入巩固候选；闲置记忆评估归档（decay 候选）；热门查询补充知识源/别名"
    )
    print(report)
    if not args.no_ingest:
        sys.path.insert(0, _TRINITY_ROOT)
        from trinity import Trinity
        mem = Trinity(adapter="sqlite")
        r = mem.ingest(report, agent_id="usage-feedback", category="analysis",
                       tags=["usage-feedback", "evolution-input"], postprocess=False)
        print("ingested:", (r.get("memory_id") if isinstance(r, dict) else r))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
