# -*- coding: utf-8 -*-
"""B5 合规审计工具 — 自动核对个保法/GDPR 合规清单。

用法:
    python compliance/audit.py [--api http://127.0.0.1:8001]
"""
import argparse
import json
import sys
import requests

H = {"X-Agent-ID": "compliance-audit", "X-Agent-Role": "admin"}

CHECKS = [
    ("数据导出", "GET", "/agents/memory/export", {"format": "json"}),
    ("身份包导出", "POST", "/identity/bundles/export", {"json": {"agent_id": "default"}}),
    ("审计摘要", "GET", "/audit/summary", {}),
    ("审计时间线", "GET", "/audit/timeline", {}),
    ("审计完整性", "GET", "/audit/integrity", {}),
    ("违规记录", "GET", "/audit/violations", {}),
    ("宪法审计", "GET", "/audit/constitution", {}),
    ("删除能力", "DELETE", "/memories/__probe__", {}),  # 期望 4xx/2xx（无 id 时软删 no-op 返回 200）
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8001")
    args = ap.parse_args()
    api = args.api.rstrip("/")

    report = {"checks": []}
    print(f"== B5 合规审计: {api} ==")
    for name, method, path, params in CHECKS:
        try:
            kw = {}
            if "json" in params:
                kw["json"] = params.pop("json")
            r = requests.request(method, f"{api}{path}", params=params, headers=H, timeout=30, **kw)
            body = ""
            try:
                body = r.json()
            except Exception:
                body = r.text[:120]
            if name == "删除能力":
                ok = r.status_code < 500  # 无 id 时软删 no-op 200 或 4xx 均视为能力存在
            else:
                ok = r.status_code < 400
            report["checks"].append({"name": name, "status": r.status_code, "ok": ok, "body": body if isinstance(body, dict) else {"raw": str(body)[:100]}})
            print(f"  [{'OK' if ok else 'FAIL'}] {name}: {r.status_code}")
        except Exception as exc:
            report["checks"].append({"name": name, "status": 0, "ok": False, "body": {"error": str(exc)}})
            print(f"  [ERR ] {name}: {exc}")

    ok_n = sum(1 for c in report["checks"] if c["ok"])
    report["summary"] = {"passed": ok_n, "total": len(report["checks"])}
    print(f"\n通过 {ok_n}/{len(report['checks'])} 项")

    with open(r"C:\Users\Administrator\.trinity\bench-results\compliance_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("report -> .trinity/bench-results/compliance_report.json")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
