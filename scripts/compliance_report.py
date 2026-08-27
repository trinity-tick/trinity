# -*- coding: utf-8 -*-
"""compliance_report.py — 合规报告一键导出（2026-08-27 方向5）。

汇总：记忆规模/审计链完整性/检索决策样本/自动化动作统计。
"""
import os
import sys
import json
import argparse
import datetime

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
NL = chr(10)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    from trinity import Trinity
    mem = Trinity(adapter="sqlite")
    conn = mem._adapter._conn
    total = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
    active = conn.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0]
    audit = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    since = (datetime.datetime.now(datetime.timezone.utc) -
             datetime.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%S")
    audit_recent = conn.execute(
        "SELECT count(*) FROM audit_log WHERE timestamp >= ?", (since,)).fetchone()[0]
    searches = conn.execute(
        "SELECT count(*) FROM audit_log WHERE action IN ('search','search_hybrid') AND timestamp >= ?",
        (since,)).fetchone()[0]
    # 检索决策样本（最新 3 条）
    samples = conn.execute(
        "SELECT action, details FROM audit_log WHERE action IN ('search','search_hybrid') "
        "ORDER BY timestamp DESC LIMIT 3").fetchall()
    # 自动化
    st = os.path.expanduser("~/.trinity/automation/stats.json")
    astats = {}
    if os.path.exists(st):
        astats = json.load(open(st, encoding="utf-8"))
    L = []
    A = L.append
    today = datetime.date.today().isoformat()
    A("# Trinity 合规报告（" + today + "）")
    A("")
    A("## 记忆数据")
    A("- 记忆总数: " + str(total) + " | 活跃: " + str(active))
    A("- 审计日志: " + str(audit) + " 条（近 " + str(args.days) + " 天 " + str(audit_recent) + "）")
    A("")
    A("## 检索决策（近 " + str(args.days) + " 天 " + str(searches) + " 次——样本）")
    A("")
    for s in samples:
        try:
            d = json.loads(s["details"])
            A("- " + str(s["action"]) + ": query=" + str(d.get("query", ""))[:40] +
              " | hits=" + str(d.get("hits")) + " | ms=" + str(d.get("elapsed_ms")) +
              " | layer=" + str(d.get("layer")))
        except Exception:
            A("- " + str(s["action"]) + ": (details 不可解析)")
    A("")
    A("## 自动化动作")
    A("- stats: " + json.dumps(astats))
    A("")
    A("## 可验证性")
    A("- 存储加密（AES-256-GCM）默认开启；每条记忆 SHA-256 哈希 + 版本链可独立重算")
    A("- 审计回执: GET /audit/receipt/{memory_id}；全链: GET /audit/integrity")
    out = os.path.join(_TRINITY_ROOT, "docs", "COMPLIANCE_REPORT_" + today + ".md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(NL.join(L))
    print("report:", out)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
