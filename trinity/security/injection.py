# -*- coding: utf-8 -*-
"""记忆投毒写入过滤（2026-08-24, R8 P1-6；2026-10-06 T5 换皮防御加固）。

背景：OWASP 已将 Memory poisoning persistence 列入 Agentic AI 威胁类别
（AG 类）；间接 prompt injection 可经记忆长期污染 agent 行为——恶意工具
输出/网页内容被写入记忆后，未过滤即持续影响后续会话。

本模块提供写路径的**轻量注入模式扫描**（纯规则，无 LLM，微秒级）：
  - 识别常见指令注入/越权指令/提示词覆盖/系统指令仿冒/数据外泄指令模式
  - 命中时给出危险级别（high/medium）与命中模式，由调用方决定
    （标记 / 拒绝 / 隔离写——默认标记 + 审计，不阻断正常记忆写入）

2026-10-06（T5）加固的三件事，都带反事实判据（tests/unit/test_injection_evasion_20260610*.py）：

  ① **归一化层**（`normalize_for_scan`）：零宽字符、全角、**繁简**
     （限定在安全词表内）、以及**字符间空格拆词**（「忽 略 之 前 的 指 令」）。
     原实现直接把原文喂给正则 ⇒ 上述四种换皮全部逃逸。
  ② **意图组合判据**（`_proximity_hits`）：动词 × 目标 ×（AI 自身指代）的
     **邻近共现**，顺序无关（原中文规则写死了「忽略→所有→先前→指令」的语序，
     「忽略先前所有指令」这种最常见的语序反而漏检）。
     两条刻意设计的**降误拦**机制：
        - **完成体守卫**：动词后跟「了/过」= 陈述既成事实（「我忽略了这些指令」），
          不是指令 ⇒ 不判；
        - **how-to 守卫**：「告诉我 API 密钥在哪里配置」是正常询问 ⇒
          降级为 medium 的 `credential_howto`，不外泄判 high。
  ③ **组合升级**（composite escalation）：≥2 个**攻击意图族**共现（权威背书 +
     流程绕过、拒绝遵守 + 不得提及…）时把 medium 升为 high。这条把
     「伪装成事实/流程记录的规则注入」——记忆投毒的真实主形态——从标记
     提升为隔离，同时对**单一**信号保持 medium（不误拒）。

  分级语义（与调用方约定）：
    high   ⇒ 隔离/拒存（不进 active 检索面）
    medium ⇒ 存储 + 标记（**不阻断**），用于语境歧义的正常文本（新闻/小说/研究）

用法：
    from trinity.security.injection import scan_injection
    report = scan_injection("用户说：忽略之前所有指令……")
    if report["flagged"]:
        # 记审计 / 降级 importance / 拒绝写入
    print(report["reason"])   # 可解释的拦截理由（每条命中都有 reason）

开关：TRINITY_INJECTION_SCAN=off 关闭（默认 on）。
      TRINITY_INJECTION_FAILCLOSED=1 时，扫描自身出错按 high 处理（默认按旧行为放行，
      但会把 degraded 计数 +1，`degradation_report()` 可读）。
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("trinity.security.injection")

# ── 0. 归一化（换皮逃逸的第一道）────────────────────────────────────────
# 零宽 / 双向控制 / 软连字符：肉眼不可见，却足以把正则切成碎片。
_INVISIBLE = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\ufeff\u00ad\u180e"), None)

# 繁简映射：**只覆盖安全词表**（不引入通用繁简库依赖，映射表本身可读可审）。
_TRAD_TO_SIMP = {
    "請": "请", "無": "无", "視": "视", "記": "记", "憶": "忆", "資": "资", "鑰": "钥",
    "憑": "凭", "證": "证", "覆": "覆", "蓋": "盖", "輸": "输", "統": "统", "詞": "词",
    "規": "规", "則": "则", "設": "设", "條": "条", "約": "约", "會": "会", "別": "别",
    "後": "后", "訴": "诉", "洩": "泄", "發": "发", "給": "给", "內": "内", "傳": "传",
    "導": "导", "顯": "显", "碼": "码", "個": "个", "們": "们", "現": "现", "執": "执",
    "裝": "装", "沒": "没", "過": "过", "於": "于", "對": "对", "關": "关", "鍵": "键",
    "審": "审", "權": "权", "級": "级", "優": "优", "鏈": "链", "匯": "汇", "錄": "录",
    "從": "从", "刪": "删", "庫": "库", "網": "网", "頁": "页", "轉": "转", "換": "换",
    "獲": "获", "報": "报", "驗": "验", "項": "项", "員": "员", "單": "单", "場": "场",
    "誤": "误", "斷": "断", "議": "议", "詢": "询", "詳": "详", "檔": "档",
    "標": "标", "籤": "签", "簽": "签", "環": "环", "備": "备", "註": "注", "冊": "册",
}
_TRAD_TABLE = str.maketrans(_TRAD_TO_SIMP)

_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
# 拆词逃逸：单字符（CJK 或 ASCII 字母）之间被塞入空格
_SPLIT_CJK = re.compile(r"(?<=[" + _CJK + r"])[ \t\u3000]+(?=[" + _CJK + r"])")
_SPLIT_ASCII = re.compile(r"\b(?:[A-Za-z][ \t]+){2,}[A-Za-z]\b")


def normalize_for_scan(text: str) -> str:
    """把换皮文本归一成扫描用的规范形（幂等；不改调用方持有的原文）。

    顺序：NFKC（全角/兼容字符） → 删不可见字符 → 繁简（安全词表）
          → 折叠单字符间的分隔空格（CJK 与 ASCII 两种）。
    """
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = t.translate(_INVISIBLE)
    t = t.translate(_TRAD_TABLE)
    t = _SPLIT_CJK.sub("", t)
    t = _SPLIT_ASCII.sub(lambda m: m.group(0).replace(" ", "").replace("\t", ""), t)
    return t


# ── 1. 高危：指令覆盖 / 越权 / 数据外泄 ──────────────────────────────────
_HIGH_PATTERNS: List[Tuple[Any, str]] = [
    # 系统指令仿冒 / 角色越权
    (re.compile(r"(?i)(ignore|forget|disregard)\s+(?:all\s+|any\s+|every\s+|the\s+)?"
                r"(?:previous|prior|above|earlier|preceding)\s+"
                r"(instructions?|prompts?|rules?|context|directives?|guidelines?)"),
     "instruction_override"),
    (re.compile(r"(?i)you\s+are\s+now\s+(?:the\s+)?(?:system|admin|root|superuser|god)"),
     "role_usurpation"),
    (re.compile(r"(?i)(system|developer|assistant)\s*:\s*(?:you\s+)?(?:must|should|will)"),
     "role_spoof"),
    # 提示词覆盖
    (re.compile(r"(?i)(?:new|updated|override|replace|overwrite|change)\s+(?:your\s+|the\s+)?"
                r"(?:instructions|prompt|system\s+prompt|directives)"),
     "prompt_override"),
    (re.compile(r"(?i)treat\s+(?:this|these|the\s+following)\s+as\s+(?:your\s+)?"
                r"(?:new\s+|core\s+|only\s+|authoritative\s+|permanent\s+)*"
                r"(?:instructions?|prompts?|rules?|directives?|policy|policies|guidelines?|"
                r"system\s+prompt|operating\s+procedure)"),
     "prompt_override"),
    (re.compile(r"(?i)disregard\s+(?:the\s+)?(?:system\s+)?(?:prompt|instructions)"),
     "prompt_disregard"),
    # 数据外泄
    (re.compile(r"(?i)(?:exfiltrate|leak|send|upload|post|transmit)\s+"
                r"(?:(?:all|every|any|your)\s+){0,3}"
                r"(?:data|memory|memories|secrets?|tokens?|keys?|files?)"
                r"(?:\s+(?:data|content|entries|files))?\s+(?:to|via|through|at)\s+"
                r"(?:an?\s+)?(?:url|server|endpoint|webhook|https?://)"),
     "data_exfiltration"),
    (re.compile(r"(?i)print\s+(?:(?:all|every|your)\s+){0,2}"
                r"(?:memory|memories|secret|secrets|token|tokens|password|api\s*keys?)"),
     "secret_dump"),
    # 恶意指令
    (re.compile(r"(?i)(delete|drop|wipe|erase|clear)\s+(all\s+)?(memory|memories|database|data)"),
     "destructive_command"),
    (re.compile(r"(?i)execute\s+(arbitrary|shell|code|commands?)\s*(without|no)\s*"
                r"(asking|permission|approval|verification)"),
     "arbitrary_execution"),
    # 越狱人设（DAN 类）—— 必须带**角色指派框架**才判定：光出现 "DAN" 三个字母
    # 在安全文献/小说里是正常的（实测反例：ben-29「越狱提示综述（含 DAN 案例）」）。
    (re.compile(r"(?:你(?:现在)?(?:就)?是|你将是|扮演|假装你是|请你(?:成为|作为)|从(?:现在|此刻)起你?是|"
                r"act\s+as|you\s+are\s+now|pretend\s+to\s+be|from\s+now\s+on\s+you\s+are)\s*"
                r"(?:一个)?\s*(?:DAN|dan|do\s+anything\s+now|开发者模式|越狱模式|无限制模式)"),
     "jailbreak_persona"),
    (re.compile(r"(?:开启|进入|切换到|启用)\s*(?:DAN|dan|开发者模式|越狱模式|无限制)\s*(?:模式)?"),
     "jailbreak_persona"),
]

# ── 中文改写注入（EXECUTION 568，EXECUTION 564 evasion 层 canary 归零后补齐）─
_ZH_HIGH_PATTERNS: List[Tuple[Any, str]] = [
    (re.compile(r"(?:忽略|无视|忘记|不要理会|别管)(?:所有|一切|任何)?(?:先前|之前|以上|此前)?"
                r"(?:指令|指示|提示|规则)"),
     "instruction_override"),
    (re.compile(r"你(?:现在|将|就是)?(?:是)?(?:系统管理员|管理员|超级用户|最高权限|root)"),
     "role_usurpation"),
    (re.compile(r"(?i)(?:将|把|请|尽快)?(?:所有|全部|一切|任何)?(?:记忆|数据|密钥|凭据|token|api\s*密钥)"
                r"(?:数据|内容|库)?(?:发送|上传|导出|传输|提交|同步)\s*(?:到|至|给)?\s*"
                r"(?:https?://|外部|远程|服务器|接口|webhook|网址)"),
     "data_exfiltration"),
    (re.compile(r"(?i)(?:发送|上传|导出|传输|提交|同步)(?:所有|全部|一切)?(?:数据|记忆|内容)\s*"
                r"(?:到|至|给)?\s*(?:https?://|外部|远程|服务器|接口|webhook)"),
     "data_exfiltration"),
    (re.compile(r"(?i)(?:打印|输出|列出|显示|回复)(?:所有|全部|一切)?\s*"
                r"(?:api\s*密钥|密钥|token|密码|凭据|口令)"),
     "secret_dump"),
    (re.compile(r"(?:覆盖|改写|替换|无视|推翻)(?:你(?:的)?)?(?:一切|所有|先前|之前|原有)?"
                r"(?:指令|指示|提示|规则|设定|要求)"),
     "prompt_override"),
]

_ZH_MEDIUM_PATTERNS: List[Tuple[Any, str]] = [
    (re.compile(r"(?:不要|切勿|别|禁止)(?:向|跟|给)(?:任何人|别人|他人|用户)(?:提及|告诉|透露|提起|说出)"),
     "manipulation_directive"),
    (re.compile(r"(?:假装|装作|扮演)(?:自己|你)?(?:是|为)?(?:一个)?(?:没有|无|不受)(?:任何)?"
                r"(?:限制|规则|约束|束缚)"),
     "pretense_injection"),
    (re.compile(r"(?:作为|假装)(?:一个)?(?:没有|无)(?:任何)?(?:限制|约束|规则)的(?:ai|助手|智能体|机器人)"),
     "jailbreak_hint"),
    (re.compile(r"(?:永远|长期|每轮|每次)[^。\n]{0,10}(?:记住|都要)[^。\n]{0,24}(?:输出|回复|先说|优先|打印)"),
     "persistence_request"),
    # T5：拒绝遵守规则（单点判 medium —— 「旧规定不必遵守了」可能是正常流程变更）。
    # 中文本序与倒序都要覆盖（宾语常在动词前）。
    (re.compile(r"(?:不要|不必|无需|不需|不用|不得|无需再)(?:再)?遵守[^。\n]{0,12}"
                r"(?:规则|规定|指令|要求|约定|限制|约束|准则|policy|rules?)"),
     "rule_defiance"),
    (re.compile(r"(?:规则|规定|指令|要求|约定|限制|约束|准则|policy|rules?)[^。\n]{0,8}"
                r"(?:不要|不必|无需|不需|不用|不得)(?:再)?(?:去)?遵守"),
     "rule_defiance"),
    # T5：不得在回答/回复中提及（隐蔽性要求，AI 行为导向）
    (re.compile(r"(?:不要|别|禁止|不得)[^。\n]{0,8}(?:在)?(?:回答|回复|输出|对话|上面|其中)"
                r"[^。\n]{0,8}(?:提起|提及|提到|说出|透露|出现)"),
     "anti_disclosure"),
    # T5：无约束人设（**必须**与 AI 指代共现才判，避免「该功能不受限制」误伤）
    (re.compile(r"(?:不受|没有|无|不带)(?:任何)?(?:限制|约束|束缚|规则)[^。\n]{0,12}"
                r"(?:的)?(?:ai|助手|智能体|模型|机器人|assistant|agent)"),
     "unrestricted_persona"),
    (re.compile(r"(?:助手|AI|人工智能|模型|智能体|机器人|系统)[^。\n]{0,12}"
                r"(?:不受|没有|无|不带)(?:任何)?(?:限制|约束|束缚|规则)"),
     "unrestricted_persona"),
]

# ── 中危：操纵 / 隐藏 / 持久化 ─────────────────────────────────────────
_MEDIUM_PATTERNS: List[Tuple[Any, str]] = [
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
    (re.compile(r"(?i)as\s+(?:an?\s+)?(?:ai|assistant|agent|language\s+model)[^.]{0,40}"
                r"(?:with\s+no|without\s+any|free\s+of)\s+(?:rules|restrictions|limitations|constraints)"),
     "jailbreak_hint"),
]

# ── 新增（H1-8，2026-09-13）：权威背书 + 持久化指令 —— 记忆投毒的主形态 ──────
_ZH_AUTHORITY_PATTERNS: List[Tuple[Any, str]] = [
    (re.compile(r"(?:管理员|官方|上级|公司|组织|法务|财务|IT)(?:已|均|曾)?(?:确认|授权|批准|认可|指定|通知|公告|发文)"),
     "authority_claim"),
    (re.compile(r"(?:该|此|本)(?:结论|说法|做法|流程|记录|约定)(?:已)?(?:由|经)?"
                r"(?:管理员|官方|上级|系统|公司|团队|制度)"),
     "authority_claim"),
    (re.compile(r"(?:管理员|官方)(?:授权|确认)[，,、]?"),
     "authority_claim"),
    # T5：把「这是本系统/团队/公司的约定」当权威背书的形态（换皮常见框架）
    (re.compile(r"(?:本系统|本团队|本项目|本组|内部|团队|系统)(?:的)?(?:约定|规定|惯例|共识|要求)"),
     "authority_claim"),
]
_ZH_PERSISTENCE_PATTERNS: List[Tuple[Any, str]] = [
    (re.compile(r"(?:后续|以后|今后|未来|每次|每轮)[^。\n]{0,14}(?:始终|一律|都|均|必须)[^。\n]{0,12}"
                r"(?:执行|回答|处理|遵循|照做|按此)"),
     "persistence_directive"),
    (re.compile(r"请[^。\n]{0,10}(?:始终|一律|都|必须)[^。\n]{0,10}(?:执行|回答|处理|遵循|照做|按此)"),
     "persistence_directive"),
    (re.compile(r"(?:一律|一概|统统)[^。\n]{0,8}(?:免|无需|不用|跳过|不必)[^。\n]{0,8}"
                r"(?:审批|申请|流程|确认|审核|验证)"),
     "process_bypass"),
    (re.compile(r"(?:覆盖|取代|替代|作废|推翻)(?:既有|现有|原有|原先|此前|之前)?"
                r"(?:流程|做法|规定|规则|政策|制度)"),
     "process_override"),
    (re.compile(r"(?:任何|其他|其它)(?:说法|做法|流程|规定)(?:都)?(?:已)?(?:过时|失效|作废|不正确|错误)"),
     "invalidate_others"),
    (re.compile(r"(?:优先级|优先权)最高"),
     "priority_claim"),
]
_AUTHORITY_EN_PATTERNS: List[Tuple[Any, str]] = [
    (re.compile(r"(?i)(?:admin|administrator|official|management|company|it\s+department)\s+"
                r"(?:has\s+)?(?:confirmed|approved|authorized|mandated|verified|notified)"),
     "authority_claim"),
]
_PERSISTENCE_EN_PATTERNS: List[Tuple[Any, str]] = [
    (re.compile(r"(?i)(?:always|from\s+now\s+on|in\s+the\s+future|every\s+time)[^.!?\n]{0,40}"
                r"(?:follow|answer|respond|comply|do\s+this|use\s+this)"),
     "persistence_directive"),
    (re.compile(r"(?i)(?:skip|bypass|override|replace)\s+(?:the\s+)?"
                r"(?:approval|process|procedure|policy|workflow)"),
     "process_override"),
    (re.compile(r"(?i)(?:any|all)\s+other\s+(?:claims?|statements?|processes?|explanations?)\s+"
                r"(?:are|is)\s+(?:outdated|obsolete|wrong|incorrect)"),
     "invalidate_others"),
]

# ── T5：意图组合判据（顺序无关的邻近共现）─────────────────────────────────
# 动机（实测）：原中文规则写死语序「忽略→(所有)→(先前)→(指令)」，
# 「忽略先前所有指令」这种**最自然**的中文语序反而逃逸（见交付报告 §1 样本 ev11-07）。
_OVERRIDE_VERB_RE = re.compile(
    r"(?:忽略|忽视|无视|忘掉|忘记|不要理会|别理会|不要\s*再?\s*遵守|不必遵守|无需遵守|"
    r"不要\s*管|跳过|绕过|推翻|作废|覆盖|取代|替换)")
_TARGET_RE = re.compile(
    r"(?:系统提示词|系统提示|系统指令|提示词|指令|指示|设定|约束|限制|规则|要求|准则|"
    r"行为规范|policy|instructions?|prompts?|rules?|directives?|guidelines?)")
_AGENT_REF_RE = re.compile(
    r"(?:你|您|自己|系统|AI|助手|模型|智能体|一切|所有|全部|任何|以上|上述|上文|"
    r"your|you|all|any|every|previous|prior|earlier|above|system)")

_DUMP_VERB_RE = re.compile(
    r"(?:输出|回复|回答|打印|列出|显示|导出|展示|提供|告诉|复述|泄露|说出|"
    r"print|output|reveal|show|list|dump|tell)")
_DUMP_TARGET_RE = re.compile(
    r"(?:系统提示词|系统提示|系统指令|内部记忆|全部记忆|所有记忆|记忆内容|记忆库|记忆数据|"
    r"内部资料|凭据|密钥|密\u3000*钥|口令|密码|token|api\s*密钥|"
    r"system\s*prompt|credentials?|api\s*keys?|passwords?)")
# how-to 守卫：问「怎么配置/在哪里」是正常知识问询，不是外泄请求
_HOWTO_RE = re.compile(
    r"(?:如何|怎么|怎样|哪里|在哪|哪儿|位置|路径|配置|设置|申请|轮换|更换|重置|"
    r"环境变量|文档|指南|教程|how\s+to|where|configure|setup)")

#: 参与「组合升级」的攻击意图族（单一出现仍为 medium）
_ATTACK_FAMILIES = frozenset({
    "authority_claim", "process_bypass", "process_override", "priority_claim",
    "invalidate_others", "rule_defiance", "anti_disclosure", "persistence_directive",
    "unrestricted_persona", "jailbreak_persona", "memory_dump", "secret_dump",
    "destructive_command", "data_exfiltration", "instruction_override",
})

_REASON: Dict[str, str] = {
    "instruction_override": "指令覆盖：要求丢弃/改写既有指令或规则（指向 AI 自身行为准则）",
    "role_usurpation": "角色越权：宣称自己已成为系统/管理员/root",
    "role_spoof": "系统指令仿冒：伪造 system/developer 口吻下达要求",
    "prompt_override": "提示词覆盖：要求替换原有提示词或以其为新准则",
    "prompt_disregard": "要求无视系统提示",
    "data_exfiltration": "数据外泄：要求把记忆/凭据送往外部地址",
    "secret_dump": "凭据外泄：要求输出密钥/口令/令牌",
    "memory_dump": "记忆外泄：要求输出内部记忆内容",
    "destructive_command": "破坏性指令：删除/清空记忆或数据库",
    "arbitrary_execution": "无授权任意执行",
    "manipulation_directive": "操纵指令：要求对他人隐瞒或不提及",
    "concealment": "隐藏指令：要求把内容藏起来",
    "pretense_injection": "伪装成无约束角色",
    "jailbreak_hint": "越狱提示：无约束 AI 语用",
    "jailbreak_persona": "越狱人设（DAN/开发者模式/越狱模式）",
    "unrestricted_persona": "无约束人设：要求以不受限制的身份行动",
    "persistence_request": "持久化请求：要求长期记住并影响后续行为",
    "persistence_directive": "持久化指令：要求此后一律按此执行",
    "conditional_override": "条件式覆盖：回答时改用另一套规则",
    "authority_claim": "权威背书：声称已获管理员/官方/系统约定授权",
    "process_bypass": "流程绕过：要求免审批/跳过确认",
    "process_override": "流程改写：要求覆盖既有流程",
    "invalidate_others": "作废其他说法：宣称其它说法已过时",
    "priority_claim": "自称优先级最高",
    "rule_defiance": "拒绝遵守规则（单点为 medium：可能是正常流程变更）",
    "anti_disclosure": "要求不得在回答中提及",
    "credential_howto": "仅询问凭据如何配置/在哪 — 非外泄请求，降级为标记",
}

# ── 扫描核心 ────────────────────────────────────────────────────────────


def _proximity_hits(
    text: str,
    verb_re: Any,
    target_re: Any,
    name: str,
    *,
    window: int = 12,
    require_ref_re: Any = None,
    ref_window: int = 16,
) -> List[Dict[str, str]]:
    """动词 × 目标 的邻近共现（**顺序无关**），带两条降误拦守卫。

    - 完成体守卫：动词后紧跟「了/过」⇒ 陈述既成事实，不是指令（不判）。
    - 指代守卫：`require_ref_re` 给定时，命中窗口内必须出现 AI 自身指代
      （你/系统/所有/任何…）⇒ 排除「替换了之前的命名规则」这类工程文本。
    """
    out: List[Dict[str, str]] = []
    for mv in verb_re.finditer(text):
        if text[mv.end():mv.end() + 1] in ("了", "过"):
            continue
        seg = text[mv.end():mv.end() + window]
        mt = target_re.search(seg)
        if not mt:
            continue
        lo, hi = mv.start(), mv.end() + mt.end()
        if require_ref_re is not None:
            ctx = text[max(0, lo - ref_window):hi + ref_window]
            if not require_ref_re.search(ctx):
                continue
        out.append({
            "pattern": name,
            "match": text[lo:hi][:80],
            "verb": mv.group(0),
            "target": mt.group(0),
            "_lo": lo,
            "_hi": hi,
        })
    return out


def _dedupe(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for h in hits:
        key = (h.get("pattern"), str(h.get("match"))[:40])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def scan_injection(content: str) -> Dict[str, Any]:
    """扫描内容中的记忆投毒/注入模式。

    Args:
        content: 待写入的记忆内容（原文；函数内部自行归一化）。

    Returns:
        {
          "flagged": bool,          # 是否命中任一模式
          "severity": "high"|"medium"|None,
          "hits": [{"pattern","severity","match","reason"}, ...],
          "truncated": bool,        # 内容超长被截断检测
          "reason": str,            # 可解释的总体判定理由（空串=未命中）
          "escalated": bool,        # 是否由「组合升级」把 medium 升为 high
          "normalized": bool,       # 归一化是否改变了文本（换皮证据）
        }
    """
    raw = str(content or "")
    text = normalize_for_scan(raw).strip()
    empty = {"flagged": False, "severity": None, "hits": [], "truncated": False,
             "reason": "", "escalated": False, "normalized": False}
    if not text:
        return empty

    # 超长内容截断检查（注入常藏于长文本尾部）
    truncated = len(text) > 20000
    normalized = text != raw.strip()

    hits: List[Dict[str, Any]] = []

    def _add(pattern: str, severity: str, match: str) -> None:
        hits.append({"pattern": pattern, "severity": severity,
                     "match": str(match)[:80], "reason": _REASON.get(pattern, pattern)})

    for pattern, name in _HIGH_PATTERNS + _ZH_HIGH_PATTERNS:
        m = pattern.search(text)
        if m:
            _add(name, "high", m.group(0))

    # 意图组合（顺序无关）：覆盖/越权 与 外泄/回吐
    for h in _proximity_hits(text, _OVERRIDE_VERB_RE, _TARGET_RE, "instruction_override",
                             require_ref_re=_AGENT_REF_RE):
        _add("instruction_override", "high", h["match"])
    for h in _proximity_hits(text, _DUMP_VERB_RE, _DUMP_TARGET_RE, "secret_dump", window=10):
        # how-to 守卫：问「如何配置/在哪里」不是外泄请求 ⇒ 降级为标记
        ctx = text[max(0, h["_lo"] - 20): h["_hi"] + 20]
        if _HOWTO_RE.search(ctx):
            _add("credential_howto", "medium", h["match"])
        else:
            name = "memory_dump" if re.search(r"记忆", h["match"]) else "secret_dump"
            _add(name, "high", h["match"])

    for pattern, name in _MEDIUM_PATTERNS + _ZH_MEDIUM_PATTERNS:
        m = pattern.search(text)
        if m:
            _add(name, "medium", m.group(0))

    # H1-8：权威背书 × 持久化/流程改写 的**组合判据**。
    _auth: List[Tuple[str, Any]] = []
    _dir: List[Tuple[str, Any]] = []
    for pat, n in _ZH_AUTHORITY_PATTERNS + _AUTHORITY_EN_PATTERNS:
        m = pat.search(text)
        if m:
            _auth.append((n, m))
    for pat, n in _ZH_PERSISTENCE_PATTERNS + _PERSISTENCE_EN_PATTERNS:
        m = pat.search(text)
        if m:
            _dir.append((n, m))
    _composite = bool(_auth) and bool(_dir)
    for n, m in _auth + _dir:
        _add(n, "high" if _composite else "medium", m.group(0))

    hits = _dedupe(hits)
    if not hits:
        return {"flagged": False, "severity": None, "hits": [], "truncated": truncated,
                "reason": "", "escalated": False, "normalized": normalized}

    # ── 组合升级：≥2 个攻击意图族共现 ⇒ medium 升 high ──────────────────
    escalated = False
    attack_families = {h["pattern"] for h in hits
                       if h["pattern"] in _ATTACK_FAMILIES}
    if len(attack_families) >= 2:
        for h in hits:
            if h["pattern"] in _ATTACK_FAMILIES and h["severity"] == "medium":
                h["severity"] = "high"
                h["reason"] = h["reason"] + "（与其它攻击意图族共现 ⇒ 升级为隔离）"
                escalated = True

    severity = "high" if any(h["severity"] == "high" for h in hits) else "medium"
    fams = sorted({h["pattern"] for h in hits if h["severity"] == "high"}) or \
        sorted({h["pattern"] for h in hits})
    reason = "%s｜命中族：%s" % (
        "high（隔离/拒存）" if severity == "high" else "medium（存储+标记）",
        "+".join(fams))
    if escalated:
        reason += "｜组合升级生效（≥2 攻击意图族）"
    return {"flagged": True, "severity": severity, "hits": hits, "truncated": truncated,
            "reason": reason, "escalated": escalated, "normalized": normalized}


def injection_scan_enabled() -> bool:
    """写路径注入扫描开关（默认 on，off 关闭）。"""
    return os.environ.get("TRINITY_INJECTION_SCAN", "on").strip().lower() not in ("off", "0", "false")


def injection_failclosed() -> bool:
    """扫描自身出错时是否按 high 处理（默认 off，保持既有可用性优先语义）。"""
    return os.environ.get("TRINITY_INJECTION_FAILCLOSED", "0").strip().lower() in (
        "1", "on", "true", "yes")


# ── 静默降级可观测化（T5）──────────────────────────────────────────────
# 复核结论：本模块原有两处**静默降级**——① `adapter_write_guard` 扫描抛错时
# 返回 scanned=False/isolate=False（等于放行，只写一条 logger.warning）；
# ② `injection_scan_enabled()` 为 off 时同样静默放行。二者都不改变"看起来
# 有防御"的表象。处置：**不改变默认可用性语义**，但把降级事件变成可读数
# （计数器 + 返回值里的 degraded/exempt 字段），并可显式切 fail-closed。
_DEGRADATION: Dict[str, int] = {"scan_error": 0, "scan_off": 0, "failclosed_taken": 0}


def degradation_report() -> Dict[str, int]:
    """读降级计数器（进程级；用于体检/审计，不落盘）。"""
    return dict(_DEGRADATION)


# ── H1-8 defense-in-depth（2026-09-13）：适配器层写入守卫 ────────────────────
# 动机（实测，非推测）：注入扫描自 R8 起**只挂在 client.ingest** 上，而生产代码里
# 至少 10 条路径**直写 adapter.store_memory / ingest_batch**。这些内容多为
# **LLM 从外部内容派生**（巩固/压缩/抽取），正是 MINJA 指出的威胁面
# （arXiv:2503.03704）——外部内容经 LLM 洗一遍后从"侧门"进入 active 记忆。
# 处置：把同一判据下沉到适配器层（PG + SQLite 的 store_memory / ingest_batch），
# high → 直接以 archived 落库（不进检索面），medium → 打 metadata 标记。
# 豁免（避免污染基准与镜像）：评测命名空间、密文（enc:v1:，镜像/回填）、
# 显式 metadata.injection_scan_exempt=True 的可信内部写入。
_EVAL_CATEGORIES = {"benchmark", "lme", "stress-test", "locomo", "longmemeval", "beam"}
_EVAL_AGENT_PREFIXES = ("eval-", "ablate", "bench-", "stress-")
_EVAL_TAGS = {"lme", "locomo", "benchmark", "bench", "eval", "ablate", "stress-test", "stress"}


def _eval_namespace(agent_id: Any, category: Any, tags: Any) -> bool:
    """评测/压测命名空间判定（与 core/client/_ingestion.py 同口径 + 标签层）。"""
    try:
        c = str(category or "").lower()
        a = str(agent_id or "").lower()
        if c in _EVAL_CATEGORIES or a.startswith(_EVAL_AGENT_PREFIXES):
            return True
        for t in (tags or []):
            if str(t).strip().lower() in _EVAL_TAGS:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def adapter_write_guard(
    content: str,
    *,
    agent_id: Any = "default",
    category: Any = "general",
    tags: Any = None,
    metadata: Any = None,
) -> Dict[str, Any]:
    """适配器层写入守卫（H1-8 defense-in-depth）。

    Returns:
        {
          "scanned": bool,     # 是否真的扫了
          "flagged": bool,
          "severity": "high"|"medium"|None,
          "isolate": bool,     # True ⇒ 调用方应以 status='archived' 落库
          "patterns": [str],
          "exempt": str|None,  # 未扫描的原因
          "reason": str,       # 可解释理由（T5）
          "degraded": bool,    # 未扫描/扫描失败 = 防御面降级（T5 新增，可观测）
        }
    """
    _skip = {"scanned": False, "flagged": False, "severity": None,
             "isolate": False, "patterns": [], "exempt": None,
             "reason": "", "degraded": True}
    try:
        if not injection_scan_enabled():
            _DEGRADATION["scan_off"] += 1
            return dict(_skip, exempt="scan_off", reason="注入扫描被 TRINITY_INJECTION_SCAN=off 关闭")
        text = str(content or "")
        if text.startswith("enc:v1:"):
            # 密文（SQLite 加密行 / 镜像回填）不可扫，且不应改变其状态
            return dict(_skip, exempt="ciphertext", degraded=False,
                        reason="密文行不可扫（且不应改变状态）")
        if isinstance(metadata, dict) and metadata.get("injection_scan_exempt") is True:
            return dict(_skip, exempt="exempt_flag", degraded=False,
                        reason="显式 exemption（可信内部写入）")
        if _eval_namespace(agent_id, category, tags):
            return dict(_skip, exempt="eval_namespace", degraded=False,
                        reason="评测/压测命名空间（避免污染基准；**注意这是确定性豁免**）")
        rep = scan_injection(text)
        sev = rep.get("severity")
        return {
            "scanned": True,
            "flagged": bool(rep.get("flagged")),
            "severity": sev,
            "isolate": sev == "high",
            "patterns": sorted({h["pattern"] for h in rep.get("hits", [])}),
            "exempt": None,
            "reason": rep.get("reason") or "",
            "degraded": False,
        }
    except Exception as exc:  # noqa: BLE001 —— 默认仍是可用性优先（见 docstring）
        _DEGRADATION["scan_error"] += 1
        logger.warning("adapter_write_guard 失败（放行未扫描写入）: %s", exc)
        if injection_failclosed():
            _DEGRADATION["failclosed_taken"] += 1
            return {"scanned": False, "flagged": True, "severity": "high",
                    "isolate": True, "patterns": [], "exempt": "scan_error_failclosed",
                    "reason": "扫描失败且 TRINITY_INJECTION_FAILCLOSED=1 ⇒ 按 high 隔离",
                    "degraded": True}
        return dict(_skip, exempt="scan_error",
                    reason="扫描失败：默认放行（可观测降级；如需 fail-closed 见 TRINITY_INJECTION_FAILCLOSED）")
