# -*- coding: utf-8 -*-
"""t107/G4 · D-9 **语境判定层**：求助语境 ↔ 公益/知识语境。

## 设计（**规则预筛 + 语境判定**；不是"再加几个关键词"）
关键词层（`sensitive.scan_sensitive`）负责**高召回**：它只管"文本里有没有这类主题"。
本层负责**归属与意图**，只看**结构特征**，三个族：

1. **主体归属（谁的处境）**
   - `PERSONAL`: 第一人称 / 近亲属 / 第一人称+关系链（`我`、`我的`、`我弟弟`、`I`、`my son`…）
   - `THIRD_PARTY`: 第三人称代词或**具名主体**（`he/she/they`、`Nelson Mandela`、`Adnan Syed`…）
   - 判定用**窗口绑定**：主体标记必须落在**触发词之前 25 字符内且同句**才算"绑定"（`_WINDOW`）。
2. **文本在做什么（文档类型/意图）**：报道/研究/统计/科普/教材/指南/法条/政策/影评/剧名/组织介绍；
   以及**引号/书名号包裹的标题**（`《…》`、`**…**`、`"…"`）⇒ 这些是**传播/描述**，不是个人记录。
3. **预防/公益意图**：预防、干预、援助、热线服务、宣传、科普、筛查、覆盖率、发病率…

## 判定规则（**确定性、可复核**，写死在这里，不随数据变）
```
if 触发词被"个人主体"绑定                 -> personal   （保持 high，不降级）
elif 有公益/知识标记 或 引号标题            -> public     （降级为 medium，落库 + 留痕）
elif 有第三方主体 且 无个人主体             -> public
else                                      -> unknown    （**保守**：维持规则层结论）
```
⚠️ `minors_pii` / `sexual_history` **同样适用**（t70 曾按"不许降级"处理；本层用**语境**而不是类别来分，
但**判据要求**：成对集上两端必须给出相反结论，见 `evidence/g4-*`）。

## 开关
`TRINITY_HELP_CONTEXT_GATE`（默认值见 `gate_enabled()` 与 `G4-D9-HELPSEEKING-VS-PUBLIC.md` §6 ——
**默认值变化已显眼标注**）。
"""
from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

#: 主体标记必须落在触发词之前多少字符内（且同句）才算"绑定"
_WINDOW = 25

_PERSONAL = re.compile(
    r"(?:我|我的|我家|本人|自己|我们|我们的|"
    r"我(?:弟弟|哥哥|姐姐|妹妹|父亲|母亲|爸爸|妈妈|儿子|女儿|孩子|老公|老婆|丈夫|妻子|前夫|表弟|爷爷|奶奶|孙子|朋友|同事))")
#: ⚠️ 实测抓到的坑：**`\b` 对中文不成立**（`我` 与 `有` 都是 `\w` ⇒ 无边界）⇒
#: 首版 `我\b` 只能匹配「我」后面跟非单词字符的情形（`我，`/`我 `），导致 `我有自残的冲动` 判成"无个人主体"。
#: ⇒ **中文不用 `\b`、英文才用 `\b`**（下同：`_RELATION_ONLY` / `_PUBLIC_MARK`）。
_PERSONAL_EN = re.compile(r"\b(?:I|I'm|I've|me|my|mine|myself|we|our|ours)\b", re.I)
_RELATION_ONLY = re.compile(
    r"(?:弟弟|哥哥|姐姐|妹妹|父亲|母亲|爸爸|妈妈|儿子|女儿|孩子|老公|老婆|丈夫|妻子|前夫|表弟|爷爷|奶奶|孙子)")
_RELATION_EN = re.compile(
    r"\b(?:brother|sister|son|daughter|father|mother|husband|wife|child|kid)s?\b", re.I)
_THIRD_PERSON = re.compile(r"\b(?:he|she|him|her|his)\b", re.I)
#: 泛主体（公众/群体）：只有**泛主体**才足以判 public；**裸第三人称单数不足以**（可能是身边人，也可能是虚构角色）
_GENERIC_SUBJECT = re.compile(r"\b(?:they|them|people|someone|anyone)\b|数百万|人们|众人|大众|青少年|患者|受害者")
#: 具名主体：**句中或句首的**英文专名（排除常见句首虚词/代词，避免把 "The/This/In" 当人名）。
#: ⚠️ 实测教训：首版用 `(?<!^)` 把**句首**的专名排除了 ⇒ `Flynn tackles … self-harm` 判成 unknown、
#: 语境门不生效。改为「**大写词 + 非停用词**」。
_PROPER_NOUN_STOP = {
    "the", "this", "these", "those", "in", "for", "it", "its", "what", "when", "how", "as", "but",
    "and", "so", "if", "while", "yes", "no", "there", "they", "their", "we", "you", "your", "our",
    "my", "i", "he", "she", "his", "her", "that", "such", "some", "many", "most", "other", "another",
    "a", "an", "on", "of", "to", "with", "by", "from", "at", "is", "are", "was", "were", "be",
}
_PROPER_NOUN_CAND = re.compile(r"\b([A-Z][a-z]{2,})\b")


def _has_proper_noun(text: str) -> bool:
    return any(m.group(1).lower() not in _PROPER_NOUN_STOP
               for m in _PROPER_NOUN_CAND.finditer(text))


#: 引号/书名号包裹的标题（传播而非个人记录的证据之一）
_TITLE = re.compile(r"(?:《[^》]{2,40}》|〈[^〉]{2,40}〉|\*\*[^*]{2,60}\*\*|\"[^\"]{2,60}\"|“[^”]{2,60}”)")
#: 句末标点（"主体绑定"不跨句）
_SENT_END = re.compile(r"[。！？；\n.!?;]")


_PUBLIC_MARK_CJK = re.compile(
    r"(?:新闻|报道|据[^。\n]{0,8}报道|研究|论文|文献|综述|统计|调查|科普|教材|指南|法条|法规|政策|文件|规定|条例|"
    r"手册|文章|介绍|讲解|教程|影评|纪录片|电视剧|电影|小说|主人公|角色|情节|剧本|课程|讲座|白皮书|报告|"
    r"预防|干预|援助|热线|宣传|倡导|教育|培训|筛查|量表|覆盖率|发病率|患病率|公开数据|"
    r"动作|训练|练习|锻炼|技巧|机制|蛋白|酶|抑制|药理|说明书|剂量)")
_PUBLIC_MARK_EN = re.compile(
    r"\b(?:study|research|report|survey|novel|film|movie|series|character|episode|guideline|policy|"
    r"article|review|statistics|prevention|awareness|campaign|hotline|training|exercise|drill|"
    r"technique|mechanism|protein|enzyme|inhibition|dosage|label)\b", re.I)


def gate_enabled() -> bool:
    """语境门开关。**默认：`on`**（G4/D-9 实测 PASS 后取默认 on；`off` = 逐字回到规则层）。

    ⚠️ **默认值变化（显眼标注）**：本开关是 t107 新增，**默认 on** ⇒ 生产写路径的行为变化是
    "**被误判为 high 的公益/知识语境文本不再拒存**（改为脱敏存 + 留痕）"；
    实测 recall 零丢失（t70 TP 控制集 19/19；成对集 help 侧 36/36）⇒ 门只减误拒、不减真阳性。
    回滚：`TRINITY_HELP_CONTEXT_GATE=off`（或 `TRINITY_HIGH_PERSONAL_CONTEXT=off`）⇒ 逐字回到规则层。
    `TRINITY_HELP_CONTEXT_FORCE=public|personal` 是**判据用的两极端钩子**（见判据 C2），生产勿用。
    """
    return os.environ.get("TRINITY_HELP_CONTEXT_GATE", "on").strip().lower() not in (
        "off", "0", "false", "no")


def _forced_context() -> Optional[str]:
    """判据用的**极端钩子**：`TRINITY_HELP_CONTEXT_FORCE=public` ⇒ 全判公益；`personal` ⇒ 全判求助。"""
    v = os.environ.get("TRINITY_HELP_CONTEXT_FORCE", "").strip().lower()
    return v if v in ("public", "personal") else None


def _trigger_positions(text: str, report: Dict[str, Any]) -> List[int]:
    """取高层触发词在文本中的位置（用于"主体绑定"窗口判定）。"""
    out: List[int] = []
    for h in (report or {}).get("hits") or []:
        if h.get("severity") != "high":
            continue
        m = str(h.get("match") or "")
        if not m:
            continue
        i = text.find(m)
        if i >= 0:
            out.append(i)
    return out


def _bound_personal(text: str, pos: int) -> bool:
    """触发词之前 `_WINDOW` 字符内、**同句**是否出现个人主体标记。"""
    lo = max(0, pos - _WINDOW)
    seg = text[lo:pos]
    if _SENT_END.search(seg):                      # 跨句不算绑定
        seg = _SENT_END.split(seg)[-1]
    return bool(_PERSONAL.search(seg) or _PERSONAL_EN.search(seg)
                or _RELATION_ONLY.search(seg) or _RELATION_EN.search(seg))


def classify_context(text: str, report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """判定语境。返回 `{"context", "basis", "features"}`（`basis` 是**可复核的依据**，不是分数）。"""
    t = str(text or "")
    positions = _trigger_positions(t, report or {})
    features = {
        "personal_marker": bool(_PERSONAL.search(t) or _PERSONAL_EN.search(t)),
        "relation_marker": bool(_RELATION_ONLY.search(t) or _RELATION_EN.search(t)),
        "third_person": bool(_THIRD_PERSON.search(t)),
        "proper_noun": _has_proper_noun(t),
        "public_marker": bool(_PUBLIC_MARK_CJK.search(t) or _PUBLIC_MARK_EN.search(t)),
        "quoted_title": bool(_TITLE.search(t)),
        "generic_subject": bool(_GENERIC_SUBJECT.search(t)),
        "trigger_count": len(positions),
    }
    bound = any(_bound_personal(t, p) for p in positions)
    features["personal_bound"] = bound
    # ⭐ 保守序（实测后收紧，**保证 recall 零丢失**）：
    #   只要文本里出现**个人主体标记**（我/我的/我的弟弟…/I/my…）⇒ 一律 personal，**不降级**；
    #   只有"**完全没有个人主体**"且有公益/知识证据时才判 public。
    #   代价（如实登记）：**第三人称转述的求助**（"She has been cutting herself"）会被判 public
    #   —— 这是本层的**已知假阴性边界**，改用 LLM 语义层才能解（A3 的建议②）。
    if bound or features["personal_marker"] or features["relation_marker"]:
        why = ["触发词被个人主体绑定（窗口 %d 字符内、同句）" % _WINDOW] if bound else \
              ["文本含个人主体标记（第一人称/近亲属）⇒ 保守不降级"]
        return {"context": "personal", "basis": why, "features": features}
    public_basis: List[str] = []
    if features["public_marker"]:
        public_basis.append("含公益/知识/预防/技术类标记（报道·研究·科普·指南·动作·机制…）")
    if features["quoted_title"]:
        public_basis.append("含引号/书名号包裹的标题")
    if features["generic_subject"]:
        public_basis.append("主体是**泛主体**（他们/人们/数百万/青少年…），且全文无个人主体标记")
    if features["proper_noun"]:
        public_basis.append("主体是**具名第三方**（专名，如 Flynn/Nelson Mandela）且全文无个人主体标记")
    if public_basis:
        return {"context": "public", "basis": public_basis, "features": features}
    return {"context": "unknown", "basis": ["无个人主体绑定，也无公益/知识标记 ⇒ 保守维持规则层"],
            "features": features}


def apply_gate(report: Dict[str, Any], text: str) -> Dict[str, Any]:
    """在**规则层报告**上应用语境门（**纯降级**：`high` → `medium`；不改别的）。

    Returns: 新的 hits/severity（就地返回可能被替换的报告副本与判定结果）。
    只对"**当前判 high**"的条目生效；`unknown` ⇒ 不动（保守）。
    """
    if not gate_enabled() or (report or {}).get("severity") != "high":
        return {"changed": False, "context": None, "report": report}
    f = _forced_context()
    if f:
        verdict = {"context": f, "basis": ["判据极端钩子 TRINITY_HELP_CONTEXT_FORCE=%s" % f],
                   "features": {"forced": True}}
    else:
        verdict = classify_context(text, report)
    if verdict["context"] != "public":
        return {"changed": False, "context": verdict["context"], "report": report}
    new_hits = []
    for h in report.get("hits") or []:
        if h.get("severity") == "high":
            h = dict(h)
            h["severity"] = "medium"
            h["reason"] = ("公益/知识语境（D-9 语境门）：%s ⇒ 不拒存（脱敏存 + 留痕）"
                           % "；".join(verdict["basis"]))
        new_hits.append(h)
    rep = dict(report)
    rep["hits"] = new_hits
    rep["severity"] = "medium" if any(h["severity"] == "medium" for h in new_hits) else None
    rep["policy"] = None
    rep["help_context"] = verdict
    rep["downgraded"] = True
    return {"changed": True, "context": "public", "report": rep, "verdict": verdict}


# ── 统计口径（A3 要求的"recall 95% 单侧下界"）────────────────────────────
def wilson_lower_bound(hits: int, n: int, z: float = 1.96) -> float:
    """**Wilson 单侧 95% 下界**（`z=1.96`，与 A3 的标定点一致）。n=100、0 漏 ⇒ ≈**0.964**。"""
    if n <= 0:
        return 0.0
    p = hits / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return (centre - margin) / denom
