# -*- coding: utf-8 -*-
"""G10R5/t137：`memory_perceive` 直写路径必须给 FTS 留下**可检索的明文**，且**只许用掩码后文本**。

背景（实测链）：
  · FTS 列 = `COALESCE(new.tokenized_content, new.content)`（`sqlite/_schema.py:499-500`）；
  · `tokenized_content` 为空 ⇒ 回退到 `content`；而该路径的 `content` 是**密文**（`:336 _enc_content`）
    ⇒ ⭐ **该行内容检索不可见**（实测 18 条 active 行，全 `perception`）。
  · 修法 = 调用既有 helper `adapters/sqlite/_crypto.py:85 _tokenized_for_storage`。

⚠️ **隐私前提（t137 前半段已取证，带 file:line）**：本路径**唯一的掩码点**是
`adapter_pii_guard(_plain_signal, None)`；`_mem_content`（→`content`）与 `content_hash` 都在它**之后**。
⭐ 因此 helper **必须传 `_mem_plain`（掩码后）**：传原文 `_plain_signal` 会把"检索不可见"换成
**"明文 PII 落库"**（更糟）⇒ 本判据把这条**钉成结构性约束**。

⚠️ **本判据的属性（如实声明）**：这是**源码结构判据**（AST/文本级），不是端到端运行时判据；
端到端需要在**真 PG**上发一次 perceive 才能验（本任务禁止写生产 PG）⇒ 运行时那一半**未测**。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ROUTER = REPO / "trinity" / "api" / "server" / "_routers_brain.py"


def _perceive_segment(src: str) -> str:
    """取 `memory_perceive` 函数体（到下一个顶层 `def`/`async def` 为止）。"""
    m = re.search(r"^(async def|def)\s+memory_perceive\b", src, re.M)
    assert m, "找不到 memory_perceive（判据前提失效）"
    rest = src[m.start():]
    nxt = re.search(r"^(async def|def)\s+\w+", rest[1:], re.M)
    return rest[: (nxt.start() + 1)] if nxt else rest


def _violations(src: str) -> list:
    """结构性违规清单（空 = 合规）。抽成函数是为了让牙齿能喂"被改坏"的源码。"""
    bad = []
    seg = _perceive_segment(src)
    # ① INSERT INTO memories 必须带 tokenized_content
    m = re.search(r"INSERT INTO memories.*?SELECT", seg, re.S)
    if not m:
        bad.append("找不到 `INSERT INTO memories` 段")
    elif "tokenized_content" not in m.group(0):
        bad.append("`INSERT INTO memories` 的列里**没有 `tokenized_content`** ⇒ FTS 会回退到密文 ⇒ 行不可见")
    # ② 必须调用 helper
    if "_tokenized_for_storage(" not in seg:
        bad.append("没有调用 `_tokenized_for_storage` ⇒ 该路径仍不写 tokenized_content")
    else:
        # ③ helper 的**第一个实参**必须是 _mem_plain（掩码后）——绝不许 _plain_signal（原文）
        call = re.search(r"_tokenized_for_storage\(\s*([A-Za-z_][A-Za-z0-9_]*)", seg)
        if not call:
            bad.append("无法解析 `_tokenized_for_storage` 的第一个实参（判据前提失效）")
        elif call.group(1) != "_mem_plain":
            bad.append("`_tokenized_for_storage` 的第一个实参是 `%s`，**必须是 `_mem_plain`（掩码后）**"
                       "—— 传原文等于明文 PII 落库" % call.group(1))
        if re.search(r"_tokenized_for_storage\(\s*_plain_signal", seg):
            bad.append("**危险用法**：把原文 `_plain_signal` 传进了 helper")
    # ④ 顺序：掩码必须先于 tokenize（隐私前提的机械化表达）
    i_guard = seg.find("adapter_pii_guard(")
    i_tok = seg.find("_tokenized_for_storage(")
    if i_guard == -1:
        bad.append("找不到 `adapter_pii_guard(` ⇒ 掩码前提不成立")
    elif i_tok != -1 and not (i_guard < i_tok):
        bad.append("`adapter_pii_guard` **晚于** `_tokenized_for_storage` ⇒ 可能把原文写进 FTS")
    # ⑤ helper 调用不得破坏写入：必须在 try/except 兜底内（helper 不可用不阻断写）
    if "_tokenized_for_storage(" in seg and "except Exception" not in seg[: i_tok + 400]:
        bad.append("helper 调用未见 try/except 兜底 ⇒ helper 故障可能阻断写入")
    return bad


def test_router_parses_and_members_intact():
    """四件套之一：文件能 parse，且**原有成员仍在原位**（防锚点改动吃掉邻接定义）。"""
    src = ROUTER.read_text(encoding="utf-8")
    ast.parse(src)                                    # 语法必须过
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "memory_perceive" in names, "`memory_perceive` 不在原位（被改坏了）"
    for probe in ("_plain_signal = str(signal)[:800]", "adapter_pii_guard(_plain_signal, None)",
                  "_mem_content = _enc_content(_mem_plain)", "INSERT INTO perceptions",
                  "backfill_signal_async(str(signal))"):
        assert probe in src, "原有成员/语句被改动：%r" % probe


def test_perceive_writes_plaintext_tokenized_and_only_masked():
    """核心判据：① 写 `tokenized_content`；② 输入是**掩码后** `_mem_plain`；③ 掩码在先。"""
    src = ROUTER.read_text(encoding="utf-8")
    assert _violations(src) == [], "结构性违规：%r" % _violations(src)


def test_reverse_other_categories_untouched():
    """反向：本改动**只**动 `memory_perceive`，其它写入路径（适配器/客户端）不受影响。"""
    src = ROUTER.read_text(encoding="utf-8")
    seg = _perceive_segment(src)
    # helper 调用必须**只**出现在该函数体内（全文件计数 == 段内计数）
    assert src.count("_tokenized_for_storage(") == seg.count("_tokenized_for_storage("), (
        "`_tokenized_for_storage` 在该文件其它位置也被调用了 ⇒ 改动溢出到别的路径")
    # 适配器层（session/procedural 等的写入者）不受本改动影响：它本来就调 helper
    crud = (REPO / "trinity" / "adapters" / "sqlite" / "_crud.py").read_text(encoding="utf-8")
    assert "_tokenized_for_storage" in crud, (
        "适配器层不再调用 helper（= 本改动的前提/邻接被破坏）⇒ 需人工复核")


def test_tooth_removing_helper_must_fail():
    """牙齿：把 helper 调用摘掉 ⇒ 核心判据必须红（在 pytest.raises 里显式演示）。"""
    src = ROUTER.read_text(encoding="utf-8")
    broken = re.sub(r"\n\s*_mem_tok = _SQLA\._tokenized_for_storage\(", "\n        _mem_tok = (", src, count=1)
    broken = broken.replace("tokenized_content, importance", "importance")     # 同时把列摘掉
    with pytest.raises(AssertionError):
        assert _violations(broken) == [], "摘掉 helper 后本判据应红，实测违规：%r" % _violations(broken)


def test_tooth_passing_raw_signal_must_fail():
    """牙齿②：把 helper 的第一个实参改成原文 `_plain_signal` ⇒ 必须红（**这是隐私护栏的牙齿**）。"""
    src = ROUTER.read_text(encoding="utf-8")
    broken = src.replace("_SQLA._tokenized_for_storage(\n                    _mem_plain",
                         "_SQLA._tokenized_for_storage(\n                    _plain_signal", 1)
    assert broken != src, "未能在源码里构造出`传原文`的坏例子（判据前提失效）"
    with pytest.raises(AssertionError):
        assert _violations(broken) == [], "传原文时本判据应红，实测违规：%r" % _violations(broken)
