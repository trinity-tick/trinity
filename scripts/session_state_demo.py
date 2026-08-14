#!/usr/bin/env python3
"""
OPT9 演示：会话摘要 + 续接上下文（真实 LLM）
==============================================
构造一个 8 轮的 agent 会话（模拟对话），生成会话摘要（DeepSeek），验证：
  1) 摘要落库（category=session_summary）且含关键实体/决策；
  2) build_session_context 返回续接包（摘要+最近记忆+实体）；
  3) 幂等：重复生成跳过；
  4) 摘要检索可用：Trinity.search 能通过会话摘要命中该会话的要点。

用法: python scripts/session_state_demo.py [--no-llm]
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)
os.environ["TRINITY_MEMORY_ENABLED"] = "0"


def load_credentials(path=os.path.expanduser("~/.dsh/.credentials.yaml")):
    creds = {}
    if os.path.exists(path):
        raw = open(path, "r", encoding="utf-8-sig").read()
        for line in raw.splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
            if m and not line.strip().startswith("#"):
                creds[m.group(1)] = m.group(2).strip().strip("'\"")
    return creds


def main() -> int:
    no_llm = "--no-llm" in sys.argv
    from trinity.adapters.sqlite import SQLiteAdapter
    from trinity.daemon.session_state import (
        generate_session_summary, build_session_context, summarize_all_sessions,
    )
    from trinity import Trinity

    tmp = tempfile.mkdtemp(prefix="sess_state_")
    adapter = SQLiteAdapter(db_path=os.path.join(tmp, "sessions.db"))
    adapter.connect()
    sess = "session-opt9-demo"

    llm = None
    if not no_llm:
        from trinity.daemon.memory_compressor import create_llm_compress_callable
        creds = load_credentials()
        key = os.environ.get("TRINITY_LLM_API_KEY") or creds.get("DEEPSEEK_API_KEY")
        if key:
            llm = create_llm_compress_callable(
                base_url=os.environ.get("TRINITY_LLM_BASE_URL", "https://api.deepseek.com/v1"),
                api_key=key, model="deepseek-chat", timeout=60)

    turns = [
        ("user", "请帮我规划迁移到新的数据仓库方案", 0.6),
        ("assistant", "建议评估 ClickHouse vs Doris，明天出对比文档", 0.7),
        ("user", "预算限制在 20 万以内，优先开源方案", 0.8),
        ("assistant", "已记录：预算<=20万、开源优先；推荐先用 Doris 做 POC", 0.8),
        ("user", "POC 环境用哪个服务器？", 0.5),
        ("assistant", "用 staging-01（10.0.0.21），下周一开始部署", 0.7),
        ("user", "记得把结论同步到周报", 0.5),
        ("assistant", "好的，周报将包含：Doris POC、staging-01 部署、周一启动", 0.6),
    ]
    for i, (role, content, imp) in enumerate(turns):
        adapter.store_memory(content, persona_id="user", session_id=sess,
                             importance=imp, tags=["demo"],
                             category="general", role=role)
    print(f"[setup] stored {len(turns)} turns in session {sess}")

    results = {"passed": 0, "failed": 0, "details": []}

    def check(name, ok, detail=""):
        results["passed" if ok else "failed"] += 1
        results["details"].append(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    # 1) 摘要生成 + 落库
    res = generate_session_summary(adapter, sess, llm)
    check("summary generated", not res["skipped"] and res["summary_id"],
          f"source_count={res.get('source_count')}")
    print(f"      summary: {res['summary'][:180]}")

    # 2) 摘要含关键实体（Doris/ClickHouse/staging）
    s = res.get("summary", "")
    check("summary keeps entities", no_llm or ("Doris" in s and "staging" in s),
          f"has_Doris={'Doris' in s} has_staging={'staging' in s}")

    # 3) 幂等
    res2 = generate_session_summary(adapter, sess, llm)
    check("idempotent (skipped)", res2.get("skipped") is True)

    # 4) 续接包
    ctx = build_session_context(adapter, sess, llm)
    check("continuation context", ctx["summary"] is not None and ctx["total_memories"] == 8,
          f"total={ctx['total_memories']}")

    # 5) 摘要可被检索命中（Trinity.search，英文 token 更稳）
    if not no_llm:
        try:
            # 与 demo adapter 同一 db 文件（默认分支 store_path 文件直用，见第八轮修复）
            t = Trinity(store_path=os.path.join(tmp, "sessions.db"))
            r = t.search("Doris POC", mode="keyword", top_k=10,
                         session_id=sess).get("results", [])
            hit = any("SESSION SUMMARY" in str(x.get("content", "")) for x in r)
            check("summary retrievable via search", hit, f"top={len(r)}")
        except Exception as e:
            check("summary retrievable via search", False, f"err={e}")

    # 6) 全量幂等
    allres = summarize_all_sessions(adapter, llm)
    check("summarize_all idempotent", allres["summarized"] == 0 and allres["skipped"] >= 1,
          f"{allres}")

    adapter.disconnect()
    print(f"\n=== session-state demo: {results['passed']}/{results['passed'] + results['failed']} PASS ===")
    return 0 if results["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
