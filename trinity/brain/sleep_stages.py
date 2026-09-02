# -*- coding: utf-8 -*-
"""trinity/brain/sleep_stages.py — 睡眠分阶段（EXECUTION 228，大脑化）。

借鉴 Phasor Agents（2026：Sleep-Staged Learning）——大脑睡眠分阶段：
  NREM 慢波：巩固事实记忆（随机复习——海马重放）
  REM：情感整合 + 跨域重组（"REM sleep as a dummy-model of the world"）

Trinity 现在：
  slow_wave_consolidation(): 慢波阶段（事实巩固——复习强化）
  rem_consolidation(): REM 阶段（情感记忆整合 + 跨域重组）
  sleep_cycle(): 完整睡眠周期（慢波 → REM）
"""
import os
import sys
import json

# 2026-09-02: 动态定位仓库根（消除 D: 副本隐式依赖，与 engine_worker 清理一致）
_SCRIPTS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts")


def slow_wave_consolidation(max_items: int = 20) -> dict:
    """慢波阶段：巩固事实记忆（随机复习 + 轻度强化）。"""
    try:
        sys.path.insert(0, _SCRIPTS_ROOT)
        import runpy
        _old = sys.argv
        sys.argv = ["dream_replay", f"--max={max_items}", "--write"]
        runpy.run_path(os.path.join(_SCRIPTS_ROOT, "dream_replay.py"), run_name="__main__")
        sys.argv = _old
        return {"stage": "slow_wave", "consolidated": max_items,
                "note": "事实记忆复习强化（海马重放）"}
    except Exception as e:
        return {"stage": "slow_wave", "error": str(e)[:80]}


def rem_consolidation(max_combos: int = 3) -> dict:
    """REM 阶段：情感整合 + 跨域重组。"""
    try:
        sys.path.insert(0, _SCRIPTS_ROOT)
        import runpy
        _old = sys.argv
        sys.argv = ["dream_replay", "--max=5", "--write"]
        runpy.run_path(os.path.join(_SCRIPTS_ROOT, "dream_replay.py"), run_name="__main__")
        sys.argv = _old
        # 情感记忆整合（杏仁核效应）
        from trinity.brain.emotional_consolidation import emotional_consolidate
        emo = emotional_consolidate(limit=50)
        # 跨域重组
        sys.path.insert(0, _SCRIPTS_ROOT)
        from dream_replay import dream_recombine
        recomb = dream_recombine(max_combos)
        return {"stage": "rem", "emotional": emo.get("emotional", 0),
                "recombined": recomb.get("combos", 0),
                "note": "情感整合 + 跨域重组（REM）"}
    except Exception as e:
        return {"stage": "rem", "error": str(e)[:80]}


def sleep_cycle(slow_wave_items: int = 20) -> dict:
    """完整睡眠周期：慢波（巩固）→ REM（整合）。"""
    sw = slow_wave_consolidation(slow_wave_items)
    rem = rem_consolidation()
    return {"cycle": "slow_wave → rem", "slow_wave": sw, "rem": rem,
            "completed": True}
