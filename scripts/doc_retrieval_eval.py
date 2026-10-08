#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc_retrieval_eval.py — doc 域检索评测（recall@page 基线 + doc 路由原型）

两个臂：
  A 生产路径：POST http://127.0.0.1:8001/memory/search/hybrid（47 通道混合检索，top-k）
  B doc 路由原型（本地，零外部依赖）：
     B1 目录粗路由：文档标题 + 章节标题 的 BM25
     B2 目录 + 章节正文 的 BM25（等价"只在 doc 域内检索"）
评分：命中结果能解析出源文档（metadata.source_file / source_uri basename）且
      等于该题 target 即算命中；指标 recall@1/3/5/10 与 MRR@10。

用法：
  python scripts/doc_retrieval_eval.py --arms A,B1,B2 --top-k 10
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import requests

try:
    import jieba
    jieba.setLogLevel(60)
except Exception:  # pragma: no cover
    jieba = None

PG = dict(
    host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
    port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
    dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
    user=os.environ.get("TRINITY_PG_USER", "trinity"),
    password=os.environ.get("TRINITY_PG_PASSWORD", ""),
)
API = os.environ.get("TRINITY_API_BASE", "http://127.0.0.1:8001")
OUT_DIR = Path(os.environ.get("TRINITY_OUT_DIR", r"C:/Users/Administrator/trinity/output"))
ROOT = Path(__file__).resolve().parent.parent

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    if jieba is not None:
        toks = [t.strip() for t in jieba.lcut(text) if t.strip()]
    else:
        toks = _TOKEN_RE.findall(text)
    out = []
    for t in toks:
        if re.fullmatch(r"[\W_]+", t):
            continue
        out.append(t)
        if re.fullmatch(r"[\u4e00-\u9fff]{2,}", t):  # 中文再补 bigram，抗分词差异
            out.extend(t[i:i + 2] for i in range(len(t) - 1))
    return out


class BM25:
    def __init__(self, docs: dict[str, list[str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = docs
        self.lens = {d: len(t) for d, t in docs.items()}
        self.avg = sum(self.lens.values()) / max(1, len(docs))
        self.tf = {d: Counter(t) for d, t in docs.items()}
        df = Counter()
        for t in docs.values():
            for term in set(t):
                df[term] += 1
        n = max(1, len(docs))
        self.idf = {term: math.log(1 + (n - c + 0.5) / (c + 0.5)) for term, c in df.items()}

    def search(self, query: str, top_k: int = 10):
        q = tokenize(query)
        scores = defaultdict(float)
        for term in q:
            idf = self.idf.get(term)
            if not idf:
                continue
            for d, tf in self.tf.items():
                f = tf.get(term, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.lens[d] / self.avg)
                scores[d] += idf * f * (self.k1 + 1) / denom
        return sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]


def basename(path: str | None) -> str:
    if not path:
        return ""
    return os.path.basename(str(path).replace("\\", "/"))


def _sqlite_doc_rows(persona: str, with_body: bool):
    """从 **SQLite 主存储**读文档语料（PG 不可用时的回退）。

    2026-09-30（外部审计 · 目标项 15）：本脚本原先**只能用 PG** 取语料
    （`pg` 里读 `content_tsv_zh`）。后果是：在普通 shell 里跑必然崩在

        psycopg2.OperationalError: connection to server at "127.0.0.1", port 5432
        failed: fe_sendauth: no password supplied

    （PG 口令只由 supervisor 注入服务进程，shell 里没有）。
    **一个跑不起来的基准，其登记值就无法被任何人复现** —— 这正是
    `docs_corpus_hybrid_20260916` R@10=0.35 复现不出的**结构性根因**，
    比"数值本身对不对"更值得修。

    映射关系（与 PG 版逐字段对应）：
      * ``rel``     ← ``json_extract(metadata,'$.source_file')``
      * ``section`` ← ``json_extract(metadata,'$.section')``
      * ``body``    ← **``tokenized_content``** —— 即本仓那个明文 jieba 影子列
        （见 `SECURITY.md` SB-1/SB-2）。它正是 PG 侧 ``content_tsv_zh``
        在 SQLite 上的对应物：都是**写时从明文生成的检索用词元**。
        PG 版解析 tsvector 文本用的 `re.findall(r"'([^']+)'")` 在这里找不到引号，
        会自动落到 `or tokenize(raw)` 分支，对空格分隔的词元等价。
    """
    import sqlite3
    db = os.environ.get("TRINITY_DB") or str(
        Path.home() / ".trinity" / "store" / "trinity_store.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    try:
        body_col = "tokenized_content" if with_body else "NULL"
        sql = (
            "SELECT COALESCE(json_extract(metadata,'$.source_file'), '') AS rel, "
            "COALESCE(json_extract(metadata,'$.section'), '') AS section, "
            "category, COALESCE(" + body_col + ", '') AS body "
            "FROM memories WHERE persona_id = ? AND status='active' "
            "AND json_extract(metadata,'$.source_file') IS NOT NULL"
        )
        return con.execute(sql, (persona,)).fetchall()
    finally:
        con.close()


def _eval_source() -> str:
    """``TRINITY_EVAL_SOURCE`` = ``auto``（默认，PG 失败即回退）/ ``pg`` / ``sqlite``。"""
    return (os.environ.get("TRINITY_EVAL_SOURCE") or "auto").strip().lower()


def load_doc_index(persona: str, with_body: bool):
    src = _eval_source()
    rows = None
    if src in ("auto", "pg"):
        try:
            conn = psycopg2.connect(**PG)
            cur = conn.cursor()
            # 注意：PG 里 84.5% 的 doc chunk content 是 enc:v1: 密文（SQLite 加密镜像同步而来），
            # 但 content_tsv_zh 是写时从明文生成的索引文本 → 用它做 BM25 的正文代理。
            cur.execute(
                """
                SELECT COALESCE(metadata->>'source_file', '') AS rel,
                       COALESCE(metadata->>'section', '')    AS section,
                       category,
                       COALESCE(content_tsv_zh::text, '')    AS body
                FROM memories
                WHERE persona_id = %s AND metadata->>'source_file' IS NOT NULL
                """,
                (persona,),
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as _e:  # noqa: BLE001 — 回退到 SQLite，不因 PG 缺席而整个评测跑不了
            if src == "pg":
                raise
            print(f"  [eval] PG 不可用（{type(_e).__name__}: {str(_e)[:70]}）"
                  f" ⇒ 回退到 SQLite 主存储")
    if rows is None:
        rows = _sqlite_doc_rows(persona, with_body)
    docs: dict[str, list[str]] = defaultdict(list)
    titles: dict[str, str] = {}
    encrypted = 0
    for rel, section, category, content in rows:
        if not rel:
            continue
        docs[rel].extend(tokenize(section) * 3)  # 章节标题权重 3x
        if with_body:
            raw = content or ""
            if not raw:
                encrypted += 1
            # content_tsv_zh 是 tsvector 文本形式：'词元':1,4 '词元2':7 …
            lexemes = re.findall(r"'([^']+)'", raw) or tokenize(raw)
            docs[rel].extend(lexemes)
        if rel not in titles and section:
            titles[rel] = section
    return {k: v for k, v in docs.items()}, encrypted, len(rows)


def load_fs_toc(docs_root: Path):
    """B0：直接从文件系统构建目录（标题 + 各级标题），不依赖入库情况。

    对照价值：DB 里只有 170 篇（上次 fuse 之后新增的 88 篇没进语料），
    文件系统有 255 篇 —— 目录由管道自动生成，才不会被"忘了重跑"卡住。
    """
    docs: dict[str, list[str]] = defaultdict(list)
    titles: dict[str, str] = {}
    for p in sorted(docs_root.rglob("*.md")):
        if "_sitemap.generated" in p.name:
            continue
        rel = p.relative_to(docs_root).as_posix()
        toks = [rel, rel.replace("/", " ")]
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            if line.startswith("#"):
                head = line.lstrip("#").strip()
                if head and not titles.get(rel):
                    titles[rel] = head
                toks.extend(tokenize(head) * 3)
        docs[rel].extend(toks)
    return {k: v for k, v in docs.items()}, titles


def load_doc_titles(persona: str):
    """文档标题表（`doc_toc` 在 PG 侧；SQLite 侧从 `metadata.section` 现算）。

    2026-09-30（目标项 15）：与 `load_doc_index` 同样加 SQLite 回退 ——
    原先 PG 连不上时本函数会让**整个评测**崩掉（实测 traceback 就落在这里）。
    """
    src = _eval_source()
    if src in ("auto", "pg"):
        try:
            conn = psycopg2.connect(**PG)
            cur = conn.cursor()
            cur.execute("SELECT doc_rel, max(doc_title) FROM doc_toc "
                        "WHERE doc_rel IS NOT NULL GROUP BY doc_rel")
            t = {r[0]: r[1] or "" for r in cur.fetchall()}
            conn.close()
            return t
        except Exception:  # noqa: BLE001
            if src == "pg":
                raise
    # SQLite 回退：用每个 source_file 的第一段 section 作标题
    import sqlite3
    db = os.environ.get("TRINITY_DB") or str(
        Path.home() / ".trinity" / "store" / "trinity_store.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    try:
        rows = con.execute(
            "SELECT COALESCE(json_extract(metadata,'$.source_file'),'') AS rel, "
            "COALESCE(json_extract(metadata,'$.section'),'') AS section "
            "FROM memories WHERE persona_id=? AND status='active' "
            "AND json_extract(metadata,'$.source_file') IS NOT NULL",
            (persona,)).fetchall()
    finally:
        con.close()
    t: dict[str, str] = {}
    for rel, section in rows:
        if rel and rel not in t and section:
            t[rel] = section
    return t


def arm_a(query: str, top_k: int, persona: str = "", strategy: str = "rrf"):
    """生产混合检索（REST /memory/search/hybrid）。

    2026-09-16（建议① 执行）新增 `persona` 参数：**单变量诊断**用。
    动机（实测）：覆盖补齐后（doc 入库 170 → 259 篇），B1/B2 两条**纯词法、限 doc 域**
    的臂从 0.50 涨到 **1.00**，而本臂（全库、无作用域）只从 0.25 涨到 **0.35**。
    ⇒ A 与 B1/B2 之间**混了两个变量**：① 作用域（全库 6 万行 vs doc 域 259 篇）
    ② 通道（47 通道融合 vs 纯词法）。加 `persona` 后可以把①隔离出来单独量。
    """
    try:
        payload = {"query": query, "top_k": top_k, "strategy": strategy}
        # 2026-09-30（外部审计 · 目标项 15）：**必须带 include_docs**。
        #
        # 这个基准的**评测对象就是文档语料**，而生产 hybrid 入口默认加
        # `(category NOT LIKE 'doc:%' AND category NOT LIKE 'doc_%')` 把整类排除。
        # 不传这个开关，本臂会返回 20/20 全部"未解析"（R@10=0.0）——
        # 实测复现：修 harness 之前 `[A] R@10=0.0，A 未解析: q01…q20`。
        # 那**不是检索退化**，而是"评测器看不见自己要评的东西"。
        #
        # 反向对照：B1/B2 是纯词法、直接读语料（不走 API），因此**不受影响**，
        # 实测仍精确复现登记的 1.0/1.0 ⇒ 语料本身没变，变的只是 A 臂的可见性。
        # 覆盖：`TRINITY_EVAL_INCLUDE_DOCS=0` 可回到旧行为（对照用）。
        _inc = (os.environ.get("TRINITY_EVAL_INCLUDE_DOCS", "1") or "1").strip().lower()
        payload["include_docs"] = _inc not in ("0", "false", "no", "off")
        if persona:
            payload["persona_id"] = persona
        r = requests.post(
            f"{API}/memory/search/hybrid",
            json=payload,
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        return [], f"ERR {exc}"
    hits = data.get("results") or data.get("hits") or []
    pages = []
    for h in hits:
        meta = h.get("metadata") or {}
        # 2026-10-05 实测：SQLite 回退路径上 `metadata` 可能是**字符串**（未经 json.loads）
        # ⇒ `meta.get(...)` 抛 `AttributeError: 'str' object has no attribute 'get'`，
        # 整个评测**当场崩**（本次就是在这里崩的：PG 口令不可用 ⇒ 回退 SQLite ⇒ 崩）。
        # 与本仓既有先例同款守卫：`scripts/quarantine_benchmark_active.py` 的
        # `if not isinstance(meta, dict): meta = {}`。
        # 纪律依据 §13.2：形状没对上**不得**让读数静默失真，也不该让工具崩成"看起来失败"。
        if not isinstance(meta, dict):
            meta = {}
        doc = basename(meta.get("source_file") or meta.get("source_uri") or h.get("source_uri"))
        pages.append(doc)
    return pages, "ok"


def score(pages: list[str], target: str, ks=(1, 3, 5, 10)):
    res = {}
    for k in ks:
        res[f"recall@{k}"] = 1 if target in pages[:k] else 0
    rr = 0.0
    for i, p in enumerate(pages[:10], 1):
        if p == target:
            rr = 1.0 / i
            break
    res["mrr@10"] = rr
    res["rank"] = (pages.index(target) + 1) if target in pages else 0
    return res


def resolve_fetch_k(over_fetch, top_k: int) -> int:
    """A/A2 臂的请求深度：--over-fetch 生效时用它，否则等于 --top-k（= 原行为）。

    抽成纯函数是为了让「默认不改变行为」这条契约可被单测钉住
    （tests/unit/test_doc_retrieval_doclevel_and_fetch_k.py）。
    """
    return over_fetch if (over_fetch and over_fetch > 0) else top_k


def _doclevel_score(pages: list[str], target: str, ks=(1, 3, 5, 10)) -> dict:
    """文档级打分：按文档去重（保留最佳名次）后算 recall@k。

    2026-09-21（口径修复）动机：实测本评测里生产臂 top-10 平均只覆盖 **5.65 个不同文档**
    （min 2、max 9），而 B1/B2 是「259 篇整文档」级 —— 不去重时两者**粒度不同、不可比**。
    去重后的读数是「同一把尺」下的对照（不改检索、只改打分粒度）。
    """
    seen, ded = set(), []
    for p in pages:
        if p and p not in seen:
            seen.add(p)
            ded.append(p)
    out = {f"recall@{k}": 1 if target in ded[:k] else 0 for k in ks}
    out["rank"] = (ded.index(target) + 1) if target in ded else 0
    return out


def ratchet_baseline(report: dict, path: str) -> int:
    """把 doc 域各臂 R@10 与基线比对。返回 0=通过 / 1=退化 / 2=本次缺臂（不静默放行）。

    纪律与三把老闸门同源（G6）：
      · 基线**存在但读不出** ⇒ fail-closed（1），绝不静默当成"无基线"；
      · 基线里有的臂而**本次没测** ⇒ 显式报 2（不许把"没测"当"没变差"）；
      · 只在**变差**时失败（历史欠账不阻断）。
    """
    base = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                base = json.load(fh)
        except Exception as e:  # noqa: BLE001
            print("[ratchet] FAIL：基线存在但无法解析 %s：%r" % (path, e))
            return 1
        if not isinstance(base, dict) or "arms" not in base:
            print("[ratchet] FAIL：基线格式不正确（缺 arms）：%s" % path)
            return 1
    if base is None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"arms": {a: {"recall@10": s.get("recall@10")}
                                for a, s in (report.get("arms_summary") or {}).items()},
                       "corpus": report.get("corpus") or {},
                       "ts": report.get("ts"),
                       "note": "doc 域检索基线；任一臂 R@10 下降会使闸门失败。改善后请上调。"},
                      fh, ensure_ascii=False, indent=1)
        print("[ratchet] 无基线 -> 本次建立：%s" % path)
        return 0

    cur = report.get("arms_summary") or {}
    cur_arms = report.get("arms") or []
    rc = 0
    missing = [a for a in (base.get("arms") or {}) if a not in cur_arms]
    if missing:
        print("[ratchet] 缺臂（本次未测）：%s —— 未测不等于没变差，显式报出。" % ", ".join(missing))
        rc = 2
    for a, bv in (base.get("arms") or {}).items():
        if a not in cur:
            continue
        b, v = bv.get("recall@10"), cur[a].get("recall@10")
        if b is None or v is None:
            continue
        if v < b - 1e-9:
            print("[ratchet] FAIL：臂 %s R@10 下降 %.4f -> %.4f" % (a, b, v))
            rc = 1
        else:
            print("[ratchet] OK  臂 %-3s R@10 %.4f >= 基线 %.4f" % (a, v, b))
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(ROOT / "eval" / "doc_golden_set.json"))
    ap.add_argument("--persona", default="trinity-docs")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--arms", default="A,B1,B2")
    ap.add_argument("--tag", default="")
    # 2026-09-21：把融合策略变成可测参数。服务端默认也是 rrf（_models.py:297，有反向锁测试），
    # 故默认值等价于既有行为；本仓 docs 语料实测（n=20，文档级去重口径，三轮一致）：
    # rrf R@1=0.45 / fusion 0.55 / **cascade 0.70**，R@10 三者同为 0.85（离线同域 BM25 R@1=0.90）。
    ap.add_argument("--strategy", default="rrf", choices=["rrf", "fusion", "cascade"],
                    help="hybrid 融合策略（只作用于 A / A2 臂；默认 rrf = 服务端默认）")
    # 2026-09-21：客户端「过取」选项。实测（n=20，cascade，文档级去重口径）：
    #   top_k=10 → R@1 0.70 / R@10 0.85 ；top_k=50 → R@1 0.55 / R@10 0.90
    #   ⇒ 过取 + 文档级去重能把 R@10 抬 +0.05（代价是 R@1 掉），是**客户端可部署**的杠杆。
    # 默认 0 = 不启用 ⇒ 请求与打分与改动前逐字相同。
    ap.add_argument("--over-fetch", type=int, default=0,
                    help="A/A2 臂按此深度请求（>0 时生效），打分仍取前 top-k；默认 0 = 与原来相同")
    ap.add_argument("--ratchet", action="store_true",
                    help="与基线比对：任一臂 R@10 下降即非零退出（本次缺臂返回 2，不静默放行）")
    ap.add_argument("--baseline",
                    default=str(ROOT / "dsh-ops" / "doc_retrieval_baseline.json"))
    args = ap.parse_args()

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    items = golden["items"]
    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    # A/A2 臂的请求深度：默认等于 --top-k（不改变原行为）；见 resolve_fetch_k 的单测
    _fetch_k = resolve_fetch_k(args.over_fetch, args.top_k)

    engines = {}
    meta_info = {}
    if "B1" in arms or "B2" in arms:
        titles = load_doc_titles(args.persona)
        docs_toc, _, _ = load_doc_index(args.persona, with_body=False)
        # 目录臂：标题 + 章节标题
        toc_docs = {d: list(t) for d, t in docs_toc.items()}
        for d, title in titles.items():
            toc_docs.setdefault(d, [])
            toc_docs[d].extend(tokenize(title) * 4)
        if "B1" in arms:
            engines["B1"] = BM25(toc_docs)
            meta_info["B1_docs"] = len(toc_docs)
        if "B2" in arms:
            body_docs, enc, nrows = load_doc_index(args.persona, with_body=True)
            for d, title in titles.items():
                body_docs.setdefault(d, [])
                body_docs[d].extend(tokenize(title) * 4)
            engines["B2"] = BM25(body_docs)
            meta_info["B2_docs"] = len(body_docs)
            meta_info["B2_encrypted_rows"] = enc
            meta_info["B2_rows_scanned"] = nrows
        meta_info["B1_docs"] = meta_info.get("B1_docs", len(toc_docs))
    if "B0" in arms:
        fs_docs, fs_titles = load_fs_toc(Path(os.environ.get("TRINITY_DOCS_ROOT", r"C:/Users/Administrator/trinity/docs")))
        for d, title in fs_titles.items():
            fs_docs.setdefault(d, [])
            fs_docs[d].extend(tokenize(title) * 4)
        engines["B0"] = BM25(fs_docs)
        meta_info["B0_docs"] = len(fs_docs)

    results = []
    t0 = time.time()
    for it in items:
        row = {"id": it["id"], "type": it["type"], "query": it["query"], "target": it["target"]}
        for arm in arms:
            if arm == "A":
                pages, status = arm_a(it["query"], _fetch_k, strategy=args.strategy)
                row["A"] = score(pages, it["target"])
                row["A"]["pages"] = pages
                row["A"]["status"] = status
            elif arm == "A2":
                # 单变量诊断臂：与 A 完全相同的请求 + 仅加 doc 域作用域。
                pages, status = arm_a(it["query"], _fetch_k, persona=args.persona, strategy=args.strategy)
                row["A2"] = score(pages, it["target"])
                row["A2"]["pages"] = pages
                row["A2"]["status"] = status
                row["A2"]["scope"] = {"persona_id": args.persona}
            else:
                eng = engines.get(arm)
                if eng is None:
                    continue
                hits = eng.search(it["query"], args.top_k)
                pages = [basename(d) for d, _ in hits]
                row[arm] = score(pages, it["target"])
                row[arm]["pages"] = pages
        results.append(row)

    summary = {}
    for arm in arms:
        vals = [r[arm] for r in results if arm in r]
        if not vals:
            continue
        summary[arm] = {k: round(sum(v[k] for v in vals) / len(vals), 4)
                        for k in ("recall@1", "recall@3", "recall@5", "recall@10", "mrr@10")}
        by_type = defaultdict(list)
        for r in results:
            if arm in r:
                by_type[r["type"]].append(r[arm]["recall@10"])
        summary[arm]["by_type_recall@10"] = {k: round(sum(v) / len(v), 3) for k, v in by_type.items()}
        # 2026-09-21（口径修复·纯增量）：再给一份「文档级去重」口径的读数。
        # 只**新增** doclevel 键、不改既有键 ⇒ ratchet 读的 arms_summary[*]["recall@10"]
        # 语义与基线契约都不变（回滚：删掉本段与打印处两行）。
        _dl = [_doclevel_score((r[arm].get("pages") or []), r["target"])
               for r in results if arm in r]
        if _dl:
            summary[arm]["doclevel"] = {
                k: round(sum(x[k] for x in _dl) / len(_dl), 4)
                for k in ("recall@1", "recall@3", "recall@5", "recall@10")
            }
            summary[arm]["doclevel"]["note"] = "按文档去重后重打分（与离线 BM25 臂同粒度）"
    elapsed = round(time.time() - t0, 1)

    report = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "golden": str(args.golden),
        "n_items": len(items),
        "arms": arms,
        #: 本次 A/A2 臂使用的融合策略（自描述，便于事后按策略分组比较）
        "hybrid_strategy": args.strategy,
        #: A/A2 臂的请求深度（默认 = top_k；>top_k 表示客户端过取后按文档去重取前 top_k）
        "fetch_k": _fetch_k,
        #: ratchet 用的稳定形状（arms_summary），与 results 分开以免判据依赖大数组
        "arms_summary": summary,
        "corpus": {"docs_indexed": meta_info.get("B1_docs") or meta_info.get("B2_docs"),
                   "b0_docs": meta_info.get("B0_docs")},
        "meta": meta_info,
        "summary": summary,
        "elapsed_sec": elapsed,
        "results": results,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"_{args.tag}" if args.tag else ""
    out = OUT_DIR / f"doc_retrieval_eval{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"== doc 域检索评测（{len(items)} 题，top-{args.top_k}，{elapsed}s）==")
    for arm, s in summary.items():
        print(f"  [{arm}] R@1={s['recall@1']} R@3={s['recall@3']} R@5={s['recall@5']} "
              f"R@10={s['recall@10']} MRR={s['mrr@10']} by_type={s['by_type_recall@10']}")
        _dl = s.get("doclevel") or {}
        if _dl:
            print(f"       └ 文档级去重口径: R@1={_dl['recall@1']} R@3={_dl['recall@3']} "
                  f"R@5={_dl['recall@5']} R@10={_dl['recall@10']}")
    if meta_info:
        print(f"  meta: {meta_info}")
    misses = [r["id"] for r in results if arms and r.get(arms[0], {}).get("recall@10") == 0]
    print(f"  {arms[0]} 未命中: {misses}")
    print(f"  报告：{out}")

    if getattr(args, "ratchet", False):
        return ratchet_baseline(report, args.baseline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
