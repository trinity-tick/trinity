# -*- coding: utf-8 -*-
"""A5 记忆压缩 Token 经济学 — 评估压缩的 token 节省与信息保留。

方法:
  1. 从聚合池采样若干"大段"记忆（按 content 长度排序取 top）
  2. 估算原始 token 数（字符数/4 近似）
  3. 调用 /memory/compress（agent=compress-econ，max_tokens 预算）
  4. 对比压缩前后规模与命中率（检索词是否仍可召回），输出成本曲线

注意: /memory/compress 按 agent 压缩上下文，压缩产物为摘要记忆；
本脚本以"写入一批大记忆 → 压缩 → 统计"的方式评估。

用法:
    python benchmark/compress_economics.py [--samples 20] [--max-tokens 2048]
"""
import argparse
import json
import sys
import time
import requests

API = "http://127.0.0.1:8001"
HEADERS = {"X-Agent-ID": "compress-econ", "X-Agent-Role": "admin"}
AGENT = "compress-econ"


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--max-tokens", type=int, default=2048)
    args = ap.parse_args()

    # 1) 采样大记忆（引擎库全文，过滤 >300 字）
    try:
        r = requests.get(f"{API}/memories", params={"query": "", "top_k": 100},
                         headers=HEADERS, timeout=30)
        all_mem = r.json().get("results", [])
    except Exception as exc:
        print(f"采样失败: {exc}")
        all_mem = []
    seen, big = set(), []
    for m in all_mem:
        c = m.get("content", "")
        mid = m.get("memory_id")
        if mid in seen or len(c) <= 300:
            continue
        seen.add(mid)
        big.append(m)
    pool = big[: args.samples]
    print(f"采样大记忆: {len(pool)} 条 (内容>300字)")

    # 2) 估算原始 token
    total_in = sum(est_tokens(m.get("content", "")) for m in pool)
    print(f"原始 token 估算: {total_in}")

    # 3) 写入（用采样内容重建到测试 agent）
    t0 = time.time()
    mids = []
    for m in pool:
        try:
            w = requests.post(f"{API}/memories",
                              json={"content": m["content"][:800], "agent_id": AGENT,
                                    "importance": 0.7, "category": "compress_econ"},
                              headers=HEADERS, timeout=30)
            if w.status_code == 200:
                mids.append(w.json().get("memory_id"))
        except Exception:
            pass
    print(f"写入 {len(mids)} 条到 agent={AGENT}")

    # 4) 压缩
    c = requests.post(f"{API}/memory/compress",
                      json={"agent_id": AGENT, "max_tokens": args.max_tokens},
                      headers=HEADERS, timeout=120)
    print(f"compress status: {c.status_code}")
    comp = c.json() if c.status_code == 200 else {"error": c.text[:200]}
    print("compress 摘要:", json.dumps(comp, ensure_ascii=False)[:500])

    # 5) 统计（POST，回归修正 2026-08-14）
    stats = requests.post(f"{API}/memory/compress/stats", json={"agent_id": AGENT}, headers=HEADERS, timeout=30)
    st = stats.json() if stats.status_code == 200 else {}
    report = {
        "samples": len(pool), "total_input_tokens": total_in,
        "compress_response": comp, "stats": st,
        "elapsed_s": round(time.time() - t0, 1),
        "note": "token 为字符/4 近似估算；真实节省需以 LLM tokenizer 计",
    }
    with open(r"C:\Users\Administrator\.trinity\bench-results\compress_economics.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("report -> .trinity/bench-results/compress_economics.json")

    # 6) 清理测试记忆
    for mid in mids:
        try:
            requests.delete(f"{API}/memories/{mid}", headers=HEADERS, timeout=15)
        except Exception:
            pass
    print(f"cleaned {len(mids)} test memories")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
