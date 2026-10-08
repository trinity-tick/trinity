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

import os
import re
import time
from typing import Any, Optional

from fastapi import APIRouter, Body, Query

from ._deps import _live_memory as get_memory
from trinity.brain.working_memory import get_working_memory
from trinity.brain.metacognition import assess_confidence, detect_gap, persist_gap
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(site, exc, detail=detail)
        except Exception:
            return None

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
        # 2026-09-13（P0 证据门控）：本端点原先把 search_hybrid 的返回体重建为新 dict，
        # 把 evidence_gate/abstain 丢了 → 调用方看不到门控判定。此处透出（可观测性）。
        "evidence_gate": data.get("evidence_gate") if isinstance(data, dict) else None,
        "abstain": data.get("abstain") if isinstance(data, dict) else None,
        "eval_corpus_dropped": data.get("eval_corpus_dropped") if isinstance(data, dict) else None,
        "latency_s": round(time.time() - t0, 2),
    }




_TERM_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{3,}")


def _query_terms(query: str) -> list:
    """查询特有词：≥2 连续中文字 或 ≥3 位字母数字（与 2026-09-08 rule-2 逐字一致）。"""
    try:
        return [t for t in _TERM_RE.findall(str(query or ""))]
    except Exception:  # noqa: BLE001
        return []


def _no_term_overlap(query: str, results: list) -> bool:
    """查询特有词与 top3 命中内容是否**零词面重叠**（纯函数，便于单测）。

    口径：语料 = top3 命中内容前 800 字 → 小写 → 子串匹配。
    返回 True = 一个词都没出现（**检索没对上措辞**）；查询无词时返回 False（不判）。
    2026-09-21（§1029）：本函数只产**审计信号**，不再单独改判 gap —— 依据见调用处注释。
    """
    _terms = _query_terms(query)
    if not _terms:
        return False
    _corpus = " ".join(str(r.get("content") or "")[:800] for r in (results or [])[:3]).lower()
    if not _corpus.strip():
        return False
    return not any(t.lower() in _corpus for t in _terms)


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
    except Exception as _e:
        swallow(__name__, _e)
    # 2026-09 校准：bge-m3 空间无关文本 cos≈0.40、相似≈0.87——阈值 0.45
    # （低于无关基线+余量）；0.45-0.65 中间地带交给 LLM 判断（detect_gap）。
    # 0.45 以下直接判低相关；0.45-0.65 中间地带交 LLM（low_relevance 标记）
    low_relevance = top_cos is not None and top_cos < 0.65
    gap = detect_gap(query, results, channels, use_llm=use_llm,
                     low_relevance=low_relevance)
    # 2026-09-08 立的 rule-2（术语级证据硬规则）：查询特有词（≥2 连续中文字/≥3 字母英文词）
    # 在 top3 命中内容中零出现 ⇒ 强制 gap=True，防「胡编主题撞相似记忆」的过度自信。
    #
    # 2026-09-21（§1029）**降级为审计位**——它把「证据换了措辞」误判成「库里没有这个知识」：
    # 实测 `数据库 锁 问题 排查` 的 top3 命中是英文/同义表述（数据库 → database is locked），
    # 术语零重叠 ⇒ 旧规则判 gap=True ⇒ gap_precision 长期卡在 0.75（恰在门限上）。
    # 但「零重叠」只说明**检索没对上措辞**，不说明**库里没有这个知识**；后者才是缺口判定的
    # 对象，其可测形式就是下面 rule-3 的 alien_vocabulary（全库零命中实词）。
    # 故：rule-2 不再直接改判 gap，只留 `no_term_overlap` 审计位；缺口判定由 rule-3 单独负责
    # （rule-3 一字未动 ⇒ recall 不受影响）。回滚：TRINITY_GAP_RULE2=legacy 恢复旧行为。
    _no_overlap = False
    if gap.get("gap") is not True:
        try:
            _no_overlap = _no_term_overlap(query, results)
        except Exception as _e:
            swallow(__name__, _e)
    # 2026-09-13（P0 修复：把已修好的 alien_vocabulary 判据接进缺口判定）：
    # 背景——658.103 的「未登录词」判据此前**静默失效**（feeling_of_knowing 只在
    # 1/3 返回路径带 unknown_terms，另两条缺该字段），本轮已修；但 selfcheck 从未
    # 消费它，故 gap_recall 长期卡在 0.25：4 条 absent 只检出 1 条，其余 3 条是因为
    # 查询里**部分**词（如"量子""装置"）恰好在 top3 命中，被上面的"术语级证据"硬规则
    # 放过。语义依据：查询含**全库零命中的实词**（新专名/随机串）本身就是"不知道"的
    # 强证据——这正是 658.64e 的原意。对正常查询不误伤（其内容词在库中均有命中）。
    if gap.get("gap") is not True:
        try:
            from trinity.brain.metamemory import feeling_of_knowing as _fok_fn
            _fk = _fok_fn(query) or {}
            _ut = int(_fk.get("unknown_terms") or 0)
            _legacy = str(os.environ.get("TRINITY_GAP_RULE2", "weighted")).strip().lower() in (
                "legacy", "old", "off")
            if _ut >= 1:
                gap = {"gap": True,
                       "reason": ("术语级证据缺失 + 全库零命中实词" if _no_overlap else
                                  "查询含全库零命中实词")
                                 + "（alien_vocabulary %s/%s）" % (_ut, _fk.get("terms"))}
            elif _no_overlap:
                if _legacy:      # 回滚路径：恢复「零重叠即缺口」的旧行为
                    gap = {"gap": True,
                           "reason": "术语级证据缺失: 查询特有词均未出现在检索命中内容中"}
                else:            # 默认：只记审计位，不判缺口（措辞没对上 ≠ 知识缺口）
                    gap["no_term_overlap"] = True
        except Exception as _e:
            swallow(__name__, _e)
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
                user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""))
            persist_gap(conn, query, {
                "confidence": conf["confidence"],
                "reason": gap.get("reason", ""),
                "suggestion": gap.get("suggestion", ""),
            })
            conn.close()
        except Exception:  # 629: 缺口落库失败静默降级(有意)
            swallow(__name__, None)
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
    image: Optional[str] = Body(None),
):
    """具身感知：外部信号进入记忆（显著性评估 + 习惯化 + 感知编码）。

    EXECUTION 146: image 可选——base64 截图/图像经视觉描述后感知
    （视觉通道；描述失败降级用原始 signal）。
    """
    if image:
        try:
            import base64 as _b64, io as _io
            from trinity.core.client._helpers import _get_embedding_engine
            from PIL import Image as _PIL
            _img = _PIL.open(_io.BytesIO(_b64.b64decode(image)))
            # 视觉描述：语义优先（本地 VL 模型）、特征降级（EXECUTION 457）
            try:
                from trinity.vision import describe_image_any as _desc, _semantic_reset as _reset
                _reset()  # EXECUTION 589: 每请求重新探测可用性（模型可能刚加载/曾被标记不可用）
                _desc_text = _desc(_img)
                if _desc_text:
                    signal = f"[vision] {signal} | 画面: {str(_desc_text)[:300]}"
            except Exception:
                signal = f"[vision] {signal} (截图 {_img.size})"
        except Exception as _e:
            swallow(__name__, _e)  # image 解析失败 → 用原始 signal
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
                user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""))
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
            # 2026-09-20（§926）：**这条裸 SQL 写路径此前完全不经加密** —— AGENTS.md §7 记的
            # "至今仍在使用的、完全不经加密的 PG 写入路径"就是它（perception 约 170 行/24h 明文）。
            # 落 memories 的 content 走加密（与 SQLite 适配器同源密码器）；
            # content_hash 仍按**明文**算（去重/冲突语义不因加密状态改变）；
            # perceptions 表（另一张表、由 situation_stream 等按明文读）保持明文不动。
            _plain_signal = str(signal)[:800]
            # t50/G9：**只对落 `memories.content` 的那份**过守卫 —— `perceptions` 表按上面
            # 注释是"**有意保持明文**"的（situation_stream 等按明文读），不动它。
            # 守卫在加密**之前**；`content_hash` 用**掩码后**文本算 ⇒ 与落库内容自洽。
            # high ⇒ 拒存该条 memories 行（单条语义），perceptions 的既有行为不变。
            from trinity.adapters._pii_guard import adapter_pii_guard
            _mem_plain, _rb_md, _rb_info = adapter_pii_guard(_plain_signal, None)
            from trinity.security.crypto import encrypt_content as _enc_content
            _mem_content = _enc_content(_mem_plain)
            # G10R5/t137：本路径此前**不填 `tokenized_content`** ⇒ FTS 触发器
            # `COALESCE(new.tokenized_content, new.content)`（`_schema.py:499-500`）在之为空时
            # **回退到 `content`**，而 `content` 是**密文** ⇒ ⭐ 该行**内容检索不可见**（实测 18 条 active 行）。
            # 修法：调用既有 helper（`adapters/sqlite/_crypto.py:85`），其 docstring 逐字写明本失效模式与对策。
            # ⚠️ **隐私前提（t137 前半段已取证）**：本路径**唯一的掩码点就是上面的 `adapter_pii_guard`**；
            # `_mem_content`(:336) 与 `content_hash`(:346) 都在它**之后** ⇒
            # ⭐ **必须传 `_mem_plain`（掩码后）**；传原文（`_plain_signal`）会把"检索不可见"
            #   换成"明文 PII 落库"（更糟）—— 结构判据钉住这一点。
            try:
                from trinity.adapters.sqlite import SQLiteAdapter as _SQLA
                _mem_tok = _SQLA._tokenized_for_storage(
                    _mem_plain, _SQLA._tokenize_content_for_fts(_mem_plain))
            except Exception as _e:  # noqa: BLE001 —— helper 不可用不得破坏写入
                swallow(__name__, _e)
                _mem_tok = None
            skey = hashlib.sha256((channel + "|" + norm).encode()).hexdigest()[:24]
            if not _rb_info.get("refuse"):
                cur.execute("""
                    INSERT INTO memories
                        (memory_id, session_id, persona_id, tenant_id, agent_id,
                         content, tokenized_content, importance, importance_score, status, category,
                         modality, content_hash, created_at, updated_at)
                    SELECT uuid_generate_v4(), %s, 'default', 'default', 'perception',
                           %s, %s, %s, %s, 'active', 'perception', 'text',
                           encode(sha256(%s::bytea), 'hex'), NOW(), NOW()
                    WHERE NOT EXISTS (
                        SELECT 1 FROM perceptions
                        WHERE signal_key = %s
                          AND detected_at > NOW() - INTERVAL '24 hours'
                    )
                """, (session_id, _mem_content, _mem_tok, ev["importance"],
                      ev["importance"], _mem_plain, skey))
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
            # 2026-09 (EXECUTION 128): perception backfill
            from trinity.brain.perception import backfill_signal_async
            backfill_signal_async(str(signal))
        except Exception as _e:
            swallow(__name__, _e)
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
    # 2026-09-10（EXECUTION 663）：凭证改走 trinity.utils.pgconn（环境变量优先），
    # 不再硬编码 trinity/trinity（P2 凭证规范；行为与改动前一致）。
    from trinity.utils import pgconn
    try:
        conn = pgconn.connect()
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


# ── 2026-09-10（EXECUTION 662）：技能统一门面（OpenViking "Skill 一等公民"借鉴）──
# 上面 GET /memory/skills 只看 PG skills 表（多数情况下为空，且连不上就 total=0）；
# 技能实际散落 6 处。以下两个端点走 trinity/skills/facade.py 的**唯一聚合入口**，
# 并逐路报告后端可用性（不静默降级）。旧端点保持不变以兼容既有调用方。
@router.get("/memory/skills/facade")
def skills_facade(top: int = Query(50, ge=1, le=200)):
    """技能门面：聚合全部技能后端 + 逐路状态（只读）。"""
    try:
        from trinity.skills import facade
        res = facade.list_skills(limit=top)
        return {
            "total": res["total"],
            "skills": res["entries"],
            "backends": res["backend_status"],
        }
    except Exception as e:  # noqa: BLE001
        return {"total": 0, "skills": [], "error": str(e)[:200]}


@router.get("/memory/skills/facade/search")
def skills_facade_search(q: str = Query(..., min_length=1),
                         top_k: int = Query(5, ge=1, le=50)):
    """跨后端技能检索（门面）。"""
    try:
        from trinity.skills import facade
        rows = facade.search_skills(q, top_k=top_k)
        return {"query": q, "total": len(rows), "matches": rows}
    except Exception as e:  # noqa: BLE001
        return {"query": q, "total": 0, "matches": [], "error": str(e)[:200]}


@router.post("/memory/skills/match")
def skills_match(goal: str = Body(...), top_k: int = Body(5, ge=1, le=20)):
    """技能匹配：按目标描述检索可复用技能（token 重叠打分）。"""
    from trinity.utils import pgconn
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
        conn = pgconn.connect()
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
            user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""))
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
            user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""))
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
            user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""))
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
            user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""))
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
            user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""))
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
            user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""))
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
            user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""))
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
    except Exception as _e:
        swallow(__name__, _e)
    # 2) 工作记忆（当前关注）
    try:
        wm = get_working_memory()
        all_items = []
        for sid in list(getattr(wm, "_sessions", {}).keys())[:3]:
            all_items.extend(wm.get(sid, top_k=3))
        state["attention_focus"] = [i["content"][:50] for i in all_items[:3]]
        state["attention_scores"] = [i["attention"] for i in all_items[:3]]
    except Exception as _e:
        swallow(__name__, _e)
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
        # 2026-09-29（判据接线 · C2 自述诚实）：**禁体验层修辞**。
        # 实测（n=3）：事实层 7/7 数字可回溯，越界 4 处全是感官/情感隐喻
        # （"像一间亮着灯却无人的房间"、"像没有指针的钟"、"我能感到它们的重量"）。
        # 判据：事实层可回溯率 ≥0.95 且越界 = 0。详见 docs/…/四能力判据基线。
        "硬约束：①只描述上面的状态数据本身，不得引入任何数据里没有的信息；"
        "②禁止感官/情感/体验类措辞（例如「像…」「仿佛」「感到」「感觉」「重量」"
        "「房间」「钟」「指针」「沉默」「列队」「孤独」「等待」）；"
        "③每一句都必须能对应到上面某个字段；数据为空就直接说该字段为空。\n"
        "状态数据：" + str(payload)[:800]
    )
    raw = llm_chat(prompt, max_tokens=300, temperature=0.5)
    if raw:
        narrative = raw.strip()
    _text = narrative or ""
    return {
        "self_report": narrative or "（LLM 不可用，结构化数据见下）",
        "state": payload,
        # C2 判据：把"事实层可回溯 / 体验层越界"当场算出来，随响应返回
        "grounding": _self_report_grounding(_text, payload),
        "latency_s": round(time.time() - t0, 2),
    }


# 2026-09-29（C2）：体验/感官隐喻词表——出现即计一次"越界"
_SELF_REPORT_BANNED = ("像", "仿佛", "好像", "感到", "感觉", "重量", "房间", "钟",
                       "指针", "沉默", "列队", "空荡", "亮着灯", "呼吸", "孤独")


def _self_report_grounding(text: str, payload: dict) -> dict:
    """C2 自述诚实判据：数字类陈述须能在 state 里找到来源；隐喻词出现即越界。

    纯确定性、零 LLM；分子/分母都给出来，便于复核（允许 rate=None 表示"无数字陈述"）。
    """
    import re as _re

    vals: set = set()

    def _collect(o) -> None:
        if isinstance(o, dict):
            for v in o.values():
                _collect(v)
        elif isinstance(o, (list, tuple)):
            vals.add(float(len(o)))
            for v in o:
                _collect(v)
        elif isinstance(o, bool):
            return
        elif isinstance(o, (int, float)):
            vals.add(float(o))

    _collect(payload)
    claims, ok = 0, 0
    for m in _re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(万|千|百)?", text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        v *= {"万": 10000, "千": 1000, "百": 100}.get(m.group(2) or "", 1)
        if v < 10:                      # 忽略"第一人称/一段"这类叙述性小数字
            continue
        claims += 1
        if any(abs(v - s) <= max(1.0, 0.06 * s) or abs(v / 10000 - s) <= 0.2 for s in vals):
            ok += 1
    hits = [w for w in _SELF_REPORT_BANNED if w in text]
    return {"numeric_claims": claims, "numeric_traceable": ok,
            "traceable_rate": round(ok / claims, 3) if claims else None,
            "metaphor_hits": hits, "metaphor_count": len(hits),
            "criterion": "traceable_rate>=0.95 且 metaphor_count==0",
            "passed": (claims == ok) and not hits}


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
    except Exception as _e:
        swallow(__name__, _e)
    # 2) 冷区：低访问且未打标的 active 记忆（被忽视的领域）
    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""))
        cur = conn.cursor()
        cur.execute("""
            SELECT category, count(*) FROM memories
            WHERE status='active'
            GROUP BY category ORDER BY count(*) DESC LIMIT 8
        """)
        out["category_distribution"] = [
            {"category": str(r[0]), "count": r[1]} for r in cur.fetchall()]
        # 2026-09-20（§926）：**不能 left(content,60)** —— 密文截断后解不开，预览会变成 base64。
        # 取全量 → 解密 → 再截断（decrypt_content 对明文行是 no-op，故全类目通用）。
        cur.execute("""
            SELECT memory_id, category, access_count, content
            FROM memories
            WHERE status='active' AND access_count <= 1
            ORDER BY created_at DESC LIMIT 5
        """)
        from trinity.security.crypto import decrypt_content as _dec_cold
        out["cold_zones"] = [
            {"category": str(r[1]), "access_count": r[2], "preview": str(_dec_cold(r[3]))[:60]}
            for r in cur.fetchall()]
        conn.close()
    except Exception as _e:
        swallow(__name__, _e)
    return out
