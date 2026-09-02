#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大脑体检周报（2026-09-01，大脑化层3：记忆系统健康报告自动化）

聚合本周所有评测/健康指标 → 一份周报（.trinity/bench-results/brain-report-<ts>.md）：
  1. 检索质量（quality-gate 最新：R@5/R@10/延迟）
  2. 生成质量（answer_eval 最新：AnswerAcc/类目/gen_gap/empty）
  3. 一致性（reconcile：pg_only/sq_only/hash_mismatch）
  4. 巩固质量（compression-faithfulness 最新：coverage）
  5. 服务健康（/health：status/engine/版本/uptime）
  6. 目标/事件水位（dsh_goals active / dsh_events 计数 + 水位新鲜度）

用法: python scripts/brain_report.py [--out <path>]
"""
import argparse
import datetime
import glob
import json
import os
import sqlite3
import sys
import urllib.request


def _latest(pattern):
    hits = sorted(glob.glob(pattern))
    return hits[-1] if hits else None


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    root = os.path.join(os.path.expanduser("~"), ".trinity", "bench-results")
    os.makedirs(root, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = args.out or os.path.join(root, "brain-report-%s.md" % ts)

    L = []
    L.append("# Trinity 大脑体检周报（%s）" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    L.append("")

    # 1) 检索质量
    gate = _load(_latest(os.path.join(root, "quality-gate-*.json")))
    L.append("## 1. 检索质量（quality-gate）")
    if gate:
        kw = gate.get("keyword") or {}
        kw10 = gate.get("keyword_r10") or {}
        L.append("- R@5=%.3f | R@10=%.3f | p50=%.1fms | gate_ok=%s | ts=%s"
                 % (kw.get("r5", 0), kw10.get("r5", 0), kw.get("p50_ms", 0),
                    gate.get("gate_ok"), gate.get("ts", "?")))
    else:
        L.append("- 无记录（周一自动门禁未跑）")
    L.append("")

    # 2) 生成质量
    ev = _latest(os.path.join("C:/Users/Administrator/trinity/output", "answer_eval_results.json"))
    j = _load(ev)
    L.append("## 2. 生成质量（answer_eval）")
    if j:
        L.append("- AnswerAcc=%.3f | R@5=%.3f | gen_gap=%.3f | empty=%d | cost=$%.3f | ts=%s"
                 % (j.get("AnswerAcc", 0), j.get("R@5", 0), j.get("generation_gap", 0),
                    j.get("empty_answers", -1), j.get("est_cost_usd", 0), j.get("timestamp", "?")))
        cats = j.get("by_category") or {}
        L.append("- 类目: " + ", ".join("%s %.2f" % (c, v.get("AnswerAcc", 0)) for c, v in sorted(cats.items())))
    else:
        L.append("- 无记录（周日自动评测未跑）")
    L.append("")

    # 3) 一致性
    rec = _latest(os.path.join(root, "reconcile-*.json"))
    L.append("## 3. 双库一致性")
    try:
        # 2026-09-01: 用 subprocess 隔离（runpy+redirect 会踩脚本内 sys.stdout.reconfigure）
        import subprocess
        _rp = subprocess.run(
            [sys.executable, "C:/Users/Administrator/trinity/scripts/reconcile_pg_sqlite.py"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        line = [x for x in _rp.stdout.splitlines() if x.startswith("RECONCILE")][-1]
        L.append("- " + line)
    except Exception as e:
        L.append("- reconcile 不可用: %s" % e)
    L.append("")

    # 4) 巩固质量
    ff = _latest(os.path.join(root, "compression-faithfulness-*.json"))
    fj = _load(ff)
    L.append("## 4. 巩固质量（compression faithfulness）")
    if fj:
        L.append("- coverage=%.1f%% (parents=%d) | sentence_retention=%.3f | ts=%s"
                 % (fj.get("coverage", 0) * 100, fj.get("parents", 0),
                    fj.get("sentence_retention", 0), fj.get("ts", "?")))
    else:
        L.append("- 无记录")
    L.append("")

    # 5) 服务健康
    L.append("## 5. 服务健康")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8001/health", timeout=8) as resp:
            h = json.loads(resp.read().decode("utf-8"))
        L.append("- api v=%s status=%s engine=%s uptime=%.1fh"
                 % (h.get("version"), h.get("status"),
                    (h.get("components") or {}).get("engine"), h.get("uptime_seconds", 0) / 3600))
    except Exception as e:
        L.append("- api 不可达: %s" % e)
    L.append("")

    # 6) 目标/事件水位
    try:
        conn = sqlite3.connect(os.path.expanduser("~/.trinity/store/trinity_store.db"), timeout=20)
        active = conn.execute("SELECT COUNT(*) FROM dsh_goals WHERE status='active'").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM dsh_events").fetchone()[0]
        mx = conn.execute("SELECT MAX(time) FROM dsh_events").fetchone()[0]
        conn.close()
        age_h = (datetime.datetime.now().timestamp() * 1000 - float(mx)) / 3600000.0
        # 5.5) 元认知状态（2026-09-01）：corrections 开放/已解决
        try:
            _evo_p = os.path.expanduser("~/.trinity/evolution_state.json")
            if os.path.exists(_evo_p):
                _ev = json.load(open(_evo_p, encoding="utf-8"))
                _corr = _ev.get("corrections_log") or []
                _open = sum(1 for c in _corr if c.get("status") != "resolved")
                L.append("## 5.5 元认知（corrections）")
                L.append("- total=%d | open=%d | resolved=%d" % (len(_corr), _open, len(_corr) - _open))
                L.append("")
        except Exception:
            pass
        # 5.6) 记忆市场状态（2026-09-01）：活跃订单/交易/信誉
        try:
            _ob_p = os.path.join(os.path.expanduser("~/.trinity"), "memory_market_orderbook.json")
            if os.path.exists(_ob_p):
                _obd = json.load(open(_ob_p, encoding="utf-8")) or {}
                _act = sum(1 for d in _obd.values() if d.get("is_active"))
                _tot = len(_obd)
                _tx_p = os.path.join(os.path.expanduser("~/.trinity"), "memory_market_trust_exchange.json")
                _txn = 0
                if os.path.exists(_tx_p):
                    _txd = json.load(open(_tx_p, encoding="utf-8")) or {}
                    _txn = len(_txd.get("transactions") or _txd.get("history") or [])
                L.append("## 5.6 记忆市场")
                L.append("- active orders=%d | total assets=%d | txns=%d" % (_act, _tot, _txn))
                L.append("")
        except Exception:
            pass
        # 5.7) 用量（2026-09-01）：PG 审计 24h 动作（真实调用基线）
        try:
            import psycopg2 as _pg
            _c = _pg.connect(host="127.0.0.1", port=5432, dbname="trinity",
                             user=os.environ.get("TRINITY_PG_USER", "trinity"),
                             password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
                             connect_timeout=8)
            _cur = _c.cursor()
            _cur.execute("SELECT action, COUNT(*) FROM audit_log WHERE timestamp::timestamptz > NOW() - INTERVAL '24 hours' GROUP BY action ORDER BY 2 DESC LIMIT 6")
            _rows = _cur.fetchall()
            _c.close()
            if _rows:
                L.append("## 5.7 用量（24h 审计动作）")
                L.append("- " + " | ".join("%s=%d" % (a, n) for a, n in _rows))
                L.append("")
        except Exception:
            pass
        L.append("## 6. 目标与事件水位")
        L.append("- active goals=%d | dsh_events=%d | 水位 %.1fh 前" % (active, events, age_h))
    except Exception as e:
        L.append("## 6. 目标与事件水位")
        L.append("- 读取失败: %s" % e)

    # 7) 预测→验证（2026-09-01）：历史档案 + 线性趋势预测 + 上次预测误差验证
    _hist_path = os.path.join(root, "metrics-history.json")
    _hist = []
    if os.path.exists(_hist_path):
        try:
            _hist = json.load(open(_hist_path, encoding="utf-8"))
        except Exception:
            _hist = []
    _acc = (j or {}).get("AnswerAcc")
    _r5 = (j or {}).get("R@5")
    if _acc is not None:
        # 验证上轮预测
        _prev = None
        for _h in _hist:
            if _h.get("predicted_next_acc") is not None and not _h.get("prediction_verified"):
                _prev = _h
                break
        L.append("## 7. 预测（AnswerAcc）")
        if _prev is not None:
            _err = abs(float(_prev["predicted_next_acc"]) - float(_acc))
            _prev["prediction_verified"] = True
            _prev["actual_acc"] = _acc
            _prev["error"] = round(_err, 3)
            L.append("- 上次预测验证: 预测 %.3f vs 实际 %.3f（误差 %.3f）" % (_prev["predicted_next_acc"], _acc, _err))
        # 线性趋势预测
        _accs = [float(h["acc"]) for h in _hist if h.get("acc") is not None] + [float(_acc)]
        if len(_accs) >= 3:
            _slope = (_accs[-1] - _accs[0]) / (len(_accs) - 1)
        else:
            _slope = 0.0
        _pred = min(1.0, max(0.0, float(_acc) + _slope))
        _hist.append({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                      "acc": _acc, "r5": _r5,
                      "predicted_next_acc": round(_pred, 3)})
        _hist = _hist[-40:]
        json.dump(_hist, open(_hist_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        L.append("- 下次预测 ≈ %.3f（趋势 %+.4f/轮）" % (_pred, _slope))
        # 2026-09-01（预测链增强）：R@5 同步预测
        _r5s = [float(h["r5"]) for h in _hist if h.get("r5") is not None]
        if _r5s and _r5 is not None:
            _r5pred = min(1.0, max(0.0, float(_r5) + (_r5s[-1] - _r5s[0]) / max(len(_r5s) - 1, 1)))
            L.append("- 下次 R@5 预测 ≈ %.3f" % _r5pred)
        L.append("")

    text = "\n".join(L)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)
    print("BRAIN-REPORT -> %s" % out)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
