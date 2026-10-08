# -*- coding: utf-8 -*-
r"""G12（t155）：检索排除口径 **唯一来源** + **下推到 SQLite**（必须在取 top-k **之前**）

## 背景（实测）

· 排除此前**只在 PG 侧实现**（`trinity/adapters/_pg_search.py` 的 `category != ALL(%s)`），
  `trinity/adapters/sqlite/**` **0 处** ⇒ ⭐ **换后端即静默改变语义**（`perception` 会重新进入检索；
  SQLite 侧 active 的 `perception` = **4,513** 条）。
· ⭐ 而且**两份表**：`core/client/_search.py:49/51`（`["perception"]` + 评测语料，带 env 开关）
  vs `_hybrid_search.py:269/306`（内联 `["perception","self-reflection"]`）。

## 本判据钉住的四件事

① **正向**：SQLite 侧下推生效 ⇒ 同一查询下 `perception` **0 行**；PG 侧语义仍在（`category != ALL(`）。
② **反向**：**不在排除表里**的类目（`kb_harvested`）**仍能被检出** ⇒ 证明没把整个检索关掉。
③ **牙齿**：把排除**置空**（`exclude_categories=[]`）⇒ `perception` 行**必须重新出现** ⇒ 证明"是那条排除在起作用"。
④ **收敛**：两份表已收敛为**唯一来源** —— 判据**从唯一来源 import**（⛔ 不硬编码字符串列表），
   并断言「`_hybrid_search.py` 不再自带 `["perception"…]` 字面量」「两处调用**结构同值**」
   「SQLite 适配器的排除条件落在 `where` 组装**之前**」。

⚠️ 口径与限制（如实登记）：
  · **PG 侧不连库**（禁止写生产库）⇒ PG 侧只做**源码级**断言（SQL 片段仍在）；
  · 行尾：本仓同一目录下既有 LF 也有 CRLF（实测 `core/client/_search.py` 是 **CRLF**）⇒ 本判据一律按文本读，不假设行尾。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SQLITE_SEARCH = ROOT / "trinity" / "adapters" / "sqlite" / "_search.py"
HYBRID = ROOT / "trinity" / "core" / "client" / "_hybrid_search.py"
CLIENT_SEARCH = ROOT / "trinity" / "core" / "client" / "_search.py"
PG_SEARCH = ROOT / "trinity" / "adapters" / "_pg_search.py"

#: ⭐ **唯一来源**：判据只从这里取，不硬编码任何类目名
from trinity.core.client._search import _RETRIEVAL_EXCLUDE_CATEGORIES  # noqa: E402

MARK = "G12XMARKER"


def _seed(adapter) -> None:
    """造两行：一行在排除表里（perception），一行不在（kb_harvested）。"""
    adapter.store_memory(content="%s 感知流快照" % MARK, category="perception",
                         persona_id="p1", agent_id="a1")
    adapter.store_memory(content="%s 知识库条目 WMS 波次引擎" % MARK, category="kb_harvested",
                         persona_id="p1", agent_id="a1")


def _cats(adapter, exclude) -> list:
    rows = adapter.search_memories(MARK, exclude_categories=exclude, top_k=10, touch=False)
    return [r.get("category") for r in rows]


# ── ① 正向 + ② 反向 + ③ 牙齿（同一个临时库、同一查询）────────────────────────

def test_下推生效且不误伤(adapter) -> None:
    """① 排除生效（perception 0 行）＋ ② 反向（不在表里的类目仍能检出）。"""
    _seed(adapter)
    cats = _cats(adapter, list(_RETRIEVAL_EXCLUDE_CATEGORIES))
    assert "perception" not in cats, "排除未生效：perception 仍被返回（SQLite 侧下推失效）"
    assert "kb_harvested" in cats, (
        "反向失败：不在排除表里的类目也检不到 ⇒ 说明把整个检索关掉了，而不是只排除 perception")


def test_牙齿_把排除置空则感知重新出现(adapter) -> None:
    """③ 牙齿：`exclude_categories=[]` ⇒ perception **必须重新出现**（证明是排除在起作用）。"""
    _seed(adapter)
    cats = _cats(adapter, [])
    assert "perception" in cats, (
        "牙齿失败：置空排除后 perception 仍不出现 ⇒ 检索路径本身就没把它算进来，"
        "那么「排除生效」这个结论就是假的")


def test_牙齿_None_与空表等价(adapter) -> None:
    """③ 补充：`None`（不传）也必须等同"无排除" ⇒ 防止"默认值偷偷排除"。"""
    _seed(adapter)
    rows = adapter.search_memories(MARK, top_k=10, touch=False)
    assert "perception" in [r.get("category") for r in rows], (
        "不传 exclude_categories 时 perception 不出现 ⇒ 适配器有隐藏默认排除（不应有）")


# ── ④ 收敛：唯一来源 + 两处同值 + 位置正确（全部从源码/AST 判定，不硬编码）────

def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def test_hybrid_不再自带排除表字面量() -> None:
    """④ 收敛：`_hybrid_search.py` 不得再写 `["perception"…]` 字面量，必须从唯一来源取。"""
    src = _src(HYBRID)
    assert re.search(r"from\s+\._search\s+import\s+[^\n]*_RETRIEVAL_EXCLUDE_CATEGORIES", src), (
        "_hybrid_search 没有从唯一来源（core/client/_search）取排除表")
    assert not re.search(r'\[\s*["\']perception["\']', src), (
        "仍在自带排除表字面量 ⇒ 又是一个漂移点（应收敛为唯一来源）")


def test_hybrid_两处调用结构同值() -> None:
    """④ 收敛：两条出口的 `exclude_categories=` 表达式必须**结构相同**（不许一处改一处没改）。"""
    tree = ast.parse(_src(HYBRID))
    exprs = [ast.dump(k.value) for n in ast.walk(tree) if isinstance(n, ast.Call)
             for k in n.keywords if k.arg == "exclude_categories"]
    assert len(exprs) >= 2, "预期 ≥2 处调用点（两条出口），实测 %d" % len(exprs)
    assert len(set(exprs)) == 1, "两处 exclude_categories 表达式不同 ⇒ 口径会分叉：%s" % exprs


def test_hybrid_的排除表包含唯一来源() -> None:
    """④ 收敛：`_hybrid_search` 的排除集合 ⊇ 唯一来源（否则它把那几个类目漏掉了）。"""
    tree = ast.parse(_src(HYBRID))
    found = False
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            for k in n.keywords:
                if k.arg == "exclude_categories":
                    names = {x.id for x in ast.walk(k.value) if isinstance(x, ast.Name)}
                    found = found or any("EXCL" in nm or "RETRIEVAL" in nm for nm in names)
    assert found, "两处调用都没引用唯一来源常量"


def test_适配器排除落在_where_之前() -> None:
    """④ 位置：SQLite 侧排除必须进 WHERE（⇒ 取 top-k 之前），而不是取完之后再筛。"""
    lines = _src(SQLITE_SEARCH).splitlines()
    # ⚠️ 变量名不用 `l`（ruff E741 歧义名，会让 lint 棘轮上涨 ⇒ 本仓棘轮只许降不许升）
    add_at = [i for i, line in enumerate(lines)
              if "exclude_categories" in line and "conditions.append" in line]
    notin_at = [i for i, line in enumerate(lines) if "category NOT IN (" in line]
    where_at = [i for i, line in enumerate(lines) if re.match(r"\s*where = ", line)]
    assert notin_at, "SQLite 侧没有 `category NOT IN (…)` 条件 ⇒ 排除没下推"
    assert where_at, "找不到 `where = ...` 的组装点"
    assert min(notin_at) < min(where_at), (
        "排除条件出现在 `where` 组装【之后】（行 %d vs %d）⇒ 它不会进 WHERE ⇒ "
        "会退化成「取完 top-k 再筛」（实际返回少于 k）" % (min(notin_at), min(where_at)))
    # ⭐ 参数化检查：`category NOT IN (%s)` 与 `"?" * len(...)` 可能跨行 ⇒ 取该行**及其后 2 行**一起看
    #   （首版只看单行 ⇒ 误报"未参数化"）
    window = "\n".join(lines[min(notin_at): min(notin_at) + 3])
    assert "?" in window, "排除条件必须参数化（占位符 ?）：%r" % window[:120]


def test_适配器签名有该参数且已接线() -> None:
    """④ 接线：适配器必须**有**该参数，且客户端**真的传**了（否则参数是惰性的）。"""
    tree = ast.parse(_src(SQLITE_SEARCH))
    sig_has = False
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "search_memories":
            sig_has = "exclude_categories" in [a.arg for a in n.args.args]
    assert sig_has, "SQLiteAdapter.search_memories 没有 exclude_categories 参数"
    cl = _src(CLIENT_SEARCH)
    n_sites = len(re.findall(r"exclude_categories=list\(_RETRIEVAL_EXCLUDE_CATEGORIES\)", cl))
    assert n_sites >= 5, "客户端只有 %d 处接线（预期 ≥5：3 条 search 分支 + 融合 + 全量拉取）" % n_sites


def test_PG_侧语义仍在() -> None:
    """① 的另一半（**源码级**：PG 侧不连库）：PG 适配器的等价语义必须仍然存在。"""
    src = _src(PG_SEARCH)
    assert "category != ALL(" in src, "PG 侧的排除语义被删掉了（两侧必须同语义）"
    assert re.search(r"if\s+exclude_categories:", src), "PG 侧的排除开关消失了"


def test_唯一来源非空且不误伤() -> None:
    """口径自身健全性：排除表非空、且不包含可检索类目（防止把整库排除掉）。"""
    cats = [str(c) for c in _RETRIEVAL_EXCLUDE_CATEGORIES]
    assert cats, "唯一来源为空 ⇒ 排除形同虚设"
    assert "kb_harvested" not in cats, "把 kb_harvested 也排除了 ⇒ 与可检索面约定冲突"
