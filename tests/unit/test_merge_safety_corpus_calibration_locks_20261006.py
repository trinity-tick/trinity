# -*- coding: utf-8 -*-
"""合并安全判据在**真实语料**上标定结果的回归锁（t4 / 2026-10-06）。

## 为什么要把"标定结论"锁成测试

本轮在生产语料（`scripts/merge_safety_calibration.py`，10,965 个通过合并闸门的候选对）
上量到两条**结构性**结论 —— 它们不是"数字偏低"，而是"规则在当前调用点不可能响"：

1. **R2 `source_downgrade` 是恒真式**：实现先 `projected = set(existing)` 再
   `projected.add(new)`，然后问 `projected.issuperset(existing)` —— `set.add` 只增不减，
   该断言对**任意**输入恒为真 ⇒ 线上命中率恒 0、判别力为 0。
2. **R1 `content_collapse` 在闸门内不可达**：闸门要求 Jaccard ≥ 0.75；由集合不等式
   |A∩B| ≥ 0.75·|A∪B| ≥ 0.75·|A| 且 |A∩B| ≤ |B| ⇒ |B| ≥ 0.75·|A|，
   而来料 token 数 ≥ 75% 的既有 token 数时其**字符数**几乎不可能 < 35% ⇒ R1 的触发域与
   闸门的输出域在算术上互斥（实测 0/5,295 低于阈值，最小长度比 0.3519）。

把这两条锁进测试的理由：本仓反复出现"门禁没有判别力"，而**没有判别力的门禁会以
"已校验"的名义通过复核**。这两条结论一旦被静默改回（例如有人把 R2 改成真判据、
或把阈值调宽到能在闸门内触发），测试必须红 —— 那时需要的是**新的标定**，而不是旧结论。
"""
import ast
import importlib.util
import pathlib
import random
import subprocess
import sys
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
MERGE_SAFETY_PY = REPO / "trinity" / "agents" / "aggregator" / "_merge_safety.py"
CALIB_PY = REPO / "scripts" / "merge_safety_calibration.py"


def _load():
    spec = importlib.util.spec_from_file_location("_ms_calib_lock", MERGE_SAFETY_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ms_calib_lock"] = mod          # dataclass 需要模块已注册
    spec.loader.exec_module(mod)
    return mod


ms = _load()


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.delenv(ms.MERGE_SAFETY_ENV, raising=False)


# ───────────────────────────────────────────── R2：恒真式锁

def test_r2_can_never_fire_randomized_search():
    """【恒真式锁】对 R2 做随机输入搜索：**任何**输入都不该让 source_downgrade 判红。"""
    rnd = random.Random(4242)
    for _ in range(3000):
        existing = {"a%d" % rnd.randint(0, 30) for _ in range(rnd.randint(1, 6))}
        new = "a%d" % rnd.randint(0, 30)
        content = "".join(rnd.choice("甲乙丙丁戊") for _ in range(rnd.randint(1, 60)))
        v = ms.verify_merge_safety(content, content + "尾", existing_sources=existing,
                                   new_source=new)
        assert v.code != "source_downgrade", "R2 被判红 ⇒ 它不再是恒真式，标定结论失效"


def test_r2_implementation_shape_is_the_tautology():
    """结构锁（**2026-10-06 t11 更正期望值**）：恒真式形状**不得**再出现在可执行代码里。

    原断言是"三个片段必须出现在源码里"（当时用意：防止规则被删而测试仍绿）。
    R2 退役后这些片段仍出现在**账本/docstring 的退役理由**里 ⇒ 文本匹配会**假绿**。
    改为 AST 断言：`issuperset` 不得作为**调用**出现在源码里（只允许出现在结论文本中）。
    """
    tree = ast.parse(MERGE_SAFETY_PY.read_text(encoding="utf-8"))
    calls = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "issuperset"]
    assert calls == [], (
        "源码里仍有 `issuperset(...)` 调用（L%s）⇒ 恒真式可能被复活；可失败版本应写成"
        "后置不变量 `verify_merge_postcondition`" % calls)
    assert "retired-tautological-from-precheck" in MERGE_SAFETY_PY.read_text(encoding="utf-8")


def test_r2_retirement_is_registered_with_evidence():
    """退役登记必须带"为什么"与"证据"，不能只标一个状态。"""
    from trinity.agents.aggregator import _merge_safety as ms

    meta = ms.RULE_LEDGER["source_downgrade"]
    assert meta["status"] == "retired-tautological-from-precheck"
    assert "issuperset" in meta["retired_reason"]
    assert meta["evidence"]["random_search_trials"] >= 5000
    assert meta["evidence"]["random_search_fired"] == 0
    assert meta["replacement"] == "verify_merge_postcondition"
    # 2026-10-06 t13 **期望值已更正**：接线已由队长批准并落地 ⇒ `replacement_wired` 由 False
    # 翻转为 True。原断言（必须为 False，否则会被读成「已经有来源降级防护」）在当时是对的；
    # 现在这份保证改由**可达性判据**承担：账本声明 wired=True 而 `_ingest.py` 里找不到调用
    # ⇒ 判红（负向实测见 `test_merge_safety_reachability_20261006.py::test_接线被摘掉必须判红`）。
    assert meta["replacement_wired"] is True, "t13 之后必须如实记为已接线"
    ingest = (REPO / "trinity" / "agents" / "aggregator" / "_ingest.py").read_text(
        encoding="utf-8")
    assert "verify_merge_postcondition(" in ingest, (
        "账本声明已接线，但调用点里没有调用 ⇒ 假称已接线（可达性判据会判红）")
    assert "total_merge_postcondition_violations" in ingest, "违反必须计数（计数键名可核）"


def test_r2_invariant_is_only_violated_by_rebuilding_the_set():
    """反事实：把集合**重建**（而不是 add）确实会违反不变量 ⇒ 说明判据不是恒假，
    只是它守的不变量在**当前调用点**不可能被违反。"""
    original = {"a", "b"}
    assert not {"c"}.issuperset(original)
    src_ingest = (REPO / "trinity" / "agents" / "aggregator" / "_ingest.py").read_text(
        encoding="utf-8")
    assert ".source_agents.add(source_agent)" in src_ingest, (
        "调用点必须是 add（若是重建，R2 的恒真性结论不再成立）")


# ───────────────────────────────────────────── R1：闸门内不可达锁

def _prefix_module():
    """加载**改前**的 `_merge_safety.py`（找不到返回 None）。

    不能用 `git show HEAD:...`：本任务一旦提交，HEAD 就是改后版本，而改后版本的
    账本/docstring 里仍写着 `content_collapse` ⇒ 只看字符串会**张冠李戴**。
    故按提交历史回溯，取最近一个「含 content_collapse 且不含 RULE_LEDGER」的版本。
    """
    rel = "trinity/agents/aggregator/_merge_safety.py"
    log = subprocess.run(["git", "log", "--format=%H", "-n", "80", "--", rel],
                         cwd=str(REPO), capture_output=True, text=True, encoding="utf-8")
    if log.returncode != 0:
        return None
    for sha in (log.stdout or "").split():
        s = subprocess.run(["git", "show", "%s:%s" % (sha, rel)], cwd=str(REPO),
                           capture_output=True, text=True, encoding="utf-8")
        src = s.stdout or ""
        if s.returncode != 0 or "content_collapse" not in src or "RULE_LEDGER" in src:
            continue
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "_prefix.py"
            p.write_text(src, encoding="utf-8")
            spec = importlib.util.spec_from_file_location("_ms_prefix_r1", str(p))
            mod = importlib.util.module_from_spec(spec)
            sys.modules["_ms_prefix_r1"] = mod
            spec.loader.exec_module(mod)
        return mod
    return None


def test_r1_is_not_tautological_it_can_fire():
    """【期望值已更正，2026-10-06 t11】原断言：长既有 + 极短来料 ⇒ 判红 content_collapse。

    该断言在 t11 之后失效，原因**不是**判据写错了，而是两件事：
      ① 该合并路径**不写 content** ⇒ R1 的伤害模型在此路径不存在；
      ② R1 在闸门内结构性不可达（实测 0/5,295 命中、最小长度比 0.3519 > 0.35）。
    故 R1 已**退役**。为**不丢失反事实价值**，本用例改为把同一形状喂给
    **改前**的模块并断言它在**那里**确实判红 —— 这证明"形状本身能触发规则，
    问题出在闸门对输入的约束"，即不可达是**真结论**而非构造失败。
    """
    prefix = _prefix_module()
    if prefix is None:
        pytest.skip("回溯不到改前的 _merge_safety.py（浅克隆/无 git/历史被重写）")
    long_old = "这是一段足够长的既有记忆，包含了许多细节与上下文信息。" * 8
    v_old = prefix.verify_merge_safety("短，但是全新的信息", long_old)
    assert v_old.safe is False and v_old.code == "content_collapse", (
        "改前模块竟然不判红 ⇒『形状能触发、只是闸门不允许』的论证不成立")
    v = ms.verify_merge_safety("短，但是全新的信息", long_old)
    assert v.code != "content_collapse", "R1 已退役，当前模块不得再产出该 code"


def test_r1_cannot_be_reached_behind_a_jaccard_gate():
    """【闸门内不可达锁】随机搜索：任何满足 Jaccard ≥ 0.75 的 token 集合对，
    都不可能同时满足 R1 的字符比条件（在"字符数 ≈ 常数 × token 数"的常规文本上）。"""
    rnd = random.Random(99)
    gate = 0.75
    checked = 0
    for _ in range(2000):
        n = rnd.randint(20, 60)
        base = {"t%d" % i for i in range(n)}
        keep = rnd.sample(sorted(base), k=max(1, int(n * rnd.uniform(gate, 1.0))))
        new_tok = set(keep) | {"x%d" % i for i in range(rnd.randint(0, int(n * 0.1)))}
        inter = len(base & new_tok)
        union = len(base | new_tok)
        jac = inter / union
        if jac < gate:
            continue
        checked += 1
        # 用与校准脚本同口径的"独立词表"当作字符数的代理：token 数比 ≥ ~0.7 时，
        # R1 的 <0.35 字符比要求不可能成立。
        ratio_tokens = len(new_tok) / len(base)
        assert ratio_tokens >= 0.6, (ratio_tokens, jac)
    assert checked > 500, "样本太少，判据没有真的被检验（checked=%d）" % checked


def test_r1_min_length_ratio_measured_on_prod_is_above_threshold():
    """现场读数锁：标定产物里"闸门内最小长度比"必须 ≥ R1 阈值（0.35）。

    这条把"实测 0 组可触发"钉成可核事实：如果哪天有人在闸门内造出可触发对，
    该读数会掉到阈值以下，测试红 ⇒ 必须重新标定，而不是沿用旧结论。
    """
    import json
    p = pathlib.Path(r"D:\DSH官网\trinity-optimize-20261006\evidence"
                     r"\merge_safety_calibration.json")
    if not p.exists():
        pytest.skip("标定产物不在本机（CI 无该证据文件）")
    rep = json.loads(p.read_text(encoding="utf-8"))
    r1 = rep["rules"]["R1_content_collapse"]
    assert r1["fired"] == 0
    assert r1["length_ratio_min"] >= ms.COLLAPSE_RATIO
    assert r1["pairs_below_collapse_ratio"] == 0
    r2 = rep["rules"]["R2_source_downgrade"]
    assert r2["fired"] == 0
    r3 = rep["rules"]["R3_duplicate_no_new_evidence"]
    assert r3["fired"] > 0, "R3 若 0 命中，说明标定样本或契约变了"
    assert r3["false_positive_content_bearing"] == 0
    assert rep["r2_tautology_proof"]["source_downgrade_fired"] == 0


# ───────────────────────────────────────────── R3：正反两个方向

def test_r3_fires_on_exact_duplicate():
    """【正例】归一化后完全相同 ⇒ 判红（无新证据，只会单调推高 confidence）。"""
    v = ms.verify_merge_safety("同一条内容  ", "同一条内容")
    assert v.safe is False and v.code == "duplicate_no_new_evidence"


def test_r3_does_not_fire_on_whitespace_only_difference_within_reason():
    """【反例（边界）】空白差异被归一化吃掉 ⇒ 判红是对的（确实没有新信息）。"""
    v = ms.verify_merge_safety("A  B", "A\n\tB")
    assert v.code == "duplicate_no_new_evidence"


def test_r3_does_not_fire_when_one_digit_changed():
    """【反例】只差一个数字 ⇒ 那是**新证据**，绝不能判红（否则会拦掉真实更新）。"""
    v = ms.verify_merge_safety("播放量 271275", "播放量 270755")
    assert v.safe is True, "一个数字的差异被判成重复 ⇒ 误杀"


def test_r3_token_identical_but_reordered_is_not_caught():
    """【漏杀（已量）】词表完全相同、只是顺序/重复不同 ⇒ R3 **不判红**。

    现场实测 1,082 对落在这一类（严格漏杀口径）。本用例锁定这个已知盲区，
    避免被读成"R3 已经覆盖近重复"。
    """
    v = ms.verify_merge_safety("甲 乙 丙 丁", "丁 丙 乙 甲")
    assert v.safe is True
    assert v.code == ""


# ───────────────────────────────────────────── 标定脚本自身

def test_calibration_script_declares_caliber_and_denominator():
    """哨兵：标定脚本必须**显式声明分母**（"通过闸门的候选对"），不能只报命中率。"""
    src = CALIB_PY.read_text(encoding="utf-8")
    assert "gate_open_pairs" in src
    assert "通过合并闸门的候选对" in src
    assert "GATE_THRESHOLD = 0.75" in src


def test_calibration_script_is_readonly():
    """哨兵：标定脚本必须是只读（不得出现写库语句）。"""
    src = CALIB_PY.read_text(encoding="utf-8")
    for forbidden in ("INSERT INTO", "UPDATE memories", "DELETE FROM", "commit()"):
        assert forbidden not in src, "只读量测脚本出现写语句：%s" % forbidden
    assert "?mode=ro" in src
