# -*- coding: utf-8 -*-
r"""G27（t170）：**类被模块级 def 截断**的守卫 —— 三谓词（P2 / P1′ / P3）

## 这个坑的两次实证

· **t155 / G6R**：一个 **col-0 的模块级 `def`** 被插进类体范围之后 ⇒ 类体**提前结束** ⇒
  `PostgreSQLAdapter` 实测 **29 个抽象方法**未实现 ⇒ 类**无法实例化** ⇒ PG 写入路径被打断（40 分钟后才被撞上）。
· ⚠️ **而"只查类体内"会漏**：那次 col-0 的 `def` 落在**类结束行之后**（区间**外**）⇒
  ⇒ ⭐ 正确做法 = **两件都做**：运行时抓【后果】+ AST 抓【成因】。

## 三条谓词（扫描面 = `postgresql.py` + `trinity/adapters/_pg_*.py`）

| 谓词 | 角色 | 判据 |
|---|---|---|
| **P2 运行时** | 抓**后果** | `len(PostgreSQLAdapter.__abstractmethods__) == 0` |
| **P1′ AST** | 抓**成因** | **模块级 `def`** 出现在**该类 `end_lineno` 之后**，**且名字 ∈ 该类成员名集合** |
| ⭐ **P3 跨版本** | 抓"**方法被搬出类**"（最强） | **`HEAD` 类体方法名 ∩ 现在模块级 `def` 名** ⇒ 必须 0 |

⭐ **三件都做，且三者全 0 才算干净。**

⚠️ **P1 的假阳性教训（t162 实测，已内建到本判据）**：首版多了 `nm.startswith("_")` ⇒
把**模块级自由函数**也算进来 ⇒ 假阳 1 处（`_pg_search_enc.py:56 _row_dict`，首参是 `adapter` ⇒ 是辅助函数）。
⇒ ⭐ **"模块级 def + 下划线开头"不等于"被截断的方法"** —— **必须 `name ∈ 类成员集合`**（或用 P3 的跨版本比对）。
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = ROOT / "trinity" / "adapters"
SCAN = [ADAPTERS / "postgresql.py"] + sorted(ADAPTERS.glob("_pg_*.py"))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _head_text(rel: str) -> str:
    return subprocess.run(["git", "show", "HEAD:%s" % rel], cwd=str(ROOT),
                          capture_output=True).stdout.decode("utf-8", "replace")


def _cls_members(tree: ast.AST) -> set:
    """模块里**所有类体**的方法名集合。"""
    return {m.name for n in tree.body if isinstance(n, ast.ClassDef) for m in n.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _module_defs(tree: ast.AST):
    return [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _last_class_end(tree: ast.AST) -> int:
    ends = [n.end_lineno for n in tree.body if isinstance(n, ast.ClassDef) and n.end_lineno]
    return max(ends) if ends else 0


def p1_prime(src: str) -> list:
    """P1′：类结束行**之后**的模块级 def，且名字 ∈ 该类成员集合（⇒ 被截断的特征）。"""
    tree = ast.parse(src)
    names, last = _cls_members(tree), _last_class_end(tree)
    return [n.name for n in _module_defs(tree) if n.lineno > last and n.name in names]


def p3(src_now: str, src_head: str) -> list:
    """P3：`HEAD` 类体方法名 ∩ 现在模块级 def 名（= 事故形态本身）。"""
    if not src_head:
        return []
    head_names = _cls_members(ast.parse(src_head))
    now_mod = {n.name for n in _module_defs(ast.parse(src_now))}
    return sorted(head_names & now_mod)


# ── P2：运行时抓后果（子进程，避免污染本进程）────────────────────────────────

def test_P2_适配器必须可实例化() -> None:
    code = ("from trinity.adapters.postgresql import PostgreSQLAdapter as A;"
            "print(len(A.__abstractmethods__))")
    r = subprocess.run(["python", "-c", code], cwd=str(ROOT), capture_output=True, text=True)
    if r.returncode != 0:                      # 用当前解释器再试一次（PATH 里没有 python 时）
        import sys
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                           capture_output=True, text=True)
    assert r.returncode == 0, "导入 PostgreSQLAdapter 失败：%s" % (r.stderr or "")[-400:]
    n = int((r.stdout or "0").strip().splitlines()[-1])
    assert n == 0, (
        "⭐ P2 失败：PostgreSQLAdapter 有 %d 个未实现抽象方法 ⇒ 类不可实例化 ⇒ "
        "PG 写入路径会被打断（t155/G6R 的坑）" % n)


# ── P1′：AST 抓成因（扫描面 12 个文件）──────────────────────────────────────

def test_P1prime_不得出现类结束行之后的模块级_def() -> None:
    bad = {}
    for p in SCAN:
        if not p.exists():
            continue
        hits = p1_prime(_read(p))
        if hits:
            bad[str(p.relative_to(ROOT)).replace("\\", "/")] = hits
    assert not bad, (
        "⭐ P1′ 失败：以下文件出现『类结束行之后的模块级 def 且名字属该类』"
        "（= 类被截断的成因）：%s" % bad)


# ── P3：跨版本抓"方法被搬出类"（最强信号；本任务刚做过拆分 ⇒ 尤其相关）──────

def test_P3_HEAD_类体方法不得变成模块级_def() -> None:
    bad = {}
    for p in SCAN:
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if not p.exists():
            continue
        hits = p3(_read(p), _head_text(rel))
        if hits:
            bad[rel] = hits
    assert not bad, (
        "⭐ P3 失败：HEAD 里在类体内的方法，现在变成了**模块级 def**"
        "（= 把方法搬出类的事故形态；应放进 mixin 而不是模块级）：%s" % bad)


# ── 牙齿：把事故形态喂给 P1′ / P3 ⇒ 必须报 ──────────────────────────────────

def test_牙齿_截断形态必须被_P1prime_抓到() -> None:
    """人造"类结束后面的模块级 def，且名字属该类" ⇒ P1′ 必须报它。"""
    bad_src = ("class A:\n"
               "    def keep(self):\n"
               "        return 1\n"
               "    def moved(self):\n"
               "        return 2\n"
               "\n"
               "\n"
               "def moved(self):\n"     # ← col-0，落在类之后，名字属该类 ⇒ 截断形态
               "    return 3\n")
    assert "moved" in p1_prime(bad_src), "牙齿失败：被截断的形态没被 P1′ 抓到"
    # 对照：模块级**自由函数**（名字不属该类）**不得**误报（t162 的假阳性教训）
    ok_src = ("class A:\n"
              "    def keep(self):\n"
              "        return 1\n"
              "\n"
              "\n"
              "def _row_dict(adapter, row):\n"
              "    return dict(row)\n")
    assert p1_prime(ok_src) == [], "假阳性：模块级辅助函数被误报（P1 首版就是栽在这）"


def test_牙齿_搬出类的形态必须被_P3_抓到() -> None:
    head = ("class A:\n"
            "    def moved(self):\n"
            "        return 1\n")
    now = ("class A:\n"
           "    pass\n"
           "\n"
           "\n"
           "def moved(self):\n"
           "    return 1\n")
    assert p3(now, head) == ["moved"], "牙齿失败：方法被搬到模块级没被 P3 抓到"
