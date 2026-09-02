#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AGENTS.md 快照自动刷新（2026-09-01，修复文档漂移——快照曾冻结 8 天）"""
import datetime
import os
import sqlite3
import sys


def main() -> int:
    db = os.environ.get("TRINITY_STORE_DB") or os.path.expanduser("~/.trinity/store/trinity_store.db")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    agents = os.path.join(repo, "AGENTS.md")

    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    n_sessions = conn.execute("SELECT COUNT(*) FROM dsh_sessions").fetchone()[0]
    n_events = conn.execute("SELECT COUNT(*) FROM dsh_events").fetchone()[0]
    n_goals = conn.execute("SELECT COUNT(*) FROM dsh_goals").fetchone()[0]
    n_todos = conn.execute("SELECT COUNT(*) FROM dsh_todos").fetchone()[0]
    n_schedules = conn.execute("SELECT COUNT(*) FROM dsh_schedules").fetchone()[0]
    goals = conn.execute(
        "SELECT goal_id, status, phase, round, objective FROM dsh_goals "
        "WHERE status='active' ORDER BY created_at").fetchall()
    recent = conn.execute(
        "SELECT session_id, status, title FROM dsh_sessions ORDER BY updated_at DESC LIMIT 5").fetchall()
    conn.close()

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append("## Trinity 记忆层实时快照（生成于 " + now + "，snapshot 任务自动刷新）")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append("| 会话数 | " + str(n_sessions) + " |")
    lines.append("| 结构事件数 | " + str(n_events) + " |")
    lines.append("| 目标数 | " + str(n_goals) + " |")
    lines.append("| Todos | " + str(n_todos) + " |")
    lines.append("| 计划 | " + str(n_schedules) + " |")
    lines.append("")
    lines.append("### 活跃目标（active goals）")
    lines.append("")
    lines.append("| 状态 | 阶段 | 轮次 | 目标 |")
    lines.append("|---|---|---|---|")
    for g in goals:
        obj = (g["objective"] or "").replace("|", "/").replace("\n", " ")[:160]
        lines.append("| " + str(g["status"]) + " | " + str(g["phase"] or "-") + " | "
                     + str(g["round"] or 0) + " | " + obj + " |")
    lines.append("")
    lines.append("### 最近会话（recent sessions）")
    lines.append("")
    for r in recent:
        title = (r["title"] or "(untitled)").replace("|", "/")[:40]
        lines.append("- " + r["session_id"] + " [" + r["status"] + "] " + title)
    lines.append("")
    block = "\n".join(lines)

    text = open(agents, encoding="utf-8").read()
    start = text.find("## Trinity 记忆层实时快照")
    end = text.find("## 1. Trinity 是什么")
    if start == -1 or end == -1 or end <= start:
        print("SNAPSHOT: markers not found (start=%d end=%d) — abort" % (start, end))
        return 1
    new_text = text[:start] + block + "\n\n" + text[end:]
    open(agents, "w", encoding="utf-8").write(new_text)
    print("SNAPSHOT: AGENTS.md refreshed (sessions=%d events=%d goals=%d active=%d)"
          % (n_sessions, n_events, n_goals, len(goals)))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
