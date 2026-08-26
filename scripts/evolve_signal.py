#!/usr/bin/env python3
"""evolve_signal.py — 自进化信号采集器（SELF_EVOLUTION_DESIGN 阶段 1）。

采集 Trinity 当前"性能画像"（基线），供自进化闭环的 A/B 对比使用：
  1. QA 基线：50 题 LongMemEval 子集（seed42，RouteReasoner，judge3 三票）
     ——与 rr_ab50.py 同口径（可选 --skip-qa 跳过以省时）；
  2. 运行指标：/metrics（写放大/查询分布/缓存命中）+ /health；
  3. 数据质量：doc 占比 / 重复率 / 分层覆盖 / active 规模；
  4. 审计健康：verify_audit_integrity 结果。

输出：~/.trinity/evolve/signal_<ts>.json（性能画像）+ stdout 摘要。

用法：
    python scripts/evolve_signal.py                 # 全量（QA + 指标 + 质量）
    python scripts/evolve_signal.py --skip-qa       # 跳过 QA（快，仅运行指标）
    python scripts/evolve_signal.py --out custom.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

SIGNAL_DIR = os.path.expanduser("~/.trinity/evolve")
QA_DATA = r"C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json"
DEFAULT_N_QA = 20  # 自进化用小集（20 题 ~8min；--n-qa 可调）
PRIVATE_HOLDOUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "benchmark", "private_holdout.json",
)


def _collect_metrics() -> dict:
    """采集 /metrics 与 /health（失败降级空）。"""
    out = {}
    try:
        req = urllib.request.Request("http://127.0.0.1:8001/metrics")
        with urllib.request.urlopen(req, timeout=8) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("trinity_"):
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0]
                    try:
                        out[key] = float(parts[1])
                    except ValueError:
                        out[key] = parts[1]
    except Exception as exc:
        out["metrics_error"] = str(exc)[:80]
    try:
        req = urllib.request.Request("http://127.0.0.1:8001/health")
        with urllib.request.urlopen(req, timeout=8) as resp:
            h = json.loads(resp.read().decode())
        out["health"] = h.get("status")
        out["engine"] = h.get("components", {}).get("engine")
    except Exception as exc:
        out["health_error"] = str(exc)[:80]
    return out


def _collect_quality(db: str) -> dict:
    """数据质量统计（只读）。"""
    q = {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=8)
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) c FROM memories WHERE status='active'").fetchone()["c"]
        doc = conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE status='active' "
            "AND (category LIKE 'doc:%' OR category LIKE 'doc_%')"
        ).fetchone()["c"]
        # 精确重复
        dups = conn.execute(
            "SELECT COUNT(*) c FROM (SELECT content_hash FROM memories WHERE status='active' "
            "GROUP BY content_hash HAVING COUNT(*)>1)"
        ).fetchone()["c"]
        layers = conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE status='active' AND memory_layer IS NULL"
        ).fetchone()["c"]
        audit = conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
        conn.close()
        q = {
            "active_total": total,
            "doc_share": round(doc / max(total, 1), 4),
            "exact_dup_groups": dups,
            "null_layer_active": layers,
            "audit_entries": audit,
        }
    except Exception as exc:
        q["quality_error"] = str(exc)[:80]
    return q


def _run_qa(n: int, data_path: str = "") -> dict:
    """50 题口径的子集 QA（seed42，RouteReasoner）。返回正确题数/总数/耗时。

    data_path：私有留出子集（R8 P1-①）——自进化采纳样本应指向
    benchmark/private_holdout.json（防公开集污染/饱和）；缺省用公开集。
    """
    path = data_path or QA_DATA
    if not os.path.exists(path):
        return {"qa_error": f"data not found: {path}"}
    sys.path.insert(0, REPO)
    import tempfile

    # 用临时库（不污染权威库）——A/B 对比需要隔离
    # 2026-08-25（缺口M 修正）：先设 TRINITY_STORE 再 import trinity——
    # trinity/__init__ 导入时 ensure_bootstrapped() 创建全局聚合器绑定当前库，
    # 顺序颠倒会绑定默认大库导致检索查不到隔离库内容。
    os.environ["TRINITY_STORE"] = tempfile.mkdtemp(prefix="evolve_signal_")
    os.environ["TRINITY_LLM_EXTRACT"] = "off"
    os.environ["TRINITY_ISOLATE_TEST_WRITES"] = "off"
    from trinity.qa.route_reasoner import RouteReasoner
    from trinity import Trinity

    with open(path, encoding="utf-8") as f:
        blob = json.load(f)
    # 私有留出子集用 {"questions": [...]} 包装；公开集是裸列表
    data = blob.get("questions", blob) if isinstance(blob, dict) else blob
    random.seed(42)
    sample = random.sample(data, min(n, len(data)))

    mem = Trinity(use_ann=True)  # 2026-08-25：启用 ANN 向量通道（评测与生产对齐）

    def search_fn(q, top_k=5, agent_id=None, persona_id=None):
        return mem.search(q, top_k=top_k, agent_id=agent_id)

    rr = RouteReasoner(search_fn=search_fn, top_k=12, turn_top_k=16)
    if not rr.available:
        return {"qa_error": "no LLM key (RouteReasoner unavailable)"}

    answers = []
    t0 = time.time()
    for qi, q in enumerate(sample):
        qid = q["question_id"]
        qtype = q["question_type"]
        agent = f"sig_{qi}"
        sessions = q.get("haystack_sessions", [])
        sids = q.get("haystack_session_ids") or []
        dates = q.get("haystack_dates") or []
        # 2026-08-25（遗留修复）：qtype-aware ingest——与 rr_ab50/evolve_ab 同口径：
        # multi-session 用 turn 粒度，其他用 session 粒度聚合（RouteReasoner
        # 的 temporal/plain/pref 策略依赖 session 上下文）。
        if str(qtype) == "multi-session":
            for si, (sid, sdate) in enumerate(zip(sids, dates)):
                sess_content = sessions[si] if si < len(sessions) else []
                turns = sess_content if isinstance(sess_content, list) else sess_content.get("turns", [])
                for turn in turns:
                    role = turn.get("role", "user") if isinstance(turn, dict) else "user"
                    text = str(turn.get("content") or turn.get("text") or "")
                    if not text.strip():
                        continue
                    ts = sdate or ""
                    content = f"[DATE: {ts}] [{role}] {text.strip()}" if ts else text.strip()
                    try:
                        mem.ingest(
                            content,
                            agent_id=agent,
                            metadata={"proposition_type": "user_fact"} if qtype == "single-session-preference" else {},
                        )
                    except Exception:
                        pass  # 重复 content（同 agent）跳过——与 rr_ab50 同容错
        else:
            for si, (sid, sdate) in enumerate(zip(sids, dates)):
                sess_content = sessions[si] if si < len(sessions) else []
                turns = sess_content if isinstance(sess_content, list) else sess_content.get("turns", [])
                parts = []
                for turn in turns:
                    role = turn.get("role", "user") if isinstance(turn, dict) else "user"
                    text = str(turn.get("content") or turn.get("text") or "")
                    parts.append(f"[{role}] {text}")
                content = chr(10).join(parts)
                if not content.strip():
                    continue
                ts = sdate or ""
                if ts:
                    content = f"[DATE: {ts}] {content}"
                try:
                    mem.ingest(
                        content,
                        agent_id=agent,
                        metadata={"proposition_type": "user_fact"} if qtype == "single-session-preference" else {},
                    )
                except Exception:
                    pass  # 重复 content（同 agent）跳过——与 rr_ab50 同容错
        ans = rr.answer(q["question"], qtype=qtype, question_date=dates[-1] if dates else None,
                        agent_id=agent)
        # 2026-08-25（缺口E）：补 expected/question_type——使 signal 的 QA 结果
        # 可直接作 evolve_ab --baseline-json 基线（judge3 判分需要 expected），
        # 避免 A/B 重跑 base 轮（省 ~50% 时间）。
        answers.append({
            "question_id": qid,
            "question_type": qtype,
            "expected": str(q.get("answer", ""))[:300],
            "answer": ans.get("answer"),
            "strategy": ans.get("strategy"),
        })
    elapsed = time.time() - t0
    return {"n": len(sample), "elapsed_s": round(elapsed, 1), "answers": answers}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-qa", action="store_true")
    parser.add_argument("--n-qa", type=int, default=DEFAULT_N_QA)
    parser.add_argument("--data", default="",
                        help="私有留出子集路径（R8 P1-①，默认公开集）")
    parser.add_argument("--db", default=os.path.expanduser("~/.trinity/store/trinity_store.db"))
    parser.add_argument("--out", default="")
    parser.add_argument("--metric", default="qa", choices=["qa", "retrieval", "mrr", "ndcg"],
                        help="评测指标：qa=judge3 准确率（默认）；retrieval=R@5；mrr=MRR；ndcg=nDCG@5（排序敏感）")
    parser.add_argument("--strategy", default="rrf", choices=["rrf", "fusion"],
                        help="混合检索融合策略（fusion 时通道权重 env 生效）")
    args = parser.parse_args()

    os.makedirs(SIGNAL_DIR, exist_ok=True)
    # 2026-08-25（R@5 主信号）：--metric retrieval 时用 _run_retrieval 代替
    # _run_qa——R@5 确定性指标（无 LLM 回答波动），作为自进化主信号。
    if args.metric in ("retrieval", "mrr", "ndcg"):
        from scripts.evolve_ab import _run_retrieval
        qa_part = _run_retrieval(args.n_qa, {}, "signal", args.data, strategy=args.strategy)
        # 固化 baseline（确定性，无需 judge）：mrr 用 MRR，retrieval 用 R@5
        _bkey = {"mrr": "mrr", "ndcg": "ndcg"}.get(args.metric, "r5")
        qa_part["baseline_acc"] = qa_part.get(_bkey, 0.0)
        qa_part["baseline_correct_ids"] = sorted(
            x["question_id"] for x in qa_part.get("per_question", []) if x.get("recall"))
        # 2026-08-25（R@5 配对统计）：保存全部题 id——evolve_ab 的配对
        # McNemar 需要 all_ids（baseline + exp 全部题），否则只含 correct 子集
        # 导致配对统计样本不全。
        qa_part["all_question_ids"] = sorted(
            x["question_id"] for x in qa_part.get("per_question", []))
        # 2026-08-25（缺口N）：保留每题 mrr——evolve_ab MRR 连续值配对需要
        # baseline 的逐题 mrr（二值 recall 配对丢排序信息）。
        qa_part["mrr_per_question"] = {
            x["question_id"]: x.get("mrr", 0.0) for x in qa_part.get("per_question", [])
        }
        # 2026-08-25（nDCG）：固化每题 ndcg——nDCG 连续值配对需要 baseline 逐题 ndcg
        qa_part["ndcg_per_question"] = {
            x["question_id"]: x.get("ndcg", 0.0) for x in qa_part.get("per_question", [])
        }
        qa_part.pop("per_question", None)
    else:
        qa_part = _run_qa(args.n_qa, args.data)

    signal = {
        "ts": datetime.now().isoformat(),
        "metrics": _collect_metrics(),
        "quality": _collect_quality(args.db),
        "qa": {} if args.skip_qa else qa_part,
        "metric": args.metric,
    }

    # 2026-08-25（baseline 一致性修复）：signal 生成时 judge QA 一次并固化
    # correct_ids 到 signal 文件——所有 A/B 候选共享同一 baseline 判定，
    # 消除"每候选重新 judge baseline"造成的 run-to-run 抖动（0.6/0.7/0.8）。
    if signal["qa"].get("answers") and args.metric != "retrieval":
        try:
            rec_file = os.path.join(SIGNAL_DIR, f"sig_records_{int(time.time())}.json")
            with open(rec_file, "w", encoding="utf-8") as f:
                json.dump({"records": signal["qa"]["answers"]}, f, ensure_ascii=False)
            from scripts.evolve_ab import _judge
            _acc, _correct = _judge(rec_file)
            signal["qa"]["baseline_acc"] = _acc
            signal["qa"]["baseline_correct_ids"] = sorted(_correct)
            print(f"  baseline judged: acc={_acc:.3f} correct={len(_correct)}/{signal['qa']['n']}")
        except Exception as exc:
            print(f"  baseline judge skipped: {exc}")

    out = args.out or os.path.join(SIGNAL_DIR, f"signal_{int(time.time())}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(signal, f, ensure_ascii=False, indent=1, default=str)

    # stdout 摘要
    q = signal["quality"]
    m = signal["metrics"]
    qa = signal["qa"]
    print(f"signal written -> {out}")
    print(f"  active={q.get('active_total')} doc_share={q.get('doc_share', 0)*100:.1f}% "
          f"dup_groups={q.get('exact_dup_groups')} null_layer={q.get('null_layer_active')} "
          f"audit={q.get('audit_entries')}")
    print(f"  metrics: write_amp={m.get('trinity_write_amplification')} "
          f"queries={m.get('trinity_queries_total')} cache_hit={m.get('trinity_semantic_cache_hit_rate_pct')}")
    if qa.get("n"):
        print(f"  qa: n={qa['n']} elapsed={qa['elapsed_s']}s（answers 见 signal 文件，judge3 判分在 evolve_ab）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
