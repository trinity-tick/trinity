"""PG 适配器契约判据（G6R2/t131）—— **断言"这个类还能用" + "原有成员仍在原位"**。

## 为什么有这条（G6R/t130 的事故）
2026-10-08 09:11 一次"按锚点插入模块级 helper"的改动把 `PostgreSQLAdapter` 的**类体截断在 982 行**，
其后 **65 个方法变成该 helper 的嵌套函数体** ⇒ 运行期类只剩 13 个方法 ⇒ `StorageAdapter` 的
**29 个抽象方法无人实现** ⇒ **PG 写入路径整条坏掉**，而 `ast.parse` / `py_compile` / `import` **全部通过**
⇒ **没有任何判据发现它**（活了约 40 分钟才被人撞上）。

## 本判据的六条（+ 一条牙齿）
| # | 断言 | 抓什么 |
|---|---|---|
| T1 | `PostgreSQLAdapter.__abstractmethods__ == 0` | 事故的**直接症状** |
| T2 | `PostgreSQLAdapter(auto_connect=False)` **能实例化** | **端到端**（不只读属性） |
| T3 | **类体直接方法数 ≥ `HEAD` 版同类计数** | 事故的**主症**（65 个方法被搬走） |
| T4 | **类体区间内不得出现 col-0 的 `def`** | 类体被截断的形态（**区间用 AST 定，不数缩进**） |
| T5 | **模块级函数的体内嵌套 def 数 ≤ 4** | ⭐ **本次事故的真指纹**（65 个方法被吞进一个函数体） |
| T6 | ⭐ **`HEAD` 版类成员集合 ⊆ 工作区类成员集合** | ⭐ **"原有成员仍在原位"**（第四件套的机械形态） |
| T7 | **牙齿**：对**合成截断源**跑 T3/T5/T6 ⇒ **必须红** | 证明这判据**能区分**（不是恒绿） |

⚠️ **T4 单独抓不到本次事故**（本事故里 `_conflict_tokens` 落在**类结束行之后**，
即"区间之外"）⇒ 实测见 `evidence/g6r2_*.txt`；**真正抓住它的是 T3/T5/T6**。
"""
from __future__ import annotations

import ast
import importlib
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REL = "trinity/adapters/postgresql.py"
CLS = "PostgreSQLAdapter"


# ── 工具（**全部 AST，不用缩进字符串**）────────────────────────────────────────
def _head_src(rel: str = REL) -> str:
    r = subprocess.run(["git", "show", "HEAD:%s" % rel], cwd=str(REPO), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, "取不到 HEAD 版 %s：%s" % (rel, r.stderr)
    return r.stdout


def _tree(src: str) -> ast.Module:
    return ast.parse(src)


def _class_node(src: str, cls: str = CLS):
    for n in ast.walk(_tree(src)):
        if isinstance(n, ast.ClassDef) and n.name == cls:
            return n
    raise AssertionError("找不到类 %s" % cls)


def _class_methods(src: str, cls: str = CLS):
    """类体的**直接**方法名（不含嵌套）。"""
    return [m.name for m in _class_node(src, cls).body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _col0_defs_inside_class_range(src: str, cls: str = CLS):
    """**类体区间内**（AST 的 lineno..end_lineno）出现的 **col-0** `def`/`class`。

    ⚠️ 自伤修正（t131 自测抓到）：首版把**类自己的定义行**（`class PostgreSQLAdapter(...)` 本身
    就是 col-0 且以 `class ` 开头）算成了命中 ⇒ **假红**。现在**排除类自身那一行**（以及其装饰器行）。
    ⚠️ 另：本断言**单独**抓不到 G6R 那次事故（那次 col-0 def 落在类**结束行之后** ⇒ 区间外）——
    真正抓住它的是 T3/T5/T6（见模块头注）。这里保留它是为了抓"类体被就地插断"的另一类形态。
    """
    node = _class_node(src, cls)
    lines = src.splitlines()
    own = {node.lineno} | {d.lineno for d in node.decorator_list}
    hits = []
    for ln in range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1):
        if ln in own:
            continue
        line = lines[ln - 1] if ln - 1 < len(lines) else ""
        if line and not line[:1].isspace() and (line.startswith("def ")
                                                or line.startswith("async def ")
                                                or line.startswith("class ")):
            hits.append((ln, line[:60]))
    return hits


def _module_defs_swallowing_nested(src: str, limit: int = 4):
    """模块级函数的**体内嵌套 def 数** > limit 的（本次事故的真指纹：一个 helper 吞了 65 个）。"""
    bad = []
    for n in _tree(src).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nested = sum(1 for x in ast.walk(n) if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and x is not n)
            if nested > limit:
                bad.append((n.name, n.lineno, nested))
    return bad


def _current_src() -> str:
    return (REPO / REL).read_text(encoding="utf-8", errors="replace")


# ── T1/T2：运行期 ─────────────────────────────────────────────────────────────
def test_T1_abstractmethods_zero():
    mod = importlib.import_module("trinity.adapters.postgresql")
    am = sorted(getattr(getattr(mod, CLS), "__abstractmethods__", []) or [])
    assert am == [], "PostgreSQLAdapter 仍有 %d 个抽象方法未实现：%s" % (len(am), am)


def test_T2_instantiable():
    mod = importlib.import_module("trinity.adapters.postgresql")
    obj = getattr(mod, CLS)(auto_connect=False)          # 构造期不连库
    assert obj.__class__.__name__ == CLS


# ── T3/T4/T5/T6：结构（HEAD 为基线，**不硬编码数字**）──────────────────────────
def test_T3_class_method_count_not_below_head():
    head, cur = _head_src(), _current_src()
    n_head, n_cur = len(_class_methods(head)), len(_class_methods(cur))
    assert n_cur >= n_head, ("类体直接方法数从 HEAD 的 %d 掉到 %d ⇒ 有方法被搬出类体"
                             "（G6R/t130 事故形态）" % (n_head, n_cur))


def test_T4_no_col0_def_inside_class_range():
    """⚠️ **这条不是抓住本次事故的那条**（事故里 col-0 def 落在类结束行【之后】⇒ 区间外）。

    ⚠️ **并且它在"已解析文件"上几乎不可能命中真损坏**：类体区间 = AST 的 `lineno..end_lineno`，
    区间内的任何语句都必须是缩进的 ⇒ 出现"col-0 的 def"只可能来自**多行字符串/注释里的文本**
    ⇒ 它是**文本层嗅探器**（有假阳），保留只为抓"就地插断"的另一类形态。
    ⇒ ⭐ **真正抓本次事故的是 T3（方法数 ≥ HEAD）+ T4b（类体跨度不得短于 HEAD）+ T5 + T6。**
    """
    hits = _col0_defs_inside_class_range(_current_src())
    assert hits == [], "类体区间内出现 col-0 def/class（文本层嗅探命中）：%s" % hits


def test_T4b_class_body_span_not_shorter_than_head():
    """⭐ **截断的必然后果**：类体跨度（`end_lineno - lineno`）会显著变短（本次 2543 → 882）。

    这是"类体被截断"的**可 AST 断言**形态（比"区间内 col-0 def"那条更贴近症状本身）。
    """
    h, c = _class_node(_head_src()), _class_node(_current_src())
    span_h = getattr(h, "end_lineno", h.lineno) - h.lineno
    span_c = getattr(c, "end_lineno", c.lineno) - c.lineno
    assert span_c >= span_h, ("类体跨度从 HEAD 的 %d 行降到 %d 行 ⇒ 类体疑似被截断"
                              % (span_h, span_c))


def test_T5_no_module_def_swallowing_many_nested_defs():
    """阈值 **8**（2026-10-08 队长批准从 4 提到 8）。

    理由：**4 对测试文件偏敏感** —— 实测已有 2 处**合法**命中（测试里成组定义嵌套 helper）
    ⇒ "报警太宽 = 常红 = 没人看"。本次事故的量级是 **65**，阈值 8 仍稳稳抓住它。
    """
    bad = _module_defs_swallowing_nested(_current_src(), limit=8)
    assert bad == [], "模块级函数体内嵌套了过多 def（疑似吞掉了后面的类/函数）：%s" % bad


def test_T6_head_members_still_present():
    head = set(_class_methods(_head_src()))
    cur = set(_class_methods(_current_src()))
    lost = sorted(head - cur)
    assert lost == [], ("HEAD 里存在、工作区类体里已消失的成员（%d 个）：%s…"
                        % (len(lost), lost[:10]))


# ── T7：牙齿（合成截断 ⇒ 必须红）─────────────────────────────────────────────
def _synthetic_truncation(head_src: str) -> str:
    """复刻 G6R/t130 的形态：把一段**模块级** `def`（col-0）插到类体**中间**。

    ⚠️ 插点选**某个类方法的 def 行之前**（AST 给的行号 ⇒ 一定是语句边界）⇒ 该 col-0 def
    会把类截断在此，**其后所有方法变成它的嵌套体** —— 与事故形态逐字一致。
    """
    node = _class_node(head_src)
    methods = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert len(methods) >= 4, "样本类方法太少，牙齿测试不适用"
    anchor = methods[len(methods) // 2].lineno          # 中位方法（语句边界）
    lines = head_src.splitlines(keepends=True)
    injection = ("\ndef _accidental_col0_helper(text):\n    return text\n\n\n")
    return "".join(lines[:anchor - 1]) + injection + "".join(lines[anchor - 1:])


def test_T7_teeth_synthetic_truncation_is_caught():
    head = _head_src()
    bad = _synthetic_truncation(head)
    n_head, n_bad = len(_class_methods(head)), len(_class_methods(bad))
    lost = set(_class_methods(head)) - set(_class_methods(bad))
    swallowed = _module_defs_swallowing_nested(bad, limit=4)
    assert n_bad < n_head, "牙齿失效：合成截断后方法数没下降（%d vs %d）" % (n_bad, n_head)
    assert lost, "牙齿失效：成员集合没有丢失"
    assert swallowed, "牙齿失效：没有抓到'模块级函数吞掉后续 def'的指纹"
