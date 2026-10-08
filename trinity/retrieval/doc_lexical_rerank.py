# -*- coding: utf-8 -*-
"""trinity/retrieval/doc_lexical_rerank.py — doc 域「引擎召回 + 确定性词法重排」（可选，默认不参与任何既有路径）。

动机（2026-09-21 本机实测，同一 20 题 doc golden set，3 轮取中位）：
  · 引擎自身（cascade，50 深）          R@1 **0.550** / R@3 0.750 / R@5 0.800 / R@10 0.900
  · 引擎候选 + **本模块**的重排          R@1 **0.850** / R@3 0.900 / R@5 0.900 / R@10 0.900
  · 离线文档级 BM25（整语料，参照臂）     R@1 **0.900** / R@10 1.000
⇒ **索引里信息是全的，丢信息的是检索路径**；把排序交回确定性方法即可取回 +0.30 R@1。
  三个数字都可由 `temp/_route_composition_20260921.py` 复现（活引擎 + 生产模块 + 生产 PG），
  产物 `output/_route_composition_20260921.json`。

**三处口径偏差的实测代价（本模块的三次迭代，写下来免得下次再犯）**：
  | 实现 | R@1 |
  |---|---|
  | 逐 chunk 打分 + 取该 chunk 的 max | 0.750 |
  | 按文档聚合 tf、但漏了标题权重/bigram/k1 | 0.500（**比不重排还差**） |
  | 与已验证离线臂逐字对齐（本版） | **0.850** |
⇒ 结论：**重排的分数取决于索引口径，不取决于"用了 BM25"**。少任何一维（章节标题 ×3 权重、
  中文 bigram 补齐、k1=1.2）都复现不出标定过的那个数 —— 前两版都是"实现偏离口径"，不是候选池问题。

为什么自建索引而不是复用引擎的 BM25：引擎的 BM25 通道候选池与 top_k 绑定、且在 RRF 里不带权重
（`hybrid_retriever._rrf_fuse` 中 vector/bm25/graph 均为 1.0），实测目标文档常不在池内。
本模块用引擎**写时生成的 `content_tsv_zh`**（明文，无需解密；**直读 content 会拿到密文**，见 AGENTS §13.1）
按 `metadata->>'source_file'` 聚合成文档级语料，绕开该瓶颈。

纪律：本模块**只被显式调用**（新路由 `/memory/search/lexical-rerank`）；任何异常 fail-open，
调用方拿回原结果。索引按 persona 缓存（TTL 300s），失败退化为"不重排"并**显式标注原因**。
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple
import logging

K1, B = 1.2, 0.75      # 与已验证离线臂（doc_retrieval_eval.BM25 的默认 k1=1.2, b=0.75）同值
_TTL_S = float(os.environ.get("TRINITY_DOC_RERANK_TTL_S", "300") or 300)
_LOCK = threading.Lock()
_CACHE: Dict[str, Dict[str, Any]] = {}


def _basename(p: Any) -> str:
    return re.split(r"[\\/]", str(p))[-1] if p else ""


def _parse_tsv(txt: Optional[str]) -> List[str]:
    if not txt:
        return []
    return re.findall(r"'([^']+)'", txt)


def _connect():
    """凭证一律走本仓唯一入口 `trinity.utils.pgconn`（环境变量优先，回落 trinity/trinity）。

    为什么不自己写 os.environ.get 的默认值（2026-09-21 本模块初版就写错）：把默认写成
    postgres/"" 会与全仓 20+ 处的惯例（trinity/trinity）不一致 —— 一旦 API 进程里没有
    注入 TRINITY_PG_*，本模块会连不上，而它**看起来只是「没重排」**（fail-open 的静默形态）。
    """
    try:
        from trinity.utils.pgconn import connect as _pg_connect

        return _pg_connect(connect_timeout=8)
    except Exception:  # noqa: BLE001 — 回落路径也要能连上，不许静默变成空操作
        import psycopg2

        return psycopg2.connect(
            host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
            port=int(os.environ.get("TRINITY_PG_PORT", "5432") or 5432),
            dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
            user=os.environ.get("TRINITY_PG_USER", "trinity"),
            password=os.environ.get("TRINITY_PG_PASSWORD", ""),
            connect_timeout=8,
        )


def _sqlite_db() -> str:
    """解析 **SQLite 主存储**路径（2026-10-06 新增）。

    为什么不写一个"看着合理"的默认值（本模块 2026-09-21 就因同类错误静默失效过一次）：
    本仓同时存在 `TRINITY_STORE`（**目录**，API/supervisor 注入）与 `TRINITY_DB`（**文件**），
    而 `~/.trinity/` 下还有 `store/` 与 `store-restored/` 两个目录 —— 猜错就又是"看起来只是没重排"。
    这里按**与本仓既有脚本相同的优先级**逐个试，全部落空则返回空串（由调用方如实标注，不静默）。
    """
    env = os.environ.get("TRINITY_STORE")
    if env:
        p = env if os.path.isfile(env) else os.path.join(env, "trinity_store.db")
        if os.path.isfile(p):
            return p
    db = os.environ.get("TRINITY_DB")
    if db and os.path.isfile(db):
        return db
    try:  # 引擎自己的解析器（全仓唯一入口，避免我再编一套）
        from trinity.core.client._helpers import _find_trinity_store

        d = _find_trinity_store()
        p = d if os.path.isfile(d) else os.path.join(str(d), "trinity_store.db")
        if os.path.isfile(p):
            return p
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）trinity/retrieval/doc_lexical_rerank.py::_sqlite_db")
    home = os.path.expanduser("~")
    for cand in (os.path.join(home, ".trinity", "store-restored", "trinity_store.db"),
                 os.path.join(home, ".trinity", "store", "trinity_store.db")):
        if os.path.isfile(cand):
            return cand
    return ""


def _assemble(rows: "List[Tuple[Any, Any, Any]]") -> Dict[str, Any]:
    """把 `(rel, section, body)` 行装配成文档级语料。**两条后端共用**（§1050：口径只写一份）。

    这里承载**全部三处已标定的口径**（2026-09-21 实测：少任何一维都复现不出 R@1 0.850）：
      ① 章节标题 ×3 权重；② 中文再补 bigram（见 `_tokenize`）；③ k1=1.2（见 K1/B）。
    PG 与 SQLite 两条路径**必须走同一个装配函数** —— 否则就是本仓反复记录的"同一逻辑 N 份、口径漂移"。
    """
    docs: "Dict[str, List[str]]" = {}
    no_tsv = 0
    for rel, section, tsv in rows:
        doc = _basename(rel)
        if not doc:
            continue
        toks = docs.setdefault(doc, [])
        toks.extend(_tokenize(section or "") * 3)
        if tsv:
            # content_tsv_zh（PG）是 tsvector 文本；tokenized_content（SQLite）是空格分隔词元。
            # 前者走 _parse_tsv，后者正则找不到引号 ⇒ 自动落到 _tokenize 分支（与评测脚本同构）。
            toks.extend(_parse_tsv(tsv) or _tokenize(tsv))
        else:
            no_tsv += 1
    items = [(d, Counter(t), len(t)) for d, t in docs.items() if t]
    df: Counter = Counter()
    for _, c, _dl in items:
        df.update(c.keys())
    n = len(items)
    avgdl = (sum(dl for _, _, dl in items) / n) if n else 1.0
    idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
    return {"docs": items, "idf": idf, "avgdl": avgdl or 1.0, "n": n,
            "rows": len(rows), "no_tsv": no_tsv, "built_at": time.time()}


def _build_pg(persona: str) -> Dict[str, Any]:
    conn = _connect()
    try:
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        cur.execute("set statement_timeout = '120s'")
        cur.execute(
            """
            select coalesce(metadata->>'source_file', '') as rel,
                   coalesce(metadata->>'section', '')     as section,
                   coalesce(content_tsv_zh::text, '')     as tsv
              from memories
             where persona_id = %s and metadata->>'source_file' is not null
            """,
            (persona,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return dict(_assemble(rows), backend="pg")


def _build_sqlite(persona: str) -> Dict[str, Any]:
    """**SQLite 主存储**上的等价实现（2026-10-06 新增；PG 不可用时的回退）。

    为什么必须加（本轮实测）：本部署按 D28 **保持 SQLite**，而 PG 口令不可用
    （`fe_sendauth: no password supplied`）⇒ 本模块此前**每次搜索都 `empty_index`**、
    静默 fail-open —— 正是它自己注释里预言过的失效形态（"看起来只是「没重排」"）。
    加了这条路径，`POST /memory/search/lexical-rerank` 才会真的重排。

    字段映射（与 PG 版逐字段对应，且与 `scripts/doc_retrieval_eval._sqlite_doc_rows` **同口径**）：
      * ``rel``     ← ``json_extract(metadata,'$.source_file')``
      * ``section`` ← ``json_extract(metadata,'$.section')``
      * ``body``    ← **``tokenized_content``** —— 本仓那个明文 jieba 影子列
        （``SECURITY.md`` SB-1/SB-2），正是 PG 侧 ``content_tsv_zh`` 在 SQLite 上的对应物：
        两者都是**写时从明文生成的检索用词元**。**不能用 `content`** —— 直读会拿到密文
        （AGENTS §13.1；实测该库里存在 enc:v1 行）。
    """
    db = _sqlite_db()
    if not db:
        raise RuntimeError("SQLite store not found (TRINITY_STORE/TRINITY_DB/~/.trinity)")
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True, timeout=60)
    try:
        # `status='active'` 与评测脚本的 SQLite 路径一致（PG 版无此过滤；此处选与**已实测过
        # B2 的那条 SQLite 查询**对齐，好让两条路径的数可比）。
        rows = con.execute(
            "SELECT COALESCE(json_extract(metadata,'$.source_file'), '') AS rel, "
            "COALESCE(json_extract(metadata,'$.section'), '') AS section, "
            "COALESCE(tokenized_content, '') AS body "
            "FROM memories WHERE persona_id = ? AND status='active' "
            "AND json_extract(metadata,'$.source_file') IS NOT NULL", (persona,)).fetchall()
    finally:
        con.close()
    return dict(_assemble(rows), backend="sqlite")


def _build(persona: str) -> Dict[str, Any]:
    """优先 PG（原路径、已标定），PG 不可用则回退 SQLite，并**在元数据里标明后端**。

    为什么把 backend 写进元数据：本次问题的**全部代价**就在于"失效不可见"。
    只报 `enabled/reason` 不够 —— 必须让调用方看得出**索引是从哪个存储建的**，
    以及 PG 为什么没用上（`pg_error`），否则下次换个部署又会退化成静默哑线。
    """
    try:
        return _build_pg(persona)
    except Exception as pg_exc:  # noqa: BLE001 — PG 缺席是**常态**（D28: SQLite），不是异常
        try:
            idx = _build_sqlite(persona)
        except Exception as sq_exc:  # noqa: BLE001 — 两条都失败要如实说，不许静默
            return {"docs": [], "idf": {}, "avgdl": 1.0, "n": 0, "rows": 0, "no_tsv": 0,
                    "built_at": time.time(), "backend": "none",
                    "backend_error": "pg=%s | sqlite=%s"
                                     % (repr(pg_exc)[:120], repr(sq_exc)[:120])}
        idx["pg_error"] = repr(pg_exc)[:120]
        return idx


def _index(persona: str) -> Dict[str, Any]:
    now = time.time()
    with _LOCK:
        ent = _CACHE.get(persona)
        if ent and (now - ent["built_at"]) < _TTL_S:
            return ent
    idx = _build(persona)
    with _LOCK:
        _CACHE[persona] = idx
    return idx


def _tokenize(text: str) -> List[str]:
    """分词 + **中文 bigram 补齐**（与 scripts/doc_retrieval_eval.py::tokenize 同口径）。

    为什么要补 bigram（本轮实测的第三处口径偏差）：Trinity 的索引列 `content_tsv_zh` 是写时按
    当时的 jieba 切出来的，查询侧再切一次时**切法可能不同**（「主存储切换」vs「主存储/切换」）
    ⇒ 词元对不上 ⇒ 命中 0。补 2-gram 后即使整词没对上，也能靠字对命中（抗分词差异）。
    """
    text = (text or "").lower()
    try:
        import jieba

        toks = [t.strip() for t in jieba.lcut(text) if t.strip()]
    except Exception:  # noqa: BLE001 — jieba 不可用时退化为按非词字符切
        toks = [t for t in re.split(r"\W+", text) if t]
    out: List[str] = []
    for t in toks:
        if re.fullmatch(r"[\W_]+", t):
            continue
        out.append(t)
        if re.fullmatch(r"[\u4e00-\u9fff]{2,}", t):
            out.extend(t[i:i + 2] for i in range(len(t) - 1))
    return out


def search_with_rerank(engine: Any, query: str, top_k: int = 10, persona: str = "",
                       strategy: str = "cascade", over_fetch: int = 5, deep_cap: int = 200,
                       **filters: Any) -> Dict[str, Any]:
    """引擎**过取** → 确定性重排 —— 三个面（REST 路由 / CLI / MCP）**共用同一实现**。

    为什么抽出来（2026-09-21）：REST 路由里那段「过取 + 重排 + fail-open」如果各面各写一份，
    就会重演本仓反复记录的"同一逻辑 N 份、口径漂移"（§13.3 清单类判据的边界问题）。
    这里只做一件事：把 `search_hybrid(top_k*over_fetch)` 的候选交给 `rerank_hits`，并保证
    **任何一步失败都退回原序 + 明确标注原因**（检索可用性优先，且不许静默）。

    返回：引擎原始 dict + `lexical_rerank` 元数据（enabled/reason/…）。非 dict 时原样返回。
    """
    deep = min(max(int(top_k) * max(1, int(over_fetch)), 50), int(deep_cap))
    kw: Dict[str, Any] = {"query": query, "top_k": deep, "strategy": strategy or "cascade"}
    for k, v in filters.items():
        if v:
            kw[k] = v
    if not hasattr(engine, "search_hybrid"):
        return {"results": [], "lexical_rerank": {
            "enabled": False, "reason": "engine_has_no_search_hybrid",
            "note": "该引擎对象不支持 search_hybrid ⇒ 本能力不适用（未做任何降级检索）"}}
    try:
        data = engine.search_hybrid(**kw)
    except Exception as exc:  # noqa: BLE001 — 过取失败退回原深度，绝不让新路径比既有路径更脆
        kw["top_k"] = int(top_k)
        try:
            data = engine.search_hybrid(**kw)
        except Exception as exc2:  # noqa: BLE001
            return {"results": [], "lexical_rerank": {
                "enabled": False, "reason": "search_failed", "error": repr(exc2)[:160]}}
        if isinstance(data, dict):
            data = dict(data)
            data["lexical_rerank"] = {"enabled": False, "reason": "over_fetch_failed",
                                      "error": repr(exc)[:160], "fetch_k": int(top_k)}
        return data
    if not isinstance(data, dict):
        return data
    try:
        out = rerank_hits(query, persona or "default", list(data.get("results") or []), int(top_k))
        data = dict(data)
        data["results"] = out["results"]
        meta = dict(out["meta"], fetch_k=deep)
        # 2026-09-21（CLI 实测踩到）：候选与索引**作用域没对齐**时，重排"看起来在跑但等于没跑"——
        # 实测一次 `candidates_in=17 / candidates_with_evidence=1`（金标题根本不在候选池里）。
        # 这类失效**不会报错**，只会让分数悄悄不变 ⇒ 必须显式标注，否则又是一条静默哑线。
        _cin = int(meta.get("candidates_in") or 0)
        _cev = int(meta.get("candidates_with_evidence") or 0)
        if _cin > 0 and _cev / _cin < 0.15:
            meta["scope_warning"] = (
                "候选与索引语料几乎不重叠（%d/%d 有词法证据）⇒ 多半是候选作用域没对齐"
                "（把 persona_id 也设成索引那个 persona）" % (_cev, _cin))
        data["lexical_rerank"] = meta
    except Exception as exc:  # noqa: BLE001 — fail-open：重排失败就返回原序，并如实标注
        data = dict(data)
        data["lexical_rerank"] = {"enabled": False, "reason": "rerank_failed",
                                  "error": repr(exc)[:160], "fetch_k": deep}
    return data


def rerank_hits(query: str, persona: str, hits: List[Dict[str, Any]], top_k: int) -> Dict[str, Any]:
    """按文档级词法证据重排 hits。返回 {"results": [...], "meta": {...}}；异常由调用方兜。

    三种「没重排」的情形**必须互相区分并显式标注**（§13.5 写侧哑线：写了没被读）：
      ① 该 persona 在索引里 0 个 chunk（多半是 persona 传错）⇒ reason=empty_index；
      ② 查询词一个都没命中索引词表（生僻/英文/超短查询）⇒ reason=no_query_token_match；
      ③ 候选里没有任何文档有词法证据 ⇒ reason=no_candidate_evidence。
    三者的排序都退化为原序，但调用方一定能在响应里看出**是哪一种**，而不是看到 enabled=True 却毫无变化。
    """
    idx = _index(persona or "default")
    idf, avgdl, docs = idx["idf"], idx["avgdl"], idx["docs"]
    base = {
        "persona": persona,
        "index_docs": idx["n"],
        "index_chunks": idx.get("rows", idx["n"]),
        "index_age_s": round(time.time() - idx["built_at"], 1),
        "candidates_in": len(hits),
        # 2026-10-06：**必须**报出索引来自哪个存储。本模块此前 PG-only，在 SQLite 部署下
        # 每次搜索都是 `enabled:false / empty_index`，而响应里**看不到原因**——
        # 一个已接线、已插桩、已标定的机制就这样静默死掉了好几周。
        "backend": idx.get("backend", "unknown"),
        "note": "按**文档级** BM25（写时生成的检索词元列按 source 聚合）确定性重排；无 LLM 参与",
    }
    if idx.get("pg_error"):
        base["pg_error"] = idx["pg_error"]
    if idx.get("backend_error"):
        base["backend_error"] = idx["backend_error"]
    # 2026-10-06：**非 PG 后端必须带标定警告**。
    # 本模块标定的 R@1 0.850 是在 **PG 语料**上测出来的，而两条后端的语料**并不相同** ——
    # 实测 trinity-docs：PG 255 篇 / 4,246 行，SQLite 275 篇 / 3,569 行。
    # 静默换后端 = 用同一套分数去比两把**刻度不同**的尺（本仓反复记录过这类口径漂移）。
    if idx.get("backend") not in ("pg", None, "unknown"):
        base["calibration_warning"] = (
            "索引来自 **%s** 而非 PG —— 本模块标定的 R@1 0.850 是在 **PG 语料**上测的，"
            "两条后端语料不同（实测 trinity-docs：pg 255 篇/4246 行 vs sqlite 275 篇/3569 行）"
            "⇒ 本读数**不可与标定值直接比较**" % idx.get("backend"))
    if idx["n"] <= 0:
        return {"results": list(hits),
                "meta": dict(base, enabled=False, reason="empty_index", candidates_out=len(hits))}
    qtok = [t for t in _tokenize(query) if t in idf]
    if not qtok:
        return {"results": list(hits),
                "meta": dict(base, enabled=False, reason="no_query_token_match", candidates_out=len(hits))}
    score: Dict[str, float] = {}
    for doc, cnt, dl in docs:
        dl = dl or 1
        s = 0.0
        for t in qtok:
            tf = cnt.get(t, 0)
            if tf:
                s += idf[t] * (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * dl / avgdl))
        if s > 0:
            score[doc] = s

    def doc_of(h: Dict[str, Any]) -> str:
        meta = h.get("metadata") or {}
        return _basename(meta.get("source_file") or meta.get("source_uri") or h.get("source_uri"))

    order = {i: h for i, h in enumerate(hits)}
    n_with_ev = sum(1 for h in hits if score.get(doc_of(h), 0.0) > 0)
    ranked = sorted(
        order.items(),
        key=lambda kv: (-score.get(doc_of(kv[1]), 0.0), kv[0]),  # 无词法证据的保持原相对次序
    )
    top = [h for _, h in ranked[: max(1, int(top_k))]]
    return {
        "results": top,
        "meta": dict(base, enabled=n_with_ev > 0,
                     reason="reranked" if n_with_ev > 0 else "no_candidate_evidence",
                     query_tokens_in_index=len(qtok),
                     candidates_with_evidence=n_with_ev,
                     candidates_out=len(top)),
    }
