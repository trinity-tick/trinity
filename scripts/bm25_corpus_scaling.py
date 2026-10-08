#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B2（零依赖 BM25）的**语料增长退化**实测（建议 (b)）。

## 要回答的问题

上一轮三题集横评显示零依赖 BM25 全面不劣于生产混合检索，建议先"确认 B2 的零依赖特性
不会随库规模退化"。当前 B2 只扫 **3,562 行 / 277 文档**（= `persona='trinity-docs'` 全量），
所以真正的问题是：**如果同域文档变多，BM25 的 R@k 会掉多快？**

## 做法（复用评测自己的 BM25，不另写一份 —— §1050）

`doc_retrieval_eval.py` 里有实现好的 `BM25` 类与 `load_doc_index(persona, with_body)`，
本工具**直接 import 它们**，然后在真实语料上**按档追加干扰文档**，测量同一批留出问题
（任务形状、无词法泄漏）上的文档级 R@k。

干扰文档是**确定性**合成的（固定种子 + 固定词表），因此曲线可复跑。

## 判据（可失败）
- 若 R@10 随 N 增长**迅速崩塌**（例如 N=2000 时跌破 A2 的水平）⇒ 说明 B2 的优势
  只是"小语料红利"，**不足以据此改默认**。
- 若 R@10 在数倍语料下仍**明显高于 A2** ⇒ 该顾虑不成立。
- 两档之间差异必须可解释；**不做外推断言**，只报实测曲线。

## 纪律
- **只读**：不写库、不动任何表；只在内存里构造干扰文档。
- 产物落 `output/`（§12）。

用法：
    python scripts/bm25_corpus_scaling.py --persona trinity-docs --scales 0,500,2000,8000
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sys
import time
import logging

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/bm25_corpus_scaling.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 干扰文档词表：刻意用**与任务无关**的中文实词，模拟"同域但不同主题"的文档增长。
_VOCAB = ("库存", "对账", "补货", "波次", "拣选", "承运", "月台", "效期", "条码", "周转",
          "供应", "结算", "报关", "冷链", "仓位", "批次", "退货", "盘点", "标签", "调度",
          "接口", "熔断", "限流", "缓存", "索引", "分片", "回滚", "灰度", "压测", "巡检",
          "薪酬", "排班", "考勤", "绩效", "预算", "折旧", "票据", "税负", "审计", "合规")


def _load_eval_module():
    """按路径加载 doc_retrieval_eval，复用它的 BM25 与 load_doc_index。"""
    path = os.path.join(ROOT, "scripts", "doc_retrieval_eval.py")
    spec = importlib.util.spec_from_file_location("doc_retrieval_eval", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["doc_retrieval_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


def synth_docs(n: int, seed: int = 20261005) -> dict:
    """确定性合成 n 篇**无关**文档：{doc_name: token 列表}。纯函数、可单测。

    每篇取 20 个词、带一个唯一编号词，模拟真实文档的词汇分布但又**不含**任何
    被测问题的答案 —— 它们只增加语料与噪声，不增加正确答案。
    """
    rnd = random.Random(seed)
    out = {}
    for i in range(n):
        toks = [rnd.choice(_VOCAB) for _ in range(20)] + ["干扰文档编号%d" % i]
        out["SYNTH_DISTRACTOR_%05d.md" % i] = toks
    return out


def hit_at_k(ranked_docs: list, target: str, k: int) -> int:
    """纯函数、可单测。"""
    return 1 if target in ranked_docs[:k] else 0


def run(persona: str, scales: list, golden: str, top_k: int = 10) -> dict:
    ev = _load_eval_module()
    g = json.load(open(golden, encoding="utf-8"))
    items = g.get("items") if isinstance(g, dict) else g
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "persona": persona,
           "golden": os.path.basename(golden), "n_queries": len(items),
           "top_k": top_k, "curve": []}
    # 真实语料（B2 = 标题+正文）。注意 `load_doc_index` 返回**三元组**
    # `(docs, encrypted_count, rows_scanned)` —— 初版按二元组解包会当场 ValueError。
    try:
        body_docs, enc_rows, rows_scanned = ev.load_doc_index(persona, True)
    except Exception as e:  # noqa: BLE001
        out.update(verdict="INCONCLUSIVE",
                   error="load_doc_index failed: %s: %s" % (type(e).__name__, str(e)[:160]))
        return out
    out["real_docs"] = len(body_docs)
    out["real_rows_scanned"] = rows_scanned
    out["real_encrypted_rows"] = enc_rows
    base = dict(body_docs)
    for n in scales:
        docs = dict(base)
        docs.update(synth_docs(n))
        eng = ev.BM25(docs)
        hits = 0
        for it in items:
            # BM25 暴露的是 `search()`（返回 [(doc, score)]），不是 `rank()`
            ranked = [d for d, _ in eng.search(it["query"], top_k)]
            hits += hit_at_k(ranked, it["target"], top_k)
        out["curve"].append({"added_distractors": n, "docs_total": len(docs),
                             "hits": hits,
                             "R@%d" % top_k: round(hits / max(1, len(items)), 4)})
    out["verdict"] = "OK"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", default="trinity-docs")
    ap.add_argument("--golden", default="eval/doc_golden_set_heldout.json")
    ap.add_argument("--scales", default="0,500,2000,8000")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    scales = [int(x) for x in a.scales.split(",") if x.strip()]
    gpath = a.golden if os.path.isabs(a.golden) else os.path.join(ROOT, a.golden)
    r = run(a.persona, scales, gpath, a.top_k)

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("== B2（零依赖 BM25）语料增长退化实测 ==")
        print("题集 %s（n=%d，任务形状、无词法泄漏）  persona=%s  top_k=%d"
              % (r.get("golden"), r.get("n_queries", 0), r.get("persona"), r.get("top_k")))
        print("[采样时刻] %s" % r.get("ts"))
        if r.get("verdict") != "OK":
            print("判定：%s %s" % (r.get("verdict"), r.get("error")))
            return 2
        print("真实文档数 %d" % r["real_docs"])
        print()
        print("%-16s %-12s %-10s %s" % ("追加干扰文档", "文档总数", "命中", "R@k"))
        for c in r["curve"]:
            print("%-16d %-12d %-10d %s" % (c["added_distractors"], c["docs_total"],
                                            c["hits"], c["R@%d" % a.top_k]))
        R = [c["R@%d" % a.top_k] for c in r["curve"]]
        print()
        print("首档 → 末档：%s → %s（Δ%+.4f）" % (R[0], R[-1], R[-1] - R[0]))
        print("⇒ 曲线形态即为「语料增长退化」的实测依据；**不做外推断言**")
        os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
        fp = os.path.join(ROOT, "output", "bm25_corpus_scaling_%s.json"
                          % time.strftime("%Y%m%d_%H%M%S"))
        with open(fp, "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=1)
        print("产物：%s" % fp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
