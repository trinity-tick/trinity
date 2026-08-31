# -*- coding: utf-8 -*-
"""trinity/brain/self_talk.py — 内心独白（EXECUTION 223，大脑化）。

借鉴 lex-self-talk（内部对话：AI that talks to itself, rethinks, acts）
与 OIST 2026（AI learns better when it talks to itself）——大脑的
内部语言：行动前/中的自我对话（评估/计划/怀疑多声音）。

与自省（事后反思）互补：自省=过去反思；内心独白=进行时思考。
Trinity 现在：
  inner_dialogue(topic): 多声音内心对话（评估者/计划者/怀疑者）
  decide_with_talk(topic): 对话后决策（融合声音意见）
"""
import os
import sys
import json


def inner_dialogue(topic: str, context: str = "") -> dict:
    """内心独白：多声音内部对话。"""
    # 声音 1：评估者（现状评估）
    assessor = f"评估：关于『{topic[:30]}』，我有相关记忆可参考"
    # 声音 2：计划者（行动建议）
    planner = f"计划：可以先检索相关知识，再综合决策"
    # 声音 3：怀疑者（风险提示）
    skeptic = f"怀疑：需要确认信息是否最新、是否与当前情境匹配"
    # 声音 4：元认知（自信度）
    try:
        from trinity.brain.metamemory import feeling_of_knowing
        fok = feeling_of_knowing(topic)
        metacog = f"元认知：我对这个主题的把握是 {fok.get('feeling', '未知')}（{fok.get('fok', 0)}）"
    except Exception:
        metacog = "元认知：未知"
    return {"topic": str(topic)[:40], "voices": [assessor, planner, skeptic, metacog],
            "voice_count": 4, "ts": __import__("time").time()}


def decide_with_talk(topic: str, context: str = "") -> dict:
    """对话后决策：融合声音意见。"""
    d = inner_dialogue(topic, context)
    # 简单融合规则：有元认知信息 + 怀疑提示 → 谨慎决策
    cautious = any("不确定" in v or "确认" in v for v in d["voices"])
    confident = any("把握" in v and "0.5" in v for v in d["voices"])
    decision = "谨慎行动（先验证）" if cautious else "直接行动"
    if confident:
        decision = "自信行动"
    return {"dialogue": d, "decision": decision,
            "cautious": cautious}


def talk_to_memory(topic: str) -> bool:
    """内心独白写入记忆（inner-speech 类别——可检索）。"""
    try:
        d = inner_dialogue(topic)
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        text = "[inner-speech] 关于『" + d["topic"] + "』我的内心独白：" + "；".join(d["voices"][:3])
        m.ingest(text[:280], category="inner-speech",
                 tags=["self", "talk", topic[:10]], importance=0.6,
                 wait_backfill=True)
        return True
    except Exception:
        return False
