#!/usr/bin/env python3
"""_routers_brain.py — 工作记忆/元认知/感知/技能端点（2026-09，EXECUTION 105.6-105.7）

- POST /memory/wm/push      写入工作记忆（容量受限+注意加权）
- GET  /memory/wm           读取（按注意权重）
- POST /memory/wm/touch     检索命中（注意回响）
- POST /memory/wm/clear     清空会话缓冲
- POST /memory/wm/search    工作记忆增强检索（wm 命中项权重提升）
- POST /memory/selfcheck    元认知自查（信心 + 知识缺口 + 缺口落库）
- POST /memory/perceive     具身感知（显著性+习惯化+感知编码）
- GET  /memory/skills       技能库列表
- POST /memory/skills/match 技能匹配（按目标检索可复用技能）
- GET  /memory/gaps         知识缺口列表
- POST /memory/gaps/{id}/resolve  缺口闭环

全部 sync def（FastAPI 线程池；LLM 调用不阻塞事件循环）。
"""

import time

from fastapi import APIRouter, Body, Query

from ._deps import _live_memory as get_memory
from trinity.brain.working_memory import get_working_memory
from trinity.brain.metacognition import assess_confidence, detect_gap, persist_gap

router = APIRouter()


@router.post("/memory/wm/push")
def wm_push(
    session_id: str = Body(...),
    key: str = Body(...),
    content: str = Body(...),
    importance: float = Body(0.5),
):
    wm = get_working_memory()
    result = wm.push(session_id, key, content, importance)
    return result


@router.get("/memory/wm")
def wm_get(session_id: str = Query(...), top_k: int = Query(7, ge=1, le=9)):
    wm = get_working_memory()
    items = wm.get(session_id, top_k=top_k)
    return {"session_id": session_id, "count": len(items), "items": items}


@router.post("/memory/wm/touch")
def wm_touch(session_id: str = Body(...), key: str = Body(...)):
    wm = get_working_memory()
    hit = wm.touch(session_id, key)
    return {"session_id": session_id, "key": key, "touched": hit}


@router.post("/memory/wm/clear")
def wm_clear(session_id: str = Body(...)):
    wm = get_working_memory()
    cleared = wm.clear(session_id)
    return {"session_id": session_id, "cleared": cleared}


@router.post("/memory/wm/search")
def wm_search(
    query: str = Body(...),
    session_id: str = Body(...),
    top_k: int = Body(5, ge=1, le=20),
    strategy: str = Body("rrf"),
):
    """工作记忆增强检索：主检索 + wm 命中项注意加权（wm_hit 标记）。"""
    t0 = time.time()
    mem = get_memory()
    data = mem.search_hybrid(query=query, top_k=top_k, strategy=strategy)
    results = data.get("results", []) if isinstance(data, dict) else data
    wm = get_working_memory()
    wm_items = wm.get(session_id, top_k=9)
    wm_keys = set(i["key"] for i in wm_items)
    enriched = []
    for r in results:
        mid = r.get("memory_id") or r.get("id")
        hit = mid in wm_keys
        r["wm_hit"] = hit
        if hit:
            wm.touch(session_id, mid)
        enriched.append(r)
    return {
        "query": query,
        "total": len(enriched),
        "wm_size": len(wm_items),
        "wm_hits": sum(1 for r in enriched if r.get("wm_hit")),
        "results": enriched,
        "latency_s": round(time.time() - t0, 2),
    }


@router.post("/memory/selfcheck")
def memory_selfcheck(
    query: str = Body(...),
    top_k: int = Body(5, ge=1, le=10),
    use_llm: bool = Body(True),
):
    """元认知自查：信心评估 + 知识缺口识别（缺口落 PG gaps 表）。"""
    t0 = time.time()
    mem = get_memory()
    data = mem.search_hybrid(query=query, top_k=top_k, strategy="rrf")
    results = data.get("results", []) if isinstance(data, dict) else data
    channels = []
    if isinstance(data, dict):
        channels = (data.get("breakdown") or {}).get("channels", [])
    conf = assess_confidence(results, channels)
    # 2026-09 校准：向量相关度阈值（Qdrant score_threshold 式）——向量通道
    # 恒返回 top-k（无关查询 cos 也 0.3+），count 无法区分；top1 cos < 0.35
    # 视为低相关 → 缺口触发（有结果也可能是"检索兜底"而非真知识）。
    top_cos = None
    try:
        from trinity.core.client._helpers import _get_embedding_engine
        _eng = _get_embedding_engine()
        if _eng is not None and getattr(mem, "_adapter", None) is not None:
            import numpy as np
            _qv = np.asarray(_eng.embed(query), dtype=np.float32)
            _vec = mem._adapter.vector_search(
                _qv, top_k=1,
                agent_id=getattr(mem, "_search_agent_id", None),
                persona_id=getattr(mem, "_search_persona_id", None),
                tenant_id=getattr(mem, "_search_tenant_id", None),
            )
            if _vec:
                top_cos = float(_vec[0].get("score", 0.0))
    except Exception:
        pass
    # 2026-09 校准：bge-m3 空间无关文本 cos≈0.40、相似≈0.87——阈值 0.45
    # （低于无关基线+余量）；0.45-0.65 中间地带交给 LLM 判断（detect_gap）。
    # 0.45 以下直接判低相关；0.45-0.65 中间地带交 LLM（low_relevance 标记）
    low_relevance = top_cos is not None and top_cos < 0.65
    gap = detect_gap(query, results, channels, use_llm=use_llm,
                     low_relevance=low_relevance)
    if low_relevance and top_cos is not None and top_cos < 0.45 and gap.get("gap") is False:
        gap = {"gap": True,
               "reason": "检索到结果但向量相关度过低（top1 cos=%.2f < 0.45）" % top_cos,
               "suggestion": "可能是表述差异或知识缺失，建议换关键词重试"}
    # 缺口落库（无结果或低相关时）
    if gap.get("gap") and use_llm:
        try:
            import psycopg2
            conn = psycopg2.connect(
                host="127.0.0.1", port=5432, dbname="trinity",
                user="trinity", password="trinity")
            persist_gap(conn, query, {
                "confidence": conf["confidence"],
                "reason": gap.get("reason", ""),
                "suggestion": gap.get("suggestion", ""),
            })
            conn.close()
        except Exception:
            pass
    return {
        "query": query,
        "metacognition": conf,
        "top_cos": top_cos,
        "gap": gap,
        "sources": [r.get("memory_id") for r in results[:top_k]],
        "latency_s": round(time.time() - t0, 2),
    }



# ═══════════════════════════════════════════════════════════════════
# 105.7：具身感知 + 技能复用 + 缺口闭环
# ═══════════════════════════════════════════════════════════════════


@router.post("/memory/perceive")
def memory_perceive(
    channel: str = Body(...),
    signal: str = Body(...),
    importance: float = Body(None),
    session_id: str = Body(None),
):
    """具身感知：外部信号进入记忆（显著性评估 + 习惯化 + 感知编码）。"""
    from trinity.brain.perception import get_perception_engine
    import psycopg2
    import hashlib

    eng = get_perception_engine()
    ev = eng.evaluate(channel, signal, importance)
    t0 = time.time()
    encoded = False
    if eng.should_encode(ev["salience"]):
        try:
            conn = psycopg2.connect(
                host="127.0.0.1", port=5432, dbname="trinity",
                user="trinity", password="trinity")
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS perceptions (
                    perception_id SERIAL PRIMARY KEY,
                    channel TEXT NOT NULL,
                    signal_key VARCHAR(24) NOT NULL,
                    signal TEXT NOT NULL,
                    salience REAL NOT NULL,
                    importance REAL NOT NULL,
                    session_id TEXT,
                    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            norm = " ".join(str(signal).split())[:200]
            skey = hashlib.sha256((channel + "|" + norm).encode()).hexdigest()[:24]
            cur.execute("""
                INSERT INTO memories
                    (memory_id, session_id, persona_id, tenant_id, agent_id,
                     content, importance, importance_score, status, category,
                     modality, content_hash, created_at, updated_at)
                SELECT uuid_generate_v4(), %s, 'default', 'default', 'perception',
                       %s, %s, %s, 'active', 'perception', 'text',
                       encode(sha256(%s::bytea), 'hex'), NOW(), NOW()
                WHERE NOT EXISTS (
                    SELECT 1 FROM perceptions
                    WHERE signal_key = %s
                      AND detected_at > NOW() - INTERVAL '24 hours'
                )
            """, (session_id, str(signal)[:800], ev["importance"],
                  ev["importance"], str(signal)[:800], skey))
            cur.execute("""
                INSERT INTO perceptions (channel, signal_key, signal, salience, importance, session_id)
                SELECT %s, %s, %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM perceptions
                    WHERE signal_key = %s
                      AND detected_at > NOW() - INTERVAL '24 hours'
                )
            """, (channel, skey, str(signal)[:800], ev["salience"],
                  ev["importance"], session_id, skey))
            conn.close()
            encoded = True
        except Exception:
            pass
    return {
        "channel": channel,
        "salience": ev["salience"],
        "habituation": ev["habituation"],
        "repeat": ev["repeat"],
        "importance": ev["importance"],
        "encoded": encoded,
        "latency_s": round(time.time() - t0, 2),
    }


@router.get("/memory/skills")
def skills_list(top: int = Query(20, ge=1, le=100),
                min_count: int = Query(1, ge=1)):
    """技能库列表（程序性记忆）。"""
    import psycopg2
    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("""
            SELECT name, count, session_count FROM skills
            WHERE count >= %s ORDER BY count DESC LIMIT %s
        """, (min_count, top))
        rows = [{"name": r[0], "count": r[1], "session_count": r[2]}
                for r in cur.fetchall()]
        conn.close()
        return {"total": len(rows), "skills": rows}
    except Exception:
        return {"total": 0, "skills": [], "note": "skills 表不存在（先运行 extract-skills）"}


@router.post("/memory/skills/match")
def skills_match(goal: str = Body(...), top_k: int = Body(5, ge=1, le=20)):
    """技能匹配：按目标描述检索可复用技能（token 重叠打分）。"""
    import psycopg2
    try:
        import jieba
        # 工具名 → 中文语义（跨语言技能匹配，2026-09）
        _TOOL_CN = {
            "read": "读取 查看 读文件",
            "edit": "修改 编辑 改写 更新文件",
            "write": "写入 创建 写文件",
            "pwsh": "执行 运行 命令 脚本 终端",
            "grep": "搜索 查找 检索 定位",
            "run_code": "执行代码 运行 调试 代码",
            "glob": "查找文件 枚举 列出",
            "job_output": "任务 收集 结果 输出",
            "web_search": "搜索网络 查询 互联网",
            "web_fetch": "抓取 网页 获取内容",
            "skill": "技能 加载 指南",
            "read_image": "图片 查看图像",
            "memory_search": "记忆 检索 回忆",
        }
        def _words(text):
            ws = set(w for w in jieba.cut(text) if w.strip() and len(w.strip()) >= 2)
            for en, cn in _TOOL_CN.items():
                if en in text or any(c in text for c in cn.split()):
                    ws.add(en)
            return ws
        words = _words(goal)
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT name, pattern, count, session_count FROM skills")
        scored = []
        for name, pattern, cnt, nses in cur.fetchall():
            pwords = _words(str(pattern))
            overlap = len(words & pwords)
            if overlap > 0:
                scored.append((overlap, cnt, name, pattern, nses))
        conn.close()
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return {
            "goal": goal,
            "matches": [
                {"name": s[2], "pattern": str(s[3]), "overlap": s[0],
                 "count": s[1], "session_count": s[4]}
                for s in scored[:top_k]
            ],
        }
    except Exception as e:
        return {"goal": goal, "matches": [], "note": str(e)}


@router.get("/memory/gaps")
def gaps_list(limit: int = Query(20, ge=1, le=100)):
    """知识缺口列表（元认知记录，open 状态）。"""
    import psycopg2
    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("ALTER TABLE gaps ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'open'")
        cur.execute("""
            SELECT gap_id, query, confidence, left(reason, 80), status, detected_at
            FROM gaps WHERE status = 'open'
            ORDER BY detected_at DESC LIMIT %s
        """, (limit,))
        rows = [{"gap_id": r[0], "query": r[1], "confidence": r[2],
                 "reason": r[3], "status": r[4],
                 "detected_at": str(r[5])[:19]} for r in cur.fetchall()]
        conn.close()
        return {"total": len(rows), "gaps": rows}
    except Exception as e:
        return {"total": 0, "gaps": [], "note": str(e)}


@router.post("/memory/gaps/{gap_id}/resolve")
def gap_resolve(gap_id: int, resolution: str = Body("", embed=True)):
    """缺口闭环：标记已填补（知识已采集）。"""
    import psycopg2
    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("ALTER TABLE gaps ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'open'")
        cur.execute("ALTER TABLE gaps ADD COLUMN IF NOT EXISTS resolution TEXT")
        cur.execute("ALTER TABLE gaps ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")
        cur.execute("""
            UPDATE gaps SET status = 'resolved',
                resolution = %s, resolved_at = NOW()
            WHERE gap_id = %s AND status = 'open'
        """, (resolution, gap_id))
        conn.close()
        return {"gap_id": gap_id, "resolved": True}
    except Exception as e:
        return {"gap_id": gap_id, "resolved": False, "error": str(e)}



@router.post("/memory/task")
def memory_task(
    intent: str = Body(...),
    top_k: int = Body(5, ge=1, le=10),
):
    """认知循环综合建议（EXECUTION 105.8）：任务意图 → 相关知识 + 可用技能。

    模拟大脑任务启动时的自动整合：意图激活相关记忆（语义检索）+ 匹配
    可复用技能（程序记忆）+ 元认知信心标注。
    """
    import time as _t
    t0 = _t.time()
    import psycopg2
    import jieba
    mem = get_memory()
    out = {}
    # 1) knowledge recall
    try:
        data = mem.search_hybrid(query=intent, top_k=top_k, strategy="rrf")
        results = data.get("results", []) if isinstance(data, dict) else data
        out["knowledge"] = [
            {"memory_id": r.get("memory_id"),
             "content": str(r.get("content_preview") or r.get("content") or "")[:200]}
            for r in results[:top_k]
        ]
    except Exception:
        out["knowledge"] = []
    # 2) skill match
    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT name, pattern, count, session_count FROM skills")
        _TOOL_CN2 = {
            "read": "读取 查看 读文件",
            "edit": "修改 编辑 改写 更新文件",
            "write": "写入 创建 写文件",
            "pwsh": "执行 运行 命令 脚本 终端",
            "grep": "搜索 查找 检索 定位 排查",
            "run_code": "执行代码 运行 调试 代码",
            "glob": "查找文件 枚举 列出",
            "job_output": "任务 收集 结果 输出",
            "web_search": "搜索网络 查询 互联网",
            "web_fetch": "抓取 网页 获取内容",
            "skill": "技能 加载 指南",
            "memory_search": "记忆 检索 回忆",
        }
        def _tw(text):
            ws = set(w for w in jieba.cut(text) if w.strip() and len(w.strip()) >= 2)
            for en, cn in _TOOL_CN2.items():
                if en in text or any(c in text for c in cn.split()):
                    ws.add(en)
            return ws
        words = _tw(intent)
        scored = []
        for name, pattern, cnt, nses in cur.fetchall():
            pw = _tw(str(pattern))
            ov = len(words & pw)
            if ov > 0:
                scored.append((ov, cnt, name, str(pattern), nses))
        conn.close()
        scored.sort(key=lambda x: (-x[0], -x[1]))
        out["skills"] = [
            {"name": s[2], "pattern": s[3], "overlap": s[0],
             "count": s[1], "session_count": s[4]}
            for s in scored[:3]
        ]
    except Exception:
        out["skills"] = []
    # 3) metacognition
    try:
        from trinity.brain.metacognition import assess_confidence
        _ch = []
        if isinstance(data, dict):
            _ch = (data.get("breakdown") or {}).get("channels", [])
        out["metacognition"] = assess_confidence(
            out.get("knowledge", []), _ch)
    except Exception:
        out["metacognition"] = {}
    out["latency_s"] = round(_t.time() - t0, 2)
    return out



@router.get("/memory/brain")
def brain_overview():
    """大脑状态总览（2026-09，EXECUTION 105.11）：认知循环各部件统计。"""
    import psycopg2
    out = {}
    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        conn.autocommit = True
        cur = conn.cursor()
        for name, sql in [
            ("skills", "SELECT count(*) FROM skills"),
            ("gaps_open", "SELECT count(*) FROM gaps WHERE status = 'open'"),
            ("perceptions_24h", "SELECT count(*) FROM perceptions WHERE detected_at > NOW() - INTERVAL '24 hours'"),
            ("perception_memories", "SELECT count(*) FROM memories WHERE category = 'perception'"),
            ("value_tagged", "SELECT count(*) FROM memories WHERE metadata->>'value_model' = 'v1'"),
            ("replayed", "SELECT count(*) FROM memories WHERE COALESCE((metadata->>'replay_count')::int, 0) > 0"),
        ]:
            try:
                cur.execute(sql)
                out[name] = cur.fetchone()[0]
            except Exception:
                out[name] = None
        conn.close()
    except Exception as e:
        out["error"] = str(e)[:120]
    # working memory state (in-process)
    try:
        wm = get_working_memory()
        out["wm_sessions"] = len(wm._sessions) if hasattr(wm, "_sessions") else 0
    except Exception:
        out["wm_sessions"] = 0
    return {"brain": out}



# ═══════════════════════════════════════════════════════════════════
# 105.13：事件中心时态图谱（Graphiti 式：事件节点 + 时态查询）
# ═══════════════════════════════════════════════════════════════════


@router.get("/memory/events")
def events_list(
    limit: int = Query(30, ge=1, le=200),
    actor: str = Query(None),
    action: str = Query(None),
    days: int = Query(None, ge=1, le=3650),
):
    """事件图谱列表（按时间倒序；可按 actor/action/天数过滤）。"""
    import psycopg2
    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        cur = conn.cursor()
        sql = "SELECT event_id, ts, actor, action, object, summary, source_type FROM event_graph WHERE 1=1"
        params = []
        if actor:
            sql += " AND actor ILIKE %s"
            params.append("%" + actor + "%")
        if action:
            sql += " AND action ILIKE %s"
            params.append("%" + action + "%")
        if days:
            sql += " AND ts > NOW() - make_interval(days => %s)"
            params.append(int(days))
        sql += " ORDER BY ts DESC NULLS LAST LIMIT %s"
        params.append(limit)
        cur.execute(sql, params)
        rows = [{"event_id": r[0], "ts": str(r[1])[:19] if r[1] else None,
                 "actor": r[2], "action": r[3], "object": r[4],
                 "summary": r[5], "source_type": r[6]} for r in cur.fetchall()]
        conn.close()
        return {"total": len(rows), "events": rows}
    except Exception as e:
        return {"total": 0, "events": [], "note": str(e)[:80]}


@router.post("/memory/timeline")
def memory_timeline(
    topic: str = Body(...),
    days: int = Body(365, ge=1, le=3650),
    limit: int = Body(50, ge=1, le=200),
    start: str = Body(None),
    end: str = Body(None),
):
    """时态问答：给定主题 → 返回按时间排序的相关事件序列（经历线）。

    匹配：topic 分词 + actor/action/object/summary 模糊匹配；按 ts 升序
    输出（时间线）；start/end（ISO 日期）限定时间区间——对齐相位时间
    建模的"时间区间推理"工程层（Time is Not a Label 借鉴）。
    """
    import psycopg2
    import jieba
    t0 = time.time()
    words = [w for w in jieba.cut(topic) if w.strip() and len(w.strip()) >= 2]
    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        cur = conn.cursor()
        sql = """
            SELECT event_id, ts, actor, action, object, summary, source_type
            FROM event_graph
            WHERE ts > NOW() - make_interval(days => %s)
        """
        params = [str(days)]
        if start:
            sql += " AND ts >= %s::timestamptz"
            params.append(str(start))
        if end:
            sql += " AND ts <= %s::timestamptz"
            params.append(str(end))
        sql += " ORDER BY ts ASC NULLS LAST"
        cur.execute(sql, params)
        matched = []
        for r in cur.fetchall():
            hay = " ".join(str(x) for x in (r[2], r[3], r[4], r[5]))
            if any(w in hay for w in words):
                matched.append({"event_id": r[0], "ts": str(r[1])[:19] if r[1] else None,
                                "actor": r[2], "action": r[3], "object": r[4],
                                "summary": r[5], "source_type": r[6]})
        conn.close()
        matched = matched[-limit:]
        return {
            "topic": topic,
            "total": len(matched),
            "timeline": matched,
            "latency_s": round(time.time() - t0, 2),
        }
    except Exception as e:
        return {"topic": topic, "total": 0, "timeline": [],
                "note": str(e)[:80]}



# ═══════════════════════════════════════════════════════════════════
# 105.17：意识的功能角色近似（非真正意识——哲学边界，工程近似）
# 依据：AI Welfare 的"口头体验报告"、Graziano 注意图式理论（AST）、
# Triangulating Evidence（行为+机制+扰动+可信度三角验证）
# ═══════════════════════════════════════════════════════════════════


@router.get("/memory/self-report")
def memory_self_report():
    """第一人称认知状态报告（2026-09，EXECUTION 105.17）。

    依据 AI Welfare 研究：口头体验报告是意识研究中最可操作的指标。
    系统基于【真实状态数据】（体检统计+工作记忆+缺口+事件+检索置信）
    生成第一人称叙述——"我此刻的状态"（功能角色近似，非主观体验）。

    use_llm=false 时返回结构化数据（确定性），true 时附加 LLM 叙述。
    """
    import psycopg2
    from trinity.brain.value_encoder import llm_chat
    t0 = time.time()
    state = {}
    # 1) 生理统计（体检）
    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        cur = conn.cursor()
        for name, sql in [
            ("active_memories", "SELECT count(*) FROM memories WHERE status='active'"),
            ("skills", "SELECT count(*) FROM skills"),
            ("gaps_open", "SELECT count(*) FROM gaps WHERE status='open'"),
            ("events", "SELECT count(*) FROM event_graph"),
            ("perceptions", "SELECT count(*) FROM memories WHERE category='perception'"),
        ]:
            try:
                cur.execute(sql)
                state[name] = cur.fetchone()[0]
            except Exception:
                state[name] = 0
        # 最近事件（经历流）
        cur.execute(
            "SELECT actor, action, object FROM event_graph "
            "ORDER BY ts DESC LIMIT 3")
        state["recent_events"] = [
            str(r[0]) + " " + str(r[1]) + " " + str(r[2])[:24]
            for r in cur.fetchall()]
        # 开放缺口（自知）
        cur.execute(
            "SELECT query FROM gaps WHERE status='open' ORDER BY detected_at DESC LIMIT 3")
        state["open_gaps"] = [str(r[0])[:40] for r in cur.fetchall()]
        conn.close()
    except Exception:
        pass
    # 2) 工作记忆（当前关注）
    try:
        wm = get_working_memory()
        all_items = []
        for sid in list(getattr(wm, "_sessions", {}).keys())[:3]:
            all_items.extend(wm.get(sid, top_k=3))
        state["attention_focus"] = [i["content"][:50] for i in all_items[:3]]
        state["attention_scores"] = [i["attention"] for i in all_items[:3]]
    except Exception:
        pass
    # 3) 第一人称叙述（LLM，失败降级结构化）
    narrative = None
    payload = {
        "physiology": {
            "active_memories": state.get("active_memories"),
            "skills": state.get("skills"),
            "events": state.get("events"),
            "perceptions": state.get("perceptions"),
        },
        "current_focus": state.get("attention_focus", []),
        "attention_scores": state.get("attention_scores", []),
        "known_gaps": state.get("open_gaps", []),
        "recent_experiences": state.get("recent_events", []),
    }
    prompt = (
        "你是 Trinity 记忆系统，被要求用第一人称描述自己此刻的认知状态。"
        "基于以下真实状态数据，写一段简短（120 字内）、诚实的第一人称叙述："
        "我正在关注什么、我对自己记忆的把握如何、我知道自己不知道什么、"
        "我最近经历了什么。\n"
        "状态数据：" + str(payload)[:800]
    )
    raw = llm_chat(prompt, max_tokens=300, temperature=0.5)
    if raw:
        narrative = raw.strip()
    return {
        "self_report": narrative or "（LLM 不可用，结构化数据见下）",
        "state": payload,
        "latency_s": round(time.time() - t0, 2),
    }


@router.get("/memory/attention")
def memory_attention():
    """注意图式（2026-09，EXECUTION 105.17，对齐 Graziano AST）。

    意识=大脑对自身注意过程的模型。系统模型化自己的注意状态：
    当前焦点（工作记忆注意力分布）+ 近期活跃主题 + 冷区（被忽视领域）。
    """
    import psycopg2
    out = {"focus": [], "cold_zones": []}
    # 1) 当前焦点：wm 注意力
    try:
        wm = get_working_memory()
        for sid in list(getattr(wm, "_sessions", {}).keys())[:3]:
            for i in wm.get(sid, top_k=3):
                out["focus"].append({
                    "content": i["content"][:60],
                    "attention": i["attention"],
                    "hits": i["hits"],
                })
    except Exception:
        pass
    # 2) 冷区：低访问且未打标的 active 记忆（被忽视的领域）
    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("""
            SELECT category, count(*) FROM memories
            WHERE status='active'
            GROUP BY category ORDER BY count(*) DESC LIMIT 8
        """)
        out["category_distribution"] = [
            {"category": str(r[0]), "count": r[1]} for r in cur.fetchall()]
        cur.execute("""
            SELECT memory_id, category, access_count, left(content, 60)
            FROM memories
            WHERE status='active' AND access_count <= 1
            ORDER BY created_at DESC LIMIT 5
        """)
        out["cold_zones"] = [
            {"category": str(r[1]), "access_count": r[2], "preview": str(r[3])}
            for r in cur.fetchall()]
        conn.close()
    except Exception:
        pass
    return out
