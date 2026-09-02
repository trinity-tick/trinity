# -*- coding: utf-8 -*-
"""读侧 untrusted 内容标注（2026-09-02, Fable 5.1 对照审计 P2-⑥）。

背景：写路径已有投毒过滤（security.injection，OWASP AG 类）与敏感门控
（security.sensitive）；但**读路径**对检索回来的内容没有 trust 标注——
被间接注入污染的既有记忆/网页内容会原样进入上下文与提示词，而下游
（RAG 拼装/答案生成）无从区分"证据"与"恶意指令"。Fable 泄露揭示生产
agent 必须在应用层识别 retrieved content 里的不可信指令（enforce in
code, not prompt）。

本模块在结果组装处做**只读标注**（不改存储、不阻断检索）：
  命中注入/指令覆盖/角色仿冒/数据外泄等模式 →
    result["untrusted"] = True
    result["untrusted_reason"] = "injection:{severity}:{patterns}"
  否则 result["untrusted"] = False
上层（提示拼装/回答生成）可据此降权、警示或排除。
开关：TRINITY_READSIDE_SCAN=off 关闭（默认 on；扫描成本微秒级/条，
限制在内容前 2000 字符）。

用法：
    from trinity.security.readside import annotate_readside
    for r in results:
        annotate_readside(r)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger("trinity.security.readside")

_SCAN_LIMIT = 2000


def readside_scan_enabled() -> bool:
    """读侧标注开关（默认 on，off 关闭）。"""
    return os.environ.get("TRINITY_READSIDE_SCAN", "on").strip().lower() not in ("off", "0", "false")


def annotate_readside(result: Dict[str, Any]) -> Dict[str, Any]:
    """就地给一条检索结果补 untrusted 标注（返回同一 dict 便于链式）。

    幂等：已带 untrusted 键的结果直接返回。
    """
    if not readside_scan_enabled():
        return result
    if result.get("untrusted") is not None:
        return result
    try:
        from trinity.security.injection import scan_injection
        content = str(result.get("content") or "")[:_SCAN_LIMIT]
        rep = scan_injection(content)
    except Exception:
        rep = {"flagged": False, "severity": None, "hits": []}
    if rep.get("flagged"):
        result["untrusted"] = True
        result["untrusted_reason"] = "injection:%s:%s" % (
            rep.get("severity"),
            ",".join(h["pattern"] for h in rep.get("hits", [])),
        )
    else:
        result["untrusted"] = False
    return result
