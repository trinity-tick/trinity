# -*- coding: utf-8 -*-
"""trinity/brain/recurrence_consolidation.py — 递归巩固（EXECUTION 343）。

借鉴 RecMem（2026：Recurrence-based Memory Consolidation）——
递归式巩固：压缩 → 再巩固 → 迭代收敛（高效长时程——
每轮巩固结果再巩固）。

与睡眠巩固（阶段）互补：睡眠=阶段；本模块=递归迭代。
Trinity 现在：
  recurse_consolidate(memory): 递归巩固（迭代压缩收敛）
"""
import os
import sys
import json


def recurse_consolidate(memory: str, max_rounds: int = 4) -> dict:
    """递归巩固：迭代压缩直到收敛。"""
    current = str(memory)
    history = []
    for r in range(1, max_rounds + 1):
        # 压缩（要点提取——每轮缩短）
        if len(current) > 40:
            # 保留前段核心 + 决策词
            compressed = current[:30]
            for w in ("决定", "选择", "采用", "避免", "成功", "失败"):
                idx = current.find(w)
                if idx >= 0:
                    compressed = current[max(0, idx-8):min(len(current), idx+25)]
                    break
            current = compressed
        else:
            # 已收敛（无法再压缩）
            history.append({"round": r, "len": len(current), "converged": True})
            break
        history.append({"round": r, "len": len(current), "converged": len(current) <= 40})
    final = history[-1] if history else {"len": len(current)}
    return {"original_len": len(str(memory)), "final_len": final.get("len", 0),
            "compression": round(final.get("len", 1) / max(len(str(memory)), 1), 2),
            "rounds": len(history), "converged": final.get("converged", True),
            "final_content": current[:40],
            "note": f"递归巩固：{len(history)} 轮迭代 → 收敛（压缩 {round(final.get('len',1)/max(len(str(memory)),1),2)}）"}
