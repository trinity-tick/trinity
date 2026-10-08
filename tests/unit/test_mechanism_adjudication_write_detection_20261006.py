# -*- coding: utf-8 -*-
"""R-1 判据：`scripts/mechanism_adjudication.py` 的"写能力"判定必须是**结构性**的。

## 缺陷（2026-10-06 T1 测试归因轮 · 队长裁定 R-1）

原实现：

```python
WRITE_RX = re.compile(r"json[.]dump|write_text|_save_state|save_state")
...
if not WRITE_RX.search(txt):      # txt = 整个文件的**文本**
    continue
```

⇒ **注释/文档字符串里写出这几个词**的模块会被判成"有写入能力"。
这一处格外刺眼，因为它是"**判据被注释骗**"的直接实例，而且
`docs/GATE_WIRING.json::_method.write_path_detection` 记的**也是**"源码正则"口径 ——
也就是说："我认为哪个脚本能进 CI"这件事，建立在会被注释骗的判定上。

## 修法与判据

`_write_calls()`：只看 `ast.Call` 节点（`json.dump` / `*.dump` / `write_text` /
`write_bytes` / `save_state` / `_save_state` / `open(..., "w"|"a"|"x"|"+")`）。
注释与 docstring 在 AST 里不产生 Call 节点 ⇒ 骗不到。

本文件：① 只有注释含关键词 ⇒ **不得**判为写者；② 真写调用 ⇒ 必须判出；
③ 端到端（`_writers` 走完整判定链）两条都要成立；④ **反事实**：把判定换回原全文正则
⇒ 同一个"只有注释"的文件**会**被骗（证明本用例承重，而不是自说自话）。
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "mechanism_adjudication.py"

#: 原实现的全文正则（**只用于反事实**，不参与任何判定）
_LEGACY_WRITE_RX = re.compile(r"json[.]dump|write_text|_save_state|save_state")


def _mod():
    spec = importlib.util.spec_from_file_location("_mech_adj_r1", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["_mech_adj_r1"] = m
    spec.loader.exec_module(m)
    return m


_COMMENT_ONLY = '''# -*- coding: utf-8 -*-
"""这个模块**只是提到** json.dump / write_text，并不真的写任何东西。"""
import os

STATE = os.path.expanduser("~/.trinity/state")
OUT = os.path.join(STATE, "r1_probe_state.json")     # 后面会 json.dump 它（这句是注释）


def main():
    # write_text 也只是出现在注释里
    return OUT
'''

_REAL_WRITER = '''# -*- coding: utf-8 -*-
import json
import os

STATE = os.path.expanduser("~/.trinity/state")
OUT = os.path.join(STATE, "r1_probe_state.json")


def main(payload):
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return OUT
'''


def test_只有注释含关键词的模块不得被判为写者(tmp_path) -> None:
    m = _mod()
    assert m._write_calls(_COMMENT_ONLY) == [], (
        "只有注释/文档字符串提到 `json.dump`/`write_text` 就被判成写调用 ⇒ "
        "R-1 没修好：%r" % (m._write_calls(_COMMENT_ONLY),))
    # 反事实：原实现（全文正则）在**同一个字符串**上必然命中 —— 本用例因此承重
    assert _LEGACY_WRITE_RX.search(_COMMENT_ONLY), (
        "反事实不成立：原全文正则本该被这个 fixture 骗到")


def test_真写调用必须被判出来() -> None:
    m = _mod()
    writes = m._write_calls(_REAL_WRITER)
    assert any(w.endswith("dump") for w in writes), (
        "真的 `json.dump(...)` 调用没被判出来 ⇒ 收紧过头：%r" % (writes,))
    assert any(w.startswith("open(") for w in writes), (
        "`open(..., 'w')` 也是写，没判出来：%r" % (writes,))


def test_端到端_写成员的判定链不得被注释骗(tmp_path, monkeypatch) -> None:
    """`_writers()` 是真正被裁定产物使用的入口，两条都要成立。"""
    m = _mod()

    def _fake_files(comment_only: bool):
        d = tmp_path / ("co" if comment_only else "rw")
        d.mkdir(exist_ok=True)
        p = d / "probe_state_writer.py"
        p.write_text(_COMMENT_ONLY if comment_only else _REAL_WRITER, encoding="utf-8")
        return lambda: [str(p)]

    monkeypatch.setattr(m, "_py_files", _fake_files(True))
    assert m._writers("r1_probe_state.json") == [], (
        "端到端仍被注释骗：只有注释含关键词的模块被判成了该状态文件的写者")

    monkeypatch.setattr(m, "_py_files", _fake_files(False))
    writers = m._writers("r1_probe_state.json")
    assert [w["file"] for w in writers] == ["../co/probe_state_writer.py"] or writers, (
        "端到端漏掉了真的写者（收紧过头）")
    assert any("write_calls" in w for w in writers), (
        "裁定产物里应带上 AST 证据（write_calls），否则复核者无从判断：%r" % (writers,))


def test_编译不过的文件不得被当成写者() -> None:
    m = _mod()
    assert m._write_calls("def broken(:\n    json.dump(x, y)\n") == [], (
        "语法错的文件被硬解析成'有写调用' —— 语法错由 script_compile_gate 管，这里不该猜")
