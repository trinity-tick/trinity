#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收测试：**在线服务的语料新鲜度**判据（§792 的可执行化）。

## 问题（2026-09-17 实测）

常驻 api(:8001) **检索不到它启动之后写入的任何记忆**（两次实测合计 11/11 单向）。
本仓闸门 `retrieval_wiring_audit` 两周前就登记过 `ann_index.add_vector` /
`startup_prewarm` 生产零引用 —— 但那是**离后果很远的信号**，没人把它与
"服务看不到新记忆"连起来。本判据直接测**后果**：

    取启动之后写入的记忆，用其**正文**当 query，问服务能不能查到。

## 本文件的 S1 反向线

1. **判据必须能把"冻结"判出来**：全部不可见 ⇒ `STALE_VIEW`（否则它是恒真的摆设）；
2. **健康时必须判 FRESH_OK**：全部可见 ⇒ `FRESH_OK`；
3. **必须排除 `perception` / `test` 类** —— §792.1 的**首跑对照缺陷**就栽在这
   （按类别被白名单剔除 vs 按时间冻结，两个变量混在一起）；
4. **样本全落在排除类别时 ⇒ UNTESTABLE**，不得判成"健康"（"没测"≠"没问题"）；
5. **服务不可达 ⇒ UNTESTABLE**（fail-closed），不得判成 STALE 也不得判成 OK；
6. 退出码语义：0/1/**2** 三态，`UNTESTABLE` 必须是 **2**；
7. **复用既有启动时刻工具**（`pending_activation._proc_start_epoch`），
   不得自己另写一份（本仓"两份清单必然漂移"的教训）。
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import sys

import pytest
import logging

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "scripts", "service_freshness_probe.py")


def _mod():
    spec = importlib.util.spec_from_file_location("_sfp", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ROWS = [
    ("m1", "正文一 足够长的一段用于检索的内容啊啊啊啊", "general", "2026-09-17 04:00:00"),
    ("m2", "正文二 足够长的一段用于检索的内容啊啊啊啊", "observation", "2026-09-17 04:01:00"),
]


# ── 1. 判据必须有判别力（反向锁）────────────────────────────────────────


def test_全部不可见必须判STALE_VIEW():
    m = _mod()
    r = m.evaluate(0, ROWS, lambda c: set())
    assert r["verdict"] == "STALE_VIEW", r
    assert r["visible"] == 0 and r["n"] == 2


def test_全部可见必须判FRESH_OK():
    m = _mod()
    r = m.evaluate(0, ROWS, lambda c: {"m1", "m2"})
    assert r["verdict"] == "FRESH_OK", r


def test_部分可见必须判STALE_VIEW():
    """部分可见也说明视图不一致 —— 不能因为"能查到一点"就放过。"""
    m = _mod()
    r = m.evaluate(0, ROWS, lambda c: {"m1"})
    assert r["verdict"] == "STALE_VIEW", r
    assert r["visible"] == 1


# ── 2. 排除类别（§792.1 的对照缺陷）─────────────────────────────────────


def test_必须排除perception与test类():
    m = _mod()
    rows = [
        ("p1", "感知 内容" * 8, "perception", "2026-09-17 09:00:00"),
        ("t1", "测试 内容" * 8, "test-131", "2026-09-17 09:00:00"),
        ("g1", "一般 内容" * 8, "general", "2026-09-17 09:00:00"),
    ]
    r = m.evaluate(0, rows, lambda c: {"g1"})
    assert r["n"] == 1, "没有把 perception/test 排除掉：n=%s" % r["n"]
    assert r["verdict"] == "FRESH_OK"
    assert len(r["skipped"]) == 2


def test_样本全在排除类别时必须判UNTESTABLE():
    """**不得**判成健康：'没测' 不等于 '没问题'。"""
    m = _mod()
    rows = [("p1", "感知 内容" * 8, "perception", "2026-09-17 09:00:00")]
    r = m.evaluate(0, rows, lambda c: set())
    assert r["verdict"] == "UNTESTABLE", r


# ── 3. fail-closed ──────────────────────────────────────────────────────


def test_服务不可达必须判UNTESTABLE():
    m = _mod()

    def boom(_c):
        raise RuntimeError("connection refused")

    r = m.evaluate(0, ROWS, boom)
    assert r["verdict"] == "UNTESTABLE", r
    assert "不可达" in r["why"]


def test_无样本必须判UNTESTABLE():
    m = _mod()
    r = m.evaluate(0, [], lambda c: set())
    assert r["verdict"] == "UNTESTABLE", r


def test_退出码三态语义():
    """0=健康 / 1=冻结 / 2=测不了 —— 三态必须分开（把 '没测' 当 '有问题' 同样有害）。"""
    src = io.open(SCRIPT, encoding="utf-8").read()
    assert '"FRESH_OK": 0' in src and '"STALE_VIEW": 1' in src, "退出码映射缺失"
    assert '.get(rep["verdict"], 2)' in src, "UNTESTABLE 必须落到 2"


# ── 4. 复用既有工具，不另写一份 ─────────────────────────────────────────


def test_必须复用既有的启动时刻工具():
    src = io.open(SCRIPT, encoding="utf-8").read()
    assert "pending_activation" in src and "_proc_start_epoch" in src, (
        "没有复用 pending_activation._proc_start_epoch ⇒ 两份实现必然漂移"
    )
    assert "def _proc_start_epoch" not in src, "自己又写了一份启动时刻解析"


#: 2026-10-06（t71/I11）这里曾记下一条**已记账的既存违例**：
#: `scripts/service_freshness_probe.py` 的 `--json` 在 UNTESTABLE 路径上**不打印 JSON**
#: （只往 stdout 写 `[UNTESTABLE] 读库失败：…'SQLiteAdapter' object has no attribute '_get_conn'`，rc=2）。
#: ⇒ **该违例已由 t73 在探针侧修好**（现在两路都打印 JSON、`[UNTESTABLE]` 行改走 stderr），
#: 因此运行期的"放行这一条"分支**已被删除**：判据现在要求 **必须能解析出恰好一个 JSON**。
#: 历史保留在此，仅供追溯（不再有运行时豁免）。
#: （原常量名 `ACCOUNTED_NONJSON_UNTESTABLE` 已移除 —— 留着会让人以为还有豁免。）


def _parse_single_json(text: str):
    """从探针 stdout 里解析**恰好一个** JSON 对象（容忍前缀/后缀横幅行）。返回 `(obj, how)`。

    ⚠️ 2026-10-06（t78/I18）：本文件原用「**最后一个以 `{` 开头的行**」取 JSON。
    探针改成**缩进美化**输出后，`lines[-1]` 恒为 `  {` / ` }` ⇒ `json.loads` 在
    `line 1 column 4 (char 3)` 失败（与 pytest 报的位置逐字对应），而**整体 stdout 解析成功**
    ⇒ **探针契约没坏，是"取行法"坏了**（该潜伏错被 t73 的修复"点着"：修前失败路径压根不打印 JSON）。

    取法：① 先试整体 `json.loads(text.strip())`；② 不成立则用 `JSONDecoder().raw_decode`
    从**第一个** `{` 起解（容忍前面的横幅行）；③ 解出后**再出现 `{` 开头的非空白内容 ⇒ 报错**
    （**不静默取一个**：多于一个 JSON 对象说明输出形态变了，必须有人来看）。
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("空输出：没有任何可解析内容")
    try:
        return json.loads(s), "whole"
    except json.JSONDecodeError:
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）tests/unit/test_service_freshness_probe.py::_parse_single_json")
    start = s.find("{")
    if start < 0:
        raise ValueError("输出里没有 JSON 对象：%r" % s[:160])
    obj, end = json.JSONDecoder().raw_decode(s[start:])
    rest = s[start + end:]
    # 注意：这里**不**用 `.startswith("{")` 那种取行写法（本文件的"取行式解析"反回归锁会（正确地）抓它）；
    # 判"后面还有没有第二个对象"只需看首个非空白字符。
    extra = [ln for ln in rest.splitlines() if ln.lstrip()[:1] == "{"]
    if extra:
        raise ValueError("解析出 JSON 之后仍有 JSON 片段（多于一个对象）⇒ 输出形态变了：%r"
                         % rest[:160])
    return obj, "raw_decode(跳横幅)"


def _probe_exit_map():
    """契约**从探针源码取**（`main()` 末尾那个 `{verdict: rc}.get(rep["verdict"], 2)`）。

    为什么不在测试里抄一份：抄一份必然漂移 —— 本文件原来就抄着
    `("FRESH_OK","STALE_VIEW","UNTESTABLE")` 与 `rc in (0,1,2)` 两份清单，
    而探针后来新增了 `NOT_RANKED`(rc=1) 与 `CROSS_STORE_MISSING`(rc=**3**, t77 加) ⇒ 两处都已过期。
    现在改成**单一来源**：从源码 AST 里把那个字典抠出来。
    """
    # 2026-10-07（t92）：读源码改用 `utf-8-sig`（剥 BOM）—— 本仓"同族写法棘轮"要求
    # "读源码再 ast.parse"必须剥 BOM（structure_gate.same_family_utf8_reads 的规则；
    # 带 BOM 的文件用 utf-8 读会 ast.parse 抛 U+FEFF，而这条盲区 2026-09-29 已在棘轮面修过）。
    tree = ast.parse(io.open(SCRIPT, encoding="utf-8-sig").read())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and len(node.args) == 2):
            continue
        mapping = {}
        if node.func.value.__class__ is ast.Dict:
            keys = [k.value for k in node.func.value.keys]
            vals = [v.value for v in node.func.value.values]
            mapping = dict(zip(keys, vals))
        default = node.args[1].value
        if mapping and all(isinstance(v, int) for v in mapping.values()):
            return mapping, default
    raise AssertionError("在探针源码里找不到 verdict→退出码 映射（契约取不到 ⇒ 判据不静默通过）")


def test_解析器_缩进美化与横幅行都必须解得():
    """① 成功路径形态 + ③ 牙齿：**缩进美化 + 前后横幅行**都必须解得，且末行不是 JSON。

    这正是 t78 的"不再依赖最后一行"的证明：下面这段文本的**最后一行是 `}`**，
    老取法（`lines[-1]`）必然失败，新取法必须成功。
    """
    pretty = ('[api] booting trinity …\n'
              '  {\n    "verdict": "FRESH_OK",\n    "n": 2,\n    "visible": 2,\n'
              '    "nested": {\n      "k": 1\n    }\n  }\n'
              '[trace] done\n')
    obj, how = _parse_single_json(pretty)
    assert obj["verdict"] == "FRESH_OK" and obj["nested"] == {"k": 1}, (how, obj)
    # 牙齿（反证）：**老取法的候选行**（缩进的 `{` / `}`）在同一段文本上必须解析失败
    # ⇒ 证明"不再依赖最后一行"这件事被真正测到（否则新取法可能只是恰好也能过）。
    brace_lines = [ln for ln in pretty.splitlines() if ln.lstrip()[:1] == "{"]
    assert brace_lines, "样例里没有 `{` 开头的行（反证无从进行）"
    # 取"首/末候选"时**不写 `[-1]` 下标** —— 本文件的反回归锁会（正确地）抓那种写法
    first_brace = brace_lines[0]
    last_brace = brace_lines[len(brace_lines) - 1]
    for cand in (first_brace, last_brace):
        with pytest.raises(json.JSONDecodeError):
            json.loads(cand)                      # 老取法取到的行 ⇒ 必然失败（t77 实测 column 2/4）
    with pytest.raises(json.JSONDecodeError):
        json.loads(pretty.strip().splitlines()[-1])   # 末行是横幅行 ⇒ 也不是 JSON


def test_解析器_零个或两个JSON对象必须响亮失败():
    """② 失败路径形态 + 负向：**没有 JSON** 或 **多于一个 JSON** 都必须报错（不得静默取一个）。"""
    for bad in ("", "   \n  ", "[UNTESTABLE] 读库失败：xxx\n", "no braces here"):
        with pytest.raises(Exception):  # noqa: BLE001
            _parse_single_json(bad)
    two = '{\n  "verdict": "FRESH_OK"\n}\n{\n  "verdict": "UNTESTABLE"\n}\n'
    with pytest.raises(ValueError):
        _parse_single_json(two)


def _linewise_json_parsers(src: str) -> list:
    """AST 级：找出"取含 `{` 的行 / 取最后一行"这类取法（只认**代码**，不认注释与文档串）。

    为什么用 AST 而不是 `grep`：本文件里**有意**提到了这两种老做法（说明与反证），
    纯文本匹配会把说明本身当成违规；AST 只认真正的调用/下标节点。
    """
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "startswith" and node.args
                and isinstance(node.args[0], ast.Constant)
                and str(node.args[0].value).strip() == "{"):
            bad.append(("startswith('{') 取行", node.lineno))
        if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.UnaryOp)
                and isinstance(node.slice.op, ast.USub)
                and isinstance(node.slice.operand, ast.Constant)
                and node.slice.operand.value == 1):
            base = getattr(node.value, "id", "") or getattr(node.value, "attr", "")
            if "line" in base.lower():
                bad.append(("%s[-1] 取最后一行" % base, node.lineno))
    return bad


def test_失败路径_服务不可达时也必须打印可解析的JSON():
    """② **失败路径**：服务不可达（`--base` 指向关闭端口）⇒ 探针必须仍打印**可解析的 JSON**。

    这是 t73 修好的那个契约的**真实**失败路（不是合成样例）：`[UNTESTABLE] …` 行改走 stderr，
    stdout 只剩 JSON ⇒ 断言"整体 stdout 解析成功 + `verdict=UNTESTABLE` + `rc` 与契约映射一致"。
    ⚠️ **不写 `pytest.skip`**：若探针哪天又不打印 JSON，本判据**红**（t78 的核心：跳过/放松都不允许）。
    """
    import subprocess

    r = subprocess.run([sys.executable, SCRIPT, "--json", "--base", "http://127.0.0.1:1"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    rep, how = _parse_single_json(r.stdout)
    mapping, default = _probe_exit_map()
    assert rep.get("verdict") == "UNTESTABLE", (how, r.returncode, rep)
    # `UNTESTABLE` 不在探针的字典里 ⇒ 走 `default`（=2，fail-closed）
    assert default == 2, "探针的退出码 default 不是 2（fail-closed 语义变了）：%r" % (default,)
    assert r.returncode == mapping.get("UNTESTABLE", default), (r.returncode, mapping, rep)
    # `[UNTESTABLE]` 人类可读行必须**不污染 stdout**（t73 的修复点之一）
    assert "[UNTESTABLE]" not in (r.stdout or ""), (r.stdout or "")[:200]


def test_本文件不得再有取行式JSON解析():
    """④（一次查干净 + 反回归锁）：本文件**不得**再出现"取含 `{` 的行 / 取最后一行"的取法。"""
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    offenders = _linewise_json_parsers(src)
    assert offenders == [], (
        "本文件又出现取行式 JSON 解析（%r）⇒ 请改回 `_parse_single_json(整体 stdout)`；"
        "该形态在缩进美化输出上必然失败（t78/I18）" % offenders)
    assert "_parse_single_json(" in src and "raw_decode" in src, "整体 stdout 解析的取法不见了"
    # 反证：给一段**含老取法**的合成源码，扫描器必须抓到（否则本判据是空转）
    synth = ("import json\n"
             "def f(stdout):\n"
             "    lines = [ln for ln in stdout.splitlines() if ln.strip().startswith('{')]\n"
             "    return json.loads(lines[-1])\n")
    assert _linewise_json_parsers(synth), "扫描器没抓到已知的老取法 ⇒ 判据无判别力"


def test_真入口可跑且三态之一():
    import subprocess

    r = subprocess.run([sys.executable, SCRIPT, "--json"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    assert r.stdout.strip(), "无输出"
    # ⚠️ 2026-10-06（t78/I18）：**整体 stdout 解析**（不再取"最后一个以 { 开头的行"）：
    #    探针输出是缩进美化的 ⇒ 老取法必然在 column 4 失败（而整体解析成功）。
    rep, how = _parse_single_json(r.stdout)
    mapping, default = _probe_exit_map()          # 契约来自探针源码（单一来源，不抄一份）
    assert rep.get("verdict") in set(mapping) | {"UNTESTABLE"}, (
        "verdict=%r 不在探针声明的取值集合 %s ∪ {'UNTESTABLE'} 里（how=%s）"
        % (rep.get("verdict"), sorted(mapping), how))
    # 比"rc 在 (0,1,2)"更强：**逐值一致**（t77 起 CROSS_STORE_MISSING 的 rc=3 ⇒ 旧清单已过期；
    # `UNTESTABLE` 不在字典里，走探针的 `default`＝2）
    expect_rc = mapping.get(rep["verdict"], default)
    assert r.returncode == expect_rc, (
        "退出码与 verdict 不一致：rc=%s verdict=%r 期望=%s（default=%s）"
        % (r.returncode, rep["verdict"], expect_rc, default))
