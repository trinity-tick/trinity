#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc2query 查询侧索引 —— 受限试点（Step 3）。

## 要解决的真实问题

冷数据的**唯一入口是「查询里碰巧含有它的内容词」**。实测依据（本仓 `AGENTS.md` 快照记的）：
按类名精确查冷池 **R@10 = 0.045**（n=44 真入口），换成**带内容线索的查询** = **0.886~0.932**。
差 20 倍 ⇒ 这就是 71.8% 从未被读的根因：**任务形状的查询碰不到冷记忆**。

本试点给少量冷记忆生成「**它回答什么问题 / 什么处境下用得上**」，写进**独立 FTS 表**，
再测「任务形状查询」能否命中它。

## 为什么是试点而不是全量

active 正文约 14 MB、27k 行；全量生成需要 27k 次 LLM 调用。试点先**用最小代价证伪或证实机制**，
再决定是否铺开 —— 与本仓「先拿数据再决定」的处置方式一致。

## ⚠️ 评测设计：必须避免**循环论证**

最容易犯的错：用生成的问题 Q 建索引，再用**同一个 Q** 当查询去测「能否命中」——
那是自证（索引里就有 Q 本身）。本工具因此**强制留出（hold-out）**：
生成 k 个问题，用 **k-1 个建索引**，用**剩下的 1 个**当测试查询 ⇒ 索引**从未见过**测试查询。
`--holdout 0` 会被拒绝。

两个臂：
- **baseline**：测试查询打**现有的内容索引** `memories_fts`
- **treatment**：测试查询打**问题索引** `memories_doc2query`
判据：`treatment_hit@k > baseline_hit@k` 才算机制成立；否则**试点失败**，不铺开。

## 纪律
- **非侵入**：只 `CREATE TABLE memories_doc2query`，**不动** `memories` / `memories_fts`。
  回滚 = `DROP TABLE memories_doc2query`。
- **§13.0**：不参与检索的类目（引擎 `_RETRIEVAL_EXCLUDE_CATEGORIES`）**不进候选**，且显式报数。
- **§13.1**：内容一律走 **REST `GET /memories/{id}`**（实测返回**明文**，且这是 §13.1 指定的路径）；
  抽样断言不以 `enc:v1:` 开头；取不到明文**按原因分别计数**。
- **§13.2**：失败原因分开计数，不合并成 `skipped`。
- **可续跑**：已建索引的 id 不重复调用 LLM（省时省钱）；`--limit` 控制单轮规模。
- 读写证据落 `output/`（§12）。

用法：
    python scripts/doc2query_pilot.py --plan                 # 只读：候选面 + 成本估计
    python scripts/doc2query_pilot.py --build --limit 40     # 生成并建索引（可重复跑以续跑）
    python scripts/doc2query_pilot.py --eval --top-k 10      # A/B 判定
    python scripts/doc2query_pilot.py --drop                 # 回滚（删索引表）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import logging

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/doc2query_pilot.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STORE = os.path.join(os.path.expanduser("~"), ".trinity",
                             "store-restored", "trinity_store.db")
API = "http://127.0.0.1:8001"
TABLE = "memories_doc2query"
#: **留出问题**单独存表。为什么必须存下来、而不是在 `--eval` 时重新生成：
#:   · LLM 非确定（temperature 0.3）⇒ 重生成的问题与建索引那次**不是同一批**，
#:     虽仍满足「没进过索引」，但**不可复现**：同一试点跑两次得到不同读数；
#:   · 还要多花一遍 LLM 调用。
#: 存下来 ⇒ eval 变成**确定性、零 LLM 调用**的判据（本仓要求判据可复跑）。
HOLDOUT_TABLE = "memories_doc2query_holdout"
QUESTIONS_PER_MEMORY = 5
HOLDOUT = 1                      # 必须 >=1；见模块 docstring 的循环论证警告


def resolve_store(explicit: str = "") -> "tuple[str, str]":
    """`TRINITY_STORE` 语义是 **store 目录**（`_helpers.py:22` / `_construction.py:162-165`）。"""
    if explicit:
        return explicit, "cli"
    env = os.environ.get("TRINITY_STORE")
    if env:
        return (env if os.path.isfile(env) else os.path.join(env, "trinity_store.db")), \
            "env:TRINITY_STORE"
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from trinity.core.client._helpers import _find_trinity_store  # noqa: E402
        d = _find_trinity_store()
        return (d if os.path.isfile(d) else os.path.join(d, "trinity_store.db")), \
            "engine:_find_trinity_store"
    except Exception:  # noqa: BLE001
        return DEFAULT_STORE, "default"


def load_exclusions() -> "tuple[list, str]":
    """§13.0：排除类目只从引擎常量取。"""
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from trinity.core.client._search import _RETRIEVAL_EXCLUDE_CATEGORIES  # noqa: E402
        return sorted(set(_RETRIEVAL_EXCLUDE_CATEGORIES)), "trinity.core.client._search"
    except Exception as e:  # noqa: BLE001
        return [], "UNAVAILABLE: %s: %s" % (type(e).__name__, str(e)[:160])


def _connect(store: str, write: bool = False) -> sqlite3.Connection:
    uri = "file:%s?mode=%s" % (store.replace("\\", "/"), "rw" if write else "ro")
    con = sqlite3.connect(uri, uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=55000")
    return con


def table_exists(con) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (TABLE,)).fetchone() is not None


def tokenize(text: str) -> str:
    """与 `memories_fts` 同款：**jieba 空格分词**（实测其 content 就是空格分隔的词元）。

    不这么做，中文在 fts5 默认 tokenizer 下会整句成一个 token ⇒ 查不到。
    """
    try:
        import jieba
        return " ".join(t for t in jieba.cut(text or "") if t.strip())
    except Exception:  # noqa: BLE001
        return text or ""


def parse_questions(raw: str) -> list:
    """把 LLM 输出解析成问题列表。**纯函数、可单测**。

    容忍三种常见形态：JSON 数组 / 编号列表 / 纯行。去重、去空、截断。
    （不做「猜」式修补：解析失败返回空列表，由调用方计数上报 —— §13.2。）
    """
    if not raw:
        return []
    txt = raw.strip()
    out: list = []
    # ① JSON 数组（最稳）
    m = re.search(r"\[.*\]", txt, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                out = [str(x).strip() for x in arr]
        except Exception:  # noqa: BLE001
            out = []
    # ② 编号 / 项目符号列表
    if not out:
        for line in txt.splitlines():
            s = re.sub(r"^\s*(?:[-*•]|\d+[.、)]|\(\d+\))\s*", "", line).strip()
            if s and not s.startswith("{") and not s.startswith("["):
                out.append(s)
    # 清理：去掉引号/句末标点噪声，去重保序，限制长度
    seen, clean = set(), []
    for q in out:
        q = q.strip().strip('"').strip("'").strip()
        q = re.sub(r"^\s*\d+[.、)]\s*", "", q).strip()
        if len(q) < 4 or len(q) > 200:
            continue
        if q in seen:
            continue
        seen.add(q)
        clean.append(q)
    return clean


def select_candidates(store: str, n: int, min_importance: float,
                      categories: list, persona: str = "") -> dict:
    """挑「冷 + 重要 + 可检索 + 尚未建索引」的候选。只读。`persona` 可选。"""
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store}
    excl, esrc = load_exclusions()
    out["engine_exclude_categories"] = excl
    out["engine_exclude_source"] = esrc
    if not excl:
        out.update(verdict="INCONCLUSIVE", error="engine exclusions unavailable: %s" % esrc)
        return out
    if not os.path.exists(store):
        out.update(verdict="INCONCLUSIVE", error="store not found: %s" % store)
        return out
    try:
        con = _connect(store, write=False)
        ph = ",".join("?" for _ in excl)
        where = ("status='active' AND category NOT IN (%s) "
                 "AND CAST(COALESCE(access_count,0) AS INTEGER)=0 "
                 "AND CAST(COALESCE(importance,0) AS REAL)>=?" % ph)
        params = list(excl) + [min_importance]
        # 2026-10-05：persona 过滤（可选）。**为什么需要**：doc 域评测
        # (`doc_retrieval_eval.py`) 的语料是 `persona_id='trinity-docs'`（仓内 docs/*.md，
        # 1,985 条冷行、全部带 metadata.source_file），而库内绝大多数冷行是
        # `persona='default'`（kb_harvested 6,046 条）。**两者是不同语料** ——
        # 不加过滤就建索引，得到的留出问题指向的文档在 doc 域评测里根本不在语料内，
        # 两把尺子混用会得出无效结论。
        if persona:
            where += " AND persona_id=?"
            params.append(persona)
        if categories:
            where += " AND category IN (%s)" % ",".join("?" for _ in categories)
            params += list(categories)
        out["cold_candidates_total"] = con.execute(
            "SELECT COUNT(*) FROM memories WHERE " + where, tuple(params)).fetchone()[0]
        out["retrieval_excluded_active"] = con.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active' AND category IN (%s)"
            % ph, tuple(excl)).fetchone()[0]
        done = set()
        if table_exists(con):
            done = {r[0] for r in con.execute("SELECT DISTINCT memory_id FROM %s" % TABLE)}
        out["already_indexed"] = len(done)
        rows = con.execute(
            "SELECT memory_id, category, importance FROM memories WHERE " + where +
            " ORDER BY CAST(COALESCE(importance,0) AS REAL) DESC, created_at DESC LIMIT ?",
            tuple(params) + (n + len(done),)).fetchall()
        picked = [(m, c, i) for (m, c, i) in rows if m not in done][:n]
        out["picked"] = len(picked)
        out["picked_sample"] = [{"memory_id": m, "category": c, "importance": i}
                                for m, c, i in picked[:5]]
        from collections import Counter
        out["picked_by_category"] = dict(Counter(c for _, c, _ in picked))
        out["est_llm_calls"] = len(picked)
        con.close()
    except Exception as e:  # noqa: BLE001
        out.update(verdict="INCONCLUSIVE",
                   error="query failed: %s: %s" % (type(e).__name__, str(e)[:200]))
        return out
    out["verdict"] = "OK"
    return out


def fetch_plaintext(mid: str) -> "tuple[str, str]":
    """走 REST 取**解密后**内容（§13.1）。返回 (text, status)。

    status ∈ ok / http_error / ciphertext / empty / shape_mismatch —— **分档计数，不合并**（§13.2）。
    """
    try:
        with urllib.request.urlopen("%s/memories/%s" % (API, mid), timeout=25) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return "", "http_error"
    if not isinstance(body, dict) or "content" not in body:
        return "", "shape_mismatch"
    txt = body.get("content") or ""
    if txt.startswith("enc:v1:"):
        return "", "ciphertext"
    if not txt.strip():
        return "", "empty"
    return txt, "ok"


PROMPT = (
    "下面是一段记忆的内容。请生成 {k} 个**中文问句**，要求：\n"
    "1. 每个问句描述一个**用户会提出的任务/处境**，该记忆的内容可以回答它；\n"
    "2. **避免直接复述原文的专有名词**（尽量用任务意图描述，例如「怎么把多个数据源合并成一张表」"
    "而不是「SmartCos WMS 五维对标」）；\n"
    "3. 每个问句 10~40 字，彼此不重复。\n"
    "只输出 JSON 数组，不要任何解释。\n\n内容：\n{body}"
)


def gen_questions(text: str, k: int = QUESTIONS_PER_MEMORY) -> list:
    """用系统既有 LLM 通道（`brain_cycle.llm`）生成问题。"""
    if os.path.join(ROOT, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import brain_cycle  # noqa: E402
    raw = brain_cycle.llm(PROMPT.format(k=k, body=text[:3000]), max_tokens=500)
    return parse_questions(raw)


def build(store: str, limit: int, min_importance: float, categories: list,
          holdout: int = HOLDOUT, persona: str = "") -> dict:
    """生成并写独立 FTS 表。只 `CREATE TABLE`，不动任何既有表。`persona` 可选。"""
    if holdout < 1:
        return {"verdict": "FAILED",
                "error": "holdout 必须 >=1（否则索引里就有测试查询本身 = 循环论证）"}
    res = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store, "limit": limit,
           "holdout": holdout, "persona": persona or "(all)"}
    sel = select_candidates(store, limit, min_importance, categories, persona)
    if sel.get("verdict") != "OK":
        return sel
    con = _connect(store, write=True)
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS %s USING fts5("
                    "memory_id UNINDEXED, questions, tokenize='unicode61')" % TABLE)
        # 留出问题表：普通表即可（eval 只按 memory_id 取一行，不做全文检索）
        con.execute("CREATE TABLE IF NOT EXISTS %s ("
                    "memory_id TEXT, question TEXT)" % HOLDOUT_TABLE)
        con.commit()
        ph = ",".join("?" for _ in sel["engine_exclude_categories"])
        where = ("status='active' AND category NOT IN (%s) AND CAST(COALESCE(access_count,0) AS INTEGER)=0 "
                 "AND CAST(COALESCE(importance,0) AS REAL)>=?" % ph)
        params = list(sel["engine_exclude_categories"]) + [min_importance]
        if persona:
            where += " AND persona_id=?"
            params.append(persona)
        if categories:
            where += " AND category IN (%s)" % ",".join("?" for _ in categories)
            params += list(categories)
        done = {r[0] for r in con.execute("SELECT DISTINCT memory_id FROM %s" % TABLE)}
        rows = con.execute(
            "SELECT memory_id FROM memories WHERE " + where +
            " ORDER BY CAST(COALESCE(importance,0) AS REAL) DESC, created_at DESC LIMIT ?",
            tuple(params) + (limit * 2,)).fetchall()
        todo = [m for (m,) in rows if m not in done][:limit]

        stat = {"ok": 0, "http_error": 0, "shape_mismatch": 0, "ciphertext": 0,
                "empty": 0, "llm_error": 0, "no_questions": 0}
        indexed_questions = 0
        for i, mid in enumerate(todo, 1):
            text, st = fetch_plaintext(mid)
            if st != "ok":
                stat[st] = stat.get(st, 0) + 1
                continue
            try:
                qs = gen_questions(text)
            except Exception:  # noqa: BLE001
                stat["llm_error"] += 1
                continue
            if len(qs) < holdout + 1:
                stat["no_questions"] += 1
                continue
            keep = qs[: len(qs) - holdout]        # 留出最后 holdout 个当测试查询
            held = qs[len(qs) - holdout:]
            con.execute("INSERT INTO %s (memory_id, questions) VALUES (?, ?)" % TABLE,
                        (mid, tokenize(" ".join(keep))))
            # 留出问题**存下来**（见 HOLDOUT_TABLE 注释：让 eval 确定且零 LLM 调用）
            for hq in held:
                con.execute("INSERT INTO %s (memory_id, question) VALUES (?, ?)"
                            % HOLDOUT_TABLE, (mid, hq))
            con.commit()
            indexed_questions += len(keep)
            stat["ok"] += 1
            if i % 10 == 0:
                print("  ... %d/%d" % (i, len(todo)))
        res.update(indexed_memories=stat["ok"], indexed_questions=indexed_questions,
                   failure_breakdown=stat, todo=len(todo))
        res["index_rows"] = con.execute("SELECT COUNT(*) FROM %s" % TABLE).fetchone()[0]
        res["holdout_rows"] = con.execute(
            "SELECT COUNT(*) FROM %s" % HOLDOUT_TABLE).fetchone()[0]
    except Exception as e:  # noqa: BLE001
        con.close()
        res.update(verdict="FAILED", error="%s: %s" % (type(e).__name__, str(e)[:200]))
        return res
    con.close()
    res["verdict"] = "OK"
    return res


def _fts_hits(con, table: str, col: str, query: str, k: int) -> list:
    """在给定 FTS 表里查 top-k 的 memory_id 列表（doc2query 表直接有 memory_id；
    memories_fts 需要 rowid→memory_id 映射）。"""
    toks = [t for t in tokenize(query).split() if t]
    if not toks:
        return []
    # 用 OR 连接，避免要求全部命中（任务形状查询本来就词不同）
    mq = " OR ".join('"%s"' % t.replace('"', "") for t in toks[:12])
    try:
        if table == TABLE:
            return [r[0] for r in con.execute(
                "SELECT memory_id FROM %s WHERE questions MATCH ? LIMIT ?" % TABLE,
                (mq, k)).fetchall()]
        return [r[0] for r in con.execute(
            "SELECT m.memory_id FROM memories_fts f JOIN memories m ON m.rowid = f.rowid "
            "WHERE f.content MATCH ? LIMIT ?", (mq, k)).fetchall()]
    except Exception:  # noqa: BLE001
        return []


def evaluate(store: str, k: int, sample: int, holdout: int = HOLDOUT) -> dict:
    """A/B：**留出**的测试查询分别打内容索引与问题索引。

    留出问题**从表里读**（build 时存下）⇒ 本判据**确定性且零 LLM 调用**，
    同一库上重复跑得到同一读数。
    """
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store, "top_k": k,
           "holdout": holdout}
    con = _connect(store, write=False)
    if not table_exists(con):
        con.close()
        out.update(verdict="INCONCLUSIVE", error="%s 不存在，先跑 --build" % TABLE)
        return out
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (HOLDOUT_TABLE,)).fetchone():
        con.close()
        out.update(verdict="INCONCLUSIVE",
                   error="%s 不存在 —— 旧版试点没有存留出问题，需重跑 --build" % HOLDOUT_TABLE)
        return out
    pairs = con.execute(
        "SELECT memory_id, question FROM %s LIMIT ?" % HOLDOUT_TABLE,
        (sample,)).fetchall()
    out["n"] = len(pairs)
    out["holdout_source"] = HOLDOUT_TABLE
    base_hits = treat_hits = 0
    detail = []
    for mid, q in pairs:
        b = _fts_hits(con, "memories_fts", "content", q, k)
        t = _fts_hits(con, TABLE, "questions", q, k)
        bh, th = (mid in b), (mid in t)
        base_hits += 1 if bh else 0
        treat_hits += 1 if th else 0
        detail.append({"memory_id": mid, "query": (q or "")[:60],
                       "baseline": bh, "treatment": th})
    con.close()
    n = max(1, len(detail))
    out["baseline_hit@k"] = round(base_hits / n, 4)
    out["treatment_hit@k"] = round(treat_hits / n, 4)
    out["delta"] = round((treat_hits - base_hits) / n, 4)
    out["per_query"] = detail[:20]
    out["verdict"] = "OK"
    out["mechanism_supported"] = out["treatment_hit@k"] > out["baseline_hit@k"]
    return out


def drop(store: str) -> dict:
    con = _connect(store, write=True)
    try:
        dropped = []
        for t in (TABLE, HOLDOUT_TABLE):
            ex = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                             (t,)).fetchone() is not None
            if ex:
                con.execute("DROP TABLE %s" % t)
                con.commit()
                dropped.append(t)
        return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store,
                "verdict": "OK", "dropped": bool(dropped), "dropped_tables": dropped}
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--drop", action="store_true")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--min-importance", type=float, default=0.6)
    ap.add_argument("--category", default="", help="逗号分隔；空=全部可检索类目")
    ap.add_argument("--holdout", type=int, default=HOLDOUT)
    ap.add_argument("--persona", default="",
                    help="限定 persona（doc 域评测的语料是 trinity-docs；空=全部）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.holdout < 1:
        print("拒绝：--holdout 必须 >=1 —— 留出为 0 会导致**循环论证**（索引里就有测试查询本身）")
        return 2

    store, src = resolve_store(a.store)
    cats = [c.strip() for c in a.category.split(",") if c.strip()]
    if a.drop:
        r = drop(store)
    elif a.build:
        r = build(store, a.limit, a.min_importance, cats, a.holdout, a.persona)
    elif a.eval:
        r = evaluate(store, a.top_k, a.sample, a.holdout)
    else:
        a.plan = True
        r = select_candidates(store, a.limit, a.min_importance, cats, a.persona)

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("== doc2query 受限试点（Step 3）==")
        print("库：%s（来源 %s）" % (store, src))
        print("[采样时刻] %s" % r.get("ts"))
        if r.get("verdict") != "OK":
            print("判定：%s %s" % (r.get("verdict"), r.get("error")))
            return 2
        if a.plan:
            print("引擎排除类目 %s" % r["engine_exclude_categories"])
            print("retrieval_excluded active %d（§13.0 不进候选，显式报数）"
                  % r["retrieval_excluded_active"])
            print("冷 + 重要候选总数 %d" % r["cold_candidates_total"])
            print("已建索引 %d；本轮将处理 %d（预计 %d 次 LLM 调用）"
                  % (r["already_indexed"], r["picked"], r["est_llm_calls"]))
            print("按类目：%s" % r["picked_by_category"])
        elif a.build:
            print("本轮处理记忆 %d 条；写入问题 %d 条；表内共 %d 行"
                  % (r["indexed_memories"], r["indexed_questions"], r["index_rows"]))
            print("失败分档（§13.2 不合并）：%s" % r["failure_breakdown"])
        elif a.eval:
            print("n=%d  top_k=%d  holdout=%d" % (r["n"], r["top_k"], r["holdout"]))
            print("baseline  (内容索引 memories_fts)      hit@k = %s" % r["baseline_hit@k"])
            print("treatment (问题索引 memories_doc2query) hit@k = %s" % r["treatment_hit@k"])
            print("Δ = %+.4f ⇒ 机制%s" % (r["delta"],
                  "成立" if r["mechanism_supported"] else "**未成立（试点失败，不应铺开）**"))
        os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
        out = os.path.join(ROOT, "output", "doc2query_pilot_%s.json"
                           % time.strftime("%Y%m%d_%H%M%S"))
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=1)
        print()
        print("产物：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
