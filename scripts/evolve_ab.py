#!/usr/bin/env python3
"""evolve_ab.py — 自动 A/B 验证器（SELF_EVOLUTION_DESIGN 阶段 2）。

给定候选（env 覆盖集），与基线对比 QA 准确率：
  1. 运行 QA（seed42 子集，RouteReasoner，临时隔离库——与 rr_ab50 同口径）；
  2. judge3 判分（3 票多数，真实题面）；
  3. 输出 ABTestResult（baseline/experimental/delta/accepted/reason）。

候选 env 覆盖示例：
    --variant "TRINITY_RERANKER=off,TRINITY_GRAPH_PPR=off"
    --variant "TRINITY_LLM_MODEL=deepseek-chat"
env 覆盖在 QA 运行前 set 进子进程/当前进程，运行后恢复。

用法：
    python scripts/evolve_ab.py --n-qa 20                     # 只跑基线（绝对分）
    python scripts/evolve_ab.py --n-qa 20 --variant "TRINITY_RERANKER=off"   # A/B
    python scripts/evolve_ab.py --n-qa 20 --baseline <sig.json>             # 对照信号基线
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

DATA = r"C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json"
JUDGE = os.path.join(REPO, "benchmark", "judge3.py")
PY = os.environ.get("TRINITY_PY") or sys.executable
OUT_DIR = os.path.expanduser("~/.trinity/evolve")


def _run_qa(n: int, variant_env: dict, tag: str, data_path: str = "") -> dict:
    """运行 QA 子集（隔离临时库），返回 records。env 覆盖生效。

    data_path：私有留出子集（R8 P1-①）——采纳样本应指向
    benchmark/private_holdout.json；缺省用公开集。
    """
    # 备份并应用 variant env
    saved = {k: os.environ.get(k) for k in variant_env}
    for k, v in variant_env.items():
        os.environ[k] = v
    try:
        # 2026-08-25（缺口M 修正）：先设 TRINITY_STORE 再 import trinity（同 _run_retrieval）
        os.environ["TRINITY_STORE"] = tempfile.mkdtemp(prefix=f"evolve_ab_{tag}_")
        os.environ["TRINITY_LLM_EXTRACT"] = "off"
        os.environ["TRINITY_ISOLATE_TEST_WRITES"] = "off"
        # 2026-08-25（缺口K）：A/B 禁用语义缓存（同批题 base 后 exp 命中缓存 → 掩盖变体差异）
        os.environ["TRINITY_CACHE_BACKEND"] = "off"
        from trinity.qa.route_reasoner import RouteReasoner
        from trinity import Trinity

        with open(data_path or DATA, encoding="utf-8") as f:
            blob = json.load(f)
        data = blob.get("questions", blob) if isinstance(blob, dict) else blob
        random.seed(42)
        sample = random.sample(data, min(n, len(data)))

        mem = Trinity(use_ann=True)  # 2026-08-25：启用 ANN 向量通道（评测与生产对齐）

        def search_fn(q, top_k=5, agent_id=None, persona_id=None):
            return mem.search(q, top_k=top_k, agent_id=agent_id)

        rr = RouteReasoner(search_fn=search_fn, top_k=12, turn_top_k=16)
        if not rr.available:
            return {"error": "no LLM key (RouteReasoner unavailable)"}

        records = []
        t0 = time.time()
        for qi, q in enumerate(sample):
            qid = q["question_id"]
            qtype = q["question_type"]
            agent = f"ab_{tag}_{qi}"
            sessions = q.get("haystack_sessions", [])
            sids = q.get("haystack_session_ids") or []
            dates = q.get("haystack_dates") or []
            # 2026-08-25（遗留修复）：qtype-aware ingest——与 rr_ab50 同口径：
            # multi-session 用 turn 粒度（+24pp），其他用 session 粒度聚合
            # （RouteReasoner 的 temporal/plain/pref 策略依赖 session 上下文）。
            if str(qtype) == "multi-session":
                for si, (sid, sdate) in enumerate(zip(sids, dates)):
                    sess_content = sessions[si] if si < len(sessions) else []
                    turns = sess_content if isinstance(sess_content, list) else sess_content.get("turns", [])
                    for t_ in turns:
                        role = t_.get("role", "user") if isinstance(t_, dict) else "user"
                        content = t_.get("content", "") if isinstance(t_, dict) else str(t_)
                        if not content.strip():
                            continue
                        d = sdate or ""
                        text = f"[DATE: {d}] [{role}] {content.strip()}" if d else f"[{role}] {content.strip()}"
                        try:
                            mem.ingest(text, agent_id=agent, category="lme", tags=["lme"],
                                       postprocess=False)
                        except Exception:
                            pass
            else:
                for si, (sid, sdate) in enumerate(zip(sids, dates)):
                    sess_content = sessions[si] if si < len(sessions) else []
                    turns = sess_content if isinstance(sess_content, list) else sess_content.get("turns", [])
                    parts = []
                    for t_ in turns:
                        role = t_.get("role", "user") if isinstance(t_, dict) else "user"
                        content = t_.get("content", "") if isinstance(t_, dict) else str(t_)
                        parts.append(f"[{role}] {content}")
                    text = chr(10).join(parts)
                    if not text.strip():
                        continue
                    d = sdate or ""
                    if d:
                        text = f"[DATE: {d}] {text}"
                    try:
                        mem.ingest(text, agent_id=agent, category="lme", tags=["lme"],
                                   postprocess=False)
                    except Exception:
                        pass
            ans = rr.answer(str(q["question"]), qtype=qtype,
                            question_date=q.get("question_date"), agent_id=agent)
            records.append({
                "question_id": qid, "question_type": qtype,
                "expected": str(q.get("answer", ""))[:300],
                "answer": str(ans.get("answer") or "")[:500],
            })
            if (qi + 1) % 10 == 0:
                print(f"  [{tag}] {qi + 1}/{len(sample)} elapsed={int(time.time() - t0)}s", flush=True)
        return {"n": len(sample), "elapsed_s": round(time.time() - t0, 1), "records": records}
    finally:
        # 恢复 env
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _judge(records_path: str) -> tuple:
    """调 judge3 CLI，返回 (准确率, correct_ids 集合)。

    2026-08-24（R63 修复）：judge3 每票一次 LLM（n 题 × 3 票），5 题需
    ~15 次调用（~15min）；超时放宽到 2400s 并实时透传输出（诊断可见）。
    """
    out = os.path.join(OUT_DIR, f"judge3_{int(time.time())}.json")
    r = subprocess.run(
        [PY, JUDGE, "--in", records_path, "--out", out],
        capture_output=True, text=True, timeout=2400, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(f"judge3 failed: {r.stderr[-400:]}")
    with open(out, encoding="utf-8") as f:
        res = json.load(f)
    # judge3 输出是 per-file dict：{路径: {majority_acc: 0.78, correct_ids: [...], ...}}
    if isinstance(res, dict) and res:
        first = next(iter(res.values()))
        if isinstance(first, dict):
            acc = float(first.get("majority_acc") or first.get("accuracy") or 0.0)
            correct_ids = set(first.get("correct_ids") or [])
            return acc, correct_ids
    acc = res.get("majority_acc") or res.get("accuracy") or 0.0
    if isinstance(acc, dict):
        acc = sum(acc.values()) / max(len(acc), 1)
    return float(acc), set(res.get("correct_ids") or [])


def _paired_stats(base_correct: set, exp_correct: set, all_ids: list) -> dict:
    """配对统计：McNemar 检验 + bootstrap 差分 CI（R8 P0-② 决策门升级）。

    依据：评测方法论调研——二元判分应做配对 McNemar 而非独立比例；
    小样本 +2pp 落在噪声区间，须报告"差分 ± 置信区间"而非裸点值。
    """
    base_set = {str(x) for x in base_correct}
    exp_set = {str(x) for x in exp_correct}
    ids = [str(x) for x in all_ids]
    b = [str(x) in base_set for x in ids]
    e = [str(x) in exp_set for x in ids]
    if not b:
        return {"error": "no paired ids"}

    # McNemar（精确二项双侧，continuity 修正）
    b00 = b01 = b10 = b11 = 0
    for x, y in zip(b, e):
        if not x and not y: b00 += 1
        elif not x and y: b01 += 1
        elif x and not y: b10 += 1
        else: b11 += 1
    n_discordant = b01 + b10
    if n_discordant == 0:
        p_value = 1.0
    else:
        k = min(b01, b10)
        # 双侧二项精确检验（对称）
        p_value = 2.0 * sum(
            __import__("math").comb(n_discordant, i) * (0.5 ** n_discordant)
            for i in range(0, k + 1)
        )
        p_value = min(1.0, p_value)

    # bootstrap 差分 CI（配对重采样，1000 次）
    import random
    rng = random.Random(42)
    n = len(b)
    diffs = []
    for _ in range(1000):
        idx = [rng.randrange(n) for _ in range(n)]
        db = sum(1 for i in idx if b[i]) / n
        de = sum(1 for i in idx if e[i]) / n
        diffs.append(de - db)
    diffs.sort()
    lo = diffs[25]    # 2.5%
    hi = diffs[974]   # 97.5%
    delta = sum(e) / n - sum(b) / n

    return {
        "n": n,
        "delta": round(delta, 4),
        "ci_low": round(lo, 4),
        "ci_high": round(hi, 4),
        "mcnemar_p": round(p_value, 4),
        "b01": b01, "b10": b10,  # 仅候选对 / 仅基线对
    }


def _paired_mrr_stats(base_mrr: dict, exp_mrr: dict, all_ids: list) -> dict:
    """MRR 连续值配对统计（2026-08-25 缺口N）：每题 base/exp 的 MRR 差。

    二值配对（recall 命中与否）丢失 MRR 的排序信息——PPR off 可能只改排名
    不改命中集合 → delta=0 假象。此处对每题 mrr 差值做 bootstrap CI，
    判定 delta>0 且 CI 下界>0 才采纳。
    """
    ids = [str(x) for x in all_ids]
    diffs = []
    for qid in ids:
        b = base_mrr.get(qid, 0.0)
        e = exp_mrr.get(qid, 0.0)
        diffs.append(e - b)
    n = len(diffs)
    if n == 0:
        return {"error": "no paired ids"}
    import random as _rng
    rng = _rng.Random(42)
    boot = []
    for _ in range(2000):
        idx = [rng.randrange(n) for _ in range(n)]
        boot.append(sum(diffs[i] for i in idx) / n)
    boot.sort()
    delta = sum(diffs) / n
    return {
        "n": n,
        "delta": round(delta, 4),
        "ci_low": round(boot[50], 4),
        "ci_high": round(boot[1949], 4),
        "mrr_mean_base": round(sum(base_mrr.get(q, 0.0) for q in ids) / max(n, 1), 4),
        "mrr_mean_exp": round(sum(exp_mrr.get(q, 0.0) for q in ids) / max(n, 1), 4),
    }


def _run_retrieval(n: int, variant_env: dict, tag: str, data_path: str = "",
                   strategy: str = "rrf") -> dict:
    """检索指标模式（2026-08-25）：R@5 命中率，确定性、无 LLM judge。

    与 recall_diag_multi 同口径：ingest 时给每条记忆打 sid-<session_id> 标签，
    检索 top-k 后用结果里的 sid 标签与 answer_session_ids 求交集。
    指标 = 命中 gold session 的题数 / 总题数。

    QA acc 受 LLM 回答波动影响（±1-2 题噪声），检索指标只看检索结果，
    波动小、成本低（无 judge3 LLM 调用）——更适合作为自进化 A/B 主信号。
    """
    saved = {k: os.environ.get(k) for k in variant_env}
    for k, v in variant_env.items():
        os.environ[k] = v
    try:
        # 2026-08-25（缺口M 修正）：必须先设 TRINITY_STORE 再 import trinity——
        # trinity/__init__.py 的 ensure_bootstrapped() 在导入时创建全局
        # MemoryAggregator 并绑定当前 TRINITY_STORE；此前 import 在 env 设置前，
        # 聚合器绑定默认大库 → search_hybrid 走聚合器查大库而非隔离库 → 空结果。
        os.environ["TRINITY_STORE"] = tempfile.mkdtemp(prefix=f"evolve_ret_{tag}_")
        os.environ["TRINITY_LLM_EXTRACT"] = "off"
        os.environ["TRINITY_ISOLATE_TEST_WRITES"] = "off"
        # 2026-08-25（缺口K 修复）：A/B 禁用语义缓存——缓存 key 不含 env 变量
        # （make_text_key 只含 query/top_k/strategy），base 与 exp 同批题同 query
        # 会命中同一缓存 → 变体差异被缓存掩盖（MRR 恒相同）。A/B 必须测真实检索。
        os.environ["TRINITY_CACHE_BACKEND"] = "off"
        from trinity import Trinity

        with open(data_path or DATA, encoding="utf-8") as f:
            blob = json.load(f)
        data = blob.get("questions", blob) if isinstance(blob, dict) else blob
        random.seed(42)
        sample = random.sample(data, min(n, len(data)))

        mem = Trinity(use_ann=True)  # 2026-08-25：启用 ANN 向量通道（评测与生产对齐）
        # 2026-08-25（缺口M 修复）：引擎 warm-up + 自检——首次调用时 Trinity 引擎
        # （聚合器/混合检索/向量索引）惰性初始化，前 1-2 次 search_hybrid 可能
        # 返回空（实测 rep0/rep1 失败、rep2+ 成功）。预热 + 验证检索有结果，
        # 无结果则重建引擎重试一次。
        for _attempt in range(2):
            try:
                mem.ingest("__evolve_warmup__ engine init probe", agent_id="_warmup",
                           category="_warmup", tags=["_warmup"], postprocess=False)
                _w = mem.search_hybrid("warmup probe query", top_k=1, agent_id="_warmup")
                _wl = _w.get("results", []) if isinstance(_w, dict) else _w
                if _wl:
                    break  # 引擎就绪
                # 空结果 → 引擎可能未就绪，重建实例（env 已设，指向同一隔离库）
                mem = Trinity(use_ann=True)  # 2026-08-25：启用 ANN 向量通道（评测与生产对齐）
            except Exception:
                mem = Trinity(use_ann=True)  # 2026-08-25：启用 ANN 向量通道（评测与生产对齐）
        # 2026-08-25（BM25 维度）：等待 BM25 索引就绪——后台线程构建，
        # 不等待则 BM25 通道空索引降级（此前所有 MRR 评测缺 BM25 通道）。
        # warm-up 的 ingest 触发 _ensure_bm25_index；这里轮询 _bm25_ready。
        try:
            _bm25_wait = 0
            while not getattr(mem, "_bm25_ready", False) and _bm25_wait < 60:
                time.sleep(0.5)
                _bm25_wait += 0.5
                if not getattr(mem, "_bm25_index", None) and _bm25_wait > 5:
                    mem.search_hybrid("bm25 trigger", top_k=1, agent_id="_warmup")
            if getattr(mem, "_bm25_ready", False):
                print(f"  [bm25 ready in {_bm25_wait:.1f}s]")
        except Exception:
            pass
        t0 = time.time()
        per_q = []
        # 2026-08-25（缺口L）：memory_id → sid 映射——search_hybrid 返回瘦身结果
        # （仅 memory_id/score），需按 id 映射回 sid 标签（ingest 时记录）。
        mid_to_sid: dict = {}
        for qi, q in enumerate(sample):
            qid = q["question_id"]
            gold = set(q.get("answer_session_ids") or [])
            sessions = q.get("haystack_sessions", [])
            sids = q.get("haystack_session_ids") or []
            dates = q.get("haystack_dates") or []
            agent = f"ret_{tag}_{qi}"
            # ingest with sid tags (session-granularity, qtype-aware)
            for si, sess in enumerate(sessions):
                sid = sids[si] if si < len(sids) else f"s{si}"
                d = dates[si] if si < len(dates) else ""
                turns = sess if isinstance(sess, list) else sess.get("turns", [])
                parts = []
                for t_ in turns:
                    role = t_.get("role", "user") if isinstance(t_, dict) else "user"
                    content = t_.get("content", "") if isinstance(t_, dict) else str(t_)
                    parts.append(f"[{role}] {content}")
                text = chr(10).join(parts)
                if not text.strip():
                    continue
                if d:
                    text = f"[DATE: {d}] {text}"
                try:
                    _res = mem.ingest(text, agent_id=agent, category="lme",
                                      tags=["lme", f"sid-{sid}"], postprocess=False)
                    _mid = (_res or {}).get("memory_id") if isinstance(_res, dict) else None
                    if _mid:
                        mid_to_sid[str(_mid)] = sid
                except Exception:
                    pass
            # retrieval top-5（按序——MRR 需要 gold 的最高排名）
            # 2026-08-25（缺口L 修复）：改用 search_hybrid——mem.search() 默认 hybrid
            # mode 但 _hybrid_retriever 初始为 None（_construction.py），实际回退 FTS5
            # 关键词（不读 GRAPH_PPR/RERANK/ADAPTIVE_ROUTING env → 变体无效果）。
            # search_hybrid() 触发延迟初始化的 HybridRetriever（读 env），变体才生效。
            try:
                hits = mem.search_hybrid(str(q["question"]), top_k=5, agent_id=agent,
                                         strategy=strategy)
            except Exception:
                hits = mem.search(str(q["question"]), top_k=5, agent_id=agent)
            hl = hits.get("results", []) if isinstance(hits, dict) else hits
            hit_sids = []
            for h in hl:
                if h.get("tags"):
                    for t_ in (h.get("tags") or []):
                        if str(t_).startswith("sid-"):
                            hit_sids.append(str(t_)[4:])
                elif h.get("memory_id"):
                    _sid = mid_to_sid.get(str(h["memory_id"]))
                    if _sid:
                        hit_sids.append(_sid)
            # 2026-08-25（结构进化：会话关联扩展）——跨会话话题召回：
            # 对 top 命中记忆提取高频实体词，作为扩展查询再检索（把同话题
            # 其他 session 的记忆补进候选）。env TRINITY_SESSION_EXTENSION=on。
            # 针对失败模式：multi-session 题（gold 分散多 session）与语义泛查询。
            if os.environ.get("TRINITY_SESSION_EXTENSION", "off").strip().lower() in ("1", "on", "true", "yes"):
                try:
                    import re as _re
                    _STOP = {"the", "a", "an", "is", "are", "was", "were", "i", "me", "my",
                             "you", "your", "and", "or", "of", "in", "on", "for", "with",
                             "to", "that", "this", "have", "has", "do", "does", "did",
                             "what", "how", "when", "where", "which", "can", "would"}
                    _term_ct: dict = {}
                    for h in hl[:3]:
                        _c = (h.get("content") or "")[:1500]
                        for t in _re.findall(r"[a-z0-9]+", _c.lower()):
                            if t in _STOP or len(t) < 4:
                                continue
                            _term_ct[t] = _term_ct.get(t, 0) + 1
                    _top_terms = [t for t, _ in sorted(_term_ct.items(), key=lambda x: -x[1])[:3]]
                    if _top_terms:
                        _ext_q = str(q["question"]) + " " + " ".join(_top_terms)
                        _h2 = mem.search_hybrid(_ext_q, top_k=3, agent_id=agent, strategy=strategy)
                        _hl2 = _h2.get("results", []) if isinstance(_h2, dict) else _h2
                        for h in _hl2:
                            if h.get("tags"):
                                for t_ in (h.get("tags") or []):
                                    if str(t_).startswith("sid-"):
                                        hit_sids.append(str(t_)[4:])
                            elif h.get("memory_id"):
                                _sid2 = mid_to_sid.get(str(h["memory_id"]))
                                if _sid2:
                                    hit_sids.append(_sid2)
                except Exception:
                    pass
            hit_set = set(hit_sids)
            # 2026-08-25（会话扩展修正）：MRR/nDCG 只基于原始 top-5
            # （扩展候选排第 6+ 位不参与排名指标）
            _top5_sids = hit_sids[:5]
            # MRR：gold session 在结果中的最高排名倒数（1=第1位, 0=未命中）
            mrr_q = 0.0
            for rank, sid in enumerate(_top5_sids, start=1):
                if sid in gold:
                    mrr_q = 1.0 / rank
                    break
            # 2026-08-25（nDCG@5）：排序敏感指标——DCG = sum(rel_i/log2(i+1))，
            # rel_i=1 当 result[i] 是 gold session；IDCG = gold 数在 top-5 的理想 DCG。
            # 对排序位置敏感（第1位命中 vs 第5位命中分数差异大）——解锁排序参数可测性。
            k = min(5, len(_top5_sids))
            dcg = 0.0
            for i in range(k):
                if _top5_sids[i] in gold:
                    dcg += 1.0 / (i + 2)  # log2(i+2)
            idcg = sum(1.0 / (i + 2) for i in range(min(k, len(gold))))
            ndcg_q = dcg / idcg if idcg > 0 else 0.0
            per_q.append({
                "question_id": qid,
                "gold": sorted(gold),
                "hit": sorted(hit_set),
                "recall": 1.0 if (gold & hit_set) else 0.0,
                "mrr": mrr_q,
                "ndcg": round(ndcg_q, 4),
            })
        elapsed = time.time() - t0
        n = max(len(per_q), 1)
        r5 = sum(1 for x in per_q if x["recall"]) / n
        r1 = sum(1 for x in per_q if x["mrr"] >= 0.999) / n
        mrr = sum(x["mrr"] for x in per_q) / n
        ndcg = sum(x.get("ndcg", 0.0) for x in per_q) / n
        return {"n": len(per_q), "r5": round(r5, 4), "r1": round(r1, 4),
                "mrr": round(mrr, 4), "ndcg": round(ndcg, 4),
                "elapsed_s": round(elapsed, 1), "per_question": per_q}
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-qa", type=int, default=20)
    parser.add_argument("--variant", default="", help="候选 env 覆盖，逗号分隔 K=V")
    parser.add_argument("--baseline-json", default="",
                        help="基线答案文件（records 列表）——省一次基线运行")
    parser.add_argument("--min-improve", type=float, default=0.02,
                        help="采纳阈值（绝对值 pp，默认 0.02）")
    parser.add_argument("--tag", default="auto")
    parser.add_argument("--data", default="",
                        help="私有留出子集路径（R8 P1-①，默认公开集）")
    parser.add_argument("--metric", default="qa", choices=["qa", "retrieval", "mrr", "ndcg"],
                        help="评测指标：qa=judge3 准确率（默认）；retrieval=R@5；mrr=MRR；ndcg=nDCG@5（排序敏感，解锁排序参数可测性）")
    parser.add_argument("--strategy", default="rrf", choices=["rrf", "fusion"],
                        help="混合检索融合策略：rrf=RRF 融合（默认）；fusion=加权融合（通道权重 env 生效）")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    variant_env = {}
    for kv in args.variant.split(","):
        kv = kv.strip()
        if "=" in kv:
            k, v = kv.split("=", 1)
            variant_env[k.strip()] = v.strip()

    # ── 基线 ──
    baseline_acc = None
    baseline_records = None
    _base_correct = set()
    _mrr_base_perq: list = []
    _mrr_exp_perq: list = []
    if args.baseline_json and os.path.exists(args.baseline_json):
        with open(args.baseline_json, encoding="utf-8") as f:
            blob = json.load(f)
        base_records = blob.get("qa", {}).get("answers") if "qa" in blob else blob.get("records")
        # 2026-08-25（R@5 主信号）：retrieval signal 无 answers（QA 模式产物），
        # 只有 baseline_correct_ids + baseline_acc + n——直接复用。
        pre_judged = (blob.get("qa") or {}).get("baseline_correct_ids")
        pre_acc = (blob.get("qa") or {}).get("baseline_acc")
        pre_n = (blob.get("qa") or {}).get("n", 0)
        if pre_judged is not None and args.metric in ("retrieval", "mrr", "ndcg"):
            baseline_acc = float(pre_acc or 0.0)
            _base_correct = set(pre_judged)
            # 用全部题 id（含未命中的）作配对统计样本——signal 已存 all_question_ids
            _all_ids = (blob.get("qa") or {}).get("all_question_ids") or []
            base_records = [{"question_id": qid} for qid in _all_ids]
            baseline_records = base_records
            # 2026-08-25（缺口N）：signal 的逐题 mrr/ndcg（连续值配对用）
            _mrr_perq = (blob.get("qa") or {}).get("mrr_per_question") or {}
            _ndcg_perq = (blob.get("qa") or {}).get("ndcg_per_question") or {}
            _mrr_base_perq.extend(
                {"question_id": qid, "mrr": _mrr_perq.get(qid, 0.0),
                 "ndcg": _ndcg_perq.get(qid, 0.0)} for qid in _all_ids)
            print(f"baseline (signal pre-judged): {baseline_acc:.3f} "
                  f"correct={len(_base_correct)}/{len(_all_ids)}")
        elif base_records:
            base_records = [{"question_id": r["question_id"],
                             "question_type": r.get("question_type", "single-session"),
                             "expected": r.get("expected", ""),
                             "answer": r.get("answer", "")} for r in base_records]
            rec_file = os.path.join(OUT_DIR, f"base_{args.tag}_{int(time.time())}.json")
            with open(rec_file, "w", encoding="utf-8") as f:
                json.dump({"records": base_records}, f, ensure_ascii=False)
            if pre_judged is not None:
                baseline_acc = float(pre_acc or 0.0)
                _base_correct = set(pre_judged)
                baseline_records = base_records
                print(f"baseline (signal pre-judged): {baseline_acc:.3f} "
                      f"correct={len(_base_correct)}/{len(base_records)}")
            else:
                baseline_acc, _base_correct = _judge(rec_file)
                baseline_records = base_records
                print(f"baseline (from json, re-judged): {baseline_acc:.3f} (n={len(base_records)})")

    if baseline_acc is None:
        if args.metric in ("retrieval", "mrr", "ndcg"):
            base = _run_retrieval(args.n_qa, {}, "base", args.data, strategy=args.strategy)
            if base.get("error"):
                print("baseline error:", base["error"])
                return 1
            _mkey = {"mrr": "mrr", "ndcg": "ndcg"}.get(args.metric, "r5")
            baseline_acc = base.get(_mkey, 0.0)
            baseline_records = [{"question_id": x["question_id"]} for x in base["per_question"]]
            _base_correct = {x["question_id"] for x in base["per_question"] if x["recall"]}
            _mrr_base_perq = base.get("per_question", [])
            print(f"baseline {args.metric}: {baseline_acc:.4f} "
                  f"(n={base['n']} r5={base.get('r5')} mrr={base.get('mrr')} elapsed={base['elapsed_s']}s)")
        else:
            base = _run_qa(args.n_qa, {}, "base", args.data)
            if base.get("error"):
                print("baseline error:", base["error"])
                return 1
            rec_file = os.path.join(OUT_DIR, f"base_{args.tag}_{int(time.time())}.json")
            with open(rec_file, "w", encoding="utf-8") as f:
                json.dump({"records": base["records"]}, f, ensure_ascii=False)
            baseline_acc, _base_correct = _judge(rec_file)
            baseline_records = base["records"]
            print(f"baseline: {baseline_acc:.3f} (n={base['n']} elapsed={base['elapsed_s']}s)")

    # ── 候选 ──
    if not variant_env:
        result = {
            "ts": datetime.now().isoformat(), "tag": args.tag,
            "baseline_score": baseline_acc, "experimental_score": None,
            "delta": None, "accepted": False,
            "reason": "no variant provided (baseline only)",
        }
        with open(os.path.join(OUT_DIR, f"ab_{args.tag}_{int(time.time())}.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0

    if args.metric in ("retrieval", "mrr", "ndcg"):
        exp = _run_retrieval(args.n_qa, variant_env, "exp", args.data, strategy=args.strategy)
        if exp.get("error"):
            print("variant error:", exp["error"])
            return 1
        _mkey2 = {"mrr": "mrr", "ndcg": "ndcg"}.get(args.metric, "r5")
        exp_acc = exp.get(_mkey2, 0.0)
        exp_correct = {x["question_id"] for x in exp["per_question"] if x["recall"]}
        _mrr_exp_perq = exp.get("per_question", [])
        print(f"variant {args.metric}: {exp_acc:.4f} "
              f"(n={exp['n']} r5={exp.get('r5')} mrr={exp.get('mrr')} elapsed={exp['elapsed_s']}s)")
    else:
        exp = _run_qa(args.n_qa, variant_env, "exp", args.data)
        if exp.get("error"):
            print("variant error:", exp["error"])
            return 1
        rec_file2 = os.path.join(OUT_DIR, f"exp_{args.tag}_{int(time.time())}.json")
        with open(rec_file2, "w", encoding="utf-8") as f:
            json.dump({"records": exp["records"]}, f, ensure_ascii=False)
        exp_acc, exp_correct = _judge(rec_file2)
        print(f"variant: {exp_acc:.3f} (n={exp['n']} elapsed={exp['elapsed_s']}s)")

    # ── R8 P0-② 决策门升级：配对 McNemar + bootstrap CI ──
    # 依据：评测方法论调研——n 小样本 +2pp 落在噪声区间，采纳须
    # "delta>0 且 CI 下界>0"（配对统计而非裸点值）。
    # R8 P1-②：采纳信号只用 QA 差分 CI；R@5 是"喂入历史"设定下的
    # 饱和值（0.992）无区分度——仅作回归护栏，绝不参与采纳决策。
    all_ids = [r.get("question_id") for r in baseline_records]
    if args.metric in ("mrr", "ndcg"):
        # 2026-08-25（缺口N）：连续值配对——二值配对丢排序信息。
        # mrr → 每题 mrr；ndcg → 每题 ndcg（排序敏感）。
        _key = "mrr" if args.metric == "mrr" else "ndcg"
        _base_map = {p["question_id"]: p.get(_key, 0.0) for p in _mrr_base_perq}
        _exp_map = {p["question_id"]: p.get(_key, 0.0) for p in _mrr_exp_perq}
        stats = _paired_mrr_stats(_base_map, _exp_map, all_ids)
    else:
        stats = _paired_stats(_base_correct, exp_correct, all_ids)
    delta = stats.get("delta", 0.0)
    ci_low = stats.get("ci_low", 0.0)
    p_value = stats.get("mcnemar_p", 1.0)
    accepted = stats.get("n", 0) > 0 and delta > 0 and ci_low > 0
    reason = (
        f"paired: delta={delta:+.3f} CI=[{stats.get('ci_low')}, {stats.get('ci_high')}] "
        f"p={p_value} — accepted" if accepted
        else f"not accepted: delta={delta:+.3f} CI=[{stats.get('ci_low')}, {stats.get('ci_high')}] "
             f"p={p_value} (need delta>0 AND CI.low>0)"
    )
    result = {
        "ts": datetime.now().isoformat(), "tag": args.tag,
        "variant": args.variant, "n": stats.get("n", 0),
        "baseline_score": baseline_acc, "experimental_score": exp_acc,
        "delta": round(delta, 4), "accepted": accepted, "reason": reason,
        "ci_low": stats.get("ci_low"), "ci_high": stats.get("ci_high"),
        "mcnemar_p": stats.get("mcnemar_p"), "b01": stats.get("b01"), "b10": stats.get("b10"),
    }
    out_path = os.path.join(OUT_DIR, f"ab_{args.tag}_{int(time.time())}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"saved -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
