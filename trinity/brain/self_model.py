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

def reflect(last_query: str, affect, percepts=None) -> str:
    """会话自省（EXECUTION 150）：从当前状态生成"我"的反思描述。

    自省 = 元认知的自我维度：不仅知道关注什么，还评估"我学到了什么/
    状态如何"。返回反思文本（可入记忆/情境）。
    """
    try:
        lines = []
        lq = str(last_query or "").strip()
        if lq:
            lines.append(f"我在关注：{lq[:40]}")
        if affect:
            pol = affect.get("polarity")
            aro = float(affect.get("arousal") or 0.0)
            if pol == "neg":
                lines.append("我的状态：谨慎（近期经历偏消极）")
            elif pol == "pos":
                lines.append("我的状态：积极")
            elif aro > 0.6:
                lines.append("我的状态：紧迫")
            else:
                lines.append("我的状态：平稳")
        if percepts:
            n = len(percepts or [])
            if n:
                lines.append(f"我感知到 {n} 个近期事件信号")
        lines.append("我的学习：检索到的记忆正在塑造我的权重（Hebbian）")
        return " | ".join(lines)
    except Exception:
        return ""


def reflect_to_memory(adapter, session_id: str) -> bool:
    """把会话自省写入记忆（category=self-reflection，可检索）。"""
    try:
        _ctx = adapter.context_load(session_id) if adapter and hasattr(adapter, "context_load") else None
        if not _ctx:
            return False
        _txt = reflect(_ctx.get("last_query", ""), _ctx.get("affect"), _ctx.get("percepts"))
        if _txt and hasattr(adapter, "store_memory"):
            adapter.store_memory(
                content="[self-reflection] " + _txt,
                category="self-reflection",
                tags=["self", "reflection"], importance=0.5,
            )
            return True
        return False
    except Exception:
        return False
