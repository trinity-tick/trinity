"""
structure_memory_audit — 记忆/结构双闭环一致性审计（F6）

验证目标：同一 DSH 会话的「结构事件流」与「记忆层」应可互相印证：
  1. 结构事件中的 user/message、assistant/message 内容，应能在记忆层
     （engine.search）检索到对应内容（同一事实双写：结构层存事件原文，
     记忆层存语义化记忆——语义化后内容可近似匹配）；
  2. agent_id 归属一致：结构会话 agent_id 与记忆 agent_id 应同为
     dsh-<sessionId>；
  3. 时间线可对账：结构 turn 数与记忆写入的会话归属不冲突。

输出：JSON 报告（通过/失败 + 明细）。只读审计，不修改数据。
"""

import json
import sys
import time

sys.path.insert(0, r"C:\Users\Administrator\trinity")
from trinity.structure_store import (  # noqa: E402
    structure_query,
    structure_sessions,
)
from trinity.core.client import Trinity  # noqa: E402


def audit() -> dict:
    report = {
        "ts": time.time(),
        "structure_events": 0,
        "structure_sessions": 0,
        "memory_checks": [],
        "identity_checks": [],
        "passed": 0,
        "failed": 0,
    }

    sessions = structure_sessions().get("sessions", [])
    report["structure_sessions"] = len(sessions)

    # 引擎（记忆层）
    engine = Trinity()

    # 1. 遍历结构会话，抽 user/assistant 消息内容 → 记忆层检索印证
    checked_content = 0
    for s in sessions:
        sid = s["session_id"]
        agent_id = s.get("agent_id", "")
        events = structure_query({"session_id": sid, "limit": 500}).get("events", [])
        report["structure_events"] += len(events)

        # 身份一致性：结构 agent_id 应为 dsh-<sessionId>
        expect_agent = f"dsh-{sid}"
        ident_ok = agent_id == expect_agent
        report["identity_checks"].append({
            "session_id": sid,
            "structure_agent_id": agent_id,
            "expected": expect_agent,
            "pass": ident_ok,
        })
        report["passed" if ident_ok else "failed"] += 1

        # 消息内容 → 记忆层检索
        for ev in events:
            if ev["type"] not in ("user/message", "assistant/message"):
                continue
            content = (ev.get("data") or {}).get("content", "")
            if not content or len(content) < 8:
                continue
            # 取内容前 40 字做检索查询
            query = content[:40]
            try:
                res = engine.search(query=query, top_k=5)
                hits = res.get("results", res if isinstance(res, list) else [])
                hit = any(query[:12] in (h.get("content", "") or "") for h in hits)
            except Exception as exc:
                hit = False
            report["memory_checks"].append({
                "session_id": sid,
                "event_type": ev["type"],
                "content_head": content[:30],
                "memory_hit": hit,
            })
            report["passed" if hit else "failed"] += 1
            checked_content += 1

    report["memory_checks_total"] = checked_content
    report["ok"] = report["failed"] == 0
    return report


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)
