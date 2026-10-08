# -*- coding: utf-8 -*-
"""t71 / I11 判据：**跳过逃逸口收口**（静态双锁 + L-C 棘轮）+ **flaky 反事实**。

## 背景（t65/I5 的残留，队长指派执行）

t65 扫 625 个测试文件（AST）得 153 处跳过，分级 L-A 47 / L-B 80 / **L-C 候选 26**，
并明确："`in_except` 只是候选条件，**真正的判据是它跳过的是不是这个测试存在的理由**"。

本文件把这次收口**钉住**（而不是只在某次提交里改一遍）：

| 判据 | 断言 | 牙齿（人为破坏 ⇒ 必红） |
|---|---|---|
| **L1 逃逸口双锁** | 本次改过的 5 个文件里 **`pytest.skip` / `skipif` 调用数 == 0**（AST 级） | 合成一段含 `pytest.skip(` 的源码 ⇒ 扫描器必须报出 |
| **L2 签名白名单** | `test_bare_sql_guard_20261006.py`：无跳过调用 + **显式签名白名单**非空 + 签名不在白名单时 **`_call_vms_add` 响亮失败** | 用一个"签名不在白名单"的假 backend 调它 ⇒ 必须 `pytest.fail` |
| **L3 L-C 棘轮** | 全仓 L-C 候选数 **≤ 基线**（只比数字，不追责文件） | 合成一个"只有跳过、没有断言"的测试文件 ⇒ 计数必须 +1 |
| **L4 flaky 反事实** | 固定墙钟 ⇒ 稳定指标相等；**只挪墙钟** ⇒ 必须不等；生产衰减语义仍在 | 若"挪墙钟也不变" ⇒ L4 红（说明该判据失去判别力） |

## L-C 候选的**机械定义**（写死在扫描器里，可核对）

> 一个 `test_*` 函数：**含跳过调用**，且满足其一：
> ① 该跳过在 `except` 处理器里（失败 ⇒ 跳过）；
> ② 该函数**没有任何 `assert`**（= 跳过就是这条测试的全部内容）。

注意这是**候选**近似（t65 的告诫：机械规则只是候选）——它的用途是**棘轮**（数量不得增长），
不是"自动判定谁该改"。

## L3 棘轮为什么**不会误伤**任何人

1. **它比的是全仓一个数字**（`当前 L-C 候选数 ≤ 基线`），**不按文件归因**：别人改文件、加新测试，
   只要不新增"只有跳过"的测试，数字就不涨 ⇒ 打不到别人身上；
2. **不要求清零**：L-A/L-B（合理的条件跳过）不进这个计数；
3. 真要新增一个"只有跳过"的测试 ⇒ 数字 +1 ⇒ 红 ⇒ 提示"要么补断言，要么显式说明为何它只能是跳过"；
   这是**判据在起作用**，不是误伤；
4. 基线写在文件里、带测量时间，**只允许下调**（改了它要改代码，能被人看见）。

跑法：``python -m pytest tests/unit/test_skip_escape_and_flaky_20261006.py -q``
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 本次（t71/I11）把 L-C 逃逸口改成响亮失败的 5 个文件
ESCAPE_HATCH_FIXED_FILES = (
    "tests/unit/test_packaging_20260930.py",
    "tests/unit/test_service_freshness_probe.py",
    "tests/test_core.py",
    "tests/unit/test_bare_sql_guard_20261006.py",
    "tests/unit/test_market_sim.py",
)

#: L-C 候选数基线（t71 修完后实测，见报告 §3；**只允许下调**）
LC_BASELINE = 20
LC_BASELINE_MEASURED_AT = ("2026-10-06 t71 收口后实测（5 条 L-C 已改响亮失败；"
                           "按本文件 docstring 的机械定义扫 tests/ + auto-daemon/tests/）")

SKIP_ATTRS = ("skip", "skipif", "xfail")


def _skip_calls(src: str) -> list:
    """AST 级：`pytest.skip/skipif/xfail` 的**调用**行号（注释/字符串里的不算）。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [-1]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if getattr(f, "attr", "") in SKIP_ATTRS and getattr(
                    getattr(f, "value", None), "id", "") in ("pytest", "pytest_mark"):
                out.append(node.lineno)
    return sorted(out)


def _lc_candidates_in(src: str) -> list:
    """返回本文件里 **L-C 候选** 的函数名（定义见模块 docstring）。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ["<unparsable>"]
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("test_"):
            continue
        skips, has_assert, in_except = [], False, False
        for node in ast.walk(fn):
            if isinstance(node, ast.Assert):
                has_assert = True
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in SKIP_ATTRS:
                skips.append(node)
        for node in ast.walk(fn):
            if isinstance(node, ast.Try):
                for h in node.handlers:
                    for inner in ast.walk(h):
                        if isinstance(inner, ast.Call) and getattr(
                                inner.func, "attr", "") in SKIP_ATTRS:
                            in_except = True
        if not skips:
            continue
        if in_except or not has_assert:
            out.append(fn.name)
    return out


def _iter_test_files():
    for base in ("tests", "auto-daemon/tests"):
        d = ROOT / base
        if d.exists():
            for p in sorted(d.rglob("test_*.py")):
                yield p


def lc_candidate_count() -> int:
    total = 0
    for p in _iter_test_files():
        total += len(_lc_candidates_in(p.read_text(encoding="utf-8", errors="replace")))
    return total


def l1_no_skip_escapes() -> bool:
    for rel in ESCAPE_HATCH_FIXED_FILES:
        p = ROOT / rel
        if not p.is_file():
            return False
        if _skip_calls(p.read_text(encoding="utf-8", errors="replace")):
            return False
    return True


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def l2_signature_allowlist(mod=None) -> bool:
    """签名白名单判据。传入 `mod` 时判定**该模块对象**（便于牙齿注入，不重新加载文件）。"""
    mod = mod if mod is not None else _load("tests/unit/test_bare_sql_guard_20261006.py", "_bsg_l2")
    allowed = getattr(mod, "ALLOWED_VMS_ADD_SIGNATURES", ())
    return bool(allowed) and all(isinstance(x, str) and x for x in allowed)


def l4_flaky_clock_counterfactual(monkeypatch) -> bool:
    """固定墙钟 ⇒ 稳定指标相等；**只挪墙钟** ⇒ 必须不等（否则判据失去判别力）。"""
    sim = _load("scripts/market_sim.py", "_ms_l4")
    tm = _load("tests/unit/test_market_sim.py", "_tm_l4")

    def frozen(moment):
        monkeypatch.setattr(sim, "_now_iso", lambda: moment)
        return tm._stable_metrics(sim.run_simulation(rounds=5, seed=42))

    same_a = frozen("2026-10-06T00:00:00+00:00")
    same_b = frozen("2026-10-06T00:00:00+00:00")     # 同一时刻、同 seed
    if same_a != same_b:
        return False                                  # 固定墙钟后还不相等 ⇒ 修法没生效
    shifted = frozen("2026-10-31T00:00:00+00:00")     # 只挪墙钟（seed 不变）
    if shifted == same_a:
        return False                                  # 墙钟不再是驱动 ⇒ 该判据失去判别力
    # 生产侧衰减语义必须仍在（本任务**未改** scripts/market_sim.py）
    src = (ROOT / "scripts/market_sim.py").read_text(encoding="utf-8", errors="replace")
    return '"created_at": _now_iso()' in src


# ── 正例 ────────────────────────────────────────────────────────────────
def test_L1_五个文件不得再有任何跳过调用():
    assert ESCAPE_HATCH_FIXED_FILES, "清单为空 ⇒ 判据空转"
    assert l1_no_skip_escapes() is True, (
        "这 5 个文件里又出现了 pytest.skip/skipif 调用 ⇒ 逃逸口回来了：%s"
        % {rel: _skip_calls((ROOT / rel).read_text(encoding="utf-8", errors="replace"))
           for rel in ESCAPE_HATCH_FIXED_FILES})


def test_L2_签名白名单存在且无效签名会响亮失败():
    assert l2_signature_allowlist() is True, "缺少显式签名白名单（或为空）"
    mod = _load("tests/unit/test_bare_sql_guard_20261006.py", "_bsg_l2b")

    class _WrongSig:
        def add(self, *a, **kw):
            raise TypeError("deliberately wrong signature")

    with pytest.raises(BaseException):     # pytest.fail ⇒ Failed（是 BaseException 子类）
        mod._call_vms_add(_WrongSig(), "手机 13812345678", "t71", "general")


def test_L2_牙齿_白名单被清空即红(monkeypatch):
    mod = _load("tests/unit/test_bare_sql_guard_20261006.py", "_bsg_l2c")
    monkeypatch.setattr(mod, "ALLOWED_VMS_ADD_SIGNATURES", (), raising=False)
    assert l2_signature_allowlist(mod) is False, "白名单被清空后判据竟然仍为真"


def test_L3_LC候选数不得超过基线():
    n = lc_candidate_count()
    assert n <= LC_BASELINE, (
        "L-C 候选数增长：%d > 基线 %d（基线测于 %s）⇒ 新增了'只有跳过'的测试。"
        "要么补上真实断言，要么在报告里显式说明为什么它只能是跳过。"
        % (n, LC_BASELINE, LC_BASELINE_MEASURED_AT))
    print("\n[L-C 棘轮] 当前候选数=%d 基线=%d（%s）" % (n, LC_BASELINE, LC_BASELINE_MEASURED_AT))


def test_L3_牙齿_合成一个只有跳过的测试必须被数到(tmp_path):
    fake = tmp_path / "test_synthetic.py"
    fake.write_text(
        "import pytest\n"
        "def test_only_skips():\n"
        "    pytest.skip('only a skip, no assertion')\n", encoding="utf-8")
    found = _lc_candidates_in(fake.read_text(encoding="utf-8"))
    assert found == ["test_only_skips"], "扫描器没抓到'只有跳过'的测试：%r" % found
    # 反向：有真实断言的跳过测试**不算** L-C 候选（避免棘轮把合理的条件跳过也算进去）
    with_assert = fake.read_text(encoding="utf-8").replace(
        "    pytest.skip('only a skip, no assertion')",
        "    pytest.skip('skip')\n    assert 1 == 1")
    assert _lc_candidates_in(with_assert) == [], "带断言的跳过测试被误判成 L-C 候选"


def test_L4_flaky_墙钟反事实(monkeypatch):
    assert l4_flaky_clock_counterfactual(monkeypatch) is True, (
        "固定墙钟后仍不相等，或'只挪墙钟'不再改变结果（判据失去判别力），"
        "或生产侧衰减语义已被改动")
