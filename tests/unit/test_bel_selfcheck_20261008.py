# -*- coding: utf-8 -*-
"""G31/t174 ③ 判据面：**BEL（U+0007）自检**的可失败断言（含人造牙齿与反向）。

背景（本轮真事）：`tests/unit/test_memory_id_integrity_20261008.py` 里曾出现 **2 个 BEL**，
成因 = **非 raw 字面量里 `\\a` 被解析**（`"...；\\agent_id…"` ⇒ `；\\x07gent_id`）；
字节级守卫（`test_text_integrity_20261006.py` 的 `control_chars`）当时**抓住了它**。
本判据是**同一件事的随手自检形态**：只读计数 + 定位，**零风险**。

为什么它不会恒绿（牙齿）：`test_C2` 用**人造含 BEL 的字节**喂同一个探测器 ⇒ 必须报出 `line:col`。
为什么它不会假阳（反向）：`test_C3` 喂**字面 `\\a` 两字符**（不是 BEL 字节）⇒ 探测器**不得**报。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BEL_CHECK = os.path.join(ROOT, "scripts", "bel_check.py")


def _load():
    """按**路径**加载 `scripts/bel_check.py`（与 `test_mkdtemp_cleanup.py` 同一手法）。"""
    spec = importlib.util.spec_from_file_location("_g31_bel_check", BEL_CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_C1_改动文本文件不得引入BEL():
    """**正向**：`git status` 的改动/未跟踪文本文件里 —— **本轮【引入】的 BEL 必须为 0**。

    ⚠️ 口径 = **只判引入**（与 `test_text_integrity_20261006.py` 的既有纪律一致）：
      · 已跟踪文件：把当前字节与 `git show HEAD:<path>` 对比 ⇒ 只有当 **当前 > HEAD** 才算"引入"；
      · 未跟踪文件（`??`）：没有 HEAD ⇒ 里面有 BEL 就是"引入"；
      · **存量 BEL**（两边相等且 >0）**列出但不判红** —— 不隐藏，但不把它算成本轮的账。
    """
    m = _load()
    files = m.changed_files()
    introduced, pre_existing, unreadable = [], [], []
    for p in files:
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        hits, err = m.scan_file(p)
        if err:
            unreadable.append("%s: %s" % (rel, err))
            continue
        if not hits:
            continue
        head = subprocess.run(["git", "show", "HEAD:%s" % rel], cwd=ROOT, capture_output=True).stdout
        n_head = head.count(b"\x07")
        n_cur = len(hits)
        if n_cur > n_head:
            introduced.append("%s: HEAD=%d 现=%d 首处 %s:%d:%d"
                              % (rel, n_head, n_cur, rel, hits[0][0], hits[0][1]))
        else:
            pre_existing.append("%s: BEL=%d（HEAD 也是 %d ⇒ 存量，不计红）" % (rel, n_cur, n_head))
    print("[CHK] 时点 %s | 扫描改动/未跟踪文本文件 %d 个 | 存量 BEL 文件 %d 个 | 不可读 %d 个"
          % (time.strftime("%Y-%m-%d %H:%M:%S"), len(files), len(pre_existing), len(unreadable)))
    for x in pre_existing:
        print("[CHK]   存量：%s" % x)
    assert unreadable == [], ("有文件不可读 ⇒ UNTESTABLE（不得当通过）", unreadable)
    assert introduced == [], (
        "改动文件里**引入了** U+0007(BEL) ⇒ 多半是**非 raw 字面量里的 \\a 被解析**；"
        "定位：file:line:col；修复：**字节级** replace(b'\\x07', b'a')（⛔ 不要 shell 编码往返）。命中：%s" % introduced)


def test_C2_牙齿_人造BEL必须被报出():
    """⭐ **牙齿**：人造字节（含 BEL）⇒ 探测器必须报，且**行列号正确** ⇒ 证明不是恒绿。"""
    m = _load()
    raw = "#: 前缀 **hermes_sync_***；\x07gent_id = 空\n第二行无问题\n".encode("utf-8")
    hits = m.find_bel_bytes(raw)
    assert hits, "人造 BEL 没被报出 ⇒ 探测器恒绿（牙齿失效）"
    ln, col, ctx = hits[0]
    assert ln == 1, ("行号应为 1", hits)
    assert "<BEL>" in ctx, ("上下文里应把 BEL 显式标出（便于人读与定位）", hits)
    assert col == len("#: 前缀 **hermes_sync_***；".encode("utf-8")) + 1, ("列号（字节列）应为 BEL 的位置", col)


def test_C3_反向_字面反斜杠a不得被误报():
    """**反向**：`\\a` 作为**两个可打印字符**（backslash + a）⇒ **不得**报（防假阳）。"""
    m = _load()
    raw = "raw = r\"\\agent_id\"   # 这是两个字面字符，不是 BEL\n".encode("utf-8")
    assert b"\x07" not in raw
    assert m.find_bel_bytes(raw) == [], "把字面 `\\a` 误判成 BEL ⇒ 探测器假阳"


def test_C4_零风险与定位法自证():
    """**零风险自证**：探测器只读字节、不修改文件；且脚本对不存在的文件报"不可读"（rc=2 语义）。"""
    m = _load()
    hits, err = m.scan_file(os.path.join(ROOT, "scripts", "bel_check.py"))
    assert err is None and hits == [], "脚本源码自身不应含 BEL"
    before = os.path.getmtime(BEL_CHECK)
    m.find_bel_bytes(open(BEL_CHECK, "rb").read())
    assert os.path.getmtime(BEL_CHECK) == before, "探测器竟然改了文件时间戳 ⇒ 不是只读"
    missing = os.path.join(ROOT, "does_not_exist_g31.txt")
    if os.path.exists(missing):                      # 防御性：绝不该存在
        pytest.fail("测试前提被破坏：%s 意外存在" % missing)
    hits2, err2 = m.scan_file(missing)
    assert hits2 is None and err2 is not None, "不存在的文件必须报错（UNTESTABLE），而不是静默通过"
