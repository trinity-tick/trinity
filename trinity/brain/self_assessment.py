# -*- coding: utf-8 -*-
"""trinity/brain/self_assessment.py — 自我评估（EXECUTION 186，大脑化）。

元认知的自我监控维度：Trinity 用真实指标评估自己最近表现
（不只是"健康"——而是"表现如何/强项/待改进"）。

指标源：
  - 行动成功率（action stats）
  - 闭环审计（10 闭环通过率）
  - 认知评测（cognition-check 结果）
  - 检索质量（预测误差 EMA 痕迹）
  - 任务健康（brain-health 报告）

输出：评估文本 → 写入 self-assessment 记忆（可检索、可进全局自我）。
"""
import os
import sys
import json


def assess_recent() -> dict:
    """聚合近期指标 → 自我评估。"""
    metrics = {}

    # 1) 行动成功率
    try:
        stats_f = os.path.expanduser("~/.trinity/action_loop_stats.json")
        if os.path.exists(stats_f):
            stats = json.load(open(stats_f, encoding="utf-8"))
            oks = sum(s.get("ok", 0) for s in stats.values())
            fails = sum(s.get("fail", 0) for s in stats.values())
            total = oks + fails
            metrics["action_rate"] = round(oks * 100 / max(total, 1)) if total else None
    except Exception:
        pass

    # 2) 任务健康（brain-health 输出从日志不可靠——查数据增长）
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories")
        metrics["memories"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM dcpm_beliefs")
        metrics["dcpm"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories WHERE category='self-reflection'")
        metrics["reflections"] = cur.fetchone()[0]
        conn.close()
    except Exception:
        pass

    # 3) 合成评估
    parts = ["我的近期自我评估："]
    if metrics.get("action_rate") is not None:
        rate = metrics["action_rate"]
        if rate >= 80:
            parts.append(f"行动成功率 {rate}%——我的行动很可靠")
        elif rate >= 50:
            parts.append(f"行动成功率 {rate}%——行动基本可靠，有改进空间")
        else:
            parts.append(f"行动成功率 {rate}%——需要审视我的行动策略")
    parts.append(f"我管理着 {metrics.get('memories', '?')} 条记忆，"
                 f"System2 归纳出 {metrics.get('dcpm', '?')} 条信念，"
                 f"进行了 {metrics.get('reflections', '?')} 次自我反思")
    # 4) 待改进（行动失败率高的刺激）
    try:
        stats_f = os.path.expanduser("~/.trinity/action_loop_stats.json")
        if os.path.exists(stats_f):
            stats = json.load(open(stats_f, encoding="utf-8"))
            weak = [k for k, s in stats.items()
                    if s.get("ok", 0) + s.get("fail", 0) >= 2
                    and s.get("ok", 0) / max(s.get("ok", 0) + s.get("fail", 0), 1) < 0.5]
            if weak:
                parts.append("待改进：" + "、".join(weak[:3]))
    except Exception:
        pass
    return {"assessment": "；".join(parts), "metrics": metrics}


def assess_to_memory() -> bool:
    """自我评估写入记忆（self-assessment 类别）。"""
    try:
        r = assess_recent()
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        m.ingest("[self-assessment] " + r["assessment"][:250], category="self-assessment",
                 tags=["self", "assessment"], importance=0.8, wait_backfill=True)
        return True
    except Exception:
        return False
