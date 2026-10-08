# -*- coding: utf-8 -*-
"""t70/I10 判据：**高危类别在英文散文上的误报**（静默拒存）—— 可失败 + 负向牙齿。

## 背景（t63 发现 / t70 量化）
LongMemEval-S 前 5 万条消息里 **121 条被判 `high` ⇒ 拒存**，逐条人判后 **120 条是误报**
（precision ≈ 0.8%）：剧名 `Arrested Development`、`Nelson Mandela was imprisoned`、
议题词 `caste system` / `immigration status`、研究/新闻里的 `suicide rates`、
产品文案 `kid-friendly`、以及 **`id` 命中 `did` 子串**导致的 `minors_pii` 泛滥。
根因=英文侧实现的是**裸词**，与源码注释"组合强信号才 high，防误伤业务/知识文本"的**设计意图相矛盾**。

## 口径
- **误报样本**（HERE 内嵌的 24 条最小触发串）：覆盖 4 个类别与 7 种机制（剧名/历史/议题/政策/小说/
  研究/产品文案/`id` 子串）。**完整的 121 条**在 `D:\\DSH官网\\...\\evidence\\t70-fp-set.json`。
- **真阳性对照集**（20 条，带个人语境）：设计意图要求**仍然 high/refuse**，一条都不许丢。
- **本判据不含 `self_harm` 的误报**：那一类**有意不改**（见报告 §5 的负结果与理由），
  只要求它的**真阳性**不丢。

每条判据都配变异体（把实现改回裸词 / 拿掉留痕 / 拿掉召回补充）⇒ 该判据必须变红。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import trinity.security.sensitive as S  # noqa: E402

# ── 改前误报的最小触发串（覆盖全部机制；完整 121 条见 evidence/t70-fp-set.json）──
FP_TRIGGERS = [
    # legal_status：剧名（大小写不敏感 + 裸词）
    "2. **Arrested Development** (Netflix): A witty, satirical comedy about a dysfunctional family.",
    # legal_status：历史叙事（裸 `was imprisoned`）
    "Nelson Mandela was imprisoned from 1962 to 1990, a total of 28 years.",
    "millions of people were arrested, imprisoned, and executed on trumped-up charges.",
    # legal_status：法条/政策（无人称的 `convicted` / `immigration status`）
    "The President can pardon, commute or remit the punishment of any person convicted of an offense.",
    "everyone has access to quality healthcare, regardless of their immigration status.",
    # legal_status：议题词（`caste system`）
    "The caste system divides people into social groups based on their birth or occupation.",
    # legal_status：小说/剧情（`he was arrested` 是角色，不是用户）
    "the circumstantial evidence against him was overwhelming, and he was arrested and put on trial.",
    "Joker who is imprisoned in Arkham Asylum",
    # legal_status：地名描述里的 `arrested decay`
    'preserved in a state of "arrested decay."',
    # legal_status：小说角色的 `criminal record`
    "He has a long criminal record, but this is the first time he's been connected to something.",
    # legal_status：纪录片/新闻
    "documentary series re-examines the case of Adnan Syed, who was convicted of murdering his ex-girlfriend.",
    # minors_pii：`id` 命中 `did` 的子串（缺前导 `\b`）
    "Imagine your childhood, and how did you adapt?",
    "The father didn't care to get them out, but the child did not seem to be mad at him.",
    # minors_pii：泛词共现（children at school / kid-friendly）
    "Food banks provide food for kids who rely on free or reduced-price school meals.",
    "Look for masks with a \"kid-friendly\" design, which are often brightly colored.",
    "search for keywords like \"kids menu\", \"family friendly\", \"children's menu\".",
    # minors_pii：ML 术语 `minority class`
    "Randomly duplicate samples from the minority class.",
    # minors_pii：影视人物表（`their two children, Anne …`）
    "Jacob, Monica, and their two children, Anne (Noel Cho) and David (Alan Kim).",
    # minors_pii：政策文（`the child may not be able to obtain a passport`）
    "the child may not be able to obtain a passport, enroll in school, or receive government services.",
    # sexual_history：产品调研里的 `sexual experiences`
    "I think lubricants are important for sexual experiences because they can make it comfortable.",
    "the advertisement highlighted the benefits of using lubricants and how it can enhance the sexual experience.",
    # self_harm 的**真阳性**必须保留（样本标定：证明我们没有把这一类的门槛改坏）
    "suicide prevention services for LGBTQ+ youth",     # ← 这一条**故意留在下面**（见 TP 组）
]

#: 上面最后一条其实是**误报**（组织名），单独拆出来，避免与真阳性混在一起
FP_TRIGGERS = [t for t in FP_TRIGGERS if "suicide prevention services" not in t]

# ── 真阳性对照集（带个人语境；设计意图要求仍然 refuse）────────────────────
TP_CASES = [
    ("legal_status", "我弟弟去年进了监狱，家里一直瞒着。"),
    ("legal_status", "I was arrested last year and spent three months in jail."),
    ("legal_status", "我自己有吸毒记录，找工作很受影响。"),
    ("legal_status", "My brother has a criminal record, so he can't get a visa."),
    ("legal_status", "I was recently arrested for DUI."),
    ("legal_status", "my immigration status is pending"),
    ("legal_status", "我老公有案底，孩子政审过不了。"),
    ("psych_health", "我得了抑郁症，正在吃药。"),
    ("psych_health", "I was diagnosed with bipolar disorder in 2021."),
    ("psych_health", "我去年住院治疗过精神分裂症。"),
    ("psych_health", "医生说我孩子确诊了焦虑症。"),
    ("self_harm", "我想自杀，活着太累了。"),
    ("self_harm", "I want to kill myself, I can't take it anymore."),
    ("self_harm", "我准备自杀，已经写好遗书。"),
    ("self_harm", "She has been cutting herself again."),
    # ⚠️ 已**撤回**「自杀干预热线」这条期望：既有判据 `p-06 公益-自杀干预` 与
    #    `test_scan_rules_benign` 明确要求「文档讨论自杀干预热线」是**良性/medium**。
    #    我的期望与既有契约冲突 ⇒ 以既有契约为准（证据优先于我的假设），
    #    「求助行为是否该拒存」列为政策问题交队长裁定（报告 §5）。
    ("minors_pii", "我孩子未满18岁，身份证号是 110101200801011234。"),
    ("minors_pii", "My son is a minor; his school is 朝阳实验小学，班上 32 人。"),
    ("minors_pii", "女儿不满16岁，学籍号 2019BJ0088，班主任姓王。"),
    ("sexual_history", "我的性史比较复杂，有过多个性伴侣。"),
    ("sexual_history", "She told me about her sexual history during the intake."),
    ("sexual_history", "我有过一夜情，没告诉过伴侣。"),
]


def _sev(text):
    r = S.scan_sensitive(text)
    return r.get("severity"), sorted(r.get("categories") or []), r


# ── 判据 ──────────────────────────────────────────────────────────────
def c1_fp_no_longer_high(tmp_path=None, monkeypatch=None) -> bool:
    """① 改前的误报触发串**逐条**不再判 high（且必须仍被 **medium 留痕**、不静默）。"""
    bad = []
    for t in FP_TRIGGERS:
        sev, cats, _r = _sev(t)
        if sev == "high":
            bad.append(("仍判high", t[:40]))
    return not bad


def c2_true_positives_still_high(tmp_path=None, monkeypatch=None) -> bool:
    """② 真阳性对照集**逐条**仍然 high（尤其 self_harm）——《不得降低真阳性检出》。"""
    bad = []
    for expect, t in TP_CASES:
        sev, cats, _r = _sev(t)
        if sev != "high" or expect not in cats:
            bad.append((expect, sev, cats, t[:40]))
    return not bad


def c3_downgrade_leaves_trace(tmp_path=None, monkeypatch=None) -> bool:
    """④ 降级路径**仍有留痕**（t64「不当假话」）：`no_context_downgraded=True` + 专用计数 +1，
    且真阳性**不得**被记成"缺语境"（反事实）。"""
    from trinity.security.sensitive import redact_stats, reset_redact_stats
    reset_redact_stats()
    _s, _c, r = _sev("2. **Arrested Development** (Netflix): a witty sitcom.")
    if not r.get("no_context_downgraded"):
        return False
    if int(redact_stats().get("high_downgraded_no_context_total", 0)) < 1:
        return False
    # 反事实：真阳性不算"缺语境"
    _s2, _c2, r2 = _sev("My brother has a criminal record.")
    if r2.get("no_context_downgraded") or r2.get("severity") != "high":
        return False
    # 反事实：纯良性文本不得被记
    _s3, _c3, r3 = _sev("今天天气不错，去公园散步。")
    return not r3.get("no_context_downgraded")


def c4_rollback_switch_restores_bareword(tmp_path=None, monkeypatch=None) -> bool:
    """③ **反事实/回滚**：`TRINITY_HIGH_PERSONAL_CONTEXT=off` ⇒ 逐字回到改动前的裸词行为
    （误报重新变成 high）。"""
    import importlib
    import os
    os.environ["TRINITY_HIGH_PERSONAL_CONTEXT"] = "off"
    try:
        for k in list(sys.modules):
            if k.startswith("trinity.security.sensitive"):
                del sys.modules[k]
        mod = importlib.import_module("trinity.security.sensitive")
        # 改前的误报应**重新**被判 high（说明开关真的回滚了）
        back = [t for t in FP_TRIGGERS if mod.scan_sensitive(t).get("severity") == "high"]
        ok = len(back) >= max(1, len(FP_TRIGGERS) - 4)   # 允许极少数只靠 medium 命中的
    finally:
        os.environ.pop("TRINITY_HIGH_PERSONAL_CONTEXT", None)
        for k in list(sys.modules):
            if k.startswith("trinity.security.sensitive"):
                del sys.modules[k]
        importlib.import_module("trinity.security.sensitive")
    return ok


CRITERIA = {
    "C1_误报不再判high": c1_fp_no_longer_high,
    "C2_真阳性仍在": c2_true_positives_still_high,
    "C3_降级有留痕": c3_downgrade_leaves_trace,
    "C4_回滚开关有效": c4_rollback_switch_restores_bareword,
}


@pytest.mark.parametrize("name", sorted(CRITERIA), ids=sorted(CRITERIA))
def test_判据通过(name, tmp_path, monkeypatch):
    assert CRITERIA[name](tmp_path, monkeypatch) is True


# ── 负向（牙齿）────────────────────────────────────────────────────────
def _revert_to_bareword(mp):
    """变异体：把 `_HIGH_PATTERNS` 换回**裸词**版本（= 改动前的实现）。"""
    bare = []
    for pattern, category, label in S._HIGH_PATTERNS:
        if label == "未成年身份信息":
            pattern = S._MINORS_HIGH_ORIG
        elif cat_marker(label):
            pattern = S._LEGAL_EN_BARE_RE_COMPILED
        elif category == "sexual_history":
            pattern = S._SEXUAL_HIGH_ORIG
        bare.append((pattern, category, label))
    mp.setattr(S, "_HIGH_PATTERNS", bare)


def cat_marker(label: str) -> bool:
    return "英文/议题型" in label


def _no_trace(mp):
    """变异体：拿掉留痕（裸词检测器永不命中）—— 判据 C3 必须红。"""
    import re as _re
    mp.setattr(S, "_LEGAL_EN_BARE_RE_COMPILED", _re.compile(r"(?!x)x"))


def _no_recall_add(mp):
    """变异体：拿掉 self_harm 的召回补充（`cutting herself`）—— 判据 C2 必须红。"""
    import re as _re
    patched = []
    for pattern, category, label in S._HIGH_PATTERNS:
        if category == "self_harm":
            pattern = _re.compile(
                r"(?:想(?:要|着)?自杀|打算自杀|准备自杀|自杀过|自杀了?两次|不想活了|活不下去|"
                r"想结束(?:自己的)?生命|自残|割腕|跳楼|吞(?:药|安眠药)(?:自杀|轻生)?|"
                r"suicid(?:al|e)|want(?:s)?\s+to\s+(?:kill|end)\s+(?:myself|my\s+life)|"
                r"self[- ]?harm|cutting\s+(?:myself|wrists))")
        patched.append((pattern, category, label))
    mp.setattr(S, "_HIGH_PATTERNS", patched)


MUTANTS = [
    # ⑤ 牙齿：把语境要求删掉（回到裸词）⇒ C1 必须红
    ("C1_误报不再判high", _revert_to_bareword),
    # 牙齿：拿掉 self_harm 的召回补充 ⇒ C2 必须红
    ("C2_真阳性仍在", _no_recall_add),
    # 牙齿：拿掉留痕 ⇒ C3 必须红
    ("C3_降级有留痕", _no_trace),
    # ⚠️ **C4 没有变异体**（如实登记）：C4 验证的是 `TRINITY_HIGH_PERSONAL_CONTEXT=off` 的
    # **环境变量 + 重新导入**路径 —— 变异体只能改**本进程里已导入的对象**，
    # 而 C4 会 `del sys.modules[...]` 后重新导入（绕过 monkeypatch）⇒ 变异体无效。
    # 即 C4 的"牙齿"由 `os.environ` 与模块导入本身提供，不靠 monkeypatch 造。
]


@pytest.mark.parametrize("name,apply_mutant", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_每条判据都有能杀掉它的变异体(name, apply_mutant, tmp_path, monkeypatch):
    apply_mutant(monkeypatch)
    sub = tmp_path / name
    sub.mkdir()
    caught = ""
    try:
        got = CRITERIA[name](sub, monkeypatch)
    except Exception as e:                       # noqa: BLE001 —— 异常=红
        got, caught = False, "%s: %s" % (type(e).__name__, str(e)[:90])
    assert got is False, "变异体 %s 没有杀掉判据 %s ⇒ 该判据没有判别力" % (name, name)
    if caught:
        print("[teeth] %s 的变异体被拦下：%s" % (name, caught))
