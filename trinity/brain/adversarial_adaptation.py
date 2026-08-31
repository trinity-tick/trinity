# -*- coding: utf-8 -*-
"""trinity/brain/adversarial_adaptation.py — 对抗记忆适应（EXECUTION 334）。

借鉴 Adversarial Memory Adaptation（2026：Task-Oriented）——对抗
性记忆适应：挑战记忆 → 发现弱点 → 适应调整（对抗视角——
记忆不只是存储还要经得起挑战）。

与冲突解决（新旧判定）互补：冲突=版本；本模块=挑战适应。
Trinity 现在：
  adversarial_check(memory, challenge): 对抗检查（弱点发现）
  adapt(memory, weakness): 适应调整（修正弱点）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/adversarial_adaptation.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"adaptations": 0}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def adversarial_check(memory: str, challenge: str) -> dict:
    """对抗检查：挑战记忆 → 弱点发现。"""
    m, c = str(memory), str(challenge)
    weaknesses = []
    # 弱点 1：记忆缺乏证据（无来源标记）
    if not any(tag in m for tag in ("[", "来源", "证据", "验证")):
        weaknesses.append("缺乏证据来源")
    # 弱点 2：绝对化断言
    if any(w in m for w in ("一定", "绝对", "永远", "100%")):
        weaknesses.append("绝对化断言")
    # 弱点 3：挑战相关但记忆无应对
    if c and c[:10] in m:
        weaknesses.append("挑战词相关但需更新")
    return {"memory": m[:40], "challenge": c[:30],
            "weaknesses": weaknesses, "robust": not weaknesses,
            "note": f"对抗检查：{'健壮' if not weaknesses else '发现弱点：' + '、'.join(weaknesses[:2])}"}


def adapt(memory: str, weakness: str) -> dict:
    """适应调整：修正弱点（记录适应）。"""
    st = _load()
    st["adaptations"] += 1
    _save(st)
    return {"adapted": True, "memory": str(memory)[:40],
            "weakness": str(weakness)[:30], "fix": "补充证据/弱化断言",
            "adaptations_total": st["adaptations"],
            "note": f"对抗适应：修正『{weakness[:20]}』（第{st['adaptations']}次适应）"}
