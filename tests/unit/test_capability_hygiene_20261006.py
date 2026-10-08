# -*- coding: utf-8 -*-
"""T6 能力面收口判据（2026-10-06）。

## 靶心

「能力清单」与「代码现实」四类脱钩，逐类都要有**能红**的判据：

  ① **死副本** —— 同名类有多处定义，其中若干与运行副本不一致（甚至不兼容）；
  ② **幽灵成员** —— loader / registry 引用了全仓不存在的成员；
  ③ **恒假 / 恒真** —— 名字暗示有逻辑、实体却是单条常量 return（或 `X or True`）；
  ④ **能力数字** —— 50 守卫 / 47 通道被读成能力数，而 enforcing/contributing 实为 0。

台账（**判据的单一来源**）在
`trinity/modules/second_brain/capability_ledger.py`；本文件负责**重算现实**并比对。

## 为什么每一组都配一条「负向实测」

本仓纪律：**判据必须是能失败的**。只写正向断言的话，一个"永远匹配不到东西"的
检查（正则写错、遍历空集合、把断言写成 `assert True`）会长期全绿而毫无判别力
—— 本仓已有前科（见 `scripts/fake_green_audit.py`）。所以每组都用一个**合成变异体**
证明"它真的抓得住"：

  · 复制一份类定义  ⇒ ① 必须红；
  · 把唯一一处定义删掉 ⇒ ① 也必须红（防止"删了就绿"）；
  · 新增一个未登记的同名副本 ⇒ 闭世界断言必须红；
  · 伪造一个缺成员的对象 ⇒ ② 必须红；
  · 新增一个未登记的恒假方法 ⇒ ③ 必须红；
  · 让别名指向两个不同对象 ⇒ 转口同一性判据必须红。

运行：
    python -m pytest tests/unit/test_capability_hygiene_20261006.py -q
"""
from __future__ import annotations

import ast
import io
import os
import re
import tokenize
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

from trinity.modules.second_brain import capability_ledger as L          # noqa: E402
import logging

SECOND_BRAIN = ROOT / "trinity" / "modules" / "second_brain"


# ══════════════════════════════════════════════════════════════════════════
# 纯判据（正/负向共用同一套函数 —— 负向实测才有意义）
# ══════════════════════════════════════════════════════════════════════════

def single_def_violations(sites_by_name: dict, names) -> list:
    """① 必须在能力面上「只此一份」的名字：定义点数 != 1 即违规（多与少都算）。"""
    return ["%s(定义 %d 处)" % (n, len(sites_by_name.get(n, [])))
            for n in sorted(names) if len(sites_by_name.get(n, [])) != 1]


def unregistered_duplicates(sites_by_name: dict, facade_names, shadow_names) -> list:
    """④ 门面导出的类不得有**未登记**的第二处定义；已登记者也不得只剩一处。"""
    bad = []
    for n in sorted(facade_names):
        n_sites = len(sites_by_name.get(n, []))
        if n_sites > 1 and n not in shadow_names:
            bad.append("%s: %d 处定义，未登记为影子副本" % (n, n_sites))
        if n_sites == 1 and n in shadow_names:
            bad.append("%s: 已登记为影子副本，实际只有 1 处定义" % n)
    return bad


def undeclared_noop(reality: dict, declared) -> list:
    """③ 现实里的恒假方法 - 台账登记的 = 未登记（闭世界的一侧）。"""
    return sorted(set(reality) - set(declared))


def stale_noop_declarations(reality: dict, declared) -> list:
    """③ 台账登记了、现实里已不存在 = 台账过期（闭世界的另一侧）。"""
    return sorted(set(declared) - set(reality))


def ghost_members(names, obj) -> list:
    """② 被引用却在该对象上不存在的成员。"""
    return ["%s" % n for n in sorted(names) if not hasattr(obj, n)]


def alias_mismatches(expected: dict) -> list:
    """⑥ 转口同一性：`名字 -> 对象` 必须**两两 `is` 相等**。"""
    objs = list(expected.values())
    if not objs:
        return ["没有可比对的对象"]
    first = objs[0]
    return ["%s 不是同一对象" % k for k, v in expected.items() if v is not first]


def roster_mismatches(roster: dict, live: dict) -> list:
    """⑤ 台账登记的数值必须等于活体对象实测值。"""
    return ["%s: 登记 %r != 实测 %r" % (k, roster[k]["value"], live.get(k))
            for k in sorted(roster) if roster[k]["value"] != live.get(k)]


def export_count_ok(declared, module_all) -> bool:
    """包自报导出名数 == `__all__` 长度（两者都漂移就不算"一致"）。"""
    return declared == len(module_all) and len(module_all) == len(set(module_all))


#: 视为「能力宣称」的数字（本仓反复被误读的那几个）
_CAPABILITY_NUM_RE = re.compile(r"\b(122|129|50|47|29)\b")

#: 与数字同行出现即视为**已标定口径**（或显式留痕说明它已作废）
_CALIBER_MARKERS = (
    "declared", "registered", "enforcing", "contributing",
    "原文案", "移除", "派生", "len(__all__)", "占位",
)


def uncalibrated_number_lines(doc: str, markers=_CALIBER_MARKERS) -> list:
    """能力数字不得以**裸宣称**形态出现（同行必须带口径或留痕说明）。

    ⚠️ 首版判据写的是 `assert "122 modules" not in doc` —— 被**自己的留痕**骗了：
    修正后的 docstring 里就有「原文案写「122 modules」」这句。这正是本仓
    「判据只看文本会被留痕骗」的老毛病。改为"逐行要求带口径标记"：
    裸的 `Guardian chain: 50-tier` 会被抓，带 `declared` 的不会被误伤。
    """
    bad = []
    for line in doc.splitlines():
        if not _CAPABILITY_NUM_RE.search(line):
            continue
        if not any(m in line for m in markers):
            bad.append(line.strip())
    return bad


# ══════════════════════════════════════════════════════════════════════════
# 夹具：本包源码 / 定义点 / 门面花名册
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def sources() -> dict:
    return L.load_capability_sources()


@pytest.fixture(scope="module")
def sites(sources: dict) -> dict:
    return L.class_definition_sites(sources)


@pytest.fixture(scope="module")
def facade_classes(sources: dict) -> list:
    """engine.py `__all__` 里**确实是 second_brain 内定义的类**的那些名字。"""
    tree = ast.parse(sources["trinity/modules/second_brain/engine.py"])
    names: list = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            names = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    all_sites = L.class_definition_sites(sources)
    return [n for n in names if all_sites.get(n)]


# ══════════════════════════════════════════════════════════════════════════
# ① 死副本：能力类必须恰好一处定义
# ══════════════════════════════════════════════════════════════════════════

def test_能力类定义点恰好一处(sites: dict) -> None:
    bad = single_def_violations(sites, L.SINGLE_DEFINITION_CLASSES)
    assert not bad, (
        "以下能力类的定义点数不是 1（多 = 死副本/幽灵实现，少 = 被误删）：\n  "
        + "\n  ".join(bad)
        + "\n⇒ 收敛为一处（其余显式转口 alias），或把**决定不收敛**的那一处登记进 "
          "capability_ledger.DECLARED_SHADOW_COPIES 并写明理由与风险")
    # 反向：登记表里没有一条是空话
    for name, why in L.SINGLE_DEFINITION_CLASSES.items():
        assert why and len(why) > 10, "%s 缺收敛理由" % name


def test_负向实测_重复定义必须被抓(sources: dict) -> None:
    """变异体：把 engine_guardian_retrieval.py 里的类定义复制一份。"""
    rel = "trinity/modules/second_brain/engine_guardian_retrieval.py"
    mutant = dict(sources)
    mutant["trinity/modules/second_brain/__mutant_copy__.py"] = (
        "class GuardianChainV50:\n    pass\n")
    before = single_def_violations(L.class_definition_sites(sources),
                                   ["GuardianChainV50"])
    after = single_def_violations(L.class_definition_sites(mutant), ["GuardianChainV50"])
    assert before == [], "基线：%s 应当恰好一处定义" % rel
    assert after, "复制一份同名类定义后判据仍然全绿 ⇒ 这条判据没有牙齿"


def test_负向实测_定义消失也必须被抓(sources: dict) -> None:
    """反向变异：删掉唯一定义点也要红（否则"删掉文件"会成为一条假绿捷径）。"""
    mutant = {k: v for k, v in sources.items()
              if k != "trinity/modules/second_brain/engine_guardian_retrieval.py"}
    after = single_def_violations(L.class_definition_sites(mutant), ["GuardianChainV50"])
    assert after, "删掉唯一定义点后判据仍绿 ⇒ 只数'是否超过一处'是不够的"


# ══════════════════════════════════════════════════════════════════════════
# ④ 闭世界：不存在「默默多留一份」的同名副本
# ══════════════════════════════════════════════════════════════════════════

def test_门面导出的类不得有未登记的副本(sites: dict, facade_classes: list) -> None:
    bad = unregistered_duplicates(sites, facade_classes, set(L.DECLARED_SHADOW_COPIES))
    assert not bad, (
        "引擎门面导出的类在同一包内出现了未登记的第二处定义：\n  " + "\n  ".join(bad))


def test_同名副本清单是闭世界(sources: dict) -> None:
    """second_brain 里**每一个**同名定义都必须有处置（收敛 / 登记 / 清点）。"""
    reality = set(L.duplicate_class_names(sources))
    declared = set(L.DECLARED_SHADOW_COPIES) | set(L.DOCUMENTED_DUPLICATES)
    assert reality - declared == set(), (
        "以下同名类没有任何处置登记（不得默默留着）：%s" % sorted(reality - declared))
    assert declared - reality == set(), (
        "台账登记了、现实里已不存在的同名类（台账过期，请更新）：%s"
        % sorted(declared - reality))
    # 每一类都必须带 disposition / 风险说明
    for name, rec in L.DOCUMENTED_DUPLICATES.items():
        assert rec.get("disposition") and rec.get("risk"), "%s 缺处置或风险说明" % name
    for name, rec in L.DECLARED_SHADOW_COPIES.items():
        assert rec.get("reason") and rec.get("risk"), "%s 缺不收敛理由或风险" % name


def test_负向实测_新的未登记副本必须被抓(sources: dict) -> None:
    """变异体：给一个**当前只有一处定义**的类补一份副本。

    （首跑用 `RiskLevel` 是错的 —— 它本来就是已登记的同类名，再补一处
      只会把 2 处变 3 处，闭世界断言照样绿。这正是"负向实测"要暴露的东西：
      **变异必须真的制造出新的违规**。）
    """
    mutant = dict(sources)
    mutant["trinity/modules/second_brain/__mutant_copy__.py"] = \
        "class ConfidenceScorer:\n    pass\n"
    reality = set(L.duplicate_class_names(mutant))
    declared = set(L.DECLARED_SHADOW_COPIES) | set(L.DOCUMENTED_DUPLICATES)
    assert "ConfidenceScorer" not in set(L.duplicate_class_names(sources)), \
        "ConfidenceScorer 在基线上就不是唯一 ⇒ 本变异无效，请换一个名字"
    assert reality - declared, "新增未登记同名副本后闭世界断言仍绿 ⇒ 判据没牙齿"


# ══════════════════════════════════════════════════════════════════════════
# ③ 恒假 / 恒真：declared-noop 与恒真自述
# ══════════════════════════════════════════════════════════════════════════

def test_恒假方法必须登记为declared_noop(sources: dict) -> None:
    reality = L.constant_return_methods(sources)
    extra = undeclared_noop(reality, L.DECLARED_NOOP_METHODS)
    stale = stale_noop_declarations(reality, L.DECLARED_NOOP_METHODS)
    assert not extra, (
        "以下方法名字暗示有逻辑、实体却只是单条常量 return，且**未登记**：\n  "
        + "\n  ".join("%s -> %s" % (k, reality[k]) for k in extra)
        + "\n⇒ 要么实现它，要么登记进 capability_ledger.DECLARED_NOOP_METHODS "
          "（写明为什么不能实现 / 不能改名为 noop_*）")
    assert not stale, "台账登记了但现实里已不存在（台账过期）：%s" % stale
    # 每条登记必须给出实体常量与两条理由
    for key, rec in L.DECLARED_NOOP_METHODS.items():
        assert rec.get("const") == reality.get(key), (
            "%s: 登记的返回常量 %r != 实测 %r" % (key, rec.get("const"), reality.get(key)))
        assert rec.get("why") and rec.get("why_not_renamed_to_noop"), \
            "%s: 缺 why / why_not_renamed_to_noop" % key


def test_负向实测_新的恒假方法必须被抓(sources: dict) -> None:
    mutant = dict(sources)
    mutant["trinity/modules/second_brain/__mutant_copy__.py"] = (
        "class FakeCapability:\n"
        "    def verify_everything(self):\n"
        "        return []\n")
    reality = L.constant_return_methods(mutant)
    extra = undeclared_noop(reality, L.DECLARED_NOOP_METHODS)
    assert extra, "新增未登记的恒假方法后判据仍绿 ⇒ 闭世界断言没牙齿"
    assert not undeclared_noop(L.constant_return_methods(sources),
                               L.DECLARED_NOOP_METHODS)


def test_恒真能力自述必须登记(sources: dict) -> None:
    """`X or True` 恒为 True —— 读数失去判别力，必须登记（闭世界）。"""
    reality = L.true_claim_counts(sources)
    declared = {k: v["count"] for k, v in L.DECLARED_TRUE_CLAIMS.items()}
    assert reality == declared, (
        "`X or True` 的分布与台账不符：\n  实测 %r\n  登记 %r\n"
        "⇒ 新增处要么改真判据，要么登记进 capability_ledger.DECLARED_TRUE_CLAIMS"
        % (reality, declared))
    # 恒真必须**只**出现在已登记影子副本里（不得扩散到运行副本）
    allowed = {rec["shadow"] for rec in L.DECLARED_SHADOW_COPIES.values()}
    stray = sorted(set(reality) - allowed)
    assert not stray, (
        "以下文件不是已登记影子副本，却在用 `X or True` 自述能力：%s" % stray)


def test_declared_noop不得同时出现在运行副本里(sources: dict) -> None:
    """登记为 declared-noop 的**唯一**可用于运行时的那处必须显式带 CONTRIBUTES=False。

    这一条防的是"把恒假方法登记一下就算收口了"：登记只描述现状，
    `CONTRIBUTES=False` 才是让**下游**（degradation / aggregator）据此不把它算作
    在用通道的那件事。
    """
    from trinity.modules.second_brain.engine_guardian_retrieval import RetrievalSystemV47

    assert RetrievalSystemV47.CONTRIBUTES is False
    assert RetrievalSystemV47.DATA_SOURCE is None
    assert RetrievalSystemV47().contributing_count() == 0
    assert RetrievalSystemV47().search("任意查询") == []


# ══════════════════════════════════════════════════════════════════════════
# ② 幽灵成员：loader / registry 引用的成员必须真实存在
# ══════════════════════════════════════════════════════════════════════════

LOADER = ROOT / "trinity" / "modules" / "second_brain" / "loader.py"


def _loader_source() -> str:
    return LOADER.read_text(encoding="utf-8")


def test_loader登记的能力类在目标模块里都存在() -> None:
    """`r.register(N, path, cls, ...)` 的 cls 必须真能在 path 里解析到。"""
    import importlib

    src = _loader_source()
    triples = re.findall(
        r'\(\s*"[A-Za-z0-9_]+"\s*,\s*"(?P<mod>[A-Za-z0-9_.]+)"\s*,\s*'
        r'"(?P<cls>[A-Za-z0-9_]+)"\s*,', src)
    assert len(triples) == 12, "loader 登记条目数变了，请复核本判据：%d" % len(triples)
    missing = []
    for mod_path, cls_name in triples:
        mod = importlib.import_module(mod_path)
        if not hasattr(mod, cls_name):
            missing.append("%s.%s" % (mod_path, cls_name))
    assert not missing, "loader 登记了不存在的类（幽灵成员）：%s" % missing


def test_loader诊断入口不得调用不存在的成员() -> None:
    """`self._guardian.<x>` / `self._retrieval.<x>` 必须真实存在。

    T6 实测背景：loader 曾从 `guardian.py` / `guardian_retrieval.py` 两个**影子副本**
    导入，这两份缺 `enforcing_count()` / `contributing_count()` / `total`，
    于是 `SecondBrainLoader.diagnostics()` **100% 抛 AttributeError**
    （`'GuardianChainV50' object has no attribute 'enforcing_count'`）。
    """
    from trinity.modules.second_brain.loader import SecondBrainLoader

    src = _loader_source()
    refs = {
        "_guardian": sorted(set(re.findall(r"self\._guardian\.(\w+)", src))),
        "_retrieval": sorted(set(re.findall(r"self\._retrieval\.(\w+)", src))),
    }
    assert refs["_guardian"], "loader 不再引用 self._guardian.* —— 判据已失效，请复核"
    assert refs["_retrieval"], "loader 不再引用 self._retrieval.* —— 判据已失效，请复核"

    sb = SecondBrainLoader(lazy=True)
    bad = ghost_members(refs["_guardian"], sb.guardian_chain) + \
        ghost_members(refs["_retrieval"], sb.retrieval)
    assert not bad, "loader 调用了不存在的成员（幽灵成员）：%s" % bad

    # 真正的活体路径：入口本身必须可跑（历史上这里必抛 AttributeError）
    d = sb.diagnostics()
    assert d["guardian_levels_declared"] == 50
    assert d["retrieval_channels_registered"] == 47
    assert d["guardian_levels"] == d["guardian_levels_enforcing"] == 0
    assert d["retrieval_channels"] == d["retrieval_channels_contributing"] == 0


def test_负向实测_幽灵成员必须被抓() -> None:
    class _NoEnforcing:
        total = 50

    bad = ghost_members(["enforcing_count", "total"], _NoEnforcing())
    assert bad == ["enforcing_count"], "缺成员没有被抓出来 ⇒ 判据没牙齿"
    assert ghost_members(["total"], _NoEnforcing()) == []


# ══════════════════════════════════════════════════════════════════════════
# ⑥ 转口（alias）不得改变运行时行为
# ══════════════════════════════════════════════════════════════════════════

def test_转口是同一对象_而非同名不同体() -> None:
    import inspect
    from trinity.modules.second_brain import engine as eng
    from trinity.modules.second_brain import guardian, guardian_retrieval
    from trinity.modules.second_brain import engine_guardian_retrieval as egr

    for name, expected in (
        ("GuardianChainV50", {
            "guardian": guardian.GuardianChainV50,
            "guardian_retrieval": guardian_retrieval.GuardianChainV50,
            "engine_guardian_retrieval": egr.GuardianChainV50,
            "engine": eng.GuardianChainV50,
        }),
        ("RetrievalSystemV47", {
            "guardian_retrieval": guardian_retrieval.RetrievalSystemV47,
            "engine_guardian_retrieval": egr.RetrievalSystemV47,
            "engine": eng.RetrievalSystemV47,
        }),
    ):
        assert alias_mismatches(expected) == [], (
            "%s 的多个导入点不是同一对象 ⇒ 又出现「同名不同体」" % name)
        # 唯一真身的位置
        assert inspect.getsourcefile(expected["engine_guardian_retrieval"]).endswith(
            "engine_guardian_retrieval.py"), "%s 的真身不在唯一实现文件里" % name


def test_负向实测_两个不同类必须被判为不同() -> None:
    class A:
        pass

    class B:
        pass

    bad = alias_mismatches({"a": A, "b": B})
    assert bad == ["b 不是同一对象"], "不同对象没有被判出来 ⇒ 同一性判据没牙齿"
    assert alias_mismatches({"a": A, "a2": A}) == []


def test_p1_preamble的16个类必须同一对象() -> None:
    from trinity.modules.second_brain import engine_core_types as core
    from trinity.modules.second_brain import p1_preamble as pre

    names = [n for n, why in L.SINGLE_DEFINITION_CLASSES.items()
             if "p1_preamble" in why]
    assert len(names) == 16, "p1_preamble 的转口类数量变了：%d" % len(names)
    for n in names:
        assert getattr(pre, n) is getattr(core, n), "%s 不是同一个对象" % n


def test_运行时行为未变_活的构造与调用路径() -> None:
    """MRO / 构造 / 一条真实调用路径 —— 三项都要，才叫"行为未变"。"""
    from trinity.modules.second_brain.engine_guardian_retrieval import (
        GuardianChainV50, RetrievalSystemV47)

    gc = GuardianChainV50()                                   # 活体构造
    assert GuardianChainV50.__mro__ == (GuardianChainV50, object)
    assert len(gc.shields) == 50 and gc.total == 50
    assert gc.validate() is True                              # 既有契约
    assert gc.enforcing_count() == 0

    rs = RetrievalSystemV47()                                 # 活体构造
    assert RetrievalSystemV47.__mro__ == (RetrievalSystemV47, object)
    assert len(rs.channels) == 47 and rs.total == 47
    assert rs.validate() is True
    assert rs.search("活的调用路径") == []
    assert rs.contributes() is False


def test_AuditableRecallReceipt唯一且台账真的能用() -> None:
    """同文件内曾两次定义同名类：生成器的 generate() 必然 TypeError。

    现在两名字各自唯一，`generate()` 必须**真的能跑通**（这是能力，不是占位）。
    """
    from trinity.modules.second_brain.confidence_scored_retrieval import (
        AuditableReceiptJournal, AuditableRecallReceipt, MemoryFact)

    assert hasattr(AuditableRecallReceipt, "to_dict"), "记录类被生成器遮蔽了"
    journal = AuditableReceiptJournal()
    receipt = journal.generate("T6 判据", [MemoryFact(fact_id="f1", content="x")],
                               latency_ms=1.0)
    assert isinstance(receipt, AuditableRecallReceipt)
    assert journal.get_receipt(receipt.receipt_id) is receipt
    assert journal.export_receipts()[0]["receipt_id"] == receipt.receipt_id
    assert journal.statistics()["total_receipts"] == 1


# ══════════════════════════════════════════════════════════════════════════
# ⑤ 能力数字：50 / 47 的口径
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def engine():
    """惰性构造 SecondBrainV636（重型，仅一次）。

    这里**不**调 `run_diagnostics()`（那会写 sidecar 文件）—— 本判据只需要
    `total_modules` 这一个由注册表长度派生的读数。
    """
    from trinity.modules.second_brain.engine_core import SecondBrainV636

    return SecondBrainV636()


def test_名册数字与代码现实一致(engine) -> None:
    import trinity.modules.second_brain as sb_pkg

    from trinity.modules.second_brain.engine_guardian_retrieval import (
        GuardianChainV50, RetrievalSystemV47)

    gc, rs = GuardianChainV50(), RetrievalSystemV47()
    live = {
        "guardian_declared": gc.total,
        "guardian_enforcing": gc.enforcing_count(),
        "channels_registered": rs.total,
        "channels_contributing": rs.contributing_count(),
        "second_brain_modules": engine.total_modules,
        "second_brain_exports": sb_pkg.EXPORTED_COUNT,
    }
    assert roster_mismatches(L.ROSTER, live) == []
    assert live == {"guardian_declared": 50, "guardian_enforcing": 0,
                    "channels_registered": 47, "channels_contributing": 0,
                    "second_brain_modules": 22, "second_brain_exports": 91}, live


def test_包自报导出名数是派生值而非手抄字面量() -> None:
    """`__init__.py` 曾写「Exported modules: 29」，现实是 91 —— 无任何判据盯着它。"""
    import trinity.modules.second_brain as sb_pkg

    assert export_count_ok(sb_pkg.EXPORTED_COUNT, sb_pkg.__all__)
    doc = sb_pkg.__doc__ or ""
    bad = uncalibrated_number_lines(doc)
    assert not bad, (
        "包 docstring 里以下行**裸宣称**能力数字（未带 declared/enforcing/contributing "
        "等口径标记，也不是留痕说明）：\n  " + "\n  ".join(bad))
    assert "50 declared / 0 enforcing" in doc, "守护链数字必须带口径"
    assert "47 registered / 0 contributing" in doc, "通道数字必须带口径"


#: T6 改动前的包 docstring 原文（用于负向实测 —— 它**必须**被判红）
_LEGACY_INIT_DOC = """\
Second Brain engine — 122 modules for memory encoding, retrieval, reasoning, and self-evolution.

Papers: P1-P129 aligned
Guardian chain: 50-tier
Retrieval channels: 47-way
Exported modules: 29 (6 originals + 23 newly activated from engine facade)
Version: sourced from trinity.version (single source of truth)
"""


def test_负向实测_过期能力数字必须被抓() -> None:
    """把这套判据拿回**改动前**的 docstring 上跑 —— 必须报红。"""
    bad = uncalibrated_number_lines(_LEGACY_INIT_DOC)
    assert len(bad) == 4, "改前的 4 行裸宣称没有被全部抓到：%r" % bad
    assert any("122 modules" in b for b in bad)
    assert any("50-tier" in b for b in bad)
    assert any("47-way" in b for b in bad)
    assert any("Exported modules: 29" in b for b in bad)
    # 反向：带口径的写法不得被误伤
    assert uncalibrated_number_lines(
        "Guardian chain : **50 declared / 0 enforcing**") == []


def test_负向实测_导出数漂移必须被抓() -> None:
    import trinity.modules.second_brain as sb_pkg

    assert export_count_ok(sb_pkg.EXPORTED_COUNT, sb_pkg.__all__) is True
    # 变异 1：常量漂移（有人往 __all__ 加名字却没管常量）
    assert export_count_ok(sb_pkg.EXPORTED_COUNT + 1, sb_pkg.__all__) is False
    # 变异 2：__all__ 漂移
    assert export_count_ok(sb_pkg.EXPORTED_COUNT, list(sb_pkg.__all__) + ["__mutant__"]) is False
    # 变异 3：__all__ 里出现重复名（数量对不上"名数"）
    assert export_count_ok(3, ["a", "a", "b", "b"]) is False


def test_负向实测_数字对不上必须被抓() -> None:
    roster = {"x": {"value": 47}}
    assert roster_mismatches(roster, {"x": 47}) == []
    assert roster_mismatches(roster, {"x": 50}) == ["x: 登记 47 != 实测 50"]
    assert roster_mismatches(roster, {}) == ["x: 登记 47 != 实测 None"]


def test_roster必须同时给出声明口径与实现口径() -> None:
    """50 / 47 不得只以「能力」形态出现 —— 每个数字都要配实现口径。"""
    for key, rec in L.ROSTER.items():
        assert rec.get("meaning") and rec.get("live"), "%s 缺口径说明/来源" % key
    assert {"guardian_declared", "guardian_enforcing",
            "channels_registered", "channels_contributing"} <= set(L.ROSTER)
    assert "名册" in L.ROSTER_CAVEAT


# ══════════════════════════════════════════════════════════════════════════
# ⑦ 名字说有、实质没有：declared-lexical（向量通道）/ 配额为 0（冷槽位）
# ══════════════════════════════════════════════════════════════════════════

def _resolve(ref_file: str) -> Path:
    p = Path(ref_file)
    return p if p.is_absolute() else ROOT / ref_file


def _code_lines(path: Path) -> dict:
    """`{文件行号: 该行**代码**文本}` —— 已剔除注释与 docstring。

    ## 为什么必须有这个函数（t10→t14 自查，队长点出）

    本函数出现之前的硬判据是 `contains in line`（**裸子串匹配整份文本**）。
    那意味着：只要期望内容出现在**注释**或 **docstring** 里，判据就通过 ——
    而被它守护的真实符号/引用可能早已消失。这与本仓"凡按文本判定的东西都会被
    文本骗"是同一形态，也正是本判据第一版被**自己的更正留痕**骗掉的翻版。

    机制（不是子串，而是**两层剥离后的代码行匹配**）：
      ① `tokenize` 拿到每个 `COMMENT` token 的列号 ⇒ 截断每行注释；
      ② `ast` 找出所有模块/类/函数的 docstring 常量节点，按其
         `lineno..end_lineno` 把整段从候选里删掉；
      ③ 剩下的才是"代码行"。只有它们的**去注释文本**才参与 `contains` 匹配。
    """
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()

    cut = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                cut.setdefault(tok.start[0], tok.start[1])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）tests/unit/test_capability_hygiene_20261006.py::_code_lines")

    code = {}
    for i, ln in enumerate(lines, 1):
        c = cut.get(i)
        text = ln[:c] if c is not None else ln
        if text.strip():
            code[i] = text

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return code
    drop = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            d = body[0]
            for i in range(d.lineno, getattr(d, "end_lineno", d.lineno) + 1):
                drop.add(i)
    for i in drop:
        code.pop(i, None)
    return code


#: 【t65】文本锚的**最小实质长度**。旧结构允许任意短串当 `contains`
#: （例如 `"return False"` 这种极常见行），若照搬成锚就等于**降门槛**。
#: 但既有登记里确实有短锚（`"return False"` 13 字符已达标），故取 8：
#: 要求 ≥8 且**至少含一个标识符字符**（字母/下划线/数字）——纯标点或纯空白一律红。
MIN_ANCHOR_LEN = 8


def _anchor_text(r: dict) -> str:
    """登记条目的**权威文本**：新结构用 `anchor`，旧结构回退到 `contains`。"""
    return str(r.get("anchor") or r.get("contains") or "")


def _anchor_bad_reason(r: dict) -> str:
    """锚本身是否**够格**（与"在不在文件里"是两件事）。

    ⚠️ 这一条是 t65 **加硬**的，不是替代：原来只要求"内容在文件里"，
    现在额外要求"锚有实质内容" ⇒ 防止有人把锚退化成 `"x"`、`" "`、`")"` 之类
    来把判据变绿（那正是"降低门槛"）。
    """
    a = _anchor_text(r)
    if not a:
        return "缺 anchor/contains"
    if len(a.strip()) < MIN_ANCHOR_LEN:
        return "锚过短（%d < %d）：%r" % (len(a.strip()), MIN_ANCHOR_LEN, a)
    if not re.search(r"[A-Za-z0-9_]", a):
        return "锚里没有任何标识符字符（纯标点/空白）：%r" % a
    return ""


def _bad_refs(entries: dict, label: str) -> list:
    """登记引用的 `(file, anchor[, line_hint])` 必须真的站得住。

    ## 2026-10-06（t65）结构升级：**行号 → 文本锚**

    旧结构 `{file, line, contains}` 把 `line` 当权威 ⇒ 同文件里别人插行就把登记推走，
    产生"应更新登记"的告警（实测 `engine_worker.py` 1007→1045 / 1010→1048 / 1005→[1045,1054,1056]）。
    现在：**`anchor` 是唯一的权威**，`line` / `line_hint` 只作人类提示。

    ## 硬度分级（**门槛只升不降**）

      · **硬 A**：`anchor` 必须出现在该文件的**代码行**里 —— 注释与 docstring 已被
        `_code_lines()` 剔除，所以「只写在注释/docstring 里」**不通过**（与旧 `contains` 同强度）；
      · **硬 B（t65 新增）**：`anchor` 必须有**实质**（≥ `MIN_ANCHOR_LEN` 且含标识符字符）
        —— 防止把锚退化成 `"x"` 之类来把判据弄绿；
      · **硬 C**：若给了 `line_hint`，它必须在文件行数范围内（越界=登记写错了，判红）；
      · **软**：`line_hint` 与锚的实际所在行不一致 ⇒ 只发**提示**，且措辞明确
        "**锚是权威、不必为漂移改登记**"（旧版措辞是"应更新登记" ⇒ 那是维护负担的来源）。
    """
    bad = []
    hints = []
    for key, rec in entries.items():
        refs = rec.get("file_line") or rec.get("evidence_file_line") or []
        assert refs, "%s/%s 没有任何 file:line 证据" % (label, key)
        for r in refs:
            path = _resolve(r["file"])
            if not path.is_file():
                bad.append("%s/%s: 文件不存在 %s" % (label, key, r["file"]))
                continue
            why = _anchor_bad_reason(r)
            if why:
                bad.append("%s/%s: %s 的登记锚不合规 —— %s" % (label, key, r["file"], why))
                continue
            anchor = _anchor_text(r)
            all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            code = _code_lines(path)
            hits = [i for i, text in sorted(code.items()) if anchor in text]
            if not hits:
                bad.append("%s/%s: %s 的**代码行**里找不到锚 %r（登记指向不存在的内容；"
                           "只出现在注释/docstring 里也不算数）"
                           % (label, key, r["file"], anchor))
                continue
            hint = r.get("line_hint", r.get("line"))
            if hint is None:
                continue
            if not 1 <= int(hint) <= len(all_lines):
                bad.append("%s/%s: 提示行号 %s 越界（%s 共 %d 行）"
                           % (label, key, hint, r["file"], len(all_lines)))
                continue
            if int(hint) not in hits:
                hints.append("%s/%s: %s 提示行 %s 已不等于锚所在 %s"
                             % (label, key, r["file"], hint, hits))
    if hints:
        warnings.warn("capability_ledger 行号**提示**与当前不符（锚是权威，无需为漂移改登记；"
                      "如不想看这条，删掉该条目的 line_hint 即可）：\n  " + "\n  ".join(hints),
                      stacklevel=2)
    return bad


def test_declared_lexical登记的file_line必须真的写着那句话() -> None:
    """登记的行号不是摆设：必须指向**真的含有该内容**的那一行。"""
    assert _bad_refs(L.DECLARED_LEXICAL_CHANNELS, "DECLARED_LEXICAL_CHANNELS") == []
    for key, rec in L.DECLARED_LEXICAL_CHANNELS.items():
        assert rec.get("disposition") and rec.get("why") and rec.get("owner"), \
            "%s 缺处置 / 理由 / 归属" % key
        assert "declared-lexical" in rec["disposition"]


def test_向量通道默认实现确实是词法且披露字段可派生(monkeypatch) -> None:
    """复核 t3 的发现：默认配置下 `vector` 这个键**不由向量检索服务**。"""
    monkeypatch.delenv("TRINITY_VECTOR_CHANNEL", raising=False)
    from trinity.core.client._hybrid_index import (
        _use_embedding_channel, vector_channel_impl)

    class SQLiteAdapter:
        pass

    ad = SQLiteAdapter()
    assert _use_embedding_channel(ad, use_ann=False) is False
    assert _use_embedding_channel(ad, use_ann=True) is False, (
        "默认 lexical 是**短路**分支：连 use_ann=True 都不启用嵌入 —— 这正是"
        "「名字叫 vector、实质是词法」的机制")
    assert vector_channel_impl(ad, False) == "lexical:SQLiteAdapter.search_memories"
    # 反事实：改开关后确实会走嵌入 ⇒ 说明"可切换"这条能力是真的存在
    monkeypatch.setenv("TRINITY_VECTOR_CHANNEL", "pgvector")
    assert _use_embedding_channel(ad, use_ann=False) is True


def test_冷槽位登记的file_line必须真的写着那句话() -> None:
    assert _bad_refs(L.DECLARED_ZERO_QUOTA, "DECLARED_ZERO_QUOTA") == []
    for key, rec in L.DECLARED_ZERO_QUOTA.items():
        assert rec.get("disposition"), "%s 缺处置" % key
        assert rec.get("risk"), "%s 缺风险说明" % key
        assert rec.get("owner"), "%s 缺归属（谁有写权）" % key


def test_冷槽位必须标declared_off而不是沉默的noop() -> None:
    """配额为 0 有两种：**有实测依据的决定** vs **没依据的漏洞**。前者必须留证据。"""
    rec = L.DECLARED_ZERO_QUOTA["TRINITY_ATLAS_COLD_SLOTS"]
    assert rec["production_quota"] == 0
    assert "declared-off-by-measurement" in rec["disposition"]
    assert rec.get("evidence_of_intent"), "默认关闭必须有实测依据，否则它就是待修缺陷"
    assert "不得" in rec["correct_ledger_wording"], "必须写明『能力清单该怎么写』的禁忌措辞"
    assert rec.get("v2_lever"), "必须写明重新打开的杠杆"


def test_负向实测_登记行号写错必须被抓() -> None:
    """① `contains` 指向文件里不存在的字符串 ⇒ 判红；② 行号越界 ⇒ 判红。"""
    mutant = {"X": {"file_line": [
        {"file": "trinity/engine_worker.py", "line": 1005,
         "contains": "_THIS_STRING_IS_NOT_ON_THIS_LINE_"},
    ]}}
    assert _bad_refs(mutant, "mutant"), "内容不存在没有被抓 ⇒ 判据没牙齿"

    out_of_range = {"X": {"file_line": [
        {"file": "trinity/engine_worker.py", "line": 10 ** 6,
         "contains": "_COLD_SLOTS_DEFAULT"},
    ]}}
    assert _bad_refs(out_of_range, "oor"), "行号越界没有被抓 ⇒ 判据没牙齿"

    ok = {"X": {"file_line": [
        {"file": "trinity/engine_worker.py", "line": 1005,
         "contains": "_COLD_SLOTS_DEFAULT"},
    ]}}
    assert _bad_refs(ok, "ok") == []


def test_负向实测_内容只写在注释或docstring里必须仍判红(tmp_path) -> None:
    """队长点出的自查项：**硬判据不得被注释/docstring 骗**。

    构造两个合成文件，把"期望内容"**只**放进注释 / **只**放进 docstring，
    而真实代码里没有 ⇒ 硬判据必须判红。

    本用例在 `_code_lines()` 引入**之前**会**通过**（裸子串匹配整份文本）
    —— 也就是说它正是为那个漏洞准备的回归哨兵。
    """
    only_comment = tmp_path / "only_comment.py"
    only_comment.write_text(
        "# _COLD_SLOTS_DEFAULT = \"0\"\n"
        "VALUE = 1\n", encoding="utf-8")
    bad = _bad_refs({"X": {"file_line": [
        {"file": str(only_comment), "line": 1,
         "contains": '_COLD_SLOTS_DEFAULT = "0"'}]}}, "only_comment")
    assert bad, "只有注释里有该内容 ⇒ 必须判红（否则硬判据会被注释骗）"

    only_doc = tmp_path / "only_docstring.py"
    only_doc.write_text(
        '"""module docstring\n'
        'def _cold_slots_setting():\n'
        '"""\n'
        "VALUE = 1\n", encoding="utf-8")
    bad2 = _bad_refs({"X": {"file_line": [
        {"file": str(only_doc), "line": 1,
         "contains": "def _cold_slots_setting()"}]}}, "only_docstring")
    assert bad2, "只有 docstring 里有该内容 ⇒ 必须判红"

    # 反向：同样的内容放在**真代码**里 ⇒ 必须通过（不得过度拦截）
    real = tmp_path / "real_code.py"
    real.write_text(
        "# 注释里也有 _COLD_SLOTS_DEFAULT 字样，但下面才是真的\n"
        '_COLD_SLOTS_DEFAULT = "0"\n', encoding="utf-8")
    ok = _bad_refs({"X": {"file_line": [
        {"file": str(real), "line": 2,
         "contains": '_COLD_SLOTS_DEFAULT = "0"'}]}}, "real_code")
    assert ok == [], "真代码里有该内容却被判红 ⇒ 过度拦截：%r" % ok


def test_行号提示不符只报不判红_但必须报出来() -> None:
    """若某条**主动**给了 `line_hint` 而它与锚所在不符：**不**判红，但必须可见。

    （t65 起措辞变了：提示行号**不是权威**，所以告警里明确写"无需为漂移改登记"。
    这条同时钉住"提示 ≠ 硬判据"：若有人把提示不符改回硬失败，本用例会红。）
    """
    drifted = {"X": {"file_line": [
        # 锚确实在文件里，但提示行号故意写成 1
        {"file": "trinity/engine_worker.py", "line_hint": 1,
         "anchor": "_COLD_SLOTS_DEFAULT"},
    ]}}
    with pytest.warns(UserWarning, match="提示"):
        bad = _bad_refs(drifted, "drifted")
    assert bad == [], "提示行号不符不应判红（否则并发改仓会让门禁恒红）"


# ══════════════════════════════════════════════════════════════════════════
# ⑫ 【t65】登记从「行号」升级为「**文本锚**」
#
# 起因：第 4 次全量里 `capability_ledger` 又报了行号漂移 ——
# `engine_worker.py` 1007→[1045]、1010→[1048]、1005→[1045,1054,1056]
# （本轮 t9/t12/t23/t51 各在该文件插过行）。**登记里的"哪一行"已不准**，
# 而这条判据被明确设计成"不判红" ⇒ 它只会持续刷存在感、不会保护任何东西。
# ⇒ 改造：`line` **删除**，只认 `anchor`（在**代码行**里找得到的那段文本）。
# 文本锚不会因别人插行而漂移 ⇒ 登记**不再需要维护**。
#
# ⚠️ **门槛只升不降**：旧硬判据是"内容必须在文件里"（现在等价于"锚必须在文件里"），
# 另外**新增**"锚必须有实质"（≥8 字符且含标识符字符）——防退化。
# ══════════════════════════════════════════════════════════════════════════

def _temp_shifted_copy(tmp_path, src_rel: str, prefix_lines: int = 40):
    """把真文件**前面插 N 行**（模拟"别人在同一个文件里插行"），返回临时副本路径。"""
    real = _resolve(src_rel)
    lines = real.read_text(encoding="utf-8", errors="replace").splitlines()
    shifted = ["# 模拟插入的 %d 行" % i for i in range(prefix_lines)] + lines
    out = tmp_path / ("shifted_" + os.path.basename(src_rel))
    out.write_text("\n".join(shifted) + "\n", encoding="utf-8")
    return out


def test_文本锚在别人插行后仍然成立_且不产生任何告警(tmp_path) -> None:
    """**t65 的核心性质**：登记用文本锚 ⇒ 文件被插 40 行后**依然成立、且不刷告警**。

    这正是旧结构做不到的：旧结构里 `line` 会立刻漂移并触发"应更新登记"的告警。
    """
    shifted = _temp_shifted_copy(tmp_path, "trinity/engine_worker.py")
    reg = {"X": {"file_line": [
        {"file": str(shifted), "anchor": '_COLD_SLOTS_DEFAULT = "0"'},
    ]}}
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        bad = _bad_refs(reg, "shifted")
    assert bad == [], "文本锚在被插行后仍应成立：%r" % bad
    assert w == [], "文本锚不应产生任何告警（登记无需维护）：%s" % [str(x.message)[:80] for x in w]


def test_负向实测_锚被移走或改写必须判红() -> None:
    """移走/改写那段文本 ⇒ 红（这就是"锚在不在"必须可失败）。"""
    gone = {"X": {"file_line": [
        {"file": "trinity/engine_worker.py", "anchor": "_COLD_SLOTS_DEFAULT_XYZ_NOT_THERE"},
    ]}}
    assert _bad_refs(gone, "gone") != [], "锚不存在却没判红 ⇒ 判据失效"

    rewritten = {"X": {"file_line": [
        {"file": "trinity/engine_worker.py", "anchor": 'def _cold_slots_setting(extra)'},
    ]}}
    assert _bad_refs(rewritten, "rewritten") != [], "锚被改写却没判红"


def test_负向实测_锚退化成无实质内容必须判红() -> None:
    """**门槛不降**：把锚换成 `"x"` / 纯空白 / 纯标点 ⇒ 必须红。

    旧结构下这些都是"合法 contains"（只要有那一行就通过）；新结构要求锚有实质
    ⇒ 不能靠把锚写虚来让判据变绿。
    """
    for fake in ("x", "   ", ")(", "..."):
        reg = {"X": {"file_line": [
            {"file": "trinity/engine_worker.py", "anchor": fake},
        ]}}
        assert _bad_refs(reg, "degenerate") != [], "退化锚 %r 未被判红" % fake
    assert _bad_refs({"X": {"file_line": [{"file": "trinity/engine_worker.py"}]}},
                     "no_anchor") != [], "完全没有锚却没判红"


def test_门槛未降_两张真登记表仍逐条成立且条目数不减() -> None:
    """旧口径要求点名的条目**仍须逐条点名**，且**条数不得减**。"""
    zq = L.DECLARED_ZERO_QUOTA["TRINITY_ATLAS_COLD_SLOTS"]["file_line"]
    lx = L.DECLARED_LEXICAL_CHANNELS["breakdown.vector"]["evidence_file_line"]
    assert len(zq) >= 4, "DECLARED_ZERO_QUOTA 的引用条目从 4 条减少了：%d" % len(zq)
    assert len(lx) >= 3, "DECLARED_LEXICAL_CHANNELS 的引用条目从 3 条减少了：%d" % len(lx)
    assert _bad_refs(L.DECLARED_ZERO_QUOTA, "DECLARED_ZERO_QUOTA") == []
    assert _bad_refs(L.DECLARED_LEXICAL_CHANNELS, "DECLARED_LEXICAL_CHANNELS") == []
    # 新结构下不再携带权威行号（这就是"今后编辑不再漂移"的机制）
    for r in zq + lx:
        assert "anchor" in r, "仍有条目没升级成 anchor：%r" % r
        assert "line" not in r, "仍残留权威 `line` 字段（会重新开始漂移）：%r" % r


# ══════════════════════════════════════════════════════════════════════════
# ⑧ 【t22】「存在但不生效」升格为**能力事实**（verifier 的 F5/§0.3 复核）
#
# 主题：**名字说有、实质没有**。t6 的"收敛"只解决了**身份**（同名类剩 1 处定义、
# `same_object=true`），**没有**解决**效力**：
#   · `GuardianChainV50` 50 级守卫里 **0** 级在 enforcing
#   · `RetrievalSystemV47` 47 个通道里 **0** 个在 contributing
# 这两条现在登记在 `capability_ledger.EFFECTIVENESS_FACTS`，由本节判据重算。
# ══════════════════════════════════════════════════════════════════════════

def _live_effectiveness() -> dict:
    from trinity.modules.second_brain.engine_guardian_retrieval import (
        GuardianChainV50, RetrievalSystemV47)

    gc, rs = GuardianChainV50(), RetrievalSystemV47()

    def hist(d):
        h: dict = {}
        for v in d.values():
            h[type(v).__name__] = h.get(type(v).__name__, 0) + 1
        return h

    return {
        "guardian": {"declared": gc.total, "effective": gc.enforcing_count(),
                     "value_type_histogram": hist(gc.shields)},
        "retrieval": {"declared": rs.total, "effective": rs.contributing_count(),
                      "value_type_histogram": hist(rs.channels)},
    }


def test_生效数量必须作为能力事实登记_而不只是登记数字() -> None:
    """**声明数 ≠ 生效数**：两个口径都要登记，且"为什么是 0"也要登记。

    只登记数字（`50` / `47`）会让读者把它读成能力量 —— 这正是本轮的核心死法。
    """
    live = _live_effectiveness()
    for cap, fact in L.EFFECTIVENESS_FACTS.items():
        lv = live[cap]
        assert fact["declared"] == lv["declared"], (
            "%s 声明数登记为 %s，活体实测 %s" % (cap, fact["declared"], lv["declared"]))
        assert fact["effective"] == lv["effective"], (
            "%s 生效数登记为 %s，活体实测 %s（**生效数变了就必须显式改登记 + 说明加了什么真执行体**）"
            % (cap, fact["effective"], lv["effective"]))
        # 事实必须是**有内容的**，不能是占位
        for field in ("counting_method", "why_zero", "can_be_positive_when"):
            assert len(fact.get(field, "")) > 20, "%s 的 %s 是个占位串" % (cap, field)
        assert fact["effective"] == 0, (
            "%s 的生效数不再是 0 —— 若你确实加了真执行体，请更新 EFFECTIVENESS_FACTS 与本节，"
            "并在报告里说明" % cap)


def test_生效数为0的理由是构造性的_名册全是字符串名字() -> None:
    """钉住**理由**（值类型直方图），而不只是钉住数字。

    静态（AST）与运行期两条路径都必须给出一致的类型直方图 ——
    否则"为什么是 0"就只是叙述而不是事实。
    """
    src = L.load_capability_sources()
    static = L.effectiveness_registry_facts(src)
    live = _live_effectiveness()
    for cap, fact in L.EFFECTIVENESS_FACTS.items():
        attr = fact["registry_attr"]
        assert attr in static, "静态扫描没找到 `self.%s` 的一次性字面量赋值" % attr
        assert static[attr]["value_type_histogram"] == fact["registry_value_types"], (
            "%s::self.%s 的值类型直方图与登记不符：静态 %s vs 登记 %s"
            % (cap, attr, static[attr]["value_type_histogram"], fact["registry_value_types"]))
        assert live[cap]["value_type_histogram"] == fact["registry_value_types"], (
            "%s::self.%s 的运行期值类型直方图与登记不符：%s vs %s"
            % (cap, attr, live[cap]["value_type_histogram"], fact["registry_value_types"]))


def test_负向实测_改指标不等于改能力_把dict塞进名册会让生效数虚增() -> None:
    """**度量可被游戏**：`enforcing_count()` 的定义只是 `not isinstance(v, str)`。

    于是"把 `get_new_shields()`（17 条 name/paper/purpose 元数据 dict）并进名册"
    就能把 0 变成 17 —— 而**一个真执行体都没加**。本用例把这一点变成可判据的事实：
    直方图判据必须对这种"虚增"变红，从而强制改的人**说明白加了什么**。
    """
    fake_src = {
        "fake.py": "class C:\n"
                   "    def __init__(self):\n"
                   "        self.shields = {'L1': 'NameOnly', 'L2': {'name': 'Meta'}}\n"
    }
    static = L.effectiveness_registry_facts(fake_src)
    assert static["shields"]["value_type_histogram"] == {"str": 1, "dict": 1}, (
        "静态直方图没认出非字符串值：%s" % static["shields"]["value_type_histogram"])
    assert L.effectiveness_registry_facts(fake_src)["shields"]["n"] == 2

    # 算术推演（**不改任何对象**）：把现成的 17 条 dict 名册并进去会怎样
    from trinity.modules.second_brain.engine_guardian_retrieval import GuardianChainV50
    gc = GuardianChainV50()
    richer = gc.get_new_shields()
    assert len(richer) == 17 and all(isinstance(v, dict) for v in richer.values())
    merged = dict(gc.shields)
    merged.update(richer)
    fictitious = sum(1 for v in merged.values() if not isinstance(v, str))
    assert fictitious == 17, "算术推演应得 17，实得 %s" % fictitious
    # ⇒ 结论：数字可变，能力不变。登记里的 `can_be_positive_when` 必须写明这一点。
    assert "get_new_shields" in L.EFFECTIVENESS_FACTS["guardian"]["can_be_positive_when"], (
        "登记必须点明「什么条件下会 >0」，以及它**不是**能力提升")


def test_死名册_zero调用点必须被判死() -> None:
    """**"存在但不生效"的机械判据**：登记了、却零调用点的名册。

    `get_new_shields()` 有 17 条内容，`engine_core` 只在注释里提到它 ⇒ 死代码。
    """
    src = L.load_capability_sources()
    dead = L.dead_registry_call_sites(src)
    assert "get_new_shields" in dead
    assert dead["get_new_shields"]["dead"] is True, (
        "`get_new_shields` 出现了调用点：%s ⇒ 它已生效，请更新登记与本节"
        % dead["get_new_shields"]["reference_sites"])
    assert dead["get_new_shields"]["definition_sites"] == 1


def test_负向实测_死名册判据必须能红_以及文本提及不算引用() -> None:
    """两条承重证明：

    1. 一旦有人真的调用它，判据必须**不再说它死**（否则这条判据恒真）；
    2. 只写在**注释/docstring**里的提及**不得**被当成调用点
       —— 这正是我自己定的"判据不能被文本骗"那条规矩（本仓已有前科）。
    """
    called = {"f.py": "def g():\n    return get_new_shields()\n"}
    assert L.dead_registry_call_sites(called)["get_new_shields"]["dead"] is False, (
        "真调用点必须让 `dead` 变 False，否则这条判据是恒真的")

    text_only = {"f.py": "def g():\n"
                         "    # get_new_shields 只是被提到，没有被调用\n"
                         '    """docstring 也提到 get_new_shields"""\n'
                         "    return 1\n"}
    rec = L.dead_registry_call_sites(text_only)
    assert rec["get_new_shields"]["dead"] is True, (
        "注释/docstring 里的提及被误当成调用点 ⇒ 判据会被文本骗：%s"
        % rec["get_new_shields"]["reference_sites"])


# ══════════════════════════════════════════════════════════════════════════
# ⑨ 【t22 · t20 追加】BOM 表**可达性**判据（verifier 抓到 `foreign_bom` 恒假分支）
#
# 缺陷（已复现）：`scripts/text_integrity_guard.py` 原先把 2 字节的 UTF-16 BOM 放进
# 与 **4 字节切片** `raw[:4]` 比较的集合里 ⇒ 那两个常量**永不可达** ⇒
# `foreign_bom` 对**真实 UTF-16 文件**永不触发（只有 UTF-32 正常）。
# 危害是**标签错/丢修复线索**（`invalid_utf8 + control_chars` 仍会兜住），不是静默放行。
#
# 要求补的不是"UTF-16 能触发"这一条用例，而是**能发现"常量不可达/分支恒假"的检查方式**。
# ══════════════════════════════════════════════════════════════════════════

def _guard():
    import importlib.util

    path = ROOT / "scripts" / "text_integrity_guard.py"
    spec = importlib.util.spec_from_file_location("_tig_for_hygiene", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_BOM表里每个常量都必须可达() -> None:
    """**通用静态可达性**：表里每个 BOM 常量都必须能被识别出来（顺序也必须对）。"""
    g = _guard()
    probe = g.bom_reachability_probe()
    assert len(probe) >= 3, "BOM 表太小，判据没有覆盖面"
    unreachable = [p for p in probe if p["detected_as"] != p["name"]]
    assert unreachable == [], (
        "以下 BOM 常量**不可达**（常量长度与比较逻辑不匹配 ⇒ 分支恒假）：%s"
        % unreachable)


def test_负向实测_BOM常量长度与比较切片不匹配必须被抓() -> None:
    """把**历史上那个坏实现**喂给探针 ⇒ 必须报出"不可达"。

    这就是"这个判据本来能提前抓到这次的 bug"的证明（而不是事后补一条 UTF-16 用例）。
    """
    g = _guard()

    def buggy_detector(raw, table):
        # 复刻缺陷：用固定的 4 字节切片去比一组**长度不一**的常量
        for bom, name in table:
            if raw[:4] == bom:
                return name
        return None

    probe = g.bom_reachability_probe(detector=buggy_detector)
    bad = sorted(p["bom_hex"] for p in probe if p["detected_as"] is None)
    # 通用命题：**长度与被比较切片（4）不一致的常量必然不可达**
    mismatched = sorted(p["bom_hex"] for p in probe if p["bom_len"] != 4)
    assert bad == mismatched, (
        "旧实现下不可达的应恰好是长度≠4 的那些常量：不可达 %s vs 长度不匹配 %s"
        % (bad, mismatched))
    assert bad, "旧实现必须至少有一个不可达常量，否则本用例没有咬到东西"
    assert "fffe" in bad and "feff" in bad, (
        "本次事故的那两个 2 字节 UTF-16 常量必须在不可达集合里：%s" % bad)

    # 修复后的实现：同一探针下**没有**不可达项
    assert [p for p in g.bom_reachability_probe() if p["detected_as"] is None] == []


def test_负向实测_真实UTF16夹具必须报出foreign_bom(tmp_path) -> None:
    """端到端：UTF-16 LE/BE 的真实字节（`.encode('utf-16')` / 手工 BE BOM）必须触发。"""
    g = _guard()
    payload = "# 中文注释\nx = 1\n"
    cases = {
        "utf16le": payload.encode("utf-16"),
        "utf16be": b"\xfe\xff" + payload.encode("utf-16-be"),
        "utf32le": payload.encode("utf-32"),
        "utf32be": b"\x00\x00\xfe\xff" + payload.encode("utf-32-be"),
    }
    for name, raw in cases.items():
        rec = g.inspect_bytes(raw, ".txt")
        assert rec["foreign_bom"] is True and "foreign_bom" in rec["hard"], (
            "%s 没触发 foreign_bom：bom_name=%s hard=%s" % (name, rec["bom_name"], rec["hard"]))

    # UTF-16LE 的 BOM 以 UTF-32LE 的 BOM 为前缀 ⇒ 必须先长后短，否则会被误标
    assert g.inspect_bytes(cases["utf32le"], ".txt")["bom_name"] == "utf-32-le"
    assert g.inspect_bytes(cases["utf16le"], ".txt")["bom_name"] == "utf-16-le"
    # 正常 UTF-8（无 BOM）不得被误报
    assert g.inspect_bytes(payload.encode("utf-8"), ".txt")["bom_name"] is None


# ══════════════════════════════════════════════════════════════════════════
# ⑩ 【t29】`structure_gate` 的**扫描面覆盖率**：把"门禁漏掉了多少"变成可核事实
#
# 立项理由（我 t22 的外部观察）：`structure_gate` 报 **176 条** `[warn] AST 遍历失败
# ⇒ 跳过该文件（漏记绑定）`。它是本轮反复引用的结构门，而它的**覆盖面本身未知**。
#
# t29 实测定性：**176/176 都是同一个原因** —— 文件带 UTF-8 BOM，而读取用 `utf-8`
# （不剥 BOM）⇒ `ast.parse` 抛 `invalid non-printable character U+FEFF`。
# **这一课 2026-09-29 已在棘轮面修过**（`_scan_silent_pass_repo` 用 `utf-8-sig`，注释里
# 写着"注入 BOM 版 except:pass 文件，门禁仍报 PASS"），但**没传导到调用点面**。
# 且排除表也漂移了：棘轮面有 `SILENT_FAILURE_SKIP_DIRS`，调用点面自写一套。
#
# 本节钉住的是**覆盖面恒等式**（而不是"176 已修"）：它让"漏掉多少"永远可见。
# ══════════════════════════════════════════════════════════════════════════

def _sg():
    import importlib.util

    path = ROOT / "scripts" / "structure_gate.py"
    spec = importlib.util.spec_from_file_location("_structure_gate_for_hygiene", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_扫描面五桶恒等式成立且解析失败为0() -> None:
    """**判据本体**：`analyzed + ps1 + ast_failed + excluded_decl + excluded_self == visited`
    且 `ast_failed == 0`。任一不满足 ⇒ 红（门禁里由 `structure:scan_coverage` 承担）。"""
    sg = _sg()
    self_path = str(ROOT / "trinity" / "core" / "client" / "_advanced.py")
    r = sg.scan_face_counts(root=str(ROOT), self_path=self_path)
    v = r["verdict"]
    assert v["identity_ok"], "五桶拆不干净：%s" % v
    assert v["ast_failed"] == 0, (
        "有 %d 个文件解析失败（应当为 0；失败文件：%s）"
        % (v["ast_failed"], [f["file"] for f in r["failed"]][:8]))
    assert v["ok"] is True
    # 判定面不能退化：真的分析了大量文件，且真的排除了大量文件（两桶都必须非零）
    assert v["analyzed"] > 1000, "已分析文件数只有 %d ⇒ 扫描面可疑" % v["analyzed"]
    assert v["excluded_decl"] > 100, "声明式排除数只有 %d ⇒ 排除表形同虚设" % v["excluded_decl"]


def test_负向实测_解析失败的文件必须让覆盖面判据变红(tmp_path) -> None:
    """构造一个**真的解析不了**的文件 ⇒ 覆盖面判据必须红，并**点名该文件**。

    注意用的不是 BOM（BOM 已修好），而是真语法错误 —— 这类文件**不该**被静默跳过。
    """
    sg = _sg()
    (tmp_path / "good.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    r = sg.scan_face_counts(root=str(tmp_path))
    v = r["verdict"]
    assert v["identity_ok"], "五桶仍应拆干净：%s" % v
    assert v["ast_failed"] == 1, "解析失败数应为 1，实测 %s" % v["ast_failed"]
    assert v["ok"] is False, "有解析失败文件时覆盖面判据**必须红**（不能只发 warn）"
    assert any("broken.py" in f["file"] for f in r["failed"]), (
        "失败文件必须被点名：%s" % r["failed"])


def test_负向实测_五桶恒等式被破坏必须变红() -> None:
    """纯函数层：把任一桶改一个数 ⇒ 恒等式必须不成立、`ok` 必须 False。"""
    sg = _sg()
    assert sg._coverage_verdict(10, 2, 1, 5, 0, 2)["ok"] is True      # 5+2+0+2+1 = 10
    for tampered in (
        sg._coverage_verdict(10, 2, 1, 6, 0, 2),    # analyzed 多算 1
        sg._coverage_verdict(10, 2, 1, 5, 0, 3),    # ps1 多算 1
        sg._coverage_verdict(10, 3, 1, 5, 0, 2),    # 排除多算 1
        sg._coverage_verdict(11, 2, 1, 5, 0, 2),    # 访问数被多报 1
    ):
        assert tampered["identity_ok"] is False, "恒等式被破坏却没判红：%s" % tampered
        assert tampered["ok"] is False
    # ast_failed > 0 时必须红（即使恒等式成立）
    nf = sg._coverage_verdict(10, 2, 1, 4, 1, 2)
    assert nf["identity_ok"] is True and nf["ok"] is False


def test_负向实测_带BOM的文件在剥BOM后必须被计入已分析(tmp_path) -> None:
    """**钉住那一课**：同样一个带 BOM 的合法 `.py`，修复后必须算"已分析"。

    若有人把 `_read_text` 改回 `encoding="utf-8"`，本用例立刻红
    —— 这正是"同一课没传导"的防复现装置。
    """
    sg = _sg()
    (tmp_path / "bom_ok.py").write_bytes(b"\xef\xbb\xbf" + "x = 1\n".encode("utf-8"))
    r = sg.scan_face_counts(root=str(tmp_path))
    assert r["verdict"]["analyzed"] == 1, (
        "带 BOM 的合法文件未被计入已分析：%s（读取函数应剥 BOM）" % r["verdict"])
    assert r["verdict"]["ast_failed"] == 0


def test_负向实测_scratch目录必须走声明式排除而不是解析失败(tmp_path) -> None:
    """`temp/` 下的脚本必须算**声明式排除**（`excluded_decl`），不能算"解析失败"。

    这条把"排除必须是声明式的"钉住：旧实现里 169 个 `temp/` 脚本进了分析面、
    然后因 BOM 而"失败即跳过"——那是"静默跳过"，不是"声明式排除"。
    """
    sg = _sg()
    (tmp_path / "temp").mkdir()
    (tmp_path / "temp" / "probe.py").write_bytes(b"\xef\xbb\xbf" + "y = 2\n".encode("utf-8"))
    (tmp_path / "keep.py").write_text("z = 3\n", encoding="utf-8")
    v = sg.scan_face_counts(root=str(tmp_path))["verdict"]
    assert v["excluded_decl"] == 1, "temp/ 下的文件未被声明式排除：%s" % v
    assert v["analyzed"] == 1 and v["ast_failed"] == 0, v
    assert v["identity_ok"] and v["ok"], v


# ══════════════════════════════════════════════════════════════════════════
# ⑪ 【t33】盲区关闭：真源码不得带 BOM + 同族写法棘轮 + 死代码已删
#
# 背景：t29 定性出"176 个文件被跳过 AST"，其中 **6 个是真源码 `.py` 带 BOM**；
# 而全仓有几十处"读源码再 ast.parse"用的是 `encoding="utf-8"`（不剥 BOM）
# ⇒ 这 6 个文件对**那些门禁全部不可见**（`fake_green_audit` 实测：剥前 126 已扫/6 失败，
# 剥后 132 已扫/0 失败）。
# t33 把这 6 个 BOM 剥掉（Δ 恰好 3 字节、`ast.dump` 完全相同、compile/import 均通过），
# 并在此**钉住三件事**防止重演。
# ══════════════════════════════════════════════════════════════════════════

def test_真源码py不得带BOM() -> None:
    """**判据本体**：分析面里带 UTF-8 BOM 的 `.py` 必须为 **0**（不是 `<=`，是零容忍）。

    为什么零容忍：BOM 不改变 Python 执行（CPython 自己会剥），但会让**每一个**用
    `encoding="utf-8"` 读源码再 `ast.parse` 的工具**静默看不见这个文件** ——
    一处 BOM 就能让几十个门禁同时瞎掉这个文件。t33 前分析面里有 6 个。
    """
    sg = _sg()
    self_path = str(ROOT / "trinity" / "core" / "client" / "_advanced.py")
    r = sg.scan_face_counts(root=str(ROOT), self_path=self_path)
    v = r["verdict"]
    assert v["bom_source"] == 0, (
        "分析面里有 %d 个真源码 `.py` 带 UTF-8 BOM ⇒ 它们对全仓'读源码再 ast.parse'的门禁"
        "（当前基线 %d 处）**全部不可见**：%s"
        % (v["bom_source"], sg.SAME_FAMILY_BASELINE, r["bom_files"][:8]))
    assert v["ok"] is True, v


def test_负向实测_带BOM的真源码必须被判红并点名(tmp_path) -> None:
    """造一个带 BOM 的**真源码**夹具 ⇒ 覆盖面判据必须红，并点名该文件。"""
    sg = _sg()
    (tmp_path / "src.py").write_bytes(b"\xef\xbb\xbf" + "x = 1\n".encode("utf-8"))
    (tmp_path / "ok.py").write_text("y = 2\n", encoding="utf-8")
    r = sg.scan_face_counts(root=str(tmp_path))
    assert r["verdict"]["bom_source"] == 1, r["verdict"]
    assert r["verdict"]["identity_ok"] is True, "BOM 不计入恒等式桶，恒等式仍应成立"
    assert r["verdict"]["ok"] is False, "带 BOM 的真源码必须让判据红"
    assert any("src.py" in f for f in r["bom_files"]), r["bom_files"]


def test_同族写法棘轮只降不升() -> None:
    """`读源码用 encoding="utf-8" 再 ast.parse` 的处数必须 `<=` 基线（只降不升）。

    这条**不要求**把存量改完（几十处，逐个改是几十次手术）；它只保证**不再增长**。
    外部复核用同一个纯函数自己数一遍，与门禁 `structure:utf8sig_rule` 的报数对照。
    """
    sg = _sg()
    import io as _io
    n = 0
    files = []
    for dp, dn, fn in os.walk(str(ROOT)):
        parts = set(dp.replace(str(ROOT), "").split(os.sep))
        if parts & sg.SILENT_FAILURE_SKIP_DIRS:
            continue
        for f in sorted(fn):
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            try:
                text = _io.open(p, encoding="utf-8-sig", errors="replace").read()
            except OSError:
                continue
            rel = p.replace(str(ROOT), "").replace(os.sep, "/")
            hits = sg.same_family_utf8_reads(rel, text)
            if hits:
                n += len(hits)
                files.append(hits[0]["file"])
    assert n <= sg.SAME_FAMILY_BASELINE, (
        "同族写法 %d 处 > 基线 %d ⇒ 新增了「用 utf-8 读源码再 ast.parse」：\n  %s\n"
        "⇒ 请改用 `_read_text`/`utf-8-sig`（BOM 会让这些文件对判据不可见）"
        % (n, sg.SAME_FAMILY_BASELINE, "\n  ".join(files[:8])))
    assert n > 0, "一处都没扫到 ⇒ 判据可能已失效（存量本该 >0）"


def test_负向实测_同族写法判据必须能红_且只认读动作() -> None:
    """两件事一起钉：

    1. 坏写法（`utf-8` 读 + `ast.parse`）必须被检出（否则棘轮是恒真的）；
    2. **`utf-8-sig` 不算**，而且**只在写文件时用 `utf-8`** 也不算 ——
       首版文本判据把 `structure_gate.check_registry_consumer` 自己误报进去了
       （它写台账用的是 `io.open(..., "w", encoding="utf-8")`），
       所以这条必须证明"判据判定的是**读**这个动作"。
    """
    sg = _sg()
    bad = ('import ast, io\n'
           'def scan(p):\n'
           '    t = io.open(p, encoding="utf-8", errors="replace").read()\n'
           '    return ast.parse(t)\n')
    assert sg.same_family_utf8_reads("b.py", bad), "坏写法没被检出 ⇒ 棘轮恒真"

    good = ('import ast, io\n'
            'def scan(p):\n'
            '    t = io.open(p, encoding="utf-8-sig").read()\n'
            '    return ast.parse(t)\n')
    assert sg.same_family_utf8_reads("g.py", good) == [], "utf-8-sig 被误报"

    writer_only = ('import ast, io\n'
                   'def f(p):\n'
                   '    io.open(p, "w", encoding="utf-8").write("{}\\n")\n'
                   '    t = io.open(p, encoding="utf-8-sig").read()\n'
                   '    return ast.parse(t)\n')
    assert sg.same_family_utf8_reads("w.py", writer_only) == [], (
        "只写文件时用 utf-8 被误报 ⇒ 判据判定的是'函数里出现过这个串'而不是'读'这个动作")

    # 本判据所在的脚本自己不得命中（严禁"判据自己踩自己要防的坑"）
    import io as _io
    src = _io.open(str(ROOT / "scripts" / "structure_gate.py"), encoding="utf-8-sig").read()
    assert sg.same_family_utf8_reads("scripts/structure_gate.py", src) == [], (
        "structure_gate.py 自己命中了同族写法（t33 修过一处，不得回退）")


def test_死代码_ast_pass_count_已删除() -> None:
    """T29-R5：`_ast_pass_count()` 是**零调用点**的死代码，t33 已删。

    它属于本轮主题"存在但不生效"：名字与棘轮面高度相似、却没有任何判据在用。
    本用例把它**钉住不许复活**（真有需求请用 `_scan_silent_pass_repo`）。
    """
    sg = _sg()
    assert not hasattr(sg, "_ast_pass_count"), (
        "`_ast_pass_count` 又回来了 —— 它是死代码（零调用点）。"
        "要按文件统计 except:pass 请用 `_scan_silent_pass_repo()`。")
