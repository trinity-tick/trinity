# -*- coding: utf-8 -*-
"""Collector 事件上报 demo —— 激活事件驱动采集链路。

模拟一个 agent 通过 AgentConnector 上报生命周期事件（会话开始/工具调用/会话结束），
EventDrivenCollector 缓冲 → flush 写入 SQLite，验证 collector 链路可用。

用法:
    python scripts/collector_demo.py [--agent agent-alpha]
"""
import argparse
import sys
import time

sys.path.insert(0, r"C:\Users\Administrator\trinity")

from trinity.memory.active_collector import EventDrivenCollector, AgentConnector  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="agent-alpha")
    args = ap.parse_args()

    print(f"== Collector 事件上报 demo (agent={args.agent}) ==")
    collector = EventDrivenCollector()
    conn = AgentConnector(event_collector=collector, agent_name=args.agent)

    print("[1] 上报: 会话开始")
    conn.on_conversation_start(task_desc="库位优化方案评审")
    print("[2] 上报: 工具调用 x2")
    conn.on_tool_call_before(tool_name="search_memories", tool_args={"query": "库位"})
    conn.on_tool_call_after(tool_name="search_memories", result_preview="3 条结果")
    print("[3] 上报: 决策 + 会话结束")
    conn.on_decision(decision="采纳方案 A", reasoning="成本最低")
    conn.on_session_end(summary="评审完成，方案通过")

    n_buffered = len(collector._buffer)
    print(f"[4] 缓冲事件数: {n_buffered}")

    n_written = collector.flush()
    print(f"[5] flush 写入: {n_written} 条")
    st = collector._stats
    print(f"    统计: events_captured={st.events_captured} flushed={st.events_flushed} errors={st.errors}")

    if n_written > 0:
        print("\n[OK] Collector 链路可用：事件上报 → 缓冲 → flush 落库")
    else:
        print("\n[!] 未写入（查看 flush 日志/适配器状态）")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
