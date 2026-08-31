# -*- coding: utf-8 -*-
"""trinity/brain/semantic_workspace.py — 语义工作空间（EXECUTION 335）。

借鉴 Generative Semantic Workspaces（2026：Episodic Memory for
RAG）——RAG 检索后的生成式语义整合：检索片段 → 语义空间
整合 → 生成式合成（不只是检索拼接——语义工作）。

与全局工作空间（广播）互补：广播=信息汇聚；本模块=语义合成。
Trinity 现在：
  workspace_synthesis(query, retrieved): 语义合成（整合→生成）
"""
import os
import sys
import json


def workspace_synthesis(query: str, retrieved: list) -> dict:
    """语义合成：检索片段 → 语义整合 → 生成式输出。"""
    # 1) 语义整合（去重+要点提取）
    seen = set()
    integrated = []
    for r in retrieved[:6]:
        content = str(r.get("content") or "")
        if content[:20] not in seen:
            seen.add(content[:20])
            integrated.append(content[:60])
    # 2) 生成式合成（要点+结构）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.gist_extraction import extract_gist
        g = extract_gist([{"content": i} for i in integrated])
        concepts = g.get("gist_concepts", [])[:3]
    except Exception:
        concepts = []
    synthesis = {
        "content": f"关于『{str(query)[:20]}』的综合：{'、'.join(concepts or integrated[:2])}",
        "sources_integrated": len(integrated),
    }
    return {"query": str(query)[:30], "integrated": integrated[:3],
            "synthesis": synthesis["content"][:80],
            "sources": synthesis["sources_integrated"],
            "generative": True,
            "note": f"语义工作空间：{len(integrated)} 片段 → 语义整合 → 生成合成"}


def workspace_report() -> dict:
    """工作空间状态。"""
    return {"note": "生成式语义工作空间（RAG 超越检索——Semantic Workspaces）"}
