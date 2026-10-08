# -*- coding: utf-8 -*-
"""t18：语料向量索引落盘的**开关不能被绕过**（`save_now()` 自身必须看开关）。

## 背景（现场归属的产物，不是洁癖）

2026-10-06 查 `~/.trinity/data/corpus_vec.*` 的写入者时发现：`_enabled()`（`TRINITY_CORPUS_INDEX_PERSIST=0`）
**只被两个调用者**看过 —— `maybe_save()`（`:186`）与 `_on_exit()`（`:67`），
而 `save_now()` 自身不看 ⇒ **任何直接调用 `save_now()` 的路径都会绕过开关**。
今天所有调用点都被门住，所以开关有效；但这是"门禁可被绕过"的典型形态。

（同一次现场调查的另一半结论：该文件**另有常驻写入者**（维护链 prewarm），
所以"mtime 未变"不能作为"我没写"的取证 —— 见 `ACCESS-COUNT-SINGLE-COUNT.md` §9。）
"""
from __future__ import annotations

import os
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
PERSIST_PY = REPO / "trinity" / "core" / "client" / "_corpus_persist.py"


class _FakeIndex:
    """最小索引替身：`size()` 非零（否则 `save_now` 会在更早的分支返回）。"""

    def __init__(self, boom: bool = True):
        self.saved = []
        self._boom = boom

    def size(self) -> int:
        return 3

    def save(self, path):
        self.saved.append(path)
        if self._boom:                    # 真被写到盘就炸，用来证明"写路径是活的"
            raise AssertionError("开关关掉时不得落盘（save 被调用了）")
        return True


def _persist():
    from trinity.core.client import _corpus_persist as cp

    return cp


def test_开关关掉时save_now必须不写盘(monkeypatch):
    """**核心判据**：`TRINITY_CORPUS_INDEX_PERSIST=0` 时直调 `save_now()` ⇒ `False` 且不落盘。"""
    cp = _persist()
    monkeypatch.setenv("TRINITY_CORPUS_INDEX_PERSIST", "0")
    idx = _FakeIndex()
    monkeypatch.setitem(cp._state, "idx", idx)

    assert cp.save_now() is False, "开关关掉时 save_now() 必须返回 False"
    assert idx.saved == [], "开关关掉时不得调用 idx.save()：%r" % idx.saved


def test_负向实测_把守卫摘掉写路径就会真的被走到(monkeypatch):
    """**负向实测（牙齿）**：把守卫摘掉（`_enabled()` 恒真）后，`save_now()` 必须真的去落盘。

    这条证明"上面那条 `idx.saved == []` 之所以成立，是因为守卫拦住了"，
    而不是因为探针本身走不到写路径。

    注意 `save_now()` 对落盘异常是**吞掉**的（"落盘尽力而为"），所以这里断言的是
    **写路径被走到**（`idx.saved` 非空），而不是"抛异常"。
    """
    cp = _persist()
    monkeypatch.setenv("TRINITY_CORPUS_INDEX_PERSIST", "0")
    monkeypatch.setattr(cp, "_enabled", lambda: True)      # ← 摘掉守卫
    idx = _FakeIndex()
    monkeypatch.setitem(cp._state, "idx", idx)

    cp.save_now()
    assert idx.saved, "摘掉守卫后写路径必须可达（否则本负向实测没测到东西）"


def test_守卫在源码里_且save_now自身看开关():
    """结构哨兵：`save_now()` 函数体内必须出现 `_enabled()` 检查。

    用 AST 取 `save_now` 的源码段，避免"注释里写了"被当成"实现了"。
    """
    import ast

    src = PERSIST_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "save_now"]
    assert fn, "找不到 save_now 定义"
    seg = ast.get_source_segment(src, fn[0]) or ""
    assert "_enabled()" in seg, "save_now() 自身必须检查 _enabled()（否则可被绕过）"
    assert "not _enabled()" in seg, "检查形态应为 `if not _enabled(): return False`"
    # 牙齿：把守卫行删掉，同一侦测器必须报否
    mutated = seg.replace("not _enabled()", "True", 1)
    assert "not _enabled()" not in mutated, "变异体没变 ⇒ 本用例的侦测器无效"


# ── 2026-10-06（t21，verifier 抓到的**可绕过口**）：守卫必须"钉死"，不能 setdefault ──

CONFTEST_PY = REPO / "tests" / "unit" / "conftest.py"
_ENV_VAR = "TRINITY_CORPUS_INDEX_PERSIST"


def test_守卫是硬赋值_外部设1也必须被覆盖():
    """**核心判据（t21）**：`conftest.py` 必须把它**硬赋值**为 `"0"`。

    成因（verifier 实测 + 我复现）：原用 `setdefault`，而
    `dsh-ops/trinity-supervisor.ps1:247` 会设 `TRINITY_CORPUS_INDEX_PERSIST=1`
    ⇒ 在 supervisor 环境里跑测试，守卫被**静默绕过**、测试会去写生产语料缓存
    （我实测：外部预设 =1 跑一批真 hybrid 测试 ⇒ manifest mtime 18:52:57 → 19:32:42）。
    """
    # ① 本进程内：值必须是 "0"（conftest 已把外部值覆盖掉）
    assert os.environ.get(_ENV_VAR) == "0", (
        "conftest 没有把开关钉死为 0（当前 %r）⇒ 外部 env 可绕过守卫" % os.environ.get(_ENV_VAR))

    # ② 结构层：conftest 必须用**硬赋值**这条形态，且不得对该变量用 setdefault
    src = CONFTEST_PY.read_text(encoding="utf-8")
    assert 'os.environ["%s"] = "0"' % _ENV_VAR in src, (
        "conftest 里找不到硬赋值 `os.environ[\"%s\"] = \"0\"`" % _ENV_VAR)
    assert 'setdefault("%s"' % _ENV_VAR not in src, (
        "conftest 又用回了 setdefault ⇒ 外部 env 会静默绕过守卫")

    # ③ 牙齿：把硬赋值变异回 setdefault，同一侦测器必须报红
    mutated = src.replace('os.environ["%s"] = "0"' % _ENV_VAR,
                          'os.environ.setdefault("%s", "0")' % _ENV_VAR, 1)
    assert mutated != src, "变异体没变 ⇒ 本用例的插入点失效"
    broke = ('setdefault("%s"' % _ENV_VAR in mutated) \
        or ('os.environ["%s"] = "0"' % _ENV_VAR not in mutated)
    assert broke, "把硬赋值改回 setdefault 后侦测器没抓到 ⇒ 判据无效"


def test_用例内显式opt_in仍然生效(monkeypatch):
    """守卫是**默认**，不是**禁止**：用例自己显式 opt-in 必须仍然有效。

    （`monkeypatch.setenv` 在 conftest 之后执行 ⇒ 能覆盖硬赋值；这是"要测落盘的测试"
    唯一正当的写法：**在用例内、显式、可读**。）
    """
    cp = _persist()
    monkeypatch.setenv(_ENV_VAR, "1")
    assert cp._enabled() is True, "用例内显式 opt-in 失效 ⇒ 真需要落盘的测试将无法编写"
