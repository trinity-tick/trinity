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
  legal_status   犯罪记录/案底/拘留/移民状态/种姓（**T5：补中文「监狱」族**）
  psych_health   精神/心理诊断类（确诊/住院/用药/**T5：第一人称患病** + 疾病名）
  sexual_history 性史/性经历/性伴侣等强信号
  self_harm      自杀/自残/轻生意图（支持语境也不落库，Anthropic 同款边界）

── 三档语义（T5 明确，`policy_action()` / `apply_policy()`）────────────────
  ① **存（store）**     severity=None ⇒ 原样入库，无标记。
  ② **脱敏存（redact）** severity=medium ⇒ 入库 + metadata 标记，
     并**掩码可识别标识符**（身份证/手机/学籍/邮箱/银行卡；`redact_identifiers()`）。
     这一档是「语境歧义」的归宿：新闻/小说/研究里的「监狱」「抑郁」进这一档，
     **绝不拒存**。
  ③ **拒存（refuse）**   severity=high ⇒ 内容不落库，审计 POLICY_PURGE；
     `TRINITY_SENSITIVE_POLICY=quarantine` 时降级为**隔离归档**（落库但 archived，
     不进检索面，审计 POLICY_QUARANTINE）。
  边界判据：**个人语境 ⇒ 拒存；公共语境（新闻/小说/研究/科普）⇒ 脱敏存**。
  两个反事实钉在 tests/unit/test_sensitive_tiers_20261006.py：
    - 「我弟弟去年进了监狱」→ high（拒存）｜「新闻：该监狱超员被通报」→ medium（存）
    - 「我得了抑郁症」→ high（拒存）｜「研究显示抑郁症患者睡眠结构差异」→ medium（存）

用法：
    from trinity.security.sensitive import sensitive_scan_enabled, scan_sensitive
    report = scan_sensitive("...我 14 岁女儿在 XX 中学，身份证号 31...")
    if report["flagged"] and report["severity"] == "high":
        # 拒存 + 审计 POLICY_PURGE（默认策略），或隔离归档
    tier = report["action"]          # 'store' | 'redact' | 'refuse'
开关：TRINITY_SENSITIVE_SCAN=off 关闭扫描（默认 on）；
      TRINITY_SENSITIVE_POLICY=quarantine 把高危从"拒存"降级为"隔离归档"
      （默认 refuse = 拒存）。
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trinity.security.sensitive")

# ── 高危：NEVER_STORE 类别（默认拒存）───────────────────────────────
# (正则, 类别名, 中文标签)。刻意保守：组合强信号才 high，防误伤业务/知识文本。
# ── I10/t70：高危类别的**个人语境锚**（防英文散文/知识文本误报）──────────────
# 症状（t70 实测量化，LongMemEval-S 前 5 万条消息）：**121 条被判 high ⇒ 拒存**，
# 逐条人判后 **120 条是误报**（precision ≈ 0.8%）：剧名 `Arrested Development` ×7、
# `Nelson Mandela was imprisoned`、议题词 `caste system` / `immigration status`、
# 研究/新闻里的 `suicide rates`、产品文案 `kid-friendly` …
# 根因（**与下面这行注释的设计意图对照**：`刻意保守：组合强信号才 high，防误伤业务/知识文本`）：
# 英文侧实现的是**裸词**，个人语境这条要求**根本没写进去**：
#   ① `(?:was\s+)?(?:arrested|convicted|imprisoned|incarcerated)` —— `was ` 可省 ⇒ 裸词即命中；
#   ② `caste\s+(?:status|system)` / `immigration\s+status` —— 它们是**议题**，不是个人记录；
#   ③ minors 侧 `(?:ID|id|…)` **缺前导 `\b`** ⇒ `id` 命中了 `did` / `kiddo` 里的子串，
#      且 `school|class|address|phone` 是**泛词**（"children at school" 也命中）。
# ⇒ 这不是"阈值太松"，是**设计意图未被实现**。
# 修法：**要求个人语境锚**（第一/第二人称或亲属词，中英双语；锚在同句 30 字符内）、
#       minors 侧补词边界 + 只认「具体号码」或「带所有格的详情词」。
# 开关：`TRINITY_HIGH_PERSONAL_CONTEXT`（**默认 on**；设 `off/0` 逐字回到裸词行为）。
_PERSONAL_ANCHOR = (
    r"(?:我|我的|我家|本人|自己|我们|你|你的|你们|"
    r"家人|家里|亲戚|朋友|同事|伴侣|老公|老婆|丈夫|妻子|"
    r"弟弟|哥哥|姐姐|妹妹|父亲|母亲|爸爸|妈妈|儿子|女儿|孩子|"
    r"I|I'm|I've|me|my|mine|myself|we|our|ours|you|your|yours)"
)

# ⚠️ 第二次修正（实测教训，见 HIGH-CATEGORY-FALSE-POSITIVES.md §3）：
# 上面那种"**30 字符内出现人称词**"的锚在**对话体**里几乎无判别力 —— LongMemEval 的文本是
# AI 应答，"I"、"you"、"my" 到处都是（"I'm glad you're interested in … caste system" 也会命中）
# ⇒ 首轮修完仍有 **66/121** 被判 high。改为**结构相邻**锚：
#   ① 主语代词 + 系动词/助动词(+至多一个时间副词) + 个人记录动词 —— `I was arrested` ✓，
#      `I'm glad you…convicted` ✗（中间夹实词）；`he was eventually arrested` ✗（eventually 不在表内）；
#   ② **第一人称所有格** + 记录/议题型名词 —— `my criminal record` / `my immigration status` ✓，
#      `their immigration status`（政策文）✗；
#   ③ 其它所有格 + `criminal record` —— `his criminal record` ✓（个人属性）。
_SUBJ = r"(?:I|we|he|she|they|我|我们|他|她|他们|本人|自己)"
#: **只用第一人称**做主语的锚：实测（t70）语料里的 `he was arrested` **全部**是虚构叙事
#: （#110/#111 是小说情节）⇒ 3rd-person 裸代词在对话语料里与"新闻/小说"不可区分；
#: 第三人称的真实场景由 ④ 的**关系人**形式覆盖（`my brother was arrested`）。
_SUBJ_1P = r"(?:I|we|我|我们|本人|自己)"
_AUX = r"(?:was|were|am|is|are|be|been|being|have|has|had|got|get)?"
_ADV = r"(?:recently|once|twice|again|just|previously|earlier|ever|never|actually)?"
_RELATION = (r"(?:brother|sister|son|daughter|father|mother|husband|wife|friend|partner|"
             r"kid|child|弟弟|哥哥|姐姐|妹妹|父亲|母亲|儿子|女儿|朋友|老公|老婆)")
_LEGAL_EN_PERSONAL = (
    r"(?i)(?:"
    # ① **第一人称**主语 + 系动词/助动词(+至多一个时间副词) + 个人记录动词：`I was arrested` ✓
    r"(?:" + _SUBJ_1P + r"\s*" + _AUX + r"\s*" + _ADV + r"\s*"
    r"(?:arrested|convicted|imprisoned|incarcerated)\b)"
    # ② 第一人称所有格 + 记录/议题型名词：`my criminal record` / `my immigration status` ✓（`their …` ✗）
    r"|(?:(?:my|our|我|我的|我们|我们的)\s*(?:criminal\s+record|arrest|conviction|imprisonment|"
    r"immigration\s+status|caste\s+(?:status|system)|visa\s+(?:overstay|denied|revoked)))"
    # ③ 其它所有格 + `criminal record`（个人属性）：`his criminal record` ✓
    r"|(?:(?:his|her|their|他|她|他们)(?:的)?\s*(?:criminal\s+record|案底))"
    # ④ 关系人（我的/他的 弟弟/儿子/朋友…）+ (has/was) + 记录名词或动词：`My brother has a criminal record` ✓
    r"|(?:(?:my|our|his|her|their|我|我的|我们|我们的)\s*" + _RELATION + r"\s*(?:'s|s')?\s*"
    r"(?:was|were|is|are|has|had|have|got|with)?\s*(?:a|an)?\s*"
    r"(?:arrested|convicted|imprisoned|incarcerated|criminal\s+record|arrest|conviction|"
    r"imprisonment|record))"
    r")"
)


def high_personal_context_required() -> bool:
    """高危类别是否要求**个人语境锚**（I10/t70）。**默认 on**。

    `off` / `0` / `false` / `no` ⇒ 逐字回到改动前的"裸词即 high"行为（用于对照与回滚）。
    """
    return os.environ.get("TRINITY_HIGH_PERSONAL_CONTEXT", "on").strip().lower() not in (
        "off", "0", "false", "no")


#: 英文/议题型高危险词（**裸词**版本）—— 只用于「缺语境」留痕与回滚对照，不再单独判 high
#: ⚠️ 必须带 `(?i)`：原实现是 `(?i)` 开头的**大小写不敏感**匹配（`Arrested Development` 就是这么命中的）；
#:    回滚态若漏了它，就回不到"改动前"，实测漏了 7 条（剧名 `Arrested`）。
_LEGAL_EN_BARE = (
    r"(?i)(?:criminal\s+record|(?:was\s+)?(?:arrested|convicted|imprisoned|incarcerated)|"
    r"immigration\s+status|visa\s+(?:overstay|denied|revoked)|caste\s+(?:status|system))"
)
#: 加上**必需**的个人语境锚（结构相邻，见上方第二次修正的说明）
_LEGAL_EN_ANCHORED = _LEGAL_EN_PERSONAL
#: 留痕用：裸词命中检测（与 high 判定**无关**，只用来证明"这条本该 high 但缺语境"）
_LEGAL_EN_BARE_RE = re.compile("(?i)" + _LEGAL_EN_BARE)

#: 未成年标记（**带词边界**：原实现 `minor|child|kid` 无 `\b` ⇒ `minority` 也命中）
_MINOR_MARK = (r"(?:未满\s*1[0-8]\s*岁|不满\s*1[0-8]\s*岁|年?仅?\s*1[0-7]\s*岁|未成\s*年|"
               r"未成年(?:孩子|子女|人)?|under\s*1[0-8]|\b(?:minor|child|kid|children)\b)")
#: 未成年**身份信息**：具体号码，或**带所有格**的详情词（"children at school"/"kid-friendly" 不算）
#: ⚠️ 实测教训（t70）：`身份证` 后**不能**要求 `\b` —— 常见写法是「身份证**号**」，
#:    证/号 之间没有词边界 ⇒ 会漏掉真阳性（既有判据 `我的女儿 14 岁…身份证号 3101…` 当场变红）。
_MINOR_IDENTITY = (
    r"(?:[A-Za-z]{0,2}\d{6,}"
    r"|(?:身份证(?:号)?|护照|社保卡|学籍号|ssn|passport|student\s*id|ID\s*number)[^.\n]{0,10}\d{2,}"
    r"|(?:my|his|her|their|our|your)\s+(?:son|daughter|child|kid|boy|girl)?'?s?\s*"
    r"(?:school|class|address|phone|name|guardian|teacher)"
    r"|(?:孩子|他|她|我|女儿|儿子)的\s*(?:学校|班级|住址|手机号|监护人|班主任|姓名))"
)
#: 性史：`sexual experience(s)` 在**产品/调研**语境里是泛词 ⇒ 只认带所有格的形式
_SEXUAL_HIGH = (r"(?:性史|性经历|性伴侣|sexual\s+history|sexual\s+partner|发生过关系|一夜情|"
                r"(?:my|his|her|their|our|your)\s+sexual\s+experiences?)")
#: **改动前**的原始版本（`TRINITY_HIGH_PERSONAL_CONTEXT=off` 时逐字回滚用；t70 前的工作树快照）
_MINORS_HIGH_ORIG = re.compile(
    r"(?:未满\s*1[0-8]\s*岁|不满\s*1[0-8]\s*岁|年?仅?\s*1[0-7]\s*岁|未成\s*年|未成年孩子|"
    r"under\s*1[0-8]|(?:a\s+)?(?:minor|child|kid))[^。\n]{0,40}"
    r"(?:身份证|身份证号|护照|社保卡|学籍号|学校|班级|住址|家庭住址|手机号|电话号码|监护人|家长姓名|"
    r"(?:ID|id|ssn|passport|student\s*id|school|class|address|phone|guardian)\b)"
)
_MINORS_HIGH_ORIG_LABEL = "未成年身份信息"
_SEXUAL_HIGH_ORIG = re.compile(
    r"(?:性史|性经历|性伴侣|sexual\s+history|sexual\s+experience|sexual\s+partner|发生过关系|一夜情)"
)
_LEGAL_EN_BARE_RE_COMPILED = re.compile("(?i)" + _LEGAL_EN_BARE)

_HIGH_PATTERNS: List[Any] = [
    # 未成年身份信息：年龄词 + 近邻(40字符内)身份信息词 共现
    # I10/t70：默认用**收紧版**（两侧都加词边界/要求具体号码或所有格）；`off` ⇒ 逐字回滚到原版
    ((re.compile(_MINOR_MARK + r"[^。\n]{0,40}" + _MINOR_IDENTITY)
      if high_personal_context_required() else _MINORS_HIGH_ORIG),
     "minors_pii", "未成年身份信息"),
    # 犯罪/法律敏感状态 —— 中文个人记录强信号（本身即个人语境，语义不变）
    (re.compile(
        r"(?:犯罪记录|刑事案底|案底|被判过刑|判刑入狱|拘留记录|逮捕记录|吸毒记录)"
    ), "legal_status", "犯罪记录/法律敏感状态"),
    # 犯罪/法律敏感状态 —— **英文/议题型**：I10/t70 起**要求个人语境锚**
    #（`TRINITY_HIGH_PERSONAL_CONTEXT=off` ⇒ 用裸词，与改动前一致）
    (re.compile(_LEGAL_EN_ANCHORED if high_personal_context_required() else _LEGAL_EN_BARE),
     "legal_status", "犯罪记录/法律敏感状态（英文/议题型）"),
    # 精神/心理诊断（疾病名 + 确诊/住院/用药语境）
    (re.compile(
        r"(?:确诊(?:为)?|被诊断|诊断出|住院(?:治疗)?(?:过|了)?|正在服(?:用)?|在(?:接受|进行))(?:了)?"
        r"(?:抑郁症|焦虑症|双相(?:情感)?障碍|精神分裂(?:症)?|创伤后应激(?:障碍)?|强迫症|进食障碍|边缘型人格)|"
        r"(?:diagnosed\s+with|hospitalized\s+for|medication\s+for)\s+"
        r"(?:depression|anxiety|bipolar\s+disorder|schizophrenia|ptsd|ocd|eating\s+disorder|borderline\s+personality)|"
        r"psychiatric\s+(?:diagnosis|hospital|ward)"
    ), "psych_health", "精神/心理诊断"),
    # 性史强信号（I10/t70：去掉裸 `sexual experience`，只认带所有格；`off` ⇒ 回滚到原版）
    ((re.compile(_SEXUAL_HIGH) if high_personal_context_required() else _SEXUAL_HIGH_ORIG),
     "sexual_history", "性史"),
    # 自残/轻生意图（Anthropic 边界：即使求助语境也不落库）
    # ⚠️ I10/t70 **不改本类的判定门槛**（真阳性风险，见 HIGH-CATEGORY-FALSE-POSITIVES.md §5）；
    #    仅补一处**召回**缺口：`cutting herself/himself` 原先不命中。
    (re.compile(
        r"(?:想(?:要|着)?自杀|打算自杀|准备自杀|自杀过|自杀了?两次|不想活了|活不下去|想结束(?:自己的)?生命|"
        # ⚠️ t70 **撤回**了「自杀(干预)热线」这条召回补充：它会把
        #    「这篇文档讨论了自杀干预热线 400-161-9995 的运营」（公益/知识文本）判成 high ⇒
        #    两条既有判据（`p-06 公益-自杀干预` 必须 medium、`test_scan_rules_benign`）当场变红。
        #    「求助行为本身是否该拒存」是**政策问题**，留给队长裁定（见报告 §5 的负结果）。
        r"自残|割腕|跳楼|吞(?:药|安眠药)(?:自杀|轻生)?|"
        r"suicid(?:al|e)|want(?:s)?\s+to\s+(?:kill|end)\s+(?:myself|my\s+life)|self[- ]?harm|"
        r"cutting\s+(?:my|her|his|them)sel(?:f|ves)|cutting\s+wrists)"
    ), "self_harm", "自残/轻生意图"),
    # ── T5 新增（复核发现的两处漏检，均带公共语境降级）───────────────────
    # ① 第一人称/身边人的**精神科诊断**（原判据只认「确诊/住院/用药」动词，
    #    「我得了抑郁症」「用户患有抑郁症」这种最直白的表述只落到 medium ⇒ 会被存下来）
    (re.compile(
        r"(?:我|我的|本人|自己|自己家|他|她|用户|当事人|家人|孩子)"
        r"[^。\n]{0,12}(?:得了|患有|患上|罹患|发现有|查出来有|被诊断(?:为|出)?|确诊(?:为|了)?)"
        r"[^。\n]{0,6}(?:抑郁症|焦虑症|双相(?:情感)?障碍|精神分裂(?:症)?|创伤后应激(?:障碍)?|"
        r"强迫症|进食障碍|边缘型人格|躁郁症|depression|anxiety|bipolar\s+disorder|schizophrenia|ptsd)"
    ), "psych_health", "精神/心理诊断"),
    # ② 中文「监狱」族 + 个人语境（原判据只有 案底/判刑/拘留 与英文 jail/prison，
    #    纯中文「进了监狱/坐牢/服刑」整族未被覆盖）
    (re.compile(
        r"(?:我|我的|本人|自己|他|她|用户|当事人|家人|亲戚|朋友|弟弟|哥哥|姐姐|妹妹|父亲|母亲|儿子|女儿)"
        r"[^。\n]{0,20}(?:进(?:过|了)?监狱|坐(?:过|了)?牢|服刑|入狱|蹲(?:过|了)?监狱?|"
        r"被(?:判|定)罪|被判(?:刑|过刑)|进过看守所|缓刑|假释)"
    ), "legal_status", "犯罪记录/法律敏感状态"),
]

# ── 公共语境降级（T5）─────────────────────────────────────────────────
# 动机（实测，必须自行复核）：中文语境下「监狱」「抑郁」出现在**新闻/小说/研究**
# 里是完全正常的文本，属"不该拦的"。裸词表做不到这件事，所以降级判据看**语境与
# 意图**：命中高危模式但同时出现公共语境标记 ⇒ 降为 medium（脱敏存，不阻断）。
# 反事实（tests/unit/test_sensitive_tiers_20261006.py）：
#   「我弟弟去年进了监狱」high ↔ 「新闻：该监狱超员被通报」medium
#   「我得了抑郁症」      high ↔ 「研究显示抑郁症患者…」  medium
_CONTEXT_MARKERS = re.compile(
    r"(?:新闻报道|新闻|报道|据[^。\n]{0,8}报道|研究(?:显示|表明|发现|指出)|论文|文献|综述|"
    r"统计(?:显示|表明)|调查(?:显示|表明)|学科|科普|教材|小说|情节|剧情|电影|电视剧|主人公|"
    r"虚构|案例(?:研究|分析)|患者群(?:体|体)|发病率|患病率|"
    r"\b(?:study|research|report|survey|novel|fiction|statistics)\b)")
#: 哪些类别允许被公共语境降级（个人身份类不许降级：未成年/自残/性史）
_DOWNGRADABLE = {"legal_status", "psych_health"}

# ── 中危：单点提及（仅标记，不拒存）──────────────────────────────────
_MEDIUM_PATTERNS: List[Any] = [
    (re.compile(r"未成年|未成年人|未满\s*1[0-8]\s*岁|(?:minor|child|kid)(?:\b|s\b)"), "minors_pii"),
    (re.compile(r"(?i)(?:犯罪|判刑|拘留|被捕|案底|criminal|arrest|convict|jail|prison|"
                r"监狱|坐牢|服刑|入狱|看守所|缓刑|假释|刑满释放|囚犯|越狱|"
                r"移民|签证|种姓|caste|immigrat|visa)"), "legal_status"),
    (re.compile(r"(?:抑郁|焦虑(?:症)?|双相|精神(?:疾病|障碍|分裂|科)|强迫症|进食障碍|躁郁|"
                r"心理(?:治疗|咨询|医生)|"
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


# ── 三档语义（T5）──────────────────────────────────────────────────────
ACTION_STORE = "store"        # 存
ACTION_REDACT = "redact"      # 脱敏存（标记 + 掩码可识别标识符）
ACTION_REFUSE = "refuse"      # 拒存
ACTION_QUARANTINE = "quarantine"  # 拒存的降级变体：隔离归档（落库但不进检索面）
ACTION_LABELS = {
    ACTION_STORE: "存",
    ACTION_REDACT: "脱敏存",
    ACTION_REFUSE: "拒存",
    ACTION_QUARANTINE: "隔离归档（拒存降级）",
}


def sensitive_redact_scope() -> str:
    """**脱敏范围**（G2 新增；`TRINITY_SENSITIVE_REDACT_SCOPE`）：

      · `all_pii`（**默认**）：有 PII 即掩码（本次扩范围后的行为）；
      · `category`：**逐字回到 G2 之前** —— 只有命中敏感类别（medium）才掩码。

    与 `TRINITY_SENSITIVE_REDACT` 的分工（两个开关不在一个维度上）：
      `TRINITY_SENSITIVE_REDACT=0` ⇒ **完全不掩码**（= t5 接线之前的形态，最保守的回滚）；
      `TRINITY_SENSITIVE_REDACT_SCOPE=category` ⇒ **精确回滚本次 G2 改动**（保留 t5 的类别档掩码）。
    """
    v = os.environ.get("TRINITY_SENSITIVE_REDACT_SCOPE", "all_pii").strip().lower()
    return v if v in ("all_pii", "category") else "all_pii"


def policy_action(report: Dict[str, Any]) -> str:
    """把扫描报告映射到三档动作。

    **G2 扩范围**：`high ⇒ 拒存/隔离`（**不变**）；`medium` **或**（`all_pii` 范围内的）
    "存在 PII" ⇒ `redact`；两者都不命中 ⇒ `store`。
    即"**有 PII 即至少脱敏存**"，而 high 档**不因含 PII 而降级**。
    """
    sev = (report or {}).get("severity")
    if sev == "high":
        return ACTION_QUARANTINE if (report.get("policy") == "quarantine") else ACTION_REFUSE
    if sev == "medium":
        return ACTION_REDACT
    if sensitive_redact_scope() == "all_pii" and bool(((report or {}).get("pii") or {}).get("flagged")):
        return ACTION_REDACT
    return ACTION_STORE


def _mask_keep(head: int, tail: int):
    def _f(m: Any) -> str:
        s = m.group(0)
        if len(s) <= head + tail + 1:
            return s[0] + "*" * max(0, len(s) - 1)
        return s[:head] + "*" * (len(s) - head - tail) + (s[-tail:] if tail else "")
    return _f


def _mask_email(m: Any) -> str:
    """G2/D1 裁定：**不保留完整域名** ⇒ 只留 TLD（`p***@***.com`）。

    改前是 `local[:1] + "***@" + 完整域名` —— 对**企业域名**（`@corp.com`）等于泄露雇主。
    """
    s = m.group(0)
    local, at, domain = s.partition("@")
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    return (local[:1] or "*") + "***" + at + ("***." + tld if tld else "***")


# ── PII 判定用的**确定性校验器**（G1/D3 裁定的「校验门」）──────────────────
# 动机（G1 全量实测）：现行 `\\d{16,19}` 的银行卡正则命中 74 次里 Luhn 只过 **2 次**；
# `\\d{17}[\\dXx]` 的身份证命中 2 次里 GB11643 校验 **0 次**通过 —— 裸正则在这份语料上
# 是**噪声放大器**（B 站 opus ID / 仓库 ID / Shopee appId / 36kr 文章 ID 都被判成卡号）。
def _luhn_ok(digits: str) -> bool:
    """银行卡号 Luhn 校验（GB/T 19584 同族算法）。"""
    if not digits.isdigit() or len(digits) < 12:
        return False
    tot, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        tot += d
        alt = not alt
    return tot % 10 == 0


_PROVINCES = {"11", "12", "13", "14", "15", "21", "22", "23", "31", "32", "33", "34", "35",
              "36", "37", "41", "42", "43", "44", "45", "46", "50", "51", "52", "53", "54",
              "61", "62", "63", "64", "65", "71", "81", "82", "91"}
_ID_W = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_C = "10X98765432"


def _id18_ok(s: str) -> bool:
    """GB 11643-1999：省份码 + 出生日期合理性 + 校验位。"""
    if not re.fullmatch(r"\d{17}[\dXx]", s):
        return False
    if s[:2] not in _PROVINCES:
        return False
    y, m, d = int(s[6:10]), int(s[10:12]), int(s[12:14])
    if not (1900 <= y <= 2026 and 1 <= m <= 12 and 1 <= d <= 31):
        return False
    tot = sum(int(s[i]) * _ID_W[i] for i in range(17))
    return _ID_C[tot % 11] == s[17].upper()


#: 真实号段（移动/联通/电信/广电，公开号段表）—— 把"11 位数字"与"手机号"分开
#: 2026-10-06（G2 召回侧实测后补）：原先漏了 **140/141/144/174**（物联网/虚拟号段），
#: 实测这导致 `17400956457` 被判"非手机号" ⇒ 已补。白名单宁多勿漏（掩错成本 < 漏掩成本）。
_PHONE_PREFIX3 = {
    "130", "131", "132", "133", "134", "135", "136", "137", "138", "139",
    "140", "141", "144", "145", "146", "147", "148", "149",
    "150", "151", "152", "153", "155", "156", "157", "158", "159",
    "162", "165", "166", "167", "170", "171", "172", "173", "174", "175", "176",
    "177", "178", "180", "181", "182", "183", "184", "185", "186", "187", "188", "189",
    "190", "191", "192", "193", "195", "196", "197", "198", "199",
}


def _phone_ok(s: str) -> bool:
    return len(s) == 11 and s.isdigit() and s[:3] in _PHONE_PREFIX3


#: ⭐ G9R-7/t121：**结构形态排除** —— 摘要/ID 形态的 hex 串里的数字子串**不是**手机号/卡号。
#: 机制（verifier 在 G9R-6 实测、本任务复现）：`019c16702723433c` 的子串 `16702723433`
#: 命中 `1[3-9]\d{9}`（第二位 `6` ✓）且 `167` 在 `_PHONE_PREFIX3` 里 ⇒ 通过号段门；
#: 旧的边界 `(?<!\d)/(?!\d)` **拦不住**它（前一位是字母 `c`，不是数字）。
#: ⇒ 判定只看**结构**（不看具体字符串）：
#:   取包含该匹配的**最大 hex 词元**；若该词元「比匹配更宽」且「长度 ∈ 常见摘要长度 {16,32,40,64,128}
#:   **或 ≥ 40」且「非纯数字」⇒ 判为结构串，**不作为数字类 PII 候选**（既不判 PII，也不掩码）。
#:   （`≥40` 兜住非标准长度：实测 60 位 hex 仍会被判成手机号 ⇒ 已覆盖。）
#: ⚠️ 已知假阴性边界（如实登记）：**真手机号被恰好 16/32/40/64/128 位的含字母 hex 串包住**时会漏
#:   （例：`abc13812345678de`）。这是有意的取舍：手机号是 11 位、不会自带 16 位 hex 外壳；
#:   而"摘要串里的 11 位数字子串"在结构列上是**已实测的确定性误报**。
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_HASH_LIKE_LENS = frozenset({16, 32, 40, 64, 128})


def _is_hash_like_token(text: str, start: int, end: int) -> bool:
    """`text[start:end]` 是否落在**摘要形态**的词元内（纯 hex、含字母、长度 ∈ `_HASH_LIKE_LENS`）。"""
    lo, hi = start, end
    while lo > 0 and text[lo - 1] in _HEX_DIGITS:
        lo -= 1
    while hi < len(text) and text[hi] in _HEX_DIGITS:
        hi += 1
    tok = text[lo:hi]
    if len(tok) <= (end - start):        # 词元没比匹配更宽 ⇒ 匹配本身就是整个词元（如裸手机号）
        return False
    if len(tok) not in _HASH_LIKE_LENS and len(tok) < 40:
        return False
    return not tok.isdigit()             # 纯数字（16 位卡号等）**不排除**，只有"含字母的 hex 外壳"才排除


# ── 邮箱占位域：**有意排除**（G3-R3 显式登记，2026-10-06）────────────────────
# 队长复核提出：「`zhangsan@example.com` 不被掩」到底是**有意**还是 **TLD 门副作用**？
# 源码判定路径（`_email_ok`）逐条为：① 单标签/内网名 ⇒ 拒；② **本清单** ⇒ 拒；
# ③ `len(tld)>=2 and tld.isalpha()` 才通过。**本题命中的是第 ② 条**（`.com` 能过第 ③ 条）
# ⇒ **属有意排除，不是副作用**。理由：这些域由 RFC 2606 / RFC 6761 保留
# （`example.com/.net/.org`、`.test`、`.invalid`、`.localhost`）或为内网专用名
# （`local`/`internal`/`corp`），**不可能承载真实的个人邮箱** ⇒ 掩码它们只会增加
# "看起来做了事"的噪声。**这是设计取舍，故必须在代码里显式登记 + 由判据钉住**
# （C9 占位域不掩 / C10 真实域必掩，各带变异体）。
# 覆盖面读数（G3-R3 全量普查）：被本门拦掉的域清单与次数见
# `D:\DSH官网\trinity-optimize-20261006\evidence\t43r3-email-gate.txt`。
_EMAIL_RESERVED_NAMES = {"localhost", "local", "test", "invalid", "example", "internal", "corp"}
_EMAIL_PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net"}
#: 兼容别名（t42 证据脚本引用过 `_BAD_TLD`；语义 = 两张表的并集）
_BAD_TLD = set(_EMAIL_RESERVED_NAMES) | set(_EMAIL_PLACEHOLDER_DOMAINS)


def _email_ok(s: str) -> bool:
    """邮箱是否"可能是真实个人邮箱"（决定要不要掩）。

    拒的四条（且**逐条可归类**，便于 G3-R3 的清单归因）：
      ① `dom` 是空 / 单标签内网名（`localhost/local/test/invalid/example/internal/corp`）；
      ② `dom` 是 RFC 2606 保留域（`example.com/.org/.net`）—— **有意排除**；
      ③ `dom` 的 **TLD** 本身是保留名（`foo.test` / `x.invalid` / `a.example` / `b.local`）
         —— 2026-10-06（G3-R3）补：与 ② 同一意图（保留名不可能承载真实邮箱），
         原先只做整域精确匹配 ⇒ `foo.test` 会被掩（**精度方向的不一致**，非漏掩；
         全量普查该形态 **0 命中**，故补齐为零行为影响）；
      ④ TLD 不足 2 位或非纯字母（`user@1.2.3.4`、`user@host.1` 等）。
    """
    dom = s.rpartition("@")[2].lower()
    if not dom or dom in _EMAIL_RESERVED_NAMES:      # ①
        return False
    if dom in _EMAIL_PLACEHOLDER_DOMAINS:            # ② 有意排除（见上方登记）
        return False
    tld = dom.rsplit(".", 1)[-1]
    if tld in _EMAIL_RESERVED_NAMES:                 # ③
        return False
    return len(tld) >= 2 and tld.isalpha()           # ④


#: 「密钥·token(赋值)」的值排除表：这些值**是变量/占位符**，不是密钥（G1 实测的误报形态）
_SECRET_VALUE_EXCLUDE = re.compile(
    r"(?i)^(?:\$\{|os\.environ|get_|self\.|<.*>|REPLACE_WITH|local-only|changeme|your[_-]|"
    r"\*\*\*|xxx+|[A-Z_]{4,}$)")


def _mask_grouped_card(m: Any) -> str:
    """分组卡号（`6222 0212 3456 7890`）：只保留前 3 位数字，**分隔符也一并抹掉**。"""
    digits = re.sub(r"\D", "", m.group(0))
    return digits[:3] + "*" * max(0, len(digits) - 3)


def _luhn_spaced(s: str) -> bool:
    return _luhn_ok(re.sub(r"\D", "", s))


def _mask_head(n: int):
    """保留前 n 位、**去掉尾号**（G2/D1 裁定）。"""
    return _mask_keep(n, 0)


def _mask_secret(m: Any) -> str:
    """密钥族字面量：保留族前缀，其余全遮（`sk-***`）。"""
    s = m.group(0)
    for sep in ("-", "_"):
        if sep in s[:6]:
            return s.split(sep, 1)[0] + sep + "***"
    return s[:2] + "***"


def _mask_secret_assign(m: Any) -> str:
    """`key=value` 形态：保留键名，值全遮；值若是变量/占位符则**原样保留**（不掩）。"""
    val = m.group(2)
    if _SECRET_VALUE_EXCLUDE.match(val):
        return m.group(0)
    return m.group(1) + "***"


#: PII 规则表（G2）：(类别, 正则, 掩码器, 校验器 or None)
#: **顺序敏感**：身份证必须在银行卡之前（否则 18 位身份证会被当卡号）；
#: 密钥(赋值) 排在邮箱之后（避免把 `password=...@...` 这类形态互相抢）。
_PII_RULES: List[Any] = [
    ("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), _mask_head(3), _id18_ok),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), _mask_head(3), _phone_ok),
    ("银行卡号", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), _mask_head(3), _luhn_ok),
    # 2026-10-06（G2 召回侧实测）：**带分隔符的卡号**（`6222 0212 3456 7890`）裸 `\d{16,19}`
    # 看不见 ⇒ 补一条 4-4-4-4 分组形态，并**同样过 Luhn**（实测全量 0 命中 ⇒ 零误报、纯前瞻）。
    ("银行卡号", re.compile(r"(?<!\d)(?:\d{4}[ \-]){3}\d{4}(?!\d)"),
     _mask_grouped_card, _luhn_spaced),
    ("邮箱", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), _mask_email, _email_ok),
    ("学籍号", re.compile(r"(?<=学籍号)\s*[:：]?\s*[A-Za-z0-9\-]{5,}"), _mask_head(3), None),
    ("密钥·token", re.compile(r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}"
                              r"|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
                              r"|AKIA[0-9A-Z]{16})"), _mask_secret, None),
    ("密钥·token", re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|secret|client[_-]?secret"
                              r"|password|passwd|pwd)\s*[:=]\s*[\"']?)([A-Za-z0-9._\-]{8,})"),
     _mask_secret_assign, None),
    ("护照", re.compile(r"(?<![A-Za-z0-9])[EG]\d{8}(?![0-9])"), _mask_head(1), None),
    ("社保号", re.compile(r"(?:社保|社会保障|医保)\s*(?:号|卡号)?\s*[:：]?\s*[A-Za-z0-9\-]{6,20}"),
     _mask_head(3), None),
]

#: **G1/D2 裁定：不默认纳入**的类（在此显式登记，配合"必须不命中"的判据锁住）
#:   IP（273 行命中里 79% 是非个人 PII：`127.0.0.1`/版本号 `1.8.5.6`/私网）
#:   住址 / 姓名 / 车牌 / 驾照 / QQ·微信（G1 实测误报率 60–100%）
_PII_NOT_ENABLED = ("IP", "住址", "姓名", "车牌", "驾照", "QQ/微信")


def scan_pii(text: str, *, enabled: Optional[Any] = None) -> Dict[str, Any]:
    """**PII 存在性扫描**（G2 新增；只判"有没有"，不改文本）。

    Returns: {"flagged": bool, "kinds": [类别...], "hits": [{"kind","match"}], "count": int}

    与 `scan_sensitive` 的分工：`scan_sensitive` 判**敏感类别**（未成年/法律/心理/性史 ⇒ 三档），
    本函数判**通用 PII**（身份证/手机/银行卡/邮箱/学籍/密钥/护照/社保）⇒ 决定"是否掩码"。
    """
    t = str(text or "")
    if not t:
        return {"flagged": False, "kinds": [], "hits": [], "count": 0}
    hits: List[Dict[str, str]] = []
    for kind, pat, rep, validator in (enabled if enabled is not None else _PII_RULES):
        for m in pat.finditer(t):
            got = m.group(0)
            if validator is not None and not validator(got):
                continue
            # G9R-7/t121：结构形态排除（摘要 hex 里的数字子串 ⇒ 不作数字类 PII 候选）
            if got.isdigit() and _is_hash_like_token(t, m.start(), m.end()):
                continue
            if rep is _mask_secret_assign and _SECRET_VALUE_EXCLUDE.match(m.group(2)):
                continue
            hits.append({"kind": kind, "match": got[:60]})
    kinds = sorted({h["kind"] for h in hits})
    return {"flagged": bool(hits), "kinds": kinds, "hits": hits, "count": len(hits)}


# ── 可观测计数（T5 追加，2026-10-06「不可逆变换必须可查」）──────────────────
# 动机：medium 档的掩码是**默认 on** 且**信息销毁型**（掩码后无法还原）。判据只能证明
# 「作用域正确」，证明不了「在生产语料上的实际掩码量级」。因此提供进程级计数，
# 并**尽力桥接到 API 的 /metrics**（resident API 那个进程里可被 scrape）：
#   trinity_sensitive_scans_total                    扫描次数（写路径每次写入一次）
#   trinity_sensitive_flagged_medium_total           判为 medium（脱敏存档）的次数
#   trinity_sensitive_flagged_high_total             判为 high（拒存/隔离档）的次数
#   trinity_sensitive_redacted_total                 实际发生掩码的次数（分子）
#   trinity_sensitive_redacted_identifiers_total     被掩码的标识符**个数**
#   trinity_sensitive_redacted_by_kind_total{kind=}  按标识符种类
# 语义：**进程内累计、重启归零**（与 API 其它计数器同族）。重启后立刻读它，
# 即可在"是否保留默认 on"上做判断：看 redacted_total / flagged_medium_total 的比值。
# 桥接是**惰性且零副作用**的：只有在本进程已经加载过 API 层（resident API 必有）
# 时才取注册表；否则只保留本地计数，绝不触发 `trinity.api` 包的重导入。
_STATS_LOCK = threading.Lock()
_REDACT_STATS: Dict[str, Any] = {
    "scans_total": 0,
    "medium_flagged_total": 0,
    "high_flagged_total": 0,
    "redacted_total": 0,
    "redacted_identifiers_total": 0,
    # ── G2 追加：把"因 PII 而掩码"与"因类别而掩码"分开读（前 6 个指标语义不变）──
    "pii_flagged_total": 0,          # 扫描时"存在 PII"的次数
    "redacted_pii_only_total": 0,    # 掩码原因 = 纯 PII（未命中敏感类别）
    "redacted_category_total": 0,    # 掩码原因 = 命中敏感类别（medium）
    # ── I10/t70 追加：**"本该 high 但缺个人语境 ⇒ 不判 high"** 的次数（留痕，不当假话）──
    "high_downgraded_no_context_total": 0,
    # ⭐ G9R-11/t125（D-9 裁定①）：**"本该拒存但被降级为掩码"的条数**。
    # 为什么**新增**而不是复用 `high_downgraded_no_context_total`：
    #   · 后者计的是 **t70 的机制**（"high 词但**缺个人语境**"），与 D-9 的
    #     "**公益/知识语境**"是**两个不同事件** ⇒ 复用会把两者混在一起，
    #     于是"有多少条本该拒的变成了掩"仍然**答不出来**（正是本次要补的审计缺口）；
    #   · 复用还会**改变既有指标的含义**（对已读它的地方是兼容性破坏）；
    #   · 新增的代价只有"多一个序列"（**无标签、基数 1**）+ 一行 `_METRIC_MAP`，
    #     且走既有惰性 `/metrics` 桥接 ⇒ 增量成本最低。
    # 口径：**按判定（文本）计，一条被降级 = +1**；**进程内累计、重启归零**（与同族计数一致）。
    "help_context_downgraded_total": 0,
    "by_kind": {},
}
_METRIC_MAP = {
    "scans_total": "trinity_sensitive_scans_total",
    "medium_flagged_total": "trinity_sensitive_flagged_medium_total",
    "high_flagged_total": "trinity_sensitive_flagged_high_total",
    "redacted_total": "trinity_sensitive_redacted_total",
    "redacted_identifiers_total": "trinity_sensitive_redacted_identifiers_total",
    "pii_flagged_total": "trinity_sensitive_pii_flagged_total",
    "redacted_pii_only_total": "trinity_sensitive_redacted_pii_only_total",
    "redacted_category_total": "trinity_sensitive_redacted_category_total",
    "high_downgraded_no_context_total": "trinity_sensitive_high_downgraded_no_context_total",
    # G9R-11/t125：D-9 语境门**降级**（本该拒存 ⇒ 改为脱敏存）的条数
    "help_context_downgraded_total": "trinity_sensitive_help_context_downgraded_total",
}
_metrics_sink: Any = None  # None=未解析 / False=不可用（本进程没有 API 层）


def _metrics_sink_obj() -> Any:
    """取 API 的指标注册表单例；**不主动导入 `trinity.api`**（避免拖入 GraphQL 重依赖）。

    否定结果**不缓存**：若本进程在首次掩码时还没加载 API 层（例如离线脚本），
    之后 API 层真的加载了（resident API 的启动顺序）也能接上；每次只是 `sys.modules` 查表。
    """
    global _metrics_sink
    if _metrics_sink:
        return _metrics_sink
    mod = sys.modules.get("trinity.api.middleware")
    if mod is None:
        if "trinity.api" in sys.modules:
            try:
                import importlib
                mod = importlib.import_module("trinity.api.middleware")
            except Exception:  # noqa: BLE001
                mod = None
        if mod is None:
            return False
    try:
        got = getattr(mod, "get_metrics", None)
        _metrics_sink = got() if callable(got) else False
    except Exception:  # noqa: BLE001 —— 指标不可用绝不影响安全门控
        _metrics_sink = False
    return _metrics_sink or False


def _bump(key: str, amount: int = 1, labels: Optional[Dict[str, str]] = None) -> None:
    with _STATS_LOCK:
        _REDACT_STATS[key] = int(_REDACT_STATS.get(key, 0)) + amount
    sink = _metrics_sink_obj()
    if sink:
        name = _METRIC_MAP.get(key)
        if name:
            try:
                sink.inc(name, labels, float(amount))
            except Exception as _e:  # noqa: BLE001
                # B1/t85：**不静默**（此前是 `except: pass`）。指标后端不可用只降级，
                # 但必须留痕 —— 否则"没接线"与"出错了"在本仓无法区分。
                logger.debug("metrics sink inc failed (%s): %s",
                             type(_e).__name__, str(_e)[:120])


def _bump_kind(kind: str, amount: int) -> None:
    with _STATS_LOCK:
        _k = _REDACT_STATS.setdefault("by_kind", {})
        _k[kind] = int(_k.get(kind, 0)) + amount
    sink = _metrics_sink_obj()
    if sink:
        try:
            sink.inc("trinity_sensitive_redacted_by_kind_total", {"kind": kind}, float(amount))
        except Exception as _e:  # noqa: BLE001
            # B1/t85：**不静默**（此前是 `except: pass`）—— 同上，只降级 + 留痕。
            logger.debug("metrics sink inc(kind) failed (%s): %s",
                         type(_e).__name__, str(_e)[:120])


def redact_stats() -> Dict[str, Any]:
    """读「脱敏存」相关计数（进程内累计）+ 一个便于判断误伤的比值。

    `redacted_share_of_medium` = 掩码次数 / 判为 medium 的次数：
    比值高 ⇒ 被标记为 medium 的内容里大多数确实含标识符（掩码在干实事）；
    比值低 ⇒ 多数 medium 内容没有标识符（掩码未触发，说明该档主要在"标记"而非"销毁"）。
    两者都**不代表**掩码正确性本身（那由作用域判据保证），只回答"量级是否异常"。
    """
    with _STATS_LOCK:
        snap = dict(_REDACT_STATS)
        snap["by_kind"] = dict(_REDACT_STATS.get("by_kind") or {})
    med = int(snap.get("medium_flagged_total") or 0)
    snap["redacted_share_of_medium"] = (
        round(int(snap.get("redacted_total") or 0) / med, 4) if med else None)
    snap["_semantics"] = "进程内累计、重启归零；仅统计本进程"
    return snap


def reset_redact_stats() -> None:
    """清零本地计数（**仅供测试/诊断**；不动 API 注册表里已累计的样本）。"""
    with _STATS_LOCK:
        for k in list(_REDACT_STATS):
            _REDACT_STATS[k] = {} if k == "by_kind" else 0


#: 向后兼容别名（G2 起真正的规则表是 `_PII_RULES`；此别名只保留 (类别,正则,掩码器) 三元组）
_REDACTORS: List[Any] = [(k, p, r) for (k, p, r, _v) in _PII_RULES]


def redact_identifiers(text: str, *, cause: Optional[str] = None) -> Any:
    """掩码可识别标识符（脱敏存的执行体）。返回 (脱敏后文本, 命中标签列表)。

    **G2 起**：
      · 判定扩到"**有 PII 即掩码**"（`_PII_RULES`，含 G1/D3 裁定的**校验门**：
        银行卡 Luhn / 身份证 GB11643 / 手机真实号段 / 邮箱 TLD）；
      · 掩码格式按 **G1/D1 裁定**改为**保留前 3、去尾号**，邮箱只留 TLD；
      · `cause` 用于把"因 PII 而掩码"与"因类别而掩码"分开计数
        （`redacted_pii_only_total` / `redacted_category_total`）；
      · **不含 IP / 住址 / 姓名 / 车牌 / 驾照 / QQ·微信**（G1/D2 裁定：误报率过高，不默认开）。

    只做**确定性掩码**，不引入 LLM；未命中时原样返回（幂等）。
    """
    out = str(text or "")
    labels: List[str] = []
    kinds: List[str] = []
    total = 0
    for name, pat, rep, validator in _PII_RULES:
        hits = 0

        def _sub(m: Any, _rep: Any = rep, _validator: Any = validator) -> str:
            nonlocal hits
            got = m.group(0)
            if _validator is not None and not _validator(got):
                return got                      # 校验门不过 ⇒ 不掩（G1/D3）
            # G9R-7/t121：结构形态排除（摘要 hex 里的数字子串 ⇒ 不掩，见 `_is_hash_like_token`）
            if got.isdigit() and _is_hash_like_token(out, m.start(), m.end()):
                return got
            new = _rep(m)
            if new != got:                       # 占位符等"原样返回"的情形不计入
                hits += 1
            return new

        new_out = pat.sub(_sub, out)
        if hits:
            labels.append("%s×%d" % (name, hits))
            kinds.append(name)
            total += hits
            out = new_out
    if labels:
        _bump("redacted_total")
        _bump("redacted_identifiers_total", total)
        if cause == "pii":
            _bump("redacted_pii_only_total")
        elif cause == "category":
            _bump("redacted_category_total")
        for k in kinds:
            _bump_kind(k, 1)
    return out, labels


def apply_policy(text: str, report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """三档决策的**单入口**（调用方可直接按 action 落库）。

    返回 {"action", "tier", "content", "redactions", "reason", "isolate"}。
    - store  : content=原文
    - redact : content=掩码后文本
    - refuse : content=None（不落库）
    - quarantine: content=掩码后文本 + isolate=True（落 archived）
    """
    report = report if report is not None else scan_sensitive(text)
    action = policy_action(report)
    if action == ACTION_REFUSE:
        return {"action": action, "tier": ACTION_LABELS[action], "content": None,
                "redactions": [], "isolate": False,
                "reason": "高危敏感类别：%s ⇒ 内容不落库" % ",".join(report.get("categories", []))}
    red, labels = redact_identifiers(
        text, cause=("category" if report.get("flagged") else "pii"))
    if action == ACTION_REDACT:
        return {"action": action, "tier": ACTION_LABELS[action], "content": red,
                "redactions": labels, "isolate": False,
                "reason": "脱敏存：%s ⇒ 掩码，不阻断"
                          % ("命中敏感类别 " + ",".join(report.get("categories", []))
                             if report.get("flagged")
                             else "命中 PII " + ",".join((report.get("pii") or {}).get("kinds", [])))}
    if action == ACTION_QUARANTINE:
        return {"action": action, "tier": ACTION_LABELS[action], "content": red,
                "redactions": labels, "isolate": True,
                "reason": "高危 + TRINITY_SENSITIVE_POLICY=quarantine ⇒ 隔离归档（不进检索面）"}
    return {"action": action, "tier": ACTION_LABELS[action], "content": red,
            "redactions": labels, "isolate": False, "reason": "未命中敏感类别，也无 PII"}


def sensitive_scan_enabled() -> bool:
    """写路径敏感扫描开关（默认 on，off 关闭）。"""
    return os.environ.get("TRINITY_SENSITIVE_SCAN", "on").strip().lower() not in ("off", "0", "false")


def sensitive_redact_enabled() -> bool:
    """「脱敏存」档的开关（**默认 on**）。

    **G2 扩范围后**：不再只在"命中敏感类别"时生效 —— **有 PII 即掩码**
    （`_PII_RULES`，含 G1/D3 校验门）。关掉（`TRINITY_SENSITIVE_REDACT=0`）
    即**逐字回到本次改动前的行为**（类别命中才掩码；纯 PII 原文落库）。
    """
    return os.environ.get("TRINITY_SENSITIVE_REDACT", "on").strip().lower() not in (
        "off", "0", "false", "no")


# ── B1/A5-01（t85）：**判定决策记录** —— 让每条敏感判定可查询（纯加法）──────────
# 依据：EU AI Act Art 12「High-risk AI systems shall technically allow for the automatic
# recording of events (logs) over the lifetime of the system.」；
# 45 CFR 164.514「Documents the methods and results of the analysis that justify such determination」。
# 现状（t85 报告 §1 逐处定性）：判定链只有**四处碎片**（进程计数 / 行级 metadata /
# POLICY_PURGE 审计 / 适配器路径**完全无痕**），**都不可按条查询**。本块补"按条可查"的记录。
# **纯加法**：不改规则表、不改 `policy_action`、不改 `hits` 结构 ⇒ **判定语义零变化**。
#   · 记录里**没有 `score` 字段** —— 本模块不产生概率分数，**没算就不写**（宁缺勿编）；
#   · `tier` 是**开关感知**的（如 `TRINITY_SENSITIVE_REDACT=0` ⇒ `store`）⇒ 记录 == 实际动作；
#   · **默认不落盘**（仅内存 deque）：落到磁盘会新增一份被保留的制品，而按 A3/DPC 依据
#     「保留 ⇒ 仍属个人数据、义务不降」⇒ 落盘保持 **opt-in**（`TRINITY_DECISION_LOG_PATH`）。
SENSITIVE_POLICY_VERSION = "2026-10-06.b1"
_DECISION_SCHEMA = "trinity.sensitive.decision/v1"
#: ⚠️ **每进程随机盐**：`content_fp` 因此 **跨进程不可比**（同一内容在别的进程/重启后指纹不同）。
#: 目的：阻断"已知明文确认"（固定盐下，别人可拿候选文本算指纹来核对日志里有没有这条）。
_DECISION_SALT = os.urandom(16)
_DECISION_LOG: Any = None       # 懒建 deque（不在导入期分配）
_DECISION_LOCK = threading.Lock()


def decision_log_size() -> int:
    """记录缓冲上限（`TRINITY_DECISION_LOG_SIZE`，默认 **512**；`0`/`off` ⇒ **关闭记录**）。"""
    raw = os.environ.get("TRINITY_DECISION_LOG_SIZE", "512").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 512


def decision_records_enabled() -> bool:
    """是否记录判定（= 缓冲上限 > 0）。"""
    return decision_log_size() > 0


def _decision_deque():
    """取（或按当前上限重建）记录缓冲；关闭时返回 None。"""
    import collections
    global _DECISION_LOG
    n = decision_log_size()
    if n <= 0:
        return None
    if _DECISION_LOG is None or getattr(_DECISION_LOG, "maxlen", None) != n:
        _DECISION_LOG = collections.deque(maxlen=n)
    return _DECISION_LOG


def _slug(text: str) -> str:
    """把标签变成稳定、可 grep 的片段（保留中日韩字符；其余非字母数字折成 `-`）。"""
    import re as _re
    s = _re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", str(text or "").strip().lower())
    return s.strip("-") or "unnamed"


def _rule_ids_from(report: Dict[str, Any]) -> List[str]:
    """从**已有**判定结果派生稳定规则 ID（**只读它的输出**，不改规则表结构）。"""
    ids: List[str] = []
    for h in (report or {}).get("hits") or []:
        ids.append("%s:%s:%s" % (str(h.get("severity") or "rule"),
                                 _slug(h.get("category")), _slug(h.get("pattern"))))
    for k in ((report or {}).get("pii") or {}).get("kinds") or []:
        ids.append("pii:%s" % _slug(k))
    if (report or {}).get("no_context_downgraded"):
        ids.append("noctx:legal_status:en-bare")
    return sorted(set(ids))


def _safe_trigger(match: str) -> str:
    """触发词**脱敏**：含 ≥6 位连续数字时掩掉（防把 PII 原值写进记录）。"""
    import re as _re
    return _re.sub(r"\d{6,}", "***", str(match or ""))[:80]


def _decision_should_record(report: Dict[str, Any]) -> bool:
    """**只在真的有判定时记录**（防恒真：纯良性文本、空文本 ⇒ 不产生记录）。"""
    r = report or {}
    return bool(r.get("flagged") or (r.get("pii") or {}).get("flagged"))


def _build_decision_record(report: Dict[str, Any], *, text: str = "") -> Dict[str, Any]:
    """组装一条决策记录（**只如实转换已有判定，不新增/不推演**；**无 `score` 字段**）。

    ⚠️ `content_fp` = `sha256(每进程随机盐 + 内容)[:12]` ⇒ **跨进程不可比**（见 `_DECISION_SALT`）。
    """
    import datetime as _dt
    import hashlib as _hl
    r = report or {}
    sev = r.get("severity")
    pii = r.get("pii") or {}
    # —— tier：**开关感知**（与 `policy_action` 同判据，再叠加"掩码开关"这一层实况）——
    if sev == "high":
        tier = ACTION_QUARANTINE if r.get("policy") == "quarantine" else ACTION_REFUSE
        basis = "severity=high"
    elif sev == "medium" or (sensitive_redact_scope() == "all_pii" and pii.get("flagged")):
        if sensitive_redact_enabled():
            tier = ACTION_REDACT
            basis = "severity=medium" if sev == "medium" else "pii-only"
        else:
            tier, basis = ACTION_STORE, "redact-switch-off"
    else:
        tier, basis = ACTION_STORE, "no-hit"
    content = str(text or "")
    return {
        "schema": _DECISION_SCHEMA,
        "policy_version": SENSITIVE_POLICY_VERSION,
        "rule_ids": _rule_ids_from(r),
        "categories": list(r.get("categories") or []),
        "pii_kinds": list(pii.get("kinds") or []),
        "severity": sev,
        "tier": tier,
        "action_basis": basis,
        "switches": {
            "scan": "on" if sensitive_scan_enabled() else "off",
            "redact": "on" if sensitive_redact_enabled() else "off",
            "scope": sensitive_redact_scope(),
            "personal_context": "on" if high_personal_context_required() else "off",
            "policy": _policy(),
        },
        "downgraded": bool(r.get("downgraded")),
        "no_context_downgraded": bool(r.get("no_context_downgraded")),
        "triggers": [_safe_trigger(h.get("match")) for h in (r.get("hits") or [])][:8],
        "content_len": len(content),
        "content_fp": _hl.sha256(_DECISION_SALT + content.encode("utf-8")).hexdigest()[:12],
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds"),
    }


#: ⭐ D-13/t108：**落盘最小化白名单** —— 只有这些字段允许写进 JSONL（**绝不含正文**）。
#: 内存 deque 里保留完整记录（含同样无正文的字段），但**磁盘只写白名单**（万一将来有人往记录里
#: 加字段，磁盘面也不会跟着扩大 ⇒ 这是"可失败"的最小化约束，判据 §C5 钉住）。
_DECISION_PERSIST_FIELDS = (
    "schema", "policy_version", "rule_ids", "categories", "pii_kinds", "severity", "tier",
    "action_basis", "switches", "downgraded", "no_context_downgraded", "triggers",
    "content_len", "content_fp", "ts",
)
#: 保留策略：单个文件上限（`TRINITY_DECISION_LOG_MAX_BYTES`，默认 **5 MiB**）；超限轮转，只留 **.1 一代**。
_DECISION_MAX_BYTES_DEFAULT = 5 * 1024 * 1024
_DECISION_PERSIST_STATS: Dict[str, int] = {"lines": 0, "bytes": 0, "rotations": 0, "errors": 0}


def decision_persist_max_bytes() -> int:
    """单文件上限（字节）。`0` ⇒ **不轮转（无界）**——**判据的牙齿**会用到这个口径。"""
    raw = os.environ.get("TRINITY_DECISION_LOG_MAX_BYTES", "").strip()
    try:
        return max(0, int(raw)) if raw else _DECISION_MAX_BYTES_DEFAULT
    except ValueError:
        return _DECISION_MAX_BYTES_DEFAULT


def _decision_persist_path() -> Optional[str]:
    """落盘路径（未设置 ⇒ `None` ⇒ **默认不落盘**）。"""
    p = os.environ.get("TRINITY_DECISION_LOG_PATH")
    return p or None


def _decision_minimize(rec: Dict[str, Any]) -> Dict[str, Any]:
    """按白名单裁剪一条记录（**磁盘面最小化**）。"""
    return {k: rec[k] for k in _DECISION_PERSIST_FIELDS if k in rec}


def _rotate_decision_file(path: str, incoming: int) -> bool:
    """保留策略：若 `已写 + incoming > 上限` ⇒ 轮转（当前文件改名 `path.1`，**只留一代**）。

    Returns: 是否发生了轮转。上限为 `0` ⇒ 不轮转（无界，**判据的牙齿目标**）。
    """
    limit = decision_persist_max_bytes()
    if limit <= 0:
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size + incoming <= limit:
        return False
    try:
        os.replace(path, path + ".1")
    except OSError as _e:                     # noqa: BLE001 —— 只降级，但不静默
        _DECISION_PERSIST_STATS["errors"] += 1
        logger.debug("decision log rotate failed: %s", str(_e)[:120])
        return False
    _DECISION_PERSIST_STATS["rotations"] += 1
    return True


def decision_persistence_stats() -> Dict[str, Any]:
    """落盘面的可机读读数（含**保留策略**参数与计数）。"""
    stats = dict(_DECISION_PERSIST_STATS)
    stats.update({
        "path": _decision_persist_path(),
        "enabled": bool(_decision_persist_path()),
        "max_bytes": decision_persist_max_bytes(),
        "generations_kept": 1,
        "minimized_fields": list(_DECISION_PERSIST_FIELDS),
        "contains_content": False,
        "content_fp_salt": "per-process-random（**跨进程不可比**）",
    })
    return stats


def _record_decision(report: Dict[str, Any], *, text: str = "") -> None:
    """写一条决策记录（内存 deque；可选 JSONL）。**只降级、不阻断、不静默**。"""
    try:
        if not decision_records_enabled() or not _decision_should_record(report):
            return
        rec = _build_decision_record(report, text=text)
        dq = _decision_deque()
        if dq is not None:
            with _DECISION_LOCK:
                dq.append(rec)
        path = _decision_persist_path()
        if path:
            import json as _json
            line = _json.dumps(_decision_minimize(rec), ensure_ascii=False) + "\n"
            raw = len(line.encode("utf-8"))
            _rotate_decision_file(path, raw)      # D-13：保留策略（超限轮转）
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
            _DECISION_PERSIST_STATS["lines"] += 1
            _DECISION_PERSIST_STATS["bytes"] += raw
    except Exception as _e:  # noqa: BLE001 —— 观测不得影响写入路径，但**不静默**
        _DECISION_PERSIST_STATS["errors"] += 1
        logger.debug("decision record failed (%s): %s", type(_e).__name__, str(_e)[:120])


def decision_log(limit: int = 50, *, action: Optional[str] = None,
                 category: Optional[str] = None, rule_id: Optional[str] = None,
                 since: Optional[str] = None) -> List[Dict[str, Any]]:
    """**按条查询**决策记录（最新在前；`limit <= 0` ⇒ 全部）。

    过滤：`action`（= `tier`）、`category`（命中类别）、`rule_id`（规则 ID，子串匹配）、
    `since`（ISO 时点，字符串比较 ⇒ 只对同一时钟来源有意义）。
    ⚠️ `content_fp` **跨进程不可比**（每进程随机盐）⇒ 别拿别的进程的指纹来对照。
    """
    dq = _decision_deque()
    if dq is None:
        return []
    with _DECISION_LOCK:
        recs = list(dq)
    recs.reverse()
    out: List[Dict[str, Any]] = []
    for r in recs:
        if action and r.get("tier") != action:
            continue
        if category and category not in (r.get("categories") or []):
            continue
        if rule_id and not any(rule_id in x for x in (r.get("rule_ids") or [])):
            continue
        if since and str(r.get("ts") or "") < since:
            continue
        out.append(r)
    return out if limit is None or limit <= 0 else out[:limit]


def decision_log_stats() -> Dict[str, Any]:
    """记录的可机读聚合（总量 / 容量 / 按 tier / 按类别 / 按规则；是否落盘）。"""
    recs = decision_log(limit=0)
    by_tier: Dict[str, int] = {}
    by_cat: Dict[str, int] = {}
    by_rule: Dict[str, int] = {}
    for r in recs:
        by_tier[str(r.get("tier"))] = by_tier.get(str(r.get("tier")), 0) + 1
        for c in r.get("categories") or []:
            by_cat[c] = by_cat.get(c, 0) + 1
        for rid in r.get("rule_ids") or []:
            by_rule[rid] = by_rule.get(rid, 0) + 1
    return {
        "schema": _DECISION_SCHEMA,
        "policy_version": SENSITIVE_POLICY_VERSION,
        "total": len(recs),
        "capacity": decision_log_size(),
        "persisted": bool(os.environ.get("TRINITY_DECISION_LOG_PATH")),
        "persistence": decision_persistence_stats(),      # D-13/t108：落盘面（含保留策略）
        # G9R-11/t125：把 D-9 **降级**的聚合数也镜像到决策记录的公开面
        # （计数是聚合、B1 是逐条 ⇒ 两者可互相印证；两者都是**进程内累计、重启归零**）
        "help_context_downgraded_total": redact_stats().get("help_context_downgraded_total", 0),
        "by_tier": by_tier,
        "by_category": by_cat,
        "by_rule_id": by_rule,
    }


def reset_decision_log() -> None:
    """清空记录（**仅供测试/诊断**；不动任何计数）。"""
    dq = _decision_deque()
    if dq is not None:
        with _DECISION_LOCK:
            dq.clear()


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
    pii = scan_pii(text)
    if pii["flagged"]:
        _bump("pii_flagged_total")
    _empty = {"flagged": False, "severity": None, "policy": None, "action": ACTION_STORE,
              "categories": [], "hits": [], "truncated": False, "downgraded": False,
              "no_context_downgraded": False, "pii": pii}
    if not text:
        return dict(_empty, action=ACTION_STORE)

    truncated = len(text) > 20000
    _bump("scans_total")  # 写路径每次写入一次 ⇒ 是 medium 档占比的分母口径
    hits: List[Dict[str, Any]] = []
    for pattern, category, label in _HIGH_PATTERNS:
        m = pattern.search(text)
        if m:
            sev, reason = "high", "高危组合命中：%s" % label
            # 公共语境降级（T5）：新闻/小说/研究里的「监狱」「抑郁」不得拒存
            if category in _DOWNGRADABLE and _CONTEXT_MARKERS.search(text):
                sev = "medium"
                reason = ("公共语境（新闻/小说/研究）降级：%s ⇒ 脱敏存，不拒存" % label)
            hits.append({"category": category, "severity": sev,
                         "pattern": label, "match": m.group(0)[:80], "reason": reason})
    if not hits:
        for pattern, category in _MEDIUM_PATTERNS:
            m = pattern.search(text)
            if m:
                hits.append({"category": category, "severity": "medium",
                             "pattern": CATEGORY_LABELS.get(category, category),
                             "match": m.group(0)[:80],
                             "reason": "单点提及（%s）⇒ 标记 + 掩码，不阻断"
                                       % CATEGORY_LABELS.get(category, category)})

    # ── I10/t70：**"本该 high 但缺个人语境"必须留痕**（t64「不当假话」原则）──
    # 误报不再拒存，但**不许静默**：计一个专用计数 + 在 hits 里给出**为什么没判 high**。
    no_ctx = False
    if high_personal_context_required():
        _bare = _LEGAL_EN_BARE_RE_COMPILED.search(text)
        if _bare and not any(h["severity"] == "high" for h in hits):
            no_ctx = True
            _bump("high_downgraded_no_context_total")
            hits.append({
                "category": "legal_status", "severity": "medium",
                "pattern": "犯罪记录/法律敏感状态（**缺个人语境**）",
                "match": _bare.group(0)[:80],
                "reason": "英文/议题型高危词但**缺个人语境** ⇒ 不判 high（I10/t70）；"
                          "已计入 trinity_sensitive_high_downgraded_no_context_total（不静默）"})

    if not hits:
        # G2：**未命中类别但存在 PII** ⇒ action=redact（这正是本次扩范围要覆盖的情形）
        _rep = dict(_empty, truncated=truncated, action=policy_action(_empty))
        _record_decision(_rep, text=text)     # B1/t85：纯 PII 也是一次**判定** ⇒ 要留记录
        return _rep

    severity = "high" if any(h["severity"] == "high" for h in hits) else "medium"
    _bump("high_flagged_total" if severity == "high" else "medium_flagged_total")
    categories = sorted({h["category"] for h in hits})
    report = {
        "flagged": True,
        "severity": severity,
        "policy": _policy() if severity == "high" else None,
        "categories": categories,
        "hits": hits,
        "truncated": truncated,
        "downgraded": any(h.get("severity") == "medium" and h.get("category") in _DOWNGRADABLE
                          and "降级" in str(h.get("reason")) for h in hits),
        "no_context_downgraded": no_ctx,
        "pii": pii,
    }
    report["action"] = policy_action(report)
    report = _apply_help_context_gate(report, text)   # G4/D-9（t107）：语境门（**纯降级**）
    if report.get("severity") != "high":
        report["action"] = policy_action(report)
    _record_decision(report, text=text)        # B1/t85：唯一改动点（纯加法，不改判定）
    return report


def _apply_help_context_gate(report: Dict[str, Any], text: str) -> Dict[str, Any]:
    """G4/D-9 语境门（t107）：把**公益/知识语境**误判的 high 降级为 medium（脱敏存 + 留痕）。

    · **纯降级**：只把 `high` → `medium`，从不升级、从不改 medium/None；
    · **属"个人语境层"**：与 `TRINITY_HIGH_PERSONAL_CONTEXT` 同一个闸（`off` ⇒ 逐字回到 t70 前行为
      ⇒ t70 的 121/121 回滚判据不受影响）；
    · 门本体在 `trinity/security/help_context.py`（失败即**不动**，绝不影响写入路径）。
    """
    if not text:
        return report
    try:
        from trinity.security import help_context as _hc
        if not (high_personal_context_required() and _hc.gate_enabled()):
            return report
        out = _hc.apply_gate(report, text)
        if out.get("changed"):
            # ⭐ G9R-11/t125：**"降级"本身可计数**（D-9 裁定①：可见且可数）。
            # 按判定（文本）计：一条 high 被降为 medium ⇒ +1（进程内累计、重启归零）。
            _bump("help_context_downgraded_total")
        return out["report"]
    except Exception as _e:  # noqa: BLE001 —— 观测/降级层不得阻断写入，但**不静默**
        logger.debug("help-context gate skipped (%s): %s", type(_e).__name__, str(_e)[:120])
        return report


def policy_block_result(report: Dict[str, Any]) -> Dict[str, Any]:
    """组装拒存时返回给调用方的结果字典（与 ingest 返回结构同形）。"""
    import datetime as _dt
    return {
        "memory_id": "",
        "version_id": None,
        "sha256_hash": None,
        "error": "policy_refused_sensitive",
        "action": ACTION_REFUSE,
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
