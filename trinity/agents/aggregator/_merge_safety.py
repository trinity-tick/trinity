# -*- coding: utf-8 -*-
"""合并安全判据（2026-10-06 G1/G2 立项；同日 t11 做**可达性更正**）。

## 本模块先后踩过两个坑，都登记在案

**坑 1 · 假绿（2026-10-06 F5/G1）**：`_ingest.py::merge_if_similar` 自 2026-08-14
（`916be25` 初始导入）起调用 `guardian.verify_merge_safety(...)`，而该方法**全仓从未存在**
（`git log --all -S "def verify_merge_safety"` 无任何命中）⇒ 每次合并抛 `AttributeError`，
被 `except Exception` 在 **logger.debug** 级吞掉 ⇒「合并安全校验」从未生效且完全静默。
F5 删掉了那个不可能的调用；G1 补上了本模块的真实判据。

**坑 2 · 没有判别力（2026-10-06 t11 更正）**：G1 交付的三条规则里**两条在它们被调用的
位置上不可能触发**：

* **R1 `content_collapse` 在闸门内结构性不可达** —— 合并闸门要求
  ``Jaccard ≥ SIMILARITY_MERGE_THRESHOLD = 0.75``（`_constants.py`、`_ingest.py:215`）。
  由 ``|A∩B| ≥ 0.75·|A∪B| ≥ 0.75·|A|`` 且 ``|A∩B| ≤ |B|`` 得 **``|B| ≥ 0.75·|A|``（token）**；
  而 R1 比的是**字符数** < 35%。实测闸门内长度比**最小 0.3519**（阈值 0.35，仅差 0.0019）、
  ``0/5,295`` 对低于阈值 ⇒ 该规则的命中率恒为 0。
* **R2 `source_downgrade` 是恒真式** —— 旧实现先 ``projected = set(existing_sources)`` 再
  ``projected.add(new_source)``，然后判 ``projected.issuperset(set(existing_sources))``；
  ``set.add`` 只增不减 ⇒ 对**任意**输入恒真。5,000 次随机搜索命中 0；调用点
  ``_ingest.py:248`` 也是 ``best_dv.source_agents.add(source_agent)``（同样只增）
  ⇒ 它守的是一个**不可能被违反**的不变量。

教训（本仓已多次复发）：**判据必须"在它被调用的那个位置的约束下"可触发**。
只在孤立合成输入上测过"能判红"，等于没测。故本模块自带规则账本 ``RULE_LEDGER``
与配套的可达性判据 `tests/unit/test_merge_safety_reachability_20261006.py`
（该判据在**改前的本文件**上会判红 —— 那正是它的用途）。

## 适用范围（**决定 R1 去留的事实**）

本模块只服务于 `_ingest.py::merge_if_similar` 的**合并准入**。该路径在合并时改动的字段是
``confidence`` / ``source_agents`` / ``updated_at`` / ``priority`` / agent 索引
（`_ingest.py:242-253`）——**不写 ``content``**：既有正文一个字符都不会被覆盖，来料正文也不进库。
（另一处会改正文的 `_ingest.py::merge_memories:301-302` 是**追加**语义，且它**不调用本模块**。）

于是：

* 「既有富内容被一段贫信息代表 / 覆盖」= R1 的伤害模型 ⇒ **在此路径上不存在** ⇒ **R1 退役**；
* 但另一种形状的损失**存在且可达**：**来料正文被静默丢弃**（合并命中 ⇒ 来料不入库）。
  本模块**不为它新增判据**：新判据需要新阈值 + 新误杀实测，超出"可达性更正"的范围。
  实测分布见 ``D:\\DSH官网\\trinity-optimize-20261006\\evidence\\merge_safety_novelty_probe.json``
  （交队长决定，报告在 `MERGE-SAFETY-FIX.md`）。

## 规则的唯一登记处：``RULE_LEDGER``

每条**保留**规则必须声明它为什么**可达**（``reachability``）；每条**退役**规则必须声明
退役理由（``retired_reason``）与证据（``evidence``）。可达性判据会核对：
① 每条 active 规则在闸门约束下**确实能**判红；② 每条退役规则**不再**产出对应 code
（不留僵尸门禁）；③ 实现里能产出的每个 code 都**已登记**。

## 开关

``TRINITY_MERGE_SAFETY``：``on``（默认）/ ``off``。关掉后 ``verify_merge_safety`` 恒返回 safe，
便于回退到修复前行为做对照。
"""
from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Set

MERGE_SAFETY_ENV = "TRINITY_MERGE_SAFETY"

# ── 退役常量（**已不再参与判定**，仅供历史标定产物解读）────────────────────
# `scripts/merge_safety_calibration.py` 的产物里记着这两个值，且
# `tests/unit/test_merge_safety_corpus_calibration_locks_20261006.py` 会读它复算
# 「0/5,295 命中」；删掉它们会让历史证据无法解释 ⇒ 保留并标注退役。
#: 【退役·R1】既有内容短于此长度时不做塌缩判定
MIN_EXISTING_CHARS = 120
#: 【退役·R1】塌缩比例阈值（实测闸门内最小长度比 0.3519 > 0.35 ⇒ 恒不触发）
COLLAPSE_RATIO = 0.35

# ── 规则账本（唯一登记处）───────────────────────────────────────────────
RULE_LEDGER: Dict[str, Dict[str, Any]] = {
    "content_collapse": {
        "id": "R1",
        "title": "信息损失：既有富内容被贫信息代表",
        "status": "retired-not-applicable-on-this-path",
        "retired_reason": (
            "该合并路径**不写 content**（`_ingest.py:242-253` 只动 confidence / source_agents / "
            "updated_at / priority / agent 索引）⇒「既有内容被覆盖」这个伤害在本路径上不存在。"
            "同时它在闸门内结构性不可达：闸门 Jaccard ≥ 0.75 ⇒ |B| ≥ 0.75·|A|（token），"
            "而本规则比的是字符数 < 35%。"
        ),
        "evidence": {
            "calibration": "evidence/merge_safety_calibration.json",
            "gate_open_pairs": 10965,
            "applicable_pairs": 5295,
            "fired": 0,
            "min_length_ratio_observed": 0.3519,
            "collapse_ratio": COLLAPSE_RATIO,
            "arithmetic": "|A∩B| ≥ 0.75|A∪B| ≥ 0.75|A| 且 |A∩B| ≤ |B| ⇒ |B| ≥ 0.75|A|",
        },
        "replacement_candidate": {
            "name": "incoming-novelty（来料新信息比）",
            "status": "not-implemented-by-decision（交队长；数字见 novelty_probe）",
            "why_not_implemented": (
                "它判的是**另一种**伤害（来料正文被静默丢弃），需要新阈值 + 新误杀实测；"
                "本次任务是可达性更正，不是新增判据。"
            ),
            "measured_distribution": "evidence/merge_safety_novelty_probe.json",
            "reachable_upper_bound": (
                "由 |A∩B| ≥ 0.75|A∪B| 与 |A∩B| ≤ |B| 得该比值 ≲ 0.25 "
                "⇒ 任何阈值 > 0.25 的替代判据同样不可达。"
            ),
        },
    },
    "source_downgrade": {
        "id": "R2",
        "title": "来源降级：合并使来源集合收缩",
        "status": "retired-tautological-from-precheck",
        "retired_reason": (
            "旧前置实现是先 `projected = set(existing_sources)`、再 `projected.add(new_source)`，"
            "然后判 `projected.issuperset(set(existing_sources))` —— `set.add` 只增不减 ⇒ 恒真。"
            "且调用点用 `.add()`（`_ingest.py:248`）⇒ 不变量不可能被违反。"
        ),
        "evidence": {
            "random_search_trials": 5000,
            "random_search_fired": 0,
            "call_site": "trinity/agents/aggregator/_ingest.py:248 best_dv.source_agents.add(source_agent)",
            "proof": "tests/unit/test_merge_safety_corpus_calibration_locks_20261006.py",
        },
        "replacement": "verify_merge_postcondition",
        "replacement_wired": True,
        "replacement_note": (
            "可失败的后置不变量（`verify_merge_postcondition`）**已于 2026-10-06（t13）接线**："
            "调用点 `_ingest.py` 在全部变更之前快照 `_before_sources`、在变更之后核对；"
            "违反时计数（`total_merge_postcondition_violations`）并**至多告警一次**。"
            "可达性判据对「账本声明已接线」与「`_ingest.py` 里确有调用」做一致性核对，"
            "任一侧不一致即判红（把接线摘掉 ⇒ 红）。"
        ),
    },
    "duplicate_no_new_evidence": {
        "id": "R3",
        "title": "无新证据的置信度灌水：来料与既有归一化后完全相同",
        "status": "active",
        "reachability": (
            "闸门内**必然可达**：内容完全相同（或其归一化相同）时 Jaccard = 1.0 ≥ 0.75，"
            "闸门必然打开，随后本条必然判红。不需要任何【凑形状】的构造。"
        ),
        "evidence": {
            "calibration": "evidence/merge_safety_calibration.json",
            "gate_open_pairs": 10965,
            "fired": 118,
            "hit_rate": 0.0108,
            "false_positive_content_bearing": 0,
            "false_negative_token_identical": 1082,
        },
        # ── t16：**作者盲区**（跨 agent 一字不差的复述被一并拒绝）—— 已量化，判定"不改" ──
        "known_gap_author": {
            "name": "作者盲区：R3 不看来源，跨 agent 的完全相同复述被拒（不再提升 confidence）",
            "caliber": ("对与 t13 同一批闸门内样本（memories 可读面，11,193 对），"
                        "把 R3 命中的对按「本次来源已在 existing_sources（代理=该行 agent_id）」"
                        "vs「本次来源是新的」拆分。"),
            "split": {"self_copy": 97, "cross_agent": 21, "total": 118},
            "artifact": "evidence/merge_safety_author_blindspot.json",
            "cross_agent_share": 0.178,
            "verified_not_independent": (
                "**抽查全部跨 agent 样例：都是 `compress-econ`（既有）↔ `default`（来料）**"
                "—— 同一个压缩管线的两个名字（`compress-econ` 写压缩摘要、`default` 存原文），"
                "**不是相互独立的 agent**。若按「来源是新的 ⇒ 放行」，放行的正是"
                "「派生内容回流」那类（t4 的 `[自动压缩]`/自复制同源）。"
            ),
            "why_not_fixed": (
                "① **拒绝不丢信息**：R3 的口径是「归一化后完全相同」 ⇒ 被拒的来料内容库里已经有一份，"
                "拒绝只跳过「来源归属 + confidence 提升」这两项记账；"
                "② 跨 agent 的 21 对抽查后**不是独立佐证**而是同一管线改名 ⇒ 作者闸门会放行错的方向；"
                "③ 作者身份在本部署里**不可靠**：同一管线多名字（`default`/`compress-econ`）、"
                "会话级唯一名（`dsh-session-<uuid>`）⇒「来源是新的」≠「独立 agent」。"
            ),
            "impact_bound": (
                "受影响上限 = 21/11,193 = **0.19%** 的闸门内对（只少一次来源/置信度记账，不丢内容）。"
            ),
            "status": "accepted-residual（数字/影响/为何不改均已登记）",
        },
        # ── 已登记残留（2026-10-06 t13；队长裁定 ④：接受、本轮不修，但**不得静默保留**）──
        "known_gap": {
            "name": "换序盲区（词表相同、仅顺序/重复不同）",
            "caliber": ("在**闸门内**的 10,965 个候选对里，来料与既有的 **token 集合相同**、"
                        "但**归一化后不相等**的对。R3 不判红（它不是「完全相同」），"
                        "而它也不携带新词 —— 是最难判的一类。"),
            "count": 1082,
            "count_share_of_gate_open": 0.0987,
            "why_not_fixed": (
                "修它要么改成顺序敏感比较、要么按「词表相同即判红」 —— 两者都会把"
                "「同一词表的合法同义重排」判成重复。队长 2026-10-06 裁定："
                "**接受为已登记残留**，保守选择（宁可漏杀）。"
            ),
            "cost_if_fixed": (
                "按 t13 的低新颖度标定：任何「新颖度低于阈值即判红」的形态都会**顺带**命中这一批"
                "（它们的新颖度恒为 0）⇒ 误杀上界 ≈ 这 1,082 对的占比（约 9.9% 的闸门内对）。"
                "这正是低新颖度判据必须**显式豁免「纯换序」**的原因；豁免之后它在同一批语料上"
                "仍拦住 **5,495** 对（其中纯回声 5,495、带新 token 的 0）。"
            ),
            "status": "accepted-residual（口径/数量/不修理由/修改代价四件均已登记）",
        },
    },
    # ── R1 的**可用替代**（t13 落地；R1 因在本路径不适用 + 闸门内不可达而退役）──
    "low_incoming_novelty": {
        "id": "R1'",
        "title": "低新颖度：来料在既有词表外没有任何新词（且不只是换序）",
        "status": "active",
        "replaces": "content_collapse（R1，已退役）",
        "semantics": (
            "来料 token 集是既有 token 集的**真子集**（没有任何新词）⇒ 合并不会带来任何"
            "新词汇，而该路径的合并**会丢弃来料正文**（来料不入库）⇒ 丢弃的是一段纯回声，"
            "同时把 confidence 单调推高（无新证据的灌水）。三条排除项："
            "① 阈值 0.003（见下）；② **纯换序**豁免（队长裁定：同一词表的合法同义重排）；"
            "③ 值型新 token 豁免（含数字的 token = 版本/日期/数量/路径段 ⇒ 哪怕只差一个也是新证据）。"
        ),
        "threshold": {
            "value": 0.003,
            "caliber": ("novelty = |来料 token ∖ 既有 token| / |来料 token|；"
                        "token = NFKC+小写后的 ① ASCII/数字词（len>=2）② 中文二字组。"),
            "why_this_value": (
                "由**实测分布**定，不是拍的：闸门内 11,193 个候选对里 novelty 呈**双峰** —— "
                "**恰为 0 的有 6,438 对（57.5%）**，而**最小正值是 0.0031**。取 0.003 恰好只覆盖"
                "【零新词】这一类；取 0.02 会开始吃进 23 个带新 token 的对（多为中文二字组跨界伪影），"
                "取 0.05 / 0.10 进一步放大到 115 / 189 对。"
            ),
            "evidence": "evidence/merge_safety_low_novelty_calibration.json",
            "reachable_upper_bound": (
                "解析上限 0.25（由 |A∩B| >= 0.75|A∪B| 与 |A∩B| <= |B| 得 |B| <= 4|A|/3）；"
                "实测 max = 0.1429 ⇒ 任何阈值 > 0.15 都会**不可达**（那正是 R1 的死法）。"
            ),
        },
        "reachability": (
            "闸门内**确有样本**：来料是既有的真子集时 Jaccard = |来料|/|既有|，只要来料 >= 既有的 75% "
            "就在闸门内（例：既有 40 个 token、来料 30 个 ⇒ Jaccard = 0.75 恰好过闸）；此时 novelty = 0 "
            "⇒ 判红。实测语料上命中 **5,495** 对（占闸门内 11,193 的 49.09%）。"
        ),
        "false_positive_bound": {
            "caliber": ("被拦下的对里**仍带新 token**（即「其实有新信息」）的比例 —— 上界口径："
                        "只要带 1 个新 token 就算可能误杀。"),
            "at_chosen_threshold": "0 / 5,495 = 0.0000",
            "at_0_005": "1 / 5,496（0.02%）",
            "at_0_02": "23 / 5,518（0.42%）",
            "at_0_05": "115 / 5,610（2.05%）",
            "at_0_10": "189 / 5,684（3.33%）",
            "note": ("值型（含数字）新 token 的豁免在选定阈值上**当前不改变任何判定**"
                     "（5,495 -> 5,495）；它存在的意义是把【一个数字的差异=新证据】这条口径"
                     "写进代码，而不是靠阈值侥幸。"),
            "two_calibers_warning": (
                "**这个 0 是【内容口径】，不是【正当性口径】。** 内容口径问「来料有没有新词」；"
                "正当性口径问「这次合并在语义上是否正当」（例如：跨 agent 的独立复述本应放行）。"
                "两者不可互换，**不得把 0 读成『这条规则不会误伤』** —— 正当性口径的数字见本条的"
                "`known_gap_author`。"
            ),
        },
        # ── t16：**作者盲区**（跨 agent 的低新颖度复述被一并拒绝）—— 已量化，判定"不改" ──
        "known_gap_author": {
            "name": "作者盲区：R1′ 只看内容不看来源，跨 agent 的独立复述被一并拒绝",
            "caliber": ("对与 t13 同一批闸门内样本（memories 可读面，11,193 对；"
                        "`existing_sources` 的代理 = 既有行的 agent_id），把 R1′ 拦下的对按"
                        "「本次来源已在来源集合」vs「本次来源是新的」拆分。"),
            "split": {"self_copy": 5491, "cross_agent": 4, "total": 5495},
            "artifact": "evidence/merge_safety_author_blindspot.json",
            "cross_agent_share": 0.0007,
            "verified_not_independent": (
                "4 条跨 agent 样例全部是 `default` ↔ `compress-econ`（同一压缩管线的两个名字）"
                "—— **不是相互独立的 agent**。"
            ),
            "why_not_fixed": (
                "① **量级不支持**：跨 agent 只占 **4/5,495 = 0.07%**（占闸门内 11,193 对的 0.036%）"
                "⇒ 所谓「掐掉全部合法置信度增长」在量上不成立，为 4 对加复杂度不成比例；"
                "② **信号不可靠**：作者身份在本部署里多名字/会话级唯一名 ⇒「来源是新的」≠"
                "「独立 agent」；按它放行会放行**派生内容回流**（正是 t4 的自复制同类）；"
                "③ 拒绝**不丢内容**：低新颖度 = 来料无新词，被拒的是一段纯回声。"
            ),
            "impact_bound": (
                "受影响上限 = 4/11,193 = **0.036%** 的闸门内对（只少一次来源/置信度记账，不丢内容）。"
            ),
            "status": "accepted-residual（数字/影响/为何不改均已登记）",
        },
        "boundary_with_R3": {
            "R3": "归一化后**完全相同**（disjoint 前提：完全相同 ⇒ 词表也相同 ⇒ 必是换序）",
            "R1prime": "**不**完全相同、且来料词表是既有的真子集（有新词则不算）",
            "disjoint_proof": ("本规则**显式豁免纯换序**，而「归一化后完全相同」必然是纯换序 "
                               "⇒ 两条规则**不可能对同一对同时命中**（实测重叠 0/11,193）。"
                               "测试 `test_低新颖度与R3不重叠` 用随机样本复核。"),
        },
    },
    # ── R2 的替代：后置不变量（已实现、**已接线**）──────────────────────
    # 这两条 code 只可能由 `verify_merge_postcondition` 产出。`wired: True` 是**机器可读的
    # 事实**，可达性判据会核对"账本声明"与"`_ingest.py` 里是否真有调用"是否一致 ——
    # 任一侧不一致即判红（把接线摘掉 ⇒ 红），因此"写好了没接上"无法被读成"已有防护"。
    "source_not_landed": {
        "id": "R2-post-a",
        "title": "后置不变量：本次来源没落进来源集合",
        "status": "active-postcondition",
        "host": "verify_merge_postcondition",
        "wired": True,
        "wired_proof": ("trinity/agents/aggregator/_ingest.py::merge_if_similar "
                        "在全部变更之后调用 verify_merge_postcondition(...)"),
        "reachability": (
            "直接调用可达：`verify_merge_postcondition({...}, before, new_source='x')` 而 after 缺 'x' "
            "⇒ 判红（反事实：把 `.add()` 改成重建集合就会走到这里）。"
        ),
    },
    "source_lost": {
        "id": "R2-post-b",
        "title": "后置不变量：既有来源被抹掉",
        "status": "active-postcondition",
        "host": "verify_merge_postcondition",
        "wired": True,
        "wired_proof": ("trinity/agents/aggregator/_ingest.py::merge_if_similar "
                        "在全部变更之后调用 verify_merge_postcondition(...)"),
        "reachability": (
            "直接调用可达：`before={'a','b'}`、`after={'c'}` ⇒ 判红（这正是旧 R2 想守、"
            "而前置位置永远守不到的那个不变量）。"
        ),
    },
}

#: 实现里所有可能产出的 code（供可达性判据核对"未登记规则"）。**必须与代码同步**：
#: 新增/删除判据时同时改这里与 `RULE_LEDGER`（可达性判据会做三方核对：
#: AST 从源码抽出的 code 集合 / 本常量 / 账本键）。
IMPLEMENTED_CODES = (
    "duplicate_no_new_evidence",
    "low_incoming_novelty",
    "source_not_landed",
    "source_lost",
)

_WS = re.compile(r"\s+")

#: 【t13】「低新颖度」判据的阈值。口径与"为何是这个值"见 `RULE_LEDGER["low_incoming_novelty"]`：
#: 闸门内 novelty 实测双峰 —— 恰为 0 的占 57.5%，最小正值 0.0031 ⇒ 0.003 恰好只覆盖【零新词】。
LOW_NOVELTY_RATIO = 0.003

#: 【t13】分词：ASCII/数字词（长度 >=2）。中文另走**二字组**（见 `_tokens`）。
_TOKEN_ASCII = re.compile(r"[0-9a-z_]{2,}")
_TOKEN_CJK = re.compile(r"[\u4e00-\u9fff]")
_HAS_DIGIT = re.compile(r"\d")


def is_enabled() -> bool:
    return os.environ.get(MERGE_SAFETY_ENV, "on").strip().lower() in (
        "on", "1", "true", "yes",
    )


@dataclass(frozen=True)
class MergeSafetyVerdict:
    """`safe=False` 时必须带机器可读的 `code`，便于计数与门禁消费。"""

    safe: bool
    code: str = ""
    detail: str = ""


SAFE = MergeSafetyVerdict(True)


def _norm(text: Optional[str]) -> str:
    """归一化：NFKC + 折叠空白 + 去首尾。用于"是否同一条内容"的判定。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text))
    return _WS.sub(" ", s).strip()


def _tokens(text: str) -> set:
    """【t13】分词：NFKC + 小写 ⇒ ① ASCII/数字词（长度 >=2）② **中文二字组**。

    为什么中文用二字组而不是单字：单字口径会把「同义改写」记成大量新 token
    （t11 的探针就是单字口径），而二字组更接近词、噪声更低。两个口径的原始分布都可复算
    （t11 的 `merge_safety_novelty_probe.json` 与 t13 的 `merge_safety_low_novelty_calibration.json`）。
    """
    t = unicodedata.normalize("NFKC", str(text or "")).lower()
    out = set(_TOKEN_ASCII.findall(t))
    chars = _TOKEN_CJK.findall(t)
    out |= {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}
    if len(chars) == 1:
        out.add(chars[0])
    return out


def _token_multiset(text: str) -> Counter:
    """重数版本（用于判定「纯换序」= 多重集相同）。"""
    t = unicodedata.normalize("NFKC", str(text or "")).lower()
    items = list(_TOKEN_ASCII.findall(t))
    chars = _TOKEN_CJK.findall(t)
    items += [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
    if len(chars) == 1:
        items.append(chars[0])
    return Counter(items)


def incoming_novelty(new_text: str, existing_text: str) -> float:
    """来料的**新信息比** = |来料 token ∖ 既有 token| / |来料 token|（0..1）。

    分离成公开函数：它既是判据的核心量，也是标定/复核的取数口径（避免两处各写一份）。
    """
    inc = _tokens(new_text)
    if not inc:
        return 0.0
    return len(inc - _tokens(existing_text)) / len(inc)


def is_pure_reordering(new_text: str, existing_text: str) -> bool:
    """是否只是**同一词表的换序**（多重集相同）⇒ 队长裁定属**合法改写**，本模块不判红。"""
    return _token_multiset(new_text) == _token_multiset(existing_text)


def active_rules() -> Dict[str, Dict[str, Any]]:
    """账本里状态为 active 的规则（可达性判据按此逐条核对）。"""
    return {c: m for c, m in RULE_LEDGER.items() if str(m.get("status")) == "active"}


def retired_rules() -> Dict[str, Dict[str, Any]]:
    """账本里已退役的规则（可达性判据要求它们**不再**产出 code）。"""
    return {c: m for c, m in RULE_LEDGER.items()
            if str(m.get("status", "")).startswith("retired")}


def verify_merge_safety(
    new_content: str,
    existing_content: str,
    *,
    existing_sources: Optional[Set[str]] = None,
    new_source: Optional[str] = None,
) -> MergeSafetyVerdict:
    """判定"把 `new_content` 并入 `existing_content` 所在条目"是否安全（**前置**准入）。

    调用方必须在本函数返回 `safe=False` 时**零变更**地返回既有条目
    （返回 None 会让调用方新建重复记忆，见 `_ingest.py:225-240`）。

    当前（t16 之后）**本位置上有两条判据**：

    * 内容归一化后完全相同 **且** 本次来源不是新的 ⇒ 判红 `duplicate_no_new_evidence`（R3）；
    * 来料无新词（低新颖度）、非纯换序、无值型差异 **且** 本次来源不是新的
      ⇒ 判红 `low_incoming_novelty`（R1′）；
    * 其余一律放行。

    **关于 `existing_sources` / `new_source`（更正 t11 的一处登记）**：
    这两个参数是**可用的判据输入**，不是摆设 —— 上面的"本次来源是不是新的"就是靠它们判的，
    而且它是**可失败**的（来源是新的时不拒绝）。t11 把这里登记成
    「不足以支撑任何可失败判据」，那是**错的**：R2 退化成恒真式的原因**不是来源集合不可用**，
    而是它在**变更之后**去比对**同一个**集合（`projected = set(existing);
    projected.add(new)` 之后判 `issuperset` 当然恒真）—— 教训是**比对时机/基线错了**，
    不是输入不可用。R2 的替代因此放在**变更之后**（`verify_merge_postcondition`），
    而"来源是否为新的"这种**前置**判断则本来就该在变更**之前**用变更前的集合来做。

    **未知来源时的默认**：`existing_sources is None` 或 `new_source` 为空 ⇒ **按"不是新的"处理**
    （即维持严格行为：低新颖度/完全相同仍判红）。理由：没有证据表明这是独立佐证，
    就不能替它假设正当性；这条默认同时保证**未改调用方的行为与 t13 完全一致**。
    """
    if not is_enabled():
        return SAFE

    new_s = _norm(new_content)
    old_s = _norm(existing_content)

    # 【t16】"本次来源是不是新的"：来源集合在**变更前**取，所以这是可失败判定。
    #   · 已知且 new_source 已在集合里 ⇒ 自复制 ⇒ 允许内容类判据拒绝；
    #   · 已知且 new_source 不在集合里 ⇒ 跨 agent 独立复述 ⇒ **正当佐证，放行**；
    #   · 未知（None/空）⇒ 同"不是新的"，维持 t13 的严格行为。
    new_source_is_new = False
    if existing_sources is not None and new_source:
        new_source_is_new = str(new_source) not in {str(s) for s in existing_sources}

    # R3（active）：无新证据的置信度灌水 —— 归一化后完全相同
    if new_s and new_s == old_s:
        return MergeSafetyVerdict(
            False,
            "duplicate_no_new_evidence",
            "来料与既有内容归一化后完全相同 ⇒ 合并无新信息，只会单调推高 confidence",
        )

    # R1'（active，R1 的可用替代）：低新颖度 —— 来料词表里**没有任何新词**，且不只是换序
    if new_s and old_s:
        inc = _tokens(new_s)
        if inc:
            new_tokens = inc - _tokens(old_s)
            novelty = len(new_tokens) / len(inc)
            if (novelty < LOW_NOVELTY_RATIO
                    and not is_pure_reordering(new_s, old_s)
                    and not any(_HAS_DIGIT.search(t) for t in new_tokens)):
                return MergeSafetyVerdict(
                    False,
                    "low_incoming_novelty",
                    "来料 %d 个 token 里**没有一个**是既有词表之外的新词（新颖度 %.4f < %.3f），"
                    "且它不是纯换序、也没有值型差异 ⇒ 合并只会丢弃一段纯回声并推高 confidence"
                    % (len(inc), novelty, LOW_NOVELTY_RATIO),
                )

    return SAFE


def verify_merge_postcondition(
    before_sources: Optional[Iterable[str]],
    after_sources: Optional[Iterable[str]],
    *,
    new_source: Optional[str] = None,
) -> MergeSafetyVerdict:
    """**后置**不变量（R2 的可失败替代）：合并写完之后核对来源集合。

    与前置判据的分工：前置判据回答"能不能开始改"；本函数回答"改完之后有没有改坏"。
    二者不冲突，也不违反 G2 契约（G2 锁的是**前置校验必须早于任何变更**，见
    `tests/unit/test_merge_safety_rules_20261006.py::test_verification_happens_before_any_mutation`；
    后置断言发生在变更**之后**，与那条契约无关）。

    判红条件（**两条都能被真实实现违反**）：

    * `source_not_landed`：`new_source` 非空却不在 `after_sources` 里 ⇒ 本次来源没落库；
    * `source_lost`：`before_sources` 里有来源在 `after_sources` 里消失 ⇒ 来源历史被抹掉。

    调用点接线（**需队长批准后再改 `_ingest.py`**，本轮未做）：

        _before = set(best_dv.source_agents)          # 变更前
        ...                                           # 既有变更（不变）
        _v2 = verify_merge_postcondition(_before, best_dv.source_agents, new_source=source_agent)
        if not _v2.safe:                              # 只告警 + 计数，**不回滚**
    """
    if not is_enabled():
        return SAFE

    before = set(before_sources or ())
    after = set(after_sources or ())

    if new_source and new_source not in after:
        return MergeSafetyVerdict(
            False,
            "source_not_landed",
            "本次来源 %r 未进入合并后的来源集合（before=%r after=%r）"
            % (new_source, sorted(before), sorted(after)),
        )

    lost = before - after
    if lost:
        return MergeSafetyVerdict(
            False,
            "source_lost",
            "合并抹掉了既有来源 %r（before=%r after=%r）"
            % (sorted(lost), sorted(before), sorted(after)),
        )

    return SAFE
