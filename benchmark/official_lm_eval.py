#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""官方 LongMemEval 评测（2026-09-02，十一评阶段 A：mock 降级 → 官方实测）

数据集: benchmark/data/longmemeval_oracle.json（xiaowu0162/longmemeval-cleaned, 500q, 6 类）

评测语义（对齐官方 oracle 变体）:
  - 每问: 把 haystack_sessions 的消息全部摄入临时库（每条消息=一条记忆, session_id=会话id）
  - R@k: 检索 query 的 top-k 中是否含 answer_session_ids 会话的消息（官方 R@k 语义）
  - AnswerAcc（--answer）: top-k 上下文 → LLM 生成 → judge 判定是否覆盖 gold answer

用法:
  python benchmark/official_lm_eval.py --limit 500          # R@k 全量（无 LLM, 快）
  python benchmark/official_lm_eval.py --limit 100 --answer # R@k + AnswerAcc 子集
"""
# NOTICE(EXECUTION 458C): 官方 LongMemEval 锁定数字入口（正式）——分工见 docs/RUNNER_MAP.md。
import argparse
import collections
import json
import os
import re
import shutil
import sys
import tempfile
import time
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:  # 独立脚本可能没有 trinity 路径：退回原静默行为
    def swallow(*_a, **_k):
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(*_a, **_k)
        except Exception:
            return None

ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

DSET = os.path.join(ROOT, "benchmark", "data", "longmemeval_oracle.json")
PRICING = {"input": 0.27, "output": 1.10}


def normalize(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())


def _llm_callable(model: str = "deepseek-chat", timeout: int = 60,
                  base_url: str = "", api_key: str = ""):
    """构造 LLM 调用器。`base_url` / `api_key` 允许按**角色**覆盖（2026-09-27 加）。

    理由：跨家族判分（BP-4/R3）要求 judge 与 reader **不是同一模型**，而不同家族
    往往意味着**不同厂商端点与密钥** ⇒ 只换模型名不够，必须能同时换端点/密钥。
    默认参数全为空 ⇒ 行为与改动前**一字不差**（读 `TRINITY_LLM_*` 与 DEEPSEEK_API_KEY）。
    """
    from trinity.daemon.memory_compressor import create_llm_compress_callable
    creds = {}
    path = os.path.expanduser("~/.dsh/.credentials.yaml")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8-sig"):
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
            if m and not line.strip().startswith("#"):
                creds[m.group(1)] = m.group(2).strip().strip("'\"")
    api_key = (api_key or os.environ.get("TRINITY_LLM_API_KEY")
               or creds.get("DEEPSEEK_API_KEY"))
    return create_llm_compress_callable(
        base_url=(base_url or os.environ.get("TRINITY_LLM_BASE_URL",
                                            "https://api.deepseek.com/v1")),
        api_key=api_key, model=model, timeout=timeout)


def resolve_judge_llm(args, reader_llm=None):
    """判分模型解析（2026-09-27 新增 `--judge-model` 接线）。

    **动机（实测口径缺陷）**：此前 judge 与 reader **恒为同一个 `llm` 对象**
    （`judge(llm, ...)`），`caliber_block` 也只能把 `judge_model` 记成 reader 模型名
    ⇒ 所有 QA 分数都是**自评口径**，跨 judge 家族不可比（BP-4 / R3；公开榜多为 GPT-4o judge）。

    **接线规则**：
      - `--judge-model` 为空 ⇒ **跟随 reader**（默认，行为与改动前完全一致）；
      - 非空且与 `--model` 不同 ⇒ 单独构造判分调用器，端点取
        `--judge-base-url` → `TRINITY_JUDGE_LLM_BASE_URL` → `TRINITY_LLM_BASE_URL` → deepseek 默认；
        密钥取 `TRINITY_JUDGE_LLM_API_KEY` → `TRINITY_LLM_API_KEY` → `DEEPSEEK_API_KEY`。
    **判据**：`tests/unit/test_judge_model_wiring.py`（空=同一对象；非空=不同对象；
    并断言 caliber 的 `judge_model` 记的是**判分**模型名）。
    """
    jm = str(getattr(args, "judge_model", "") or "").strip()
    rm = str(getattr(args, "model", "") or "").strip()
    if not jm or jm == rm:
        return reader_llm
    jbase = (str(getattr(args, "judge_base_url", "") or "").strip()
             or os.environ.get("TRINITY_JUDGE_LLM_BASE_URL", "").strip())
    jkey = (os.environ.get("TRINITY_JUDGE_LLM_API_KEY", "").strip()
            or os.environ.get("TRINITY_LLM_API_KEY", "").strip())
    return _llm_callable(jm, base_url=jbase, api_key=jkey)


JUDGE_SYS = ("You are a strict fact-checker for a memory benchmark. Given a QUESTION, "
             "the GOLD ANSWER, and the MODEL ANSWER, decide whether the model answer "
             "contains the gold answer's key fact(s) (paraphrase allowed). "
             "Reply with exactly YES or NO.")


def judge(llm, question, gold, model_ans):
    # 2026-09-02: LLM 偶发返回非字符串（int/dict）——全面 str() 防御
    question = str(question or "")
    gold = str(gold or "")
    model_ans = str(model_ans or "")
    if not model_ans.strip():
        return False
    an = normalize(model_ans)
    gn = normalize(gold)
    if gn and len(gn) >= 4 and gn in an:
        return True
    try:
        out = str(llm(JUDGE_SYS, "QUESTION: %s\nGOLD ANSWER: %s\nMODEL ANSWER: %s\n\nDoes the model answer contain the gold answer's key fact? Reply YES or NO."
                    % (question[:300], gold[:300], model_ans[:600]))).strip().upper()
        return out.startswith("YES")
    except Exception:
        return False


DATE_RE = re.compile(r"\b(20\d{2}[-/\u5e74]\d{1,2}([-/\u6708]\d{1,2})?|\d{1,2}[-/\u6708]\d{1,2}[-/]20\d{2}|"
                     r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ .]?\d{1,2}(st|nd|rd|th)?(,? ?20\d{2})?)\b", re.I)


def _date_clues(results):
    seen = []
    for h in results[:5]:
        c = str(h.get("content") or "")
        for m in DATE_RE.finditer(c):
            if m.group(0) not in seen:
                seen.append(m.group(0))
    return "; ".join(seen[:40]) or "(none)"


def build_qa_prompt(qtype, question, results, strategy, cap=5, qdate=None, item_chars=600):
    """按题型路由生成提示（EXECUTION 460；策略经 458.1b A/B 验证：
    TR 用日期线索+时序（+13.3pp），MS/SS-P 用跨会话整合+会话标注（MS +6.7pp / SS-P +20pp），
    其余题型保持 base 口径不变）。cap：上下文条数上限（默认 5 = 锁定口径）。
    item_chars：**单条上下文截断上限**（默认 600 = 锁定口径）。
    2026-09-24（§1326 口径闭环）：新增该参数**只为** full-context 基线臂服务 ——
    基线臂要把整段 haystack 灌进去，600 字符/会话会把 53 个会话压成 3 万字符的"伪全上下文"。
    默认值不变 ⇒ 既有检索臂行为**逐字节不变**（判据见 tests/unit/test_fullctx_arm.py）。"""
    hits = results[:cap]
    if strategy == "base":
        ctx = "\n\n".join("[%d] %s" % (i + 1, str(h.get("content", ""))[:item_chars])
                            for i, h in enumerate(hits))
        prompt = ("Question: %s\n\nContext:\n%s\n\nAnswer concisely using ONLY the context. Answer:"
                  % (question, ctx))
        return "You are an AI assistant answering from memory context only. Answer concisely.", prompt
    tagged = []
    for i, h in enumerate(hits):
        c = str(h.get("content") or "")[:item_chars]
        sid = str(h.get("session_id") or "")
        tagged.append("[%d][session:%s] %s" % (i + 1, sid[:18], c) if sid else "[%d] %s" % (i + 1, c))
    ctx = "\n\n".join(tagged)
    base_sys = "You are an AI assistant answering from memory context only. Answer concisely."
    if qtype == "temporal-reasoning":
        sys_p = base_sys + " Pay attention to dates and temporal order."
        # 2026-09-14（735）**问题日期锚点**（TRINITY_TR_QDATE，默认 off，A/B 后决定）：
        # 动机：temporal-reasoning 大量问"多少天前/多久之后"，而提示里只有上下文日期、没有"今天是哪天"，
        # 模型只能靠猜。数据集本身带 question_date（调用点已传入）。
        # ⚠️ 743 **已回滚为默认 off**：735 的 A/B（0.658→0.758）是在 **oracle 设定**下做的（EXECUTION 742 发现），
        # 在 **full-haystack** 上复测（n=30）：off 0.0333 / on 0.0333 ⇒ **Δ=+0.000，不泛化**。
        # 按纪律"未过验收门（在真实检索设定下）即回滚"，默认保持 off；保留开关供后续在 S 上继续实验。
        if os.environ.get("TRINITY_TR_QDATE", "off").lower() in ("1", "on", "true", "yes") and qdate:
            sys_p = sys_p + (" The current date is %s; compute any interval relative to it." % str(qdate)[:20])
        clues = _date_clues(results)
        user_p = ("Question: %s\n\nContext:\n%s\n\nDate clues in context: %s\n\n"
                  "If the question asks when/order, reason carefully from the date clues. "
                  "Answer concisely using ONLY the context. Answer:") % (question, ctx, clues)
    elif qtype == "multi-session":
        sys_p = base_sys + " Integrate evidence across sessions; if sessions conflict, trust the later one."
        user_p = ("Question: %s\n\nContext (multiple sessions of the same user):\n%s\n\n"
                  "Note: integrate facts across sessions; conflicting facts resolve to the later session. "
                  "Answer concisely using ONLY the context. Answer:") % (question, ctx)
    elif qtype == "single-session-preference":
        sys_p = base_sys + (" Integrate evidence across sessions and answer in line with "
                            "this user's expressed preferences.")
        user_p = ("Question: %s\n\nContext:\n%s\n\n"
                  "Note: integrate the user's preferences; answer in the style/direction they would prefer. "
                  "Answer concisely using ONLY the context. Answer:") % (question, ctx)
    elif qtype == "knowledge-update":
        # EXECUTION 460: KU = 新信息覆盖旧信息——same conflict-newer logic as MS
        sys_p = base_sys + (" Knowledge updates supersede older facts; "
                            "answer with the most recent correct information.")
        user_p = ("Question: %s\n\nContext (ordered by recency):\n%s\n\n"
                  "Note: newer messages may update/override earlier facts; answer with the LATEST "
                  "correct information only. Answer concisely using ONLY the context. Answer:") % (question, ctx)
    else:
        return base_sys, ("Question: %s\n\nContext:\n%s\n\n"
                          "Answer concisely using ONLY the context. Answer:" % (question, ctx))
    return sys_p, user_p


# EXECUTION 463: 按类目路由的检索/上下文配置（top_k=检索条数，cap=上下文条数上限）
# 463 全量复测结论：SS-P/KU 的 cap14 外推在子集(+10pp/+6.7pp)不稳健——
# 全量 v3=0.626 < v2 0.642（SS-P -6.7pp/MS -3.0pp 噪声翻转），已回滚；
# EXECUTION 467 复测结论：MS 查询词覆盖组装 30 题 +10pp 但在全量翻转（v4=0.618 < v2 0.642，
# MS -9.8pp）→ 已回滚。经验固化：30 题抽样对 MS 类不可靠，采纳前必须全量验证或 ≥60 题分层样本。
# 官方锁定口径 = v2（0.642）：multi-session 20/14（EXECUTION 462 全量锁证 +22.6pp）。
# EXECUTION 469 实验：temporal-reasoning 时间线数据层（top-40 日期排序上下文），30 题 +10.0pp flips=7——
# 全量 v5 验证通过才转正式；默认已开启仅当本文件作为评测入口（带 --strategy routed）。
_ROUTE = {
    "multi-session": {"top_k": 20, "cap": 14},
    "temporal-reasoning": {"top_k": 40, "cap": 8, "mode": "timeline"},
}

_MONS = ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"]
_MON3 = [m[:3] for m in _MONS]


def _date_key(text):
    low = str(text or "").lower()
    words = low.split()
    for i, w in enumerate(words):
        if w in _MONS or w in _MON3:
            mon = (_MONS.index(w) if w in _MONS else _MON3.index(w)) + 1
            day = 0
            year = 0
            if i + 1 < len(words):
                dig = "".join(ch for ch in words[i + 1] if ch.isdigit())
                if dig:
                    v = int(dig)
                    if v > 31:
                        year = v
                    else:
                        day = v
            if i + 2 < len(words):
                d2 = "".join(ch for ch in words[i + 2] if ch.isdigit())
                if d2 and 1900 <= int(d2) <= 2100:
                    year = int(d2)
            return (year, mon, day)
        parts = w.split("-")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            return (int(parts[0]), int(parts[1]), int(parts[2]))
    return None


def timeline_ctx(results, k=8):
    """时间线数据层（EXECUTION 469）：**先按相关性取前 k 条，再按日期排序展示**。

    2026-09-13（重大修正）：原实现是 `evs.sort(by date)[:k]` —— **先按日期排序再取前 k**，
    等于选「**最早 k 条**」而不是「**最相关 k 条**」。

    为什么以前没暴露：内容里的日期标注此前全是 `[DATE: 2026-09-13]`（摄取当天），
    **全部相等 ⇒ 排序是稳定空操作 ⇒ 实际拿到的就是相关性前 k 条**（歪打正着）。
    本轮修好日期来源后，真实 2023 日期出现 ⇒ 排序真的生效 ⇒ 上下文变成 2022-12 的最早几条，
    模型随即从「0 days」变成「the context does not mention X」（实测 insufficient 4 → **16**）。

    ⇒ 也就是说：**EXECUTION 469 声称的 +10.0pp 是在「日期全相等」条件下测的**，
    那次 A/B 里的「时间线排序」从未真正生效过。本函数修好后，时间线才第一次真的按时间排。
    """
    head = list(results)[:k]            # ① 相关性优先（引擎序）
    dated, undated = [], []
    for i, h in enumerate(head):
        dk = _date_key(str(h.get("content") or ""))
        (dated if dk else undated).append((dk, i, h))
    dated.sort(key=lambda x: (x[0], x[1]))   # ② 仅在这 k 条内部按日期展示
    out = [h for _, _, h in dated] + [h for _, _, h in undated]
    seen = {str(h.get("content") or "") for h in out}
    for h in results:
        if len(out) >= k:
            break
        c = str(h.get("content") or "")
        if c not in seen:
            out.append(h)
            seen.add(c)
    return out[:k]


def replay_prompt_from_row(row: dict, strategy: str, cap=None):
    """从**逐题明细行**重建喂给 reader 的提示（离线 replay：固定证据，只换生成）。

    为什么需要它（2026-09-19 评测修复②）
    ------------------------------------
    实测：LongMemEval-S full-haystack **R@10=0.958 / AnswerAcc=0.612**（SS-A R@10=1.0→Acc 0.589；
    TR R@10=0.940→Acc 0.376）——**证据找到了却答不出来**，35 点缺口在「证据 → 答案」。
    但此前**无法在不重跑检索的前提下做这个定位**：逐题明细只存 top_ids，不存喂给 reader 的上下文，
    于是"提示/组装"与"reader 模型"两个变量永远混在一起（本仓原文：每次问『为什么 Acc=0.05』
    都要**重跑一次昂贵评测**）。

    本函数与在线路径**同形**重建：同样的 build_qa_prompt + 同样的 timeline 路由 + 同样的聚合块拼接
    ⇒ 两条臂（base 提示 vs routed 提示）看到的是**逐字节相同的证据**，差异只来自"生成"。

    诚实约束：**没有证据必须拒绝**（raise）——禁止"看起来跑了、其实喂了空上下文"产出漂亮读数
    （本仓 823 的同型死法：静默 fallback → 假绿）。
    """
    items = row.get("ctx_items")
    if not items:
        raise ValueError("明细行缺 ctx_items（证据未缓存）⇒ 拒绝 replay："
                         "空上下文会产出'漂亮但无意义'的读数")
    qtype = str(row.get("type") or "?")
    question = str(row.get("question") or "")
    k = int(cap) if cap else len(items)
    if strategy == "routed" and qtype == "temporal-reasoning":
        ctx_results = timeline_ctx(list(items))
        k = len(ctx_results)
    else:
        ctx_results = list(items)
    sys_p, prompt = build_qa_prompt(qtype, question, ctx_results, strategy, cap=k,
                                    qdate=row.get("qdate"))
    extra = row.get("extra") or []
    if extra:
        prompt = (prompt + "\n\nAdditional aggregated context (not a retrieved document):\n"
                  + "\n".join(str(x) for x in extra))
    return sys_p, prompt


def _run_replay(args) -> int:
    """离线 replay 入口：读明细 jsonl → 同证据重建提示 → 只换"生成"再判分。

    用法（两条臂，证据完全相同）：
        python benchmark/official_lm_eval.py --replay out/run.detail.jsonl --strategy base   --out out/replay_base.json
        python benchmark/official_lm_eval.py --replay out/run.detail.jsonl --strategy routed --out out/replay_routed.json
    """
    rows = []
    with open(args.replay, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        print("REPLAY: 明细为空 -> %s" % args.replay)
        return 2
    llm = _llm_callable(args.model)
    judge_llm = resolve_judge_llm(args, llm)
    t0 = time.time()
    n = acc = chars = 0
    by_type: dict = {}
    skipped = 0
    rows_out = []
    for r in rows:
        q = str(r.get("question") or "")
        gold = str(r.get("gold") or "")
        if not q or gold == "" or not r.get("ctx_items"):
            skipped += 1
            continue
        try:
            sys_p, prompt = replay_prompt_from_row(r, args.strategy, cap=r.get("ctx_cap"))
        except Exception as _e:  # noqa: BLE001 — 缺证据的行如实跳过，不静默喂空
            skipped += 1
            continue
        try:
            ans = llm(sys_p, prompt)
        except Exception:
            ans = ""
        ok = judge(judge_llm or llm, q, gold, ans) if ans.strip() else False
        n += 1
        chars += len(prompt)
        acc += 1 if ok else 0
        st = by_type.setdefault(str(r.get("type") or "?"), {"total": 0, "acc": 0})
        st["total"] += 1
        st["acc"] += 1 if ok else 0
        rows_out.append({"type": r.get("type"), "ok": bool(ok), "pred": str(ans)[:400],
                         "online_ok": r.get("ok"), "prompt_chars": len(prompt),
                         "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    out = {
        "test": "answer_side_replay",
        "arm": args.strategy,
        "source_detail": os.path.basename(args.replay),
        "questions": n,
        "skipped_no_evidence": skipped,
        "AnswerAcc": round(acc / n, 4) if n else None,
        "by_type": {k: {"total": v["total"], "AnswerAcc": round(v["acc"] / v["total"], 4)}
                    for k, v in sorted(by_type.items())},
        "est_cost_usd": round((chars / 1e6) * PRICING["input"], 4),
        "elapsed_s": round(time.time() - t0, 1),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "replay_rows": rows_out,
    }
    # 口径修正（2026-09-19，replay 路径自纠）：replay **恒有生成 + 判分**，但 args.answer 是 False
    # （命令行没带 --answer）⇒ 若直接 caliber_block(args) 会把 judge_model 记成 None，
    # 违反本仓刚立的"禁止沉默的未知"。这里构造一个口径视图：answer=True、retrieval=replay(arm)。
    class _CaliberView:
        pass

    _cv = _CaliberView()
    _cv.dataset = args.dataset
    _cv.model = args.model
    _cv.answer = True
    _cv.retrieval = "replay(%s)" % args.strategy
    _cv.top_k = None
    out.update(caliber_block(_cv, chars, n))
    out["judge_model"] = args.model
    # 口径以**证据来源**为准（replay 没跑检索，不能把自己的 --dataset 当成口径）
    _proto = next((r.get("protocol") for r in rows if r.get("protocol")), None)
    if _proto:
        out["protocol"] = _proto
    # 在线 vs replay 的逐题对照（同一批题、同一证据）
    both = [r for r in rows_out if r.get("online_ok") is not None]
    if both:
        flips_up = sum(1 for r in both if r["ok"] and not r["online_ok"])
        flips_dn = sum(1 for r in both if not r["ok"] and r["online_ok"])
        out["vs_online"] = {"n": len(both), "flip_to_correct": flips_up, "flip_to_wrong": flips_dn,
                            "online_acc": round(sum(1 for r in both if r["online_ok"]) / len(both), 4),
                            "replay_acc": round(sum(1 for r in both if r["ok"]) / len(both), 4)}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("REPLAY arm=%s n=%d skipped(no-evidence)=%d AnswerAcc=%.4f cost=$%.4f"
          % (args.strategy, n, skipped, out["AnswerAcc"] or 0.0, out["est_cost_usd"]))
    if out.get("vs_online"):
        print("  vs online:", json.dumps(out["vs_online"], ensure_ascii=False))
    print("  saved -> %s" % args.out)
    return 0 if n else 1


def caliber_block(args, qa_chars: int = 0, qa_n: int = 0,
                  full_context_baseline=None, full_context_baseline_src: str = "",
                  full_context_baseline_n=None) -> dict:
    """**四口径字段**（2026-09-19 口径接线①）—— 评测产物自带可比性元数据。

    为什么单列一个纯函数（而不是散在 out 字典里）
    --------------------------------------------
    网络横评（JamJet 同-judge 榜 / OmniMemEval 统一复现）证明：**同一系统换 harness 可差 30–38pp**
    （Mem0 LongMemEval 94.4 → 复现 56.00；Zep LoCoMo 94.7 → 63.83）。按 2026 年的四条纪律，
    一个可比较的分数必须同时给出：

      · BP-1/红线 R5：**检索预算**（Graphiti 每 query 用 2.6M 字符，控预算后领先消失）；
      · BP-2：**full-context 基线**（Δ = Score(MAG) − Score(FullContext)，否则基准可能根本用不到记忆）；
      · BP-4/R3：**judge 模型**（AgentMemory 96.2% 因用 GPT-4o judge 被同-judge 榜排除）；
      · R12：**reader/backbone**（否则"记忆的贡献"与"模型的贡献"混在一起）。

    本函数是这四项的**单一来源**：产物 `out` 直接 update 它 ⇒ SCORES.json 登记时四项有真值可填，
    不必再靠"未记录"占位（上一轮 17 条登记全是未记录，正是因为产物里没有这些数据）。

    诚实约束：**None 必须带说明**（未跑就是未跑），与 scores_gate 的 caliber:no_silent_unknown 同一条纪律。
    """
    proto = "full-haystack" if args.dataset else "oracle"
    return {
        "protocol": proto,
        "dataset_path": (os.path.basename(str(args.dataset)) if args.dataset else "(oracle 默认变体)"),
        "reader_model": getattr(args, "model", None),
        "judge_model": ((str(getattr(args, "judge_model", "") or "").strip()
                         or getattr(args, "model", None))
                        if getattr(args, "answer", False) else None),
        "judge_independent": bool(str(getattr(args, "judge_model", "") or "").strip()
                                  and str(getattr(args, "judge_model", "")).strip()
                                  != str(getattr(args, "model", "") or "").strip()),
        "judge_note": (
            "judge 与 reader 为**同一模型**（自评口径）——跨 judge 家族不可比："
            "AgentMemory 96.2% 即因用 GPT-4o judge 被同-judge 榜排除（BP-4/R3）；"
            "本仓历史上 0.560/0.644 两个数就是不同 judge 口径下的产物。"
            if getattr(args, "answer", False) else "未跑 --answer ⇒ 无 judge（本产物只有检索指标）"),
        "retrieval": getattr(args, "retrieval", None),
        "retrieval_top_k": getattr(args, "top_k", None),
        # 检索预算的**实测上界**：喂给 reader 的 prompt 总字符（含指令模板）。
        # 口径声明：这是"预算上界"而非"纯上下文长度"——指令模板占比固定，可用于跨 run 比较。
        "qa_prompt_chars_total": int(qa_chars or 0),
        "qa_prompt_chars_mean": (round(float(qa_chars) / qa_n, 1) if qa_n else None),
        "full_context_baseline": (None if full_context_baseline is None
                                  else round(float(full_context_baseline), 4)),
        "full_context_baseline_note": (
            "本轮未跑 full-context 对照 ⇒ Δ = Score(MAG) − Score(FullContext) "
            "不可算（BP-2）；字段先落盘、值待补，禁止用 0 冒充"
            if full_context_baseline is None else
            "基线来自 %s（口径：整段 haystack 直灌、无检索；n=%s）⇒ Δ = 本臂 − 基线，见 delta_vs_full_context"
            % (str(full_context_baseline_src or "(未注明)"), str(full_context_baseline_n or "?"))),
        "full_context_baseline_src": full_context_baseline_src,
        "full_context_baseline_n": full_context_baseline_n,
    }


# ---------------------------------------------------------------- full-context 基线臂（2026-09-24 §1326）
# 动机（BP-2）：**「记忆系统」必须先证明基准真的用到了记忆** ——
# Δ = Score(MAG) − Score(FullContext)，只报前者等于没证明。本仓 SCORES.json 里 17 条登记的
# `full_context_baseline` 长期是「未记录」，因为 harness 从来没有「整段 haystack 直灌」这一档
# （`--retrieval` 只有 adapter|hybrid|both）。本臂补的就是这一档 —— 不新增模块，
# 复用同一 reader 提示构建器、同一 judge、同一 caliber 块，**唯一的差别是上下文来源**。

FULLCTX_ITEM_CHARS = 1200          # 单会话截断上限（可 --fullctx-item-chars 覆盖）
FULLCTX_MAX_CHARS = 150000         # 整题预算上限（可 --fullctx-max-chars 覆盖）

#: 2026-10-07（t92/B8 · D2）：**截断告警阈值** = 被截断会话数 / 会话总数。
#: 为什么需要它：本文件的 `caliber_warning` 只在"预算丢弃"时发出，而实测存在**第二种损失**
#: ——`item_chars` 把每个会话**截短**（36 题子集：dropped=0，但 1671/1724 个会话被截断，
#: 原文 17,665,102 → 2,037,984 字符，**丢 88.5%**）⇒ `caliber_warning is None` 会被读成
#: "这一档是完整 full-context"，那是**误导的绿灯**。取 0.5：本口径 0.9693 必响；
#: 而"真·完整直灌"（0 截断）与"轻微截断"不响（防恒响）。
FULLCTX_TRUNCATION_WARN_RATIO = 0.5


def full_context_items(q, item_chars=FULLCTX_ITEM_CHARS, max_total_chars=FULLCTX_MAX_CHARS):
    """把一道题的**整段 haystack** 转成 `results` 形状的列表（供 build_qa_prompt 复用）。

    返回 `(items, meta)`。**丢弃必须显式报数**（本仓纪律：剔除要报数，不许静默丢数据）：
    `meta` 里给出 sessions_total / sessions_used / truncated_sessions / chars。
    """
    sessions = q.get("haystack_sessions") or []
    dates = q.get("haystack_dates") or []
    sids = q.get("haystack_session_ids") or []
    items, total, used, cut = [], 0, 0, 0
    for i, sess in enumerate(sessions):
        turns = []
        for t in (sess or []):
            if isinstance(t, dict):
                role = str(t.get("role") or "")
                txt = str(t.get("content") or "")
                turns.append(("%s: %s" % (role, txt)) if role else txt)
            else:
                turns.append(str(t))
        body = "\n".join(turns)
        head = ""
        if i < len(dates):
            head += "[%s] " % str(dates[i])[:24]
        if i < len(sids):
            head += "[session:%s] " % str(sids[i])[:18]
        content = head + body
        if len(content) > item_chars:
            content = content[:item_chars]
            cut += 1
        if total + len(content) > max_total_chars:      # 预算到顶：整会话跳过（不半截插入）
            break
        items.append({"content": content,
                      "session_id": (str(sids[i]) if i < len(sids) else ""),
                      "date": (str(dates[i]) if i < len(dates) else "")})
        total += len(content)
        used += 1
    meta = {"sessions_total": len(sessions), "sessions_used": used,
            "sessions_dropped_by_budget": max(0, len(sessions) - used),
            "sessions_truncated_by_item_chars": cut,
            "context_chars": total, "item_chars": item_chars, "max_total_chars": max_total_chars}
    return items, meta


def fullctx_caliber_warnings(dropped: int, truncated: int, sessions_total: int,
                             item_chars: int = FULLCTX_ITEM_CHARS,
                             ratio_threshold: float = FULLCTX_TRUNCATION_WARN_RATIO) -> list:
    """**两种损失一起表达**（t92/B8 · D2；措辞沿用本文件既有风格）。

    损失的来源有**两个**，护栏必须都拦：
      ① 预算丢弃（`dropped>0`）—— 本文件原有的那条，措辞**逐字保留**；
      ② **单会话截断**（`truncated/sessions_total > ratio_threshold`）—— 本次新增。
    `dropped` 的语义**一个字不改**；返回 list[str]：0/1/2 条，
    **真·完整直灌（两者都为 0）⇒ 空列表**（防"恒响"）。
    """
    warns = []
    if dropped:
        warns.append("预算砍掉了 %d 个会话 ⇒ 这不是完整 full-context，Δ 只能当**下界**" % dropped)
    ratio = (truncated / float(sessions_total)) if sessions_total else 0.0
    if truncated and ratio > ratio_threshold:
        warns.append("单会话上限 %d 字符截断了 %d/%d 个会话（%.4f）⇒ 这不是完整 full-context，"
                     "Δ 只能当**下界**" % (item_chars, truncated, sessions_total, round(ratio, 4)))
    return warns


def _run_full_context(args, qs, llm) -> int:
    """full-context 基线臂：不检索，整段 haystack 直灌，其余（提示/judge/口径）与检索臂一致。"""
    from collections import OrderedDict
    # 2026-09-27：判分模型与 reader 解耦（空 = 跟随 reader ⇒ 行为不变）
    judge_llm = resolve_judge_llm(args, llm)
    stats: "OrderedDict[str, dict]" = OrderedDict()
    t0 = time.time()
    acc_total = 0
    n = 0
    chars_total = 0
    metas = []
    for q in qs:
        qtype = q.get("question_type", "?")
        items, meta = full_context_items(q, item_chars=args.fullctx_item_chars,
                                         max_total_chars=args.fullctx_max_chars)
        metas.append(meta)
        sys_p, prompt = build_qa_prompt(qtype, q.get("question", ""), items, args.strategy,
                                        cap=len(items), qdate=q.get("question_date"),
                                        item_chars=args.fullctx_item_chars)
        chars_total += len(prompt)
        ans = ""
        try:
            ans = str(llm(sys_p, prompt) or "")
        except Exception as e:  # noqa: BLE001
            print("[WARN] llm 失败（该题计错）：%s" % str(e)[:80])
        ok = judge(judge_llm or llm, q.get("question", ""), q.get("answer", ""), ans) if args.answer else None
        st = stats.setdefault(qtype, {"total": 0, "acc": 0})
        st["total"] += 1
        n += 1
        if ok:
            st["acc"] += 1
            acc_total += 1
    stats = {k: dict(v, **{"acc_rate": round(v["acc"] / v["total"], 4) if v["total"] else None})
             for k, v in stats.items()}
    acc_rate = round(acc_total / n, 4) if n else None
    _t92_dropped = sum(m["sessions_dropped_by_budget"] for m in metas)
    _t92_truncated = sum(m["sessions_truncated_by_item_chars"] for m in metas)
    _t92_sess_total = sum(m["sessions_total"] for m in metas)
    _t92_warns = fullctx_caliber_warnings(_t92_dropped, _t92_truncated, _t92_sess_total,
                                          item_chars=args.fullctx_item_chars)
    out = {
        "arm": "full-context",
        "note": ("**整段 haystack 直灌、无检索**（BP-2 基线臂）。n=%d；"
                 "claimable=false（本仓口径：n>=30 且协议为 full-haystack 才可 claim）" % n),
        "n": n, "answer_acc": acc_rate, "by_type": stats,
        "full_context": {
            "item_chars": args.fullctx_item_chars, "max_total_chars": args.fullctx_max_chars,
            "sessions_total": sum(m["sessions_total"] for m in metas),
            "sessions_used": sum(m["sessions_used"] for m in metas),
            "sessions_dropped_by_budget": sum(m["sessions_dropped_by_budget"] for m in metas),
            "sessions_truncated_by_item_chars": sum(m["sessions_truncated_by_item_chars"] for m in metas),
        },
        "strategy": args.strategy, "model": args.model, "elapsed_s": round(time.time() - t0, 1),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        # 基线臂自带下限校验：整段直灌若被预算砍掉大量会话，Δ 就不是"无检索"的公平对照
        "caliber_warning": ("预算砍掉了 %d 个会话 ⇒ 这不是完整 full-context，Δ 只能当**下界**"
                            % sum(m["sessions_dropped_by_budget"] for m in metas)
                            if sum(m["sessions_dropped_by_budget"] for m in metas) else None),
        # 2026-10-07（t92/B8 · D2）**只加不改**：第二个损失来源（**单会话截断**）也进护栏。
        # 既有 `caliber_warning` 的语义与措辞**逐字保留**（老消费者不受影响）；
        # 需要"两种损失一起看"的消费者读 `caliber_warnings`（列表）。
        "full_context_loss": {
            "sessions_dropped_by_budget": _t92_dropped,
            "sessions_truncated_by_item_chars": _t92_truncated,
            "truncated_ratio": (round(_t92_truncated / float(_t92_sess_total), 4)
                                if _t92_sess_total else 0.0),
            "truncation_warn_ratio": FULLCTX_TRUNCATION_WARN_RATIO,
        },
        "caliber_warning_truncation": (_t92_warns[1] if len(_t92_warns) > 1 else None),
        "caliber_warnings": _t92_warns or None,
    }
    out.update(caliber_block(args, chars_total, n))
    out["full_context_baseline"] = acc_rate          # 基线臂自身即基线
    out["full_context_baseline_note"] = "本产物就是 full-context 基线臂（n=%d）" % n
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("FULLCTX n=%d AnswerAcc=%s -> %s" % (n, acc_rate, args.out))
    if out["caliber_warning"]:
        print("[WARN] " + out["caliber_warning"])
    for _t92_w in (out.get("caliber_warnings") or [])[1:]:
        print("[WARN] " + _t92_w)
    return 0


#: 2026-09-20（§917.5）：隔离库目录的**回收开关**。实测 %TEMP%/lme_official_* 累积
#: 2,489 个目录 / 14.1 GB（09-16 起）—— 每题建一个 tempdir 却从不删除。
KEEP_TMP_ENV = "TRINITY_EVAL_KEEP_TMP"
TMP_PREFIX = "lme_official_"


def sweep_stale_tmp(root: str = None, max_age_h: float = 24.0, prefix: str = TMP_PREFIX) -> int:
    """回收**上一次运行**遗留的隔离库目录，返回删除个数（永不抛异常）。

    安全设计（三条都必须有，否则这就是个"删库脚本"）：
      ① **按 mtime 老化**（默认 **24h**：500 题全量约 5h，留足余量，绝不误伤在跑的运行）；
      ② `max_age_h <= 0` **拒绝执行**（等价于"删光"，不允许）；
      ③ `TRINITY_EVAL_KEEP_TMP=1` 时一个都不删（排障需要现场时用）。
    """
    try:
        if str(os.environ.get(KEEP_TMP_ENV, "") or "").strip().lower() in ("1", "on", "true", "yes"):
            return 0
        if max_age_h is None or float(max_age_h) <= 0:
            return 0
        base = root or tempfile.gettempdir()
        if not base or not os.path.isdir(base):
            return 0
        cutoff = time.time() - float(max_age_h) * 3600.0
        n = 0
        for name in os.listdir(base):
            if not name.startswith(prefix):
                continue
            p = os.path.join(base, name)
            try:
                if not os.path.isdir(p) or os.path.getmtime(p) > cutoff:
                    continue
                shutil.rmtree(p, ignore_errors=True)
                if not os.path.exists(p):
                    n += 1
            except Exception:  # noqa: BLE001
                continue
        return n
    except Exception:  # noqa: BLE001
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--answer", action="store_true", help="同时跑 LLM 答案生成 + judge")
    ap.add_argument("--model", default="deepseek-chat")
    # 2026-09-27：**跨家族判分**接线（BP-4/R3）。空 = 跟随 reader（自评口径，caliber 会标注）。
    # 实测动机：此前 judge 与 reader 恒为同一对象 ⇒ 0.612 这个数无法与公开榜（多为 GPT-4o judge）比较，
    # 而「换 judge 家族」正是本仓自己写在 caliber_note 里的头号口径缺口。
    ap.add_argument("--judge-model", default="",
                    help="判分模型；空=跟随 --model（自评口径）。跨家族判分请显式指定，"
                         "并用 --judge-base-url / TRINITY_JUDGE_LLM_API_KEY 提供另一家端点与密钥")
    ap.add_argument("--judge-base-url", default="",
                    help="判分模型 base_url；空=TRINITY_JUDGE_LLM_BASE_URL → TRINITY_LLM_BASE_URL → deepseek 默认")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--strategy", default="base", choices=["base", "routed"],
                    help="生成策略：base=官方原提示（锁定口径 0.560）；routed=按题型路由 A/B 验证策略（EXECUTION 460）")
    ap.add_argument("--out", default=os.path.join(ROOT, "output", "official_lmeval_results.json"))
    # 2026-09-11（第三轮审计建议 P0-2）：**统一评测口径**——原实现把 DSET 硬编码为
    # oracle 变体（haystack 只含答案会话），得到 R@k=1.000 这类**不可与公开榜单比较**的
    # 数字。新增 --dataset 后可指向官方全 haystack 变体
    # （benchmark/data/longmemeval_s_cleaned.json，277MB，每问 48-62 个干扰会话），
    # 与公开方案（Mem0 94.4% / Hindsight 91.4% QA）同口径。默认值不变（口径锁定）。
    ap.add_argument("--types", default="", help="按题型过滤（逗号分隔），用于单题型 A/B；例：single-session-preference")
    ap.add_argument("--per-type", type=int, default=0,
                    help="按 question_type 均衡采样：每类取前 K 题（0=关闭，走 --limit 前缀截断）")
    ap.add_argument("--dataset", default="",
                    help="数据集路径；空=默认 oracle 变体（口径锁定）；"
                         "传 benchmark/data/longmemeval_s_cleaned.json = 官方全 haystack 设置")
    # 2026-09-11（V2 评估修复）：**双路径对照**。此前本 runner 只走
    # ad.search_memories（适配器/FTS），从不经过生产 search_hybrid —— 于是「大脑化」
    # 那一层（调制/分层先验/辅助通道/精度分档/重排）从未被这个唯一与公开可比的评测
    # 测量过（EXECUTION 702/254）。两条路径跑在**同一个隔离 SQLite 池**上，可直接比较，
    # 且不连 PG、无基准污染风险。
    #   adapter = 保持锁定口径（默认，行为完全不变）
    #   hybrid  = 走生产 search_hybrid
    #   both    = 两条都跑并记录 top-10 重合度（判断"大脑层是否改变结果"）
    ap.add_argument("--retrieval", default="adapter",
                    choices=["adapter", "hybrid", "both", "fullcontext"],
                    help="检索路径：adapter=适配器/FTS（默认）；hybrid=生产 search_hybrid；both=双路径对照")
    # 2026-09-14（P0-1 利用层）：上下文装配器 A/B（默认 off，锁定口径不变）。
    ap.add_argument("--assembler", default="off", choices=["off", "auto", "on"],
                    help="上下文装配器：off=关闭（默认）；auto=按时序/聚合/偏好意图触发；"
                         "on=确定性算子全开（时间线+区间表+计数+偏好），作为额外上下文块追加")
    # 2026-09-19（评测修复②）：**离线 replay**——固定证据、只换生成。
    # 动机：R@10=0.958 但 AnswerAcc=0.612，35 点缺口在「证据→答案」；而此前逐题明细不存上下文，
    # 每次归因都要重跑一次昂贵评测 ⇒ 提示变量与模型变量永远混在一起。
    ap.add_argument("--replay", default="",
                    help="离线 replay：读逐题明细 jsonl（需含 ctx_items 证据），同证据重建提示后"
                         "只换生成（--strategy / --model）再判分；不跑检索")
    # 2026-09-24（§1326 口径闭环）：full-context 基线臂的两个预算旋钮 + 基线引用。
    ap.add_argument("--fullctx-item-chars", type=int, default=FULLCTX_ITEM_CHARS,
                    help="full-context 臂的单会话截断上限（默认 %d）" % FULLCTX_ITEM_CHARS)
    ap.add_argument("--fullctx-max-chars", type=int, default=FULLCTX_MAX_CHARS,
                    help="full-context 臂的整题预算上限（默认 %d；超出部分整会话跳过并报数）"
                         % FULLCTX_MAX_CHARS)
    ap.add_argument("--full-context-baseline", default="",
                    help="检索臂引用一份 full-context 基线产物（json）⇒ 本产物写入 "
                         "full_context_baseline 与 delta_vs_full_context（BP-2 的 Δ 从此可算）")
    args = ap.parse_args()

    if args.replay:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        return _run_replay(args)

    # 2026-09-14（P0-1 利用层）：**上下文装配器** A/B 开关。
    # off（默认，锁定口径不变）/ auto（按时序·聚合·偏好意图触发）/ on（三算子全开）。
    # 装配器只输出**确定性旁路块**（时间线·区间表·计数·偏好），由本 harness 追加到提示词，
    # **不占 top-k、不改 results 顺序** ⇒ R@k 口径完全不受影响，可比性成立。
    os.environ["TRINITY_CONTEXT_ASSEMBLER"] = str(args.assembler)

    _dset_path = args.dataset or DSET
    data = json.load(open(_dset_path, encoding="utf-8"))
    qs = data if isinstance(data, list) else (data.get("questions") or data.get("data") or [])
    # 2026-09-11（建议 P0-2 配套）：官方文件按 question_type **成组排序**，直接 --limit N
    # 只会取到最前面 1-2 类（实测前 3 题全是 temporal-reasoning）⇒ 子集偏斜。
    # --per-type K 改为每类取前 K 题（题型均衡、可对外声明口径）。
    # 2026-09-13（W2/W3 验收）：--types 按题型过滤，便于**单题型 A/B**（不必跑满 120 题）
    if getattr(args, "types", ""):
        _want = {_t.strip() for _t in str(args.types).split(",") if _t.strip()}
        qs = [_q for _q in qs if _q.get("question_type") in _want]
    if args.per_type:
        _by: dict = {}
        for _q in qs:
            _t = _q.get("question_type", "?")
            _by.setdefault(_t, []).append(_q)
        _keep_ids = {id(_x) for _v in _by.values() for _x in _v[: args.per_type]}
        qs = [_q for _q in qs if id(_q) in _keep_ids]
    else:
        qs = qs[: args.limit]
    print("official LongMemEval: %d questions, model=%s%s" % (len(qs), args.model, " (+answer)" if args.answer else ""))

    # 2026-09-14（699）**自捕缺陷**：原写法 llm 仅在 --answer 时初始化 ⇒ 695 的 LLM 重排探针
    # 与 698 的 HyDE 探针调用的是 None（异常被吞），**等于没开**。现按"是否需要 LLM"初始化。
    _probe_llm = (os.environ.get("TRINITY_EVAL_HYDE", "").lower() in ("1", "on", "true")
                  or os.environ.get("TRINITY_EVAL_LLM_RERANK", "").lower() in ("1", "on", "true"))
    llm = _llm_callable(args.model) if (args.answer or _probe_llm) else None
    judge_llm = resolve_judge_llm(args, llm) if llm is not None else None

    # 2026-09-24（§1326）：full-context 基线臂**在此分派**，走独立函数、完全不碰检索路径
    # （默认臂的代码路径一个字节都不变 ⇒ 无回归面）。
    if args.retrieval == "fullcontext":
        if llm is None:
            print("[FAIL] full-context 臂必须带 --answer（否则没有 reader/judge 可跑）")
            return 2
        return _run_full_context(args, qs, llm)

    from trinity.adapters.sqlite import SQLiteAdapter

    stats = {}
    t0 = time.time()
    r_total = {k: 0 for k in (1, 3, 5, 10)}
    acc_total = 0
    _detail: list = []   # 逐题明细（2026-09-13 新增：错误分析不再需要重跑评测）
    tok_in = tok_out = 0
    # 2026-09-19（口径接线①）：检索预算实测累计（喂给 reader 的 prompt 字符数）
    _qa_chars = 0
    _qa_n = 0
    n = 0

    _swept = sweep_stale_tmp()
    if _swept:
        print("[tmp] 回收上次运行遗留的隔离库目录：%d 个（TRINITY_EVAL_KEEP_TMP=1 可保留现场）" % _swept)
    for qi, q in enumerate(qs):
        question = q.get("question", "")
        gold = q.get("answer", "")
        qtype = q.get("question_type", "?")
        ans_sessions = set(q.get("answer_session_ids") or [])
        sessions = q.get("haystack_sessions") or []
        # 2026-09-13：把**基准题自带的 question_date** 传给检索引擎（环境变量）。
        # 动机：temporal_context 的相对天数此前一直用「今天」当参照 ⇒ 2023 年的语料被标成
        # −1200 天（实测 −1212），既无用又误导。基准题自带该字段，传下去即可。
        _qd = q.get("question_date") or ""
        if _qd:
            os.environ["TRINITY_QUESTION_DATE"] = str(_qd)
        else:
            os.environ.pop("TRINITY_QUESTION_DATE", None)
        st = stats.setdefault(qtype, {"total": 0, "r1": 0, "r5": 0, "r10": 0, "acc": 0})
        st["total"] += 1
        n += 1
        # 2026-09-14（698）**查询侧扩展探针**（HyDE 式，默认关）：TRINITY_EVAL_HYDE=1。
        # 动机（697 实测）：SS-P 题的 top-5 与被问主题**基本无关**（探针打印：问"摄影配件"，
        # 召回的是"蜡烛照片布景/Python 报错/藜麦早餐/麦凯恩/数据科学"）——题干本身信息太少，
        # 相似度检索无从下手；排序侧三种改造已被门禁拒绝 ⇒ 只能从**查询侧**补上下文。
        # 做法：让模型写一句"用户此前可能说过的、含其背景/偏好的一句话"，与题干拼接后再检索。
        _hyde = ""
        if os.environ.get("TRINITY_EVAL_HYDE", "").lower() in ("1", "on", "true"):
            try:
                _hyde = str(llm(
                    "You expand under-specified user requests into one first-person sentence.",
                    ("A user asks: " + str(question)[:300] + chr(10)
                     + "Write ONE short first-person sentence that this user might have said earlier, "
                     + "describing their own context, gear, habits or preferences relevant to the "
                     + "request (e.g. what they use, what they tried, what they like). "
                     + "Output the sentence only.")) or "").strip()
                if _hyde:
                    question = str(question) + " " + _hyde[:300]
                    st["hyde_questions"] = st.get("hyde_questions", 0) + 1
            except Exception:  # noqa: BLE001
                st["hyde_errors"] = st.get("hyde_errors", 0) + 1

        tmpdir = tempfile.mkdtemp(prefix="lme_official_")
        db = os.path.join(tmpdir, "store.db")
        ad = SQLiteAdapter(db_path=db)
        ad.connect()
        # 2026-09-11：生产检索路径客户端（惰性构造，指向同一隔离池；不连 PG）
        _hyb = None
        if args.retrieval in ("hybrid", "both"):
            try:
                from trinity.core.client import Trinity as _T

                _hyb = _T(store_path=db, adapter="sqlite")
            except Exception as _hexc:  # noqa: BLE001
                print("[warn] hybrid 客户端构造失败，回退 adapter 路径: " + str(_hexc)[:120])
                _hyb = None
        try:
            # 会话 id 用官方 haystack_session_ids（与 answer_session_ids 对齐）
            sid_list = q.get("haystack_session_ids") or []
            # 2026-09-13（TR 根因修复）：**必须把会话日期带进去**。
            # 实测：此前 ingest 不带任何时间字段 ⇒ 所有记忆的 created_at = 摄取时刻（今天），
            # 于是日期标注/时间线/排序一律认为「一切都发生在今天」，
            # 模型输出「0 days ago」「Both events happened on 2026-09-13」。
            # 而数据集自带 haystack_dates（形如 '2023/05/20 (Sat) 02:21'）却从未被使用。
            _dates = q.get("haystack_dates") or []
            records = []
            for idx, msgs in enumerate(sessions):
                real_sid = str(sid_list[idx]) if idx < len(sid_list) else "sess_%d" % idx
                _sd = str(_dates[idx]) if idx < len(_dates) else ""
                _sd_iso = ""
                if _sd:
                    _m = re.match(r"(\d{4})[/-](\d{2})[/-](\d{2})", _sd)
                    if _m:
                        _sd_iso = "%s-%s-%s" % (_m.group(1), _m.group(2), _m.group(3))
                for m in msgs:
                    content = str(m.get("content") or "") if isinstance(m, dict) else str(m)
                    if not content.strip():
                        continue
                    # 2026-09-14（750）**把会话日期写进上下文**（TRINITY_EVAL_DATE_IN_CONTENT，默认 off）：
                    # 根因实测（749）：TR full-haystack R@1 26/30、R@10 29/30，但 **correct 仅 1/30**，
                    # 28 条失败的回答全是 "Insufficient information / context does not provide dates"
                    # —— 即**检索到了证据，但证据块里没有日期**。数据集自带 haystack_dates，此前只写进
                    # metadata（模型看不到）。本开关把 `[YYYY-MM-DD] ` 前缀并入正文，模型即可直接读到。
                    _date_ok = os.environ.get("TRINITY_EVAL_DATE_IN_CONTENT", "off").lower() in ("1", "on", "true", "yes")
                    _c = ("[%s] %s" % (_sd_iso, content)) if (_date_ok and _sd_iso) else content
                    records.append({
                        "content": _c[:2000],
                        "persona_id": "u1",
                        "session_id": real_sid,
                        "agent_id": "u1",
                        "role": "user" if isinstance(m, dict) and m.get("role") == "user" else "assistant",
                        "importance": 0.5,
                        "tags": ["lme_official"],
                        "metadata": {"session_date": _sd_iso} if _sd_iso else None,
                    })
            try:
                ad.ingest_batch(records)
            except Exception as _e:
                for rec in records:
                    try:
                        ad.store_memory(**rec)
                    except Exception as _e:
                        swallow(__name__, _e)
            # EXECUTION 462/463: routed 按类目检索配置（MS 20 起）；Recall 口径不变——
            # R@k 统计仍按 args.top_k 截断
            _rcfg = _ROUTE.get(qtype, {}) if args.strategy == "routed" else {}
            _search_k = max(args.top_k, _rcfg.get("top_k", args.top_k))
            # 2026-09-11：检索路径分派（默认 adapter → 口径与行为完全不变）
            results = ad.search_memories(query=question, top_k=_search_k)
            # 2026-09-14（713）修复：原实现只在 hybrid/both 分支里给 _hres 赋值，
            # 于是 --retrieval adapter --answer 会在旁路字段块（第 477 行 isinstance(_hres…)）
            # 抛 UnboundLocalError —— 即 claim_gate 要求的「朴素基线臂（adapter+答案）」根本无法测量。
            # 实测：MS n=60 的 FTS 基线臂跑到第 1 题即崩（exit 1）。
            _hres = None
            _hyb_scored = []  # both 模式下 hybrid 的并列结果（不覆盖主口径）
            if _hyb is not None:
                try:
                    _hres = _hyb.search_hybrid(query=question, top_k=_search_k)
                    _hlist = (_hres.get("results", []) if isinstance(_hres, dict)
                              else list(_hres or []))
                    if args.retrieval == "both":
                        _a_ids = {r.get("memory_id") for r in results[:10]}
                        _h_ids = {r.get("memory_id") for r in _hlist[:10]}
                        st["dual_path_questions"] = st.get("dual_path_questions", 0) + 1
                        st["dual_path_overlap_sum"] = (
                            st.get("dual_path_overlap_sum", 0) + len(_a_ids & _h_ids))
                        st["dual_path_hybrid_nonempty"] = (
                            st.get("dual_path_hybrid_nonempty", 0) + (1 if _hlist else 0))
                    if args.retrieval == "hybrid":
                        results = _hlist
                    else:
                        # both：**保持 adapter 为头条口径**（否则 R@k 会被悄悄换成
                        # hybrid 口径，与历史数字不可比）；hybrid 单独并列统计
                        _hyb_scored = _hlist
                except Exception as _exc:  # noqa: BLE001
                    st["dual_path_errors"] = st.get("dual_path_errors", 0) + 1
                    print("[warn] hybrid 检索失败（保留 adapter 结果）: " + str(_exc)[:120])
            # 2026-09-14（695）**LLM 重排探针**（实验，默认关）：回答"融合排序器是不是瓶颈"。
            # 背景：SS-P 实测 R@1 0.300 / R@10 0.733 / R@50 0.967，两种纯排序改造（乘性共识、
            # 会话聚合）经 A/B 均**未改变任何题目的 rank-1** ⇒ 只能问"若让模型重排能到多少"。
            # 只在评测脚本内生效，不改生产路径。TRINITY_EVAL_LLM_RERANK=1 开启。
            if (os.environ.get("TRINITY_EVAL_LLM_RERANK", "").lower() in ("1", "on", "true")
                    and results):
                try:
                    _cand = list(results[:20])
                    _lines = ["[%d] %s" % (_i, str((_r or {}).get("content")
                                           or (_r or {}).get("content_preview") or "")[:220])
                              for _i, _r in enumerate(_cand)]
                    _p = ("Question: " + str(question)[:300] + chr(10) + chr(10)
                          + "Candidate memories:" + chr(10) + chr(10).join(_lines) + chr(10) + chr(10)
                          + "Return ONLY a JSON array of the indices most likely to come from the "
                          + "session holding the evidence needed to answer the question, best first. "
                          + "At most 10 indices.")
                    _o = llm("You are a retrieval reranker.", _p)
                    _m = re.search(r"\[[^\]]*\]", str(_o) or "")
                    if _m:
                        _idx = json.loads(_m.group(0))
                        _seen, _new = set(), []
                        for _i in _idx:
                            if isinstance(_i, int) and 0 <= _i < len(_cand) and _i not in _seen:
                                _seen.add(_i)
                                _new.append(_cand[_i])
                        for _i, _r in enumerate(_cand):
                            if _i not in _seen:
                                _new.append(_r)
                        results = _new + list(results[20:])
                        st["llm_rerank_questions"] = st.get("llm_rerank_questions", 0) + 1
                except Exception as _e:  # noqa: BLE001
                    st["llm_rerank_errors"] = st.get("llm_rerank_errors", 0) + 1
            hit_sessions = {r.get("session_id") for r in results}
            # 2026-09-14（742 诊断）：TRINITY_EVAL_DEBUG=1 时打印逐题 top-1 会话 vs gold ——
            # 动机：今天 harness 读 SS-P R@1=1.000，而手工复刻同口径只有 1/3 ⇒ 必须先证伪测量。
            if os.environ.get("TRINITY_EVAL_DEBUG", "").lower() in ("1", "on", "true"):
                print("[dbg] q=%s gold=%s top1=%s in_ans=%s n_res=%d qtext=%r"
                      % (str(q.get("question_id"))[:16], sorted(ans_sessions)[:1],
                         str((results[0] or {}).get("session_id"))[:16] if results else None,
                         bool(results) and ((results[0] or {}).get("session_id") in ans_sessions),
                         len(results), str(question)[:60]))
                for _i, _r in enumerate(results[:3]):
                    print("      [%d] sid=%s content=%r" % (_i, str((_r or {}).get("session_id"))[:14],
                                                             str((_r or {}).get("content"))[:50]))
            for k in (1, 3, 5, 10):
                top = results[:k]
                if any(r.get("session_id") in ans_sessions for r in top):
                    r_total[k] += 1
                    if k in (1, 5, 10):
                        st["r%d" % k] += 1
            # 2026-09-14（693）：**逐题真值**（此前明细里写的是 bool(累计计数器)，
            # 一旦该类题有过一次命中，之后每行都是 True —— 明细因此无法用于逐题诊断）。
            _r1 = any(r.get("session_id") in ans_sessions for r in results[:1])
            _r5 = any(r.get("session_id") in ans_sessions for r in results[:5])
            _r10 = any(r.get("session_id") in ans_sessions for r in results[:10])
            # 2026-09-11 双路径对照：hybrid 侧同口径独立计分（both 模式）
            if _hyb_scored:
                for k in (1, 3, 5, 10):
                    top = _hyb_scored[:k]
                    if any(r.get("session_id") in ans_sessions for r in top):
                        st["hybrid_r%d" % k] = st.get("hybrid_r%d" % k, 0) + 1
            # AnswerAcc（EXECUTION 460: 策略路由；base 保持锁定口径；
            # EXECUTION 462/463: 上下文深度按类目 cap）
            if args.answer:
                _ctx_results = results
                # 2026-09-13（W1/W2/W3 旁路字段）：语料级增强（偏好卡/时间线/会话摘要）
                # **不占用 top-k 名额** —— 作为额外上下文块追加，R@k 口径完全不受影响。
                _extra = []
                if isinstance(_hres, dict):
                    if _hres.get("preference_card"):
                        _extra.append(str(_hres["preference_card"]))
                    if _hres.get("event_timeline"):
                        _extra.append(str(_hres["event_timeline"]))
                    _st_map = _hres.get("session_tiers") or {}
                    for _sid, _txt in list(_st_map.items())[:3]:
                        _extra.append("[SESSION %s] %s" % (str(_sid)[:12], str(_txt)[:300]))
                    # W3 第二版：真·会话级扩展（会话内下钻、且不在已召回行中的**新**段落）
                    _exp_map = _hres.get("session_expansion") or {}
                    for _sid, _items in list(_exp_map.items())[:3]:
                        for _it in (_items or [])[:2]:
                            _extra.append("[SESSION %s EXTRA] %s"
                                          % (str(_sid)[:12], str(_it.get("content") or "")[:400]))
                    # W3 第三版：覆盖扩展（聚合型问题的额外证据，同样不占 top-k）
                    for _it in (_hres.get("coverage_expansion") or [])[:20]:
                        _extra.append("[COVERAGE EXTRA] %s" % (str(_it.get("content") or "")[:400]))
                # 2026-09-14（P0-1 利用层）：**上下文装配器**。
                # 动机：TR full-haystack Acc 仅 0.0333、MS 0.4167 —— 检索已到顶（R@10 0.917），
                # 缺口在「证据 → 答案」。装配器把可判定的部分（时间线排序、成对区间、
                # 会话/日期计数、偏好归并）用确定性代码算完，作为额外块追加；**不占 top-k**。
                if args.assembler != "off":
                    try:
                        from trinity.retrieval.context_assembler import assemble as _ca_asm
                        _asm = _ca_asm(question, results,
                                       question_date=q.get("question_date"), qtype=qtype)
                        if _asm.get("ops"):
                            _extra.append(str(_asm.get("text") or ""))
                            st["asm_ops"] = st.get("asm_ops", 0) + len(_asm["ops"])
                            st["asm_questions"] = st.get("asm_questions", 0) + 1
                    except Exception:
                        st["asm_errors"] = st.get("asm_errors", 0) + 1
                _cap = _rcfg.get("cap", 5)
                if args.strategy == "routed" and _rcfg.get("mode") == "timeline":
                    _ctx_results = timeline_ctx(results)
                    _cap = len(_ctx_results)
                sys_p, prompt = build_qa_prompt(qtype, question, _ctx_results, args.strategy, cap=_cap,
                                                qdate=q.get("question_date"))
                if _extra:
                    prompt = prompt + "\n\nAdditional aggregated context (not a retrieved document):\n" + "\n".join(_extra)
                ans = ""
                try:
                    ans = llm(sys_p, prompt)
                except Exception:
                    ans = ""
                tok_in += len(prompt)
                tok_out += len(ans)
                _qa_chars += len(prompt)      # 口径接线①：检索预算实测（prompt 字符数）
                _qa_n += 1
                ok = judge(judge_llm or llm, question, gold, ans) if ans.strip() else False
                if ok:
                    acc_total += 1
                    st["acc"] += 1
                # 2026-09-13：**逐题明细落盘**（诊断缺口：原 harness 只存聚合值，
                # 于是每次问「为什么 Acc=0.05」都要**重跑一次昂贵评测**；而错误分析恰恰
                # 是决定下一步做什么的唯一依据）。写成 <out>.detail.jsonl，一行一题。
                try:
                    _detail.append({
                        "type": qtype, "question": str(question)[:300],
                        "gold": str(gold)[:200], "pred": str(ans)[:400],
                        "ok": bool(ok),
                        "r1": bool(_r1), "r5": bool(_r5), "r10": bool(_r10),
                        "top_ids": [str(x.get("session_id") or x.get("id") or "")[:20]
                                     for x in (_ctx_results or [])[:10]],
                        "n_extra": len(_extra),
                        # 2026-09-19（评测修复②）：**缓存证据**，使「只换生成」的离线 replay 成为可能。
                        # 不存证据就只能重跑一次昂贵评测（本仓原文痛点）——那也正是 35 点缺口
                        # 迟迟无法归因的原因：提示变量与模型变量永远混在一起。
                        "ctx_items": [{"content": str(x.get("content") or "")[:800],
                                       "session_id": str(x.get("session_id") or "")[:32]}
                                      for x in (_ctx_results or [])[:12]],
                        "ctx_cap": int(_cap or 0),
                        "extra": [str(_e)[:800] for _e in (_extra or [])][:6],
                        "qdate": str(q.get("question_date") or "")[:24],
                        # replay 时用来还原口径（否则 replay 产物会把 full-haystack 记成 oracle）
                        "protocol": ("full-haystack" if args.dataset else "oracle"),
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })
                except Exception:
                    pass
            else:
                # 2026-09-14（693）：**不跑 LLM 也写逐题明细**（诊断用，零额外成本）。
                # 动机：SS-P 诊断只能靠聚合 R@k 分解（recall 26.7% / rerank 43.3%），
                # 无法回答"哪一个会话压过了 gold"——因为明细只在 --answer 路径下写。
                try:
                    _kcap = int(args.top_k or 10)
                    _detail.append({
                        "type": qtype, "question": str(question)[:300],
                        "gold": str(gold)[:200], "pred": None, "ok": None,
                        "r1": bool(_r1), "r5": bool(_r5), "r10": bool(_r10),
                        "top_ids": [str(x.get("session_id") or x.get("id") or "")[:24]
                                    for x in (results or [])[:max(10, _kcap)]],
                        "n_extra": 0,
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })
                except Exception:
                    pass
        finally:
            ad.disconnect()
        if qi < 2 or (qi + 1) % 50 == 0:
            print("  [%d/%d] %s R@5=%s" % (qi + 1, len(qs), qtype, "Y" if st["r5"] else "N"))

    # 逐题明细写盘（旁路：任何异常都不得影响评测结果）
    try:
        if _detail:
            _dpath = args.out + ".detail.jsonl"
            with open(_dpath, "w", encoding="utf-8") as _dfh:
                for _d in _detail:
                    _dfh.write(json.dumps(_d, ensure_ascii=False) + "\n")
            print("  detail -> %s (%d rows)" % (_dpath, len(_detail)))
    except Exception as _e:
        print("  detail dump FAILED: %s" % str(_e)[:100])

    out = {
        "test": "official_longmemeval",
        "dataset": ("LongMemEval %s (%s)" % (
            "full-haystack" if args.dataset else "oracle", os.path.basename(_dset_path))),
        "questions": n,
        "R@1": round(r_total[1] / n, 4),
        "R@3": round(r_total[3] / n, 4),
        "R@5": round(r_total[5] / n, 4),
        "R@10": round(r_total[10] / n, 4),
        "AnswerAcc": round(acc_total / n, 4) if args.answer else None,
        "strategy": args.strategy,
        "est_cost_usd": round((tok_in / 1e6) * PRICING["input"] + (tok_out / 1e6) * PRICING["output"], 4) if args.answer else 0.0,
        "by_type": {c: {"total": s["total"], "R@1": round(s["r1"] / s["total"], 4),
                        "R@5": round(s["r5"] / s["total"], 4),
                        "R@10": round(s["r10"] / s["total"], 4),
                        "AnswerAcc": round(s["acc"] / s["total"], 4) if args.answer else None}
                   for c, s in sorted(stats.items())},
        # 2026-09-14（699）：探针**运行期计数**入档（否则"探针是否真的生效"无从证明——
        # 695 的 LLM 重排探针就因 llm=None 静默失效，只能靠事后才发现）。
        "probes": {
            "hyde_questions": sum(s.get("hyde_questions", 0) for s in stats.values()),
            "hyde_errors": sum(s.get("hyde_errors", 0) for s in stats.values()),
            "llm_rerank_questions": sum(s.get("llm_rerank_questions", 0) for s in stats.values()),
            "llm_rerank_errors": sum(s.get("llm_rerank_errors", 0) for s in stats.values()),
            # 2026-09-14（P0-1）：装配器运行期计数（证明"真的触发了"，而非静默 no-op）
            "asm_questions": sum(s.get("asm_questions", 0) for s in stats.values()),
            "asm_ops": sum(s.get("asm_ops", 0) for s in stats.values()),
            "asm_errors": sum(s.get("asm_errors", 0) for s in stats.values()),
        },
        "assembler": args.assembler,
        "elapsed_s": round(time.time() - t0, 1),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    }
    # 2026-09-19（口径接线①）：四口径字段入档（reader/judge/预算/full-context 基线），
    # 让 docs/SCORES.json 登记时**有真值可填**，而不是再写"未记录"。
    # 2026-09-24（§1326 口径闭环）：若带 `--full-context-baseline <json>`，把该基线的
    # AnswerAcc 写进本产物并算 Δ = 本臂 − 基线（BP-2）。**读不出来就如实报错，不静默填 0**。
    _fcb = _fcb_src = _fcb_n = None
    _fcb_err = None
    if getattr(args, "full_context_baseline", ""):
        try:
            _b = json.load(open(args.full_context_baseline, encoding="utf-8"))
            _fcb = _b.get("answer_acc")
            _fcb_n = _b.get("n")
            _fcb_src = os.path.basename(args.full_context_baseline)
            if _fcb is None:
                _fcb_err = "基线产物里没有 answer_acc 字段 ⇒ Δ 不可算（不填 0）"
        except Exception as _e:  # noqa: BLE001
            _fcb_err = "基线产物读取失败：%s" % str(_e)[:80]
    out.update(caliber_block(args, _qa_chars, _qa_n,
                             full_context_baseline=_fcb,
                             full_context_baseline_src=(_fcb_src or ""),
                             full_context_baseline_n=_fcb_n))
    if getattr(args, "full_context_baseline", ""):
        # 本臂 AnswerAcc 的键名是 **大写 `AnswerAcc`**（见上方 out 构造）；先核形状再用，别猜。
        _acc = out.get("AnswerAcc")
        if _acc is None:
            _acc = (out.get("summary") or {}).get("AnswerAcc") if isinstance(out.get("summary"), dict) else None
        out["delta_vs_full_context"] = (round(float(_acc) - float(_fcb), 4)
                                        if (_acc is not None and _fcb is not None) else None)
        out["delta_note"] = ("Δ = 本臂 AnswerAcc(%.4f) − full-context 基线(%.4f)（BP-2）"
                             % (float(_acc), float(_fcb)) if (_acc is not None and _fcb is not None)
                             else (_fcb_err or "本臂无 AnswerAcc（未 --answer？）⇒ Δ 不可算"))
    # 2026-09-11：双路径对照聚合。此前本 runner 唯一与公开可比的口径**从不经过
    # 生产 search_hybrid**（EXECUTION 702/254）→「大脑层是否改变检索结果」从未被测过。
    # 这里把 hybrid 侧与 adapter 侧**同口径并列**输出（adapter 仍为头条口径，数字可比）。
    if args.retrieval == "both":
        _dq = sum(s.get("dual_path_questions", 0) for s in stats.values())
        _dov = sum(s.get("dual_path_overlap_sum", 0) for s in stats.values())
        _dne = sum(s.get("dual_path_hybrid_nonempty", 0) for s in stats.values())
        _derr = sum(s.get("dual_path_errors", 0) for s in stats.values())

        def _hr(k):
            return (round(sum(s.get("hybrid_r%d" % k, 0) for s in stats.values()) / _dq, 4)
                    if _dq else None)

        out["dual_path"] = {
            "questions": _dq,
            "hybrid_nonempty": _dne,
            "top10_overlap_avg": round(_dov / _dq, 2) if _dq else None,
            "errors": _derr,
            "hybrid_R@1": _hr(1),
            "hybrid_R@5": _hr(5),
            "hybrid_R@10": _hr(10),
            "note": "adapter=头条口径（与历史数字可比）；hybrid=生产检索路径并列对照",
        }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("=" * 66)
    print("  Official LongMemEval — %d questions" % n)
    print("  R@1=%.4f  R@3=%.4f  R@5=%.4f  R@10=%.4f" % (out["R@1"], out["R@3"], out["R@5"], out["R@10"]))
    if args.answer:
        print("  AnswerAcc=%.4f  cost=$%.3f" % (out["AnswerAcc"], out["est_cost_usd"]))
    for c in sorted(out["by_type"]):
        s = out["by_type"][c]
        print("  %-24s n=%d R@1=%.3f R@5=%.3f%s" % (c, s["total"], s["R@1"], s["R@5"],
              " Acc=%.3f" % s["AnswerAcc"] if args.answer else ""))
    print("=" * 66)
    print("saved -> %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
