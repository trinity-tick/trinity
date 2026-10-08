# -*- coding: utf-8 -*-
"""判据：静态检查的「已配置 / 已安装 / 已在 CI 执行」三者必须被登记且不得漂移。

## 固化的实测缺陷（2026-09-29 外部审计）

本仓的静态检查缺口不是「没配」，而是「**配了但从没人执行**」——
本仓反复记录的死法「存在但不生效」在工程纪律上的复制：

    ruff : pyproject.toml 已配置（含 ignore 清单）· 当前解释器未安装 · 9 个 workflow 里 0 个执行
    mypy : 无配置 · 无依赖 · 无 CI 步骤（完全不存在）

后果：74.4% 的函数返回标注与 65.2% 的参数标注**不产生任何验证增益**；
而 `py.typed` 已声明（对下游承诺了类型信息）却无自检。

## 本判据锁什么

    · 登记（docs/LINT_ENFORCEMENT.json）必须存在、可解析、且写明缺口与解锁条件
    · 登记里的 configured / ci_invocations / ignore 清单必须与**重新推导的事实**一致
      ⇒ 悄悄加一条 CI 步骤、或悄悄把某条规则从 ignore 里拿掉，都会立刻红
    · 凡是「配置了却 0 次执行」的检查，登记里必须有一条 `_debt` 说明它
      ⇒ 缺口不允许静默存在，也不允许被无声地"解决"掉

不直接在本轮把 ruff/mypy 接进 CI 是有理由的取舍：本仓 580,516 行 / 3,106 个 .py，
且 ignore 清单静默掉了 F821/F401/F811/F841 四条最高信号规则。在未量化错误面之前启用，
只会造出一个恒红的门禁（本仓准则：恒红的门禁会被忽略）。解锁条件写在登记的 `_debt` 里。
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTER = ROOT / "docs" / "LINT_ENFORCEMENT.json"
WORKFLOWS = ROOT / ".github" / "workflows"
PYPROJECT = ROOT / "pyproject.toml"

#: 外部审计实测、用于说明"为何不能直接全量启用"的四条最高信号规则
HIGH_SIGNAL = {"F401", "F811", "F821", "F841"}


def _load_register() -> dict:
    with io.open(REGISTER, encoding="utf-8") as fh:
        return json.load(fh)


def _pyproject_text() -> str:
    return PYPROJECT.read_text(encoding="utf-8", errors="replace")


def _workflow_texts() -> dict[str, str]:
    out = {}
    for wf in sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml")):
        try:
            out[wf.name] = wf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return out


def _ci_invocation_files(tool: str) -> list[str]:
    """哪些 workflow 真的**执行**了该工具 —— 直接调用**或**经脚本间接调用。

    2026-09-29：间接检测是必要的。本轮的修复把 ruff 接进 CI 的方式是
    `python scripts/lint_ratchet.py`，而不是直接 `ruff check`。只认直接调用会把
    「已在 CI 强制」误判为「仍未执行」，从而把一个已修的问题继续报成缺口。
    """
    direct = re.compile(
        r"(?:^|\s)(?:python\s+-m\s+)?%s\s+(?:check|--version|\.)" % re.escape(tool), re.M)
    script_rx = re.compile(r"python\s+(?:-\S+\s+)*([\w./\\-]+\.py)")
    # 2026-09-29（外部审计修复，根因 D）：**脚本源码分支必须要求"调用形态"**，
    # 不能见到工具名就算。原实现是 `tool in p.read_text(...)` 裸子串匹配 ——
    # 本轮加了一个引用 `scripts/undefined_global_audit.py` 的 CI 步骤后立刻假红：
    # 该脚本第 63 行只在字符串 `".mypy_cache"`（一个**跳过目录名**）里含 "mypy"，
    # 却被推导成"ci.yml 执行了 mypy" ⇒ 登记与实际不符 ⇒ 判据红。
    # 这是**判据自身的假阳性**，修判据而不是去改登记里的数字（否则等于把噪声写进事实）。
    # 收紧为：工具名前必须是空白/引号/方括号等分隔符（排除 `.mypy_cache` 这种前缀），
    # 且工具名后必须是空白/引号/逗号/行尾。
    invocation = re.compile(
        r"(?:^|[\s\"'\[])(?:python[^\n]{0,16}-m\s+)?%s(?=[\s\"'\],]|$)"
        % re.escape(tool), re.M)
    out: list[str] = []
    for name, text in _workflow_texts().items():
        if direct.search(text):
            out.append(name)
            continue
        for rel in script_rx.findall(text):
            p = ROOT / rel.replace("\\", "/")
            try:
                if p.is_file() and invocation.search(
                        p.read_text(encoding="utf-8", errors="replace")):
                    out.append(name)
                    break
            except OSError:
                continue
    return sorted(out)


def _ruff_configured() -> bool:
    return "[tool.ruff" in _pyproject_text()


def _ruff_ignored() -> list[str]:
    text = _pyproject_text()
    m = re.search(r"\[tool\.ruff\.lint\][^\[]*?ignore\s*=\s*\[(.*?)\]", text, re.S)
    if not m:
        return []
    return sorted(x.strip().strip("\"'") for x in m.group(1).split(",") if x.strip())


def _mypy_configured() -> bool:
    if "[tool.mypy" in _pyproject_text():
        return True
    return any((ROOT / p).is_file() for p in ("mypy.ini", ".mypy.ini", "setup.cfg"))


def test_登记必须存在_可解析_且写明缺口与解锁条件():
    assert REGISTER.is_file(), "docs/LINT_ENFORCEMENT.json 缺失 —— 缺口又回到无人记录的状态"
    reg = _load_register()
    assert reg.get("_why_a_register"), "登记必须说明为何用登记而不是直接启用"
    for tool in ("ruff", "mypy"):
        assert tool in reg, "登记缺少 %s 一节" % tool
    assert reg.get("_debt"), (
        "登记必须列出静态检查缺口（_debt）⇒ 缺口不允许静默存在")
    for d in reg["_debt"]:
        assert d.get("item") and d.get("impact") and d.get("unblock"), (
            "每条 _debt 必须写明 item / impact / unblock（否则只是一句抱怨）")


def test_ruff_的配置事实必须与登记一致():
    reg = _load_register()["ruff"]
    assert _ruff_configured() == reg["configured"], (
        "ruff 配置状态变了（推导 %s，登记 %s）⇒ 同步 docs/LINT_ENFORCEMENT.json"
        % (_ruff_configured(), reg["configured"]))
    assert _ruff_ignored() == sorted(reg["ignored_rules_all"]), (
        "ruff 的 ignore 清单变了（现为 %s，登记 %s）⇒ 同步登记"
        % (_ruff_ignored(), sorted(reg["ignored_rules_all"])))
    # 高信号子集必须是全清单里的那四条（不能只写一半，也不能漏记）
    assert (HIGH_SIGNAL & set(_ruff_ignored())) == set(reg["ignored_high_signal_rules"]), (
        "登记的高信号规则 %s 与实际被静默的 %s 不一致"
        % (reg["ignored_high_signal_rules"], sorted(HIGH_SIGNAL & set(_ruff_ignored()))))


def test_ruff_当前错误数必须与棘轮基线一致():
    """登记里报的 current_error_total 必须等于棘轮基线 —— 两个数不一致就是在骗人。"""
    reg = _load_register()["ruff"]
    baseline = json.loads((ROOT / reg["ratchet_baseline"]).read_text(encoding="utf-8"))
    assert int(baseline["_total"]) == int(reg["current_error_total"]), (
        "登记 current_error_total=%s，而 %s 的 _total=%s ⇒ 同步二者"
        % (reg["current_error_total"], reg["ratchet_baseline"], baseline["_total"]))


def test_推迟解除ignore必须有量化依据():
    """把"先不解除 ignore"写成了决策，就必须带数字 —— 否则它只是省略。"""
    reg = _load_register()["ruff"]
    cost = reg.get("measured_cost_of_unignoring_high_signal") or {}
    assert cost.get("repo_total", 0) > 100, (
        "登记缺少解除 ignore 的实测代价 ⇒ 推迟决定没有依据")
    assert cost.get("package_trinity_total", 0) > 0, "缺少 trinity/ 包内的代价数字"
    assert cost.get("by_rule_repo"), "缺少按规则分解的数字"
    for r in reg["ignored_high_signal_rules"]:
        assert r in cost["by_rule_repo"], "规则 %s 没有实测计数" % r


def test_CI执行次数必须与登记一致():
    """核心绊线：谁在 CI 里悄悄加了（或删了）静态检查，这里立刻红。"""
    reg = _load_register()
    for tool in ("ruff", "mypy"):
        files = _ci_invocation_files(tool)
        assert len(files) == reg[tool]["ci_invocations"], (
            "%s 在 CI 里的执行次数变了（推导 %d 次 %s，登记 %d 次）⇒ "
            "同步 docs/LINT_ENFORCEMENT.json 的 ci_invocations / ci_invocation_files"
            % (tool, len(files), files, reg[tool]["ci_invocations"]))
        assert sorted(files) == sorted(reg[tool]["ci_invocation_files"]), (
            "%s 的 CI 执行文件清单变了（推导 %s，登记 %s）"
            % (tool, files, reg[tool]["ci_invocation_files"]))


def test_mypy_当前错误数必须与棘轮基线一致():
    """与 ruff 那条对称：登记里的 mypy 数字必须等于棘轮基线，两边不一致就是在骗人。"""
    reg = _load_register()["mypy"]
    assert reg.get("ratchet_baseline"), "mypy 已上棘轮就必须登记 ratchet_baseline"
    baseline = json.loads((ROOT / reg["ratchet_baseline"]).read_text(encoding="utf-8"))
    assert int(baseline["_total"]) == int(reg["current_error_total"]), (
        "登记 current_error_total=%s，而 %s 的 _total=%s ⇒ 同步二者"
        % (reg["current_error_total"], reg["ratchet_baseline"], baseline["_total"]))
    assert reg["enforcement"] in ("ratchet", "hard"), (
        "mypy 已接进 CI（ci_invocations=%s）却仍标 enforcement=%r ⇒ 状态名不副实"
        % (reg.get("ci_invocations"), reg["enforcement"]))


def test_配置了却零执行必须在登记里被点名():
    """缺口必须与登记一致：配了却不跑 ⇒ _debt 里必须有对应条目。"""
    reg = _load_register()
    debt_items = " ".join(d["item"] for d in reg.get("_debt", []))
    for tool in ("ruff", "mypy"):
        configured = (_ruff_configured() if tool == "ruff" else _mypy_configured())
        invocations = reg[tool]["ci_invocations"]
        if invocations == 0:
            assert tool in debt_items, (
                "%s 配置状态=%s 且 CI 执行 0 次，但 _debt 里没有任何一条点名它 ⇒ "
                "缺口被静默了" % (tool, configured))


def test_间接调用推导不得被偶然提及误触发(monkeypatch, tmp_path):
    """**判据自身的假阳性**回归锁（2026-09-29 实测踩到）。

    原实现是裸子串 `tool in 脚本源码`。本轮加了一个引用
    `scripts/undefined_global_audit.py` 的 CI 步骤后判据立刻假红 ——
    该脚本第 63 行只在跳过目录名 `".mypy_cache"` 里含 "mypy"，
    却被推导成"ci.yml 执行了 mypy"。修法是收紧为**调用形态**。
    本判据用真实函数（而非复刻正则）钉住这两种情形。
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    probe = scripts / "_probe_indirect.py"

    # 必须拿**本模块**（而不是某个别名）来打桩：函数是按模块全局解析 ROOT 的
    _self = sys.modules[__name__]
    monkeypatch.setattr(_self, "ROOT", tmp_path)
    monkeypatch.setattr(_self, "_workflow_texts",
                        lambda: {"fake.yml": "python scripts/_probe_indirect.py"})

    # ① 仅偶然提及（"mypy" 出现在 .mypy_cache 里）⇒ **不得**算作调用
    probe.write_text('SKIP = {".mypy_cache", "__pycache__"}\n', encoding="utf-8")
    assert _self._ci_invocation_files("mypy") == [], (
        "把 `.mypy_cache` 这种偶然提及推导成「CI 执行了 mypy」⇒ 判据会假红")

    # ② 真调用形态 ⇒ **必须**算作调用（否则收紧过头，把真执行漏掉）
    probe.write_text('subprocess.run([sys.executable, "-m", "mypy", "trinity"])\n',
                     encoding="utf-8")
    assert _self._ci_invocation_files("mypy") == ["fake.yml"], (
        "真调用 `-m mypy` 没被推导出来 ⇒ 收紧过头，会把真执行漏判成缺口")


def _probe_installed_in(python_exe: str, tool: str) -> "bool | None":
    """在**指定解释器**里问「这个工具装没装」。返回 None = 探不出（调用方须显式处理）。"""
    code = ("import importlib.util as u;"
            "print(1 if u.find_spec(%r) is not None else 0)" % tool)
    try:
        r = subprocess.run([python_exe, "-c", code], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except Exception:  # noqa: BLE001 —— 探不出就是 None，绝不读成「没装」
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() == "1"


def test_解释器安装状态字段必须被登记():
    """登记里的 installed_in_serving_interpreter 必须能被**独立核验**（2026-10-06 加固）。

    ## 原判据为什么不可靠（实测，2026-10-06 测试归因轮 T1）

    字段名说的是「**服务同款解释器**里装没装」，而原实现用
    `importlib.util.find_spec(tool)` 在**当前**解释器里推导 ——
    于是同一条判据、同一份登记，结论取决于**谁在跑测试**：

        · 系统 Python 3.14（= 服务同款，装 ruff/mypy）⇒ 推导 True、登记 True ⇒ 绿；
        · `.venv` 的 CPython 3.11.15（跑测试用的那个，未装 ruff/mypy）⇒ 推导 False
          ⇒ **假红**（`assert False == True`，ruff 与 mypy 各一次）。

    「上次在哪台解释器上跑」不是事实，也不该被一条**登记核对**判据当成事实。
    按本仓纪律：判据必须锚在**被指称的那个对象**上，且探不出时**明说**而不是静默通过。

    ## 现在的判据

    ① 登记必须写明「服务同款解释器」的路径（否则该字段根本无从核验）；
    ② 该解释器**在本机存在**时，用子进程去问它 ruff/mypy 的实际安装状态，
       必须与登记逐项一致 ⇒ 从**任何**解释器跑都得到同一个结论，且仍然可失败；
    ③ 该解释器本机不存在（如 CI runner）⇒ **skip 并说明**（不是通过，也不是假红）。
    """
    reg = _load_register()
    spec = reg.get("serving_interpreter") or {}
    py = spec.get("path")
    assert py, ("登记必须写明『服务同款解释器』的路径（serving_interpreter.path）—— "
                "该字段是 installed_in_serving_interpreter 的唯一可核验锚点")
    if not pathlib.Path(str(py)).is_file():
        pytest.skip("本机没有登记的服务同款解释器 %s ⇒ installed_in_serving_interpreter "
                    "无从核验（跳过并说明，不静默通过）" % py)
    for tool in ("ruff", "mypy"):
        derived = _probe_installed_in(str(py), tool)
        assert derived is not None, (
            "服务同款解释器探测失败（%s，工具 %s）⇒ 该字段无从核验，判红而不是放行" % (py, tool))
        assert derived == reg[tool]["installed_in_serving_interpreter"], (
            "%s 在服务同款解释器（%s）里的安装状态为 %s，登记写的是 %s ⇒ 同步登记"
            % (tool, py, derived, reg[tool]["installed_in_serving_interpreter"]))

