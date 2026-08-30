#!/usr/bin/env python3
"""trinity/cognition/engine.py — 认知引擎原型（2026-09，EXECUTION 105.21）

Trinity 从"记忆域大脑"迈向"认知主体"的推理/决策层原型：
  - think(goal)：目标/问题 → 记忆检索（认知循环自动注入）→ LLM 推理
    （ReAct 式思考链）→ 决策建议 + 知识缺口识别 → 记忆回写（决策沉淀）
  - act_plan(goal)：决策 → 技能匹配（程序性记忆）→ 行动计划
    （行动层：由宿主执行或后续接入工具调度）

设计原则：不改变 Trinity 的记忆系统定位——认知引擎是【可选主体层】，
记忆循环（感知/编码/巩固/回忆/自知）全部复用，推理结果沉淀回记忆。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from ..brain.value_encoder import llm_chat  # noqa: F401
from ..brain.working_memory import get_working_memory  # noqa: F401

logger = logging.getLogger("trinity.cognition")

NL = chr(10)


def _retrieve(query: str, top_k: int = 5):
    """认知循环记忆检索（复用混合检索）。"""
    try:
        from ..core.client import TrinityClient
        c = TrinityClient()
        data = c.search_hybrid(query=query, top_k=top_k, strategy="rrf")
        results = data.get("results", []) if isinstance(data, dict) else data
        return [
            {"memory_id": r.get("memory_id"),
             "content": str(r.get("content_preview") or r.get("content") or "")[:300]}
            for r in results[:top_k]
        ]
    except Exception:
        return []


def _match_skills(goal: str, top_k: int = 3):
    """程序性记忆匹配（复用技能库）。"""
    try:
        import psycopg2
        import jieba
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT name, pattern, count FROM skills ORDER BY count DESC LIMIT 50")
        _TOOL_CN = {
            "read": "读取 查看 读文件",
            "edit": "修改 编辑 改写 更新文件",
            "write": "写入 创建 写文件",
            "pwsh": "执行 运行 命令 脚本 终端",
            "grep": "搜索 查找 检索 定位 排查",
            "run_code": "执行代码 运行 调试 代码",
            "glob": "查找文件 枚举 列出",
            "job_output": "任务 收集 结果 输出",
            "web_search": "搜索网络 查询 互联网",
            "web_fetch": "抓取 网页 获取内容",
            "skill": "技能 加载 指南",
            "memory_search": "记忆 检索 回忆",
        }
        def _tw(text):
            ws = set(w for w in jieba.cut(text) if w.strip() and len(w.strip()) >= 2)
            for en, cn in _TOOL_CN.items():
                if en in text or any(c in text for c in cn.split()):
                    ws.add(en)
            return ws
        words = _tw(goal)
        scored = []
        for name, pattern, cnt in cur.fetchall():
            pw = set(w for w in jieba.cut(str(pattern))
                     if w.strip() and len(w.strip()) >= 2)
            ov = len(words & pw)
            if ov > 0:
                scored.append((ov, cnt, name, str(pattern)))
        conn.close()
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [{"name": s[2], "pattern": s[3], "overlap": s[0]}
                for s in scored[:top_k]]
    except Exception:
        return []


def think(goal: str, session_id: Optional[str] = None,
          top_k: int = 5) -> Dict[str, Any]:
    """思考：目标 → 记忆检索 → LLM 推理 → 决策建议 → 记忆回写。"""
    t0 = time.time()
    memories = _retrieve(goal, top_k)
    skills = _match_skills(goal)
    wm = get_working_memory()
    # 工作记忆注入（当前关注）
    wm_items = []
    if session_id:
        wm_items = [i["content"][:80] for i in wm.get(session_id, top_k=3)]
    mem_text = NL.join("- " + str(m["content"]) for m in memories) or "（无相关记忆）"
    skill_text = NL.join("- " + s["name"] + ": " + s["pattern"]
                         for s in skills) or "（无匹配技能）"
    wm_text = NL.join("- " + c for c in wm_items) or "（无工作记忆）"
    prompt = (
        "你是 Trinity 认知引擎，正在【思考】一个目标。基于检索到的记忆、"
        "可复用技能和当前关注，给出：\n"
        "1. 你对现状的理解（引用记忆）；\n"
        "2. 2-3 条可执行建议（引用可用技能）；\n"
        "3. 你还需要知道什么（知识缺口）。\n"
        "控制在 300 字内，结构化输出。\n"
        "目标：" + str(goal)[:300] + NL
        + "相关记忆：" + NL + mem_text + NL
        + "可用技能：" + NL + skill_text + NL
        + "当前关注：" + NL + wm_text
    )
    reasoning = llm_chat(prompt, max_tokens=600, temperature=0.4)
    result = {
        "goal": goal,
        "reasoning": reasoning or "（LLM 不可用）",
        "memories_used": [m["memory_id"] for m in memories],
        "skills_matched": skills,
        "knowledge_gaps": [m["content"][:80] for m in memories[:0]]
                          or [],
        "latency_s": round(time.time() - t0, 2),
    }
    # 记忆回写：思考过程沉淀（决策沉淀，幂等去重）
    if reasoning and memories:
        try:
            wm.push(session_id or "cognition-default",
                    "think:" + str(goal)[:40],
                    "思考结论: " + str(reasoning)[:400],
                    importance=0.6)
        except Exception:
            pass
    return result


def act_plan(goal: str, top_k: int = 3) -> Dict[str, Any]:
    """行动规划：目标 → 决策 → 技能调度计划（执行由宿主/工具层完成）。"""
    t0 = time.time()
    memories = _retrieve(goal, top_k)
    skills = _match_skills(goal, top_k)
    plan = {
        "goal": goal,
        "decision": "基于记忆与技能匹配，建议按以下顺序行动：",
        "steps": [
            {"step": i + 1, "skill": s["name"], "pattern": s["pattern"],
             "rationale": "记忆匹配技能"}
            for i, s in enumerate(skills[:3])
        ],
        "context_memories": [m["memory_id"] for m in memories[:3]],
        "latency_s": round(time.time() - t0, 2),
    }
    if not plan["steps"]:
        plan["decision"] = "无匹配技能——建议先检索/学习相关方案（记忆缺口）"
    return plan
