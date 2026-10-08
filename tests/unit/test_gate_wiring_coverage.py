# -*- coding: utf-8 -*-
"""判据：带棘轮语义的门禁脚本**要么在 CI 里跑，要么被显式登记**。

## 固化的实测缺陷（2026-09-29 外部审计）

外部审计实测：带棘轮语义的脚本共 **42** 个（本轮修复后计数），其中仅 **9** 个被 CI 调用
⇒ **33 个从不执行**。被漏掉的名单里包含 `scripts/structure_gate.py` ——
即本轮刚修好的「静默失败棘轮」所在的那个门禁：**修完在 CI 里依然无人跑**。

这是根因 B（声明与实际脱节）在**门禁自身**上的复制：
一个门禁"存在"（有脚本、有基线、能判失败），不代表它"被执行"。
本仓此前已用同一形态发现过两次（`SILENT_FAILURE_BUDGETS.json` 的零覆盖、
`scores_gate` 的 7 文件扫描面），所以这一次把它做成**结构性护栏**。

## 本判据锁什么

    · 登记（docs/GATE_WIRING.json）必须存在、可解析、含状态枚举与理由
    · `ci_wired` 必须与**实测**（workflow 里是否真的调用该脚本）**双向一致**
      ⇒ 悄悄摘掉一个 CI 步骤、或偷偷接上一个而没登记，都会立刻红
    · 每个棘轮脚本必须出现在 `ci_wired` 或 `unwired` 里（**不允许漏登记**）
    · `unwired` 条目必须有 status（取值在枚举内）+ 非空 reason
    · `unwired` 里不允许有僵尸（脚本已不存在）
    · `_must_be_wired` 名单里的门禁必须真的在跑（防止把门禁"降级登记"当解法）
    · 头部的计数字段必须与实测一致（防止登记慢慢变成谎言）

**不把 33 个全部接进 CI 是有意的**：其中 10 个需要真库/活体服务/LLM（在 CI 上会
不稳定或直接不可用），4 个是设计上手动跑的生成器/重算工具。剩下 19 个标为
`unassessed` —— **这是欠账而不是通过**，登记文件里逐条写明了这点。
"""
from __future__ import annotations

import ast
import io
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTER = ROOT / "docs" / "GATE_WIRING.json"
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / ".github" / "workflows"

#: 判定"带棘轮语义"的口径。
#
# 2026-10-06（T1 测试归因轮 · 队长转达 capability-hygiene 的 T10-R4）：
# **原口径是三行正则扫整个文件文本** `ratchet|只降不升|只许减不许增|棘轮` ——
# 于是**任何注释/文档字符串里写出这几个词**的文件都会被算成"棘轮门禁脚本"：
#
#   · 实测触发过假红（`53 != 54`，登记与实际对不上）；
#   · 当时的处置是**改写注释规避** —— 那只是把坑留给下一个人，不是根治；
#   · 同一个病一周内第三次显形（另一处：脚本因注释含关键词被误判成门禁脚本）。
#
# 现改为**结构性判定**：只看 AST 里"真的解析了 ratchet 参数 / 真的以 ratchet 命名"的
# 证据。注释与文档字符串**不参与**判定（AST 里它们不产生 Call/Name/Attribute 节点）。
# 证据种类（`_ratchet_evidence` 的返回值）：
#
#   filename          文件名含 ratchet（如 lint_ratchet.py / mypy_ratchet.py）
#   argflag:--ratchet argparse 真的解析了一个含 ratchet 的参数名
#   name:<id>         代码里存在含 ratchet 的标识符（如 RATCHET_KEYS / check_*_ratchet）
#   attr:<a>          代码里存在含 ratchet 的属性访问
#
# ## 本口径能保证什么、不能保证什么（读前须知）
#
# 能：**凡有结构化棘轮证据的脚本都必须已登记**（绊线，且不再被注释骗）。
# 不能：把登记里的 53 条重新推导出来 —— 那 53 条是**人工判定**（依据写在登记各条的
#       reason 与 `_method` 里），其中相当一部分把"只降不升"表述在文字里、机制在大
#       baseline 比较里，**没有任何代码形态能把它们与"引用了一个 baseline 文件的测量
#       工具"区分开**。实测三种更强的结构化口径分别得 21/22/59 条，都无法等于 53。
#      ⇒ 不假装能重新推导：本判据只锁**可被代码判定的那一半**，另一半由各脚本自身的
#      基线与判据负责；登记条目的字段合法性/僵尸/互斥仍逐条强校验（下面的用例）。
_RATCHET_CTX = ("add_argument", "add_argument_group", "add_parser", "add_subparsers")


def _ratchet_evidence(tree: "ast.Module", name: str) -> "str | None":
    """结构性棘轮证据（**只看代码**：注释与文档字符串不参与）。返回证据种类或 None。"""
    if "ratchet" in name.lower():
        return "filename"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if fn in _RATCHET_CTX:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                            and "ratchet" in arg.value.lower():
                        return "argflag:%s" % arg.value
        if isinstance(node, ast.Name) and "ratchet" in node.id.lower():
            return "name:%s" % node.id
        if isinstance(node, ast.Attribute) and "ratchet" in node.attr.lower():
            return "attr:%s" % node.attr
    return None


def _ratchet_evidence_map(directory: "pathlib.Path" = SCRIPTS) -> dict:
    """{脚本名: 证据种类}；解析失败的文件按 `syntax-error` 计入（不静默漏掉）。"""
    out: dict = {}
    if not directory.is_dir():
        return out
    for p in sorted(directory.glob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            out[p.name] = "syntax-error"
            continue
        evidence = _ratchet_evidence(tree, p.name)
        if evidence:
            out[p.name] = evidence
    return out


def _load() -> dict:
    with io.open(REGISTER, encoding="utf-8") as fh:
        return json.load(fh)


def _workflow_text() -> str:
    return "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in sorted(WORKFLOWS.glob("*.yml"))
                   + sorted(WORKFLOWS.glob("*.yaml")))


def _ratchet_scripts(directory: "pathlib.Path" = SCRIPTS) -> list[str]:
    return sorted(_ratchet_evidence_map(directory))


def _measured_wired(directory: "pathlib.Path" = SCRIPTS,
                    workflow_text: "str | None" = None) -> list[str]:
    """**有结构性棘轮证据**且真的被某个 workflow 调用（按脚本名出现的字面）的脚本。

    注意：这一侧的"是否被调用"确实只能按文本判（workflow 就是文本，调用就是那行字），
    但**被判定为门禁脚本**的那一侧现在必须过 AST（见 `_ratchet_evidence`）——
    两者混为一谈正是 T10-R4 的病根。
    """
    wf = _workflow_text() if workflow_text is None else workflow_text
    return sorted(n for n in _ratchet_scripts(directory) if n in wf)


def _unregistered_evidenced(directory: "pathlib.Path" = SCRIPTS,
                            registered: "set | None" = None,
                            workflow_text: "str | None" = None) -> list[str]:
    """**有结构化棘轮证据**却没登记的脚本（绊线的主体，也是负向实测的靶子）。"""
    if registered is None:
        reg = _load()
        registered = set(reg["ci_wired"]) | {e["script"] for e in reg["unwired"]}
    return sorted(n for n in _ratchet_scripts(directory) if n not in registered)



def test_登记必须存在且结构完整():
    assert REGISTER.is_file(), "docs/GATE_WIRING.json 缺失 —— 门禁接线又回到无人记录的状态"
    reg = _load()
    assert reg.get("_why"), "登记必须说明为何需要它"
    # 2026-09-29 第二轮：19 个 unassessed 全部转为有测量支撑的分类 ⇒ 枚举随之扩展
    assert set(reg["_status_enum"]) == {
        "ci_wired", "requires_live_store", "manual_by_design",
        "has_write_path", "has_write_path_no_flag", "runs_red", "unassessed"}
    assert reg.get("_method"), "登记必须写明分类方法（否则分类不可复核）"
    assert reg.get("_must_be_wired"), "缺少 _must_be_wired：必须明示哪些门禁不可降级"
    assert reg.get("_must_be_wired_reason")


def test_ci_wired_必须与实测双向一致():
    """核心绊线：摘掉一个 CI 步骤、或接上一个新门禁而不登记，都会红。

    2026-10-06（T10-R4 根治）：原实现拿"全文正则"得到的 53 条与 `ci_wired` 全集做
    等值比较 —— 等值比较的一半（"登记里有、正则里没有"）本身就是**判据口径的假红**
    （正则里没有很可能只是那脚本把 ratchet 写在文字里）。
    现把双向一致性**限制在"有结构化证据的脚本"这一子集上**（两侧都真：
    既要求"被 workflow 真的调用"，也要求"登记为 ci_wired"）：

        · 有证据 + 被 workflow 调用 ⇒ 必须在 ci_wired（新接门禁不登记 ⇒ 红）
        · 有证据 + 在 ci_wired   ⇒ 必须真被 workflow 调用（登记成"已跑"其实没人跑 ⇒ 红）

    登记里那些**没有**结构化证据的条目（人工判定，见文件头的口径说明）不参与这条比较，
    但它们的字段合法性/僵尸/互斥仍由下面的用例逐条强校验。
    """
    reg = _load()
    evidence = set(_ratchet_scripts())
    wf = _workflow_text()
    declared = set(reg["ci_wired"])

    declared_with_evidence = declared & evidence
    evidenced_and_invoked = {n for n in evidence if n in wf}

    not_registered = sorted(evidenced_and_invoked - declared_with_evidence)
    not_invoked = sorted(declared_with_evidence - evidenced_and_invoked)
    assert not not_registered and not not_invoked, (
        "带结构化棘轮证据的门禁脚本与 ci_wired 不一致：\n"
        "  被 workflow 调用但没登记为 ci_wired : %s\n"
        "  登记为 ci_wired 但 workflow 里没有 : %s\n"
        "  ⇒ 同步 docs/GATE_WIRING.json（并确保 _must_be_wired 里的门禁没被摘掉）"
        % (not_registered, not_invoked))


def test_带结构化棘轮证据的脚本必须已登记():
    """绊线主体（T10-R4）：**不再按全文文本**判定，改按 AST 证据。

    负向实测见 `test_可失败性_注释里写关键词的脚本不得被算作门禁脚本` 与
    `test_可失败性_有证据但未登记的脚本必须被抓到`。
    """
    reg = _load()
    registered = set(reg["ci_wired"]) | {e["script"] for e in reg["unwired"]}
    unregistered = _unregistered_evidenced(SCRIPTS, registered)
    assert not unregistered, (
        "这些脚本**在代码里**（AST 可见）带棘轮语义，却既没接进 CI 也没登记：%s\n"
        "  证据：%s\n"
        "  ⇒ 把它们接进 ci.yml，或在 docs/GATE_WIRING.json 的 unwired 里给出 status + reason"
        % (unregistered, {n: _ratchet_evidence_map()[n] for n in unregistered}))


#: 2026-10-06 实测：登记 53 条里**有结构化证据**的 22 条；其余 31 条把"只降不升"
#: 表述在文字/基线机制里（人工判定，口径见文件头）。本条是**只降不升**的棘轮：
#: 新登记条目若没有结构化证据，会让这个数上升 ⇒ 必须是一次可见的、要写理由的动作。
_NO_EVIDENCE_LIMIT = 31


def test_登记里无结构化证据的条目数不得增长():
    """防止"登记表靠文字条目膨胀"：无证据条目只降不升。

    这不是"把文字条目判红"（它们今天合法，理由在登记里），而是要求**新增**这样的
    条目必须是一次自觉的动作：要么给出结构化证据（代码里真的解析 ratchet / 真的
    以 ratchet 命名），要么在同一次改动里下调 `_NO_EVIDENCE_LIMIT` 并在此写明理由。
    """
    reg = _load()
    registered = set(reg["ci_wired"]) | {e["script"] for e in reg["unwired"]}
    no_evidence = sorted(registered - set(_ratchet_scripts()))
    assert len(no_evidence) <= _NO_EVIDENCE_LIMIT, (
        "登记里没有结构化棘轮证据的条目由 %d 涨到 %d（上限 %d）：新增 %s\n"
        "  ⇒ 给这些脚本补上可判定的棘轮入口（如 `--ratchet`），或按纪律下调 "
        "`_NO_EVIDENCE_LIMIT` 并写明理由"
        % (_NO_EVIDENCE_LIMIT, len(no_evidence), _NO_EVIDENCE_LIMIT,
           sorted(set(no_evidence) - set(_ratchet_scripts(SCRIPTS)))))


def test_可失败性_注释里写关键词的脚本不得被算作门禁脚本(tmp_path):
    """**负向实测**（T10-R4 的原病）：只有注释含关键词 ⇒ 不许被算作门禁脚本。

    同一个临时目录里放两个文件：一个**只在注释/文档字符串里**写 `ratchet 只降不升`
    却根本不解析任何参数；另一个真的 `add_argument("--ratchet", ...)`。
    断言：前者**不在**证据集里，后者**在** —— 两者必须被分开。
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "comment_only.py").write_text(
        '"""这个脚本只是提到 ratchet / 只降不升，并不解析任何参数。"""\n'
        "# 只降不升：注释里再说一遍棘轮，依旧不构成门禁\n"
        'MSG = "ratchet"\n\n\ndef main():\n    return 0\n',
        encoding="utf-8")
    (scripts / "real_gate.py").write_text(
        "import argparse\n\n\n"
        "def main():\n"
        "    ap = argparse.ArgumentParser()\n"
        '    ap.add_argument("--ratchet", action="store_true")\n'
        "    return 0\n",
        encoding="utf-8")
    evidence = _ratchet_evidence_map(scripts)
    assert "comment_only.py" not in evidence, (
        "**只有注释/文档字符串含关键词的脚本被判成了棘轮门禁脚本** —— 这正是 T10-R4 假红的成因：%r"
        % (evidence.get("comment_only.py"),))
    assert evidence.get("real_gate.py") == "argflag:--ratchet", (
        "真解析了 `--ratchet` 的脚本没被判出来 ⇒ 收紧过头，会把真门禁漏掉：%r" % (evidence,))
    # 反事实（本用例承重的证明）：把判定换回**原实现的全文正则** ⇒ 同一个文件**会**被骗。
    # 若这条不成立，说明这个 fixture 根本没在证明任何东西（§13.4 变异检验纪律）。
    legacy_rx = re.compile(r"ratchet|只降不升|只许减不许增|棘轮", re.I)
    assert legacy_rx.search((scripts / "comment_only.py").read_text(encoding="utf-8")), (
        "反事实不成立：原全文正则本该被这个「只有注释含关键词」的文件骗到")
    # 顺带把"绊线本体"也证伪一次：没登记 ⇒ 必须被 `_unregistered_evidenced` 抓到
    assert _unregistered_evidenced(scripts, registered=set()) == ["real_gate.py"], (
        "有证据但未登记的脚本没被绊线抓到 ⇒ 判据无判别力")


def test_可失败性_有证据且被workflow调用但未登记必须红(tmp_path):
    """绊线的另一半：**新接进 CI 的门禁脚本**不登记 ⇒ `_measured_wired` 与登记不一致。"""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "new_gate.py").write_text(
        'import argparse\n\n\n'
        'def main():\n    ap = argparse.ArgumentParser()\n'
        '    ap.add_argument("--ratchet", action="store_true")\n    return 0\n',
        encoding="utf-8")
    wf = "python scripts/new_gate.py --ratchet\n"
    assert _measured_wired(scripts, workflow_text=wf) == ["new_gate.py"]
    assert _unregistered_evidenced(scripts, registered=set(), workflow_text=wf) == ["new_gate.py"], (
        "被 workflow 调用且带证据的脚本未登记，绊线必须能报出来")


def test_must_be_wired_里的门禁必须真的在跑():
    """防止把"门禁降级成 unwired 登记项"当成修复方式。"""
    reg = _load()
    missing = [n for n in reg["_must_be_wired"] if n not in reg["ci_wired"]]
    assert not missing, (
        "_must_be_wired 里的门禁没被接入 CI：%s ⇒ 这不是可选项，"
        "它们守护的是本轮审计根因" % missing)


def test_每个棘轮脚本都必须被登记():
    """新增一个棘轮脚本而不接线也不登记 ⇒ 红。"""
    reg = _load()
    registered = set(reg["ci_wired"]) | {e["script"] for e in reg["unwired"]}
    unregistered = sorted(set(_ratchet_scripts()) - registered)
    assert not unregistered, (
        "这些棘轮脚本既没接进 CI 也没登记：%s ⇒ 把它们接进 ci.yml，"
        "或在 docs/GATE_WIRING.json 的 unwired 里给出 status + reason" % unregistered)


def test_unwired_条目字段合法():
    reg = _load()
    enum = set(reg["_status_enum"])
    for e in reg["unwired"]:
        assert e.get("script"), "条目缺 script"
        assert e.get("status") in enum, (
            "%s 的 status=%r 不在枚举内" % (e.get("script"), e.get("status")))
        assert (e.get("reason") or "").strip(), "%s 缺 reason" % e.get("script")
        assert e["status"] != "ci_wired", (
            "%s 标成 ci_wired 却躺在 unwired 里 —— 分类自相矛盾" % e["script"])


def test_ci_wired_与_unwired_必须互斥():
    """同一个脚本不许**同时**出现在两个集合里。

    2026-09-29（本轮自查发现的登记 bug）：`undefined_global_audit.py` 因为在探针时
    rc=1（随后被 `_to_regex` 修复变成 exit 0 并写进 ci.yml），同时出现在 `ci_wired`
    与 `unwired` 里 —— 而当时的判据只查"status 不等于 ci_wired"，**没查集合互斥**，
    所以没抓到。这条补上：登记的两个集合必须真正互斥。
    """
    reg = _load()
    both = sorted(set(reg["ci_wired"]) & {e["script"] for e in reg["unwired"]})
    assert not both, (
        "这些脚本同时被登记为「已在 CI 跑」和「未接线」：%s ⇒ 登记自相矛盾" % both)


def test_不允许僵尸登记():
    reg = _load()
    stale = [e["script"] for e in reg["unwired"]
             if not (SCRIPTS / e["script"]).is_file()]
    assert not stale, "登记了但脚本已不存在：%s ⇒ 删除这些条目" % stale


def test_头部计数必须与实测一致():
    """头部计数必须与**登记自身**和**可判定的那份事实**都对得上。

    2026-10-06（T10-R4）：原实现拿头部 `_total_ratchet_scripts` 与"全文正则命中数"
    比 —— 那会把判据口径的假红变成"登记不实"的指控。现拆成两个**都能判定**的关系：

        · `_total_ratchet_scripts` == len(ci_wired) + len(unwired)（登记自洽：它说的是
          "登记里收了多少条棘轮脚本"，这个数必须等于条目数，否则登记内部就在骗人）；
        · 有结构化证据的脚本数 <= `_total_ratchet_scripts`（登记必须**至少覆盖**所有能被
          代码判定的棘轮脚本；漏登记由 `test_带结构化棘轮证据的脚本必须已登记` 指名）。
    """
    reg = _load()
    entries = len(reg["ci_wired"]) + len(reg["unwired"])
    assert reg["_total_ratchet_scripts"] == entries, (
        "登记头部说收了 %s 条棘轮脚本，而 ci_wired(%d) + unwired(%d) = %d 条 ⇒ 登记自相矛盾"
        % (reg["_total_ratchet_scripts"], len(reg["ci_wired"]), len(reg["unwired"]), entries))
    evidenced = _ratchet_scripts()
    assert len(evidenced) <= entries, (
        "有结构化棘轮证据的脚本 %d 个 > 登记条目 %d 条 ⇒ 有脚本没被登记：%s"
        % (len(evidenced), entries, sorted(set(evidenced) - (
            set(reg["ci_wired"]) | {e["script"] for e in reg["unwired"]}))))
    assert reg["_wired_count"] == len(reg["ci_wired"]), "_wired_count 与 ci_wired 长度不符"
    assert reg["_unwired_count"] == len(reg["unwired"]), "_unwired_count 与 unwired 长度不符"
    assert reg["_unassessed_count"] == sum(
        1 for e in reg["unwired"] if e["status"] == "unassessed"), "_unassessed_count 不实"
    # 已登记为 ci_wired 且**有结构化证据**的，必须真的被某个 workflow 调用
    wf = _workflow_text()
    invocation_regex = re.compile(r"(?:^|[\s\"'\[])%s(?=[\s\"'\],]|$)",
                                  re.M)
    for name in sorted(set(reg["ci_wired"]) & set(evidenced)):
        assert invocation_regex.search(wf), (
            "%s 被登记为「已在 CI 跑」，但任何 workflow 里都找不到调用形态 ⇒ 登记成了谎言"
            % name)


def test_两个本轮新接的门禁必须确实被调用():
    """点对点确认：structure_gate 与 direct_pg_writers_audit 真在 ci.yml 里。"""
    wf = _workflow_text()
    for script, flag in (("structure_gate.py", ""),
                         ("direct_pg_writers_audit.py", "--ratchet")):
        assert script in wf, "%s 未被任何 workflow 调用" % script
        if flag:
            assert re.search(re.escape(script) + r"\s+" + re.escape(flag), wf), (
                "%s 被调用了但**没带 %s** —— 该脚本无此参数时 return 0，"
                "等于只报告不判失败（实测：不加 --ratchet 时 exit 0）" % (script, flag))
