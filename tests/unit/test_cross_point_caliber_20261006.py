# -*- coding: utf-8 -*-
"""t123/G9R-9 判据：**跨点掩码口径一致性**（主口径 `MASK_CONVENTION` ↔ 各掩码点）。

| 判据 | 内容 |
|---|---|
| **C1（正向）** | 所有**口径内**登记点都符合主口径；**显式登记为例外**的点不计入失败（但仍被报告） |
| **C2（反向：某点分叉 ⇒ 必红）** | 把 `_crypto._detect_pii` 的实测值换成旧口径 `138****1234`（留尾 4 位）⇒ **必须判它不符合且整体红** |
| **C3（牙齿：改主口径 ⇒ 未登记为例外的点必红）** | 把 `MASK_CONVENTION.digits.head` 改成 **2** ⇒ **所有非例外点**必须红，**例外点仍不计入失败** ⇒ 证明判据"比的是主口径"而不是硬编码值 |
| **C4（例外登记表可读 + 能报差异）** | `mask_point_exceptions()` 每条含 `reason/caliber/owner/since`；报告里例外点带 `is_exception=True` **且** `diff` 写明"刻意不同"的原因 |
| **C5（跨点一致 = 结构相等）** | 同一批规范样本下，`sensitive` 与 `_crypto` 的输出**落在同一个由主口径推导的结构里**（不是硬编码串相等） |

⭐ 纪律：① **断言结构而非字符串**（C1/C3/C5 都用由 `MASK_CONVENTION` 推导的正则）；
② 牙齿用 **monkeypatch**（**不改产品文件**）；③ 探针**真调用**各掩码点（`_crypto` 用最小 stub 调 `_detect_pii`）。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import trinity.security.redaction_surface as RS  # noqa: E402


def _ok(report) -> bool:
    """只把**非例外**点的失败算失败（例外点＝"刻意不同"）。"""
    return report["all_ok"] is True and not report["failing"]


def _non_exception_points(report):
    return [p for p in report["points"] if not p["is_exception"]]


# ── C1 ────────────────────────────────────────────────────────────────
def c1_all_registered_points_consistent(tmp_path=None, monkeypatch=None) -> bool:
    report = RS.caliber_report()
    if not _ok(report):
        return False
    pts = _non_exception_points(report)
    ids = {p["id"] for p in pts}
    if not {"sensitive._mask_head", "sensitive._mask_email",
            "adapters.sqlite._crypto._detect_pii"} <= ids:
        return False
    for p in report["points"]:
        if not p["observed"] or not p["expected"]:
            return False                              # 报告必须给出"实测"与"期望"
        if not p["complies"] and not p["diff"]:
            return False                              # 不符合时必须**说明差在哪**
    return True


# ── C2 反向：某点分叉 ⇒ 必红 ─────────────────────────────────────────
def _patch_crypto_to_old_caliber(mp) -> int:
    """把 `_crypto` 点的实测值换成**旧口径**（留尾 4 位）——monkeypatch，**不改产品文件**。"""
    spec = RS.PROBES.get("adapters.sqlite._crypto._detect_pii")
    if not spec:
        return 0
    # ⚠️ 键里含 `.` ⇒ 必须用 `setitem`（`setattr` 会把点号当属性路径解析）
    mp.setitem(RS.PROBES, "adapters.sqlite._crypto._detect_pii",
               {"probe": lambda: "138****1234", "family": spec["family"]})
    return 1


def c2_diverged_point_makes_report_red(tmp_path=None, monkeypatch=None) -> bool:
    if _patch_crypto_to_old_caliber(monkeypatch) < 1:
        raise AssertionError("牙齿没打到任何探针 ⇒ 变绿是假的")
    report = RS.caliber_report()
    bad = [p for p in report["points"] if p["id"] == "adapters.sqlite._crypto._detect_pii"]
    if not bad or bad[0]["complies"]:
        return False                                  # 必须判它不符合
    return not _ok(report)                            # 整体必须红


# ── C3 牙齿：改主口径 ⇒ 未登记点必红 ─────────────────────────────────
def _patch_convention_head(mp, head: int = 2) -> None:
    conv = copy.deepcopy(RS.MASK_CONVENTION)
    conv["digits"] = {**conv["digits"], "head": head}
    conv["email"] = {**conv["email"], "local_head": head}
    mp.setattr(RS, "MASK_CONVENTION", conv)


def c3_changing_convention_reddens_non_exceptions(tmp_path=None, monkeypatch=None) -> bool:
    _patch_convention_head(monkeypatch, head=2)
    report = RS.caliber_report()
    if _ok(report):
        return False                                  # 改了主口径 ⇒ 必须红
    non_exc = _non_exception_points(report)
    if not non_exc or any(p["complies"] for p in non_exc):
        return False                                  # **所有**非例外点都必须红
    exc = [p for p in report["points"] if p["is_exception"]]
    return bool(exc) and all(p["complies"] for p in exc)   # 例外点仍不计入失败


# ── C4 例外登记表 ─────────────────────────────────────────────────────
def c4_exception_registry_readable(tmp_path=None, monkeypatch=None) -> bool:
    exc = RS.mask_point_exceptions()
    if "scripts.knowledge_pack._redact" not in exc:
        return False                                  # 占位符式的**故意例外**必须在册
    for info in exc.values():
        if not all(info.get(k) for k in ("reason", "caliber", "owner", "since")):
            return False
    report = RS.caliber_report()
    marked = [p for p in report["points"] if p["is_exception"]]
    if len(marked) != len(exc):
        return False
    return all(p["diff"] and p["exception_reason"] for p in marked)


# ── C5 跨点一致（结构相等，而非字符串相等）───────────────────────────
def c5_cross_point_structural_equality(tmp_path=None, monkeypatch=None) -> bool:
    from trinity.security import sensitive as S
    from trinity.adapters.sqlite._crypto import _CryptoMixin
    from trinity.security.redaction_surface import _CryptoProbeStub
    conv = RS.MASK_CONVENTION
    head = int(conv["digits"]["head"])
    ch = str(conv["digits"]["mask_char"])
    stub = _CryptoProbeStub()
    cases = [("13814141414", "digits"), ("zhangsan@corp.cn", "email")]
    for sample, family in cases:
        a = S.redact_identifiers(sample)[0]
        b = _CryptoMixin._detect_pii(stub, sample)["redacted"]
        ok_a, _d, _x = RS._check_structure(a, family, conv)
        ok_b, _e, _y = RS._check_structure(b, family, conv)
        if not (ok_a and ok_b):
            return False                              # 两点都必须落在主口径的结构里
        if len(a) != len(b) or a[:head] != b[:head]:
            return False                              # 结构相等：长度与前缀一致
        if sample in a or sample in b:
            return False                              # 原文不得出现
        if ch not in a or ch not in b:
            return False                              # 必须真的掩了
    return True


CRITERIA = {
    "C1_口径内点全一致": c1_all_registered_points_consistent,
    "C2_某点分叉必红": c2_diverged_point_makes_report_red,
    "C3_改主口径非例外点必红": c3_changing_convention_reddens_non_exceptions,
    "C4_例外表可读且报差异": c4_exception_registry_readable,
    "C5_跨点结构相等": c5_cross_point_structural_equality,
}


@pytest.mark.parametrize("name", sorted(CRITERIA), ids=sorted(CRITERIA))
def test_criteria_pass(name, tmp_path, monkeypatch):
    assert CRITERIA[name](tmp_path, monkeypatch) is True


# ── 牙齿（人为让某点分叉 / 改主口径 ⇒ C1 必红）────────────────────────
def _mutant_diverged_point(mp) -> None:
    assert _patch_crypto_to_old_caliber(mp) >= 1, "牙齿没打到任何探针 ⇒ 变绿是假的"


def _mutant_convention_head(mp) -> None:
    _patch_convention_head(mp, head=2)


MUTANTS = [
    ("C1_口径内点全一致", _mutant_diverged_point, "某点分叉"),
    ("C1_口径内点全一致", _mutant_convention_head, "改主口径"),
]


@pytest.mark.parametrize("name,apply_mutant,label", MUTANTS, ids=[m[2] for m in MUTANTS])
def test_each_criterion_has_a_killing_mutant(name, apply_mutant, label, tmp_path, monkeypatch):
    sub = tmp_path / label
    sub.mkdir()
    caught = ""
    try:
        apply_mutant(monkeypatch)
        got = CRITERIA[name](sub, monkeypatch)
    except Exception as e:                            # noqa: BLE001 —— 异常=红
        got, caught = False, "%s: %s" % (type(e).__name__, str(e)[:90])
    assert got is False, "变异体 %s 没杀掉判据 %s ⇒ 该判据无判别力" % (label, name)
    if caught:
        print("[teeth] %s 被拦下：%s" % (label, caught))
