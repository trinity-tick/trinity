# -*- coding: utf-8 -*-
"""trinity/brain/predictive_loop.py — 预测-行动环（EXECUTION 187，大脑化）。

主动推理（free energy principle）：大脑持续预测世界，预测误差
（surprise）驱动行动去调查/修正。Trinity 现在：
  - 预测系统状态（记忆增长/任务成功率/检索性能——EMA 延续）
  - 对比实际 → 计算预测误差（surprise）
  - 误差大 → 触发调查行动（写入审计 + 行动回路刺激）

状态文件：~/.trinity/predictive_state.json（EMA 滚动）
"""
import os
import sys
import json
import time


STATE_FILE = os.path.expanduser("~/.trinity/predictive_state.json")


def _load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ema": {}, "history": []}


def _save(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _metrics() -> dict:
    """当前实际指标。"""
    m = {}
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories")
        m["memories"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM dcpm_beliefs")
        m["dcpm"] = cur.fetchone()[0]
        conn.close()
    except Exception:
        pass
    # 行动成功率
    try:
        sf = os.path.expanduser("~/.trinity/action_loop_stats.json")
        if os.path.exists(sf):
            stats = json.load(open(sf, encoding="utf-8"))
            oks = sum(s.get("ok", 0) for s in stats.values())
            fails = sum(s.get("fail", 0) for s in stats.values())
            t = oks + fails
            m["action_rate"] = round(oks * 100 / max(t, 1)) if t else None
    except Exception:
        pass
    return m


def predict_loop() -> dict:
    """预测 vs 实际 → 误差 → 必要时触发调查。"""
    st = _load()
    ema = st.get("ema", {})
    actual = _metrics()

    surprises = []
    for k, v in actual.items():
        if v is None:
            continue
        prev = ema.get(k)
        if prev is not None:
            # 相对误差（比例）
            err = abs(v - prev) / max(abs(prev), 1)
            surprises.append({"metric": k, "predicted": prev, "actual": v,
                              "error": round(err, 3)})
        # EMA 更新（平滑系数 0.3）
        ema[k] = round((ema.get(k, v) * 0.7) + (v * 0.3), 2)

    st["ema"] = ema
    st["history"].append({"ts": time.time(), "actual": actual,
                          "surprises": surprises})
    st["history"] = st["history"][-100:]
    _save(st)

    # 误差大 → 触发调查（surprise 驱动行动）
    big = [s for s in surprises if s["error"] > 0.3]
    if big:
        try:
            from trinity.adapters.postgresql import PostgreSQLAdapter
            a = PostgreSQLAdapter(auto_connect=True)
            a.connect()
            try:
                a.write_audit_log(memory_id=None, action="prediction_surprise",
                                  agent_id="predictive-loop",
                                  details={"surprises": big})
            finally:
                a.disconnect()
        except Exception:
            pass

    return {"surprises": surprises, "big_surprises": [s["metric"] for s in big],
            "ema": ema}
