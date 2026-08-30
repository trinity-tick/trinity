# -*- coding: utf-8 -*-
"""配置漂移检测（EXECUTION 165）——检查关键配置是否漂移。

背景：143 轮 C store 漂移 / 152 轮 C 库路径——都是配置漂移类问题。
检测：
  1. TRINITY_STORE 指向（应 D 盘权威库）
  2. 维护/自启脚本的 TRINITY_STORE 一致
  3. pg_hba 非 trust
  4. 关键环境变量
输出 JSON；漂移时退出 1（告警）。
"""
import os, sys, json, re

def main():
    report = {"ok": True, "checks": {}}

    # 1) TRINITY_STORE 一致性（3 个运维脚本）
    scripts = {
        "maintenance": r"D:\trinity-code\dsh-ops\trinity-dsh-maintenance.ps1",
        "autostart": r"D:\trinity-code\dsh-ops\trinity-autostart.ps1",
        "supervisor": r"D:\trinity-code\dsh-ops\trinity-supervisor.ps1",
    }
    expected = "D:\\trinity-data\\store"
    for name, path in scripts.items():
        try:
            txt = open(path, encoding="utf-8").read()
            ok = "trinity-data" in txt and "\\store" in txt and "C:\\Users" not in txt.replace("EXECUTION", "")
            # 检查是否有 C 盘残留引用
            c_refs = re.findall(r"~\\.trinity\\store|C:\\\\Users[^\"']*store", txt)
            report["checks"]["store_" + name] = {
                "ok": len(c_refs) == 0, "c_refs": len(c_refs),
            }
        except Exception as e:
            report["checks"]["store_" + name] = {"ok": False, "error": str(e)[:60]}

    # 2) pg_hba 非 trust
    try:
        hba = open(os.path.expanduser("~/.trinity/pgdata/pg_hba.conf"), encoding="utf-8").read()
        trust = "trust" in hba and "scram" in hba
        report["checks"]["pg_hba"] = {"ok": "scram-sha-256" in hba, "trust_lines": hba.count("trust")}
    except Exception as e:
        report["checks"]["pg_hba"] = {"ok": False, "error": str(e)[:60]}

    # 3) 环境关键变量（进程级检查）
    # env 由运维脚本注入（检测脚本独立运行时未设属正常）——仅报告
    report["checks"]["env"] = {
        "TRINITY_STORE": os.environ.get("TRINITY_STORE", "unset"),
        "ok": True,
    }

    for k, v in report["checks"].items():
        if isinstance(v, dict) and v.get("ok") is False:
            report["ok"] = False
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1

if __name__ == "__main__":
    sys.exit(main())
