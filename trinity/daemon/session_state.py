"""
OPT9: 会话状态化 —— agent 级会话摘要与续接（Letta 式）
========================================================
多轮 agent 会话 → 每条会话生成"会话摘要记忆"（DeepSeek），供检索与续接：

  - generate_session_summary(): 读某 session 的全部 active 记忆 → LLM 生成
    摘要（实体/决策/未决事项），落库为一条 session-summary 记忆（tag 标记）。
  - build_session_context(): 会话续接包 = 摘要 + 最近 N 条记忆 + 实体列表，
    供 agent 恢复会话时注入上下文。
  - summarize_all_sessions(): 全量（幂等：已有 summary 的 session 跳过）。

依赖：真实 LLM（TRINITY_LLM_API_KEY，复用 memory_compressor.create_llm_compress_callable）；
无 key 时降级为抽取式摘要（取高 importance 记忆前若干条）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM_PROMPT = """\
You are a session consolidator. Summarize the given conversation-turn memories
into a single session summary that preserves:
1. ALL entity names (people, systems, tools, products)
2. Key decisions, conclusions, action items, and open questions
3. Dates, numbers, and status changes
4. The user's preferences expressed in this session

Keep it under 250 words, factual, no preamble, no markdown headings."""


def _extractive_summary(memories: List[Dict[str, Any]], max_items: int = 8) -> str:
    """无 LLM 时的降级：取 importance 最高的若干条拼接。"""
    items = sorted(memories, key=lambda m: float(m.get("importance", 0.5)), reverse=True)
    parts = [str(m.get("content", ""))[:200] for m in items[:max_items]]
    return "[EXTRACTIVE-SESSION-SUMMARY] " + " | ".join(parts)


def get_session_memories(adapter, session_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    """读某会话的 active 记忆（按创建时间正序）。"""
    conn = getattr(adapter, "_conn", None)
    if conn is None:
        raise RuntimeError("adapter 未连接")
    rows = conn.execute(
        """SELECT memory_id, content, persona_id, tenant_id, importance, tags,
                  category, sha256_hash, created_at
           FROM memories
           WHERE session_id = ? AND status = 'active' AND category != 'session_summary'
           ORDER BY created_at ASC LIMIT ?""",
        (session_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def generate_session_summary(
    adapter,
    session_id: str,
    llm_callable: Optional[Any] = None,
    top_n: int = 60,
) -> Dict[str, Any]:
    """生成某会话的摘要并落库（幂等：已存在 summary 则跳过）。

    Returns:
        {"session_id", "summary_id", "summary", "source_count", "skipped"}
    """
    conn = getattr(adapter, "_conn", None)
    if conn is None:
        raise RuntimeError("adapter 未连接")

    existing = conn.execute(
        "SELECT memory_id FROM memories WHERE session_id = ? AND category = 'session_summary' "
        "AND status = 'active' LIMIT 1",
        (session_id,),
    ).fetchone()
    if existing:
        return {"session_id": session_id, "skipped": True,
                "summary_id": existing[0], "summary": "", "source_count": 0}

    memories = get_session_memories(adapter, session_id, limit=top_n)
    if not memories:
        return {"session_id": session_id, "skipped": True,
                "summary_id": None, "summary": "", "source_count": 0}

    entries = "\n".join(
        f"[{i}] ({m.get('created_at', '')}) {m.get('content', '')}"[:600]
        for i, m in enumerate(memories, 1)
    )
    user_prompt = f"Conversation-turn memories ({len(memories)} turns):\n{entries}\n\nSession summary:"

    if llm_callable is not None:
        try:
            summary = llm_callable(SUMMARY_SYSTEM_PROMPT, user_prompt).strip()
        except Exception as e:
            logger.warning("LLM summary failed (%s); using extractive fallback", e)
            summary = _extractive_summary(memories)
    else:
        summary = _extractive_summary(memories)

    summary_id = f"sesssum_{uuid.uuid4().hex[:12]}"
    persona = memories[0].get("persona_id") or "default"
    full_content = f"[SESSION SUMMARY — {len(memories)} turns]\n{summary}"
    # 走 adapter.store_memory 正规路径（保证 FTS5 触发 + 版本链一致）
    result = adapter.store_memory(
        content=full_content,
        persona_id=persona,
        session_id=session_id,
        tenant_id="default",
        importance=0.9,
        tags=["session-summary"],
        category="session_summary",
        role="system",
    )
    summary_id = result.get("memory_id") or summary_id
    logger.info("session summary %s generated for session %s (%d sources)",
                summary_id, session_id, len(memories))
    return {"session_id": session_id, "skipped": False,
            "summary_id": summary_id, "summary": summary,
            "source_count": len(memories)}


def build_session_context(
    adapter,
    session_id: str,
    llm_callable: Optional[Any] = None,
    recent_n: int = 15,
) -> Dict[str, Any]:
    """会话续接包：摘要 + 最近记忆 + 会话统计（供 agent 恢复上下文）。"""
    memories = get_session_memories(adapter, session_id, limit=1000)
    conn = getattr(adapter, "_conn", None)
    summary_row = conn.execute(
        "SELECT content FROM memories WHERE session_id = ? AND category = 'session_summary' "
        "AND status = 'active' ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    ).fetchone() if conn else None

    recent = memories[-recent_n:]
    entities = set()
    for m in memories:
        tags = m.get("tags")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = []
        for t in tags or []:
            if isinstance(t, str) and t not in ("lme", "session-summary", "sync", "hermes"):
                entities.add(t)

    return {
        "session_id": session_id,
        "summary": summary_row[0] if summary_row else None,
        "total_memories": len(memories),
        "recent_memories": recent,
        "entities": sorted(entities)[:30],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    }


def summarize_all_sessions(
    adapter,
    llm_callable: Optional[Any] = None,
    session_ids: Optional[List[str]] = None,
) -> Dict[str, int]:
    """全量会话摘要（幂等）。"""
    conn = getattr(adapter, "_conn", None)
    if session_ids is None:
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM memories WHERE status='active' "
            "AND session_id IS NOT NULL ORDER BY session_id"
        ).fetchall()
        session_ids = [r[0] for r in rows]
    done = 0
    skipped = 0
    for sid in session_ids:
        res = generate_session_summary(adapter, sid, llm_callable)
        if res["skipped"]:
            skipped += 1
        else:
            done += 1
    return {"sessions": len(session_ids), "summarized": done, "skipped": skipped}
