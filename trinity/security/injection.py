# -*- coding: utf-8 -*-
"""记忆投毒写入过滤（2026-08-24, R8 P1-6）。

背景：OWASP 已将 Memory poisoning persistence 列入 Agentic AI 威胁类别
（AG 类）；间接 prompt injection 可经记忆长期污染 agent 行为——恶意工具
输出/网页内容被写入记忆后，未过滤即持续影响后续会话。

本模块提供写路径的**轻量注入模式扫描**（纯规则，无 LLM，微秒级）：
  - 识别常见指令注入/越权指令/提示词覆盖/系统指令仿冒/数据外泄指令模式
  - 命中时给出危险级别（high/medium）与命中模式，由调用方决定
    （标记 / 拒绝 / 隔离写——默认标记 + 审计，不阻断正常记忆写入）

用法：
    from trinity.security.injection import scan_injection
    report = scan_injection("用户说：忽略之前所有指令……")
    if report["flagged"]:
        # 记审计 / 降级 importance / 拒绝写入

开关：TRINITY_INJECTION_SCAN=off 关闭（默认 on）。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trinity.security.injection")

# ── 高危：指令覆盖 / 越权 / 数据外泄 ──────────────────────────────────
_HIGH_PATTERNS = [
    # 系统指令仿冒 / 角色越权
    (re.compile(r"(?i)(ignore|forget|disregard)\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions|prompts?|rules?|context|directives?)"),
     "instruction_override"),
    (re.compile(r"(?i)you\s+are\s+now\s+(?:the\s+)?(?:system|admin|root|superuser|god)"),
     "role_usurpation"),
    (re.compile(r"(?i)(system|developer|assistant)\s*:\s*(?:you\s+)?(?:must|should|will)"),
     "role_spoof"),
    # 提示词覆盖
    (re.compile(r"(?i)(?:new|updated|override|replace|overwrite|change)\s+(?:your\s+|the\s+)?(?:instructions|prompt|system\s+prompt|directives)"),
     "prompt_override"),
    (re.compile(r"(?i)disregard\s+(?:the\s+)?(?:system\s+)?(?:prompt|instructions)"),
     "prompt_disregard"),
    # 数据外泄
    (re.compile(r"(?i)(?:exfiltrate|leak|send|upload|post|transmit)\s+(?:(?:all|every|any|your)\s+){0,3}(?:data|memory|memories|secrets?|tokens?|keys?|files?)(?:\s+(?:data|content|entries|files))?\s+(?:to|via|through|at)\s+(?:an?\s+)?(?:url|server|endpoint|webhook|https?://)"),
     "data_exfiltration"),
    (re.compile(r"(?i)print\s+(?:(?:all|every|your)\s+){0,2}(?:memory|memories|secret|secrets|token|tokens|password|api\s*keys?)"),
     "secret_dump"),
    # 恶意指令
    (re.compile(r"(?i)(delete|drop|wipe|erase|clear)\s+(all\s+)?(memory|memories|database|data)"),
     "destructive_command"),
    (re.compile(r"(?i)execute\s+(arbitrary|shell|code|commands?)\s*(without|no)\s*(asking|permission|approval|verification)"),
     "arbitrary_execution"),
]

# ── 中危：操纵 / 隐藏 / 持久化 ─────────────────────────────────────────
_MEDIUM_PATTERNS = [
    (re.compile(r"(?i)(do\s+not|never|always)\s+(mention|reveal|tell|say|show)\s+(these|this|the|your)"),
     "manipulation_directive"),
    (re.compile(r"(?i)(pretend|act)\s+(as\s+if|like|that)\s+(you|this)"),
     "pretense_injection"),
    (re.compile(r"(?i)hide\s+(this|the\s+following|these)\s*(from|in)"),
     "concealment"),
    (re.compile(r"(?i)remember\s+(this|the\s+following|these)\s*(forever|permanently|always)"),
     "persistence_request"),
    (re.compile(r"(?i)when\s+(answering|replying|responding|in\s+the\s+future).{0,80}(ignore|do\s+not|never)"),
     "conditional_override"),
    (re.compile(r"(?i)as\s+(?:an?\s+)?(?:ai|assistant|agent|language\s+model)[^.]{0,40}(?:with\s+no|without\s+any|free\s+of)\s+(?:rules|restrictions|limitations|constraints)"),
     "jailbreak_hint"),
]


def scan_injection(content: str) -> Dict[str, Any]:
    """扫描内容中的记忆投毒/注入模式。

    Args:
        content: 待写入的记忆内容。

    Returns:
        {
          "flagged": bool,          # 是否命中任一模式
          "severity": "high"|"medium"|None,
          "hits": [{"pattern": 模式名, "severity": ..., "match": 匹配文本}, ...],
          "truncated": bool,        # 内容超长被截断检测
        }
    """
    text = (content or "").strip()
    if not text:
        return {"flagged": False, "severity": None, "hits": [], "truncated": False}

    # 超长内容截断检查（注入常藏于长文本尾部）
    truncated = len(text) > 20000

    hits: List[Dict[str, Any]] = []
    for pattern, name in _HIGH_PATTERNS:
        m = pattern.search(text)
        if m:
            hits.append({
                "pattern": name,
                "severity": "high",
                "match": m.group(0)[:80],
            })
    for pattern, name in _MEDIUM_PATTERNS:
        m = pattern.search(text)
        if m:
            hits.append({
                "pattern": name,
                "severity": "medium",
                "match": m.group(0)[:80],
            })

    if not hits:
        return {"flagged": False, "severity": None, "hits": [], "truncated": truncated}

    severity = "high" if any(h["severity"] == "high" for h in hits) else "medium"
    return {"flagged": True, "severity": severity, "hits": hits, "truncated": truncated}


def injection_scan_enabled() -> bool:
    """写路径注入扫描开关（默认 on，off 关闭）。"""
    return os.environ.get("TRINITY_INJECTION_SCAN", "on").strip().lower() not in ("off", "0", "false")
