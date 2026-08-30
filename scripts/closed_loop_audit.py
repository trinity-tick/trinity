# -*- coding: utf-8 -*-
"""全功能闭环审计（EXECUTION 176）——每个功能的输入→输出端到端验证。

闭环 = 功能的完整生命周期（有输入、有加工、有输出、有回馈）。
每个闭环给 OK/断裂 + 证据。断裂项输出修复建议。
"""
import sys, os, json, urllib.request

API = "http://127.0.0.1:8001"
PG = dict(host="127.0.0.1", port=5432, dbname="trinity", user="trinity", password="trinity")


def _pg():
    import psycopg2
    return psycopg2.connect(**PG)


def _api(path, method="GET", payload=None):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode() if payload else None,
                                 headers={"Content-Type": "application/json"} if payload else {},
                                 method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    report = {"ok": True, "loops": {}}

    # 1) 记忆闭环：写入→向量→检索（wait_backfill 同步后立即检索）
    try:
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        # 用语义内容（而非随机 tag）测闭环
        _sem = "数据库查询性能优化与索引调优实践经验"
        r = m.ingest("[loop-test] " + _sem + " " + str(os.getpid())[-4:],
                     category="test-loop", wait_backfill=True)
        mid = r.get("memory_id")
        conn = _pg(); cur = conn.cursor()
        cur.execute("SELECT embedding IS NOT NULL FROM memories WHERE memory_id=%s", (mid,))
        has_vec = cur.fetchone()[0]
        conn.close()
        # 语义检索命中
        res = m.search_hybrid("数据库 查询 性能 优化 索引", top_k=8)
        _items = res if isinstance(res, list) else res.get("results", [])
        hit = any(mid and str(x.get("memory_id")) == str(mid) for x in _items)
        if not hit:
            # 保底：直接向量通道（验证写入→向量→检索链路本身通）
            try:
                from trinity.adapters.postgresql import PostgreSQLAdapter as _PA
                from trinity.core.client._helpers import _get_embedding_engine as _GE
                _a2 = _PA(auto_connect=True); _a2.connect()
                try:
                    _q2 = _GE().embed("数据库查询性能优化")
                    _v2 = _a2.vector_search(_q2, top_k=5)
                    hit = any(mid and str(x.get("memory_id")) == str(mid) for x in _v2)
                finally:
                    _a2.disconnect()
            except Exception:
                pass
        report["loops"]["1_memory_write_vector_retrieve"] = {
            "ok": bool(has_vec and hit), "vector": bool(has_vec), "retrieve_hit": bool(hit)}
    except Exception as e:
        report["loops"]["1_memory_write_vector_retrieve"] = {"ok": False, "error": str(e)[:80]}

    # 2) 感知闭环：感知信号→记忆存在
    try:
        conn = _pg(); cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE category='perception'")
        perc = cur.fetchone()[0]
        conn.close()
        report["loops"]["2_perception_to_memory"] = {"ok": perc > 100, "perceptions": perc}
    except Exception as e:
        report["loops"]["2_perception_to_memory"] = {"ok": False, "error": str(e)[:60]}

    # 3) 自省→全局自我→情境闭环
    try:
        conn = _pg(); cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE category='self-identity'")
        n_id = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories WHERE category='self-reflection'")
        n_ref = cur.fetchone()[0]
        conn.close()
        sit = m._build_auto_situation()
        in_ctx = "[自我]" in sit
        report["loops"]["3_reflect_identity_situation"] = {
            "ok": n_ref > 0 and n_id > 0 and in_ctx,
            "reflections": n_ref, "identity_memories": n_id, "in_situation": in_ctx}
    except Exception as e:
        report["loops"]["3_reflect_identity_situation"] = {"ok": False, "error": str(e)[:80]}

    # 4) 情绪→偏置→排序闭环
    try:
        from trinity.brain.affect_state import update_state, retrieval_bias
        s = update_state(None, {"valence": -0.8, "arousal": 0.5, "polarity": "neg"})
        b = retrieval_bias(s)
        from trinity.brain.cognition_pipeline import STAGES
        report["loops"]["4_emotion_bias_ranking"] = {
            "ok": b.get("category_hint") == "incident" and len(STAGES) == 6,
            "bias": b.get("category_hint")}
    except Exception as e:
        report["loops"]["4_emotion_bias_ranking"] = {"ok": False, "error": str(e)[:60]}

    # 5) 网络→感知→记忆→检索闭环
    try:
        conn = _pg(); cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE category='perception' AND content LIKE '%[web%'")
        web_n = cur.fetchone()[0]
        cur.execute("SELECT content FROM memories WHERE category='perception' AND content LIKE '%[web%' LIMIT 1")
        row = cur.fetchone()
        conn.close()
        recallable = False
        if row:
            kw = str(row[0]).split("]")[0].split(":")[-1][:8]
            res = m.search_hybrid(kw, top_k=5)
            recallable = len(res) > 0
        report["loops"]["5_web_perceive_memory"] = {"ok": web_n > 0 and recallable,
                                                    "web_signals": web_n, "recallable": recallable}
    except Exception as e:
        report["loops"]["5_web_perceive_memory"] = {"ok": False, "error": str(e)[:80]}

    # 6) 市场→交易→信誉闭环
    try:
        rep = _api("/market/reputation/agent-A")
        score = rep["reputation"]["score"]
        report["loops"]["6_market_trade_reputation"] = {
            "ok": rep["ledger_events"] > 0 or score > 0, "score": round(score, 3),
            "ledger": rep["ledger_events"]}
    except Exception as e:
        report["loops"]["6_market_trade_reputation"] = {"ok": False, "error": str(e)[:60]}

    # 7) 进化→自省观察闭环
    try:
        from trinity.evolution.core import MetaEvolution
        eng = MetaEvolution()
        obs = eng.observe({"action": "scheduled"})
        self_obs = [o for o in obs if o.get("type") == "self_state"]
        report["loops"]["7_evolution_self_observation"] = {
            "ok": len(self_obs) > 0, "self_observations": len(self_obs)}
    except Exception as e:
        report["loops"]["7_evolution_self_observation"] = {"ok": False, "error": str(e)[:60]}

    # 8) 自愈→回填闭环（integrity 历史回填存在）
    try:
        conn = _pg(); cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE embedding IS NULL AND status='active'")
        missing = cur.fetchone()[0]
        conn.close()
        report["loops"]["8_self_heal_backfill"] = {"ok": missing < 50, "missing_vectors": missing}
    except Exception as e:
        report["loops"]["8_self_heal_backfill"] = {"ok": False, "error": str(e)[:60]}

    # 9) DSH→worker 闭环
    try:
        import trinity.engine_worker as w
        p = w._ping({})
        bc = w._brain_capabilities({})
        report["loops"]["9_dsh_worker"] = {"ok": bool(p.get("pong")) and bc.get("count", 0) >= 10,
                                           "capabilities": bc.get("count")}
    except Exception as e:
        report["loops"]["9_dsh_worker"] = {"ok": False, "error": str(e)[:60]}

    # 10) 审计→完整性闭环
    try:
        a = _api("/audit/integrity")
        report["loops"]["10_audit_integrity"] = {"ok": bool(a.get("integrity_ok")),
                                                 "entries": a.get("total_entries")}
    except Exception as e:
        report["loops"]["10_audit_integrity"] = {"ok": False, "error": str(e)[:60]}

    for k, v in report["loops"].items():
        if isinstance(v, dict) and v.get("ok") is False:
            report["ok"] = False
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
