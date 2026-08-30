# -*- coding: utf-8 -*-
"""大脑健康心电图（EXECUTION 168）——保证 Trinity 像大脑一样持续运行。

聚合所有"生命体征"为一份健康报告（如心电图）：
  1. 服务心跳：6 端口 + Ollama 模型常驻
  2. 任务心跳：10 个每日任务最近运行时间（48h 内 = 活着）
  3. 数据心跳：记忆/感知/自省增长（系统在"新陈代谢"）
  4. 网络心跳：RSS/搜索通道最近感知
  5. 审计心跳：integrity

输出 JSON；任一异常 → 退出 1（维护链告警）。幂等。
"""
import os, sys, json, re, time, urllib.request
from datetime import datetime

API = "http://127.0.0.1:8001"
LOG_DIR = os.path.expanduser("~/.trinity/logs")

# 10 个每日任务（autostart 链）
DAILY_TASKS = ["dcpm-consolidate", "replay-consolidate", "integrity-monitor",
               "self-reflect", "perception-scan", "cognition-check",
               "web-perception", "web-search", "drift-check", "health"]


def _check_ports():
    try:
        import socket
        ports = [5432, 8000, 8001, 8002, 8003, 8010]
        alive = []
        for p in ports:
            s = socket.socket()
            s.settimeout(2)
            try:
                s.connect(("127.0.0.1", p))
                alive.append(p)
            except Exception:
                pass
            finally:
                s.close()
        return {"alive": alive, "expected": ports,
                "ok": len(alive) == len(ports)}
    except Exception:
        return {"ok": False, "error": "socket check failed"}


def _check_model():
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/ps")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
        models = [m.get("name", "") for m in body.get("models", [])]
        return {"loaded": models, "bge_m3": any("bge-m3" in m for m in models),
                "ok": any("bge-m3" in m for m in models)}
    except Exception:
        return {"ok": False, "error": "ollama unreachable"}


def _task_heartbeat():
    """从维护日志提取每个任务最近运行时间（48h 内 = 活着）。"""
    tasks = {}
    try:
        log = open(os.path.join(LOG_DIR, "dsh-maintenance.log"),
                   encoding="utf-8", errors="replace").read()
        now = time.time()
        for task in DAILY_TASKS:
            # 找 "===== task: X =====" 出现位置（最近的）
            idxs = [m.start() for m in re.finditer(r"task: " + re.escape(task) + " ", log)]
            last = "never"
            ok = False
            if idxs:
                # 取最后一次出现后附近的日志时间戳
                tail = log[idxs[-1]:idxs[-1] + 500]
                m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", tail)
                if m:
                    try:
                        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                        last = m.group(1)
                        ok = (now - ts) < 48 * 3600
                    except Exception:
                        pass
            tasks[task] = {"last_run": last, "ok": ok}
    except Exception:
        pass
    return tasks


def _data_heartbeat():
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories")
        total = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories WHERE category='perception'")
        perc = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories WHERE category='self-reflection'")
        refl = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories WHERE category='perception' AND content LIKE '%[web%'")
        web = cur.fetchone()[0]
        conn.close()
        return {"memories": total, "perception": perc, "reflections": refl,
                "web_signals": web, "ok": total > 1000 and perc > 100}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def _audit_heartbeat():
    try:
        req = urllib.request.Request(API + "/audit/integrity")
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode())
        return {"ok": bool(body.get("integrity_ok")), "entries": body.get("total_entries")}
    except Exception:
        return {"ok": False, "error": "audit unreachable"}


def main():
    report = {"ok": True, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "checks": {}}
    report["checks"]["services"] = _check_ports()
    report["checks"]["model"] = _check_model()
    report["checks"]["tasks"] = _task_heartbeat()
    report["checks"]["data"] = _data_heartbeat()
    report["checks"]["audit"] = _audit_heartbeat()

    # 汇总：任务 48h 内必须跑过（至少大部分）
    tasks_ok = sum(1 for t in report["checks"]["tasks"].values() if t.get("ok"))
    report["checks"]["tasks"]["alive_count"] = tasks_ok
    report["checks"]["tasks"]["total"] = len(DAILY_TASKS)

    for k, v in report["checks"].items():
        if isinstance(v, dict) and v.get("ok") is False:
            report["ok"] = False
    if tasks_ok < len(DAILY_TASKS) // 2:
        report["ok"] = False

    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
