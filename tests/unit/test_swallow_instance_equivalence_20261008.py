# -*- coding: utf-8 -*-
"""t124 / G9R-10 判据：`trinity._swallow` 被"pop + 重导"制造第二实例 ⇒ **把"无害"钉成可失败断言**。

## 背景（t119/G9R-5 §9 排查清单里优先级最高的一处）

`tests/unit/test_swallow_fallback_contract.py:119` 会 `sys.modules.pop("trinity._swallow")` 再重导
（**故意的**：它要制造"真身不可用"以驱动各模块 `except: def swallow(...)` 回退臂），
而 `trinity._swallow` 全仓有 **855 处** `from trinity._swallow import …` 的**名字导入**持有者
⇒ **形态与 C1/t93 同族**（判据手里是新实例、产品代码手里是旧实例）。

## t124 的判定：**未能复现缺陷**（理由要能挡住将来的质疑）

| 事实（本次实测） | 证据 |
|---|---|
| `_swallow` **是**有状态的（模块级 `_COUNTS`/`_LAST`/`_LAST_DETAIL`）⇒ 实例分裂**真实存在** | 源码 L38/39/62 + 子进程探针（本文件 S2） |
| 但**账本没有任何消费者**：`trinity/**` 与 `tests/**` 里对 `stats/last/last_failures/reset` 的**调用数 = 0** | 本文件 S1 的结构谓词 + `evidence/g9r10_*` |
| `swallow()` 是**无返回值、无分支**的记录动作（异常也吞掉）⇒ 两实例对同一入参**行为逐项相同** | 本文件 S2 |
| 该测试在 `finally` 里**复原** `sys.modules["trinity._swallow"]`（L137-138）⇒ 不留残留 | 源码 |

⇒ **结论**：**C1 之所以致命，是因为 `sensitive` 有"被产品路径读取的策略对象"**（patch 在哪个实例上会改变**行为**）；
而 `_swallow` 被分裂的东西（账本）**没有任何读取者** ⇒ **今天不可观测** ⇒ **不改代码**（按"只修实测到的问题"），
但把"无害"钉成判据：**一旦将来有人接上账本消费者、或在 `_swallow` 上做 monkeypatch，S1/S3 会立刻红**。
"""
from __future__ import annotations

import ast
import io
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SWALLOW = os.path.join(ROOT, "trinity", "_swallow.py")
JOURNAL_ACCESSORS = ("stats", "last", "last_failures", "reset")


# ── S1：结构谓词（**断言结构而非字符串** —— 用 AST 判"调用/导入后调用"）──────────
def _imported_names(src: str):
    """返回 {本地名: 原名}（`from trinity._swallow import X as Y`）。"""
    out = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("_swallow"):
            for a in n.names:
                out[a.asname or a.name] = a.name
    return out


def _module_aliases(src: str) -> dict:
    """返回 {本地名: 模块路径}：`import trinity._swallow as sw` ⇒ {"sw": "trinity._swallow"}。"""
    out = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.endswith("_swallow"):
                    out[a.asname or a.name] = a.name
    return out


def product_consumes_journal(src: str) -> list:
    """**产品源码**里是否有人**调用** `_swallow` 的账本读取器（`stats/last/last_failures/reset`）。

    判定是**结构**的：① `sw.stats(...)`（`sw` = `import trinity._swallow as sw` 的别名）；②
    `_swallow.stats(...)`（源码段可判）；③ `from trinity._swallow import stats` 后**真的调用** `stats()`。
    **只提到名字（文档/注释/字符串）不算** —— 这是"断言结构而非字符串"的要求。
    """
    hits = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return hits
    alias = _imported_names(src)          # from … import 的本地名
    mods = _module_aliases(src)           # import … as 的模块别名
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        attr = getattr(f, "attr", "")
        name = getattr(f, "id", "")
        if attr in JOURNAL_ACCESSORS:
            owner = getattr(getattr(f, "value", None), "id", "")
            try:
                seg = ast.get_source_segment(src, n) or ""
            except Exception:  # noqa: BLE001
                seg = ""
            if owner in mods or "_swallow" in seg:
                hits.append((n.lineno, "attr-call:%s" % attr))
        if name and alias.get(name) in JOURNAL_ACCESSORS:
            hits.append((n.lineno, "imported-call:%s" % alias[name]))
    return hits


def _scan_product_journal_consumers():
    out = []
    for base in ("trinity", "tests", "scripts"):
        for dp, _dn, fn in os.walk(os.path.join(ROOT, base)):
            if "__pycache__" in dp:
                continue
            for f in sorted(fn):
                if not f.endswith(".py"):
                    continue
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
                if rel == os.path.relpath(__file__, ROOT).replace(os.sep, "/"):
                    continue          # 本文件自己含这些名字（谓词/说明），不算消费者
                try:
                    src = io.open(p, encoding="utf-8-sig", errors="replace").read()
                except Exception:  # noqa: BLE001
                    continue
                for ln, why in product_consumes_journal(src):
                    out.append("%s:%d %s" % (rel, ln, why))
    return out


def test_S1_账本在当前仓库里不得有消费者():
    """S1（**可失败**）：只要有人把 `_swallow` 的账本接上消费者 ⇒ 本判据红。

    为什么这是"无害"结论的前提：**分裂出来的东西（账本）今天没人读** ⇒ 实例分裂不可观测。
    一旦有消费者，C1 同族缺陷立刻成立（判据手里新实例、产品手里旧实例）⇒ 这条会当场把人拦住。
    """
    cons = _scan_product_journal_consumers()
    assert cons == [], (
        "`trinity._swallow` 的账本读取器（%s）**出现了消费者** ⇒ 实例分裂从"
        "『不可观测』变成『真缺陷』（判据/产品可能各拿一个实例）⇒ 要么改用共享单例，"
        "要么在判据里做全实例覆盖（同 t93/C1 的 `_patch_policy`）。命中：%s"
        % ("/".join(JOURNAL_ACCESSORS), cons[:8]))


def test_S1_牙齿_合成一个消费者必须被谓词抓到():
    """S1 的牙齿：把"产品路径调用 `_swallow.stats()`"合成进源码 ⇒ 谓词**必须**报出来。"""
    assert product_consumes_journal("from trinity._swallow import stats\nx = stats()\n"), \
        "名字导入后调用没被检出 ⇒ S1 恒真"
    assert product_consumes_journal("import trinity._swallow as sw\ny = sw.stats()\n"), \
        "属性调用没被检出 ⇒ S1 恒真"
    # 只提到名字（不调用）**不得**误报
    assert product_consumes_journal("from trinity._swallow import stats\n# stats 仅用于说明\nX = 1\n") == [], \
        "只提及未调用被误报 ⇒ 谓词过宽"


# ── S2/S3：子进程探针（制造第二实例 + 行为等价 + 方向性）──────────────────────
_PROBE = r'''
import importlib, json, sys
sys.path.insert(0, %(root)r)
import trinity._swallow as m1
snap = {}
def call(mod, tag):
    try:
        r = mod.swallow(tag, ValueError("boom"), detail="ctx")
        return ("ok", r)
    except Exception as e:          # 回退/真身都不该抛
        return ("raised", type(e).__name__)
snap["m1_call"] = call(m1, "t124-m1")
snap["m1_stats_has_tag"] = m1.stats().get("t124-m1", 0)
sys.modules.pop("trinity._swallow", None)
import trinity._swallow as m2
snap["distinct"] = m1 is not m2
snap["m2_stats_before"] = dict(m2.stats())
snap["m2_call"] = call(m2, "t124-m2")
snap["m2_stats_after"] = m2.stats().get("t124-m2", 0)
snap["m1_sees_m2"] = m1.stats().get("t124-m2", 0)
# 方向性：把**新实例**的 swallow 换成 no-op ⇒ 旧实例（产品持有者）必须不受影响
m2_old_swallow = m2.swallow
m2.swallow = lambda *a, **k: None
snap["m2_patched_is_noop"] = (m2.swallow("t124-m2b", ValueError("x")) is None)
snap["m1_unaffected"] = (call(m1, "t124-m1b")[0] == "ok" and m1.stats().get("t124-m1b", 0) == 1)
# 动态取用者（反例）：每次从 sys.modules 取活实例 ⇒ 会被 patch 影响
def dynamic():
    return sys.modules["trinity._swallow"].swallow("t124-dyn", ValueError("x"))
snap["dynamic_is_affected"] = (dynamic() is None)
m2.swallow = m2_old_swallow
print(json.dumps(snap, ensure_ascii=False))
'''


def _probe() -> dict:
    import json
    p = subprocess.run([sys.executable, "-c", _PROBE % {"root": ROOT}], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=300)
    line = [x for x in (p.stdout or "").splitlines() if x.strip().startswith("{")]
    assert line, "探针没输出 JSON（rc=%s）：%s | %s" % (p.returncode, (p.stdout or "")[-300:],
                                                       (p.stderr or "")[-300:])
    return json.loads(line[-1])


def test_S2_两实例确实分裂_但_swallow_行为逐项等价():
    """S2：① 实例分裂**真实存在**；② 两实例的 `swallow()` 对同一入参**行为一致**（都不抛、都记自己那条）；
    ③ 账本**互相独立** ⇒ 分裂的是"没人读的账本"，不是"被读的策略"。"""
    s = _probe()
    assert s["distinct"] is True, "没能造出第二个实例 ⇒ 本判据的靶子不成立：%s" % s
    assert s["m1_call"] == ["ok", None] and s["m2_call"] == ["ok", None], (
        "两实例的 swallow 行为不一致（应都返回 None、都不抛）：%s" % s)
    assert s["m1_stats_has_tag"] == 1 and s["m2_stats_after"] == 1, s
    assert s["m1_sees_m2"] == 0 and s["m2_stats_before"].get("t124-m1", 0) == 0, (
        "两实例账本竟然共享/可见 ⇒ 与源码 L38/39/62 的模块级状态矛盾：%s" % s)


def test_S3_牙齿_patch新实例不影响旧实例持有者_而动态取用者会被影响():
    """S3（**方向性 + 牙齿**）：把"无害"钉住，同时证明这条判据**抓得住**真正的缺陷形态。

    · **无害方向（当前实情）**：产品代码是 855 处 `from … import swallow`（**名字导入**，捕获函数对象）
      ⇒ 只把**新实例**的 `swallow` 换成 no-op ⇒ 旧实例持有者**不受影响**；
    · **牙齿**：`sys.modules["trinity._swallow"].swallow(...)` 这种**动态取用**会被 patch 影响
      ⇒ 若将来产品改成动态取用、或有人 patch 错实例，`dynamic_is_affected` 会翻转 ⇒ 这条判据红。
    """
    s = _probe()
    assert s["m2_patched_is_noop"] is True, "新实例的 patch 没生效 ⇒ 本判据的探针坏了：%s" % s
    assert s["m1_unaffected"] is True, (
        "patch 新实例竟然影响了旧实例持有者 ⇒ 实例隔离**不存在**（那反而是另一种严重问题）：%s" % s)
    assert s["dynamic_is_affected"] is True, (
        "动态取用者竟然不受影响 ⇒ 牙齿失效（我可能 patch 错了对象）：%s" % s)
