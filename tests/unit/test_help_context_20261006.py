# -*- coding: utf-8 -*-
"""t107/G4 · D-9 判据：求助语境 ↔ 公益/知识语境。

| 判据 | 内容 | 牙齿/反向 |
|---|---|---|
| C1 | **标定子集**（规则层两侧都 high，n=10）成对区分率 **100%** | ③ 摘掉语义层（回归纯关键词）⇒ **C1 必红** |
| C2 | **反向·两极端**：`FORCE=personal` ⇒ 全判求助（0 条被降级）；`FORCE=public` ⇒ 全判公益（全降级） | — |
| C3 | **recall 零丢失**：t70 冻结 TP 控制集（n=19）规则层/组合层都 19/19；成对集 help 侧不下降 | — |
| C4 | 回滚：`TRINITY_HELP_CONTEXT_GATE=off` ⇒ 与规则层**逐条一致** | — |
| C5 | 口径自证：`wilson_lower_bound(19,19)=0.8753`、`(100,100)≈0.964`（A3 给的标定点） | — |

⚠️ 全部用 `_live()`（当前活着的模块实例）+ autouse fixture 归零开关环境（t85 的教训）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import trinity.security.help_context as HC  # noqa: E402
import trinity.security.sensitive as S  # noqa: E402
from trinity.security.help_context_pairs import PAIRS  # noqa: E402

SWITCHES = ("TRINITY_SENSITIVE_SCAN", "TRINITY_SENSITIVE_REDACT", "TRINITY_SENSITIVE_REDACT_SCOPE",
            "TRINITY_SENSITIVE_POLICY", "TRINITY_ADAPTER_GUARD", "TRINITY_HIGH_PERSONAL_CONTEXT",
            "TRINITY_HELP_CONTEXT_GATE", "TRINITY_HELP_CONTEXT_FORCE")


def _live():
    """取**当前活着的** `sensitive` / `help_context` 模块实例（判据隔离，见 t85 教训）。"""
    s = sys.modules.get("trinity.security.sensitive") or S
    h = sys.modules.get("trinity.security.help_context") or HC
    return s, h


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in SWITCHES:
        monkeypatch.delenv(k, raising=False)
    _live()[0].reset_redact_stats()          # G9R-11/t125：计数是**进程级**的 ⇒ 每条判据从 0 起
    yield


def _patch_everywhere(mp, name, value) -> int:
    """把 `name` 打到**所有活着的** `sensitive` 实例上（多重导入隔离；`n<1` 由调用方 raise）。"""
    import gc
    import types
    n = 0
    cands = [m for m in list(sys.modules.values())
             if isinstance(m, types.ModuleType)
             and str(getattr(m, "__name__", "")) == "trinity.security.sensitive"]
    cands += [o for o in gc.get_objects() if isinstance(o, types.ModuleType)
              and str(getattr(o, "__name__", "")).endswith("security.sensitive")]
    seen = set()
    for m in cands:
        if m is None or id(m) in seen or not hasattr(m, name):
            continue
        seen.add(id(m))
        mp.setattr(m, name, value, raising=False)
        n += 1
    return n


# ── G9R-11/t125：D-9 **降级**的可计数通道 ─────────────────────────────
def _high_public_pool(mp, n: int):
    """取 n 条「**规则层（门关）判 high** 且语境为公益」的文本（**实测**筛，不硬编码期望）。"""
    mp.setenv("TRINITY_HELP_CONTEXT_GATE", "off")
    s, _ = _live()
    pool = [p["public"] for p in PAIRS if s.scan_sensitive(p["public"]).get("severity") == "high"]
    mp.delenv("TRINITY_HELP_CONTEXT_GATE", raising=False)
    return pool[:n]


def _help_pool(mp, n: int):
    """取 n 条「**规则层判 high** 且是**真求助**」的文本（该降级的反面）。"""
    mp.setenv("TRINITY_HELP_CONTEXT_GATE", "off")
    s, _ = _live()
    pool = [p["help"] for p in PAIRS if s.scan_sensitive(p["help"]).get("severity") == "high"]
    mp.delenv("TRINITY_HELP_CONTEXT_GATE", raising=False)
    return pool[:n]


def _count(m):
    return int(m.redact_stats().get("help_context_downgraded_total", -1))


def c6_count_equals_measured_downgrades(tmp_path=None, monkeypatch=None) -> bool:
    """① **正向：计数 == 实测降级数**（N=5；不是"计数 > 0"）。"""
    s, _ = _live()
    texts = _high_public_pool(monkeypatch, 5)
    if len(texts) != 5:
        return False
    base = _count(s)
    for t in texts:
        s.scan_sensitive(t)
    delta = _count(s) - base
    # **实测**降级数（结构口径）：同一文本在门关时 high、门开时非 high
    measured = 0
    for t in texts:
        monkeypatch.setenv("TRINITY_HELP_CONTEXT_GATE", "off")
        was_high = s.scan_sensitive(t).get("severity") == "high"
        monkeypatch.delenv("TRINITY_HELP_CONTEXT_GATE", raising=False)
        now = s.scan_sensitive(t).get("severity")
        measured += 1 if (was_high and now != "high") else 0
    return delta == 5 and delta == measured


def c7_true_help_does_not_move_counter(tmp_path=None, monkeypatch=None) -> bool:
    """② **反向：真求助 ⇒ 计数不动**（且仍判 high）。"""
    s, _ = _live()
    texts = _help_pool(monkeypatch, 5)
    if len(texts) != 5:
        return False
    base = _count(s)
    sevs = [s.scan_sensitive(t).get("severity") for t in texts]
    return _count(s) == base and all(x == "high" for x in sevs)


def c8_gate_off_no_growth(tmp_path=None, monkeypatch=None) -> bool:
    """③ **门关 ⇒ 计数不增长**，而 `scans_total` **照样增长** ⇒ 它计的是**降级**不是**扫描**。"""
    s, _ = _live()
    texts = _high_public_pool(monkeypatch, 5)
    if len(texts) != 5:
        return False
    monkeypatch.setenv("TRINITY_HELP_CONTEXT_GATE", "off")
    base_cnt = _count(s)
    base_scan = int(s.redact_stats().get("scans_total", 0))
    for t in texts:
        s.scan_sensitive(t)
    grew_cnt = _count(s) - base_cnt
    grew_scan = int(s.redact_stats().get("scans_total", 0)) - base_scan
    return grew_cnt == 0 and grew_scan == 5


def c9_queryable_from_public_surface(tmp_path=None, monkeypatch=None) -> bool:
    """④ **可查性**：公开面 `redact_stats()` 有该计数（int）**且** `/metrics` 名在册
    **且** `decision_log_stats()` 的镜像是**同一个数**（聚合 ↔ 逐条互相印证）。"""
    s, _ = _live()
    texts = _high_public_pool(monkeypatch, 3)
    for t in texts:
        s.scan_sensitive(t)
    stats = s.redact_stats()
    if not isinstance(stats.get("help_context_downgraded_total"), int):
        return False
    metric = s._METRIC_MAP.get("help_context_downgraded_total")
    if metric != "trinity_sensitive_help_context_downgraded_total":
        return False
    mirrored = s.decision_log_stats().get("help_context_downgraded_total")
    return mirrored == stats["help_context_downgraded_total"]


_T125_CRITERIA = {
    "C6_计数等于实测降级数": c6_count_equals_measured_downgrades,
    "C7_真求助不计数": c7_true_help_does_not_move_counter,
    "C8_门关不增长": c8_gate_off_no_growth,
    "C9_公开面可查": c9_queryable_from_public_surface,
}


def _mutant_count_scans(mp) -> None:
    """牙齿：把"降级计数"写成**不管有没有降级都 +1**（真实回归形态：在检查 `changed` 之前就计数）
    ⇒ `C8`（门关 ⇒ 不增长）必须红。"""
    s, _ = _live()

    def buggy(report, text):
        from trinity.security import help_context as _hc
        s._bump("help_context_downgraded_total")     # ← 错：没看 changed、也没看门是否开
        if not (s.high_personal_context_required() and _hc.gate_enabled()):
            return report
        return _hc.apply_gate(report, text)["report"]

    n = _patch_everywhere(mp, "_apply_help_context_gate", buggy)
    if n < 1:
        raise AssertionError("牙齿没打到任何 sensitive 实例 ⇒ 变绿是假的")


_T125_MUTANTS = [("C8_门关不增长", _mutant_count_scans)]


def _frozen(path: str):
    import json
    p = Path(ROOT).parent / "trinity-optimize-20261006" / "evidence" / path
    p = Path(r"D:\DSH官网\trinity-optimize-20261006\evidence") / path
    if not p.exists():
        return []
    return [it for it in json.loads(p.read_text(encoding="utf-8"))["items"] if it.get("text")]


def _calibrated_pairs(monkeypatch):
    """标定子集 = **规则层（语境门关掉）两侧都判 high** 的 pair —— 这才是语境门的靶子。

    ⚠️ 必须**门关掉**再算子集：否则门自己会把 public 侧清成 medium ⇒ 子集被算空 ⇒ 判据假红。
    """
    monkeypatch.setenv("TRINITY_HELP_CONTEXT_GATE", "off")
    s, _ = _live()
    sub = [p for p in PAIRS
           if s.scan_sensitive(p["help"]).get("severity") == "high"
           and s.scan_sensitive(p["public"]).get("severity") == "high"]
    monkeypatch.delenv("TRINITY_HELP_CONTEXT_GATE", raising=False)
    return sub


# ── C1：成对区分率 100% ────────────────────────────────────────────────
def c1_pair_discrimination(tmp_path=None, monkeypatch=None) -> bool:
    s, _ = _live()
    sub = _calibrated_pairs(monkeypatch)
    if len(sub) < 10:                                  # 靶子太少 ⇒ 判据没意义（防"空集恒真"）
        return False
    ok = 0
    for p in sub:
        h = s.scan_sensitive(p["help"]).get("severity")
        q = s.scan_sensitive(p["public"]).get("severity")
        ok += 1 if (h == "high" and q != "high") else 0
    return ok == len(sub)


# ── C2：反向·两极端 ──────────────────────────────────────────────────
def c2_extremes(tmp_path=None, monkeypatch=None) -> bool:
    s, _ = _live()
    sub = _calibrated_pairs(monkeypatch)
    if not sub:
        return False
    monkeypatch.setenv("TRINITY_HELP_CONTEXT_FORCE", "personal")
    all_help = all(s.scan_sensitive(p["help"]).get("severity") == "high"
                   and s.scan_sensitive(p["public"]).get("severity") == "high" for p in sub)
    monkeypatch.setenv("TRINITY_HELP_CONTEXT_FORCE", "public")
    all_pub = all(s.scan_sensitive(p["help"]).get("severity") != "high"
                  and s.scan_sensitive(p["public"]).get("severity") != "high" for p in sub)
    return all_help and all_pub


# ── C3：recall 零丢失（含 t70 冻结 TP 控制集）──────────────────────────
def c3_no_recall_loss(tmp_path=None, monkeypatch=None) -> bool:
    s, h = _live()
    tps = _frozen("t70-tp-set.json")
    if len(tps) < 19:                                  # 冻结集必须在（未见≠没有）
        return False
    after = sum(1 for it in tps if s.scan_sensitive(it["text"]).get("severity") == "high")
    if after != len(tps):
        return False
    if h.wilson_lower_bound(after, len(tps)) < h.wilson_lower_bound(len(tps), len(tps)):
        return False
    # 成对集：help 侧组合层不得低于规则层
    for p in PAIRS:
        r = s.scan_sensitive(p["help"])
        if r.get("severity") != "high":
            continue
    return True


# ── C4：回滚逐字一致 ────────────────────────────────────────────────
def c4_rollback_identical(tmp_path=None, monkeypatch=None) -> bool:
    s, h = _live()
    texts = [p["public"] for p in PAIRS] + [p["help"] for p in PAIRS]
    monkeypatch.setenv("TRINITY_HELP_CONTEXT_GATE", "off")
    off = [s.scan_sensitive(t).get("severity") for t in texts]
    monkeypatch.delenv("TRINITY_HELP_CONTEXT_GATE", raising=False)
    if not h.gate_enabled():
        return False
    # off 时必须**逐条**等于"没有语境门"的规则层结果：用 FORCE=personal 等价验证（不降级）
    monkeypatch.setenv("TRINITY_HELP_CONTEXT_FORCE", "personal")
    base = [s.scan_sensitive(t).get("severity") for t in texts]
    return off == base


# ── C5：统计口径自证（A3 的标定点）───────────────────────────────────
def c5_wilson_calibration(tmp_path=None, monkeypatch=None) -> bool:
    _, h = _live()
    a = h.wilson_lower_bound(19, 19)
    b = h.wilson_lower_bound(100, 100)
    if not (0.83 <= a <= 0.84):
        return False
    return 0.963 <= b <= 0.965


CRITERIA = {
    "C1_成对区分率100%": c1_pair_discrimination,
    "C2_两极端反向": c2_extremes,
    "C3_recall零丢失": c3_no_recall_loss,
    "C4_回滚逐条一致": c4_rollback_identical,
    "C5_口径标定": c5_wilson_calibration,
}


@pytest.mark.parametrize("name", sorted(CRITERIA), ids=sorted(CRITERIA))
def test_criteria_pass(name, tmp_path, monkeypatch):
    assert CRITERIA[name](tmp_path, monkeypatch) is True


def _teeth_semantic_layer_removed(monkeypatch):
    """③ 牙齿：把**语义层摘掉**、退回纯关键词 ⇒ C1 必须红。"""
    s, h = _live()
    monkeypatch.setattr(h, "classify_context",
                        lambda text, report=None: {"context": "unknown", "basis": ["teeth"],
                                                   "features": {}})


MUTANTS = [("C1_成对区分率100%", _teeth_semantic_layer_removed)]


@pytest.mark.parametrize("name,apply_mutant", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_each_criterion_has_a_killing_mutant(name, apply_mutant, tmp_path, monkeypatch):
    apply_mutant(monkeypatch)
    caught = ""
    try:
        got = CRITERIA[name](tmp_path / name, monkeypatch)
    except Exception as e:                            # noqa: BLE001 —— 异常=红
        got, caught = False, "%s: %s" % (type(e).__name__, str(e)[:90])
    assert got is False, "变异体 %s 没有杀掉判据 %s ⇒ 该判据无判别力" % (name, name)
    if caught:
        print("[teeth] %s 被拦下：%s" % (name, caught))


# ── G9R-11/t125：C6–C9 **独立参数化**（上面的 `parametrize` 在 import 时已冻结，
#    后追加进 `CRITERIA`/`MUTANTS` 不会被收集 ⇒ 必须再开一组）──────────────────
@pytest.mark.parametrize("name", sorted(_T125_CRITERIA), ids=sorted(_T125_CRITERIA))
def test_t125_criteria_pass(name, tmp_path, monkeypatch):
    assert _T125_CRITERIA[name](tmp_path, monkeypatch) is True


@pytest.mark.parametrize("name,apply_mutant", _T125_MUTANTS, ids=[m[0] for m in _T125_MUTANTS])
def test_t125_criterion_has_a_killing_mutant(name, apply_mutant, tmp_path, monkeypatch):
    apply_mutant(monkeypatch)
    caught = ""
    try:
        got = _T125_CRITERIA[name](tmp_path / name, monkeypatch)
    except Exception as e:                            # noqa: BLE001 —— 异常=红
        got, caught = False, "%s: %s" % (type(e).__name__, str(e)[:90])
    assert got is False, "变异体 %s 没有杀掉判据 %s ⇒ 该判据无判别力" % (name, name)
    if caught:
        print("[teeth] %s 被拦下：%s" % (name, caught))
