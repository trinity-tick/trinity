# -*- coding: utf-8 -*-
"""trinity/brain/affect.py — 情感层（2026-09，EXECUTION 132）

认知依据：杏仁核的情感显著性驱动记忆编码强度（与 value_encoder 的
salience 因素互补）——情感极性（valence）与唤醒度（arousal）是记忆
"情绪色彩"的两维。对标 ZenBrain 7 层架构的情感层。

实现（零 LLM、毫秒级）：
  - 中文情感词典（积极/消极词 + 强度）+ 否定词反转 + 感叹词唤醒增强
  - assess(content) -> {"valence": -1..1, "arousal": 0..1, "polarity": "pos/neg/neu"}
失败降级返回中性（不破坏写入）。
"""

POSITIVE_WORDS = [
    "成功", "完成", "达成", "满意", "喜欢", "喜欢", "高兴", "顺利", "通过", "解决",
    "修复", "胜利", "优秀", "完美", "感谢", "很棒", "好消息", "提升", "改善", "稳定",
    "可靠", "高效", "推荐", "满意", "愉快", "欢迎", "庆祝", "里程碑",
]
NEGATIVE_WORDS = [
    "失败", "错误", "故障", "事故", "灾难", "警告", "风险", "危险", "问题", "崩溃",
    "丢失", "损失", "中断", "超时", "拒绝", "错误", "异常", "卡住", "慢", "糟糕",
    "后悔", "担心", "害怕", "愤怒", "失望", "严重", "告警", "耗尽", "攻击", "入侵",
]
NEGATION_WORDS = ["不", "没", "未", "无", "别", "莫"]
INTENSIFIERS = ["非常", "极其", "特别", "太", "很", "超", "严重", "极度"]


def _assess_rules(text: str) -> dict:
    pos_hits = sum(1 for w in POSITIVE_WORDS if w in text)
    neg_hits = sum(1 for w in NEGATIVE_WORDS if w in text)
    # 否定反转（"不成功" → 消极）
    for w in NEGATION_WORDS:
        for p in POSITIVE_WORDS:
            if w + p in text:
                neg_hits += 1
                pos_hits = max(0, pos_hits - 1)
        for n in NEGATIVE_WORDS:
            if w + n in text:
                pos_hits += 1
                neg_hits = max(0, neg_hits - 1)
    # 感叹词/强度词 → 唤醒度
    arousal = 0.3
    if "!" in text or "！" in text:
        arousal += 0.2
    arousal += 0.1 * min(3, sum(1 for w in INTENSIFIERS if w in text))
    arousal = min(1.0, arousal)

    if pos_hits > neg_hits:
        valence = min(1.0, 0.3 + 0.2 * pos_hits)
        polarity = "pos"
    elif neg_hits > pos_hits:
        valence = max(-1.0, -0.3 - 0.2 * neg_hits)
        polarity = "neg"
    else:
        valence = 0.0
        polarity = "neu"
    return {"valence": round(valence, 2), "arousal": round(arousal, 2),
            "polarity": polarity, "pos_hits": pos_hits, "neg_hits": neg_hits}


def assess(content: str) -> dict:
    """情感评估（规则，零 LLM）。失败返回中性。"""
    try:
        text = str(content or "")
        if not text.strip():
            return {"valence": 0.0, "arousal": 0.0, "polarity": "neu"}
        return _assess_rules(text)
    except Exception:
        return {"valence": 0.0, "arousal": 0.0, "polarity": "neu"}


def query_affect_terms(query: str) -> list:
    """从查询提取情感倾向（返回极性词），用于检索情感匹配。"""
    text = str(query or "")
    out = []
    for w in POSITIVE_WORDS:
        if w in text:
            out.append((w, "pos"))
    for w in NEGATIVE_WORDS:
        if w in text:
            out.append((w, "neg"))
    return out
