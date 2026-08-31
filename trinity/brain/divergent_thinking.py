# -*- coding: utf-8 -*-
"""trinity/brain/divergent_thinking.py — 发散思维（EXECUTION 233，大脑化）。

借鉴 Divergent Thinking in Interactive LLM Agents（2026）——
发散思维：从一个主题发散多个候选想法（角度×联想×组合）。

Trinity 的创造素材：
  联想（192 跳跃）× 重组（212 梦境）× 模拟（209 推演）
→ 整合为发散思维引擎：
  ideate(topic, n): 从 n 个角度/组合生成候选想法
  evaluate_ideas(): 可行性/新颖性评估
"""
import os
import sys
import json


def ideate(topic: str, n: int = 3) -> dict:
    """发散生成：从主题多角度产生候选想法。"""
    ideas = []
    # 角度 1：记忆联想（相关经验）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        r = m.search_hybrid(topic, top_k=3)
        items = r if isinstance(r, list) else r.get("results", [])
        if items:
            ideas.append({"idea": f"基于经验：{str(items[0].get('content') or '')[:50]}",
                          "angle": "experience"})
    except Exception:
        pass
    # 角度 2：跨域组合（联想跳跃）
    try:
        from trinity.brain.associative_memory import creative_mix
        mix = creative_mix([topic[:10], "优化", "创新"])
        for cb in mix.get("combinations", [])[:1]:
            ideas.append({"idea": cb[:70], "angle": "cross_domain"})
    except Exception:
        pass
    # 角度 3：反向思考（反事实/假设）
    try:
        from trinity.brain.mental_simulation import simulate
        sim = simulate(topic, f"{topic}的全新方案", use_llm=False)
        if sim.get("simulated"):
            ideas.append({"idea": f"假设推演：{sim['simulation'][:60]}",
                          "angle": "counterfactual"})
    except Exception:
        pass
    # 角度 4：梦境重组
    try:
        sys.path.insert(0, r"D:\\trinity-code\\scripts")
        from dream_replay import dream_recombine
        rec = dream_recombine(2)
        for d in rec.get("dreams", [])[:1]:
            ideas.append({"idea": f"梦境重组：{d[:60]}", "angle": "dream"})
    except Exception:
        pass
    return {"topic": str(topic)[:40], "ideas": ideas[:n], "count": len(ideas)}


def evaluate_ideas(ideas: list) -> dict:
    """想法评估：可行性（有依据）+ 新颖性（跨域）。"""
    evaluated = []
    for it in ideas:
        angle = it.get("angle", "")
        feasible = 0.8 if angle in ("experience",) else (0.6 if angle == "cross_domain" else 0.5)
        novel = 0.3 if angle == "experience" else (0.8 if angle in ("cross_domain", "dream") else 0.6)
        evaluated.append({"idea": it.get("idea", "")[:60], "angle": angle,
                          "feasible": feasible, "novel": novel,
                          "score": round(feasible * 0.5 + novel * 0.5, 2)})
    evaluated.sort(key=lambda x: x["score"], reverse=True)
    return {"evaluated": evaluated, "top": evaluated[0]["idea"] if evaluated else None}
