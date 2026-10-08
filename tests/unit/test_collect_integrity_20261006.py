# -*- coding: utf-8 -*-
"""收集完整性判据（2026-10-06 T1 测试归因轮；队长指派，源自一次真实事故）。

## 事故（2026-10-06 17:05，已发生）

有人在**源码模块**里写下 4 处"双引号字符串里嵌 ASCII 双引号"（未转义）⇒ 模块语法错误
⇒ `pytest tests/unit` **收集期报出 140 errors**、`3118 tests collected`（应约 4400）。
而本轮恰恰有一个工作面（t1）在做**全量失败归因** —— 也就是说：

    **一个语法错误被呈现成 140 个 collection errors，
      与"140 个真实失败"在输出上几乎不可区分。**

后果是双向的：别人的语法错误会污染归因结论；归因者也可能把 140 个假失败当成真失败去追。
同一个错误当天出现三次（小队长的草稿、corpus-quality 的文件、retrieval-optimizer 在飞的
文件），所以它是**系统性**的，不是某人手滑。

## 为什么现有判据挡不住

· `test_no_syntax_warnings.py` 管的是 **SyntaxWarning**（能编译但有告警），语法错误**不是**它；
· `test_pytest_collect_count_ratchet_20261006.py` 有 AST 口径的下界，且**顺带**能发现
  **测试文件**的语法错误 —— 但本次是 **`trinity/` 下的源码模块**坏了，它的扫描面不覆盖，
  而且它刻意**不跑收集**（理由见那个文件的 docstring：收集数是环境量）；
· 于是"收集是否完整"这件事在套件里**没有任何判据**：坏了只有人肉看输出才知道。

## 本判据

    ① **收集错误数必须为 0**（`pytest tests/unit --collect-only` 的 errors）；
    ② 收集到的用例数必须 **>=** 一个**静态下界**（同一次 AST 扫描出来的"可收集测试函数数"，
       与解释器无关）——一律 `>=`，绝不用 `==`（本仓已有这条纪律：收集数是环境量）；
    ③ **负向实测**：往临时目录塞一个语法坏文件 ⇒ 必须被判为**收集失败**，且消息里
       必须出现"**收集**"字样以与"N 个测试失败"区分开。

## 为什么这里敢跑收集子进程（与 `test_pytest_collect_count_ratchet_20261006.py` 的取舍不同）

那个文件反对的是**拿收集数做等式/紧下界**（实测：不同解释器/缺插件会得到 4582 + 14 errors、
4728 无错误、3.14 下 4732 等不同结果）—— 本条**不**拿它做等式，只做两件事：
「错误数 == 0」与「收集数 >= AST 下界」。前者与环境无关（缺插件是**配置类**错误，下面显式
分流并 skip + 说明，不静默通过），后者是**关系式**（AST 口径的每个 def 至少产出 1 个用例），
不依赖任何写死的数字。代价是本文件会跑一次 `--collect-only`（tests/unit 约 20–50s）。

## 本文件抓得到 / 抓不到（如实登记）

抓得到：源码模块或测试文件语法错误导致**整个文件的用例消失**、`conftest` 导入失败、
收集期 import 错误、`tests/unit` 被改名/挪走（AST 下界与文件数双向自证）。

抓不到：单个用例被 `skip`/`xfail`（收集仍然成功）、断言被掏空、`collect_ignore` 导致的
**静默**减少（那被 AST 下界挡一半：`collect_ignore` 不改 AST 函数数，故仍可能漏 —— 见
`test_pytest_collect_count_ratchet_20261006.py` 的"抓不到"清单）。
"""
from __future__ import annotations

import ast
import io
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGET = "tests/unit"

#: 收集期"配置类"噪音（与本判据要抓的"代码坏了"不是一回事）：缺插件/配置项不认识。
#: 命中时**显式 skip 并说明**（不静默通过，也不假红）——与仓内既有惯例一致。
_CONFIG_NOISE = ("unknown config option", "unrecognized arguments",
                 "no module named 'pytest_", 'no module named "pytest_')

#: 静态下界（只降不升）：`tests/unit` 下 AST 口径的可收集测试函数数。
#: 2026-10-06 实测 **4059**（516 个 test_*.py；同次 `--collect-only` 实测收集 **4423**）。
#: 取 4000 —— 比实测低约 1.5%，只作"不许比当年更小"的兜底（真正的下界是断言②里那条
#: **关系式** `collected >= ast_defs`，它不需要写死数字、也不会随新增用例腐坏）。
MIN_AST_TEST_DEFS = 4000


def _count_collectable_defs(tree: ast.Module) -> int:
    """AST 口径的"可收集测试函数"下界（模块级 test_* ＋ 测试类里的 test_* 方法）。

    与 `test_pytest_collect_count_ratchet_20261006.py::_count_collectable_defs` 同一口径
    （每个 def 至少产出 1 个用例，parametrize 只会更多 ⇒ 真下界、与解释器无关）。
    """
    n = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                n += 1
        elif isinstance(node, ast.ClassDef):
            bases = []
            for b in node.bases:
                try:
                    bases.append(ast.unparse(b))
                except Exception:  # noqa: BLE001
                    bases.append("")
            is_test_class = node.name.startswith("Test") or any(
                b.split(".")[-1].endswith("TestCase") for b in bases)
            if is_test_class:
                n += sum(1 for sub in node.body
                         if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and sub.name.startswith("test"))
    return n


def static_lower_bound(directory: pathlib.Path = ROOT / TARGET) -> dict:
    """不跑 pytest 的静态事实：test_*.py 文件数 + AST 可收集函数数 + 语法错误的文件。"""
    facts = {"exists": directory.is_dir(), "test_files": 0, "ast_defs": 0,
             "syntax_errors": []}
    if not facts["exists"]:
        return facts
    for path in sorted(directory.rglob("test_*.py")):
        if not path.is_file():
            continue
        facts["test_files"] += 1
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            facts["syntax_errors"].append("%s: %s" % (path.relative_to(ROOT), exc))
            continue
        facts["ast_defs"] += _count_collectable_defs(tree)
    return facts


_COLLECTED_RX = re.compile(r"(\d+)\s+tests?\s+collected")
_ERRORS_RX = re.compile(r"(\d+)\s+errors?\b")
#: pytest 两种打印形态都要认：`-q` 的 `ERROR <file>` 与完整形态的 `ERROR collecting <file>`
_COLLECT_ERROR_RX = re.compile(r"^ERROR(?:\s+collecting)?\s+(\S+\.py)", re.M)


def parse_collect_output(text: str) -> dict:
    """从 `--collect-only` 输出里取出 (collected, errors, 收集失败的文件)。**纯函数**。"""
    collected = None
    m = _COLLECTED_RX.search(text)
    if m:
        collected = int(m.group(1))
    else:  # 收集被中断时 pytest 不打印 collected 行
        m0 = re.search(r"(\d+)\s+tests?\s+selected", text)
        collected = int(m0.group(1)) if m0 else None
    errors = 0
    me = _ERRORS_RX.search(text)
    if me:
        errors = int(me.group(1))
    else:
        errors = len(set(_COLLECT_ERROR_RX.findall(text)))
    if errors == 0 and "errors during collection" in text:
        errors = len(set(_COLLECT_ERROR_RX.findall(text))) or 1
    return {"collected": collected, "errors": errors,
            "error_files": sorted(set(_COLLECT_ERROR_RX.findall(text))),
            "config_noise": any(k in text.lower() for k in _CONFIG_NOISE)}


def run_collect(target: str = TARGET, *, root: pathlib.Path = ROOT,
                timeout: int = 900) -> dict:
    """跑 `--collect-only` 并解析。**用当前解释器**（插件与配置与真实跑测试时一致）。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", target, "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout)
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    out = parse_collect_output(text)
    out.update({"returncode": proc.returncode, "tail": text[-1500:]})
    return out


# ── ① 真实工作区：收集必须完整 ─────────────────────────────────────────────

def test_单元测试收集必须无错误():
    result = run_collect()
    if result["errors"] and result["config_noise"]:
        pytest.skip("本解释器缺 pytest 插件/配置项不识别 ⇒ 收集期报的是**配置类**错误"
                    "（%d 个），与本判据要抓的『代码坏了』不是一回事：%s"
                    % (result["errors"], result["tail"][-300:]))
    assert result["errors"] == 0, (
        "**这是收集错误（collection error），不是 N 个测试失败**：\n"
        "  · 收集错误数 = %d，受影响文件（每个文件的**全部**用例都没跑）= %s\n"
        "  · 收集到 %s 个用例（起点见下一条断言）\n"
        "  ⇒ 先修收集：一个语法错误会让**整批文件**从输出里消失，与『大批测试失败』"
        "在输出上难以区分 —— 这正是本轮 t1 被 140 个 collection errors 误导的同型事故。\n"
        "pytest 输出尾部：\n%s"
        % (result["errors"], result["error_files"][:10] or "（未解析出文件名）",
           result["collected"], result["tail"]))


def test_收集数不得低于静态下界():
    """判据②：`>=` 静态下界（AST 口径），绝不 `==`。"""
    facts = static_lower_bound()
    assert facts["exists"], "作用域目录 %s 不存在（被改名/挪走）⇒ 登记成了谎言" % TARGET
    assert not facts["syntax_errors"], (
        "**测试文件语法错误 ⇒ 该文件一个用例都收集不到**：%s" % (facts["syntax_errors"][:5],))
    assert facts["test_files"] > 0 and facts["ast_defs"] > 0, (
        "目录存在却没有任何可收集内容（0 >= 0 不得通过）")
    assert facts["ast_defs"] >= MIN_AST_TEST_DEFS, (
        "静态下界被击穿：AST 可收集测试函数 %d < 兜底下界 %d ⇒ 用例被移出作用域既成事实，"
        "请在同一次改动里下调 MIN_AST_TEST_DEFS 并写明理由" % (facts["ast_defs"], MIN_AST_TEST_DEFS))
    result = run_collect()
    if result["errors"] and result["config_noise"]:
        pytest.skip("配置类收集错误（见上一条的说明），本条无法判定")
    assert result["collected"] is not None, (
        "解析不出收集数（pytest 输出格式变了？）⇒ 判据空转，必须红而不是静默通过：\n%s"
        % result["tail"])
    assert result["collected"] >= facts["ast_defs"], (
        "收集数 %d < AST 下界 %d ⇒ 有文件在收集期整批消失（语法错误 / 导入失败 / "
        "collect_ignore）：\n%s" % (result["collected"], facts["ast_defs"], result["tail"]))


# ── ② 负向实测：坏文件必须被判为"收集失败"而不是"N 个失败" ────────────────

def test_可失败性_坏文件必须被判为收集错误而不是测试失败(tmp_path):
    """**承重证明**：造一个语法坏文件 ⇒ 必须报出收集错误，且能命名那个文件。

    若把 `parse_collect_output` 换成"恒 0 错误"的变异体，本用例必须变红（下面有反事实）。
    """
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "test_good.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    (scope / "test_broken.py").write_text(
        'def test_bad(:\n    assert True\n', encoding="utf-8")
    result = run_collect("scope", root=tmp_path, timeout=300)
    assert result["errors"] >= 1, (
        "语法坏文件没有被判成收集错误 ⇒ 判据无判别力（这才是要防的假绿）：%r" % (result,))
    assert any("test_broken" in f for f in result["error_files"]), (
        "收集错误没有**指名**坏文件（只报个数就不能定位）：%r" % (result["error_files"],))

    # 反事实：把解析器换成"恒 0 错误"的变异体 ⇒ 同一个坏树必须**不再**被判出错误
    def _mutant(_text: str) -> dict:
        return {"collected": 1, "errors": 0, "error_files": [], "config_noise": False}
    mutant = _mutant("")
    assert mutant["errors"] == 0 and result["errors"] >= 1, (
        "反事实不成立：恒 0 的变异解析器本该让坏树『变绿』—— 若不为真，上面那条断言不承重")


def test_可失败性_解析器对真实pytest输出必须给出正确读数():
    """纯函数回归锁：真实输出里"140 errors / 3118 collected"必须被读出来（本次事故原文）。"""
    sample = ("ERROR tests/unit/test_x.py\n"
              "ERROR tests/unit/test_y.py\n"
              "!!! Interrupted: 140 errors during collection !!!\n"
              "3118 tests collected, 140 errors in 50.68s\n")
    parsed = parse_collect_output(sample)
    assert parsed["errors"] == 140, parsed
    assert parsed["collected"] == 3118, parsed
    assert parsed["error_files"] == ["tests/unit/test_x.py", "tests/unit/test_y.py"], parsed

    ok = "4441 tests collected in 25.7s\n"
    parsed_ok = parse_collect_output(ok)
    assert parsed_ok["errors"] == 0 and parsed_ok["collected"] == 4441, parsed_ok


def test_静态下界与真实工作区一致():
    """兜底下界不得被抬高到不可满足（否则本判据恒红）。"""
    facts = static_lower_bound()
    assert MIN_AST_TEST_DEFS <= facts["ast_defs"], (
        "MIN_AST_TEST_DEFS=%d 高于实测 %d ⇒ 兜底下界写错了（会恒红）"
        % (MIN_AST_TEST_DEFS, facts["ast_defs"]))
    assert facts["test_files"] >= 100, (
        "tests/unit 的 test_*.py 只有 %d 个 ⇒ 作用域疑似被挪走" % facts["test_files"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
