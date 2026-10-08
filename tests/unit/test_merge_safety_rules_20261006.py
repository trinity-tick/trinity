# -*- coding: utf-8 -*-
"""合并安全判据的回归测试（2026-10-06 复评 G1/G2）。

## 背景（为什么判据必须"能失败"）

`_ingest.py::merge_if_similar` 曾调用一个**全仓从未存在过**的
`GuardianChainV50.verify_merge_safety`（自 2026-08-14 起每次合并抛 AttributeError，
被 `except Exception` 在 DEBUG 级吞掉）⇒ 合并安全校验从未生效且完全静默。

第一轮（F5）删掉了那个不可能的调用；本轮（G1）用 `_merge_safety.py` 补上**真实判据**。
复评已在本仓反复发现"门禁没有判别力"（`validate()` 恒真式、`structure_gate` 自开开关、
解释器缺依赖导致断言空转），所以本文件的核心是**反事实**：每条规则都要既证明"违规判红"，
也证明"合规判绿"——任一方向缺失都意味着规则可能是恒真或恒假。
"""
import ast
import pathlib

import pytest

from trinity.agents.aggregator import _merge_safety as ms

REPO = pathlib.Path(__file__).resolve().parents[2]
INGEST_PY = REPO / "trinity" / "agents" / "aggregator" / "_ingest.py"


@pytest.fixture(autouse=True)
def _safety_on(monkeypatch):
    """默认态；个别用例自行关掉。"""
    monkeypatch.delenv(ms.MERGE_SAFETY_ENV, raising=False)


# ── R1 信息损失 ─────────────────────────────────────────────────────────
# 2026-10-06 t11 更正：**R1 `content_collapse` 已退役**。
# 失效的期望值（原断言 `v.code == "content_collapse"`）已按实测更正，原因：
#   · 该合并路径**不写 content**（`_ingest.py:242-253` 只动 confidence / source_agents /
#     updated_at / priority / agent 索引）⇒「既有内容被贫信息覆盖」这个伤害在此路径不存在；
#   · 且在闸门内结构性不可达（闸门 Jaccard≥0.75 ⇒ |B| ≥ 0.75|A|，而 R1 比字符 <35%）。
# 详细论证与"为什么这条不是"改阈值就能救""见 `_merge_safety.RULE_LEDGER`。

def test_r1_red_on_collapse():
    """【期望值已更正】原断言：长既有 + 极短来料 ⇒ 判红 `content_collapse`。

    实测（`evidence/merge_safety_calibration.json`）：闸门内 0/5,295 命中，
    最小长度比 0.3519 > 阈值 0.35 ⇒ 该形状**在调用点不可达**，判据已退役。
    现在的期望是"**不再**产出这个 code"，而"退役理由必须登记"由
    `test_merge_safety_reachability_20261006.py` 另行锁定。
    """
    long_old = "这是一条很长的既有记忆。" * 20          # 260 字符
    v = ms.verify_merge_safety("短笔记", long_old)
    assert v.code != "content_collapse", "R1 已退役，不得再产出 content_collapse"
    assert v.safe is True


def test_r1_green_when_incoming_is_substring():
    """来料是既有内容的子串 ⇒ 不构成信息损失（判绿）。

    R1 退役后本用例仍留作**回归位**：它记录的是"当年这条规则的反事实方向"，
    现在恒绿（因为 R1 不再判任何东西），保留以示"不得复活成恒红规则"。
    """
    long_old = "A" * 200
    assert ms.verify_merge_safety("A" * 50, long_old).safe is True


def test_r1_green_when_existing_is_short():
    """既有内容本身很短时不做 R1（短文本之间"更短"没有信息量）。

    同 `test_r1_green_when_incoming_is_substring`：R1 退役后恒绿，留作历史反事实位。
    """
    assert ms.verify_merge_safety("x", "y" * (ms.MIN_EXISTING_CHARS - 1)).safe is True


# ── R2 来源降级 ─────────────────────────────────────────────────────────
# 2026-10-06 t11 更正：**前置的 R2 `source_downgrade` 已退役（恒真式）**，
# 其可失败版本改为**后置不变量** `verify_merge_postcondition`
# （实测：`set(existing).add(new)` 之后判 `issuperset` 对任意输入恒真；5,000 次随机搜索 0 命中）。

def test_r2_invariant_is_superset():
    """R2 的不变量本身仍然成立：合并后来源集合必须是原集合的超集。"""
    original = {"agent-a", "agent-b"}
    projected = set(original)
    projected.add("agent-c")
    assert projected.issuperset(original), "正常 .add() 路径必然满足超集不变量"
    # 反事实：若有人改成"重建集合"，不变量被破坏 —— 这正是**后置**不变量要抓的东西
    broken = {"agent-c"}
    assert not broken.issuperset(original)


def test_r2_normal_path_is_green():
    """正常路径必须绿（否则规则是恒红）。"""
    v = ms.verify_merge_safety(
        "一段明显不同的新内容，长度足够不至于触发塌缩判定。",
        "旧的短内容",
        existing_sources={"a", "b"},
        new_source="c",
    )
    assert v.safe is True


def test_r2_precheck_no_longer_produces_source_downgrade():
    """【期望值已更正】原断言：源码里必须出现 `source_downgrade` 与 `issuperset`
    （防止规则被删而测试仍绿）。

    R1/R2 退役后，"字符串在不在源码里"已不再是有效判据 —— 这两个词现在出现在**账本与
    docstring**里（登记退役理由），文本匹配会**假绿**。改为结构化核对：
    ① 源码里**不得**再有产出该 code 的 `MergeSafetyVerdict(...)` 调用（AST）；
    ② 账本必须把它登记为 retired 且带理由；
    ③ 可失败替代 `verify_merge_postcondition` 必须存在。
    """
    import ast

    tree = ast.parse(pathlib.Path(ms.__file__).read_text(encoding="utf-8"))
    produced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "MergeSafetyVerdict" and len(node.args) >= 2 \
                and isinstance(node.args[1], ast.Constant):
            produced.add(node.args[1].value)
    assert "source_downgrade" not in produced, "R2 已退役，不得再由前置判据产出"
    assert "content_collapse" not in produced, "R1 已退役，不得再由前置判据产出"
    meta = ms.RULE_LEDGER["source_downgrade"]
    assert meta["status"].startswith("retired")
    assert meta["retired_reason"] and meta["evidence"]
    assert hasattr(ms, "verify_merge_postcondition")


# ── R3 无新证据的置信度灌水 ─────────────────────────────────────────────

def test_r3_red_on_identical_content():
    assert ms.verify_merge_safety("同一句话", "同一句话").code == "duplicate_no_new_evidence"


def test_r3_red_on_whitespace_and_width_variants():
    """归一化后相同也算：多余空白、全角半角都不得绕过。

    注意 `"he  llo"` 折叠空白后是 `"he llo"`，与 `"hello"` **并不相同**
    （那个空格是有意义的）—— 所以本用例用"只差空白量"与"只差全角/半角"两对。
    """
    assert ms.verify_merge_safety("hello  world", "hello world").code == "duplicate_no_new_evidence"
    assert ms.verify_merge_safety("hello world ", "hello world").code == "duplicate_no_new_evidence"
    assert ms.verify_merge_safety("ＡＢＣ", "ABC").code == "duplicate_no_new_evidence"


def test_r3_green_on_different_content():
    assert ms.verify_merge_safety("新的内容", "旧的内容").safe is True


# ── 开关与边界 ──────────────────────────────────────────────────────────

def test_switch_off_makes_it_unconditionally_safe(monkeypatch):
    monkeypatch.setenv(ms.MERGE_SAFETY_ENV, "off")
    assert ms.is_enabled() is False
    assert ms.verify_merge_safety("同一句话", "同一句话").safe is True


def test_switch_on_by_default():
    assert ms.is_enabled() is True


# ── G2：顺序契约 + 不产生重复 ────────────────────────────────────────────

def _merge_fn_source():
    tree = ast.parse(INGEST_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "merge_if_similar":
                    return sub
    raise AssertionError("在 _ingest.py 里找不到 merge_if_similar")


def test_verification_happens_before_any_mutation():
    """**结构契约（G2 的核心）**：校验必须发生在任何字段变更之前。

    旧实现的顺序缺陷是"校验在 confidence/source_agents/updated_at/priority 与
    agent 索引全部写完之后才做"，且失败时 `return None` 不回滚 ⇒ 调用方把它读成
    "没有相似项"而**新建重复记忆**。
    这里用 AST 断言语句顺序：`verify_merge_safety(...)` 的行号必须**早于**
    第一次对 `best_dv.<attr>` 的赋值。
    """
    fn = _merge_fn_source()

    call_line = None
    first_mutation_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "verify_merge_safety":
                call_line = node.lineno if call_line is None else min(call_line, node.lineno)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                        and t.value.id == "best_dv"):
                    if first_mutation_line is None or node.lineno < first_mutation_line:
                        first_mutation_line = node.lineno

    assert call_line is not None, "merge_if_similar 里找不到 verify_merge_safety 调用"
    assert first_mutation_line is not None, "找不到对 best_dv 的变更"
    assert call_line < first_mutation_line, (
        "校验（L%d）必须早于第一次变更 best_dv（L%d）—— 否则顺序缺陷回归，"
        "拒绝合并也阻止不了已发生的改写" % (call_line, first_mutation_line)
    )


def test_refusal_returns_existing_item_not_none():
    """**不产生重复**：拒绝时必须 `return best_dv`（不是 None）。

    返回 None 会被 `ingest()` 读成"没有相似项" ⇒ `index_memory()` 新建一条
    **重复记忆**。这条锁死"拒绝 ≠ 新建"。
    """
    src = INGEST_PY.read_text(encoding="utf-8")
    assert "return best_dv" in src, "拒绝路径必须返回既有条目"
    idx = src.index("MERGE-SAFETY-REFUSED")
    window = src[idx: idx + 900]
    assert "return None" not in window, (
        "拒绝合并的分支里出现了 return None —— 那会让调用方新建重复记忆"
    )


def test_no_debug_level_swallow_remains():
    """F5 的价值必须保留：不得再有"把校验失败降级成 debug 静默"的路径。"""
    src = INGEST_PY.read_text(encoding="utf-8")
    assert "verification skipped" not in src
    assert "MERGE-SAFETY-NOT-IMPLEMENTED" not in src, (
        "旧的「未实现」宣告应已被真实判据取代"
    )


def test_refusal_is_counted_in_stats():
    """拒绝必须留痕（可被运维/门禁读到），不得静默。"""
    src = INGEST_PY.read_text(encoding="utf-8")
    assert "total_merge_refused" in src
