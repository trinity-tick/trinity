#!/usr/bin/env python3
"""Trinity × LongMemEval-V2 适配 runner（2026-08-17, 建议7 落地）

LongMemEval-V2：面向经验丰富同事的长期智能体记忆评估（451 题 / 5 能力 /
web+enterprise 双领域 / small+medium 两档；官方 harness 已同步至
~/.trinity/bench-official/lmev2/LongMemEval-V2-main）。

协议（对齐官方 evaluation/harness.py）：
- questions.jsonl: {id, domain, question(文本或{text,image}), category,
  answer(\boxed{...} 格式), ...}
- trajectories.jsonl: {id, steps:[{goal, thought, action, observation/a11y, ...}]}
- haystacks/lme_v2_<tier>.json: {question_id: [trajectory_id, ...]}

本 runner 职责：
1) 每题的 haystack 轨迹文本化摄入 Trinity（隔离临时库，agent 作用域）
2) 问题混合检索 top-K 证据
3) QA（DeepSeek，boxed 格式 + UNKNOWN 弃权；--judge 开启 LLM 判定）
4) 报告：答案准确率 / 弃权率 / 检索延迟（LAFS 输入）/ 按 5 能力分组

注意：多模态截图未摄入（文本化 a11y/动作/思考），能力=premise-awareness
需图像上下文时该能力受限——诚实口径在报告中标注。

Usage:
  python benchmark/longmemeval_v2_runner.py --data-root <dir> --tier small \
      --domain web --limit 10 --qa
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["TRINITY_MEMORY_ENABLED"] = "0"

# V2 category → 5 能力（README）
ABILITY_MAP = {
    "static-environment": "static-state-recall",
    "static-environment-abs": "static-state-recall",
    "dynamic-environment": "dynamic-state-tracking",
    "dynamic-environment-abs": "dynamic-state-tracking",
    "procedure": "workflow-knowledge",
    "procedure-abs": "workflow-knowledge",
    "errors-gotchas": "environment-gotchas",
    "errors-gotchas-abs": "environment-gotchas",
    "premise": "premise-awareness",
    "premise-abs": "premise-awareness",
}

BOXED_RE = re.compile(r"\\boxed\{([^}]*)\}")


def extract_boxed(answer: str):
    m = BOXED_RE.search(answer or "")
    return m.group(1).strip() if m else (answer or "").strip()


def load_jsonl(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def serialize_trajectory(traj: dict) -> str:
    """轨迹 → 文本（steps: goal/thought/action/a11y observation）。"""
    parts = []
    steps = traj.get("steps") or traj.get("trajectory") or []
    for i, s in enumerate(steps):
        lines = [f"--- step {i} ---"]
        for key in ("goal", "thought", "thoughts", "action", "actions"):
            v = s.get(key)
            if isinstance(v, str) and v.strip():
                lines.append(f"[{key}] {v.strip()}")
            elif isinstance(v, dict):
                lines.append(f"[{key}] {json.dumps(v, ensure_ascii=False)[:800]}")
        obs = s.get("observation") or s.get("a11y") or s.get("a11y_tree")
        if isinstance(obs, str) and obs.strip():
            lines.append(f"[observation] {obs.strip()[:2000]}")
        elif isinstance(obs, dict):
            lines.append(f"[observation] {json.dumps(obs, ensure_ascii=False)[:2000]}")
        parts.append("\n".join(lines))
    goal = traj.get("goal", "")
    if goal and parts:
        parts.insert(0, f"[goal] {goal}")
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Trinity × LongMemEval-V2 runner")
    parser.add_argument("--data-root", required=True, help="含 questions.jsonl / trajectories.jsonl / haystacks/ 的目录")
    parser.add_argument("--tier", choices=["small", "medium"], default="small")
    parser.add_argument("--domain", choices=["web", "enterprise"], default=None)
    parser.add_argument("--limit", type=int, default=0, help="0=全部")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--qa", action="store_true", help="启用 DeepSeek QA 判定")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    data_root = os.path.abspath(args.data_root)
    q_path = os.path.join(data_root, "questions.jsonl")
    t_path = os.path.join(data_root, "trajectories.jsonl")
    hay_path = os.path.join(data_root, "haystacks", f"lme_v2_{args.tier}.json")
    for p in (q_path, t_path, hay_path):
        if not os.path.exists(p):
            print(f"[v2] 数据集文件缺失: {p}（HF 不可达时无法下载，见 README）")
            sys.exit(2)

    questions = load_jsonl(q_path)
    if args.domain:
        questions = [q for q in questions if q.get("domain") == args.domain]
    if args.limit and args.limit > 0:
        questions = questions[: args.limit]
    trajectories = {t["id"]: t for t in load_jsonl(t_path)}
    haystack = json.load(open(hay_path, encoding="utf-8"))
    print(f"[v2] questions={len(questions)} tier={args.tier} domain={args.domain or 'all'}")

    tmpdir = tempfile.mkdtemp(prefix="lmev2_")
    from trinity import Trinity
    mem = Trinity(adapter="sqlite", store_path=tmpdir)

    # DeepSeek judge
    llm_chat = None
    if args.qa:
        api_key = None
        cred = os.path.expanduser("~/.dsh/.credentials.yaml")
        if os.path.exists(cred):
            for line in open(cred, encoding="utf-8-sig"):
                if line.strip().startswith("DEEPSEEK_API_KEY"):
                    api_key = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break
        if not api_key:
            print("[v2] 未找到 DEEPSEEK_API_KEY，--qa 不可用")
            sys.exit(2)
        import urllib.request

        def llm_chat(system, user, max_tokens=160, temp=0.0):
            payload = {"model": "deepseek-chat",
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}],
                       "temperature": temp, "max_tokens": max_tokens}
            req = urllib.request.Request(
                "https://api.deepseek.com/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]

    results = []
    t0 = time.time()
    for qi, q in enumerate(questions):
        qid = str(q.get("id", qi))
        qtype = str(q.get("category", ""))
        question = q.get("question")
        if isinstance(question, dict):
            question = question.get("text", "")
        question = str(question)
        expected = extract_boxed(str(q.get("answer", "")))
        ability = ABILITY_MAP.get(qtype, qtype or "unknown")
        traj_ids = haystack.get(qid, [])
        agent = f"lmev2_{qi}"

        # 1) 摄入 haystack 轨迹
        t_ing = time.time()
        ingested = 0
        for tid in traj_ids:
            traj = trajectories.get(tid)
            if not traj:
                continue
            text = serialize_trajectory(traj)
            if not text.strip():
                continue
            try:
                r = mem.ingest(text, agent_id=agent, category="lmev2",
                               tags=["lmev2", qtype], postprocess=False)
                if r.get("memory_id"):
                    ingested += 1
            except Exception:
                continue
        dt_ing = time.time() - t_ing

        # 2) 检索
        t_r = time.time()
        hits = mem.search(question, top_k=args.top_k, agent_id=agent)
        dt_r = time.time() - t_r
        hit_list = hits.get("results", []) if isinstance(hits, dict) else (hits or [])

        # 3) QA
        qa_correct = None
        qa_answer = None
        if args.qa and hit_list:
            ctx = "\n---\n".join((h.get("content") or "")[:800] for h in hit_list[:5])
            sys_p = (
                "You are an experienced colleague in a customized web/enterprise environment. "
                "Answer based ONLY on the provided memory evidence. "
                "If the information is not present, output exactly \\boxed{UNKNOWN}. Do not guess."
            )
            try:
                raw = llm_chat(sys_p, f"Memory evidence:\n{ctx}\n\nQuestion: {question}\nAnswer:")
                qa_answer = extract_boxed(raw)
                norm = lambda s: (s or "").strip().lower()
                if norm(qa_answer) == "unknown" or norm(qa_answer) == "":
                    qa_correct = norm(expected) == "unknown"
                else:
                    qa_correct = norm(qa_answer) == norm(expected) or (
                        expected and norm(expected) in norm(qa_answer)
                    )
            except Exception as exc:
                qa_answer = f"ERR:{type(exc).__name__}"

        results.append({
            "question_id": qid, "category": qtype, "ability": ability, "domain": q.get("domain"),
            "question": question[:150], "expected": expected[:80],
            "n_trajectories": len(traj_ids), "ingested": ingested,
            "n_retrieved": len(hit_list), "retrieval_latency_s": round(dt_r, 4),
            "ingest_latency_s": round(dt_ing, 4),
            "qa_correct": qa_correct, "qa_answer": (qa_answer or "")[:120],
        })
        if (qi + 1) % 10 == 0 or qi + 1 == len(questions):
            print(f"[v2] {qi + 1}/{len(questions)} {time.time() - t0:.0f}s", flush=True)

    # 4) 汇总（按能力 + 域）
    n = len(results)
    report = {
        "dataset": "longmemeval-v2", "tier": args.tier, "domain": args.domain or "all",
        "questions": n, "top_k": args.top_k, "multimodal_note": "截图未摄入，仅文本化 a11y/动作/思考",
        "by_ability": {}, "by_domain": {}, "overall": {},
    }
    acc = sum(1 for x in results if x["qa_correct"]) / max(1, sum(1 for x in results if x["qa_correct"] is not None))
    abst = sum(1 for x in results if x["qa_answer"] and x["qa_answer"].lower() == "unknown")
    lat = sum(x["retrieval_latency_s"] for x in results) / max(1, n)
    report["overall"] = {
        "answer_accuracy": round(acc, 4),
        "abstention": round(abst / max(1, n), 4),
        "mean_retrieval_latency_s": round(lat, 4),
        "ingested_trajectories": sum(x["ingested"] for x in results),
        "n_evaluated": sum(1 for x in results if x["qa_correct"] is not None),
    }
    for ability in sorted({x["ability"] for x in results}):
        sub = [x for x in results if x["ability"] == ability]
        ev = [x for x in sub if x["qa_correct"] is not None]
        report["by_ability"][ability] = {
            "n": len(sub),
            "answer_accuracy": round(sum(1 for x in ev if x["qa_correct"]) / max(1, len(ev)), 4),
            "n_evaluated": len(ev),
        }
    for dom in sorted({x.get("domain") or "?" for x in results}):
        sub = [x for x in results if (x.get("domain") or "?") == dom]
        ev = [x for x in sub if x["qa_correct"] is not None]
        report["by_domain"][dom] = {
            "n": len(sub),
            "answer_accuracy": round(sum(1 for x in ev if x["qa_correct"]) / max(1, len(ev)), 4),
        }

    out_path = args.out or os.path.join(
        os.path.expanduser("~/.trinity/bench-results"),
        f"lmev2_{args.tier}_{args.domain or 'all'}_{time.strftime('%Y%m%d_%H%M%S')}.json",
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"report": report, "results": results}, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("报告已保存:", out_path)


if __name__ == "__main__":
    main()
