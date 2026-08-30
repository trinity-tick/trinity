# -*- coding: utf-8 -*-
"""trinity/brain/self_model.py — 会话自我模型雏形（2026-09，EXECUTION 149）

从"会话身份"走向"自我模型"第一步：每个会话维护一段 identity
（自我描述：关注领域 + 近期主题 + 情绪基调），作为检索情境的一部分。

- 关注领域：从会话查询高频词推导（简单词频）
- 近期主题：最近查询（持久化上下文已有）
- 情绪基调：会话情绪状态（affect_state）
- identity 文本进入 _build_auto_situation（检索带"我是谁"）

实现轻量：基于 session_context 的 last_query + affect 组合。
"""


def build_identity(last_query: str, affect, domain_hint: str = "") -> str:
    """构造会话身份描述。"""
    try:
        parts = []
        lq = str(last_query or "").strip()
        if lq:
            parts.append(f"近期关注：{lq[:40]}")
        if affect:
            pol = affect.get("polarity")
            if pol == "neg":
                parts.append("情绪基调：谨慎/风险意识")
            elif pol == "pos":
                parts.append("情绪基调：积极")
            elif affect.get("arousal", 0) > 0.6:
                parts.append("情绪基调：紧迫")
        if domain_hint:
            parts.append(f"领域：{domain_hint[:30]}")
        return "；".join(parts) if parts else ""
    except Exception:
        return ""


def extract_domain(queries) -> str:
    """从查询序列提取关注领域（高频词）。"""
    try:
        from collections import Counter
        words = Counter()
        for q in queries or []:
            for w in str(q).split()[:8]:
                w = w.strip()
                if len(w) >= 2:
                    words[w] += 1
        top = [w for w, _ in words.most_common(3)]
        return " ".join(top)[:40] if top else ""
    except Exception:
        return ""
