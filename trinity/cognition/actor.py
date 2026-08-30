#!/usr/bin/env python3
"""trinity/cognition/actor.py — 行动执行器（2026-09，EXECUTION 105.22）

认知主体的行动层：把 act_plan 的步骤在【认知域内】执行（安全边界：
记忆/检索/体检/技能查询——不执行宿主文件/命令操作，那由 DSH 宿主负责），
生成观察结果并回写记忆（category=action_result，经历沉淀）。

执行的动作（全部只读/无副作用）：
  retrieve   记忆检索（知识获取）
  skills     技能匹配（程序性记忆）
  selfcheck  元认知自查（自知）
  brain      状态体检（自我状态）
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

from ..brain.value_encoder import llm_chat

logger = logging.getLogger("trinity.cognition.actor")

NL = chr(10)


def _retrieve(query: str, top_k: int = 4) -> list:
    try:
        from ..core.client import TrinityClient
        c = TrinityClient()
        data = c.search_hybrid(query=query, top_k=top_k, strategy="rrf")
        results = data.get("results", []) if isinstance(data, dict) else data
        return [str(r.get("content_preview") or r.get("content") or "")[:200]
                for r in results[:top_k]]
    except Exception:
        return []


def _skills(goal: str) -> list:
    try:
        import psycopg2
        import jieba
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT name, count FROM skills ORDER BY count DESC LIMIT 30")
        words = set(w for w in jieba.cut(goal) if w.strip() and len(w.strip()) >= 2)
        scored = [(c, n) for n, c in cur.fetchall()
                  if any(w in n for w in words)][:4]
        conn.close()
        return [n for _c, n in scored] or ["（无直接匹配，可先检索学习）"]
    except Exception:
        return []


def _selfcheck(goal: str) -> Dict[str, Any]:
    try:
        from ..brain.metacognition import assess_confidence
        mems = _retrieve(goal, 5)
        return assess_confidence(mems, ["fts", "vector"])
    except Exception:
        return {"confidence": 0.0}


def execute(goal: str, session_id: str = "default") -> Dict[str, Any]:
    """认知域行动执行 + 观察回写。"""
    t0 = time.time()
    observations = []
    # 1) retrieve（知识获取）
    mems = _retrieve(goal, 4)
    observations.append({"action": "retrieve", "result": len(mems),
                         "detail": mems[:2]})
    # 2) skills（程序性记忆）
    sk = _skills(goal)
    observations.append({"action": "skills", "result": sk})
    # 3) selfcheck（自知）
    meta = _selfcheck(goal)
    observations.append({"action": "selfcheck",
                         "result": meta.get("confidence"),
                         "gap": meta.get("level")})
    # 4) LLM 行动总结（观察 → 结论）
    obs_text = NL.join(
        "- " + o["action"] + ": " + str(o.get("result"))[:150]
        for o in observations)
    summary = llm_chat(
        "你是 Trinity 认知主体。基于以下行动观察，给出结论与下一步建议"
        "（120 字内）：\n目标：" + str(goal)[:200] + NL + obs_text,
        max_tokens=250, temperature=0.4)
    result = {
        "goal": goal,
        "observations": observations,
        "conclusion": (summary or "（LLM 不可用）").strip(),
        "latency_s": round(time.time() - t0, 2),
    }
    # 观察回写（经历沉淀，category=action_result——幂等标记）
    if summary:
        try:
            import psycopg2
            conn = psycopg2.connect(
                host="127.0.0.1", port=5432, dbname="trinity",
                user="trinity", password="trinity")
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO memories
                    (memory_id, session_id, persona_id, tenant_id, agent_id,
                     content, importance, importance_score, status, category,
                     modality, content_hash, created_at, updated_at)
                VALUES (uuid_generate_v4(), %s, 'default', 'default', 'cognition',
                        %s, 0.6, 0.6, 'active', 'action_result', 'text',
                        encode(sha256(%s::bytea), 'hex'), NOW(), NOW())
            """, (session_id, "行动结论: " + str(summary)[:400],
                  "行动结论: " + str(summary)[:400]))
            conn.close()
        except Exception:
            pass
    return result
