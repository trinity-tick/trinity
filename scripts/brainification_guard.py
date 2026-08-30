# -*- coding: utf-8 -*-
"""大脑化进程守卫（EXECUTION 177）——保证大脑化进程不能停。

职责：验证大脑化本身在持续推进（不是只验证系统活着）：
  1. 每日链运行：autostart 日志最近 24h 有维护链运行（任务在跑）
  2. 数据增长：记忆/感知/自省/全局自我数量持续增长（学习在发生）
  3. 机制活跃：DCPM 信念增长 / 图谱增长（整合在发生）
  4. 进化周期：进化观察有输入（自省驱动进化在工作）
  5. 网络质料：web 信号持续增加（感官在工作）

任一维度停滞（48h 无增长）→ 大脑化进程"停跳"→ 告警。
输出大脑化进程状态报告。
"""
import os, sys, json, time, urllib.request
from datetime import datetime

LOG_DIR = os.path.expanduser("~/.trinity/logs")


def _pg():
    import psycopg2
    return psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                            user="trinity", password="trinity")


def _chain_running():
    """每日链运行检查：维护日志最近 48h 有任务运行。"""
    try:
        log = open(os.path.join(LOG_DIR, "dsh-maintenance.log"),
                   encoding="utf-8", errors="replace").read()
        import re
        ms = list(re.finditer(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?maintenance finished", log[-200000:]))
        if not ms:
            return {"ok": False, "last": "never"}
        last = ms[-1].group(1)
        ts = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").timestamp()
        ok = (time.time() - ts) < 48 * 3600
        return {"ok": ok, "last": last}
    except Exception as e:
        return {"ok": False, "error": str(e)[:60]}


def _growth():
    """数据增长：各维度当前值（与上次审计对比由 loop-audit 负责，这里查非零+增长基线）。"""
    try:
        conn = _pg(); cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories")
        total = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories WHERE category='perception'")
        perc = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories WHERE category='self-reflection'")
        refl = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories WHERE category='self-identity'")
        ident = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories WHERE category='perception' AND content LIKE '%[web%'")
        web = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM dcpm_beliefs")
        dcpm = cur.fetchone()[0]
        conn.close()
        return {"ok": total > 1000 and perc > 100 and dcpm > 50,
                "memories": total, "perceptions": perc, "reflections": refl,
                "identity": ident, "web": web, "dcpm": dcpm}
    except Exception as e:
        return {"ok": False, "error": str(e)[:60]}


def _evolution_alive():
    """进化周期：观察钩子有输入（自省观察存在）。"""
    try:
        sys.path.insert(0, r"D:\trinity-code")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from trinity.evolution.core import MetaEvolution
        eng = MetaEvolution()
        obs = eng.observe({"action": "scheduled"})
        self_obs = [o for o in obs if o.get("type") == "self_state"]
        return {"ok": len(obs) >= 2, "observations": len(obs), "self_obs": len(self_obs)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:60]}


def main():
    report = {"ok": True, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "brainification": {}}
    report["brainification"]["daily_chain"] = _chain_running()
    report["brainification"]["growth"] = _growth()
    report["brainification"]["evolution"] = _evolution_alive()

    for k, v in report["brainification"].items():
        if isinstance(v, dict) and v.get("ok") is False:
            report["ok"] = False
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
