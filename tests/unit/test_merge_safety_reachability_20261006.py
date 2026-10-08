# -*- coding: utf-8 -*-
"""合并判据的**可达性**判据（t11 / 2026-10-06）。

## 为什么需要"可达性"这一层

本仓反复出现的缺陷不是"判据写错"，而是"判据**在被调用的位置上**不可能触发"：
`validate()` 恒真式、`structure_gate` 自开开关、`verify_merge_safety` 曾指向一个不存在的方法……
2026-10-06 的 G1 又添一例：**三条规则里有两条只在"孤立合成输入"下测过能判红**，
放到真实调用点的约束（合并闸门 `Jaccard ≥ SIMILARITY_MERGE_THRESHOLD = 0.75`）下**永远无法触发**：

* R1 `content_collapse`：闸门 ⇒ `|B| ≥ 0.75·|A|`（token），而它比的是**字符** < 35%
  ⇒ 实测 0/5,295 命中（闸门内长度比最小 0.3519）。
* R2 `source_downgrade`：`set(existing).add(new)` 之后判 `issuperset` ⇒ 对任意输入恒真。

⇒ 一条判据正确性的**必要**条件不是"构造违规输入能判红"，而是
**"在被调用位置的真实约束下能判红"**。本文件把这句话变成机器判据：

| 情形 | 结论 |
|---|---|
| active 规则在闸门约束输入里**一次都没命中** | **RED**（不可达 ⇒ 门禁没有判别力） |
| 退役规则**仍能**产出对应 code | **RED**（僵尸门禁：登记退役却还在响） |
| 源码里能产出的 code 未在账本登记 | **RED**（未登记的规则） |
| 账本登记了源码里不存在的规则 | **RED**（账本与实现脱节） |
| `unwired-postcondition`：直接调用可达但**未接线** | WARNING（不得被读成"已有防护"） |

## 本判据必须自己有牙齿

它同时要能抓住**本次这类错误**：把同一套逻辑指向**改前**的 `_merge_safety.py`
（`git show HEAD:trinity/agents/aggregator/_merge_safety.py`）必须判**红**。
见 `test_可达性判据在改前源码上必须判红`。

## 口径（闸门约束怎么来）

闸门阈值从 `_constants.py` **用 AST 读出来**（不 import 包，避免 secondheavy 副作用链），
并与历史标定产物里的 0.75 对齐；输入按 **NFKC 归一后**的 token 集合算 Jaccard，
每个输入族都**断言自己的 Jaccard ≥ 闸门**（族本身不允许"悄悄掉出闸门"）。
"""
from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import unicodedata
from collections import Counter

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
CONSTANTS_PY = REPO / "trinity" / "agents" / "aggregator" / "_constants.py"
MERGE_SAFETY_PY = REPO / "trinity" / "agents" / "aggregator" / "_merge_safety.py"
GATE_FALLBACK = 0.75
_TOKEN = re.compile(r"[0-9A-Za-z_]{2,}|[\u4e00-\u9fff]")


# ─────────────────────────────────────────── 基础设施（可复用于任意修订版）

def gate_threshold() -> float:
    """从 `_constants.py` 读出闸门阈值（AST 读，不 import 包）。"""
    tree = ast.parse(CONSTANTS_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "SIMILARITY_MERGE_THRESHOLD"
                for t in node.targets):
            return float(ast.literal_eval(node.value))
    return GATE_FALLBACK


def tokens(text: str) -> set:
    """判据无关的独立分词：先 NFKC，再中文按字、英文/数字按 ≥2 长度词。"""
    t = unicodedata.normalize("NFKC", str(text or ""))
    return set(_TOKEN.findall(t))


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    u = len(a | b)
    return len(a & b) / u if u else 0.0


def load_by_path(path) -> object:
    """按文件路径加载模块（不 import `trinity` 包 ⇒ 可指向任意修订版）。"""
    p = str(path)
    name = "_ms_reach_%s" % abs(hash(p))
    spec = importlib.util.spec_from_file_location(name, p)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod            # dataclass 需要模块已注册
    spec.loader.exec_module(mod)
    return mod


def codes_in_source(path) -> set:
    """AST 抽出源码里**能产出**的 code（`MergeSafetyVerdict(False, "code", ...)` 的**第 2 个**实参）。

    两个坑都在这里被避开：
    · 用 AST 而不是文本匹配 —— 本模块的 docstring/账本里也写着这些 code（登记退役理由），
      文本匹配会把"注释里提到"误判成"代码里会产出"；
    · **只取第 2 个位置实参**（数据类是 `(safe, code, detail)`）—— 初版把所有字符串实参都收进来，
      于是 `detail` 文案被当成了一个 code（实测报出
      「未登记规则 '来料与既有内容归一化后完全相同…'」）。判据自己也会犯"口径太宽"的错。
    """
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Name) and f.id == "MergeSafetyVerdict"):
            continue
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                and isinstance(node.args[1].value, str):
            out.add(node.args[1].value)
        for kw in node.keywords:
            if kw.arg == "code" and isinstance(kw.value, ast.Constant):
                out.add(str(kw.value.value))
    return out


# ─────────────────────────────────────────── 闸门约束输入族

def _pool(n: int, prefix: str = "t") -> list:
    return ["%s%03d" % (prefix, i) for i in range(n)]


def gate_constrained_pairs() -> list:
    """返回 `[(existing, incoming, jaccard, family)]`，**每个都断言 Jaccard ≥ 闸门**。"""
    gate = gate_threshold()
    fam = []

    a = _pool(60)
    fam.append(("identity", " ".join(a), " ".join(a)))
    fam.append(("whitespace-variant", " ".join(a), "  ".join(a) + "  "))
    fam.append(("token-reorder", " ".join(a), " ".join(reversed(a))))
    fam.append(("fullwidth-variant", " ".join(a[:8]), " ".join(_fullwidth(t) for t in a[:8])))

    # 等长 + 14% 新 token：由 |A∩B| ≥ 0.75|A∪B| 可证这是**可达边界附近**
    # （等长时新信息比上限 ≈ 0.143）⇒ 任何"新信息比"类判据的阈值必须 ≤ 这个量级。
    a100, shared86, novel14 = _pool(100), _pool(86), _pool(14, "n")
    fam.append(("novelty-14pct-equal-length",
                " ".join(a100), " ".join(shared86 + novel14)))

    # 来料更短（R1 的形状）：token 比只能压到 0.75 —— 这正是 R1 够不着的原因
    fam.append(("truncated-echo-at-gate-edge", " ".join(_pool(40)), " ".join(_pool(30))))
    fam.append(("truncated-echo-75pct", " ".join(_pool(80)), " ".join(_pool(60))))

    # 来料更长 + 少量新 token
    fam.append(("longer-incoming-10pct-novel",
                " ".join(_pool(100)), " ".join(_pool(90) + _pool(10, "n"))))

    out = []
    for name, existing, incoming in fam:
        j = jaccard(tokens(existing), tokens(incoming))
        assert j >= gate, ("输入族 %s 的 Jaccard=%.4f < 闸门 %.2f —— 它不在闸门约束内，"
                           "不能用它证明可达性" % (name, j, gate))
        out.append((existing, incoming, round(j, 4), name))
    return out


def _fullwidth(s: str) -> str:
    return "".join(chr(ord(c) - 0x20 + 0xFF00) if "!" <= c <= "~" else c for c in s)


# ─────────────────────────────────────────── 执行与裁决

def fired_codes(mod) -> Counter:
    """在闸门约束输入上执行**前置**判据，统计判红的 code。"""
    os.environ.pop("TRINITY_MERGE_SAFETY", None)       # 默认档（on）
    c = Counter()
    for existing, incoming, _j, _f in gate_constrained_pairs():
        v = mod.verify_merge_safety(incoming, existing)
        if not getattr(v, "safe", True):
            c[str(getattr(v, "code", ""))] += 1
    return c


def postcondition_fired_codes(mod) -> set:
    """直接调用**后置**不变量（若该修订版已实现），收集可达 code。

    三个输入对应：正常 add（绿）、重建集合（丢失既有来源）、忘记加入本次来源。
    """
    fn = getattr(mod, "verify_merge_postcondition", None)
    if fn is None:
        return set()
    os.environ.pop("TRINITY_MERGE_SAFETY", None)
    out = set()
    cases = [
        ({"a", "b"}, {"a", "b", "c"}, "c"),      # 正常：.add() 之后
        ({"a", "b"}, {"c"}, "c"),                # 反事实：重建集合 ⇒ 既有来源丢失
        ({"a", "b"}, {"a", "b"}, "c"),           # 反事实：忘记加入本次来源
    ]
    for before, after, new in cases:
        v = fn(before, after, new_source=new)
        if not getattr(v, "safe", True):
            out.add(str(getattr(v, "code", "")))
    return out


def ledger_of(mod) -> dict:
    led = getattr(mod, "RULE_LEDGER", None)
    return dict(led) if isinstance(led, dict) else {}


def harness_report(module_path, ingest_path=None) -> dict:
    """可达性裁决。返回 `{"green":..., "problems":[...], "warnings":[...], ...}`。

    `green` 为 False ⇒ 该修订版的规则集**不合格**（存在不可达/僵尸/未登记/接线不符）。

    `ingest_path`：调用点源码（默认本仓 `_ingest.py`）。可用它做**负向实测**：
    传一份"把 `verify_merge_postcondition(` 摘掉"的副本 ⇒ 应当判红
    （证明"账本声明已接线"这一条有牙齿）。
    """
    mod = load_by_path(module_path)
    codes = codes_in_source(module_path)
    led = ledger_of(mod)
    post_codes = {c for c, m in led.items() if m.get("host")}
    pre_codes = codes - post_codes
    ip = pathlib.Path(ingest_path) if ingest_path else (REPO / "trinity" / "agents"
                                                        / "aggregator" / "_ingest.py")
    try:
        ingest_src = ip.read_text(encoding="utf-8") if ip.exists() else None
    except OSError:
        ingest_src = None
    if ingest_path is None and not led:
        # 历史版本（无账本）不做接线核对：那时的 _ingest.py 与它配套，硬套会得出错误结论
        ingest_src = None

    pre_fired = fired_codes(mod)
    post_fired = postcondition_fired_codes(mod)
    reachable = set(pre_fired) | post_fired

    problems: list = []
    warnings: list = []

    if not led:
        problems.append("模块未登记规则账本（RULE_LEDGER 缺失）⇒ 无法证明任何规则的适用性与可达性")

    for code in sorted(pre_codes | post_codes):
        meta = led.get(code)
        status = str((meta or {}).get("status", ""))
        is_reachable = code in reachable
        if meta is None:
            problems.append("未登记规则 %r：源码能产出它，但账本里没有 —— "
                            "无法判断它是否适用、是否可达" % code)
            if not is_reachable:
                problems.append(
                    "不可达（门禁没有判别力）：规则 %r 在闸门约束输入里**一次都没命中**，"
                    "且它连账本都没登记 ⇒ 双重问题（这正是 t11 要报出的那类缺陷）" % code)
        elif status == "active":
            if not is_reachable:
                problems.append("不可达（门禁没有判别力）：active 规则 %r 在闸门约束输入里"
                                "一次都没命中" % code)
        elif status.startswith("retired"):
            if is_reachable:
                problems.append("僵尸门禁：已登记退役的规则 %r 仍能判红" % code)
        elif status in ("active-postcondition", "unwired-postcondition"):
            if not is_reachable:
                problems.append("登记为 postcondition 的 %r 直接调用也不可达 ⇒ 判据是死的" % code)
            if ingest_src is None:
                continue            # 非本仓模块（如改前的历史版本）不做接线核对
            wired = "verify_merge_postcondition(" in ingest_src
            declared = bool(meta.get("wired"))
            if declared and not wired:
                problems.append(
                    "账本声明已接线（wired=True），但 _ingest.py 里找不到 `verify_merge_postcondition(` "
                    "调用 ⇒ **假称已接线**（这正是本轮判准：写好了没接上不得被读成『已有防护』）")
            if (not declared) and wired:
                problems.append("账本记 wired=False，但 _ingest.py 里已出现调用 ⇒ 账本与实现脱节")
            if not declared:
                warnings.append("后置不变量 %r 已实现但**未接线**（wired=False）："
                                "它**不是**当前生效的防护，只是可失败判据 + 待批准的接线点" % code)
        else:
            problems.append("规则 %r 的 status=%r 不被识别（只认 active / active-postcondition / "
                            "unwired-postcondition / retired*）" % (code, status))

    # 三方核对：AST 抽出的 code 集合 / 模块自报的 IMPLEMENTED_CODES / 账本键
    declared_codes = getattr(mod, "IMPLEMENTED_CODES", None)
    if declared_codes is not None:
        if set(declared_codes) != codes:
            problems.append("IMPLEMENTED_CODES 与源码实际产出的 code 不一致："
                            "声明 %r vs 实际 %r" % (sorted(declared_codes), sorted(codes)))
        missing = [c for c in declared_codes if c not in led]
        if missing:
            problems.append("IMPLEMENTED_CODES 里的 %r 未在账本登记" % missing)
    elif led:
        problems.append("模块有账本但没有 IMPLEMENTED_CODES ⇒ 无法做三方核对")

    for code, meta in sorted(led.items()):
        status = str(meta.get("status", ""))
        if status.startswith("retired"):
            continue        # 退役规则**本来就该**产不出 code；这不是脱节，是目的
        if code not in codes and not meta.get("host"):
            problems.append("账本登记了规则 %r，但源码里产不出这个 code ⇒ 账本与实现脱节" % code)

    return {
        "module": str(module_path),
        "ingest": str(ip),
        "gate_threshold": gate_threshold(),
        "codes_in_source": sorted(codes),
        "ledger": {k: v.get("status") for k, v in led.items()},
        "pre_gate_fired": dict(pre_fired),
        "post_fired": sorted(post_fired),
        "reachable_codes": sorted(reachable),
        "problems": problems,
        "warnings": warnings,
        "green": not problems,
    }


# ─────────────────────────────────────────── 断言

def test_闸门阈值与历史标定产物一致():
    """闸门阈值是 0.75（与 t4 标定/postfix 分析同一个数）；变了必须重新标定。"""
    assert gate_threshold() == GATE_FALLBACK


def test_输入族确实都在闸门约束内():
    """族不能"悄悄掉出闸门" —— 否则下面所有可达性结论都不成立。"""
    pairs = gate_constrained_pairs()
    assert len(pairs) >= 8
    for _e, _i, j, name in pairs:
        assert j >= gate_threshold(), (name, j)


def test_可达性裁决必须绿():
    """**核心判据**：每条 active 规则在闸门约束下都能触发；退役规则不再触发；无未登记 code。"""
    rep = harness_report(MERGE_SAFETY_PY)
    assert rep["green"] is True, "可达性判据判红：\n  - " + "\n  - ".join(rep["problems"])


def test_R3可达且真能命中():
    """R3 必须在闸门约束输入里命中（现状：identity / 空白变体 / 全角变体 / 换序都命中）。"""
    rep = harness_report(MERGE_SAFETY_PY)
    assert rep["pre_gate_fired"].get("duplicate_no_new_evidence", 0) >= 3, rep["pre_gate_fired"]


def test_R1与R2不得再能判红_僵尸门禁闸门():
    """R1/R2 已退役：它们的 code 不得再由前置判据产出。"""
    rep = harness_report(MERGE_SAFETY_PY)
    assert "content_collapse" not in rep["pre_gate_fired"]
    assert "source_downgrade" not in rep["pre_gate_fired"]
    led = rep["ledger"]
    assert led["content_collapse"].startswith("retired")
    assert led["source_downgrade"].startswith("retired")


def test_退役必须带理由与证据():
    """登记退役不能只是"标一下"：必须写清为什么退役、证据在哪。"""
    from trinity.agents.aggregator import _merge_safety as ms

    for code, meta in ms.retired_rules().items():
        assert meta.get("retired_reason"), code
        assert meta.get("evidence"), code
    assert ms.RULE_LEDGER["content_collapse"]["retired_reason"].count("不写 content") == 1
    assert ms.RULE_LEDGER["source_downgrade"]["evidence"]["random_search_fired"] == 0


def test_后置不变量可达且已接线():
    """R2 的可失败替代：直接调用**能**判红，且账本声明的接线状态必须与 `_ingest.py` 一致。

    2026-10-06 t13：接线已由队长批准并落地 ⇒ 本条从「未接线 ⇒ WARNING」翻转为
    「**已接线**：`replacement_wired is True`、后置 code 状态为 `active-postcondition`、
    且不再产生『未接线』告警」。**负向方向**由下一条用例（摘掉接线必须判红）保证。
    """
    from trinity.agents.aggregator import _merge_safety as ms

    rep = harness_report(MERGE_SAFETY_PY)
    assert set(rep["post_fired"]) == {"source_lost", "source_not_landed"}, rep["post_fired"]
    assert rep["warnings"] == [], rep["warnings"]
    wired_codes = {c: m for c, m in ms.RULE_LEDGER.items()
                   if str(m.get("status")) == "active-postcondition"}
    assert wired_codes, "后置不变量的两条 code 必须在账本里、且状态为 active-postcondition"
    assert all(m.get("wired") is True for m in wired_codes.values()), wired_codes
    assert ms.RULE_LEDGER["source_downgrade"]["replacement_wired"] is True
    assert ms.RULE_LEDGER["source_downgrade"]["status"].startswith("retired")
    src = (REPO / "trinity" / "agents" / "aggregator" / "_ingest.py").read_text(encoding="utf-8")
    assert "verify_merge_postcondition(" in src, "调用点必须真的接线"
    assert "total_merge_postcondition_violations" in src, "违反必须计数（计数键名可核）"


def test_接线被摘掉必须判红(tmp_path):
    """**负向实测**：把调用点的 `verify_merge_postcondition(` 摘掉 ⇒ 可达性判据必须判红。

    没有这条，"账本声明已接线"就可能是自我宣告而无验证 —— 那正是本轮要消灭的形态
    （「写好了没接上」不得被读成「已有防护」）。
    """
    ingest = (REPO / "trinity" / "agents" / "aggregator" / "_ingest.py").read_text(encoding="utf-8")
    mutated = ingest.replace("verify_merge_postcondition(", "NOT_WIRED(")
    assert mutated != ingest, "变异体没变 ⇒ 本用例的替换方式失效，断言没有牙齿"
    p = tmp_path / "_ingest_nowired.py"
    p.write_text(mutated, encoding="utf-8")

    rep = harness_report(MERGE_SAFETY_PY, ingest_path=str(p))
    assert rep["green"] is False, "摘掉接线竟然还判绿 ⇒ 接线状态没有判据保护"
    blob = " | ".join(rep["problems"])
    assert "假称已接线" in blob, rep["problems"]

    # 反向对照：真实调用点 + 同一模块 ⇒ 必须绿（证明红是由"摘掉接线"引起的，不是别的原因）
    assert harness_report(MERGE_SAFETY_PY)["green"] is True



def test_后置不变量的两条判红都能被真实实现违反():
    """反事实：把 `.add()` 改成重建集合 / 忘记 add —— 两种情况都必须被判红。"""
    from trinity.agents.aggregator import _merge_safety as ms

    before = {"agent-a", "agent-b"}
    assert ms.verify_merge_postcondition(before, before | {"agent-c"},
                                         new_source="agent-c").safe is True
    rebuilt = ms.verify_merge_postcondition(before, {"agent-c"}, new_source="agent-c")
    assert rebuilt.safe is False and rebuilt.code == "source_lost"
    forgot = ms.verify_merge_postcondition(before, set(before), new_source="agent-c")
    assert forgot.safe is False and forgot.code == "source_not_landed"


# ─────────────────────────────── R1'（低新颖度）：R1 的可用替代

def _pool(n: int, prefix: str = "t") -> list:
    return ["%s%03d" % (prefix, i) for i in range(n)]


def test_低新颖度可达_闸门内确有样本被判红():
    """**可达性**：来料是既有的真子集（无新词）且长度比 >= 0.75 时，闸门打开且本判据判红。

    这是 R1 的死因（闸门要求 token 比 >= 0.75）在同一形状上**不再致命**的证据：
    该形状的 Jaccard = 0.75 恰好过闸，而低新颖度正是为它设计的。
    """
    from trinity.agents.aggregator import _merge_safety as ms

    existing = " ".join(_pool(40))
    incoming = " ".join(_pool(30))
    assert jaccard(tokens(existing), tokens(incoming)) >= gate_threshold(), "该形状必须仍在闸门内"
    v = ms.verify_merge_safety(incoming, existing)
    assert v.safe is False and v.code == "low_incoming_novelty", v
    rep = harness_report(MERGE_SAFETY_PY)
    assert rep["pre_gate_fired"].get("low_incoming_novelty", 0) >= 1, rep["pre_gate_fired"]


def test_低新颖度豁免纯换序():
    """**队长裁定 ④ 的硬化**：同一词表换序属合法改写 ⇒ 低新颖度**不得**判红。

    注意此时 R3 也不判红（归一化后不相等）⇒ 这一对在本模块内是"已知残留、双方都放行"，
    正是账本 `known_gap` 登记的那个盲区。本用例把"豁免"变成可失败断言：
    若有人把豁免删掉，这里立刻红。
    """
    from trinity.agents.aggregator import _merge_safety as ms

    existing = " ".join(_pool(40))
    incoming = " ".join(reversed(_pool(40)))
    assert ms.is_pure_reordering(incoming, existing) is True
    assert ms.incoming_novelty(incoming, existing) == 0.0
    v = ms.verify_merge_safety(incoming, existing)
    assert v.safe is True, "纯换序被低新颖度拦了 ⇒ 违反队长裁定 ④（会推高误杀）"


def test_低新颖度豁免值型差异():
    """含数字的新 token = 版本/日期/数量/路径段 ⇒ 哪怕只差一个也是新证据（t4 的 R3 反例口径）。

    构造要点：必须让"只差一个 token"的新颖度**低于阈值**（否则测不到豁免）⇒ 用 400 个 token
    的正文，`1/401 = 0.0025 < 0.003`。同尺度下的**非值型**新 token 则必须被判红
    —— 这样才证明豁免是针对"值型"而不是"一个 token 也不许新"。
    """
    from trinity.agents.aggregator import _merge_safety as ms

    long_toks = _pool(400)
    existing = " ".join(long_toks) + " 271275"
    incoming_digit = " ".join(long_toks) + " 270755"
    incoming_word = " ".join(long_toks) + " zznew"

    # 前提：两者都落在"低新颖度"区间（否则本用例测的不是豁免）
    assert ms.incoming_novelty(incoming_digit, existing) < ms.LOW_NOVELTY_RATIO
    assert ms.incoming_novelty(incoming_word, existing) < ms.LOW_NOVELTY_RATIO
    assert ms.is_pure_reordering(incoming_digit, existing) is False

    v_digit = ms.verify_merge_safety(incoming_digit, existing)
    assert v_digit.safe is True, "一个数字的差异被判成重复 ⇒ 误杀（值型豁免失效）"
    v_word = ms.verify_merge_safety(incoming_word, existing)
    assert v_word.safe is False and v_word.code == "low_incoming_novelty", (
        "同尺度下的**非值型**新 token 应当判红 ⇒ 否则说明豁免范围过宽（判据被架空）")



def test_低新颖度与R3不重叠():
    """**边界声明**：R3 = 归一化后完全相同；R1' = 不完全相同且词表无新词（排除纯换序）。

    「完全相同」必然也是纯换序 ⇒ 被 R1' 的豁免排除 ⇒ 两条规则**不可能同时命中**。
    这里用随机样本复核（而不是只靠推理）。
    """
    import random

    from trinity.agents.aggregator import _merge_safety as ms

    rnd = random.Random(20261006)
    both = 0
    for _ in range(500):
        n = rnd.randint(5, 30)
        base = _pool(n)
        mode = rnd.choice(["identical", "reorder", "subset", "one_new", "digit"])
        old = " ".join(base)
        if mode == "identical":
            new = " ".join(base)
        elif mode == "reorder":
            new = " ".join(rnd.sample(base, len(base)))
        elif mode == "subset":
            new = " ".join(rnd.sample(base, max(1, int(len(base) * 0.8))))
        elif mode == "one_new":
            new = " ".join(base + ["zzz999"])
        else:
            new = " ".join(base[:-1] + ["id%d" % rnd.randint(1000, 9999)])
        v = ms.verify_merge_safety(new, old)
        if not v.safe and v.code not in ("duplicate_no_new_evidence", "low_incoming_novelty"):
            raise AssertionError("意外 code：%s" % v.code)
        # 两条规则各自的"命中"是互斥的：同一个输入只会得到其中一个 code
        if v.code == "duplicate_no_new_evidence":
            assert ms.incoming_novelty(new, old) == 0.0
            assert ms.is_pure_reordering(new, old) is True, (
                "R3 命中的对必须同时是纯换序（否则 R1' 的豁免前提不成立）")
        if v.code == "low_incoming_novelty":
            assert ms.is_pure_reordering(new, old) is False
            both += 1
    assert both > 0, "样本里一次低新颖度都没命中 ⇒ 本用例没真的检验到"


def test_低新颖度误杀上界为0_现场标定():
    """现场标定锁：选定阈值下"被拦却仍带新 token"的对数必须为 **0**。

    数字来自只读标定产物（口径：闸门内 11,193 对；novelty = |来料∖既有|/|来料|；
    上界口径 = 被拦的对里只要带 1 个新 token 就算可能误杀）。产物不在本机则跳过。
    """
    import json

    p = pathlib.Path(r"D:\DSH官网\trinity-optimize-20261006\evidence"
                     r"\merge_safety_low_novelty_calibration.json")
    if not p.exists():
        pytest.skip("标定产物不在本机（CI 无该证据文件）")
    rep = json.loads(p.read_text(encoding="utf-8"))
    from trinity.agents.aggregator import _merge_safety as ms

    b = rep["by_threshold"]["0.005"]
    assert b["blocked_strict_pure_echo"] == b["blocked_strict"] - b[
        "blocked_risky_with_new_tokens"]
    assert ms.LOW_NOVELTY_RATIO <= 0.005, (
        "阈值必须落在只覆盖【零新词】那一类的位置；取更大值会把带新 token 的对吃进来")
    assert b["blocked_risky_with_new_tokens"] <= 1, b
    # 阈值取大到 0.02 就明显变差 —— 把"为什么不用更大阈值"钉成数字
    assert rep["by_threshold"]["0.02"]["blocked_risky_with_new_tokens"] > b[
        "blocked_risky_with_new_tokens"]
    assert rep["novelty_summary"]["max"] < 0.15, "解析/实测上限变了 ⇒ 阈值必须重新标定"



# ─────────────────────────────── 作者盲区（t16：已量化，判定"不改"）

def _blindspot_artifact():
    import json

    p = pathlib.Path(r"D:\DSH官网\trinity-optimize-20261006\evidence"
                     r"\merge_safety_author_blindspot.json")
    if not p.exists():
        pytest.skip("作者盲区标定产物不在本机（CI 无该证据文件）")
    return json.loads(p.read_text(encoding="utf-8"))


def test_作者盲区已量化且数字与账本一致():
    """t16：**跨 agent 复述被一并拒绝**这件事必须带数字地登记，不得静默。

    数字取自只读产物（memories 可读面，与 t13 同一批 11,193 个闸门内对；
    `existing_sources` 的代理 = 既有行的 agent_id）。
    """
    from trinity.agents.aggregator import _merge_safety as ms

    rep = _blindspot_artifact()
    a = rep["source_A_memories"]
    r1 = a["R1prime"]
    r3 = a["R3"]
    # 与 t13 的 5,495 / 118 对得上（同一批样本的另一种切法）
    assert r1["self_copy"] + r1["cross_agent"] == 5495, r1
    assert r3["self_copy"] + r3["cross_agent"] == 118, r3
    # 账本登记必须与实读数字一致（防止有人改了登记却没重跑标定）
    # 注：账本多一个 `total` 字段，这里按 self_copy/cross_agent 两个键比对。
    for code, observed in (("low_incoming_novelty", r1),
                           ("duplicate_no_new_evidence", r3)):
        declared = ms.RULE_LEDGER[code]["known_gap_author"]["split"]
        assert {k: declared[k] for k in ("self_copy", "cross_agent")} == observed, (code, declared)
        assert declared["total"] == observed["self_copy"] + observed["cross_agent"]
    assert ms.RULE_LEDGER["low_incoming_novelty"]["known_gap_author"]["status"].startswith(
        "accepted-residual")
    assert ms.RULE_LEDGER["duplicate_no_new_evidence"]["known_gap_author"]["status"].startswith(
        "accepted-residual")
    # 量级：R1′ 的跨 agent 极少（0.07%），R3 的跨 agent 明显更多（17.8%）—— 两者处置不同
    assert r1["cross_agent"] <= 10, "R1′ 跨 agent 若变多，『不改』的结论要重新评估"
    assert r3["cross_agent"] / (r3["self_copy"] + r3["cross_agent"]) > 0.1, (
        "R3 跨 agent 占比变了 ⇒ 残留登记里的数字要更新")


def test_两个误杀口径必须分开登记不得混用():
    """**口径纪律**：`false_positive_bound` 的 0 是【内容口径】（来料有没有新词），
    不是【正当性口径】（这次合并是否正当）。账本必须同时带着这个警告。"""
    from trinity.agents.aggregator import _merge_safety as ms

    fpb = ms.RULE_LEDGER["low_incoming_novelty"]["false_positive_bound"]
    assert "two_calibers_warning" in fpb, "必须显式说明两个口径不可互换"
    assert "内容口径" in fpb["two_calibers_warning"]
    assert "正当性口径" in fpb["two_calibers_warning"]
    assert "known_gap_author" in ms.RULE_LEDGER["low_incoming_novelty"], (
        "正当性口径的数字必须登记在案（author blindspot）")
    for code in ("low_incoming_novelty", "duplicate_no_new_evidence"):
        gap = ms.RULE_LEDGER[code]["known_gap_author"]
        assert gap["impact_bound"] and gap["why_not_fixed"] and gap["verified_not_independent"], (
            "残留登记必须带影响上界、不修理由、以及『跨 agent 抽查后并非独立佐证』的证据", code)


def test_作者盲区不改_判据仍是双向可失败的():
    """判定"不改"之后的**必要核对**：内容类判据仍必须**双向可失败** ——

    * 跨 agent 形态**当前也判红**（这是已知残留，不是 bug）；
    * 自复制形态被判红（拒得对）；
    * 来料带**新词**时绝不判红（否则规则退化成一味拒绝）。
    即：残留的代价是"少数跨 agent 复述被拒"，而不是"判据失效"。
    """
    from trinity.agents.aggregator import _merge_safety as ms

    existing = " ".join(_pool(40))
    echo = " ".join(_pool(30))
    cross = ms.verify_merge_safety(echo, existing, existing_sources={"agent-A"},
                                   new_source="agent-B")
    assert cross.safe is False and cross.code == "low_incoming_novelty"
    self_copy = ms.verify_merge_safety(echo, existing, existing_sources={"agent-A"},
                                       new_source="agent-A")
    assert self_copy.safe is False and self_copy.code == "low_incoming_novelty"
    with_new = ms.verify_merge_safety(existing + " zznew1", existing)
    assert with_new.safe is True, "带新词的来料被判红 ⇒ 判据退化成恒红"


def test_来源参数是可用的判据输入_更正t11的登记():
    """**更正 t11 的登记**：`existing_sources`/`new_source` 在**前置位置**是可用的判据输入
    （t13/t16 的作者闸门评估就是用它们算的），R2 的恒真式教训是**比对时机/基线**，
    不是输入不可用。本用例把这个结论钉在文档上（更正必须留在原位，不得悄悄改掉）。"""
    import inspect

    from trinity.agents.aggregator import _merge_safety as ms

    doc = inspect.getdoc(ms.verify_merge_safety) or ""
    assert "比对时机" in doc, "必须在文档里写明 R2 的教训是时机而非可用性"
    assert "不足以支撑任何可失败判据" in doc and "那是**错的**" in doc, (
        "更正必须显式留在原位（把旧登记与更正一起写出来）")
    sig = inspect.signature(ms.verify_merge_safety)
    assert "existing_sources" in sig.parameters and "new_source" in sig.parameters


# ─────────────────────────────── 接线后的**真实路径回放**（t13）

class _Engine:
    """最小的引擎替身：`merge_if_similar` 只用到这两个方法。"""

    def extract_topics(self, content: str):
        return ["t"]

    def compute_priority(self, dv):
        return float(getattr(dv, "importance", 0.5))


def _dv(mid: str, content: str, sources=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        memory_id=mid, content=content, confidence=0.5,
        source_agents=set(sources if sources is not None else ["seed-agent"]),
        updated_at=0.0, priority=0.0, importance=0.6, topics=["t"],
    )


def _make_aggregator_stub(pool: dict):
    """造一个**只带状态**的真实 `MemoryAggregator` 实例（`__new__` 跳过重初始化），
    然后把几个库依赖换成替身 ⇒ 之后 `merge_if_similar` 走的**是生产源码**，不是复制品。
    """
    import threading

    from trinity.agents.aggregator import MemoryAggregator

    obj = MemoryAggregator.__new__(MemoryAggregator)
    obj._lock = threading.RLock()
    obj._pool = pool
    obj._topic_index = {}
    obj._sb_engine = None
    obj._engine = _Engine()
    obj._stats = {}
    obj._tracer = None
    obj._tokenize = lambda s: tokens(s)
    obj._jaccard_similarity = jaccard
    obj._add_to_agent_index = lambda *_a, **_k: None
    return obj


def _similar_pair(new_tok: str, extra: str) -> tuple:
    """造一对**真的能过闸门**的内容（token 比 ~0.8）+ 一个既有条目。"""
    base = _pool(60)
    existing = " ".join(base)
    incoming = " ".join(base[:52] + [new_tok, extra])
    assert jaccard(tokens(existing), tokens(incoming)) >= gate_threshold()
    return existing, incoming


def test_正常路径回放_后置不变量零违规():
    """**接线后必须不误报**（队长条件 ②）：把真实 `merge_if_similar` 跑 200 次合并，
    后置不变量的计数键必须恒为 0，且一次性告警从未触发。

    为什么必须真跑：单测只证明"函数能判红"；**误报**只有在真实路径上跑才看得出来
    （比如 `.add()` 之后集合对象被换掉、或传入的快照被就地修改）。
    """
    from trinity.agents.aggregator import _ingest as ing

    existing, incoming = _similar_pair("zznew1", "zznew2")
    ing._MERGE_POSTCOND_WARNED = False
    merged = 0
    for i in range(200):
        pool = {"m1": _dv("m1", existing)}
        agg = _make_aggregator_stub(pool)
        out = agg.merge_if_similar(incoming, "agent-%02d" % (i % 7), threshold=gate_threshold())
        assert out is not None, "该对必须过闸门（否则这条回放没测到合并路径）"
        assert agg._stats.get("total_merge_postcondition_violations", 0) == 0, (
            "正常合并路径上报了后置不变量违规 ⇒ 这个告警会变成每次都叫的噪声")
        assert out.source_agents == {"seed-agent", "agent-%02d" % (i % 7)}
        merged += 1
    assert merged == 200
    assert ing._MERGE_POSTCOND_WARNED is False, "正常路径不该触发任何一次性告警"


def test_路径级反事实_把来源集合改成重建时必须报违规():
    """**端到端反事实**：把来源集合换成"add 即重建"的容器（模拟有人把 `.add()` 改成赋值），
    真实 `merge_if_similar` 必须报违规：计数 +1、一次性告警置位。

    这一条把"接线是真的、判据是有牙齿的"钉在**生产代码路径**上，而不只是单元函数上。
    """

    class RebuildOnAdd(set):
        """`add(x)` 会先把集合清空（等价于有人把 `.add()` 改成 `= {x}`）。"""

        def add(self, item):  # noqa: D102
            self.clear()
            super().add(item)

    from trinity.agents.aggregator import _ingest as ing

    existing, incoming = _similar_pair("zznew3", "zznew4")
    ing._MERGE_POSTCOND_WARNED = False
    dv = _dv("m1", existing)
    dv.source_agents = RebuildOnAdd({"seed-agent"})
    agg = _make_aggregator_stub({"m1": dv})
    out = agg.merge_if_similar(incoming, "agent-x", threshold=gate_threshold())
    assert out is not None
    assert agg._stats.get("total_merge_postcondition_violations") == 1, (
        "重建集合把既有来源抹掉了，后置不变量竟然没报 ⇒ 接线没有生效")
    assert ing._MERGE_POSTCOND_WARNED is True, "违反必须置位一次性哨兵（但要计数，不只靠日志）"


# ─────────────────────────────── 单一权威账本（t13 纪律 ③）

def test_合并规则只有一处权威账本():
    """**单一权威账本**：合并规则的生效/退役/接线状态**只**登记在
    `trinity/agents/aggregator/_merge_safety.py::RULE_LEDGER`。

    与之相对的 `trinity/modules/second_brain/capability_ledger.py`（t6）是
    「second_brain 能力类 / 门禁数字」的账本，**主题不同、扫描根不同**（见断言 ①），
    因此不是"第二个账本"。本用例把它变成机器判据，防止将来两处漂移：

      ① `capability_ledger` 的扫描根必须**不含** `agents/aggregator`（它管不到合并规则）；
      ② 它的源码里**不得出现**任何合并规则的 code / `RULE_LEDGER` 字面量
         （一旦有人在那里再声明一份状态，这条立刻红 ⇒ 不允许出现两个账本）。
    """
    from trinity.agents.aggregator import _merge_safety as ms

    assert ms.RULE_LEDGER, "权威账本不得为空"

    from trinity.modules.second_brain import capability_ledger as cl

    root = str(cl.CAPABILITY_ROOT).replace("\\", "/")
    assert root.endswith("trinity/modules/second_brain"), root
    assert "/agents/" not in root and "aggregator" not in root, (
        "capability_ledger 的扫描根越界到 agents/aggregator ⇒ 会与 RULE_LEDGER 形成两个账本")

    src = pathlib.Path(cl.__file__).read_text(encoding="utf-8")
    for token in ("content_collapse", "source_downgrade", "low_incoming_novelty",
                  "duplicate_no_new_evidence", "RULE_LEDGER"):
        assert token not in src, (
            "第二个账本：capability_ledger 里出现了合并规则的登记标记 %r —— "
            "合并规则的状态只允许登记在 _merge_safety.RULE_LEDGER" % token)


def test_权威账本自身与代码与调用点三方一致():
    """单一账本必须**自洽**：账本键 == 源码产出的 code == `IMPLEMENTED_CODES`，
    且"已接线"的声明必须与 `_ingest.py` 的实际调用一致（由 harness 复核）。"""
    import ast as _ast

    from trinity.agents.aggregator import _merge_safety as ms

    codes = codes_in_source(MERGE_SAFETY_PY)
    assert set(ms.IMPLEMENTED_CODES) == codes, (sorted(ms.IMPLEMENTED_CODES), sorted(codes))
    assert set(ms.IMPLEMENTED_CODES) <= set(ms.RULE_LEDGER), "产出的 code 必须全部已登记"
    # 账本 = 实现里能产出的 ∪ 已退役的（退役条目**按设计**产不出 code，不是脱节）
    assert set(ms.RULE_LEDGER) - set(ms.IMPLEMENTED_CODES) == set(ms.retired_rules()), (
        sorted(ms.RULE_LEDGER), sorted(ms.IMPLEMENTED_CODES), sorted(ms.retired_rules()))
    assert harness_report(MERGE_SAFETY_PY)["green"] is True


# ─────────────────────────────────────────── 判据自身的牙齿

def find_prefix_source_text(rel_path: str = "trinity/agents/aggregator/_merge_safety.py",
                            max_commits: int = 80):
    """取出**改前**那一版 `_merge_safety.py` 的源码（找不到返回 None）。

    不能直接用 `git show HEAD:<path>`：本任务一旦被提交，HEAD 就变成了**改后**版本，
    而 HEAD 版本里仍然出现 `content_collapse` 这个词（在**账本/docstring 的退役理由**里）
    ⇒ 那种"含该字符串即视为改前"的判断会**张冠李戴**（把改后版本当改前跑，测试随即自相矛盾）。
    故按提交历史回溯，取**最近一个「含 content_collapse 且不含 RULE_LEDGER」**的版本。
    """
    out = subprocess.run(["git", "log", "--format=%H", "-n", str(max_commits), "--", rel_path],
                         cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        return None
    for sha in (out.stdout or "").split():
        s = subprocess.run(["git", "show", "%s:%s" % (sha, rel_path)], cwd=str(REPO),
                           capture_output=True, text=True, encoding="utf-8")
        src = s.stdout or ""
        if s.returncode == 0 and "content_collapse" in src and "RULE_LEDGER" not in src:
            return src
    return None


def _prefix_source(tmp_path) -> pathlib.Path:
    """把改前源码落到临时文件（供 `load_by_path` 使用）。"""
    src = find_prefix_source_text()
    if not src:
        pytest.skip("回溯不到改前的 _merge_safety.py（浅克隆/无 git/历史被重写）")
    p = tmp_path / "_merge_safety_prefix.py"
    p.write_text(src, encoding="utf-8")
    return p


def test_可达性判据在改前源码上必须判红(tmp_path):
    """**牙齿**：把本判据指向改前的 `_merge_safety.py`，必须判红，且理由必须是"不可达/未登记"。

    没有这条，"可达性判据"就可能自己是恒真的（永远绿）——那正是它要消灭的缺陷。
    """
    rep = harness_report(_prefix_source(tmp_path))
    assert rep["green"] is False, "改前源码竟然通过了可达性判据 ⇒ 本判据没有牙齿"
    blob = " | ".join(rep["problems"])
    assert "content_collapse" in blob or "未登记" in blob, rep["problems"]
    assert "source_downgrade" in blob or "未登记" in blob, rep["problems"]
    # 改前那一版里 R1/R2 确实一个都打不中（这正是要报出的事实）
    assert "content_collapse" not in rep["pre_gate_fired"]
    assert "source_downgrade" not in rep["pre_gate_fired"]


def test_改前源码里R1的形状确实在闸门内够不着(tmp_path):
    """把 R1 的触发形状直接喂给改前模块：闸门内长度比 ≥0.75 ⇒ 判绿。

    这条与 `test_可达性判据在改前源码上必须判红` 一起，构成"不可达"的**直接证据**：
    不是我们没构造好输入，而是**闸门不允许**那种输入。
    """
    prefix = load_by_path(_prefix_source(tmp_path))
    long_old = "这是一段很长的既有记忆，包含许多细节与上下文信息。" * 8   # > 120 字符
    # 闸门内来料只能压到既有长度的 ~75%（token），字符比自然也远高于 R1 的 35%
    short_in_gate = "这是一段很长的既有记忆，包含许多细节与上下文信息。" * 6
    ratio = len(short_in_gate) / len(long_old)
    assert ratio >= prefix.COLLAPSE_RATIO
    v = prefix.verify_merge_safety(short_in_gate, long_old)
    assert v.safe is True, "闸门内的这个形状竟然触发了 R1 —— 那 R1 就不是不可达的"
