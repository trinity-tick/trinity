#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""retrieval_contribution.py — **统一检索基准口径** + 逐通道贡献 / 显著性（2026-10-06, T3）

┌─ 为什么要这个脚本 ─────────────────────────────────────────────────────────┐
│ 历史口径互不一致，导致同一件事得出相反结论：                                │
│   · `scripts/quality_gate.py` 报 **keyword R@5 == hybrid R@5**（0.916 逐类相同）│
│   · `scripts/channel_ablation.py` 解析的是 quality_gate 的**打印行**，只得到    │
│     R@5 一个标量，无逐题差异 ⇒ **无法做配对显著性检验**，delta=0 也可能只是    │
│     仪器分辨不出。                                                          │
│   · `docs/RETRIEVAL_KNOWN_LIMITS.md` A5 的 nDCG 用的是**另一套口径**（自建临时  │
│     store + mock facts，relevance 映射 direct=2/update=1，分母=全部 query）。 │
│   · `benchmark/stats_ci.py` 只给**边际**置信区间（单臂 mean±CI），**不给配对**   │
│     差值检验 ⇒ 两臂 CI 重叠时无法判定，且它不做逐题配对。                    │
│ 本脚本把上述并成**一个口径**：同一查询集、同一 R@k/nDCG 定义、同一分母、      │
│ 逐题留档 ⇒ 才能做 McNemar / 配对置换 / 配对 bootstrap。                      │
└──────────────────────────────────────────────────────────────────────────┘

口径定义（**逐条可复核**）
--------------------------
查询集：`benchmark/data/longmemeval_mock_dataset.json` 的 `questions`（500 条，
  顺序固定、`--limit` 只取前 N ⇒ 任何两臂的查询集逐字相同）。

金标（每题的期望条目）：`context_facts[].fact`，**按归一化文本去重**后成为该题的
  期望集合 E(q)。`relevance` 映射：`direct`→2、`update`→1、其它→1。
  归一化 = `unicodedata.normalize("NFKC", s)` + 去首尾空白（内部空白保留）。

命中判定：检索行 r 命中期望事实 e ⟺ `norm(e) in norm(r["content"])`（子串包含，
  与 quality_gate 一致）。已实测本数据集的 139 条去重事实**两两互不为子串**（见
  `--stats` 输出 `substring_pairs=0`）⇒ 包含判定不会一次命中多条、不放大召回。

  分级相关度：`rel(r) = max{ grade(e) : e ∈ E(q), e 命中 r }`，未命中任何 e 时
  该行**不进入判定**（既不进 DCG，也不算 unjudged）。

指标（全部在**同一分母 n**上取平均，n = 有金标且有 question 的题数）：
  R@k      = mean_q [ 1{∃ e∈E(q) 出现在前 k 行} ]          （即 hit@k，0/1）
  recall@k = mean_q [ |{e∈E(q) 出现在前 k 行}| / |E(q)| ]  （多事实题才区分）
  MRR      = mean_q [ 1 / (首个命中的 rank+1) ]，无命中记 0
  nDCG@5   = mean_q [ DCG@5 / IDCG@5 ]，DCG = Σ_{i=1..5} rel_i / log2(i+1)，
             IDCG 用 E(q) 的 grade 降序前 5 项算
  unjudged@k = mean_q [ (前 k 行里未命中任何 e 的行数) / k ]  ← **标注缺口披露**
  latency  = 每题墙钟 ms 的 p50 / p95

**与历史口径的差异（必须知道）**
  1. `quality_gate` 是**二值** hit（任一 fact 子串命中）且**不去重**金标 ⇒ 多事实题
     它与本脚本的 R@5 定义一致，但它**没有** nDCG/MRR/recall@k，也**不留逐题结果**
     ⇒ 本脚本的 R@k 与它可直接互比（已实测一致：0.916），其余指标是新增。
  2. `RETRIEVAL_KNOWN_LIMITS.md` A5 的 ΔMRR/ΔnDCG 是 **hybrid vs keyword** 两个
     `search()` 模式之差；本脚本把这两者作为两条独立臂，用**同一评分函数**重算
     ⇒ 数字若与 A5 不同，差异只能来自评分函数（A5 用自建 mock store 的 relevance
     分级，本脚本用数据集自带 `relevance` 字段）。
  3. `benchmark/stats_ci.py` 给边际 CI；本脚本给**配对**显著性（McNemar 精确检验
     + 配对置换 + 配对 bootstrap），三法并列 ⇒ p 值不依赖单一假设。
  4. `channel_ablation.py` 通过子进程 + env 跑 `quality_gate.py`，**只有标量**。
     本脚本在同进程内逐题留档；跨进程不可复现的问题由 `PYTHONHASHSEED=0` 兜底
     （该坑见 channel_ablation.py 注释：不钉种子时同臂自比就有约 4/10 差异）。

用法
----
  # 0) 数据集体检（去重事实数 / 子串对 / 重复度）——**先看这个再读任何数字**
  python scripts/retrieval_contribution.py stats

  # 1) 跑基准（默认全部 SQLite 臂 + 零依赖 BM25，500 题）
  python scripts/retrieval_contribution.py run --out benchmark/results/rc_$(date +%s).json

  # 2) 出表（逐通道贡献 + 显著性）
  python scripts/retrieval_contribution.py table --in <上面那个 json>

  # 3) 单臂复现
  python scripts/retrieval_contribution.py run --arms prod_search_hybrid,pure_bm25

  # 4) 生产 API 通道行为探针（只读，不重启任何服务）
  python scripts/retrieval_contribution.py probe --api http://127.0.0.1:8001
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import statistics
import sys
import time
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple
import logging

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATASET = os.path.join(ROOT, "benchmark", "data", "longmemeval_mock_dataset.json")
K_LIST = (1, 3, 5, 10)
_NORM_RE = re.compile(r"\s+")


# ─────────────────────────── 文本 / 评分 ───────────────────────────
def norm(s: Any) -> str:
    return _NORM_RE.sub(" ", unicodedata.normalize("NFKC", str(s or "")).strip())


_GRADE = {"direct": 2, "update": 1}


def grade_of(fact: Dict[str, Any]) -> int:
    try:
        return _GRADE.get(str((fact or {}).get("relevance") or "").strip().lower(), 1)
    except Exception:
        return 1


def expected_of(q: Dict[str, Any]) -> Dict[str, int]:
    """该题的期望集合：{归一化事实文本 → grade}（按文本去重，取最大 grade）。"""
    out: Dict[str, int] = {}
    for f in q.get("context_facts") or []:
        t = norm((f or {}).get("fact", ""))
        if not t:
            continue
        out[t] = max(out.get(t, 0), grade_of(f))
    return out


def grade_row(row: str, exp: Dict[str, int]) -> Optional[int]:
    """检索行的相关度 = 它命中的期望事实里最大的 grade；未命中任何 = None。"""
    best: Optional[int] = None
    for e, g in exp.items():
        if e in row:
            if best is None or g > best:
                best = g
    return best


def _dcg(rels: Sequence[int], k: int) -> float:
    s = 0.0
    for i, r in enumerate(rels[:k]):
        s += (2 ** r - 1) / math.log2(i + 2)
    return s


def score_query(contents: Sequence[str], exp: Dict[str, int], k_list=K_LIST) -> Dict[str, Any]:
    """单题评分。返回逐题指标（供配对检验）。"""
    graded = [grade_row(norm(c), exp) for c in contents]
    rows = len(graded)
    out: Dict[str, Any] = {"n_expected": len(exp), "n_rows": rows}
    first_hit = next((i for i, g in enumerate(graded) if g is not None), None)
    out["mrr"] = round(1.0 / (first_hit + 1), 6) if first_hit is not None else 0.0
    for k in k_list:
        top = graded[:k]
        # 命中集合：前 k 行里出现的期望事实
        found = set()
        for c in contents[:k]:
            nc = norm(c)
            for e in exp:
                if e in nc:
                    found.add(e)
        out["r@%d" % k] = 1 if found else 0
        out["recall@%d" % k] = round(len(found) / max(len(exp), 1), 6)
        out["unjudged@%d" % k] = round(
            sum(1 for g in top if g is None) / float(k), 6)
    for k in (5,):
        ideal = sorted(exp.values(), reverse=True)[:k]
        idcg = _dcg(ideal, k)
        rels = [g if g is not None else 0 for g in graded[:k]]
        out["ndcg@%d" % k] = round(_dcg(rels, k) / idcg, 6) if idcg > 0 else None
        out["graded"] = bool(ideal)
    return out


# ─────────────────────────── 零依赖 BM25 ───────────────────────────
_WORD_RE = re.compile(r"[A-Za-z0-9_']+")
_CJK_RE = re.compile(r"[\u3000-\u9fff\uf900-\ufaff]")


def tokenize(text: str, mode: str) -> List[str]:
    """mode: regex（零依赖，默认）/ jieba / both。"""
    t = norm(text).lower()
    toks = _WORD_RE.findall(t)
    if mode == "regex" or not _CJK_RE.search(t):
        return toks
    try:
        import jieba
        jieba.setLogLevel(60)
        toks += [w.strip().lower() for w in jieba.cut(t) if w.strip()]
    except ImportError:
        # jieba 缺失 ⇒ 只留 regex 词。**不是静默失败**：`mode` 参数与报告里
        # 的 tokenizer 字段都会披露实际用了哪一套，读者不必猜。
        return toks
    return toks


class PureBM25:
    """自包含 BM25（Okapi，k1/b 可调）——**零第三方依赖**（jieba 缺失时退化为 regex）。

    与 `trinity/retrieval/bm25_index.py` 的关系：本类**不 import** 它，故意另写一份，
    以使"零依赖 BM25"这一臂**不被被测代码自身的 bug 污染**（若两边共用实现，
    BM25 通道的缺陷会同时出现在基准与对照臂上，结论不可用）。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75, mode: str = "regex"):
        self.k1, self.b, self.mode = k1, b, mode
        self.docs: List[Tuple[str, List[str]]] = []
        self.df: Dict[str, int] = {}
        self.postings: Dict[str, List[int]] = {}
        self.dl: List[int] = []
        self.avgdl = 0.0

    def add(self, doc_id: str, text: str) -> None:
        toks = tokenize(text, self.mode)
        i = len(self.docs)
        self.docs.append((doc_id, toks))
        self.dl.append(len(toks))
        for w in set(toks):
            self.df[w] = self.df.get(w, 0) + 1
            self.postings.setdefault(w, []).append(i)

    def finalize(self) -> None:
        self.avgdl = (sum(self.dl) / len(self.dl)) if self.dl else 0.0

    def search(self, query: str, top_k: int = 10) -> List[Tuple[str, float]]:
        n = len(self.docs)
        if not n:
            return []
        qs = tokenize(query, self.mode)
        scores: Dict[int, float] = {}
        for w in qs:
            pl = self.postings.get(w)
            if not pl:
                continue
            df = self.df.get(w, 0)
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            for d in pl:
                tf = self.docs[d][1].count(w)
                dl = self.dl[d] or 1
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                scores[d] = scores.get(d, 0.0) + idf * tf * (self.k1 + 1) / denom
        ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
        return [(self.docs[d][0], s) for d, s in ranked[:top_k]]


# ─────────────────────────── 配对统计（纯 stdlib） ───────────────────────────
def _binom_sf(k: int, n: int) -> float:
    """P(X >= k), X~Bin(n, 0.5) —— McNemar 精确检验的单侧尾。"""
    if n <= 0 or k <= 0:
        return 1.0
    if k > n:
        return 0.0
    p = sum(math.comb(n, i) for i in range(k, n + 1)) / float(2 ** n)
    return min(1.0, p)


def mcnemar_exact(a: Sequence[int], b: Sequence[int]) -> Optional[Dict[str, Any]]:
    """配对二值（R@k）检验：只数不一致的对。"""
    b01 = sum(1 for x, y in zip(a, b) if x == 1 and y == 0)  # b 独有命中
    b10 = sum(1 for x, y in zip(a, b) if x == 0 and y == 1)  # a 独有命中
    n = b01 + b10
    if n == 0:
        return {"b01": 0, "b10": 0, "n_discordant": 0, "p_exact": 1.0,
                "method": "mcnemar-exact", "note": "两臂逐题完全相同"}
    p = min(1.0, 2 * _binom_sf(max(b01, b10), n))
    return {"b01": b01, "b10": b10, "n_discordant": n, "p_exact": round(p, 6),
            "method": "mcnemar-exact(two-sided)"}


def paired_permutation(a: Sequence[float], b: Sequence[float], iters: int = 20000,
                       seed: int = 20261006) -> Optional[Dict[str, Any]]:
    """配对置换检验（H0：差值分布对称于 0）。统计量 = mean(a) - mean(b)。"""
    if len(a) != len(b) or not a:
        return None
    d = [x - y for x, y in zip(a, b)]
    obs = sum(d) / len(d)
    if all(abs(x) < 1e-12 for x in d):
        return {"delta": 0.0, "p_perm": 1.0, "iters": 0, "method": "paired-permutation",
                "note": "逐题差值恒 0"}
    rng = random.Random(seed)
    ge = 0
    for _ in range(iters):
        s = 0.0
        for x in d:
            s += x if rng.getrandbits(1) else -x
        if abs(s / len(d)) >= abs(obs) - 1e-12:
            ge += 1
    return {"delta": round(obs, 6), "p_perm": round((ge + 1) / (iters + 1), 6),
            "iters": iters, "method": "paired-permutation(two-sided)"}


def paired_bootstrap(a: Sequence[float], b: Sequence[float], iters: int = 5000,
                     seed: int = 20261006) -> Optional[Dict[str, Any]]:
    """配对 bootstrap：对**题**重采样，给出差值的 95% CI。"""
    if len(a) != len(b) or not a:
        return None
    n = len(a)
    rng = random.Random(seed)
    ds = []
    for _ in range(iters):
        s = 0.0
        for _i in range(n):
            j = rng.randrange(n)
            s += a[j] - b[j]
        ds.append(s / n)
    ds.sort()
    lo = ds[int(0.025 * len(ds))]
    hi = ds[int(0.975 * len(ds)) - 1]
    return {"delta": round(sum(a) / n - sum(b) / n, 6), "ci95": [round(lo, 6), round(hi, 6)],
            "iters": iters, "method": "paired-bootstrap(percentile)"}


def _pct(vals: Sequence[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[idx]


# ─────────────────────────── 数据集 ───────────────────────────
def load_dataset(path: str) -> List[Dict[str, Any]]:
    data = json.load(open(path, encoding="utf-8"))
    qs = data.get("questions") if isinstance(data, dict) else data
    return [q for q in (qs or []) if q.get("question")
            and [f for f in (q.get("context_facts") or []) if (f or {}).get("fact")]]


def cmd_stats(args: argparse.Namespace) -> int:
    qs = load_dataset(args.dataset)
    facts: List[str] = []
    for q in qs:
        facts.extend(expected_of(q).keys())
    uniq = sorted(set(facts))
    dup = len(facts) - len(uniq)
    sub = 0
    for a in uniq:
        for b in uniq:
            if a != b and a in b:
                sub += 1
                break
    from collections import Counter
    c = Counter(facts)
    out = {
        "dataset": os.path.basename(args.dataset),
        "questions": len(qs),
        "expected_mentions": len(facts),
        "expected_distinct": len(uniq),
        "duplication_ratio": round(len(facts) / max(len(uniq), 1), 3),
        "facts_shared_by_multiple_questions": sum(1 for _k, v in c.items() if v > 1),
        "top_repeated_facts": c.most_common(5),
        "substring_pairs": sub,
        "substring_hazard": ("OK：无事实是另一事实的子串 ⇒ 包含判定不会一次命中多条"
                             if sub == 0 else
                             "**危险**：存在子串对 ⇒ 包含判定会放大召回"),
        "corpus_scan_verdict": (
            "**题集冗余度极高**：%d 次期望命中只落在 %d 条去重事实上（重复率 %.2fx），"
            "且 %d/%d 条事实被多题共享 ⇒ 本题集的 R@k 部分由**语料重复**而非检索能力贡献。"
            % (len(facts), len(uniq), len(facts) / max(len(uniq), 1),
               sum(1 for _k, v in c.items() if v > 1), len(uniq))),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


# ─────────────────────────── 臂定义 ───────────────────────────
# env 覆盖：本脚本**逐臂设置再恢复**（同进程），并在结果里留档实际 env。
ARMS: Dict[str, Dict[str, Any]] = {
    # ① 生产混合检索（POSIX 入口，与 quality_gate 逐字同构）
    "prod_search_hybrid": {"kind": "trinity", "env": {}, "call": "search_hybrid_mode"},
    # ①L 走**引擎入口**并显式指定 routing=light（2026-10-06 修：`search()` 新增
    #    `routing` 透传。修之前 `Trinity.search(mode="hybrid", routing="light")` 会
    #    **TypeError**（无该参数）⇒ light 档对引擎入口不可达、恒走 full）。
    #    本臂 = 该透传的**前后对比**：它的 p50 应与 `routing_light`（经 search_hybrid
    #    直连）同量级，而**修复前**根本无法经引擎入口测到。
    "prod_search_hybrid_light": {"kind": "trinity", "env": {},
                                 "call": "search_hybrid_mode", "routing": "light"},
    # ①b 生产混合检索（绕过 `search()` 的空结果回退，直接量融合路径）
    "prod_hybrid_direct": {"kind": "trinity", "env": {}, "call": "search_hybrid_direct",
                           "capture_scores": True},
    # ② 单通道：词法/关键词（FTS5）
    "chan_keyword": {"kind": "trinity", "env": {}, "call": "search_keyword",
                     "capture_scores": True},
    # ③ 向量通道：**本臂已删除**（2026-10-06 t19 更正）。
    #    曾命名为 `chan_vector_embed`（`TRINITY_VECTOR_CHANNEL=auto` +
    #    `TRINITY_EMBED_BACKEND=onnx`，期望测"真嵌入通道"），但它**在 SQLite 上测不到嵌入**：
    #      · `_use_embedding_channel(adapter, False)` 在 **mode="auto" 且非 PG 且 use_ann=False**
    #        时返回 **False**（`_hybrid_index.py:91-94`：非 PG 未开 ANN 时**故意**沿用词法，
    #        理由是 `_vector_search` 会走"拉全量 + 内存建索引"重路）⇒ 该臂仍是词法；
    #      · 于是它**恒等于** `chan_keyword`（n=50 冒烟实测：两臂 R@5/R@10/MRR/nDCG 逐项相同）。
    #    ⇒ **"看起来测嵌入、实际测词法"正是本任务要消灭的中间态**，故直接删除而不是留着。
    #    真嵌入通道的读数需要：非 PG + `use_ann=True`，或 PG 适配器（见报告 §11.3）。
    # ④ 引擎的 hybrid 模式（无 hybrid retriever 时退化为 FTS）
    "engine_search_hybrid": {"kind": "trinity", "env": {}, "call": "search_hybrid_mode",
                             "no_hybrid_retriever": True},
    # ⑤ RRF 权重逐个归零（full 融合的**活旋钮**：vector/bm25/graph）
    "w_vector0": {"kind": "trinity", "env": {"TRINITY_VECTOR_WEIGHT": "0"},
                  "call": "search_hybrid_direct"},
    "w_bm25_0": {"kind": "trinity", "env": {"TRINITY_BM25_WEIGHT": "0"},
                 "call": "search_hybrid_direct"},
    "w_graph0": {"kind": "trinity", "env": {"TRINITY_GRAPH_WEIGHT": "0"},
                 "call": "search_hybrid_direct"},
    # ⑤b 只留 BM25（vector/graph 全关）——"单通道"里最强的那一路
    "w_bm25_only": {"kind": "trinity",
                    "env": {"TRINITY_VECTOR_WEIGHT": "0", "TRINITY_GRAPH_WEIGHT": "0"},
                    "call": "search_hybrid_direct"},
    # ⑥ 后置阶段逐个关（这些是直连 full 路径的）
    "no_rerank": {"kind": "trinity", "env": {"TRINITY_CROSSENCODER_RERANK": "off"},
                  "call": "search_hybrid_direct"},
    "no_scoped_topup": {"kind": "trinity", "env": {"TRINITY_SCOPED_TOPUP": "off"},
                        "call": "search_hybrid_direct"},
    # ⑦ 自适应路由的 light 档（FTS 快通道 + light 多通道）。
    #    注意：`search()` **没有 routing 参数**（`_search.py` 全仓零匹配）⇒ 只能经
    #    `search_hybrid(routing="light")` 到达；生产 `search()` 入口**恒走 full**
    #    ⇒ 文档里那句"短查询走 light 快通道"对引擎入口**不成立**（见交付报告）。
    "routing_light": {"kind": "trinity", "env": {}, "call": "search_hybrid_direct",
                      "routing": "light"},
    # ⑦f/⑦g **routing 的严格 A/B**（2026-10-06 t19 更正用）。
    #    背景：t3 报告里把 `prod_search_hybrid`(0.9140/0.7282) 与 `prod_search_hybrid_light`
    #    (0.9160/0.8729) 写成"逐项相同" —— **错**。后者经 `search()` 入口，其 routing 未显式
    #    给值时 `_search.py` 不透传 ⇒ 仍是 `search_hybrid(routing="auto")`，而**题集查询全部
    #    >8 字符** ⇒ auto 判 full ⇒ 它**不是 light 臂**，只是 full 的另一条链路。
    #    真正的 light/full 对比必须**同一条链路**（直连融合），只改 routing：
    "prod_hybrid_direct_light": {"kind": "trinity", "env": {},
                                 "call": "search_hybrid_direct", "routing": "light",
                                 "capture_scores": True},
    "prod_hybrid_direct_full": {"kind": "trinity", "env": {},
                                "call": "search_hybrid_direct", "routing": "full",
                                "capture_scores": True},
    # ⑦u 不带 persona 过滤的对照（作用域 vs 机制 归因必需）
    "chan_keyword_nofilter": {"kind": "trinity", "env": {}, "call": "search_keyword",
                              "no_persona": True},
    "prod_hybrid_direct_nofilter": {"kind": "trinity", "env": {},
                                    "call": "search_hybrid_direct", "no_persona": True},
    "prod_search_hybrid_nofilter": {"kind": "trinity", "env": {},
                                    "call": "search_hybrid_mode", "no_persona": True},
    # ⑦v **词法候选门槛变体**（"修复 0 贡献通道"的候选修法）。
    #    现行 `_search_fts` 把词条拼成 `t1* OR t2* OR ...`（纯 OR）⇒ 任何一词命中即入
    #    候选，长/原句查询的候选池被无关文档灌满。实测 120 条多词查询的 OR 候选
    #    均值 60.4 条（p95 94），而 **AND 候选恒为 0 条（120/120 全空）** ⇒
    #    "AND 优先"这条修法**不可能生效**（AND 分支永远空手，等价于现状）。
    #    故这里测的是**有下界的 OR**：至少命中 j 个不同查询词才进候选，再用 jieba 实词
    #    覆盖率排序；不足 top_k 时按 rank 顺序用剩余 OR 候选补齐（保持召回不下降）。
    "fts_or_min2": {"kind": "fts_variant", "min_terms": 2, "pool": 60},
    "fts_or_min3": {"kind": "fts_variant", "min_terms": 3, "pool": 60},
    "fts_or_cov": {"kind": "fts_variant", "min_terms": 2, "pool": 60, "coverage_rank": True},
    # ⑧ no-op 上界：不加任何排序，直接取该 persona 的全部事实前 top_k。
    "oracle_persona": {"kind": "oracle"},
    # ⑧b **doc_route 专用闸门臂**（2026-10-06，应队长追加测量）：
    #    `trinity/retrieval/doc_router.py::fetch_doc_chunks` 在 SQLite 上原为**恒返 []**
    #    （守卫 `hasattr(adapter, "_get_conn")` 是方言盲的：`_get_conn` 只有 PG 有）
    #    ⇒ 注入阶段恒空 ⇒ 历史"doc_route 开启后无实质变化"的读数**是在注入死掉的前提下测的**。
    #    该修复（capability-hygiene/t14）落在 `trinity/retrieval/`（**不在我的写域**），
    #    本脚本只负责**在同口径下重测** on/off，并披露"注入是否真的发生"。
    "doc_route_off": {"kind": "doc_route", "on": False},
    "doc_route_on": {"kind": "doc_route", "on": True},
    # ⑨ 零依赖 BM25（**不经 Trinity**：自包含实现 + 全量题集词表）
    "pure_bm25": {"kind": "bm25", "env": {}, "mode": "regex"},
    # ⑦b 零依赖 BM25 + jieba（量分词对英文题集的影响；中文语料上才是它的主场）
    "pure_bm25_jieba": {"kind": "bm25", "env": {}, "mode": "both"},
}


def _apply_env(env: Dict[str, str]) -> Dict[str, Any]:
    old = {}
    for k, v in (env or {}).items():
        old[k] = os.environ.get(k)
        os.environ[k] = v
    return old


def _restore_env(old: Dict[str, Any]) -> None:
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _contents(res: Any) -> List[str]:
    if isinstance(res, dict):
        rows = res.get("results") or []
    else:
        rows = res or []
    return [str(r.get("content") or "") for r in rows if isinstance(r, dict)]


def _scores(res: Any) -> List[float]:
    """返回结果行的分数序列（**用于并列统计**）。

    为什么必须记：`capability-hygiene` 在合成语料上发现整个 top-5 **五条同分**（1.01）
    ⇒ 取哪 5 条完全由 `PYTHONHASHSEED` 决定，即**该语料上的"排序"没有判别力**。
    任何 A/B 若落在"并列内部"，测到的就是随机性而不是通道效果。故本函数把分数序列
    逐题留档，供 `tie_stats` 出并列占比与并列条数分布。
    """
    rows = (res or {}).get("results") if isinstance(res, dict) else (res or [])
    out: List[float] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        v = r.get("hybrid_score")
        if v is None:
            v = r.get("score")
        try:
            out.append(round(float(v), 6))
        except (TypeError, ValueError):
            out.append(float("nan"))
    return out


def build_store(dataset: str, limit: int, reuse: str = ""):
    """建临时 SQLite store 并摄入长记忆题集语料（与 quality_gate 同构）。

    `reuse`（`--store`）：复用已建好的 store 目录 —— 迭代式基准（改一个门槛重跑一条臂）
    不必每次重付 2~4 分钟摄入成本。**注意**：复用时不重新摄入，语料即为该目录内容；
    报告里记录所用 store 路径使结果可追溯。
    """
    from trinity import Trinity
    import tempfile
    qs = load_dataset(dataset)[:limit]
    if reuse and os.path.isdir(reuse):
        return Trinity(adapter="sqlite", store_path=reuse), qs, reuse, -1, 0, 0.0
    store = tempfile.mkdtemp(prefix="rc_bench_")
    mem = Trinity(adapter="sqlite", store_path=store)
    t0 = time.time()
    n_ing = 0
    n_ing_failed = 0
    for q in qs:
        for f in q.get("context_facts") or []:
            ft = (f or {}).get("fact", "")
            if not ft:
                continue
            try:
                mem.ingest(ft, persona_id=q.get("persona_name") or "default",
                           session_id=str((f or {}).get("session_id") or q.get("session_id") or "0"),
                           category=q.get("category", "general"),
                           tags=["rc", q.get("category", "")])
                n_ing += 1
            except Exception as exc:  # noqa: BLE001 —— 单条摄入失败不中断整批
                n_ing_failed += 1
                if n_ing_failed <= 3:   # 只留前 3 条样本，避免刷屏
                    print("[rc]   ingest failed: %s: %s" % (type(exc).__name__, exc))
    return mem, qs, store, n_ing, n_ing_failed, round(time.time() - t0, 2)


def run_arm(name: str, spec: Dict[str, Any], mem, qs: List[Dict[str, Any]],
            top_k: int, corpus: str, pure: Optional[PureBM25]) -> Dict[str, Any]:
    kind = spec.get("kind")
    old_env = _apply_env(spec.get("env") or {})
    try:
        if kind == "bm25":
            return _run_bm25_arm(name, spec, qs, top_k, pure)
        if kind == "oracle":
            return _run_oracle_arm(name, spec, mem, qs, top_k)
        if kind == "fts_variant":
            return _run_fts_variant_arm(name, spec, mem, qs, top_k)
        if kind == "doc_route":
            return _run_doc_route_arm(name, spec, mem, qs, top_k)
        return _run_trinity_arm(name, spec, mem, qs, top_k, corpus)
    finally:
        _restore_env(old_env)


def _run_doc_route_arm(name, spec, mem, qs, top_k) -> Dict[str, Any]:
    """doc_route on/off 的**同口径**对比臂。

    三个必须披露的量（否则就是重复上一次的错误）：
      · `chunks_total`  —— `fetch_doc_chunks` 实际取到多少章节块（**注入的前置条件**）；
      · `injected_total`—— `rerank` 实际把多少条块注入了结果；
      · `queries_with_injection` —— 有注入的查询数。
    `chunks_total == 0` ⇒ 本次读数**无效**（注入死掉），必须在报告里写明而不是报"无实质变化"。
    """
    os.environ["TRINITY_DOC_ROUTE"] = "on" if spec.get("on") else "off"
    res: Dict[str, Any] = {"arm": name, "kind": "doc_route", "env": {"TRINITY_DOC_ROUTE":
                                                                    os.environ["TRINITY_DOC_ROUTE"]},
                           "call": "search_hybrid(include_docs=True)",
                           "per_query": [], "lat": [], "chunks_total": 0, "injected_total": 0,
                           "queries_with_injection": 0, "queries_with_chunks": 0}
    try:
        import trinity.retrieval.doc_router as _dr
    except Exception as exc:  # noqa: BLE001
        res["import_error"] = "%s: %s" % (type(exc).__name__, exc)
        return res
    for q in qs:
        exp = expected_of(q)
        pid = q.get("persona_name") or None
        t0 = time.perf_counter()
        try:
            r = mem.search_hybrid(query=q["question"], top_k=top_k, strategy="rrf",
                                  persona_id=pid, include_docs=True)
        except Exception as exc:  # noqa: BLE001
            r = {"results": [], "_error": "%s: %s" % (type(exc).__name__, exc)}
        dt = (time.perf_counter() - t0) * 1000
        contents = _contents(r)
        # 注入事件数：doc_route 的注入行带 `channels`/来源标记，这里用最保守的口径
        # ——统计"结果里 category 以 doc: 开头"的条数（注入的章节块就是 doc:* 行）。
        n_doc_rows = sum(1 for row in (r.get("results") or [])
                         if str(row.get("category") or "").startswith("doc:"))
        if n_doc_rows:
            res["injected_total"] += n_doc_rows
            res["queries_with_injection"] += 1
        sc = score_query(contents, exp)
        sc.update({"qid": q.get("question_id"), "category": q.get("category"),
                   "latency_ms": round(dt, 3), "n_returned": len(contents),
                   "doc_rows": n_doc_rows})
        res["per_query"].append(sc)
        res["lat"].append(dt)
        if isinstance(r, dict) and isinstance(r.get("breakdown"), dict):
            res.setdefault("breakdowns", []).append(r["breakdown"])
        if isinstance(r, dict) and r.get("doc_route_chunks") is not None:
            res["chunks_total"] += int(r.get("doc_route_chunks") or 0)
    # 直接探一次 `fetch_doc_chunks`（若适配器里有 doc:* 行）：把"注入前置条件"量出来
    try:
        sample = qs[0].get("persona_name") if qs else None
        rows = mem._adapter.get_all_memories(limit=500)
        docs = {str((row.get("metadata") or {}).get("source_file") or "")
                for row in rows if str(row.get("category") or "").startswith("doc:")}
        docs.discard("")
        for d in list(docs)[:5]:
            res["chunks_total"] += len(_dr.fetch_doc_chunks(mem._adapter, d, limit=6) or [])
            res["queries_with_chunks"] += 1
        res["doc_source_files_in_store"] = len(docs)
        del sample
    except Exception as exc:  # noqa: BLE001
        res["chunks_probe_error"] = "%s: %s" % (type(exc).__name__, exc)
    return res


def _fts_pool(ad, query: str, where: str, params: List[Any], pool: int) -> List[Dict[str, Any]]:
    """按 `_search_fts` 的**同一套 SQL 口径**取 OR 候选池（不截断到 top_k）。

    与 `trinity/adapters/sqlite/_search.py::_search_fts` 的唯一差别是把 LIMIT 放到
    `pool`（该函数签名里 top_k 就是 LIMIT）。此处**故意不信**它返回的顺序之外的东西。
    """
    terms = ad._tokenize_fts_query(query)[:64]
    safe = [t.replace('"', '""') for t in terms if t.strip()]
    if not safe:
        return []
    fts_query = " OR ".join('"%s"*' % t for t in safe)
    sql = f"""
        SELECT m.memory_id, m.content, m.persona_id, m.category, m.importance,
               fts.rank as score
        FROM memories m
        INNER JOIN (SELECT rowid, rank FROM memories_fts
                    WHERE memories_fts MATCH ?) fts ON m.rowid = fts.rowid
        WHERE {where}
        ORDER BY score LIMIT ?
    """
    conn = ad._get_read_conn()
    if not conn:
        return []
    cur = conn.execute(sql, [fts_query] + list(params) + [pool])
    out = []
    for row in cur.fetchall():
        out.append({"memory_id": row["memory_id"], "content": row["content"],
                    "persona_id": row["persona_id"], "category": row["category"],
                    "importance": row["importance"], "score": row["score"]})
    return out


def _plain_content(ad: Any, row: Any) -> str:
    """取检索行的**明文**内容。

    坑（2026-10-06 实测）：SQLite 库里 `content` 列是 `enc:v1:` 密文，而 `_search_fts`
    在返回结果时用 `_decrypt_text_resilient` 解密 —— 任何**绕开该出口**的统计（例如本脚本
    的候选门槛变体）必须自己解密，否则在密文上做子串匹配、覆盖率恒为 0，
    看上去"变体无效"而其实是口径错了。
    """
    raw = row.get("content") if isinstance(row, dict) else row
    fn = getattr(ad, "_decrypt_text_resilient", None)
    if callable(fn):
        try:
            return str(fn(raw, memory_id=(row or {}).get("memory_id"),
                          where="rc/_plain_content") or "")
        except Exception:  # noqa: BLE001 —— 解密失败即回退原值（下面还会判 enc: 前缀）
            logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/retrieval_contribution.py::_plain_content")
    s = str(raw or "")
    if s.startswith("enc:v1:"):
        try:
            from trinity.security.crypto import decrypt_content
            return str(decrypt_content(s) or s)
        except Exception:  # noqa: BLE001
            return s
    return s


def _run_fts_variant_arm(name, spec, mem, qs, top_k) -> Dict[str, Any]:
    """词法候选门槛变体（见 ARMS 里 `fts_or_min*` 的说明）。"""
    ad = mem._adapter
    res = {"arm": name, "kind": "fts_variant", "env": {},
           "call": "adapter FTS OR pool + min_terms 门槛",
           "min_terms": spec.get("min_terms", 2), "pool": spec.get("pool", 60),
           "per_query": [], "lat": []}
    for q in qs:
        exp = expected_of(q)
        pid = q.get("persona_name") or None
        where = "m.status = 'active'"
        params: List[Any] = []
        if pid:
            where += " AND m.persona_id = ?"
            params.append(pid)
        where += " AND (m.category NOT LIKE 'doc:%' AND m.category NOT LIKE 'doc_%')"
        t0 = time.perf_counter()
        pool = _fts_pool(ad, q["question"], where, params, int(spec.get("pool", 60)))
        # 覆盖统计用**词根**而不是 `_tokenize_fts_query` 的输出：后者对**非 CJK** 输入
        # 走 `query.strip().split()`，于是 `"What did Jack run in Sydney in September 2024?"`
        # 会切出 `"2024?"`（带问号）这种**永远匹配不到索引词**的"词条" —— 实测每行覆盖数
        # 恒为 0（本臂首版三臂全 0.0000 就是这个原因，见 2026-10-06 踩坑记录）。
        # 改为抽 `[A-Za-z0-9']{3,}` 的词根（>=3 字符以滤掉 the/of/and 这类纯噪声），
        # 两边统一 casefold（FTS5 unicode61 建索引时已折叠大小写）。
        terms = sorted({w for w in re.findall(r"[A-Za-z0-9']{3,}", q["question"].lower())})
        keep, rest = [], []
        for row in pool:
            # **必须先解密**：库里 `content` 是 `enc:v1:` 密文（实测第一行内容形如
            # `enc:v1:CLvix23z9RidohpDDkmM1NCGBAyYUrtqE`）⇒ 直接在密文上做子串覆盖，
            # 覆盖数恒为 0（本臂前两版三臂全 0.0000 的第二个原因）。
            c = norm(_plain_content(ad, row)).casefold()
            n = sum(1 for t in terms if t in c)
            (keep if n >= int(spec.get("min_terms", 2)) else rest).append((n, row))
        if spec.get("coverage_rank") and keep:
            keep.sort(key=lambda x: -x[0])
        order = [r for _n, r in keep] + [r for _n, r in rest]
        contents = [_plain_content(ad, r) for r in order[:top_k]]
        dt = (time.perf_counter() - t0) * 1000
        sc = score_query(contents, exp)
        sc.update({"qid": q.get("question_id"), "category": q.get("category"),
                   "latency_ms": round(dt, 3), "n_returned": len(contents),
                   "pool_size": len(pool), "n_kept": len(keep)})
        res["per_query"].append(sc)
        res["lat"].append(dt)
    return res


def _run_oracle_arm(name, spec, mem, qs, top_k) -> Dict[str, Any]:
    """上界臂：不加任何排序，直接把该 persona 的全部事实按库内顺序取前 top_k。"""
    res = {"arm": name, "kind": "oracle", "env": {}, "call": "adapter.get_all_memories",
           "per_query": [], "lat": [], "oracle_pool": {}}
    pool: Dict[str, List[str]] = {}
    # 口径注意：**不能用 `get_index_documents`** —— 它只回 (memory_id, content)
    # 两列（见 postgresql.py:1178 / sqlite/_crud.py:781 的契约），取不到 persona_id。
    try:
        allrows = mem._adapter.get_all_memories(limit=100000) or []
    except Exception:
        allrows = []
    for r in allrows:
        pool.setdefault(str(r.get("persona_id") or ""), []).append(str(r.get("content") or ""))
    res["oracle_pool"] = {p: len(v) for p, v in pool.items()}
    for q in qs:
        exp = expected_of(q)
        pid = str(q.get("persona_name") or "")
        t0 = time.perf_counter()
        contents = pool.get(pid, [])[:top_k]
        dt = (time.perf_counter() - t0) * 1000
        sc = score_query(contents, exp)
        sc.update({"qid": q.get("question_id"), "category": q.get("category"),
                   "latency_ms": round(dt, 3), "n_returned": len(contents),
                   "pool_size": len(pool.get(pid, []))})
        res["per_query"].append(sc)
        res["lat"].append(dt)
    return res


def _run_bm25_arm(name, spec, qs, top_k, pure) -> Dict[str, Any]:
    call = spec.get("call", "search_hybrid_direct")
    res = {"arm": name, "kind": "bm25", "env": spec.get("env") or {},
           "mode": spec.get("mode", "regex"), "per_query": [], "lat": []}
    for q in qs:
        exp = expected_of(q)
        t0 = time.perf_counter()
        hits = pure.search(q["question"], top_k=top_k)
        dt = (time.perf_counter() - t0) * 1000
        contents = [h[0] for h in hits]
        sc = score_query(contents, exp)
        sc.update({"qid": q.get("question_id"), "category": q.get("category"),
                   "latency_ms": round(dt, 3), "n_returned": len(hits)})
        res["per_query"].append(sc)
        res["lat"].append(dt)
    return res


def _run_trinity_arm(name, spec, mem, qs, top_k, corpus) -> Dict[str, Any]:
    call = spec.get("call")
    res = {"arm": name, "kind": "trinity", "env": spec.get("env") or {}, "call": call,
           "per_query": [], "lat": [], "breakdowns": [], "fallbacks": []}
    if spec.get("no_hybrid_retriever"):
        mem._hybrid_retriever = None  # 强制 `search(mode="hybrid")` 走 FTS 回退
    if call in ("search_hybrid_direct",):
        try:
            mem._wait_bm25_ready()
        except Exception as exc:  # noqa: BLE001
            # BM25 预热失败 ⇒ 该臂的 bm25 通道恒空。**必须记下来**：否则"BM25 零贡献"
            # 会被读成"BM25 通道是摆设"，而实际只是这次预热没成功。
            res["bm25_wait_error"] = "%s: %s" % (type(exc).__name__, exc)
    # 向量通道自查：`search()`/`search_hybrid` 在向量不可用或**后端结构性不可达**时会
    # **静默回退词法**（实测两例：① 缺 sklearn ⇒ 每题一行 "向量搜索失败，回退到纯 SQLite 搜索"；
    # ② SQLite + `use_ann=False` 时 `_use_embedding_channel` 对任何值都返回 False ⇒ 连嵌入式都
    # 不试）。**每一臂都记录**，因为"测量工具缺失会让某个臂静默变成另一个臂"（t19 教训）。
    res["vector_preflight"] = _vector_preflight(mem)

    for q in qs:
        exp = expected_of(q)
        pid = None if spec.get("no_persona") else (q.get("persona_name") or None)
        t0 = time.perf_counter()
        try:
            if call == "search_keyword":
                r = mem.search(query=q["question"], mode="keyword", top_k=top_k, persona_id=pid)
            elif call == "search_semantic":
                r = mem.search(query=q["question"], mode="semantic", top_k=top_k,
                               persona_id=pid, use_vector=True)
            elif call == "search_hybrid_mode":
                kw = {"query": q["question"], "mode": "hybrid", "top_k": top_k,
                      "persona_id": pid}
                if spec.get("routing"):
                    kw["routing"] = spec["routing"]
                r = mem.search(**kw)
            else:
                r = mem.search_hybrid(query=q["question"], top_k=top_k, strategy="rrf",
                                      persona_id=pid, routing=spec.get("routing", "auto"))
        except Exception as exc:  # noqa: BLE001
            r = {"results": [], "_error": "%s: %s" % (type(exc).__name__, exc)}
        dt = (time.perf_counter() - t0) * 1000
        contents = _contents(r)
        sc = score_query(contents, exp)
        sc.update({"qid": q.get("question_id"), "category": q.get("category"),
                   "latency_ms": round(dt, 3), "n_returned": len(contents),
                   "scores": (_scores(r) if spec.get("capture_scores") else None)})
        if r.get("_error"):
            sc["error"] = r["_error"]
        res["per_query"].append(sc)
        res["lat"].append(dt)
        if isinstance(r, dict) and isinstance(r.get("breakdown"), dict):
            res["breakdowns"].append(r["breakdown"])
        if isinstance(r, dict) and r.get("scoped_topup"):
            res.setdefault("scoped_topup", []).append(r["scoped_topup"])
    return res


def _vector_preflight(mem) -> Dict[str, Any]:
    """一次真实向量检索探测：返回**实际实现名** + 是否发生静默回退 + 异常类型。

    关键字段 `impl`：`lexical:*` 表示该臂的 `vector` 通道**根本不是向量**
    （`_use_embedding_channel` 返回 False）。t19 就是靠它发现 `chan_vector_embed`
    这一臂在 SQLite 上恒等于 `chan_keyword`（整个臂是假的）。
    """
    out: Dict[str, Any] = {"ok": False}
    try:
        rows = mem._vector_search("probe query", 3)
        out["ok"] = True
        out["n"] = len(rows or [])
        out["silent_empty"] = (len(rows or []) == 0)  # 空结果也可能是"内部吞了异常"
    except Exception as exc:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        from trinity.core.client._hybrid_index import (
            _use_embedding_channel as _uec, vector_channel_impl)
        out["uses_embedding"] = bool(_uec(mem._adapter, getattr(mem, "use_ann", False)))
        out["impl"] = vector_channel_impl(mem._adapter, getattr(mem, "use_ann", False))
    except Exception as exc:  # noqa: BLE001
        out["impl_error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        from trinity.embeddings.engine import create_engine
        eng = create_engine(backend=os.environ.get("TRINITY_EMBED_BACKEND") or "auto")
        inner = getattr(eng, "_engine", None) or getattr(eng, "engine", None) or eng
        out["embed_backend"] = type(inner).__name__
    except Exception as exc:  # noqa: BLE001
        out["embed_backend_error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


# ─────────────────────────── 汇总 / 出表 ───────────────────────────
def aggregate(res: Dict[str, Any]) -> Dict[str, Any]:
    pq = res["per_query"]
    n = len(pq)
    agg: Dict[str, Any] = {"arm": res["arm"], "n": n, "env": res.get("env") or {},
                           "kind": res.get("kind"), "call": res.get("call")}
    if not n:
        return agg
    for k in K_LIST:
        agg["R@%d" % k] = round(sum(x["r@%d" % k] for x in pq) / n, 4)
        agg["recall@%d" % k] = round(sum(x["recall@%d" % k] for x in pq) / n, 4)
        agg["unjudged@%d" % k] = round(sum(x["unjudged@%d" % k] for x in pq) / n, 4)
    agg["MRR"] = round(sum(x["mrr"] for x in pq) / n, 4)
    nd = [x["ndcg@5"] for x in pq if x.get("ndcg@5") is not None]
    agg["nDCG@5"] = round(sum(nd) / len(nd), 4) if nd else None
    agg["mean_returned"] = round(sum(x.get("n_returned", 0) for x in pq) / n, 2)
    agg["empty_rate"] = round(sum(1 for x in pq if x.get("n_returned", 0) == 0) / n, 4)
    lat = res.get("lat") or []
    agg["p50_ms"] = round(_pct(lat, 0.50), 1)
    agg["p95_ms"] = round(_pct(lat, 0.95), 1)
    agg["sum_s"] = round(sum(lat) / 1000.0, 1)
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for x in pq:
        by_cat.setdefault(str(x.get("category")), []).append(x)
    agg["per_category_R@5"] = {c: round(sum(y["r@5"] for y in v) / len(v), 4)
                               for c, v in sorted(by_cat.items())}
    if res.get("breakdowns"):
        agg["breakdown_nonzero_rate"] = _bd_rate(res["breakdowns"])
    if res.get("scoped_topup"):
        st = res["scoped_topup"]
        agg["scoped_topup"] = {"queries": len(st),
                               "added_total": sum(int(s.get("added") or 0) for s in st),
                               "queries_with_added": sum(1 for s in st if s.get("added"))}
    return agg


def _bd_rate(bds: List[Dict[str, Any]]) -> Dict[str, Any]:
    keys = set()
    for b in bds:
        for k, v in b.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                keys.add(k)
    out = {}
    for k in sorted(keys):
        nz = sum(1 for b in bds if isinstance(b.get(k), (int, float)) and b.get(k))
        tot = sum(float(b.get(k) or 0) for b in bds if isinstance(b.get(k), (int, float)))
        out[k] = {"queries_nonzero": nz, "n_queries": len(bds),
                  "nonzero_rate": round(nz / max(len(bds), 1), 4), "sum": round(tot, 1)}
    vc = {}
    for b in bds:
        v = b.get("vector_channel")
        if v is not None:
            vc[str(v)] = vc.get(str(v), 0) + 1
    if vc:
        out["_vector_channel_impl"] = vc
    return out


ARM_GROUPS = [
    ("对照组（带 persona 作用域）",
     ["prod_search_hybrid", "prod_hybrid_direct", "oracle_persona",
      "pure_bm25", "pure_bm25_jieba"]),
    ("对照组（**不加 persona** 作用域，直接比机制）",
     ["prod_search_hybrid_nofilter", "prod_hybrid_direct_nofilter",
      "chan_keyword_nofilter"]),
    ("单通道 / 路由",
     ["chan_keyword", "engine_search_hybrid", "routing_light",
      "prod_hybrid_direct_light", "prod_hybrid_direct_full",
      "prod_search_hybrid_light", "w_bm25_only"]),
    ("词法候选门槛变体（多词 FTS5 的候选修法实测）",
     ["fts_or_min2", "fts_or_min3", "fts_or_cov"]),
    ("doc_route 专用闸门（注入前置条件必须一并报）",
     ["doc_route_off", "doc_route_on"]),
    ("权重归零（消融）", ["w_vector0", "w_bm25_0", "w_graph0"]),
    ("后置阶段消融", ["no_rerank", "no_scoped_topup"]),
]


def cmd_table(args: argparse.Namespace) -> int:
    blob = json.load(open(args.infile, encoding="utf-8"))
    arms: Dict[str, Any] = blob["arms"]
    aggs = {k: aggregate(v) for k, v in arms.items()}
    order = [a for _g, lst in ARM_GROUPS for a in lst if a in aggs]
    order += [a for a in aggs if a not in order]

    L: List[str] = []
    L.append("## 逐通道 / 逐臂贡献表")
    L.append("")
    L.append("口径：见 `scripts/retrieval_contribution.py` 模块 docstring；"
             "题集 `%s`，查询数 n=%d，PYTHONHASHSEED=%s"
             % (blob.get("dataset"), blob.get("limit"), blob.get("py_hash_seed")))
    L.append("")
    L.append("| 臂 | 类别 | R@1 | R@5 | R@10 | recall@5 | MRR | nDCG@5 | unjudged@5 | 空结果率 | p50 ms | 耗时 s |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for a in order:
        g = aggs[a]
        if not g.get("n"):
            L.append("| %s | %s | — | — | — | — | — | — | — | — | — | — |"
                     % (a, g.get("kind")))
            continue
        L.append("| %s | %s | %.4f | %.4f | %.4f | %.4f | %.4f | %s | %.4f | %.4f | %.1f | %.1f |"
                 % (a, g.get("kind"), g["R@1"], g["R@5"], g["R@10"], g["recall@5"],
                    g["MRR"], ("%.4f" % g["nDCG@5"]) if g["nDCG@5"] is not None else "n/a",
                    g["unjudged@5"], g["empty_rate"], g["p50_ms"], g["sum_s"]))
    L.append("")

    base = args.baseline
    if base in arms:
        L.append("## 显著性（配对，基准臂 = `%s`）" % base)
        L.append("")
        L.append("三法并列：**McNemar 精确**（只对 R@5 的 0/1 计数，最稳）、"
                 "**配对置换**（对 MRR / nDCG@5 这类连续量）、**配对 bootstrap** 95% CI。"
                 "b01/b10 = 对方独有命中 / 基准独有命中。")
        L.append("")
        L.append("| 臂 | ΔR@5 | b01 | b10 | p(McNemar) | ΔMRR | p(置换) | ΔnDCG@5 | ΔnDCG 95%CI | 判定 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        bq = {x["qid"]: x for x in arms[base]["per_query"]}
        for a in order:
            if a == base:
                continue
            aq = {x["qid"]: x for x in arms[a]["per_query"]}
            common = [q for q in bq if q in aq]
            if not common:
                continue
            r_b = [bq[q]["r@5"] for q in common]
            r_a = [aq[q]["r@5"] for q in common]
            mrr_b = [bq[q]["mrr"] for q in common]
            mrr_a = [aq[q]["mrr"] for q in common]
            nd_b = [bq[q]["ndcg@5"] or 0.0 for q in common]
            nd_a = [aq[q]["ndcg@5"] or 0.0 for q in common]
            mc = mcnemar_exact(r_a, r_b)
            pm = paired_permutation(mrr_a, mrr_b, iters=args.iters)
            pn = paired_permutation(nd_a, nd_b, iters=args.iters)
            bst = paired_bootstrap(nd_a, nd_b, iters=min(5000, args.iters))
            d5 = sum(r_a) / len(r_a) - sum(r_b) / len(r_b)
            verdict = _verdict(mc, pm, pn)
            L.append("| %s | %+.4f | %d | %d | %s | %+.4f | %s | %+.4f | [%+.4f,%+.4f] | %s |"
                     % (a, d5, mc["b01"], mc["b10"], mc["p_exact"],
                        pm["delta"], pm["p_perm"], pn["delta"],
                        bst["ci95"][0], bst["ci95"][1], verdict))
        L.append("")
        L.append("判定规则：p<0.05 记「显著」，否则记「无显著差异（仪器分辨不出）」——"
                 "**不得**读成「等价」；CI 跨 0 亦同。")
        L.append("")

    L.append("## 语料体检（读数字前必看）")
    L.append("")
    for k, v in (blob.get("corpus_stats") or {}).items():
        L.append("- `%s` = %s" % (k, json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v))
    L.append("")

    L.append("## 响应内通道计数（breakdown 非零率）")
    L.append("")
    L.append("| 臂 | " + " | ".join(sorted({kk for a in arms.values()
                                            for kk in (aggregate(a).get("breakdown_nonzero_rate") or {})
                                            if kk != "_vector_channel_impl"})) + " |")
    L.append("|---|" + "---|" * len({kk for a in arms.values()
                                     for kk in (aggregate(a).get("breakdown_nonzero_rate") or {})
                                     if kk != "_vector_channel_impl"}))
    keys = sorted({kk for a in arms.values()
                   for kk in (aggregate(a).get("breakdown_nonzero_rate") or {})
                   if kk != "_vector_channel_impl"})
    for a in order:
        g = aggregate(arms[a]).get("breakdown_nonzero_rate") or {}
        if not g:
            continue
        L.append("| %s | " % a + " | ".join(
            ("%d/%d" % (g[k]["queries_nonzero"], g[k]["n_queries"])) if k in g else "—"
            for k in keys) + " |")
    L.append("")
    text = "\n".join(L)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0


def _verdict(mc: Dict[str, Any], pm: Optional[Dict[str, Any]],
             pn: Optional[Dict[str, Any]]) -> str:
    ps = [mc.get("p_exact")]
    if pm:
        ps.append(pm.get("p_perm"))
    if pn:
        ps.append(pn.get("p_perm"))
    ps = [p for p in ps if p is not None]
    if not ps:
        return "n/a"
    if min(ps) >= 0.05:
        return "无显著差异"
    return "显著（见各 p）"


# ─────────────────────── 并列 / 同分诊断（tie_stats） ───────────────────────
def tie_stats(per_query: Sequence[Dict[str, Any]], k: int = 5) -> Dict[str, Any]:
    """**截断处同分**统计：第 k 名与第 k+1 名分数相等 ⇒ 取哪 k 条由并列次序决定（≈随机）。

    动机（`capability-hygiene` 自查发现，2026-10-06）：合成语料上整个 top-5 五条**同分**
    （1.01）⇒ 取哪 5 条完全由 `PYTHONHASHSEED` 决定 ⇒ 该语料上"排序无判别力"。
    若这在生产口径上也普遍，那么任何 A/B 在这类查询上测到的是**并列内的任意次序**，
    而不是通道质量 ⇒ 必须**单列或排除**，不得据此下"通道优劣"结论。
    """
    n = 0
    n_scores_eq_at_k = 0
    n_fully_tied = 0
    tie_group_sizes: List[int] = []
    tie_group_of_k: List[int] = []       # 含第 k 名的那一组的条数
    tie_frac_of_k: List[float] = []      # 该组条数 / k
    for x in per_query:
        s = x.get("scores")
        if not s:
            continue
        n += 1
        vals = [v for v in s if v == v]  # 去 NaN
        if len(vals) <= k:
            continue
        if abs(vals[k - 1] - vals[k]) > 1e-9:
            continue
        n_scores_eq_at_k += 1
        # 找含第 k 名（1 起 ⇒ 下标 k-1）的**并列组**范围和大小
        v0 = vals[k - 1]
        lo = k - 1
        while lo > 0 and abs(vals[lo - 1] - v0) <= 1e-9:
            lo -= 1
        hi = k - 1
        while hi + 1 < len(vals) and abs(vals[hi + 1] - v0) <= 1e-9:
            hi += 1
        size = hi - lo + 1
        tie_group_sizes.append(size)
        tie_group_of_k.append(size)
        tie_frac_of_k.append(size / float(k))
        if size >= len(vals):
            n_fully_tied += 1
    def _p(vals, q):
        if not vals:
            return None
        a = sorted(vals)
        return a[min(len(a) - 1, int(q * (len(a) - 1)))]
    return {
        "n_queries_with_scores": n, "k": k,
        "tie_at_truncation": n_scores_eq_at_k,
        "tie_at_truncation_rate": round(n_scores_eq_at_k / n, 4) if n else None,
        "fully_tied_queries": n_fully_tied,
        "tie_group_size_of_k": {"mean": round(sum(tie_group_of_k) / len(tie_group_of_k), 2)
                                if tie_group_of_k else None,
                                "p50": _p(tie_group_of_k, 0.50), "p95": _p(tie_group_of_k, 0.95),
                                "max": max(tie_group_of_k) if tie_group_of_k else None},
        "tie_group_over_k": {"mean": round(sum(tie_frac_of_k) / len(tie_frac_of_k), 2)
                             if tie_frac_of_k else None, "max": round(max(tie_frac_of_k), 2)
                             if tie_frac_of_k else None},
        "verdict": (
            "**截断处普遍同分 ⇒ 这些查询的 top-k 由并列次序（≈PYTHONHASHSEED）决定，"
            "A/B 在这类查询上测的是随机性**" if n and n_scores_eq_at_k / n > 0.2 else
            "截断处同分不普遍（<20%）⇒ A/B 受并列影响有限" if n else "无分数留档"),
    }


def cmd_ties(args: argparse.Namespace) -> int:
    blob = json.load(open(args.infile, encoding="utf-8"))
    arms = blob["arms"]
    out = {"k": args.k, "infile": os.path.basename(args.infile), "arms": {}}
    for name, res in arms.items():
        st = tie_stats(res.get("per_query") or [], k=args.k)
        if st.get("n_queries_with_scores"):
            out["arms"][name] = st
    L = ["## 截断处同分诊断（k=%d）" % args.k, "",
         "含义：第 %d 名与第 %d 名分数相等 ⇒ 取哪 %d 条由**并列次序**决定" % (args.k, args.k + 1, args.k), "",
         "| 臂 | 有分数的查询数 | 截断处同分查询数 | 同分占比 | 含第 %d 名的并列组均值 | 组/k 均值 | 全并列查询 |"
         % args.k,
         "|---|---|---|---|---|---|---|"]
    for name, st in out["arms"].items():
        g = st.get("tie_group_size_of_k") or {}
        L.append("| %s | %d | %d | %s | %s | %s | %d |"
                 % (name, st["n_queries_with_scores"], st["tie_at_truncation"],
                    st["tie_at_truncation_rate"], g.get("mean"),
                    (st.get("tie_group_over_k") or {}).get("mean"), st["fully_tied_queries"]))
    L.append("")
    for name, st in out["arms"].items():
        L.append("- `%s`：%s" % (name, st["verdict"]))
    print("\n".join(L))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(L))
    return 0


# ─────────────────────────── 生产 API 探针 ───────────────────────────
def _metric(text: str, name: str) -> Optional[float]:
    for ln in text.splitlines():
        if ln.startswith(name + " "):
            try:
                return float(ln.split(None, 1)[1])
            except Exception:
                return None
    return None


def _pool_gate(api: str, timeout: float) -> Dict[str, Any]:
    """读 `GET /metrics` 的检索池状态。

    **为什么要门控**（队长 2026-10-06 裁定，现场实测依据）：
    `/memory/search/hybrid` 在检索池槽位被占满时会**降级为纯词法**并回
    `{lexical_only:true, degraded_reason:"search_pool_saturated"}` —— 那不是多通道融合。
    若不门控，探针测到的是"降级态"，而**历史结论「生产混合检索打不过零依赖 BM25」
    是在真融合态下得出的** ⇒ 拿降级样本去比，就会"再次证实"那个结论，
    属于**用降级态冒充融合态**。故：`slots_held == 0` 才取样，否则退避重试。
    （现场另有 `trinity_search_oldest_slot_seconds≈5.8`、`pool_recoveries_total=0`
    ⇒ 槽位没卡死，是秒级瞬时饱和，退避即可拿到干净样本。）
    """
    import urllib.request
    try:
        with urllib.request.urlopen(api.rstrip("/") + "/metrics", timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True,
            "slots_held": _metric(txt, "trinity_search_slots_held"),
            "pool_size": _metric(txt, "trinity_search_pool_size"),
            "oldest_slot_s": _metric(txt, "trinity_search_oldest_slot_seconds"),
            "pool_recoveries": _metric(txt, "trinity_search_pool_recoveries_total")}


def cmd_probe(args: argparse.Namespace) -> int:
    """只读探针：把题集逐题发给**生产 API**，统计响应内的通道计数与延迟。

    **不做重启**、不写库；ground truth 不参与（生产库是干净语料，不是评测语料）。
    作用：回答「生产这一路（PG, routing=auto→full）里每个通道到底有没有供料」，
    并把 **降级态（lexical_only）与真融合态**分开统计 —— 见 `_pool_gate` 的说明。
    """
    import urllib.request
    gated = (args.gate == "on")
    qs = load_dataset(args.dataset)[:args.limit]
    rows = []
    t0 = time.time()
    n_retry = 0
    for i, q in enumerate(qs):
        # ── 槽位门控：slots_held == 0 才取样 ─────────────────────────
        if gated:
            for attempt in range(args.gate_retries):
                g = _pool_gate(args.api, args.timeout)
                if not g.get("ok"):
                    raise SystemExit("probe: /metrics 不可读：%s" % g.get("error"))
                if g.get("slots_held") == 0:
                    break
                n_retry += 1
                time.sleep(args.gate_wait)
            else:
                g = _pool_gate(args.api, args.timeout)
                rows.append({"qid": q.get("question_id"), "gate": "busy",
                             "slots_held": g.get("slots_held")})
                continue
        body = json.dumps({"query": q["question"], "top_k": args.top_k,
                           "mode": "hybrid", "strategy": "rrf"}).encode("utf-8")
        req = urllib.request.Request(args.api.rstrip("/") + "/memory/search/hybrid",
                                     data=body, headers={"Content-Type": "application/json"})
        st = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            dt = (time.perf_counter() - st) * 1000
            bd = payload.get("breakdown") if isinstance(payload, dict) else None
            degraded = bool((payload or {}).get("degraded")) or bool(
                (payload or {}).get("lexical_only")) or bool((bd or {}).get("lexical_only"))
            rows.append({"qid": q.get("question_id"), "ms": round(dt, 1),
                         "gate": "clean" if gated else "ungated",
                         "degraded": degraded,
                         "state": "degraded(lexical_only)" if degraded else "true_fusion",
                         "degraded_reason": (payload or {}).get("degraded_reason"),
                         "n": len((payload or {}).get("results") or []),
                         "n_channels_nonzero": sum(
                             1 for _k, v in (bd or {}).items()
                             if isinstance(v, (int, float)) and not isinstance(v, bool) and v),
                         "breakdown": bd if isinstance(bd, dict) else {}})
        except Exception as exc:  # noqa: BLE001
            rows.append({"qid": q.get("question_id"), "error": "%s: %s" % (type(exc).__name__, exc)})
        if (i + 1) % 50 == 0:
            print("probe %d/%d (gate retries=%d)" % (i + 1, len(qs), n_retry), flush=True)
    ok = [r for r in rows if "error" not in r and r.get("gate") != "busy"]
    clean = [r for r in ok if r.get("state") == "true_fusion"]
    degraded = [r for r in ok if r.get("state") == "degraded(lexical_only)"]

    def _summ(sub: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not sub:
            return {"n": 0}
        keys: Dict[str, int] = {}
        for r in sub:
            for k, v in (r.get("breakdown") or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    keys[k] = keys.get(k, 0) + (1 if v else 0)
        return {"n": len(sub),
                "p50_ms": round(_pct([r["ms"] for r in sub], 0.50), 1),
                "p95_ms": round(_pct([r["ms"] for r in sub], 0.95), 1),
                "mean_returned": round(sum(r["n"] for r in sub) / len(sub), 2),
                "channel_keys_nonzero_counts": keys}

    out = {
        "api": args.api, "queries": len(rows), "gated": gated,
        "gate_retries": n_retry,
        "busy_skipped": sum(1 for r in rows if r.get("gate") == "busy"),
        "errors": sum(1 for r in rows if "error" in r),
        "true_fusion": _summ(clean),
        "degraded_lexical_only": _summ(degraded),
        "clean_fraction": round(len(clean) / max(len(ok), 1), 4),
        "verdict": ("**clean 样本充足，可作生产真融合读数**" if len(clean) >= args.limit * 0.5
                    else "**当前并发条件下无法测到真融合**（clean 样本不足）"
                    if clean else
                    "**当前并发条件下完全无法测到真融合**（100% 降级）"),
        "elapsed_s": round(time.time() - t0, 1),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        json.dump({"summary": out, "rows": rows}, open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    return 0


# ─────────────────────────── run ───────────────────────────
def cmd_run(args: argparse.Namespace) -> int:
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
    # PG 连接预算：与 scripts/quality_gate.py:73-78 同款（不设会打爆 max_connections=200，
    # 实测该脚本单进程曾持有 194 条连接）。
    os.environ.setdefault("TRINITY_PG_POOL_MAX", "3")
    os.environ.setdefault("TRINITY_PG_POOL_MIN", "1")
    names = [a.strip() for a in args.arms.split(",") if a.strip()] or list(ARMS)
    unknown = [a for a in names if a not in ARMS]
    if unknown:
        print("unknown arms: %s" % unknown, file=sys.stderr)
        return 2

    # 数据集体检（每次跑都记录，使报告自带语料上下文）
    import io as _io
    import contextlib
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_stats(argparse.Namespace(dataset=args.dataset))
    corpus_stats = json.loads(buf.getvalue())

    # `fts_variant` / `oracle` 臂**也**要建库（它们直接读适配器），漏判会拿到 mem=None。
    need_trinity = any(ARMS[a].get("kind") in ("trinity", "fts_variant", "oracle")
                       for a in names)
    pure = None
    mem = None
    qs: List[Dict[str, Any]] = []
    store = None
    n_ing = 0
    ing_s = 0.0
    if need_trinity:
        print("[rc] building store ...", flush=True)
        mem, qs, store, n_ing, n_ing_failed, ing_s = build_store(
            args.dataset, args.limit, getattr(args, "store", "") or "")
        print("[rc] store ready (%d rows ingested in %.1fs, failed=%d, store=%s)"
              % (n_ing, ing_s, n_ing_failed, store), flush=True)
    else:
        qs = load_dataset(args.dataset)[:args.limit]
        n_ing_failed = 0

    if any(ARMS[a].get("kind") == "bm25" for a in names):
        pure = PureBM25(k1=args.bm25_k1, b=args.bm25_b,
                        mode=ARMS[[a for a in names if ARMS[a].get("kind") == "bm25"][0]].get("mode", "regex"))
        # 语料：全量题集去重事实（**只含事实文本**，模拟"零依赖 BM25 只有一份干净语料"）
        seen = set()
        for q in load_dataset(args.dataset)[:args.limit]:
            for e in expected_of(q):
                if e not in seen:
                    seen.add(e)
                    pure.add(e, e)
        pure.finalize()
        if args.corpus == "dup":
            # 加噪对照：把重复行（820）也加进去，让 idf 被重复度污染
            pure = PureBM25(k1=args.bm25_k1, b=args.bm25_b, mode="regex")
            for i, q in enumerate(load_dataset(args.dataset)[:args.limit]):
                for f in q.get("context_facts") or []:
                    t = norm((f or {}).get("fact", ""))
                    if t:
                        pure.add("%s#%d" % (t, i), t)
            pure.finalize()
        print("[rc] pure BM25 corpus docs=%d avgdl=%.1f" % (len(pure.docs), pure.avgdl), flush=True)

    results: Dict[str, Any] = {}
    meta = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dataset": os.path.basename(args.dataset),
        "dataset_path": args.dataset,
        "limit": args.limit,
        "top_k": args.top_k,
        "corpus": args.corpus,
        "py_hash_seed": os.environ.get("PYTHONHASHSEED", "(random)"),
        "ingested_rows": n_ing,
        "ingest_failed_rows": n_ing_failed,
        "ingest_s": ing_s,
        "store": store,
        "corpus_stats": corpus_stats,
        "arms": results,
    }
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    def _flush() -> None:
        """**逐臂落盘**：全量 500 题的跑动可能几十分钟，中途被打断不能白跑。"""
        if not args.out:
            return
        meta["aggregates"] = {k: aggregate(v) for k, v in results.items()}
        meta["partial"] = len(results) < len(names)
        tmp = args.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, args.out)

    for name in names:
        spec = ARMS[name]
        t0 = time.time()
        print("[rc] arm %s ..." % name, flush=True)
        r = run_arm(name, spec, mem, qs, args.top_k, args.corpus, pure)
        r["elapsed_s"] = round(time.time() - t0, 1)
        results[name] = r
        g = aggregate(r)
        print("[rc]   %s R@5=%.4f R@10=%.4f MRR=%.4f nDCG@5=%s p50=%.1fms (%.1fs)"
              % (name, g.get("R@5", -1), g.get("R@10", -1), g.get("MRR", -1),
                 g.get("nDCG@5"), g.get("p50_ms", -1), r["elapsed_s"]), flush=True)
        _flush()

    out = meta
    if args.out:
        print("[rc] saved %s" % args.out)
    else:
        print(json.dumps({k: aggregate(v) for k, v in results.items()},
                         ensure_ascii=False, indent=1))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="统一检索基准口径 / 逐通道贡献 / 显著性")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stats", help="数据集体检")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("run", help="跑基准")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--arms", default="")
    p.add_argument("--corpus", choices=["clean", "dup"], default="clean")
    p.add_argument("--bm25-k1", type=float, default=1.5)
    p.add_argument("--bm25-b", type=float, default=0.75)
    p.add_argument("--store", default="", help="复用已建 store 目录（跳过重新摄入）")
    p.add_argument("--out", default="")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("table", help="出表")
    p.add_argument("--in", dest="infile", required=True)
    p.add_argument("--baseline", default="prod_search_hybrid")
    p.add_argument("--iters", type=int, default=20000)
    p.add_argument("--out", default="")
    p.set_defaults(fn=cmd_table)

    p = sub.add_parser("ties", help="截断处同分诊断")
    p.add_argument("--in", dest="infile", required=True)
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--out", default="")
    p.set_defaults(fn=cmd_ties)

    p = sub.add_parser("probe", help="生产 API 通道探针（只读，按检索池槽位门控）")
    p.add_argument("--api", default="http://127.0.0.1:8001")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--gate", default="on", choices=["on", "off"],
                   help="on（默认）：slots_held==0 才取样，否则退避重试")
    p.add_argument("--gate-wait", type=float, default=1.5)
    p.add_argument("--gate-retries", type=int, default=20)
    p.add_argument("--out", default="")
    p.set_defaults(fn=cmd_probe)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/retrieval_contribution.py::<module>")
    raise SystemExit(main())
