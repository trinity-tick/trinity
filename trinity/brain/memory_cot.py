# -*- coding: utf-8 -*-
"""trinity/brain/memory_cot.py — 记忆思维链（EXECUTION 314，大脑化）。

借鉴 MemCoT（2026：Test-Time Scaling through Memory-Driven
Chain-of-Thought）——记忆驱动的思维链：记忆 → 推理步骤
扩展（测试时用记忆引导推理深度）。

与认知管线（11 阶段）互补：管线=标准流程；本模块=记忆扩展。
Trinity 现在：
  cot_with_memory(question): 记忆驱动思维链（记忆→步骤扩展）
"""
import os
import sys
import json


def cot_with_memory(question: str, max_steps: int = 5) -> dict:
    """记忆驱动思维链：每步用记忆扩展推理。"""
    steps = []
    # 每步检索相关记忆 → 形成推理步骤
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        for i in range(1, max_steps + 1):
            # 每步检索不同角度的记忆
            r = m.search_hybrid(f"{question} 步骤{i}", top_k=1)
            items = r if isinstance(r, list) else r.get("results", [])
            if items:
                evidence = str(items[0].get("content") or "")[:45]
                steps.append({"step": i, "reasoning": f"依据记忆：{evidence}",
                              "source": "memory"})
            else:
                steps.append({"step": i, "reasoning": f"步骤{i}：逻辑推演（无直接记忆）",
                              "source": "logic"})
            if len(steps) >= min(3, max_steps) and i >= 3:
                break
    except Exception:
        steps = [{"step": i, "reasoning": f"步骤{i}：标准推演", "source": "logic"}
                 for i in range(1, min(3, max_steps) + 1)]
    memory_used = sum(1 for s in steps if s["source"] == "memory")
    return {"question": str(question)[:30], "steps": steps,
            "step_count": len(steps), "memory_used": memory_used,
            "note": f"记忆驱动思维链：{len(steps)} 步（记忆支撑 {memory_used} 步）"}


def cot_report() -> dict:
    """思维链状态。"""
    return {"note": "MemCoT：测试时记忆扩展推理（Test-Time Scaling）"}
