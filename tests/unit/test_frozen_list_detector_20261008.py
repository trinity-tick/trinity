# -*- coding: utf-8 -*-
"""t128 / G9R-14 判据：**"判据清单在 import 时冻结 ⇒ 绿但没跑"** 族的检测器 + 正例/反例/牙齿/自闭环。

## ⭐ 先更正任务前提（**本任务硬测量推翻的那条**）

任务书写的是"`@pytest.mark.parametrize` 在 **import 时冻结**，所以装饰器之后 `append` 的项**不会**被收集"。
**实测反例**（本文件 C2，含 `--collect-only` 计量）：在模块顶层、装饰器**之后** `CASES.append(...)` / `CASES += [...]`
⇒ **收集数 = 4**（装饰器时是 2）⇒ ⭐ **`parametrize` 持的是"引用"，参数在 collection（import 之后）才物化**
⇒ **模块顶层、装饰器之后的追加是有效的**。

**真正会"绿但没跑"的形态**（修正后的族，本文件 C1/仓库扫描用的就是它）：
1. **R1 非 import 期变异**：追加写在 **函数 / fixture / 类体 / `if __name__`** 里 ⇒ import 时不执行 ⇒ 那些项永远不进参数集；
2. **R2 追加到"另一个名字"**：`X_EXTRA.append(...)` 而 parametrize 绑的是 `X`（且没人绑定 `X_EXTRA`）
   ⇒ 追加看着"加上了"，其实没进任何一个参数集（t125 的 `NameError: CRITERIA` → 改对后"绿但没跑"很可能就是这个形态）。

## t128 实测（本仓当前树，2026-10-08）

| 口径 | 数 |
|---|---|
| 直接绑定"模块顶层清单"的 parametrize 站点 | **40**（**20** 个测试文件） |
| 逐站点硬测量：`len(清单)`（import 后）vs 该参数化用例**被收集**条数 | **Δ 全 = 0** ⇒ **漏收集合计 0** |
| 修正族扫描 **R1**（非 import 期变异） | **0 处** |
| 修正族扫描 **R2**（追加到另一个名字） | **0 处** |
| 全仓收集数（`pytest tests --collect-only -q`，**2026-10-08 09:44:49**） | **5556** |
| ⭐ 牙齿：只数 `def test_*` | **5061** ⇒ 差 **495**（证明用的是**收集数**） |

⇒ **结论：这一族在本仓当前树无缺陷 ⇒ 不改任何既有文件**；本判据把"**修正后的族** + 反例 + 牙齿 + 自闭环"钉住。
"""
from __future__ import annotations

import ast
import io
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MUTATORS = ("append", "extend", "insert", "update", "add", "setdefault", "pop", "clear", "remove")


# ── 检测器（结构判据）────────────────────────────────────────────────────
def _module_lists(tree) -> dict:
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, (ast.List, ast.Dict)):
                    out[t.id] = n.lineno
    return out


def _bound_names(tree) -> dict:
    """parametrize 绑定的名字 ⇒ {名字: (装饰器行, 用例函数名)}。"""
    out = {}
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in n.decorator_list:
            if not (isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == "parametrize"
                    and len(dec.args) >= 2):
                continue
            a2 = dec.args[1]
            if isinstance(a2, ast.Name):
                out[a2.id] = (getattr(dec, "lineno", n.lineno), n.name)
            elif (isinstance(a2, ast.Call) and getattr(a2.func, "id", "") in ("sorted", "list", "tuple")
                    and a2.args and isinstance(a2.args[0], ast.Name)):
                out[a2.args[0].id] = (getattr(dec, "lineno", n.lineno), n.name)
    return out


def _scoped_mutations(tree, names) -> dict:
    """{name: [(line, kind, scope)]}；scope='module' 表示 import 期就会执行（**有效**）。"""
    out = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for n in ast.walk(fn):
            if n is fn:
                continue
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in MUTATORS:
                owner = getattr(getattr(n.func, "value", None), "id", "")
                if owner in names:
                    out.setdefault(owner, []).append((n.lineno, "%s()" % n.func.attr, fn.name))
            if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id in names:
                out.setdefault(n.target.id, []).append((n.lineno, "增强赋值", fn.name))
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Subscript) and getattr(t.value, "id", "") in names:
                        out.setdefault(t.value.id, []).append((n.lineno, "NAME[k]=v", fn.name))
    for n in tree.body:                      # 模块级 = import 期执行 ⇒ 有效
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in MUTATORS:
            owner = getattr(getattr(n.func, "value", None), "id", "")
            if owner in names:
                out.setdefault(owner, []).append((n.lineno, "%s()" % n.func.attr, "module"))
        if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id in names:
            out.setdefault(n.target.id, []).append((n.lineno, "增强赋值", "module"))
    return out


def frozen_list_sites(src: str) -> list:
    """R1：绑定的清单在**非模块级**被变异（import 时不执行）⇒ 返回 [(清单名, 装饰器行, [(行,形态,作用域)])]。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    bound = _bound_names(tree)
    if not bound:
        return []
    scoped = _scoped_mutations(tree, set(bound))
    out = []
    for nm, evs in sorted(scoped.items()):
        bad = [(ln, k, sc) for ln, k, sc in evs if sc != "module"]
        if bad:
            out.append((nm, bound[nm][0], bad))
    return out


def orphan_append_sites(src: str) -> list:
    """R2：被追加的名字**没被任何 parametrize 绑定**，但与某个绑定名"名字相近" ⇒ 可能是"追加到另一个清单"。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    bound = set(_bound_names(tree))
    if not bound:
        return []
    allmut = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in MUTATORS:
            owner = getattr(getattr(n.func, "value", None), "id", "")
            if owner:
                allmut.setdefault(owner, []).append(n.lineno)
    out = []
    for nm, lines in sorted(allmut.items()):
        if nm in bound:
            continue
        if any(nm.startswith(b + "_") or b.startswith(nm + "_") for b in bound):
            out.append((nm, lines, sorted(bound)))
    return out


def scan_repo() -> list:
    hits = []
    for dp, dn, fn in os.walk(os.path.join(ROOT, "tests")):
        if "__pycache__" in dp:
            continue
        for f in sorted(fn):
            if not (f.startswith("test_") and f.endswith(".py")):
                continue
            rel = os.path.relpath(os.path.join(dp, f), ROOT).replace(os.sep, "/")
            if rel.endswith("test_frozen_list_detector_20261008.py"):
                continue                       # 本文件自带正/反例源码串，跳过自己
            try:
                src = io.open(os.path.join(dp, f), encoding="utf-8-sig", errors="replace").read()
            except Exception:  # noqa: BLE001
                continue
            for nm, dec, bad in frozen_list_sites(src):
                hits.append("R1 %s :: %s @L%d 非import期变异 %s"
                            % (rel, nm, dec, "; ".join("L%d %s@%s" % t for t in bad)))
            for nm, lines, bound in orphan_append_sites(src):
                hits.append("R2 %s :: 追加到 %s（L%s）而未绑定（绑定=%s）"
                            % (rel, nm, ",".join(map(str, lines)), bound))
    return hits


def _collect_count(path: str) -> int:
    r = subprocess.run([sys.executable, "-m", "pytest", path, "--collect-only", "-q",
                        "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    return len([ln for ln in (r.stdout or "").splitlines() if "::" in ln])


def _def_count(path: str) -> int:
    tree = ast.parse(io.open(path, encoding="utf-8-sig", errors="replace").read())
    return sum(1 for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_"))


# ── 复现样例 ──────────────────────────────────────────────────────────────
#: **真族（R1）**：追加写在函数里 ⇒ import 时不执行 ⇒ 那两项**永远不进参数集**
BAD_R1 = '''# -*- coding: utf-8 -*-
import pytest
CASES = [("a", 1), ("b", 2)]

def _add_more():                 # 没有任何地方在 import 期调用它
    CASES.append(("c", 3))
    CASES.extend([("d", 4)])

@pytest.mark.parametrize("name,n", CASES)
def test_each(name, n):
    assert n >= 0
'''

#: **反例**：模块顶层、装饰器**之后**追加 ⇒ 实测**仍会被收集**（引用语义）
GOOD_AFTER = '''# -*- coding: utf-8 -*-
import pytest
CASES = [("a", 1), ("b", 2)]

@pytest.mark.parametrize("name,n", CASES)
def test_each(name, n):
    assert n >= 0

CASES.append(("c", 3))           # import 期执行 ⇒ collection 时可见 ⇒ **有效**
CASES += [("d", 4)]
'''


def test_C1_非import期追加必须被报出():
    hits = frozen_list_sites(BAD_R1)
    assert hits, "检测器没报出『追加写在函数里（import 时不执行）』⇒ 判据恒真"
    nm, dec, bad = hits[0]
    assert nm == "CASES" and all(sc != "module" for _l, _k, sc in bad), hits
    assert {k for _l, k, _s in bad} == {"append()", "extend()"}, hits


def test_C1_实测_真族确实漏收集(tmp_path):
    """⭐ 硬测量：把真族样例写下来跑 `--collect-only` ⇒ 收集 **2** 条，而 import 后清单长度 **4** ⇒ 漏 **2**。"""
    f = tmp_path / "test_repro_r1.py"
    f.write_text(BAD_R1, encoding="utf-8")
    assert _collect_count(str(f)) == 2, "真族样例应只收集 2 条（函数里的追加不生效）"


def test_C2_反例_模块顶层装饰器之后的追加不得被报(tmp_path):
    """⭐ **更正前提的那条**：模块顶层、装饰器之后的追加 **不是**漏收集形态（引用语义）——
    检测器**不得**报它，且实测**收集数 = 4**（全被收集）。"""
    assert frozen_list_sites(GOOD_AFTER) == [], "把『import 期追加』误报成冻结风险 ⇒ 检测器过宽"
    f = tmp_path / "test_repro_c2.py"
    f.write_text(GOOD_AFTER, encoding="utf-8")
    assert _collect_count(str(f)) == 4, "模块顶层追加在 collection 时可见 ⇒ 应收集 4 条"


def test_C3_牙齿_只数def会与收集数对不上(tmp_path):
    """牙齿：`def test_*` = 1，而收集 = 4（参数化展开）⇒ 若谁把测量从"收集数"换成"def 数"，立刻对不上。"""
    f = tmp_path / "test_repro_teeth.py"
    f.write_text(GOOD_AFTER, encoding="utf-8")
    defs, collected = _def_count(str(f)), _collect_count(str(f))
    assert defs == 1 and collected == 4, (defs, collected)
    assert defs != collected, "def 数竟然等于收集数 ⇒ 牙齿失去判别力"


def test_C4_全仓不得有修正族的站点():
    """仓库级保险丝：R1/R2 命中数必须为 0（当前实测 0；将来谁写出来 ⇒ 本条红）。"""
    hits = scan_repo()
    assert hits == [], (
        "发现『绿但没跑』风险（t128/G9R-14 族）：R1=追加写在 import 时不执行的位置；"
        "R2=追加到另一个名字。修法：把追加移到**装饰器之前的模块顶层**，或放进**另一个清单**并另开一组 "
        "parametrize（t125 在 test_help_context 的形态）。命中：%s" % hits[:6])


#: ⭐ **正确形态的自证**：本文件自己用"模块顶层清单 + 装饰器之前就完整"的写法
#: （C4 自闭环同时断言：它**没有** R1/R2 站点）。
DETECTOR_SELF_CASES = [
    ("append", "装饰器之后 append（函数内 ⇒ 会漏）"),
    ("extend", "装饰器之后 extend（函数内 ⇒ 会漏）"),
    ("update", "追加到另一个名字"),
]


@pytest.mark.parametrize("kind,_desc", DETECTOR_SELF_CASES)
def test_self_cases_are_bound_before_decorator(kind, _desc):
    """演示"正确形态"：模块顶层清单在装饰器之前已完整 ⇒ 全部被收集（本文件自证用）。"""
    assert kind in ("append", "extend", "update")


def test_C4_自闭环_本文件自己不得被这一族咬到():
    """⚠️ 队长提醒：本任务的产出也可能被同族咬到 ⇒ 结构地自证：
    ① 本文件没有 R1/R2 站点；② 本文件**确实**用了『parametrize 绑定模块级清单』（否则①是空的）。"""
    src = io.open(os.path.abspath(__file__), encoding="utf-8-sig").read()
    assert frozen_list_sites(src) == [] and orphan_append_sites(src) == [], (
        "本判据文件自己就有该族形态 ⇒ 自闭环失败")
    tree = ast.parse(src)
    mods = set(_module_lists(tree))
    bound = set(_bound_names(tree))
    assert bound & mods, ("本文件没有 parametrize 绑定模块级清单 ⇒ 上一条自证是空的",
                          sorted(bound), sorted(mods))
