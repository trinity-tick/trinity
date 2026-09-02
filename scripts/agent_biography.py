#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自传式记忆（2026-09-01，大脑化层3：自我模型——"这个 agent 是谁、做过什么、偏好如何演化"）

按 agent_id 聚合时间线视图：
  1. 身份：session 数、记忆数、活跃状态
  2. 时间线：目标（active/completed 按时间）、todo 活动、最近事件
  3. 记忆画像：类目分布、标签 Top、importance 分布
  4. 偏好演化：evolution active_preferences（跨会话偏好）
  5. 技能：skill_scores（evolution state）

用法: python scripts/agent_biography.py [--agent <id>] [--top 8] [--json]
"""
import argparse
import json
import os
import sqlite3
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="", help="指定 agent_id（空=Top N 概览）")
    ap.add_argument("--top", type=int, default=8, help="概览显示前 N 个 agent")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    db = os.environ.get("TRINITY_STORE_DB") or os.path.expanduser("~/.trinity/store/trinity_store.db")
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row

    def _q(sql, *p):
        return conn.execute(sql, p).fetchall()

    if args.agent:
        aid = args.agent
        sessions = _q("SELECT COUNT(*) c FROM dsh_sessions WHERE agent_id=?", aid)
        mems = _q("SELECT category, COUNT(*) c FROM memories WHERE agent_id=? AND status='active' GROUP BY category ORDER BY 2 DESC", aid)
        tags = _q("SELECT tags, COUNT(*) c FROM memories WHERE agent_id=? AND status='active' AND tags IS NOT NULL GROUP BY tags ORDER BY 2 DESC LIMIT 8", aid)
        imp = _q("SELECT ROUND(importance*10)/10 b, COUNT(*) c FROM memories WHERE agent_id=? AND status='active' GROUP BY b ORDER BY 1", aid)
        # dsh_goals 无 agent_id 列（结构层契约）；目标画像用全局统计
        goals = _q("SELECT status, COUNT(*) c FROM dsh_goals GROUP BY status")
        recent = _q("SELECT type, time FROM dsh_events e JOIN dsh_sessions s ON e.session_id=s.session_id WHERE s.agent_id=? ORDER BY e.time DESC LIMIT 10", aid)
        bio = {
            "agent_id": aid,
            "sessions": sessions[0][0] if sessions else 0,
            "active_memories": sum(r[1] for r in mems),
            "categories": {r[0]: r[1] for r in mems},
            "top_tags": {r[0]: r[1] for r in tags},
            "importance_dist": {str(r[0]): r[1] for r in imp},
            "goals": {r[0]: r[1] for r in goals},
            "recent_events": [{"type": r[0], "t": r[1]} for r in recent],
        }
        # 偏好/技能（evolution state）
        evo_path = os.path.expanduser("~/.trinity/evolution_state.json")
        if os.path.exists(evo_path):
            try:
                evo = json.load(open(evo_path, encoding="utf-8"))
                bio["preferences"] = {k: v for k, v in (evo.get("active_preferences") or {}).items()
                                      if k.startswith("dsh-" + aid[:8]) or True}
                bio["skill_scores"] = {k: v for k, v in (evo.get("skill_scores") or {}).items()}
            except Exception:
                pass
        out = bio
    else:
        rows = _q("SELECT agent_id, COUNT(*) c FROM memories WHERE status='active' AND agent_id IS NOT NULL GROUP BY agent_id ORDER BY 2 DESC LIMIT ?", args.top)
        out = {"top_agents": [{"agent_id": r[0], "active_memories": r[1]} for r in rows],
               "total_agents": _q("SELECT COUNT(DISTINCT agent_id) FROM memories WHERE agent_id IS NOT NULL")[0][0]}

    conn.close()
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        if args.agent:
            print("=== 自传: %s ===" % out["agent_id"])
            print("会话数: %s | 活跃记忆: %s" % (out["sessions"], out["active_memories"]))
            print("类目: %s" % json.dumps(out["categories"], ensure_ascii=False))
            print("Top 标签: %s" % json.dumps(out.get("top_tags", {}), ensure_ascii=False))
            print("importance 分布: %s" % json.dumps(out.get("importance_dist", {})))
            print("目标: %s" % json.dumps(out.get("goals", {})))
            if out.get("preferences"):
                print("偏好(前10): %s" % json.dumps(dict(list(out["preferences"].items())[:10]), ensure_ascii=False))
            print("最近事件(前5): %s" % json.dumps(out["recent_events"][:5], ensure_ascii=False))
        else:
            print("=== 活跃 Agent 概览（共 %d）===" % out["total_agents"])
            for a in out["top_agents"]:
                print("  %-40s %d 记忆" % (a["agent_id"], a["active_memories"]))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
