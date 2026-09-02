#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用户全局 AGENTS.md 的 Trinity 自传快照刷新（2026-09-01，自传注入实现）。"""
import json
import os
import sys
import urllib.request


def _txn_count():
    try:
        import json
        p = os.path.join(os.path.expanduser("~/.trinity"), "memory_market_trust_exchange.json")
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            return len(d.get("transactions") or d.get("history") or [])
    except Exception:
        pass
    return 0


def main() -> int:
    path = os.path.expanduser("~/.dsh/AGENTS.md")
    if not os.path.exists(path):
        print("DASH-AGENTS: %s missing" % path)
        return 1
    lines = []
    try:
        with urllib.request.urlopen("http://127.0.0.1:8001/health", timeout=8) as resp:
            h = json.loads(resp.read().decode())
        lines.append("- API: v=%s %s engine=%s" % (h.get("version"), h.get("status"),
                                                   (h.get("components") or {}).get("engine")))
    except Exception:
        lines.append("- API: 不可达")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8001/market/orderbook", timeout=8) as resp:
            ob = json.loads(resp.read().decode())
        lines.append("- 记忆市场: %d 个活跃资产可购买、%d 笔成交（trinity_market_search/buy）" % ((ob or {}).get("count", 0), (ob or {}).get("total_trades", 0) or _txn_count()))
    except Exception:
        pass
    hist_p = os.path.join(os.path.expanduser("~/.trinity"), "bench-results", "metrics-history.json")
    if os.path.exists(hist_p):
        try:
            hist = json.load(open(hist_p, encoding="utf-8"))
            last = hist[-1]
            lines.append("- 生成质量预测: 下次 AnswerAcc ~= %.3f（历史 %d 点）" % (last.get("predicted_next_acc", 0), len(hist)))
        except Exception:
            pass
    evo_p = os.path.expanduser("~/.trinity/evolution_state.json")
    if os.path.exists(evo_p):
        try:
            evo = json.load(open(evo_p, encoding="utf-8"))
            corr = evo.get("corrections_log") or []
            lines.append("- 元认知: corrections %d 条（%d resolved）" % (len(corr), sum(1 for c in corr if c.get("status") == "resolved")))
        except Exception:
            pass
    store = os.path.expanduser("~/.trinity/store/trinity_store.db")
    try:
        import sqlite3
        c = sqlite3.connect(store, timeout=15)
        rows = c.execute(
            "SELECT agent_id, COUNT(*) FROM memories WHERE status='active' AND agent_id IS NOT NULL "
            "GROUP BY agent_id ORDER BY 2 DESC LIMIT 5").fetchall()
        c.close()
        if rows:
            lines.append("- 活跃画像: " + ", ".join("%s(%d)" % (a, n) for a, n in rows))
    except Exception:
        pass
    import datetime
    lines.append("- 快照时间: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    block = "\n".join(lines)
    with open(path, encoding="utf-8") as f:
        content = f.read()
    start = content.find("<!-- TRINITY_SNAPSHOT -->")
    end = content.find("<!-- /TRINITY_SNAPSHOT -->")
    if start < 0 or end < 0:
        print("DASH-AGENTS: snapshot markers missing")
        return 1
    new_content = content[:start] + "<!-- TRINITY_SNAPSHOT -->\n" + block + "\n" + content[end:]
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)
    print("DASH-AGENTS: refreshed (%d lines)" % len(block.splitlines()))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
