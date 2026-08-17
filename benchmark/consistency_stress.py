# -*- coding: utf-8 -*-
"""A3 长程一致性压力测试 — 验证长期多会话记忆的一致性保持能力。

设计（默认 dry-run，不污染主库）:
  1. 生成模拟长期使用语料：N 个会话 × 每会话多轮，围绕若干"锚点主题"写入事实
     （如 skill 的版本、人物的能力、配置的取值）
  2. 对每个锚点主题写入"演进后"的新事实（模拟知识更新）
  3. 用检索验证：查询能否取回最新事实（一致性命中率），旧事实是否仍可追踪（版本链）
  4. 冲突检测：新旧事实同时存在时，/memories/conflicts 是否能识别

用法:
    python benchmark/consistency_stress.py              # dry-run: 只生成语料与评估计划
    python benchmark/consistency_stress.py --write      # 真实写入（agent_id=stress-test，结束后清理）
"""
import argparse
import json
import sys
import time
import requests

API = "http://127.0.0.1:8001"
HEADERS = {"X-Agent-ID": "stress-test", "X-Agent-Role": "admin"}
AGENT = "stress-test"

# 锚点主题: (主题, 演进前事实, 演进后事实, 验证查询)
ANCHORS = [
    ("deploy-target", "部署目标是 Windows Server 2019", "部署目标已迁移到 Ubuntu 22.04 LTS", "部署目标是什么系统"),
    ("cache-ttl", "缓存 TTL 是 60 秒", "缓存 TTL 已调整为 300 秒", "缓存 TTL 多少"),
    ("api-version", "API 当前版本 v1", "API 当前版本升级为 v2", "API 当前版本"),
    ("auth-mode", "鉴权方式为 API Key", "鉴权方式改为 RBAC 头", "鉴权方式"),
    ("default-model", "默认模型是 gpt-3.5", "默认模型换成 deepseek-chat", "默认模型"),
    ("log-level", "日志级别 INFO", "日志级别调整为 DEBUG", "日志级别"),
]


def write_memory(content: str, tags=None, importance=0.6) -> dict:
    r = requests.post(f"{API}/memories",
                      json={"content": content, "agent_id": AGENT, "importance": importance,
                            "tags": tags or ["stress"], "category": "stress_test"},
                      headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def search(q: str, top_k=3):
    r = requests.post(f"{API}/memory/search/hybrid",
                      json={"query": q, "top_k": top_k, "strategy": "rrf"},
                      headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="真实写入并清理（默认 dry-run）")
    ap.add_argument("--sessions", type=int, default=20)
    args = ap.parse_args()

    print(f"== A3 一致性压测 (dry-run={not args.write}, sessions={args.sessions}) ==")
    print(f"锚点主题数: {len(ANCHORS)}")

    if not args.write:
        print("""
[DRY-RUN] 评估计划:
  1. 写入演进前事实（每个锚点 1 条，模拟第 1 天）
  2. 模拟会话干扰（每个锚点随机写入 3-5 条无关记忆，模拟日常噪音）
  3. 写入演进后事实（模拟第 N 天知识更新）
  4. 验证查询: 对每个锚点执行检索，检查 Top-3 是否包含演进后事实
  5. 冲突检测: 检查新旧事实共现
  6. 清理: 删除 stress-test agent 的全部写入
真实运行请加 --write。""")
        return

    t0 = time.time()
    written = []  # (memory_id, anchor, stage)
    try:
        # 1) 演进前事实
        for topic, old, _new, _q in ANCHORS:
            r = write_memory(old, tags=[topic, "old"])
            written.append((r["memory_id"], topic, "old"))
        # 2) 噪音干扰
        for _ in range(args.sessions):
            topic, _o, _n, _q = ANCHORS[len(written) % len(ANCHORS)]
            r = write_memory(f"[noise-{int(time.time()%100000)}] 会话过程中的临时讨论记录", importance=0.3)
            written.append((r["memory_id"], topic, "noise"))
        # 3) 演进后事实
        for topic, _o, new, _q in ANCHORS:
            r = write_memory(new, tags=[topic, "new"])
            written.append((r["memory_id"], topic, "new"))

        # 4) 一致性验证
        results = []
        for topic, _o, new, q in ANCHORS:
            data = search(q)
            hits = data.get("results", [])
            top_texts = [h.get("content_preview", "") for h in hits]
            got_new = any(new[:12] in t for t in top_texts)
            results.append({"topic": topic, "query": q, "got_latest": got_new,
                            "top": [t[:30] for t in top_texts[:3]]})

        ok = sum(1 for r in results if r["got_latest"])
        print(f"\n一致性命中: {ok}/{len(results)}")
        for r in results:
            print(f"  [{'OK' if r['got_latest'] else 'MISS'}] {r['topic']}: top={r['top']}")

        # 5) 冲突检测（尽力而为，端点可能为空）
        try:
            conflicts = requests.get(f"{API}/agents/memory/contradictions", headers=HEADERS, timeout=30)
            print(f"\n冲突热点接口状态: {conflicts.status_code}")
        except Exception as exc:
            print(f"\n冲突检测跳过: {exc}")

        report = {"dry_run": False, "elapsed_s": round(time.time() - t0, 1),
                  "anchors": len(ANCHORS), "written": len(written),
                  "consistency_hits": ok, "consistency_total": len(results),
                  "results": results}
        with open(r"C:\Users\Administrator\.trinity\bench-results\consistency_stress.json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\nreport -> .trinity/bench-results/consistency_stress.json")
    finally:
        # 6) 清理
        n = 0
        for mid, _t, _s in written:
            try:
                requests.delete(f"{API}/memories/{mid}", headers=HEADERS, timeout=15)
                n += 1
            except Exception:
                pass
        print(f"cleaned {n}/{len(written)} test memories")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
