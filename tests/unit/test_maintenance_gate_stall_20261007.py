# -*- coding: utf-8 -*-
r"""维护闸"一次即关"失效的常驻判据（t99/D1，2026-10-07）

## 为什么要有这个文件

`dsh-ops/trinity-autostart.ps1` 的 `Get-WeekKey()` 曾用 `(Get-Date).ToString("yyyy-'W'ww")`，
而 **Windows PowerShell 5.1 上 `ww` 不展开**（原样返回字面量 `ww`）⇒ 对任何日期都返回常量
`2026-Www` ⇒ 闸门 `once-weekly-2026-Www.mark` **一年只可能写一次** ⇒ 周级闸首跑后**永久关闭**
（周一质量门禁链 / 周一 weekly-check / 周日 answer-eval 三条链静默停摆 16.77 天）。
⇒ 本文件把这些**钉成可失败判据**，并且**刻意限定在我那两个闸 / 那 4 份制品上**。

## 范围纪律（重要）

⛔ **不写"扫全仓 mark"的判据** —— 那正是检测器第一版的形态（把每个历史日闸都当独立闸 ⇒ 27 条告警
⇒ **过宽 ⇒ 恒红 ⇒ 没人看**）。本文件只看：
  · **3 个周闸**：`weekly` / `weekly-acc` / `weekly-check`
  · **4 份制品**：`agent_flags_report.json` / `ipi_report.json` /
    `contradiction_resolutions.jsonl` / `market_drill_report.json`
  · 以及那个**检测器自身的规则**（用合成数据压 `findings()`，确定性与机器无关）。

## 环境依赖（如实声明）

涉及 `~/.trinity/logs` 与 `~/.trinity/state` 的两条会在**目录不存在**时 `pytest.skip` + **写明原因**
（本仓惯例：跳过必须响）；**规则逻辑本身全部用合成数据测**，不依赖本机状态。
"""
from __future__ import annotations

import importlib.util
import io
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

AUTOSTART = ROOT / "dsh-ops" / "trinity-autostart.ps1"
GATE_AUDIT = ROOT / "scripts" / "maintenance_gate_audit.py"

#: ⭐ **本判据的唯一作用域**（刻意写死，不许扫全仓）
MY_GATES = ("weekly", "weekly-acc", "weekly-check")
MY_ARTIFACTS = ("agent_flags_report.json", "ipi_report.json",
                "contradiction_resolutions.jsonl", "market_drill_report.json")

LOG_DIR = os.path.expanduser(r"~\.trinity\logs")
STATE_DIR = os.path.expanduser(r"~\.trinity\state")

#: 修复落地日（用于"退化 key 只允许存在于修复之前"这一条）
FIX_DATE = "2026-10-07"


def _load_detector():
    spec = importlib.util.spec_from_file_location("_gate_audit_t99", GATE_AUDIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GA = _load_detector()


def _ps_func_source(name: str) -> str:
    """在 PowerShell 源码里按**括号配平**取函数体（PS 没有 python AST，用最笨但可靠的办法）。

    只用 `function <name>` 到下一个顶层 `function`/结尾之间的文本块 —— 对当前文件结构足够，
    且**判据会先断言"确实取到了以 function 开头且含 return 的块"**，避免静默取空。
    """
    src = io.open(AUTOSTART, encoding="utf-8-sig", errors="replace").read()
    lines = src.splitlines()
    starts = [i for i, ln in enumerate(lines) if re.match(r"^\s*function\s+%s\b" % re.escape(name), ln)]
    assert starts, "在 %s 里找不到 function %s ⇒ 判据失效（文件结构变了？）" % (AUTOSTART.name, name)
    i = starts[0]
    body = [lines[i]]
    for j in range(i + 1, len(lines)):
        if re.match(r"^\s*function\s+\w+", lines[j]) and not lines[j].lstrip().startswith("#"):
            break
        body.append(lines[j])
    return "\n".join(body)


def _ps_func_code(name: str) -> str:
    """同上的函数体，但**剔除整行注释**。

    ⚠️ 必需：修好的 `Get-WeekKey` 里有一句注释**引用了旧的坏写法**（`…ToString("yyyy-'W'ww")…`）
    ⇒ 若直接对**全文**断言"不得出现 ToString("，判据会被**自己的注释**骗成红
    （"凡按文本判定的东西都会被文本骗"的又一实例，只是方向相反）。
    """
    keep = [ln for ln in _ps_func_source(name).splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(keep)


# ══════════════════════════════════════════════════════════════════════════
# ① DEGENERATE-KEY
# ══════════════════════════════════════════════════════════════════════════

def test_week_key_must_not_use_toString_ww_specifier() -> None:
    """⭐ ① 根因判据：`Get-WeekKey` 不得用 `ToString` 的 `ww` 那种**不展开**的格式串。

    实测（Windows PowerShell 5.1）：`(Get-Date).ToString('ww')` **原样返回字面量 `ww`**
    ⇒ 该格式串产出**恒定 key** ⇒ 闸门一年只开一次。⇒ 必须走 `Calendar.GetWeekOfYear`。
    """
    body = _ps_func_code("Get-WeekKey")   # ⚠️ 用**剔除注释后**的代码，否则会被自己的注释骗红
    assert "Get-WeekKey" in body, "取函数体失败（空块）⇒ 判据无判别力"
    assert "ToString(" not in body, (
        "Get-WeekKey 又用回了 ToString ⇒ 在 PS 5.1 上 `ww` 不展开、key 恒定、闸门会再次永久关闭；"
        "必须用 Calendar.GetWeekOfYear。函数体：\n%s" % body)
    assert "GetWeekOfYear" in body, "Get-WeekKey 既没用 ToString 也没用 GetWeekOfYear ⇒ 判据需复核"
    assert "{1:D2}" in body, "周号未按两位数字格式化 ⇒ key 可能不含数字（判定退化 key 的依据会失效）"


def test_week_key_formula_yields_distinct_keys_per_week() -> None:
    """⭐ ① 的行为面（纯 Python 复算同一公式）：连续 4 个周一必须得到**4 个不同 key**。"""

    def key_of(d: date) -> str:
        # 与 .ps1 里 Calendar.GetWeekOfYear(..., FirstFourDayWeek, Monday) 等价的 ISO 周号
        iso_year, iso_week, _ = d.isocalendar()
        return "%d-W%02d" % (iso_year, iso_week)

    keys = [key_of(date(2026, 9, 21) + timedelta(weeks=i)) for i in range(4)]
    assert len(set(keys)) == 4, "连续 4 周应得 4 个不同 key，实测 %s" % keys
    for k in keys:
        assert re.search(r"\d", k.split("-W")[-1] or ""), "key 的周号部分必须含数字：%s" % k


def test_degenerate_key_marks_may_only_predate_the_fix() -> None:
    """⭐ ① 现场面：**我的 3 个周闸**里，纯字母 key 的 mark 只允许是**修复之前**留下的。

    （修复前的 `once-weekly-2026-Www.mark` 等按队长裁定**保留为现场证据** ⇒ 不能要求它们消失；
    但**修复之后**再出现纯字母 key 就是回归 ⇒ 必红。）
    """
    if not os.path.isdir(LOG_DIR):
        pytest.skip("环境不具备：%s 不存在（本机状态依赖；规则逻辑另有合成数据判据覆盖）" % LOG_DIR)
    fix_ts = time.mktime(time.strptime(FIX_DATE + " 23:59:59", "%Y-%m-%d %H:%M:%S"))
    bad = []
    for mk in GA.parse_marks(LOG_DIR):
        fam = GA.gate_family(mk["stem"])
        if fam not in MY_GATES:
            continue
        if mk["key"] and re.fullmatch(r"[A-Za-z]+", mk["key"]) and mk["mtime"] > fix_ts:
            bad.append(mk["file"])
    assert not bad, "修复之后又出现了退化 key 的闸门 mark（闸门会再次永久关闭）：%s" % bad


# ══════════════════════════════════════════════════════════════════════════
# ② GATE-STALL（合成数据，与机器无关）
# ══════════════════════════════════════════════════════════════════════════

def _mk(stem: str, age_days: float, key: str = "20260101") -> dict:
    return {"file": "once-%s.mark" % stem, "stem": stem, "gate": stem.rsplit("-", 1)[0],
            "key": key, "mtime": time.time() - age_days * 86400.0, "age_days": age_days}


def test_gate_stall_flags_a_skipped_weekly_step() -> None:
    """⭐ ② + ③ **负向牙齿**："人为把某一步 skip"（某个周闸 20 天没跑）⇒ **必须红**。"""
    skipped = [_mk("weekly-2026-W41", 20.0, "W41")]
    got = GA.findings(skipped)
    assert any(f["kind"] == "GATE-STALL" for f in got), "周闸 20 天没跑却没判红：%s" % got
    assert any(f["gate"] == "weekly" for f in got), "闸族归并错了：%s" % got


def test_gate_stall_does_not_fire_for_a_fresh_gate() -> None:
    """⭐ 反事实：**健康闸不得被报**（`screen` 现状：最新 mark 只有 0.5 天 ⇒ 必须安静）。"""
    fresh = [_mk("screen-20261007", 0.53, "20261007")]
    assert GA.findings(fresh) == [], "健康闸被误报 ⇒ 报警器过宽：%s" % GA.findings(fresh)
    # 周闸刚跑过也不该报
    assert GA.findings([_mk("weekly-2026-W41", 1.0, "W41")]) == [], "刚跑过的周闸被误报"


def test_detector_must_not_be_too_wide_over_mark_history() -> None:
    """⭐ ③ 的姊妹条（第一版"27 条过宽"的回归闸）：**同一闸族的历史 key 不是停摆。**

    合成：某日闸有 20 个**历史** mark（最老的 25 天）+ 一个**今天**的 mark
    ⇒ 只应按"闸族最新 mark"判定 ⇒ **零告警**。
    """
    hist = [_mk("screen-202609%d" % (10 + i), 25.0 - i, "202609%02d" % (10 + i)) for i in range(20)]
    hist.append(_mk("screen-20261007", 0.1, "20261007"))
    assert GA.findings(hist) == [], (
        "同一闸族的历史 mark 被当成了停摆 ⇒ 报警器过宽（这正是第一版 27 条告警的形态）：%s"
        % GA.findings(hist))


# ══════════════════════════════════════════════════════════════════════════
# ④ "关掉新鲜度检查 ⇒ 判据必红"（证明安全网不是空的）
# ══════════════════════════════════════════════════════════════════════════

def test_stall_rule_must_be_finite_and_positive() -> None:
    """④a：停摆阈值必须是**有限正数**（若被设成 inf/0，上面那条"skip ⇒ 红"就变成恒绿/恒红）。"""
    for gate in MY_GATES:
        p = GA.period_days(gate)
        assert isinstance(p, int) and p > 0, "闸 %s 的周期不是有限正数：%r" % (gate, p)
    assert GA.period_days("weekly-2026") == 7, "周闸周期应为 7 天"


def test_disabling_the_freshness_threshold_makes_the_skipped_case_undetected(monkeypatch) -> None:
    """⭐ ④ **敏感性证明**：把新鲜度阈值"关掉"（调成无穷）⇒ "skip ⇒ 红"那条判据**必然失效**。

    ⇒ 反过来证明：上面 `test_gate_stall_flags_a_skipped_weekly_step` 的"红"**不是恒红的装饰**，
    而是真的由这个检查产生的 —— 检查一旦被关，它就抓不到东西。
    """
    monkeypatch.setattr(GA, "period_days", lambda gate: 10 ** 9)
    assert GA.findings([_mk("weekly-2026-W41", 20.0, "W41")]) == [], \
        "阈值关掉后仍能报出停摆 ⇒ 说明该检查另有来源（或判据在自证）"
    monkeypatch.undo()
    assert GA.findings([_mk("weekly-2026-W41", 20.0, "W41")]), "恢复阈值后必须重新报出"


# ══════════════════════════════════════════════════════════════════════════
# 现场一致性：检测器对我这两个闸的读数必须可复算（环境依赖，缺失即 skip 且写明）
# ══════════════════════════════════════════════════════════════════════════

def test_live_state_is_consistent_with_the_detector() -> None:
    """现场一致性：`maintenance_gate_audit` 的 `findings()` 与"按闸族取最新 mark 复算"一致。

    ⚠️ 本判据**只核一致性**、不要求"现在必须新鲜" ——
    根因修复后制品要等**下一次周一调度**才会刷新（修好根因 ≠ 立刻转绿）。
    """
    if not os.path.isdir(LOG_DIR):
        pytest.skip("环境不具备：%s 不存在（本机状态依赖）" % LOG_DIR)
    marks = [m for m in GA.parse_marks(LOG_DIR) if GA.gate_family(m["stem"]) in MY_GATES]
    got = sorted((f["kind"], f["gate"]) for f in GA.findings(marks))
    fams = {}
    for m in marks:
        f = GA.gate_family(m["stem"])
        if f not in fams or m["mtime"] > fams[f]["mtime"]:
            fams[f] = m
    exp = []
    for fam, m in fams.items():
        if m["key"] and re.fullmatch(r"[A-Za-z]+", m["key"]):
            exp.append(("DEGENERATE-KEY", fam))
        if m["age_days"] > 2 * GA.period_days(fam):
            exp.append(("GATE-STALL", fam))
    assert got == sorted(exp), "检测器读数与复算不一致：%s vs %s" % (got, sorted(exp))
    assert set(fams) <= set(MY_GATES), "出现了作用域外的闸：%s" % (set(fams) - set(MY_GATES))


def test_scope_is_limited_to_my_gates_and_artifacts() -> None:
    """⭐ 范围纪律本身也要可失败：判据的作用域常量不得被"扩成扫全仓"。"""
    assert MY_GATES == ("weekly", "weekly-acc", "weekly-check")
    assert MY_ARTIFACTS == ("agent_flags_report.json", "ipi_report.json",
                            "contradiction_resolutions.jsonl", "market_drill_report.json")
    src = io.open(Path(__file__), encoding="utf-8-sig").read()
    # ⚠️ 探针字符串**必须拼出来**，不能整段写进源码：否则它在**本文件里**出现 ⇒ 判据自指 ⇒ 恒红
    # （第一版就这么踩了：ban 列表里的字面量被同一条断言搜到）
    banned = ("os" + "." + "walk(", "rg" + "lob(")
    for needle in banned:
        assert needle not in src, "本判据不得扫全仓：出现 %r" % needle
