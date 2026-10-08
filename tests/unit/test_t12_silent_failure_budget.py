# -*- coding: utf-8 -*-
"""T23 判据：`trinity/engine_worker.py` 的**裸 `except: pass` 不得高于 HEAD 基线**。

## 为什么有这条判据（本轮真实回归）

t9/t12 期间该计数从 **HEAD 的 9 涨到 16（+7）**，被 `structure_gate` 的
`silent_failure:no_growth` 记为首超文件 —— 与"消灭静默失败"的主题**直接相反**。
根因是新增代码里写了 `except Exception: pass`（其中 7 处**全部**是 T12 加的，
t9 的 `_delivery_policy_v2` 本身用的是 `swallow`）。T23 已把这 7 处改为
"经 `trinity._swallow.swallow` 显式留痕"（全仓被吞异常的**唯一入口**：
有计数、有日志、有失败分类），本判据把它钉住。

## 口径（与 gate 同形，必须一致，否则判据会自欺）

`ExceptHandler` 的有效 body（剔除纯 docstring）**只有一条 `pass`** ⇒ 计 1。
`except Exception as _e: swallow(__name__, _e)` **不计**（那是显式处理）。

## 基线纪律

`HEAD_BASELINE = 9` 是**改动前**的实测值（`git show HEAD:…` + 本模块同口径计数）。
计数**下降**时请把基线一起下调（判据用 `<=`，只允许不高于）。

⚠️ 断言是 `<=` 不是 `==`：允许别人顺手把剩下的静默处也治了；
但**不许**为了过判据把 `swallow` 改回 `pass`（那会让计数上不去而判据仍绿 ——
故本文件同时断言"不高于基线"与"无裸 `except:`"）。
"""
from __future__ import annotations

import ast
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TARGET = os.path.join(REPO, "trinity", "engine_worker.py")

#: 改动前（HEAD）实测基线。下降请同步下调；上升即回归。
HEAD_BASELINE = 9


def bare_except_pass_lines(src: str) -> list:
    """返回裸 `except …: pass` 的行号（口径见模块 docstring）。"""
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        body = [b for b in node.body
                if not (isinstance(b, ast.Expr)
                        and isinstance(getattr(b, "value", None), ast.Constant)
                        and isinstance(b.value.value, str))]
        if len(body) == 1 and isinstance(body[0], ast.Pass):
            hits.append(node.lineno)
    return hits


def _src() -> str:
    with open(TARGET, "rb") as fh:
        return fh.read().decode("utf-8")


def _head_src() -> str:
    p = subprocess.run(["git", "show", "HEAD:trinity/engine_worker.py"], cwd=REPO,
                       capture_output=True)
    if p.returncode != 0:
        return ""
    return p.stdout.decode("utf-8", "replace")


def test_no_bare_naked_except():
    """裸 `except:`（不写异常类型）一个都不许有 —— 它会连 KeyboardInterrupt 一起吞。"""
    src = _src()
    naked = [n.lineno for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.ExceptHandler) and n.type is None]
    assert naked == [], "engine_worker.py 出现裸 `except:`（行 %s）" % naked


def test_engine_worker_bare_except_pass_within_head_baseline():
    src = _src()
    hits = bare_except_pass_lines(src)
    assert len(hits) <= HEAD_BASELINE, (
        "engine_worker.py 的裸 `except: pass` = %d，高于 HEAD 基线 %d（行 %s）。\n"
        "不要用『加注释』或『except Exception: pass』绕过 —— 请改成显式处理：\n"
        "    from trinity._swallow import swallow\n"
        "    except Exception as _e:\n"
        "        swallow(__name__, _e)\n"
        "（或记计数 / 打一次日志 / 明确降级留痕）" % (len(hits), HEAD_BASELINE, hits))


def test_baseline_matches_head_measurement():
    """基线的**来源可核**：HEAD 版本按同口径数出来就是 9（不是拍脑袋写的常数）。"""
    head = _head_src()
    if not head:
        return  # 无 git（例如源码包）时跳过来源核对，主判据仍生效
    n_head = len(bare_except_pass_lines(head))
    assert n_head == HEAD_BASELINE, (
        "HEAD 实测 = %d 与本文件基线 %d 不一致 ⇒ 基线过期，请同步更新" % (n_head, HEAD_BASELINE))


def test_swallow_is_the_explicit_exit_used():
    """改法可核：T23 修的**那 7 处**必须各自走到仓内唯一入口 `swallow`。

    为什么按变量名逐个点名（而不是数总数）：总数阈值是拍脑袋的（第一版写 `>= 20`，
    实测 15 ⇒ 判据自己标定错了）。点名式断言**直接**对应"这 7 个 `pass` 被换成了什么"，
    换回去（改回 `pass`）或改成别的静默写法都会红。
    """
    src = _src()
    t23_vars = ["_e_env", "_e_note", "_e_print", "_e_log2", "_e_rate", "_e_origin", "_e_origin2"]
    missing = [v for v in t23_vars if ("swallow(__name__, %s)" % v) not in src]
    assert not missing, "T23 修复的显式出口缺失（这些异常变量没有交给 swallow）：%s" % missing
