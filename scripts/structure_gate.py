#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力契约门（2026-09-11）—— 把本会话踩过的**静默失败**固化成可执行检查。

为什么需要它
------------
本会话（EXECUTION 658.94~658.97）连续踩到同一类坑：**守护自己悄悄失效，而系统
看起来一切正常**——

  1. YAML 规则文件写错（双引号里的反斜杠转义 / plain 标量含 " : "）→ 规则集**静默为空**，
     写入约束门形同虚设，无任何报错。
  2. _search.py 的 PPR 回退分支用了**未定义的 logger** → 一旦触发即 NameError，
     被外层 except 吞掉：日志不打、错误不响。
  3. Turtle 序列化跨主语残留分号 → **肉眼看完全正常**，只有外部解析器才报错。
  4. 新路由注册进 app 后被回退 → 能力"存在过"，但没人会发现它没了。

单元测试能覆盖这些，但**测试可以被跳过、能力可以悄悄退化**。本门把"能力还在不在、
守护还会不会响"固化成一条命令，exit 1 即拒绝：

  python scripts/structure_gate.py           # 人类可读
  python scripts/structure_gate.py --json    # 机器可读（CI / 维护链）

全部检查**离线**：不连数据库、不启服务、不写任何数据。
"""

from __future__ import annotations

import sys  # §1098：给新增的 [warn] 打印用（此前这些文件没导入 sys）
import argparse
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NL = chr(10)
BUDGETS = os.path.join(ROOT, "docs", "STRUCTURE_BUDGETS.json")


class Gate:
    def __init__(self) -> None:
        self.checks = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})

    @property
    def failed(self):
        return [c for c in self.checks if not c["ok"]]


def check_budgets(g: Gate) -> None:
    """① 防回胀：行数预算（数据源 docs/STRUCTURE_BUDGETS.json）。"""
    with io.open(BUDGETS, encoding="utf-8") as fh:
        budgets = json.load(fh)["budgets"]
    for rel, limit in sorted(budgets.items()):
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            g.add("budget:" + rel, False, "文件不存在")
            continue
        with io.open(p, encoding="utf-8") as fh:
            n = sum(1 for _ in fh)
        g.add("budget:" + rel, n <= limit,
              str(n) + " / " + str(limit) + " 行")


def check_constraints_file(g: Gate) -> None:
    """② 规则文件必须真的解析出规则（防静默空规则集）。"""
    from trinity.audit.constraints import load_constraints
    eng = load_constraints()
    g.add("constraints:rules_loaded", len(eng) >= 12,
          "规则数 " + str(len(eng)))
    g.add("constraints:no_load_error", eng.load_error is None,
          str(eng.load_error))


def check_write_gate_actually_blocks(g: Gate) -> None:
    """③ 写入门必须真的会拦（防形同虚设）。

    2026-10-06（复评 F3）：本检查原先**自己把开关打开**
    （``os.environ["TRINITY_WRITE_GATE"] = "on"``）再断言拦得住，于是它验证的是
    "**若**有人导出该变量，代码路径是否可达"——**从不读部署环境的实际取值**。
    结果是"门禁 PASS"与"开关是 off（锁没上）"可以同时成立（合规语境里这叫
    ineffective control：设计有效 ≠ 运行有效）。

    现在先**读并断言真实生效的取值**，为 off 即判红；之后再照旧做可达性探测。
    """
    from trinity.audit.constraints import check_record, should_block, gate_mode

    # 必须在下面 os.environ[...] = "on" 之前读，否则读到的就是自己刚设的值。
    ambient = gate_mode()
    g.add(
        "write_gate:enabled_in_env",
        ambient in ("on", "warn"),
        "effective=%r（off 表示写入门在生产上根本没被求值）。"
        "修复：设置用户级 TRINITY_WRITE_GATE=warn（或 on）并重启 API/维护链；"
        "详见 docs/ENV_REGISTRY.md 与 trinity/audit/constraints.py:gate_mode" % (ambient,),
    )

    os.environ["TRINITY_WRITE_GATE"] = "on"
    try:
        from trinity.audit.constraints import check_record, should_block
        bad = check_record({"content": "", "agent_id": "gate-probe"})
        good = check_record({"content": "ok", "agent_id": "gate-probe",
                             "importance": 0.5})
        g.add("write_gate:blocks_empty", should_block(bad),
              "critical=" + str(len(bad.critical)))
        g.add("write_gate:allows_valid",
              (not should_block(good)) and good.conforms, "")
    finally:
        os.environ.pop("TRINITY_WRITE_GATE", None)


def _turtle_structurally_valid(ttl: str):
    lines = ttl.split(NL)
    for idx, line in enumerate(lines[:-1]):
        if line.rstrip().endswith(";"):
            nxt = lines[idx + 1].strip()
            if nxt.startswith("<"):
                return False, "跨主语残留分号: " + line.strip()[:60]
    return True, ""


def _jsonld_triples(doc):
    """抽 JSON-LD 三元组。

    2026-09-21（§1145）：**修一处潜伏缺陷** —— 原实现假定"属性值一定是列表"，
    于是单值形态（`{"p": {"@id": "o"}}` 或 `{"p": "v"}`）会被**按 dict/str 迭代**：
    dict 迭代出的是**键**（会抽出 `(s, p, "@id")` 这种假三元组），str 会被逐**字符**拆开。
    本轮给本门加 `--selftest` 时，**断言当场把它抓了出来**（这正是"判据要先能失败"的收益）。
    现在统一归一化成列表再抽。
    """
    out = set()
    for node in doc.get("@graph", []):
        s = node.get("@id")
        for k, vals in node.items():
            if k == "@id":
                continue
            if isinstance(vals, (dict, str)):      # 单值形态 ⇒ 归一化为单元素列表
                vals = [vals]
            for v in vals:
                if isinstance(v, dict) and "@id" in v:
                    out.add((s, k, v["@id"]))
                elif isinstance(v, dict) and "@value" in v:
                    out.add((s, k, v["@value"]))
                elif isinstance(v, str):
                    out.add((s, k, v))
    return out


def check_prov_export(g: Gate) -> None:
    """④ PROV-O 导出必须结构合法，且两种序列化等价（防"看着正常其实非法"）。"""
    from trinity.audit.prov_export import build_graph, to_jsonld, to_turtle

    memory = {"memory_id": "gate-probe", "content": "x", "agent_id": "a",
              "sha256_hash": "a" * 64, "created_at": "2026-09-11T10:00:00+00:00"}
    versions = [{"version_id": "v1", "content": "x", "operation": "CREATE",
                 "created_at": "2026-09-06 16:54:31.928568+08"}]
    graph = build_graph(memory=memory, versions=versions)

    ttl = to_turtle(graph)
    ok, detail = _turtle_structurally_valid(ttl)
    g.add("prov:turtle_structure", ok, detail)

    # 时间戳必须规范化成合法 xsd:dateTime（真实库给的是空格分隔 + +08）
    bad_stamp = "2026-09-06 16:54:31" in ttl
    g.add("prov:xsd_datetime_normalized", not bad_stamp, "")

    jld = json.loads(to_jsonld(graph))
    n_ttl = len([1 for t in graph.triples])
    n_jld = len(_jsonld_triples(jld))
    g.add("prov:serializations_equivalent", n_ttl == n_jld,
          "turtle=" + str(n_ttl) + " json-ld=" + str(n_jld))


def check_bitemporal(g: Gate) -> None:
    """⑤ 双时态：哈希链必须可验证、时间点回放必须单调。"""
    from trinity.audit.bitemporal import load_rows, state_at
    rows = [{"action": "create", "agent_id": "a", "memory_id": "m1",
             "timestamp": "2026-09-11T10:00:00+00:00"},
            {"action": "update", "agent_id": "a", "memory_id": "m1",
             "timestamp": "2026-09-11T11:00:00+00:00"}]
    trail = load_rows(rows)
    ok, msg = trail.verify_chain()
    g.add("bitemporal:chain_intact", ok, str(msg))
    early = state_at(trail, 0.0)["events"]
    late = state_at(trail, 4102444800.0)["events"]
    g.add("bitemporal:replay_monotonic", early <= late,
          "early=" + str(early) + " late=" + str(late))


def check_audit_routes(g: Gate) -> None:
    """⑥ 只读审计端点必须仍挂在 app 上（防注册被回退）。"""
    import trinity.api.server as srv
    paths = set()
    for r in srv.app.routes:
        p = getattr(r, "path", None)
        if p:
            paths.add(p)
    for want in ("/audit/prov/{memory_id}", "/audit/point-in-time",
                 "/audit/constraints", "/audit/constraints/validate"):
        g.add("routes:" + want, want in paths, "")


# 2026-10-06（t33，T29-R5）：**已删除** `_ast_pass_count(path)`。
#
# 删除依据（三条，可核）：
#   1. **零调用点**：全仓 grep `_ast_pass_count` 只有它自己的定义（棘轮面用的是
#      `_scan_silent_pass_repo`，那才是活的那条路）。
#   2. 它属于"**存在但不生效**"的形态（本轮主题）：一个读文件 + AST 统计的函数，
#      名字与棘轮面高度相似，**却没有任何判据在用**。留着它只有一个效果 ——
#      下一个改这块的人可能改错地方，或以为静默失败棘轮走的是它。
#   3. 它曾是"同一课没传导"的**第三处**（t29 实测：用 `encoding="utf-8"` 读 ⇒ 带 BOM 的
#      文件解析失败并按 0 计 ⇒ **低估**预算计数）；t29 先把它改成 `_read_text` 消了陷阱，
#      t33 判定：既然无人调用，**删掉比修好更合适**（修好一个死函数等于继续供养它）。
#
# 若将来真需要"按文件统计 except:pass"，请用 `_scan_silent_pass_repo()`（棘轮面，已剥 BOM、
# 已有 unparsed 计数与预算棘轮），**不要**把这个函数复活。


#: 静默失败棘轮与门禁扫描共享的排除目录（口径同源，防止两者漂移）。
#: 跳过 scratch/归档目录的理由：棘轮若被临时脚本搅动而恒红，就会变成
#: 「恒红的门禁会被忽略」——本仓已有前科（见 docs/GATE_SET.json 的降级记录）。
#:
#: 2026-10-06（t29）：**"口径同源"曾是空话** —— 这张表只被棘轮面用（`:253`），
#: 而调用点面（`check_registry_consumer`）自己写了另一套 `(.venv/.git/node_modules)`，
#: 于是 `temp/` 下 169 个临时脚本进了调用点面，并因读取不剥 BOM 而**全部 AST 失败**。
#: 现在两侧都走本表；调用点面另把"被本表排除的文件数"计入覆盖恒等式（见 `_coverage_verdict`）。
SILENT_FAILURE_SKIP_DIRS = frozenset({
    ".venv", ".git", "node_modules", ".worktrees", "backup", "backups",
    "temp", "output", "experimental", "research",
})

#: 【t33 · T29-R3】同族写法（读源码用 `encoding="utf-8"` 又 `ast.parse`）的**只降不升**基线。
#:
#: 为什么是棘轮而不是"修完"：这类写法全仓有几十处（本轮实测的扫描面内 40+ 处），
#: 逐个改是几十次手术、收益与风险不成比例；但它们**共同构成一个盲区** ——
#: 任何一个带 BOM 的真源码 `.py` 对它们**全部不可见**（t33 已把现存 6 个真源码的 BOM 剥掉，
#: 所以当前盲区为**空**）。棘轮的作用是：**新增一处即红**，把"再长出来"这件事变成可见的。
#: 拿掉一处就把它改小（只降不升）。
#: 实测（2026-10-06，t33 修掉本文件自身那一处之后，分析面口径）= **17**。
SAME_FAMILY_BASELINE = 17


def _read_text(path: str) -> str:
    """读源码文本：**必须**剥掉 UTF-8 BOM（`utf-8-sig`）。

    2026-09-29 在棘轮面修过一次这个坑（见 `_scan_silent_pass_repo` 的长注释：
    BOM ⇒ `ast.parse` 抛 `invalid non-printable character U+FEFF` ⇒ 该文件**逃过棘轮**），
    但**没有传导**到调用点面 ⇒ t29 实测：调用点面 **176 个文件** AST 全失败，
    而且按原因分桶后 **176/176 都是这一个原因**。
    ⇒ 本函数是那一课的**唯一落点**；新增任何"读源码再 parse"的地方都必须用它。
    """
    with io.open(path, encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def scan_face_counts(root: str = None, skip_dirs=None, self_path: str = None) -> dict:
    """独立的**扫描面计数**（供覆盖判据的单测与外部复核）。

    与 `check_registry_consumer` 内联计数**同源**：同一个 `_read_text`（剥 BOM）、
    同一张排除表、同一个 `_coverage_verdict`。外部可用它对门禁自己报出的
    `structure:scan_coverage` 行做交叉验证 —— 两条路径报数不一致本身就说明有漂移。
    """
    root = root or ROOT
    B2 = os.sep
    import ast as _ast  # 与本文件既有风格一致（`_ast` 为函数内局部导入）
    skip = SILENT_FAILURE_SKIP_DIRS if skip_dirs is None else skip_dirs
    st = {"visited": 0, "excluded_decl": 0, "excluded_self": 0,
          "analyzed": 0, "ast_failed": 0, "ps1": 0, "bom_source": 0}
    failed = []
    bom_files = []
    for dp, dn, fn in os.walk(root):
        parts = set(dp.replace(root, "").split(B2))
        excluded_here = bool(parts & skip)
        for f in sorted(fn):
            if not f.endswith((".py", ".ps1")):
                continue
            st["visited"] += 1
            if excluded_here:
                st["excluded_decl"] += 1
                continue
            p = os.path.join(dp, f)
            if self_path and os.path.abspath(p) == os.path.abspath(self_path):
                st["excluded_self"] += 1
                continue
            if f.endswith(".ps1"):
                st["ps1"] += 1
                continue
            # ③ 真源码 `.py` 不得带 BOM：对"读源码再 ast.parse 且用 utf-8"的那几十处不可见
            try:
                with open(p, "rb") as _fh:
                    _raw3 = _fh.read(3)
            except OSError:
                # **不得静默**（t33 自查抓到：初版这里是 `except OSError: pass`，
                # 把本文件的 except:pass 从 1 涨到 3 —— 本项正在治的就是这个形态）。
                # 读不到 ⇒ 计入解析失败并 `continue`（**不往下走**，避免与下面的 _read_text 双计）。
                st["ast_failed"] += 1
                failed.append({"file": p.replace(root, ""), "reason": "OSError(读不到)"})
                continue
            if _raw3 == b"\xef\xbb\xbf":
                st["bom_source"] += 1
                bom_files.append(p.replace(root, ""))
            try:
                _ast.parse(_read_text(p))
                st["analyzed"] += 1
            except SyntaxError as exc:
                st["ast_failed"] += 1
                failed.append({"file": p.replace(root, ""), "reason": str(
                    getattr(exc, "msg", exc))[:60]})
    return {"stats": st, "failed": failed, "bom_files": bom_files,
            "verdict": _coverage_verdict(st["visited"], st["excluded_decl"], st["excluded_self"],
                                         st["analyzed"], st["ast_failed"], st["ps1"],
                                         st["bom_source"])}


def same_family_utf8_reads(rel: str, text: str, tree=None) -> list:
    """**同族写法扫描**（t33，T29-R3 的统一规则的机械形态）。

    规则：**读源码再 `ast.parse` 必须用 `utf-8-sig`**（剥 BOM）。
    Rule 检出形态（**AST 精确判定，不用文本正则**）：同一个函数体内同时出现
      · 一次 `ast.parse(...)` / `_ast.parse(...)`，且
      · 一次 `open(..., encoding="utf-8").read()` / `io.open(..., encoding="utf-8").read()`

    ⚠️ 首版用"同一函数里出现 `parse(` 且出现 `encoding=\"utf-8\"`"这种**文本**判据，
    把 `check_registry_consumer` **自己**也算进去了 —— 它里面确实有 `encoding="utf-8"`，
    但那是在**写**台账 JSON（`io.open(_led, "w", encoding="utf-8").write(...)`），
    与"读源码去 parse"无关。⇒ **判据必须判定"读"这个动作**，而不是"函数里出现过这个串"。
    现改为 AST：只认 `open/io.open(...).read()` 且其 `encoding` 关键字**恰为** `utf-8`；
    `utf-8-sig`、`"w"` 模式、`.write()` 一律不算。

    为什么这条规则要机械检查：t29 的根因是**同一课在同一份文件里没传导** ——
    棘轮面 2026-09-29 就用了 `utf-8-sig`，调用点面却用 `utf-8`，于是同一个 BOM 让
    176 个文件解析失败。**不逐个改**（那是几十次手术）；用"只降不升"的棘轮钉住，
    新增一处即红。

    返回：[{"file", "func", "line"}]
    """
    import ast as _ast
    out = []
    if tree is None:
        try:
            tree = _ast.parse(text)
        except SyntaxError:
            return out

    def _utf8_read_call(node) -> bool:
        """`open(...).read()` 且 encoding 恰好是 utf-8 ⇒ True。"""
        if not (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute)
                and node.func.attr == "read"):
            return False
        inner = node.func.value
        if not isinstance(inner, _ast.Call):
            return False
        fname = getattr(inner.func, "id", None) or getattr(inner.func, "attr", None)
        if fname != "open":
            return False
        for kw in inner.keywords:
            if kw.arg == "encoding" and isinstance(kw.value, _ast.Constant) \
                    and kw.value.value == "utf-8":
                return True
        return False

    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        has_parse = any(isinstance(n, _ast.Call)
                        and (getattr(n.func, "attr", None) == "parse"
                             or getattr(n.func, "id", None) == "parse")
                        for n in _ast.walk(node))
        if not has_parse:
            continue
        if any(_utf8_read_call(n) for n in _ast.walk(node)):
            out.append({"file": rel, "func": node.name, "line": node.lineno})
    return out


def _coverage_verdict(visited: int, excluded_decl: int, excluded_self: int, analyzed: int,
                      ast_failed: int, ps1: int, bom_source: int = 0) -> dict:
    """扫描面覆盖率的**纯函数**（可单测）—— 把「漏掉多少」变成**可核事实**。

    **恒等式（要能"拆干净"）**：:

        analyzed + ps1 + ast_failed + excluded_decl + excluded_self == visited

    五个桶互斥且穷尽：
      · `analyzed`        —— 已分析（AST 解析成功，提及已计入台账）
      · `ps1`             —— `.ps1`：按设计不做 AST（走 `_ps1_refs` 正则）
      · `ast_failed`      —— **解析失败**（必须为 0；见下）
      · `excluded_decl`   —— **声明式排除表**（`SILENT_FAILURE_SKIP_DIRS`）里的文件
      · `excluded_self`   —— 注册表自身（避免自证）

    判定要求：① 恒等式成立；② ``ast_failed == 0``；③ ``bom_source == 0``。
    为什么 ② 必须是 0 而不是"允许并记数"：BOM/编码这类失败**是可修的**，而它一旦存在
    就会让绑定/提及**静默漏记**（t29 实测 176 个文件全因此被漏）。**真正无法分析的请进
    显式排除表** —— 排除必须是**声明式**的，不能是"失败即跳过"。

    为什么 ③ ``bom_source`` 必须为 0（t33 新增）：本门禁自己已用 `_read_text`（剥 BOM）**能读**
    带 BOM 的文件，**但全仓还有 ~47 处"读源码再 `ast.parse`"用的是 `encoding="utf-8"`**
    ⇒ 一个带 BOM 的真源码文件**对那 47 处全部不可见**。t33 实测（`fake_green_audit` 自己的
    `scan()`，同一扫描面）：剥 BOM 前 `files_scanned=126 / files_failed_parse=6`；
    剥 BOM 后 `files_scanned=132 / files_failed_parse=0` —— 差的就是那 6 个文件。
    ⇒ "本门禁能读"不等于"整套门禁都能读"，所以**真源码 `.py` 不得带 BOM** 必须单独立判据。
    """
    total = analyzed + ps1 + ast_failed + excluded_decl + excluded_self
    identity_ok = total == visited
    return {"visited": visited, "excluded_decl": excluded_decl, "excluded_self": excluded_self,
            "analyzed": analyzed, "ast_failed": ast_failed, "ps1": ps1,
            "bom_source": bom_source, "bucketed_total": total, "identity_ok": identity_ok,
            "ok": identity_ok and ast_failed == 0 and bom_source == 0}


def _scan_silent_pass_repo():
    """全仓 AST 扫描「except 体内只有一条 pass」。

    返回 ``(per_file, total, unparsed, scanned)``。

    2026-09-29（外部审计修复）：本函数是新增的——原
    `check_silent_failure_ratchet` **只**遍历 `SILENT_FAILURE_BUDGETS.json`
    的 `files` 字典，而该文件当时是 `_total: 0, files: {}` ⇒ **零次迭代**、
    恒报 `total=0` 通过。棘轮**零覆盖**，而当时实际存在 336 处
    （其中 shipped 包 `trinity/` 内 47 处 / 22 文件）。
    """
    per_file, total, unparsed, scanned = {}, 0, 0, 0
    import ast as _ast  # 与本文件既有风格一致（_ast 为函数内局部导入）
    B2 = os.sep
    for dp, dn, fn in os.walk(ROOT):
        parts = set(dp.replace(ROOT, "").split(B2))
        if parts & SILENT_FAILURE_SKIP_DIRS:
            continue
        for f in fn:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            scanned += 1
            try:
                # utf-8-sig：吃掉 BOM。2026-09-29（外部审计自测发现）——用
                # "utf-8" 解码带 BOM 的文件会让 ast.parse 抛
                # "invalid non-printable character"，该文件于是落入 unparsed
                # 而**逃过棘轮**（负向测试实测：注入一个 BOM 版 except:pass
                # 文件，门禁仍报 PASS）。Windows 上 PowerShell 的
                # `Set-Content -Encoding UTF8` 正是产出 BOM 的常见来源。
                tree = _ast.parse(io.open(p, encoding="utf-8-sig",
                                          errors="replace").read())
            except Exception:
                unparsed += 1
                continue
            n = sum(1 for x in _ast.walk(tree)
                    if isinstance(x, _ast.ExceptHandler)
                    and len(x.body) == 1 and isinstance(x.body[0], _ast.Pass))
            if n:
                rel = p.replace(ROOT, "").lstrip("\\/").replace("\\", "/")
                per_file[rel] = n
                total += n
    return per_file, total, unparsed, scanned


def check_silent_failure_ratchet(g: Gate) -> None:
    """⑦ 静默失败棘轮：except:pass 计数**只许减不许增**（防在改造前继续增长）。

    背景（2026-09-11 L1）：全仓实测 **899 处 except:pass / 431 个文件**。本仓库反复出现
    "静默失败"——异常被吞掉后既不打日志也不计数，于是"出错了"与"没接线"无法区分。
    完整改造是"吞但计数"，属分批工程；先用棘轮**冻结现状**阻止继续增长。
    修一处就把 docs/SILENT_FAILURE_BUDGETS.json 里该文件的数字下调（只降不升）。

    2026-09-29（外部审计修复）——本判据此前**从未真正检查过任何东西**：
      · 预算文件是 `_total: 0, files: {}`；
      · 本函数只 `for rel, limit in files.items()` ⇒ 空字典 = 零次迭代；
      · 于是恒报 `silent_failure:no_growth OK (total=0)`，
        而同仓实际有 **336** 处（`trinity/` 包内 **47** 处 / 22 文件）。
    这正是本仓自己命名的死法：「存在但不生效」，且**没有一处判据要求它生效**。

    现在的判据（全部基于**自己扫描**，不再依赖预算文件被手工填对）：
      1. 全仓扫描出 `total_now`，与 `_total` 基线比对 —— 增长即失败；
      2. `files` 为空却有违反 ⇒ 失败（零覆盖本身是一种缺陷，不再伪装成通过）；
      3. 出现**新的**含静默 pass 的文件 ⇒ 失败（落实 `_comment` 里
         「新增文件不得含静默 pass」这句此前**未实现**的承诺）；
      4. 单文件超出其登记上限 ⇒ 失败；
      5. 解析失败的文件数如实上报（不把它藏成"更少"）。
    """
    path = os.path.join(ROOT, "docs", "SILENT_FAILURE_BUDGETS.json")
    with io.open(path, encoding="utf-8") as fh:
        budgets = json.load(fh)
    files = budgets.get("files") or {}
    baseline = budgets.get("_total")

    per_file, total_now, unparsed, scanned = _scan_silent_pass_repo()

    reasons = []
    if not files and total_now > 0:
        reasons.append(
            "预算 files 为空（棘轮零覆盖）却有 %d 处违反 ⇒ 判据形同不存在" % total_now)
    if isinstance(baseline, int) and total_now > baseline:
        reasons.append("全仓 total %d > 基线 %d（增长 %d）"
                       % (total_now, baseline, total_now - baseline))
    grew = ["%s %d>%d" % (rel, n, files[rel])
            for rel, n in sorted(per_file.items())
            if rel in files and n > files[rel]]
    if grew:
        reasons.append("单文件增长: " + "; ".join(grew[:3]))
    new_files = sorted(set(per_file) - set(files))
    if new_files:
        reasons.append("新增含静默 pass 的文件(%d): %s"
                       % (len(new_files), ", ".join(new_files[:3])))

    # 2026-09-29（负向测试发现的残余漏洞）：**无法解析的文件不能白名单化**。
    # 一个解析不了的文件 = 一个未被检查的文件；若不计入判据，往里面塞
    # `except: pass` 就能绕过棘轮（实测：BOM 文件正是这么溜过去的）。
    # 用基线比对而非"必须为 0"——本仓确有 7 个历史不可解析文件，
    # 要求为 0 会造出一个恒红的门禁（本仓准则：恒红的门禁会被忽略）。
    unparsed_base = budgets.get("_unparsed_files")
    if isinstance(unparsed_base, int) and unparsed > unparsed_base:
        reasons.append("无法解析的文件 %d > 基线 %d（未被检查的文件变多）"
                       % (unparsed, unparsed_base))

    detail = "total=%d baseline=%s files=%d scanned=%d unparsed=%d" % (
        total_now, baseline, len(files), scanned, unparsed)
    if reasons:
        detail += " | " + " | ".join(reasons)
    g.add("silent_failure:no_growth", not reasons, detail)


def check_registry_consumer(g: Gate) -> None:
    """⑧ R1 规则：注册表声明的脑能力**必须绑消费者**；未绑定数**只许减不许增**。

    背景（2026-09-13 实测）：`brain_capabilities()` 声明 **189** 项能力，其中
    **133 项在生产路径与维护链里都没有调用点** —— 注册表只做 `__import__(mod)` +
    `available: True`，表达的是"**能 import**"，不是"**会运行**"（7.6 倍落差）。
    完整治理是逐项接线或退役；先用棘轮冻结现状，**禁止再"只声明不接线"**。
    修一处就把 docs/REGISTRY_CONSUMER_BASELINE.json 的 unbound 下调（只降不升）。
    """
    import re as _re

    path = os.path.join(ROOT, "docs", "REGISTRY_CONSUMER_BASELINE.json")
    # t33：两处读取都改走 `_read_text`（剥 BOM）。这里正是"同族写法"在**本文件内部**的残留：
    # `src` 那行原为 `io.open(adv, encoding="utf-8").read()`，是本项新判据 `structure:utf8sig_rule`
    # 的**真命中**（不是误报）——门禁自己就在犯它要防的错。BASELINE 的 json 读取同理：
    # 一旦该 JSON 带 BOM，`json.load` 会抛（崩得响，但没必要）。
    base = json.loads(_read_text(path))
    B2 = os.sep
    prod_dirs = tuple(B2 + "trinity" + B2 + d + B2 for d in
                      ("core", "api", "daemon", "retrieval", "adapters",
                       "memory", "mcp", "modules", "qa"))
    adv = os.path.join(ROOT, "trinity", "core", "client", "_advanced.py")
    src = _read_text(adv)
    # 用字符类避免转义（[.] 即字面点）
    declared = sorted(set(_re.findall("[" + chr(39) + chr(34) + "]trinity[.]brain[.]([A-Za-z0-9_]+)[" + chr(39) + chr(34) + "]", src)))
    # 2026-09-14（R41-P7）：**绑定判定由「子串匹配」升级为 AST 真实引用**。
    # 旧判定只要文件任意位置出现该名字就算绑定 ⇒ 注释里提一句、甚至一个临时探针脚本都能把能力
    # "绑"上（实测：foresight_planning 的唯一 in-scope 出现是 dsh-ops/tmp_f262*.py 的一次性探针，
    # 见 R41-P6）。AST 只认三类**真引用**：import / 属性访问 trinity.brain.x / 字符串动态引用
    # "trinity.brain.x"（注册表、importlib 用）；注释与自由文本天然不入 AST。
    import ast as _ast

    def _refs(text, rel):
        """{能力名: (类别, "file:line")}；static=import/属性访问，dynamic=字符串引用。"""
        out = {}
        try:
            tree = _ast.parse(text)
        except SyntaxError:
            return out
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for al in node.names:
                    if al.name.startswith("trinity.brain."):
                        out.setdefault(al.name.split(".")[2], ("static", "%s:%d" % (rel, node.lineno)))
            elif isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                if mod == "trinity.brain":
                    for al in node.names:
                        out.setdefault(al.name, ("static", "%s:%d" % (rel, node.lineno)))
                elif mod.startswith("trinity.brain."):
                    out.setdefault(mod.split(".")[2], ("static", "%s:%d" % (rel, node.lineno)))
            elif isinstance(node, _ast.Attribute):
                chain, cur = [], node
                while isinstance(cur, _ast.Attribute):
                    chain.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, _ast.Name):
                    chain.append(cur.id)
                    chain.reverse()
                    if len(chain) >= 3 and chain[0] == "trinity" and chain[1] == "brain":
                        out.setdefault(chain[2], ("static", "%s:%d" % (rel, node.lineno)))
            elif isinstance(node, _ast.Constant) and isinstance(node.value, str):
                m2 = _re.match(r"^trinity[.]brain[.]([A-Za-z0-9_]+)$", node.value.strip())
                if m2:
                    out.setdefault(m2.group(1), ("dynamic", "%s:%d" % (rel, node.lineno)))
        # 第四类证据（R41-P8）：**路径字符串动态加载**，如
        #   importlib.util.spec_from_file_location("cb", ".../trinity/brain/consciousness_blueprint.py")
        # 只在**调用实参**里认，且**排除模块自身文件**（模块 docstring 第 2 行几乎都写着自己的路径，
        # 那是自指不是消费——首版漏了这条，202 个模块全被"命中"，属尺子缺陷）。
        for _node in _ast.walk(tree):
            if not isinstance(_node, _ast.Call):
                continue
            for _arg in list(_node.args) + [k.value for k in _node.keywords]:
                if isinstance(_arg, _ast.Constant) and isinstance(_arg.value, str):
                    _m = _re.search(r"trinity[/\\]brain[/\\]([A-Za-z0-9_]+)[.]py", _arg.value)
                    if _m and ("brain" + os.sep + _m.group(1) + ".py") not in rel.replace("/", os.sep):
                        out.setdefault(_m.group(1), ("path", "%s:%d" % (rel, _node.lineno)))
        return out

    def _ps1_refs(text, rel):
        out = {}
        for i, line in enumerate(text.splitlines()):
            if line.lstrip().startswith("#"):
                continue  # PowerShell 注释不算证据
            for m2 in _re.finditer(r"trinity[.]brain[.]([A-Za-z0-9_]+)", line):
                out.setdefault(m2.group(1), ("dynamic", "%s:%d" % (rel, i + 1)))
            # 路径式执行（R41-P8）：维护链里大量 runpy.run_path(r"...\trinity\brain\X.py")
            # ⇒ 这是**被调度真正执行**的能力，必须算已绑（首版只认 trinity.brain.X 漏掉了它们）。
            for m3 in _re.finditer(r"trinity[/\\]+brain[/\\]+([A-Za-z0-9_]+)[.]py", line):
                out.setdefault(m3.group(1), ("path", "%s:%d" % (rel, i + 1)))
        return out

    _mentions = {}
    _order = {("prod", "static"): 4, ("prod", "path"): 3, ("prod", "dynamic"): 2,
              ("chain", "static"): 3, ("chain", "path"): 2, ("chain", "dynamic"): 1}
    prod_ev, chain_ev = {}, {}
    # 2026-10-06（t29）：**扫描面覆盖计数**。原实现只排除 .venv/.git/node_modules（自带一套，
    # 与棘轮面的 `SILENT_FAILURE_SKIP_DIRS` 漂移），并且用 "utf-8" 读取（不剥 BOM）⇒
    # 实测 **176 个文件** AST 解析失败，全部是同一个原因（BOM ⇒ `invalid non-printable U+FEFF`），
    # 而失败只是 `[warn] … pass`（静默略过）。现在：① 两侧共用同一张排除表；
    # ② 读取走 `_read_text`（剥 BOM）；③ 失败**响亮**并计入覆盖恒等式（见 `_coverage_verdict`）。
    _scan = {"visited": 0, "excluded_decl": 0, "excluded_self": 0,
             "analyzed": 0, "ast_failed": 0, "ps1": 0, "bom_source": 0}
    _ast_failed_files = []
    _bom_source_files = []
    _same_family = []
    for dp, dn, fn in os.walk(ROOT):
        _parts = set(dp.replace(ROOT, "").split(B2))
        _excluded_here = bool(_parts & SILENT_FAILURE_SKIP_DIRS)
        for f in fn:
            if not f.endswith((".py", ".ps1")):
                continue
            _scan["visited"] += 1
            if _excluded_here:
                _scan["excluded_decl"] += 1          # **声明式**排除（不是"失败即跳过"）
                continue
            p = os.path.join(dp, f)
            if p == adv:
                _scan["excluded_self"] += 1          # 注册表自身（避免自证）
                continue
            rel = p.replace(ROOT, "")
            try:
                t = _read_text(p)
            except OSError:
                _scan["ast_failed"] += 1
                _ast_failed_files.append({"file": rel, "reason": "OSError(读不到)"})
                continue
            if f.endswith(".ps1"):
                _scan["ps1"] += 1
            else:
                # t33：真源码 `.py` 不得带 UTF-8 BOM —— 对那几十处"用 utf-8 读再 parse"的门禁
                # 一律不可见（本门禁自己能读不等于别人能读）。只读 3 字节，成本可忽略。
                try:
                    with open(p, "rb") as _fh:
                        _raw3 = _fh.read(3)
                except OSError:
                    # **不得静默**（t33 自查：初版是 `except OSError: pass`）。
                    # 这里 `t` 已经读到了（上面 `_read_text` 成功），所以 3 字节读失败极罕见；
                    # 但"罕见"不是静默的理由 —— 计入解析失败，让 `structure:scan_coverage` 判红。
                    _scan["ast_failed"] += 1
                    _ast_failed_files.append({"file": rel, "reason": "OSError(3 字节读不到)"})
                    continue
                if _raw3 == b"\xef\xbb\xbf":
                    _scan["bom_source"] += 1
                    _bom_source_files.append(rel)
            ev = _refs(t, rel) if f.endswith(".py") else _ps1_refs(t, rel)
            # 第二档证据（R41-P7）：**裸标识符**出现（如 mod.spaced_repetition()、局部同名变量）。
            # 只记为 mention（不进门的"已绑"判定），供台账区分「疑似引用过」与「真接线」；
            # 注释/字符串里的提及不在此列（AST 天然排除）——那正是上一版假绑定的来源。
            if f.endswith(".py"):
                try:
                    _tree = _ast.parse(t)
                    for _nd in _ast.walk(_tree):
                        if isinstance(_nd, _ast.Attribute):
                            _mentions.setdefault(_nd.attr, "%s:%d" % (rel, _nd.lineno))
                        elif isinstance(_nd, _ast.Name):
                            _mentions.setdefault(_nd.id, "%s:%d" % (rel, _nd.lineno))
                    _scan["analyzed"] += 1
                    # t33（T29-R3）：同族写法棘轮 —— 读源码再 parse 必须 utf-8-sig。
                    # 复用同一棵已解析的树，**不额外解析**。
                    _same_family.extend(same_family_utf8_reads(rel, t, _tree))
                except SyntaxError as _exc:
                    # **响亮**：不再 `[warn] … pass`。失败文件必须点名，并且由
                    # `structure:scan_coverage` 判红（`ast_failed` 必须为 0）。
                    _scan["ast_failed"] += 1
                    _ast_failed_files.append({
                        "file": rel, "reason": "%s: %s" % (type(_exc).__name__,
                                                           str(getattr(_exc, "msg", _exc))[:60])})
            if not ev:
                continue
            if any(s in rel for s in prod_dirs):
                for k, v in ev.items():
                    if k not in prod_ev or _order[("prod", prod_ev[k][0])] < _order[("prod", v[0])]:
                        prod_ev[k] = v
            # 2026-09-13（659.48）：原范围只有 scripts/ 与 dsh-ops/ —— **不含 benchmark/ 与 tests/**。
            # 这正是 659.34 在 capability_effectiveness.classify() 里修掉的同一个盲区
            # （当时导致 NEVER_ACTIVE 误判、差点删掉活开关）。**修了台账却没修门禁 = 同一错误的第二次。**
            if any(k in rel for k in (B2 + "scripts" + B2, B2 + "dsh-ops" + B2,
                                      B2 + "benchmark" + B2, B2 + "tests" + B2)):
                for k, v in ev.items():
                    if k not in chain_ev or _order[("chain", chain_ev[k][0])] < _order[("chain", v[0])]:
                        chain_ev[k] = v
    evidence = {}
    for k in set(prod_ev) | set(chain_ev):
        cands = []
        if k in prod_ev:
            cands.append(("prod", prod_ev[k][0], prod_ev[k][1]))
        if k in chain_ev:
            cands.append(("chain", chain_ev[k][0], chain_ev[k][1]))
        evidence[k] = max(cands, key=lambda x: _order[(x[0], x[1])])
    # ── 【t29】扫描面覆盖：把「门禁的覆盖面」变成**可核事实** ──────────────────
    # 这条判据比修好那 176 个文件更重要：它让"漏掉多少"**永远可见**。
    # 五桶恒等式 + `ast_failed==0`；任一不满足 ⇒ **红**（不是 `[warn]`）。
    _cov = _coverage_verdict(_scan["visited"], _scan["excluded_decl"], _scan["excluded_self"],
                             _scan["analyzed"], _scan["ast_failed"], _scan["ps1"],
                             _scan["bom_source"])
    _cov_detail = ("访问 %d = 已分析 %d + .ps1 %d + **解析失败 %d** + 声明式排除 %d + 自身排除 %d"
                   "; **真源码带 BOM %d**"
                   % (_cov["visited"], _cov["analyzed"], _cov["ps1"], _cov["ast_failed"],
                      _cov["excluded_decl"], _cov["excluded_self"], _cov["bom_source"]))
    if _cov["ast_failed"]:
        _cov_detail += "；**解析失败文件（前 8 个）**：" + "; ".join(
            "%s [%s]" % (x["file"], x["reason"]) for x in _ast_failed_files[:8])
    if _cov["bom_source"]:
        _cov_detail += "；**带 BOM 的真源码（前 8 个）**：" + "; ".join(_bom_source_files[:8])
    g.add("structure:scan_coverage", _cov["ok"],
          _cov_detail + ("（恒等式不成立：五桶合计 %d ≠ 访问 %d）"
                         % (_cov["bucketed_total"], _cov["visited"]) if not _cov["identity_ok"] else ""))
    # t33（T29-R3）：**同族写法棘轮** —— 读源码再 `ast.parse` 必须 `utf-8-sig`。
    # 全仓存量大（本轮**不逐个改**：那是几十次手术）；用"只降不升"钉住，新增一处即红。
    g.add("structure:utf8sig_rule", len(_same_family) <= SAME_FAMILY_BASELINE,
          "%d 处「读源码用 encoding=\"utf-8\" 又 ast.parse」/ 基线 %d%s"
          % (len(_same_family), SAME_FAMILY_BASELINE,
             ("；新增处（前 8）：" + "; ".join("%s::%s(%d)" % (x["file"], x["func"], x["line"])
                                             for x in _same_family[:8])) if _same_family else ""))
    unbound = [m for m in declared if m not in evidence]
    # 2026-09-14（R41-P8）：**已登记退役**从"有效未绑"里扣除并单独报数——同机制审计对
    # registered_retired 的做法。退役=**登记**（docs/REGISTRY_RETIRED_CAPABILITIES.json），
    # 不删模块、不删数据、不改注册表；扣除后棘轮才可能合法下降（只降不升）。
    _ret_path = os.path.join(ROOT, "docs", "REGISTRY_RETIRED_CAPABILITIES.json")
    registered_retired = []
    try:
        if os.path.exists(_ret_path):
            _rj = json.load(io.open(_ret_path, encoding="utf-8"))
            registered_retired = sorted(set(_rj.get("retired") or {}) & set(unbound))
    except Exception:
        registered_retired = []
    unbound_effective = [m for m in unbound if m not in set(registered_retired)]
    # 台账产物：给"接线还是退役"的决策提供 file:line 证据（R1 治理的输入）
    try:
        import time as _time
        _led = os.path.join(ROOT, "output", "registry_consumer_ledger.json")
        os.makedirs(os.path.dirname(_led), exist_ok=True)
        io.open(_led, "w", encoding="utf-8").write(json.dumps({
            "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "declared": len(declared), "bound": len(declared) - len(unbound), "unbound": len(unbound),
            "bound_detail": {k: {"scope": v[0], "kind": v[1], "evidence": v[2]}
                             for k, v in sorted(evidence.items()) if k in declared},
            "unbound_list": unbound,
            "unbound_effective": unbound_effective,
            "registered_retired": registered_retired,
            "unbound_but_mentioned": {m: _mentions[m] for m in unbound if m in _mentions},
            "unbound_no_trace": [m for m in unbound if m not in _mentions],
        }, ensure_ascii=False, indent=1))
    except Exception:
        pass
    limit = int(base.get("unbound", 0))
    _n_static = sum(1 for v in evidence.values() if v[1] in ("static", "path"))
    g.add("registry:no_growth", len(unbound_effective) <= limit,
          "%d 有效未绑 / 基线 %d（实测未绑 %d - 已登记退役 %d；声明 %d；已绑 static+path=%d dynamic=%d）" % (
              len(unbound_effective), limit, len(unbound), len(registered_retired),
              len(declared), _n_static, len(evidence) - _n_static))


def _selftest() -> int:
    """§1145：判据要先能失败 —— 本轮选两个**纯函数**做两方向断言（不碰 I/O、不依赖当日产物）。

    ① `_turtle_structurally_valid`：跨主语残留分号（上一行以 `;` 结尾且下一行以 `<` 开头）须判 False；
       正常续行（下一行是 `ex:` 前缀）须判 True —— **边界两侧都验**；
    ② `_jsonld_triples`：`@id` 对象与字面量两种形态都要抽到三元组；空文档须抽到 **0** 条。
    """
    cases = []
    bad_ttl = "ex:a ex:b ex:c ;" + NL + "<http://x/> ex:e ex:f ."
    good_ttl = "ex:a ex:b ex:c ;" + NL + "    ex:d ex:e ex:f ."
    cases.append(("turtle 跨主语残留分号 须 False", _turtle_structurally_valid(bad_ttl)[0], False))
    cases.append(("turtle 正常续行     须 True ", _turtle_structurally_valid(good_ttl)[0], True))
    cases.append(("turtle 无分号       须 True ", _turtle_structurally_valid("ex:a ex:b ex:c .")[0], True))
    t_obj = _jsonld_triples({"@graph": [{"@id": "s", "p": {"@id": "o"}}]})
    t_lit = _jsonld_triples({"@graph": [{"@id": "s", "p": "v"}]})
    t_none = _jsonld_triples({})
    cases.append(("jsonld @id 对象    须 1 条", len(t_obj) == 1 and ("s", "p", "o") in t_obj, True))
    cases.append(("jsonld 字面量      须 1 条", len(t_lit) == 1 and ("s", "p", "v") in t_lit, True))
    cases.append(("jsonld 空文档      须 0 条", len(t_none) == 0, True))
    miss = [lab for lab, got, want in cases if bool(got) is not bool(want)]
    print("selftest: %s | 共 %d 例（%d 例须 True / %d 例须 False）"
          % ("PASS" if not miss else "FAIL", len(cases), sum(1 for _l, _g, w in cases if w),
             sum(1 for _l, _g, w in cases if not w)))
    for m in miss:
        print("   ✗ " + m)
    return 0 if not miss else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Trinity 能力契约门（离线）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--selftest", action="store_true", help="验证本门判据本身能失败（§1145）")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()

    g = Gate()
    checks = (check_budgets, check_constraints_file, check_write_gate_actually_blocks,
              check_prov_export, check_bitemporal, check_audit_routes,
              check_silent_failure_ratchet, check_registry_consumer)
    for fn in checks:
        try:
            fn(g)
        except Exception as exc:  # 门的检查自身崩了也要算失败，不能静默
            g.add("check:" + fn.__name__, False, "检查异常: " + str(exc)[:120])

    failed = g.failed
    if args.json:
        print(json.dumps({"passed": len(g.checks) - len(failed),
                          "failed": len(failed), "checks": g.checks},
                         ensure_ascii=False, indent=2))
    else:
        for c in g.checks:
            print(("  [PASS] " if c["ok"] else "  [FAIL] ") + c["check"]
                  + ("  " + c["detail"] if c["detail"] else ""))
        print("---")
        print("能力契约门: " + str(len(g.checks) - len(failed)) + " passed, "
              + str(len(failed)) + " failed")
    return 1 if failed else 0


if __name__ == "__main__":
    _rc = main()
    import datetime as _dt
    print("[采样时刻] %s（本读数只对该时刻的系统状态成立）" % _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    raise SystemExit(_rc)
