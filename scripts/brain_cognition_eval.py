# -*- coding: utf-8 -*-
"""认知能力量化评测（EXECUTION 154）——对标 MATE 情绪指标 + Hindsight 反思三能力。

模块 1：情绪状态机指标（MATE 借鉴——可测的情绪行为）
  - EMA 收敛性：同向情绪连续查询 → 状态单调趋近
  - 极性方向：neg 查询 → valence < 0；pos 查询 → valence > 0
  - 偏置正确性：高唤醒/消极 → retrieval_bias 触发预期策略

模块 2：反思三能力（Hindsight 对标——Retain/Recall/Reflect）
  - retain：会话自省是否产出（self_reflect_daily 写入）
  - recall：self-reflection 记忆能否被检索到
  - reflect：自省内容质量（关注/状态/学习三要素）

输出 JSON 报告 + 每项 PASS/FAIL。
"""
import sys, os, json, urllib.request

API = "http://127.0.0.1:8001"

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")

    report = {"ok": True, "checks": {}}

    # ── 模块 1：情绪状态机 ─────────────────────────────
    try:
        from trinity.brain.affect_state import update_state, retrieval_bias
        # 1a EMA 收敛：连续 neg 查询
        s = None
        for _ in range(5):
            s = update_state(s, {"valence": -0.8, "arousal": 0.7, "polarity": "neg"})
        ema_converged = s["valence"] < -0.7 and s["polarity"] == "neg"
        report["checks"]["emotion_ema"] = {"converged": ema_converged,
                                           "state": s}
        # 1b 极性方向
        sp = update_state(None, {"valence": 0.9, "arousal": 0.3, "polarity": "pos"})
        sn = update_state(None, {"valence": -0.9, "arousal": 0.3, "polarity": "neg"})
        polarity_ok = sp["valence"] > 0 and sn["valence"] < 0
        report["checks"]["emotion_polarity"] = {"ok": polarity_ok}
        # 1c 偏置正确性
        b_neg = retrieval_bias(sn)
        b_pos = retrieval_bias(sp)
        bias_ok = b_neg.get("category_hint") == "incident" and b_pos.get("category_hint") is None
        report["checks"]["emotion_bias"] = {"ok": bias_ok, "neg_bias": b_neg,
                                            "pos_bias": b_pos}
    except Exception as e:
        report["checks"]["emotion"] = {"error": str(e)[:100]}

    # ── 模块 2：反思三能力 ─────────────────────────────
    try:
        from trinity.adapters.postgresql import PostgreSQLAdapter
        a = PostgreSQLAdapter(auto_connect=True)
        a.connect()
        try:
            import psycopg2
            conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                    user="trinity", password="trinity")
            cur = conn.cursor()
            # retain：self-reflection 记忆产出
            cur.execute("SELECT count(*) FROM memories WHERE category='self-reflection'")
            n_reflect = cur.fetchone()[0]
            report["checks"]["reflect_retain"] = {"count": n_reflect,
                                                  "ok": n_reflect >= 5}
            # reflect 质量：三要素
            cur.execute("SELECT content FROM memories WHERE category='self-reflection' ORDER BY created_at DESC LIMIT 3")
            rows = [r[0] for r in cur.fetchall()]
            quality = 0
            for text in rows:
                t = str(text or "")
                if "我在关注" in t or "近期关注" in t:
                    quality += 1
                if "我的状态" in t:
                    quality += 1
                if "我的学习" in t or "感知" in t:
                    quality += 1
            max_q = len(rows) * 3
            report["checks"]["reflect_quality"] = {"score": quality, "max": max_q,
                                                   "ok": max_q > 0 and quality / max_q >= 0.5}
            conn.close()
            # recall：自省记忆可检索
            try:
                # 向量直查（RRF 混合被感知记忆主导——感知记忆是环境噪音类）
                from trinity.adapters.postgresql import PostgreSQLAdapter
                from trinity.core.client._helpers import _get_embedding_engine
                _a2 = PostgreSQLAdapter(auto_connect=True)
                _a2.connect()
                try:
                    _eng2 = _get_embedding_engine()
                    _qv2 = _eng2.embed("我在关注 我的状态")
                    _vr = _a2.vector_search(_qv2, top_k=5)
                    recalled = any("self-reflection" in str(r.get("content") or "") for r in _vr)
                    hits = len(_vr)
                finally:
                    _a2.disconnect()
                report["checks"]["reflect_recall"] = {"ok": recalled, "hits": hits, "mode": "vector"}
            except Exception as e:
                report["checks"]["reflect_recall"] = {"ok": False, "error": str(e)[:80]}
        finally:
            a.disconnect()
    except Exception as e:
        report["checks"]["reflection"] = {"error": str(e)[:100]}

    # 汇总
    for k, v in report["checks"].items():
        if isinstance(v, dict) and v.get("ok") is False:
            report["ok"] = False
    # EXECUTION 157: 失败告警——写审计标记（运维可 /audit/query 追踪）
    if not report["ok"]:
        try:
            from trinity.adapters.postgresql import PostgreSQLAdapter
            _a3 = PostgreSQLAdapter(auto_connect=True)
            _a3.connect()
            try:
                _a3.write_audit_log(
                    memory_id=None, action="cognition_check_failed",
                    agent_id="cognition-check",
                    details={"failed_checks": [k for k, v in report["checks"].items()
                                                if isinstance(v, dict) and v.get("ok") is False]},
                )
            finally:
                _a3.disconnect()
        except Exception:
            pass
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1

if __name__ == "__main__":
    sys.exit(main())
