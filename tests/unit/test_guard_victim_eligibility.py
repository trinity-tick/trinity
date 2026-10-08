# -*- coding: utf-8 -*-
"""S1 单测：guard 的「受害者资格」判据（2026-09-22 §1185）。

## 为什么这条单测必须存在

`trinity-memory-guard.ps1` 的 free-branch（commit-free < minFree）原先**无差别**杀
「最大托管进程」。2026-09-22 实测：机器 commit 被 plugin_proxy.exe（174.41GB，非托管）
占满，guard 每 60 秒杀一次托管服务（当天 49 次；00:48-01:44 连续 47 次），
commit-free 仍从 9GB 掉到 0GB —— **杀了也没救回来，却把 API 打成停摆**。

判据：只有在「最大托管进程**配得上**当凶手」时才允许杀 ——
① 自身 >= MinVictimGB（与 §1021 速率分支同一把尺）；② 自身 >= VictimShare × 机器最大占用者。

## 观察面（防「写了没被读」§13.5）

本测试**从 .ps1 真文件里正则抽取函数源码**再交给 powershell 求值 ——
不复制一份到测试里（副本会漂移，绿的是副本不是真判据）；并断言：
- free-branch 里**确实调用**了该函数（出现次数 = 2：定义 + 调用）；
- 旧的无条件 kill 行在 free-branch 里**已不存在**（全文只剩 per-proc 分支那一处）；
- 非杀路径有可搜索的日志特征串 `no-eligible-victim`。
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUARD = os.path.join(ROOT, "dsh-ops", "trinity-memory-guard.ps1")

# (名字, BiggestGB, TopAllGB, MinVictimGB, VictimShare, 期望)
CASES = [
    ("实锤_0815杀API_5.18GB_vs_174.41GB", 5.18, 174.41, 10.0, 0.5, False),
    ("实锤_通宵循环_7.81GB_vs_174.41GB", 7.81, 174.41, 10.0, 0.5, False),
    ("API_12GB_仍远小于占满者", 12.0, 174.41, 10.0, 0.5, False),
    ("API自身泄漏73.65GB_是最大占用者", 73.65, 73.65, 10.0, 0.5, True),
    ("API是最大占用者_40_vs_20", 40.0, 20.0, 10.0, 0.5, True),
    ("三个托管进程都很小_3GB", 3.0, 3.0, 10.0, 0.5, False),
    ("没有托管进程", 0.0, 174.41, 10.0, 0.5, False),
    ("恰好差一点_9.99GB", 9.99, 9.99, 10.0, 0.5, False),
    ("恰好命中两条边界_10_vs_20", 10.0, 20.0, 10.0, 0.5, True),
    ("回滚_只关下限仍被份额挡住", 12.0, 174.41, 0.0, 0.5, False),
    ("回滚_只关份额仍被下限挡住", 5.0, 174.41, 10.0, 0.0, False),
    ("回滚_关份额后12GB可杀", 12.0, 174.41, 10.0, 0.0, True),
    ("回滚_两开关都关_等价旧行为", 5.0, 174.41, 0.0, 0.0, True),
    ("取不到最大占用者时不误拒", 12.0, 0.0, 10.0, 0.5, True),
]


def guard_text():
    return io.open(GUARD, encoding="utf-8-sig").read()


def extract_function():
    """从真 .ps1 里抽出 Test-GuardVictimEligible（到行首的 } 为止）。"""
    text = guard_text()
    m = re.search(r"^function Test-GuardVictimEligible \{.*?^\}", text, re.S | re.M)
    assert m, "Test-GuardVictimEligible 未找到 —— 判据被删/改名即红（本测试不能静默通过）"
    src = m.group(0)
    assert "param(" in src and "$BiggestGB" in src, "抽到的函数体不像判据实现"
    return src


def run_cases(cases):
    """把真源码写进临时 .ps1（带 BOM），用 powershell 5.1 求值，返回结果列表。"""
    lines = [extract_function(), ""]
    for i, (_n, b, t, mv, sh, _e) in enumerate(cases):
        lines.append(
            "$r%d = Test-GuardVictimEligible -BiggestGB %s -TopAllGB %s -MinVictimGB %s -VictimShare %s"
            % (i, b, t, mv, sh))
        lines.append("'%d' + [char]9 + $r%d" % (i, i))
    probe = os.path.join(tempfile.gettempdir(), "trinity_guard_victim_probe.ps1")
    with io.open(probe, "wb") as fh:
        fh.write(b"\xef\xbb\xbf" + ("\r\n".join(lines) + "\r\n").encode("utf-8"))
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", probe],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    got = {}
    for ln in (out.stdout or "").splitlines():
        if "\t" in ln:
            k, _, v = ln.partition("\t")
            if k.strip().isdigit():
                got[int(k.strip())] = (v.strip().lower() == "true")
    assert len(got) == len(cases), "探针未回全部结果：%d/%d；stdout=%r stderr=%r" % (
        len(got), len(cases), (out.stdout or "")[-400:], (out.stderr or "")[-400:])
    return got


def test_victim_eligibility_cases():
    got = run_cases(CASES)
    bad = []
    for i, (name, b, t, mv, sh, exp) in enumerate(CASES):
        if got[i] != exp:
            bad.append("%s: Biggest=%s Top=%s min=%s share=%s 期望 %s 实得 %s"
                       % (name, b, t, mv, sh, exp, got[i]))
    assert not bad, "受害者资格判据不符：\n" + "\n".join(bad)


def test_counterfactual_old_policy_would_have_killed():
    """反事实：旧判据（有托管进程就杀）在上述三条实测输入下**会**杀 —— 证明这道门不是多余的。"""
    real = [c for c in CASES if c[0].startswith("实锤_") or c[0] == "API_12GB_仍远小于占满者"]
    assert len(real) == 3, "反事实用例数应为 3"
    def old_policy(biggest, has_proc):  # 旧行为（t76：原为 lambda，去掉 E731 抑制）
        return bool(has_proc) and biggest > 0
    got = run_cases(real)
    for i, (name, b, t, mv, sh, exp) in enumerate(real):
        assert old_policy(b, True) is True, "旧判据在这条输入上本应开火：%s" % name
        assert got[i] is False, "新判据必须在这条输入上拒杀：%s" % name


def test_function_is_actually_wired_into_free_branch():
    """§13.5 写侧哑线：判据必须真的被调用，且旧的无条件杀已从 free-branch 移除。"""
    text = guard_text()
    assert text.count("Test-GuardVictimEligible") == 2, "定义+调用应各一次，实得 %d" % text.count(
        "Test-GuardVictimEligible")
    assert "Test-GuardVictimEligible -BiggestGB" in text, "free-branch 未调用资格判据"
    # 旧行为：free-branch 里紧跟日志行的无条件 Stop-Process
    old_kill = ("restart biggest managed proc PID ' + $maxProc.Id + ' (' + $biggestGB + 'GB)' + $viaTag)")
    assert old_kill not in text, "free-branch 的旧日志行仍在"
    # per-proc 分支那一处无条件 kill 仍应保留（只允许存在 1 处）
    assert text.count("if($maxProc){ Stop-Process -Id $maxProc.Id -Force -ErrorAction SilentlyContinue }") == 1, \
        "无条件 kill 行数应为 1（仅 per-proc 分支）"
    assert "no-eligible-victim" in text, "非杀路径缺少可搜索的日志特征串"
