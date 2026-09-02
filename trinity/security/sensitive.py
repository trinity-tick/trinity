# -*- coding: utf-8 -*-
"""敏感内容写入门控（2026-09-02, Fable 5.1 泄露对照审计 P0-①）。

背景：Anthropic Fable 5.1 系统提示词泄露揭示其记忆系统划出"至死不记"
的隐私禁区（未成年身份信息 / 犯罪记录等法律敏感 / 精神与心理推断 /
性史 / 自残倾向）——即使用户主动暴露也强制清空。Trinity 是长期记忆
系统，此类内容一旦落库（且为加密永久库）即成为持续风险：既伤用户，
也可能污染检索面。本模块在**写路径**（ingest 前、加密落库前）做
轻量规则门控（纯规则，无 LLM，微秒级）：

  - 命中高危组合模式（NEVER_STORE 类别）→ 默认**拒存**：内容根本不
    落库，审计记 action=POLICY_PURGE（Fable 语义的"强制不记"）；
    可选降级为隔离归档（quarantine：落库但不进 active 检索面，
    TRINITY_SENSITIVE_POLICY=quarantine）。
  - 命中中危（单点提及，如"抑郁"出现在普通日记/知识文本）→ 仅打
    metadata["sensitive_scan"] 标记，不阻断写入（避免误伤）。

类别清单（对齐 Fable 隐私禁区 + 本地合规语境，zh/en 双语）：
  minors_pii     未成年身份信息（需 年龄词+身份词 邻近共现，防误伤）
  legal_status   犯罪记录/案底/拘留/移民状态/种姓
  psych_health   精神/心理诊断类（确诊/住院/用药 + 疾病名）
  sexual_history 性史/性经历/性伴侣等强信号
  self_harm      自杀/自残/轻生意图（支持语境也不落库，Anthropic 同款边界）

用法：
    from trinity.security.sensitive import sensitive_scan_enabled, scan_sensitive
    report = scan_sensitive("...我 14 岁女儿在 XX 中学，身份证号 31...")
    if report["flagged"] and report["severity"] == "high":
        # 拒存 + 审计 POLICY_PURGE（默认策略），或隔离归档
开关：TRINITY_SENSITIVE_SCAN=off 关闭扫描（默认 on）；
      TRINITY_SENSITIVE_POLICY=quarantine 把高危从"拒存"降级为"隔离归档"
      （默认 refuse = 拒存）。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List

logger = logging.getLogger("trinity.security.sensitive")

# ── 高危：NEVER_STORE 类别（默认拒存）───────────────────────────────
# (正则, 类别名, 中文标签)。刻意保守：组合强信号才 high，防误伤业务/知识文本。
_HIGH_PATTERNS: List[Any] = [
    # 未成年身份信息：年龄词 + 近邻(40字符内)身份信息词 共现
    (re.compile(
        r"(?:未满\s*1[0-8]\s*岁|不满\s*1[0-8]\s*岁|年?仅?\s*1[0-7]\s*岁|未成\s*年|未成年孩子|"
        r"under\s*1[0-8]|(?:a\s+)?(?:minor|child|kid))[^。\n]{0,40}"
        r"(?:身份证|身份证号|护照|社保卡|学籍号|学校|班级|住址|家庭住址|手机号|电话号码|监护人|家长姓名|"
        r"(?:ID|id|ssn|passport|student\s*id|school|class|address|phone|guardian)\b)"
    ), "minors_pii", "未成年身份信息"),
    # 犯罪/法律敏感状态（个人记录语境强信号）
    (re.compile(
        r"(?i)(?:犯罪记录|刑事案底|案底|被判过刑|判刑入狱|拘留记录|逮捕记录|吸毒记录|"
        r"criminal\s+record|(?:was\s+)?(?:arrested|convicted|imprisoned|incarcerated)|"
        r"immigration\s+status|visa\s+(?:overstay|denied|revoked)|种姓|caste\s+(?:status|system))"
    ), "legal_status", "犯罪记录/法律敏感状态"),
    # 精神/心理诊断（疾病名 + 确诊/住院/用药语境）
    (re.compile(
        r"(?:确诊(?:为)?|被诊断|诊断出|住院(?:治疗|过)|正在服(?:用)?|在(?:接受|进行))(?:了)?"
        r"(?:抑郁症|焦虑症|双相(?:情感)?障碍|精神分裂(?:症)?|创伤后应激(?:障碍)?|强迫症|进食障碍|边缘型人格)|"
        r"(?:diagnosed\s+with|hospitalized\s+for|medication\s+for)\s+"
        r"(?:depression|anxiety|bipolar\s+disorder|schizophrenia|ptsd|ocd|eating\s+disorder|borderline\s+personality)|"
        r"psychiatric\s+(?:diagnosis|hospital|ward)"
    ), "psych_health", "精神/心理诊断"),
    # 性史强信号
    (re.compile(
        r"(?:性史|性经历|性伴侣|sexual\s+history|sexual\s+experience|sexual\s+partner|发生过关系|一夜情)"
    ), "sexual_history", "性史"),
    # 自残/轻生意图（Anthropic 边界：即使求助语境也不落库）
    (re.compile(
        r"(?:想(?:要|着)?自杀|打算自杀|准备自杀|自杀过|自杀了?两次|不想活了|活不下去|想结束(?:自己的)?生命|"
        r"自残|割腕|跳楼|吞(?:药|安眠药)(?:自杀|轻生)?|"
        r"suicid(?:al|e)|want(?:s)?\s+to\s+(?:kill|end)\s+(?:myself|my\s+life)|self[- ]?harm|cutting\s+(?:myself|wrists))"
    ), "self_harm", "自残/轻生意图"),
]

# ── 中危：单点提及（仅标记，不拒存）──────────────────────────────────
_MEDIUM_PATTERNS: List[Any] = [
    (re.compile(r"未成年|未成年人|未满\s*1[0-8]\s*岁|(?:minor|child|kid)(?:\b|s\b)"), "minors_pii"),
    (re.compile(r"(?i)(?:犯罪|判刑|拘留|被捕|案底|criminal|arrest|convict|jail|prison|"
                r"移民|签证|种姓|caste|immigrat|visa)"), "legal_status"),
    (re.compile(r"(?:抑郁|焦虑(?:症)?|双相|精神(?:疾病|障碍|分裂)|强迫症|进食障碍|躁郁|"
                r"depress|anxiet|bipolar|schizo|ptsd|ocd|eating\s+disorder)"), "psych_health"),
    (re.compile(r"(?:性行为|性生活|性取向|性骚扰|sex(?:ual)?\s+(?:life|behavior|orientation|abuse|assault))"), "sexual_history"),
]

# 类别中文名（审计/响应用）
CATEGORY_LABELS: Dict[str, str] = {
    "minors_pii": "未成年身份信息",
    "legal_status": "犯罪记录/法律敏感状态",
    "psych_health": "精神/心理诊断",
    "sexual_history": "性史",
    "self_harm": "自残/轻生意图",
}


def _policy() -> str:
    """敏感策略：refuse（默认，拒存）| quarantine（隔离归档）。"""
    return os.environ.get("TRINITY_SENSITIVE_POLICY", "refuse").strip().lower()


def sensitive_scan_enabled() -> bool:
    """写路径敏感扫描开关（默认 on，off 关闭）。"""
    return os.environ.get("TRINITY_SENSITIVE_SCAN", "on").strip().lower() not in ("off", "0", "false")


def scan_sensitive(content: str) -> Dict[str, Any]:
    """扫描内容中的敏感类别（Fable 隐私禁区对齐）。

    Args:
        content: 待写入的记忆内容（明文，加密前）。

    Returns:
        {
          "flagged": bool,
          "severity": "high"|"medium"|None,
          "policy": "refuse"|"quarantine"|None,   # 仅 high 时有意义
          "categories": [类别英文名, ...],
          "hits": [{"category","severity","pattern","match"}],
          "truncated": bool,
        }
    """
    text = (content or "").strip()
    if not text:
        return {"flagged": False, "severity": None, "policy": None,
                "categories": [], "hits": [], "truncated": False}

    truncated = len(text) > 20000
    hits: List[Dict[str, Any]] = []
    for pattern, category, label in _HIGH_PATTERNS:
        m = pattern.search(text)
        if m:
            hits.append({"category": category, "severity": "high",
                         "pattern": label, "match": m.group(0)[:80]})
    if not hits:
        for pattern, category in _MEDIUM_PATTERNS:
            m = pattern.search(text)
            if m:
                hits.append({"category": category, "severity": "medium",
                             "pattern": CATEGORY_LABELS.get(category, category),
                             "match": m.group(0)[:80]})

    if not hits:
        return {"flagged": False, "severity": None, "policy": None,
                "categories": [], "hits": [], "truncated": truncated}

    severity = "high" if any(h["severity"] == "high" for h in hits) else "medium"
    categories = sorted({h["category"] for h in hits})
    return {
        "flagged": True,
        "severity": severity,
        "policy": _policy() if severity == "high" else None,
        "categories": categories,
        "hits": hits,
        "truncated": truncated,
    }


def policy_block_result(report: Dict[str, Any]) -> Dict[str, Any]:
    """组装拒存时返回给调用方的结果字典（与 ingest 返回结构同形）。"""
    import datetime as _dt
    return {
        "memory_id": "",
        "version_id": None,
        "sha256_hash": None,
        "error": "policy_refused_sensitive",
        "policy": {
            "action": "refuse",
            "severity": report.get("severity"),
            "categories": report.get("categories", []),
            "labels": [CATEGORY_LABELS.get(c, c) for c in report.get("categories", [])],
        },
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "pushed_memories": [],
        "extracted_entities": 0,
        "postprocess": "skipped",
    }
