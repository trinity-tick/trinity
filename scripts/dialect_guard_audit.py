#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dialect_guard_audit.py —— 「方言盲能力守卫」检测（T14，2026-10-06）

## 治的是什么

```python
if hasattr(adapter, "_get_conn"):      # ← 只在 PostgreSQLAdapter 上为真
    ... PG 分支 ...
else:
    return []                          # ← SQLite 上静默走这里：**什么都没做**
```

`_get_conn()` **只存在于 `PostgreSQLAdapter`**；`SQLiteAdapter` 只有 `_conn` /
`_get_read_conn`。所以按**后端能力名**做的 `hasattr` 守卫在另一个后端上恒假，
守卫分支里的代码**根本没执行**，而调用方看到的是一个正常的空值/0/False。

这类缺陷用"异常被吞"是找不到的（驱动层没吞任何异常）——它是**分支根本没进**。

## 判据（两条，都必须命中才报）

**R1 能力名是后端特有的**：`hasattr(<obj>, "<cap>")`，`cap` ∈
`BACKEND_SPECIFIC_CAPS`（默认含 `_get_conn` / `_get_read_conn` / `_write_conn`）。

**R2 未命中分支是「静默早退」**：能力缺失时走的那个分支体，整体是
`pass` / `continue` / `return <空常量>`（`None`/`[]`/`{}`/`()`/`0`/`False`/`""`），
**且**体内没有 `raise`、没有 `logger.<level>()` / `warnings.warn()`、没有计数器自增。

两条同时成立 ⇒ 违规：**看起来守卫了，实际什么都没做，而且不吭声**。

## 允许的三种写法（与任务书一致）

  ① 方言正确：`trinity._tags._conn_ctx(adapter)`（PG `_get_conn()` / SQLite 裸 `_conn`）；
  ② 保持 no-op 但**响亮**：分支里有 `logger.error/warning` 或 `raise` 或计数器；
  ③ 登记为 `declared-unsupported-on-<dialect>`：写进 `ALLOWLIST` 并给出理由。

用法：
    python scripts/dialect_guard_audit.py            # 人读
    python scripts/dialect_guard_audit.py --json     # 机读
退出码：0 = 无违规；1 = 有违规（**按判据报错，不是靠基线**）。
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 后端特有的能力名（只有某一个适配器实现）
BACKEND_SPECIFIC_CAPS = frozenset({
    "_get_conn",          # 只有 PostgreSQLAdapter
    "_get_read_conn",     # 只有 SQLiteAdapter
    "_write_conn",
})

#: 空值常量 → 视为"什么都不做"
_EMPTY = (None, False, 0, "", [], {}, ())

#: 登记豁免：`(相对路径, 行号)` → 理由。
#: 只允许"已按 ② 或 ③ 处置过"的条目；新增条目必须在报告里给出证据。
ALLOWLIST: dict = {}


def _is_empty_const(node) -> bool:
    if isinstance(node, ast.Constant) and node.value in _EMPTY:
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and not node.elts:
        return True
    if isinstance(node, ast.Dict) and not node.keys:
        return True
    return False


def _is_loud_expr(node) -> bool:
    """`raise` / `logger.*` / `warnings.warn` / 计数器自增 ⇒ 响亮。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Raise):
            return True
        if isinstance(sub, ast.AugAssign):          # counts[k] += 1
            return True
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute):
                base = f.value
                base_name = getattr(base, "id", None) or getattr(base, "attr", None)
                if base_name in ("logger", "warnings", "log"):
                    return True
            if isinstance(f, ast.Name) and f.id in ("warn", "warning", "error"):
                return True
    return False


def _silent_early_exit(body) -> bool:
    """分支体整体是「静默早退」吗？"""
    stmts = [s for s in body]
    if not stmts:
        return True
    if _is_loud_expr(ast.Module(body=stmts, type_ignores=[])):
        return False
    for s in stmts:
        if isinstance(s, (ast.Pass, ast.Continue)):
            continue
        if isinstance(s, ast.Return):
            v = s.value
            if v is None:
                continue
            # `return {"error": ...}` ⇒ 这是**响亮**的（把不可用如实报给调用方）
            if isinstance(v, ast.Dict) and any(
                    isinstance(k, ast.Constant) and k.value == "error" for k in v.keys):
                return False
            if isinstance(v, ast.Constant) and isinstance(v.value, str) and v.value:
                return False          # 返回一句说明文字 ⇒ 不算静默
            if _is_empty_const(v):
                continue
            return False
        return False
    return True


def _hasattr_caps(test_ast) -> set:
    out = set()
    for sub in ast.walk(test_ast):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                and sub.func.id == "hasattr" and len(sub.args) == 2:
            a = sub.args[1]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                out.add(a.value)
    return out


def _test_is_capability_negative(test) -> bool:
    """test 是否形如「能力缺失」为真：`not hasattr(...)` / `X is None or not hasattr(...)`。"""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) \
            and isinstance(test.operand, ast.Call) \
            and isinstance(test.operand.func, ast.Name) and test.operand.func.id == "hasattr":
        return True
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return any(_test_is_capability_negative(v) for v in test.values)
    return False


#: T14-R3：能力缺失分支里直连**硬编码后端**的工厂（把写入/读取送到另一个库）
_HARDCODED_CONN_FACTORIES = frozenset({"_shared_conn", "_open_conn", "_pg_conn",
                                       "_get_pg_conn", "_pg_shared_conn"})


def _has_hardcoded_conn(body) -> bool:
    """分支体里是否直连硬编码后端（`_shared_conn()` / `psycopg2.connect(...)` 等）。"""
    for sub in ast.walk(ast.Module(body=list(body), type_ignores=[])):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if isinstance(f, ast.Name) and f.id in _HARDCODED_CONN_FACTORIES:
            return True
        if isinstance(f, ast.Attribute) and f.attr == "connect":
            base = f.value
            nm = getattr(base, "id", None) or getattr(base, "attr", None)
            if nm in ("psycopg2", "sqlite3"):
                return True
    return False


def _adapter_nonnull_only(test) -> bool:
    """test 形如「<obj> is not None」**且不含任何能力名检查** ⇒ 它单独就处理了"适配器存在"。"""
    if test is None:
        return False
    if _hasattr_caps(test):
        return False
    for sub in ast.walk(test):
        if isinstance(sub, ast.Compare) and len(sub.ops) == 1 \
                and isinstance(sub.ops[0], ast.IsNot):
            return True
    return False


def _chain(node: ast.If) -> list:
    """把 `if / elif / else` 展开成 [(test|None, body)]（None 表示 else）。"""
    clauses = []
    cur = node
    while True:
        clauses.append((cur.test, cur.body))
        if len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
            cur = cur.orelse[0]
            continue
        if cur.orelse:
            clauses.append((None, cur.orelse))
        break
    return clauses


def scan_source(rel: str, src: str) -> list:
    """返回违规清单（每条带 file/line/branch/kind/why）。

    按 `if / elif / else` **整链**判断，两个关键点：

      · 链里若有**不含能力名检查**的「`<obj> is not None`」段，说明"适配器存在"这一情形
        已被单独处理 ⇒ 之后的 `else` 只会在"根本没有适配器"时走到 ⇒ **不算违规**
        （这正是修好后 access_touch/hebbian 的形状）。
      · 判据分两类：`silent-early-exit`（能力缺失分支静默早退）与
        `hardcoded-conn`（能力缺失分支直连硬编码后端 ⇒ 读写送到另一个库）。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.splitlines()
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        chain = _chain(node)
        head_test = chain[0][0] if chain else None
        head_caps = _hasattr_caps(head_test) if head_test is not None else set()
        if not (head_caps & BACKEND_SPECIFIC_CAPS):
            continue
        present_handled = False
        for test, body in chain:
            if test is None:
                if head_caps & BACKEND_SPECIFIC_CAPS and not present_handled:
                    _maybe_record(out, rel, lines, node, head_caps, "else", body)
                continue
            if _adapter_nonnull_only(test):
                present_handled = True
                continue
            caps = _hasattr_caps(test) & BACKEND_SPECIFIC_CAPS
            if caps and _test_is_capability_negative(test):
                _maybe_record(out, rel, lines, node, caps, "if(能力缺失)", body)
    out.sort(key=lambda d: (d["file"], d["line"], d["branch"]))
    return out


def _maybe_record(out, rel, lines, node, caps, which, body):
    silent = _silent_early_exit(body)
    hardcoded = _has_hardcoded_conn(body)
    if not (silent or hardcoded):
        return
    if (rel, node.lineno) in ALLOWLIST:
        return
    if hardcoded:
        why = ("守卫按后端能力名 %s 判断；能力缺失走的 %s 分支**直连硬编码后端**"
               "（`_shared_conn()`/`psycopg2.connect()` 之类）⇒ 在另一个后端上会把"
               "读写送到**另一个库**，而返回值看起来还是成功的" % (sorted(caps), which))
    else:
        why = ("守卫按后端能力名 %s 判断；该后端下恒假 ⇒ 走 %s 分支，"
               "而该分支是**静默早退**（无 raise / 无日志 / 无计数）" % (sorted(caps), which))
    out.append({
        "file": rel, "line": node.lineno, "branch": which,
        "capability": sorted(caps),
        "kind": ("hardcoded-conn" if hardcoded else "silent-early-exit"),
        "guard": (lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""),
        "why": why,
    })


def _iter_py(base: str):
    for cur, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", ".venv", ".git"}]
        for f in sorted(files):
            if f.endswith(".py") and ".bak" not in f:
                yield os.path.join(cur, f)


def audit(scan_root: str = None) -> dict:
    root = scan_root or os.path.join(ROOT, "trinity")
    findings = []
    for path in _iter_py(root):
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        try:
            src = io.open(path, encoding="utf-8-sig", errors="replace").read()
        except OSError:
            continue
        findings.extend(scan_source(rel, src))
    findings.sort(key=lambda d: (d["file"], d["line"]))
    return {"count": len(findings), "findings": findings,
            "caps": sorted(BACKEND_SPECIFIC_CAPS),
            "allowlist_size": len(ALLOWLIST)}


def main() -> int:
    ap = argparse.ArgumentParser(description="方言盲能力守卫检测（T14）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", default=None)
    a = ap.parse_args()
    rep = audit(a.root)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print("方言盲能力守卫（按后端能力名 hasattr 守卫 + 未命中分支静默早退）")
        print("=" * 78)
        print("  能力名白名单：%s" % ", ".join(rep["caps"]))
        if not rep["count"]:
            print("  （无违规）")
        for h in rep["findings"]:
            print("  %-58s %-5d [%s]" % (h["file"], h["line"], h["branch"]))
            print("        %s" % h["guard"][:96])
            print("        %s" % h["why"][:150])
        print("-" * 78)
        print("  违规数 = **%d**" % rep["count"])
    return 1 if rep["count"] else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
