# -*- coding: utf-8 -*-
"""t92 / D2 判据（**已 staged，待队长放行后移入 `tests/unit/`**）。

为什么现在放在仓库外：队长正在跑最终全量；本判据要求 harness 已有
`fullctx_caliber_warnings()`（= t92 的修复）⇒ **未修前必然红**。
若提前放进 `tests/unit/`，会把队长的全量染红（污染别人的验收）。
放行 + 修复落地后：把本文件复制为
`tests/unit/test_caliber_warning_blindspot_20261006.py` 即可（内容无需改动）。

（函数名一律用 ASCII —— 本仓教训：**全角字符（①（））不能出现在标识符里**。）

## 判据（可失败 + 负向 + 牙齿）

C1 截断率高 ⇒ **必须发出**，且**报出截断比例**（dropped=0 时也要响 —— 这正是 D2）
C2 **反事实**：真·完整直灌（dropped=0 且 truncated=0）⇒ **不得发**（防恒响）
C3 **牙齿**：把判据改回"只看 dropped"⇒ C1 必须红（证明 C1 真的依赖新条件）
C4 **老路径**：dropped>0 ⇒ 必须仍发，且**逐字保留**原措辞（不得为加新条件弄坏旧条件）
C5 阈值边界：比例 == 阈值 ⇒ 不报；> 阈值 ⇒ 报（口径可核对）
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

def _repo_root() -> str:
    """仓库根：优先按"本文件在 `<repo>/tests/unit/` 下"推导；staged 在仓库外时回退。

    为什么要有回退：本判据在**放行前**暂存于交付目录（仓库外），
    那时 `__file__` 推导出来的不是仓库根 ⇒ 直接 FileNotFoundError 会把"待修的红"变成"路径错"，
    两者含义完全不同。
    """
    cand = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for c in (cand, os.environ.get("TRINITY_REPO"), r"D:\trinity-code"):
        if c and os.path.isfile(os.path.join(c, "benchmark", "official_lm_eval.py")):
            return c
    return cand


ROOT = _repo_root()

#: 真实实测（t92 复现，2026-10-07）：36 题子集，item_chars=1200
REAL = {"dropped": 0, "truncated": 1671, "sessions_total": 1724, "item_chars": 1200}


def _olm_path() -> str:
    """harness 路径。

    `T92_OLM_PATH` 只在**放行前的预演**里用（把补丁打在临时副本上跑判据，仓库文件保持零改动）；
    正式移进 `tests/unit/` 后不必设置该变量。
    """
    return os.environ.get("T92_OLM_PATH") or os.path.join(
        ROOT, "benchmark", "official_lm_eval.py")


def _olm():
    spec = importlib.util.spec_from_file_location("_olm_t92", _olm_path())
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_olm_t92"] = mod
    spec.loader.exec_module(mod)
    return mod


def _warn_fn():
    """取修复后的纯函数；**未修时必须给出一条清楚的红**（不是 ImportError 糊过去）。"""
    fn = getattr(_olm(), "fullctx_caliber_warnings", None)
    assert fn is not None, (
        "harness 缺少 `fullctx_caliber_warnings()`（t92/D2 未修）⇒ **截断损失无法被护栏表达**；"
        "`caliber_warning is None` 会被误读成『这一档是完整直灌』")
    return fn


def _check_must_warn(warnings) -> None:
    """C1 的核心断言：截断率 96.93% 时护栏**必须**给出可读告警，且**报出比例**。"""
    assert warnings, "截断率 96.93% 却没有任何告警 ⇒ 护栏给的是**误导的绿灯**（D2）"
    joined = " || ".join(warnings)
    assert "1671" in joined and "1724" in joined, (
        "告警必须**报出截断比例**（截断数/总数），实际 = %r" % warnings)
    assert "下界" in joined and "不是完整 full-context" in joined, (
        "措辞要沿用本文件既有风格（『…⇒ 这不是完整 full-context，Δ 只能当**下界**』），实际 = %r" % warnings)


def _legacy_dropped_only(dropped: int, truncated: int, sessions_total: int) -> list:
    """故意模拟"只看 dropped"的旧实现（牙齿里用，**不要**在别处用）。"""
    if dropped:
        return ["预算砍掉了 %d 个会话 ⇒ 这不是完整 full-context，Δ 只能当**下界**" % dropped]
    return []


def test_C1_truncation_high_must_warn_with_ratio():
    _check_must_warn(_warn_fn()(**REAL))


def test_C2_counterfactual_true_full_context_must_not_warn():
    fn = _warn_fn()
    assert fn(dropped=0, truncated=0, sessions_total=1724, item_chars=1200) == [], (
        "真·完整直灌（无丢弃、无截断）却发了告警 ⇒ 判据恒响（无判别力）")


def test_C3_teeth_legacy_dropped_only_must_be_red_on_C1():
    fn = _warn_fn()
    with pytest.raises(AssertionError):
        _check_must_warn(_legacy_dropped_only(REAL["dropped"], REAL["truncated"],
                                              REAL["sessions_total"]))
    # 反向：真函数在同样的输入上**必须通过**（否则 C1 是空转）
    _check_must_warn(fn(**REAL))


def test_C4_legacy_dropped_path_still_warns_verbatim():
    fn = _warn_fn()
    w = fn(dropped=5, truncated=1671, sessions_total=1724, item_chars=1200)
    legacy = "预算砍掉了 5 个会话 ⇒ 这不是完整 full-context，Δ 只能当**下界**"
    assert legacy in w, "老路径的措辞必须**逐字保留**（不得为加新条件弄坏旧条件）：%r" % w
    assert len(w) >= 2, "两种损失应**并列**表达（丢弃 + 截断），实际 = %r" % w


def test_C5_threshold_boundary_equal_no_warn_above_warn():
    fn = _warn_fn()
    thr = getattr(_olm(), "FULLCTX_TRUNCATION_WARN_RATIO", None)
    assert isinstance(thr, float) and 0 < thr < 1, "缺少可核对的阈值常量：%r" % thr
    n = 1000
    at = int(n * thr)
    assert fn(dropped=0, truncated=at, sessions_total=n, item_chars=1200) == [], (
        "比例**等于**阈值时不该报（边界口径要不含糊）")
    assert fn(dropped=0, truncated=at + 1, sessions_total=n, item_chars=1200), (
        "比例**超过**阈值时必须报")
