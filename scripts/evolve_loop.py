#!/usr/bin/env python3
"""evolve_loop.py — 自进化全闭环编排器（SELF_EVOLUTION_DESIGN 阶段 3）。

编排 SIGNAL → VARIANT → A/B → CERTIFY：
  ① SIGNAL：evolve_signal.py（QA 小集 + 指标 + 数据质量）——快速基线；
  ② VARIANT：基于性能画像 + 历史证伪记录，LLM 提议 1-3 个候选（受限域：
     检索权重/提示词/开关/治理参数）；无 LLM key 时用内置候选清单；
  ③ A/B：evolve_ab.py 逐候选对比（judge3 三票）；
  ④ CERTIFY：显著改进（+≥2pp）→ 采纳（env 持久化到 evolve_env.json，
     由维护链/服务读取）+ 写记忆（trinity_write 决策记录）；
     无改进/退化 → 记录"证伪"到 evolve_falsified.json；
  ⑤ 收敛保护：连续 N 轮无改进 → 降频（daily→weekly→paused）；
     预算上限：每轮最多 VARIANT_AB_MAX（默认 2）个候选。

用法：
    python scripts/evolve_loop.py --n-qa 10 --max-variants 1   # 快速一轮
    python scripts/evolve_loop.py --dry-run                    # 只信号+提议，不 A/B
    python scripts/evolve_loop.py --force                      # 忽略降频（人工触发）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

PY = os.environ.get("TRINITY_PY") or sys.executable
EVOLVE_DIR = os.path.expanduser("~/.trinity/evolve")
STATE_FILE = os.path.join(EVOLVE_DIR, "evolve_state.json")
ENV_FILE = os.path.join(EVOLVE_DIR, "evolve_env.json")     # 已采纳的 env（服务/维护链读取）
FALSIFIED_FILE = os.path.join(EVOLVE_DIR, "evolve_falsified.json")
DATA = r"C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json"

MIN_IMPROVE = 0.02        # 采纳阈值（+2pp）
STREAK_PAUSE = 3          # 连续 N 轮无改进 → 降频
INTERVAL_SECONDS = {"daily": 86400, "weekly": 604800, "paused": -1}

# 受限变异域：内置候选（无 LLM key 时兜底）。
# 2026-08-25（缺口G）：只保留 QA-acc 可测参数（影响答案质量）——
# cache_off 已移除（语义缓存只影响延迟，judge3 acc 测不出差异）。
# 真实 env 变量名须与 trinity/retrieval/hybrid_retriever.py 等 os.environ.get 一致。
# 2026-08-25（缺口J）：只保留经代码验证真实存在的 env 变量。
# TRINITY_RRF_K/TRINITY_TOP_K 已移除（代码中不存在——LLM 编造）。
BUILTIN_VARIANTS = [
    {"id": "ppr_off", "env": {"TRINITY_GRAPH_PPR": "off"}, "desc": "关闭 PPR 图谱通道（对照默认 on，影响图谱证据）"},
    {"id": "imp_on", "env": {"TRINITY_IMPORTANCE_BOOST": "on"}, "desc": "开启 importance 动态加权（默认 off，hybrid 校准重排 ±0.1）"},
    {"id": "str_on", "env": {"TRINITY_STRENGTH_BOOST": "on"}, "desc": "开启双强度因子（默认 off，hybrid 校准重排 ±0.075）"},
    {"id": "routing_off", "env": {"TRINITY_ADAPTIVE_ROUTING": "off"}, "desc": "关闭自适应路由（对照默认 on，短查询走 FTS 轻通道）"},
    {"id": "conf_on", "env": {"TRINITY_CONFIDENCE_SCORER": "on"}, "desc": "开启四维置信度校准（对照默认 off）"},
    {"id": "cache_off", "env": {"TRINITY_CACHE_BACKEND": "off"}, "desc": "关闭语义缓存（对照默认 memory）"},
    # 2026-08-25（可测域扩展）：RRF_K 已验证真实有效（影响融合排序区分度）
    {"id": "rrf_k5", "env": {"TRINITY_RRF_K": "5"}, "desc": "RRF 融合常数 k=5（对照默认 60，融合更尖锐）"},
    {"id": "rrf_k30", "env": {"TRINITY_RRF_K": "30"}, "desc": "RRF 融合常数 k=30（对照默认 60，中等）"},
    {"id": "rrf_k100", "env": {"TRINITY_RRF_K": "100"}, "desc": "RRF 融合常数 k=100（对照默认 60，融合更平滑）"},
    # 2026-08-25（fusion 权重，需 --strategy fusion）：通道权重变体
    {"id": "vec_dom", "env": {"TRINITY_VECTOR_WEIGHT": "0.8", "TRINITY_BM25_WEIGHT": "0.1", "TRINITY_GRAPH_WEIGHT": "0.1"}, "desc": "fusion：向量主导（0.8/0.1/0.1）"},
    {"id": "bm25_dom", "env": {"TRINITY_VECTOR_WEIGHT": "0.1", "TRINITY_BM25_WEIGHT": "0.8", "TRINITY_GRAPH_WEIGHT": "0.1"}, "desc": "fusion：BM25 主导（0.1/0.8/0.1）"},
    {"id": "graph_dom", "env": {"TRINITY_VECTOR_WEIGHT": "0.1", "TRINITY_BM25_WEIGHT": "0.1", "TRINITY_GRAPH_WEIGHT": "0.8"}, "desc": "fusion：图谱主导（0.1/0.1/0.8）"},
    # 2026-08-25（新维度：BM25 参数）——经典 BM25 调参
    {"id": "bm25_k1_05", "env": {"TRINITY_BM25_K1": "0.5"}, "desc": "BM25 k1=0.5（对照默认 1.5，词频饱和更快）"},
    {"id": "bm25_k1_30", "env": {"TRINITY_BM25_K1": "3.0"}, "desc": "BM25 k1=3.0（对照默认 1.5，词频影响更大）"},
    {"id": "bm25_b_03", "env": {"TRINITY_BM25_B": "0.3"}, "desc": "BM25 b=0.3（对照默认 0.75，弱文档长度归一化）"},
]


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"cycles": 0, "adopted": [], "falsified_total": 0, "no_improve_streak": 0,
            "last_run_ts": 0, "interval": "daily"}


def _save_state(state: dict) -> None:
    os.makedirs(EVOLVE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def _load_falsified() -> list:
    if os.path.exists(FALSIFIED_FILE):
        try:
            with open(FALSIFIED_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_falsified(items: list) -> None:
    os.makedirs(EVOLVE_DIR, exist_ok=True)
    with open(FALSIFIED_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def _signal(skip_qa: bool, n_qa: int, data_path: str = "", metric: str = "qa",
          strategy: str = "rrf") -> dict:
    cmd = [PY, os.path.join(REPO, "scripts", "evolve_signal.py"), "--n-qa", str(n_qa)]
    if skip_qa:
        cmd.append("--skip-qa")
    if data_path:
        cmd += ["--data", data_path]
    if metric != "qa":
        cmd += ["--metric", metric]
    if strategy != "rrf":
        cmd += ["--strategy", strategy]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return {"error": r.stderr[-300:] or r.stdout[-300:]}
    # 找最新 signal 文件（2026-08-25 缺口I 修复：按 mtime 取最新，排除
    # signal_records_*/signal_test_* 等非正式 signal——此前 sorted() 字典序
    # 误选 signal_test_baseline.json（n=3 测试文件）导致 qa_n=3）。
    files = [f for f in os.listdir(EVOLVE_DIR)
             if f.startswith("signal_") and not f.startswith("signal_records_")
             and not f.startswith("signal_test_")]
    if not files:
        return {"error": "no signal file"}
    files.sort(key=lambda f: os.path.getmtime(os.path.join(EVOLVE_DIR, f)))
    # 2026-08-25（缺口E）：返回信号文件路径——evolve_ab --baseline-json 复用
    # signal 的 QA 结果（含 expected 后 judge3 可判分），省 base 重跑。
    signal = json.load(open(os.path.join(EVOLVE_DIR, files[-1]), encoding="utf-8"))
    signal["_signal_file"] = os.path.join(EVOLVE_DIR, files[-1])
    return signal


def _propose_variants(signal: dict, falsified: list, max_variants: int,
                     strategy: str = "rrf") -> list:
    """LLM 提议（受限域）→ 过滤证伪历史 → 返回候选清单。

    2026-08-25（缺口B 修复）：LLM 提议优先于内置清单——此前 LLM 提议
    append 在 builtin 之后又被 candidates[:max_variants] 截断，永远不生效；
    且死代码 random.sample(..., min(3,0)) 每次加载 277MB 数据文件。
    """
    falsified_ids = {f.get("id") for f in falsified}
    # 2026-08-25（strategy 匹配）：fusion 权重变体仅 fusion strategy 下可测；
    # rrf 时过滤掉（避免无效测试）。
    _FUSION_IDS = {"vec_dom", "bm25_dom", "graph_dom"}
    builtin = [v for v in BUILTIN_VARIANTS
               if v["id"] not in falsified_ids
               and (strategy == "fusion" or v["id"] not in _FUSION_IDS)]

    # LLM 提议（基于信号画像 + 证伪知识；失败/无 key 用内置清单）
    llm_candidates: list = []
    try:
        cred = open(os.path.expanduser("~/.dsh/.credentials.yaml"), encoding="utf-8-sig").read()
        key = None
        for line in cred.splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY"):
                key = line.split(":", 1)[1].strip().strip('"').strip("'")
                break
        if key:
            # 2026-08-25（缺口G 修复）：可测性约束——A/B 指标是 QA 准确率
            # （judge3），只影响**答案质量**的参数（检索权重/通道开关/提示词/
            # RRF 参数）才可被 acc 验证；只影响延迟/成本/写放大的参数
            # （语义缓存 TTL/backend、合并批大小、冷却时间）**无法**用 acc
            # 验证（缓存不改答案），禁止提议。必须使用**真实存在的 env 变量**
            # （可从 trinity/retrieval/hybrid_retriever.py、trinity/core/cache.py
            # 的 os.environ.get 确认），禁止编造变量名。
            prompt = (
                "你是 Trinity 记忆系统的自进化变异提议器。基于以下性能画像，"
                "提议最多 %d 个**参数级候选优化**。\n"
                "**可测性硬约束**（A/B 指标 = QA 准确率，judge3 判分）：\n"
                "1. 只能提议影响**答案质量/检索质量**的参数；\n"
                "2. **必须从以下已验证存在的 env 变量中选择**（禁止编造其他变量名）：\n"
                "   - TRINITY_GRAPH_PPR（图谱 PPR 通道，默认 on，可 off）\n"
                "   - TRINITY_ADAPTIVE_ROUTING（自适应路由，默认 on，可 off）\n"
                "   - TRINITY_CONFIDENCE_SCORER（置信度校准，默认 off，可 on）\n"
                "   - TRINITY_CACHE_BACKEND（语义缓存，memory|redis|off）\n"
                "   - TRINITY_CACHE_TTL（缓存 TTL 秒数，默认 300）\n"
                "   - TRINITY_IMPORTANCE_BOOST（importance 动态加权，默认 off，可 on——重排 ±0.1）\n"
                "   - TRINITY_STRENGTH_BOOST（双强度因子，默认 off，可 on——重排 ±0.075）\n"
                "   - TRINITY_RRF_K（RRF 融合常数，默认 60，可调如 5/30/100——影响融合排序区分度）\n"
                "   - TRINITY_BM25_K1（BM25 词频饱和，默认 1.5，可调如 0.5-3.0——关键词通道排序）\n"
                "   - TRINITY_BM25_B（BM25 文档长度归一化，默认 0.75，可调如 0.3-1.0）\n"
                "   - TRINITY_VECTOR_WEIGHT / TRINITY_BM25_WEIGHT / TRINITY_GRAPH_WEIGHT"
                "（fusion 通道权重，默认 0.35/0.25/0.25——需评测端 --strategy fusion 才生效）\n"
                "3. 每项必须可 env 覆盖、可回滚。\n"
                "4. **当前评测融合策略 = %s**：rrf 时只提议 RRF_K/GRAPH_PPR 等 rrf 生效参数；"
                "fusion 时才可提议 VECTOR_WEIGHT/BM25_WEIGHT/GRAPH_WEIGHT。\n"
                "性能画像: %s\n"
                "已证伪（勿重复提议）: %s\n"
                "输出 JSON 数组：[{\"id\": str, \"env\": {K: V}, \"desc\": str}]"
                % (max_variants, strategy, json.dumps(signal, ensure_ascii=False)[:800],
                   json.dumps(falsified[:5], ensure_ascii=False))
            )
            payload = {
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4, "max_tokens": 600,
            }
            req = urllib.request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            # 提取 JSON 数组
            start, end = content.find("["), content.rfind("]")
            if start >= 0 and end > start:
                proposed = json.loads(content[start:end + 1])
                # 2026-08-25（缺口G+J）：env 变量名存在性校验——LLM 曾编造
                # TRINITY_SEMANTIC_CACHE_TTL_SECONDS / TRINITY_TOP_K / TRINITY_RRF_K
                # （代码中不存在，导致 A/B 测无效变量）。只接受经代码验证真实
                # 存在的变量（2026-08-25 全仓 grep os.environ 确认）。
                # 2026-08-25（白名单审计）：只保留 search_hybrid 路径实际读取的 env。
                # 移除：TRINITY_RERANKER（mixed.py VectorIndex 参数，search_hybrid 不走）、
                # TRINITY_AUTO_LINK/TRINITY_LLM_EXTRACT（ingest 路径非检索）。
                # 新增：IMPORTANCE_BOOST/STRENGTH_BOOST（hybrid_retriever 校准重排参数）。
                _KNOWN_ENV = {
                    "TRINITY_ADAPTIVE_ROUTING",   # _search.py 默认 on（短查询 FTS 轻通道）
                    "TRINITY_GRAPH_PPR",          # hybrid_retriever.py 默认 on
                    "TRINITY_CONFIDENCE_SCORER",  # hybrid_retriever.py 默认 off（改分不改序）
                    "TRINITY_CACHE_BACKEND",      # hybrid_retriever.py memory|redis|off（性能类）
                    "TRINITY_CACHE_TTL",          # hybrid_retriever.py 默认 300（性能类）
                    "TRINITY_IMPORTANCE_BOOST",   # hybrid_retriever.py 默认 off——importance 加权（±0.1 重排）
                    "TRINITY_STRENGTH_BOOST",     # hybrid_retriever.py 默认 off——双强度因子（±0.075 重排）
                    "TRINITY_RRF_K",              # _search.py hybrid_retriever 构造（env 可覆盖）
                    "TRINITY_VECTOR_WEIGHT",      # fusion 向量通道权重（默认 0.35）
                    "TRINITY_BM25_WEIGHT",        # fusion BM25 通道权重（默认 0.25）
                    "TRINITY_GRAPH_WEIGHT",       # fusion 图谱通道权重（默认 0.25）
                    # 2026-08-25（新维度：BM25 参数）：k1/b 是经典 BM25 参数，
                    # 直接影响关键词通道排序（默认 k1=1.5/b=0.75）。实测 k1=0.1
                    # 改变 BM25 分数（1.917 vs 0.941）并翻转排序——真实可测维度。
                    "TRINITY_BM25_K1",            # BM25 词频饱和（默认 1.5）
                    "TRINITY_BM25_B",             # BM25 文档长度归一化（默认 0.75）
                }
                for p in proposed[:max_variants]:
                    if p.get("id") not in falsified_ids and isinstance(p.get("env"), dict):
                        env = {k: v for k, v in p["env"].items() if k in _KNOWN_ENV}
                        if env and all(str(k).startswith("TRINITY_") for k in env):
                            llm_candidates.append({"id": p["id"], "env": env,
                                                   "desc": p.get("desc", "LLM 提议")})
    except Exception:
        pass  # LLM 提议失败 → 只用内置清单

    # LLM 提议优先；不足 max_variants 时用内置清单补齐（去重）
    candidates = llm_candidates[:max_variants]
    for v in builtin:
        if len(candidates) >= max_variants:
            break
        if all(v["id"] != c["id"] for c in candidates):
            candidates.append(v)
    return candidates[:max_variants]


def _ab(variant: dict, n_qa: int, data_path: str = "", baseline_json: str = "",
        metric: str = "qa", strategy: str = "rrf") -> dict:
    """调 evolve_ab.py 跑单个候选 A/B（超时 2700s——QA+judge3 双轮 LLM 密集）。

    2026-08-25（缺口E）：baseline_json 传 signal 文件 → evolve_ab 复用其 QA 结果
    作基线（--baseline-json），只跑 exp 单轮，省 ~50% 时间。
    2026-08-25（R@5）：metric=retrieval 时 A/B 用 R@5 检索命中（确定性，无 judge）。
    2026-08-25（fusion）：strategy 透传——fusion 时通道权重变体才生效。
    """
    env_str = ",".join(f"{k}={v}" for k, v in variant["env"].items())
    cmd = [PY, os.path.join(REPO, "scripts", "evolve_ab.py"),
           "--n-qa", str(n_qa), "--variant", env_str, "--tag", variant["id"]]
    if baseline_json:
        cmd += ["--baseline-json", baseline_json]
    if metric != "qa":
        cmd += ["--metric", metric]
    if strategy != "rrf":
        cmd += ["--strategy", strategy]
    if data_path:
        cmd += ["--data", data_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=2700, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return {"error": r.stderr[-300:] or r.stdout[-300:]}
    # 找最新 ab_ 文件
    files = sorted([f for f in os.listdir(EVOLVE_DIR) if f.startswith(f"ab_{variant['id']}_")])
    if not files:
        return {"error": "no ab result"}
    with open(os.path.join(EVOLVE_DIR, files[-1]), encoding="utf-8") as f:
        return json.load(f)


def _persist_env(env: dict) -> None:
    """把已采纳 env 持久化（维护链/服务读取，应用方式见文档）。"""
    os.makedirs(EVOLVE_DIR, exist_ok=True)
    merged = {}
    if os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, encoding="utf-8") as f:
                merged = json.load(f)
        except Exception:
            pass
    merged.update(env)
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)


def _write_memory(content: str) -> None:
    """写记忆（决策记录）——失败静默。"""
    try:
        os.environ.setdefault("TRINITY_LLM_EXTRACT", "off")
        from trinity import Trinity
        mem = Trinity()
        mem.ingest(content, tags=["self-evolution"], importance=0.6)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-qa", type=int, default=10)
    parser.add_argument("--max-variants", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true", help="只信号+提议，不 A/B")
    parser.add_argument("--force", action="store_true", help="忽略降频")
    parser.add_argument("--skip-signal-qa", action="store_true")
    parser.add_argument("--data", default="",
                        help="私有留出子集路径（R8 P1-①，默认公开集）")
    parser.add_argument("--metric", default="qa", choices=["qa", "retrieval", "mrr", "ndcg"],
                        help="评测指标：qa=judge3 准确率（默认）；retrieval=R@5；mrr=MRR；ndcg=nDCG@5（排序敏感，推荐）")
    parser.add_argument("--strategy", default="rrf", choices=["rrf", "fusion"],
                        help="混合检索融合策略（透传给 evolve_ab）：rrf 默认；fusion 时通道权重变体生效")
    args = parser.parse_args()

    state = _load_state()

    # ── 降频保护 ──
    interval = state.get("interval", "daily")
    if not args.force:
        if interval == "paused":
            print("paused (no-improve streak) — use --force")
            return 0
        if time.time() - state.get("last_run_ts", 0) < INTERVAL_SECONDS.get(interval, 86400):
            print(f"not due yet (interval={interval})")
            return 0

    print(f"=== evolve cycle #{state['cycles'] + 1} ===", flush=True)

    # ── ① SIGNAL ──
    print("[1/4] signal ...", flush=True)
    signal = _signal(args.skip_signal_qa, args.n_qa, args.data,
                     metric=args.metric, strategy=args.strategy)
    if signal.get("error"):
        print("signal error:", signal["error"])
        return 1
    qa_n = signal.get("qa", {}).get("n", 0)
    print(f"  signal: qa_n={qa_n} doc_share={signal.get('quality', {}).get('doc_share')}")

    # ── ② VARIANT ──
    print("[2/4] propose variants ...", flush=True)
    falsified = _load_falsified()
    variants = _propose_variants(signal, falsified, args.max_variants, strategy=args.strategy)
    if not variants:
        print("  no variants (all falsified?) — 降频")
        state["no_improve_streak"] = state.get("no_improve_streak", 0) + 1
        if state["no_improve_streak"] >= STREAK_PAUSE:
            state["interval"] = "paused"
        _save_state(state)
        return 0
    for v in variants:
        print(f"  variant: {v['id']} — {v['desc']}")

    if args.dry_run:
        print("dry-run — 不执行 A/B（候选已生成）")
        return 0

    # ── ③ A/B + ④ CERTIFY ──
    improved = False
    for v in variants:
        print(f"[3/4] A/B {v['id']} ...", flush=True)
        # 缺口E：复用 signal 的 QA 结果作基线（省 base 重跑 ~50% 时间）
        baseline_file = signal.get("_signal_file", "")
        res = _ab(v, args.n_qa, args.data, baseline_json=baseline_file,
                  metric=args.metric, strategy=args.strategy)
        if res.get("error"):
            print(f"  A/B error: {res['error'][:120]}")
            continue
        delta = res.get("delta", 0.0)
        base_acc = res.get("baseline_score") or 0.0
        exp_acc = res.get("experimental_score") or 0.0
        n = res.get("n") or 0
        print(f"  {v['id']}: base={base_acc} exp={exp_acc} "
              f"delta={delta:+.3f} CI=[{res.get('ci_low')},{res.get('ci_high')}] "
              f"p={res.get('mcnemar_p')} accepted={res.get('accepted')}")

        # 2026-08-25（缺口C 修复）：无区分度检测——小样本全对（base=1.0 且 n<=20）
        # 无法区分候选优劣（此前 rrf_k60 在 5 题全对样本被误证伪）。
        # 无区分度 → 跳过：不记录证伪、不计入 no_improve_streak、不采纳。
        if base_acc >= 0.999 and n <= 20:
            print(f"  {v['id']}: 无区分度（base=1.0, n={n}）— 跳过，不记录证伪")
            continue

        if res.get("accepted"):
            _persist_env(v["env"])
            state["adopted"] = state.get("adopted", []) + [{
                "id": v["id"], "ts": datetime.now().isoformat(),
                "env": v["env"], "delta": delta,
            }]
            _write_memory(
                f"[自进化采纳] 候选 {v['id']}（{v['desc']}）QA {base_acc}→"
                f"{exp_acc}（delta {delta:+.3f}）已采纳。env: {v['env']}"
            )
            improved = True
        else:
            falsified.append({"id": v["id"], "ts": datetime.now().isoformat(),
                              "env": v["env"], "delta": delta})
            _save_falsified(falsified)

    # ── ⑤ 收敛保护 ──
    state["cycles"] += 1
    state["last_run_ts"] = time.time()
    if improved:
        state["no_improve_streak"] = 0
        state["interval"] = "daily"
    else:
        streak = state.get("no_improve_streak", 0) + 1
        state["no_improve_streak"] = streak
        if streak >= STREAK_PAUSE:
            state["interval"] = "paused"
            print(f"no improvement {streak} 轮 — paused（--force 恢复）")
        else:
            state["interval"] = "daily"
    _save_state(state)

    print(f"=== cycle done: adopted={len(state['adopted'])} falsified_total={len(falsified)} "
          f"streak={state['no_improve_streak']} interval={state['interval']} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
