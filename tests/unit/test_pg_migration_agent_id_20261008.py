# -*- coding: utf-8 -*-
"""G66/t209 判据：**SQLite→PG 迁移的 INSERT 必须写 `agent_id`**（**结构判据** + 旧版本必红的牙齿）。

缺陷（t205/t206 定名 + 量化）：
  `trinity/adapters/postgresql.py` 的**迁移** INSERT（原 `:1704-1711`）**14 列、省略 `agent_id`**
  ⇒ 迁移过来的行 `agent_id IS NULL`（实测 **11,808 行**、**仍在增长**），而 **PG 主写入路径**
  （同文件 `:578-590` / `:828-838`，**26 列且含 `agent_id`**）会写 ⇒ **两路不一致**。

为什么不写【行为】判据（**如实说明**）：
  行为判据要**真的触发迁移路径**（`pg_cur.execute` 循环 + 真 PG 连接 + 写库）——
  ⛔ 本任务**明令不得触发迁移路径、不得写生产库** ⇒ ⭐ 故只做**结构判据**：
  它**最便宜**、**不随数据变化 flaky**，且**恰好覆盖这类"丢列"缺陷**的形态。

⚠️ **本判据不做的事**：它**不改数据**、**不判断历史 11,808 行**（那是另立的回填决定）。

牙齿（**两条**）：
  ① **合成旧版本**：把 `agent_id` 从迁移列清单里删掉 ⇒ 必须红；
  ② ⭐ **真旧版本**：直接用 `git show HEAD:trinity/adapters/postgresql.py`（**改前的真实字节**）⇒ 必须红
     （比合成更硬：它证明"这条判据在改动之前就会发火"）。
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ADAPTER = REPO / "trinity" / "adapters" / "postgresql.py"
REL = "trinity/adapters/postgresql.py"


def _migration_insert_segment(src: str) -> str:
    """取**迁移用**的那条 `INSERT INTO memories`（锚点：紧跟 `SELECT * FROM memories ORDER BY ...`）。"""
    m = re.search(r"SELECT \* FROM memories ORDER BY created_at", src)
    assert m, "找不到迁移路径的锚点（`SELECT * FROM memories ORDER BY created_at`）⇒ 判据前提失效"
    rest = src[m.end():]
    nxt = re.search(r"INSERT\s+INTO\s+memories", rest, re.I)
    assert nxt, "迁移锚点之后找不到 `INSERT INTO memories` ⇒ 判据前提失效"
    return rest[nxt.start(): nxt.start() + 1200]


def _violations(src: str) -> list:
    """结构性违规（空 = 合规）。抽成函数 ⇒ 牙齿能喂"改坏"的源码，也能喂**真旧版本**。"""
    bad = []
    seg = _migration_insert_segment(src)
    cols = re.search(r"INSERT\s+INTO\s+memories\s*\((.*?)\)\s*VALUES", seg, re.S | re.I)
    if not cols:
        bad.append("迁移 INSERT 的列清单/VALUES 结构解析失败")
        return bad
    col_list = [c.strip() for c in cols.group(1).split(",") if c.strip()]
    if "agent_id" not in col_list:
        bad.append("⭐ 迁移 INSERT 的列清单**不含 `agent_id`** ⇒ 迁移过来的行会 `agent_id IS NULL`"
                   "（本判据要防的正是这个）")
    if "agent_id" in col_list and "tenant_id" in col_list:
        if col_list.index("agent_id") != col_list.index("tenant_id") + 1:
            bad.append("`agent_id` 未紧跟 `tenant_id`（须与元组顺序一致）：%r" % col_list[:6])
    n_ph = len(re.findall(r"%s", seg))          #: `%s::timestamptz` 也算一个 %s
    if n_ph != len(col_list):
        bad.append("VALUES 占位符数(%d) != 列数(%d) ⇒ 列/值错位风险" % (n_ph, len(col_list)))
    #: ⚠️ 取值在**元组的更下面**（注释 5 行 + 若干取值行）⇒ **不能用固定窗口**搜（本轮实测：
    #: 1200 字符窗口会把它切掉，从而产生"假红"）。⇒ 从迁移 INSERT 起**一直到文件尾**搜（足够且不会误配）。
    tail = src[src.index(seg[:40]):]
    if "agent_id" in col_list and not re.search(
            r'row_dict\.get\(\s*["\']agent_id["\']\s*\)\s*or\s*["\']default["\']', tail):
        bad.append('缺少取值 `row_dict.get("agent_id") or "default"`'
                   "（NULL 是异常态，不作为缺省）")
    return bad


def _head_src() -> str:
    r = subprocess.run(["git", "show", "HEAD:%s" % REL], cwd=str(REPO), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, "取不到 HEAD 版源码：%s" % r.stderr
    return r.stdout.lstrip("\ufeff")


# ── 正向 ────────────────────────────────────────────────────────────────
def test_adapter_parses_and_members_intact():
    """四件套：文件能 parse；判据依赖的三处仍在（迁移锚点 + INSERT + ON CONFLICT 子句）。"""
    src = ADAPTER.read_text(encoding="utf-8")
    ast.parse(src)
    for probe in ("SELECT * FROM memories ORDER BY created_at ASC", "INSERT INTO memories",
                  "ON CONFLICT (memory_id) DO NOTHING"):
        assert probe in src, "原有语句被改动：%r" % probe


def test_migration_insert_writes_agent_id():
    """⭐ 核心：迁移 INSERT 必须写 `agent_id`（列/占位符/取值三者一致）。"""
    v = _violations(ADAPTER.read_text(encoding="utf-8"))
    assert v == [], "结构性违规：%r" % v


def test_reverse_main_write_paths_still_marked():
    """反向：判据只针对**迁移**那条 INSERT ⇒ 主写入路径仍须含 `agent_id`（没被误改）。"""
    src = ADAPTER.read_text(encoding="utf-8")
    assert len(re.findall(r"agent_id", src)) >= 3, "`agent_id` 出现次数异常偏少 ⇒ 可能误删了主路径列"


def test_teeth_synthetic_old_version_must_be_red():
    """牙齿①：把 `agent_id` 从迁移列清单删掉（合成旧版本）⇒ 必须**报出违规**。"""
    src = ADAPTER.read_text(encoding="utf-8")
    broken = re.sub(r"tenant_id, agent_id,\s*\n\s*content, role", "tenant_id, content, role",
                    src, count=1)
    assert broken != src, "未能在源码里构造出「旧版本」（删掉 agent_id 列）⇒ 判据前提失效"
    v = _violations(broken)
    assert v, "旧版本应红，实测无违规 ⇒ 判据没有分辨力"


def test_teeth_real_head_version_must_be_red():
    """⭐ 牙齿②（最硬）：**改前的真实字节**（`git show HEAD:`）必须被判据判红。"""
    v = _violations(_head_src())
    assert v, ("HEAD（改前）版本竟然合规 ⇒ 要么改动没落地、要么判据看不见该缺陷：%r" % v)
    assert any("agent_id" in x for x in v), "违规原因必须与 `agent_id` 相关：%r" % v


def test_teeth_value_caliber_must_be_enforced():
    """牙齿③：列加了但**取值口径**不对（缺 `or "default"`）⇒ 也必须红。"""
    src = ADAPTER.read_text(encoding="utf-8")
    broken = src.replace('row_dict.get("agent_id") or "default"', 'row_dict.get("agent_id")', 1)
    assert broken != src, "未能构造出「取值缺 or default」的版本"
    assert _violations(broken), "取值缺 `or \"default\"` 时应红"
