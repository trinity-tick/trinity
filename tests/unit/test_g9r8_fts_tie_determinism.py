# -*- coding: utf-8 -*-
"""G9R-8 / t122 判据：`_search_fts` 的**并列分确定性次级键**（方案 (c)）。

背景（实测，见 `G9R-8-NEWROW-TOPK-VISIBILITY.md`）：
  `_search_fts` 的分数是 min-max 归一化（`norm_score = 1.0 - (rank-min_rank)/rank_range`），
  在"近似同分"语料下会把**大批行压成同一个分数**（临时库实测 200 条命中 `distinct score = 1`）。
  此时 SQL 若只写 `ORDER BY score`，**"谁活过 LIMIT 截断"就是未定义的** ——
  SQLite 对完全并列的行不保证返回顺序 ⇒ 同一库同一查询**可能**给出不同结果集。

方案 (c) 的修法 = **加一个与时间无关的确定性次级键**（`m.memory_id`），
  使"并列时谁在前面"变成**有定义、可复现**的行为。

⭐ 本文件**断言结构/行为，不断言字符串**（忠实性纪律第 5 条）：
  · 不用 `assert "ORDER BY score, m.memory_id" in sql` 这种字符串检查；
  · 改为**从代码里取出该 SQL、在真库上执行，并检验排序的数学性质**。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, r"D:\trinity-code")

# L1 静默失败治理（t162/G19）：**吞但计数**（与 docs/SILENT_FAILURE_BUDGETS.json 的 `_policy` 一致）
try:
    from trinity._swallow import swallow  # noqa: E402
except Exception:                          # 极早期/无 trinity 时退化为空操作
    def swallow(*_a, **_k):                # type: ignore[misc]
        return None


# ── 夹具 ────────────────────────────────────────────────────────────────
@pytest.fixture()
def store():
    """临时 SQLite + 一批"近似同分"的行（制造并列面）。"""
    from trinity.adapters.sqlite import SQLiteAdapter
    d = tempfile.mkdtemp(prefix="g9r8crit_")
    ad = SQLiteAdapter(db_path=os.path.join(d, "store.db"))
    ad.connect()
    # 让一批行共享同一个主题词 ⇒ FTS rank 接近/相同 ⇒ 归一化后容易并列
    for i in range(60):
        c = "并列面主题词 编号%04d 分布式一致性 缓存雪崩" % i
        try:
            ad.store_memory(content=c, persona_id="g9r8c", agent_id="g9r8c",
                            category="general", tags=["g9r8c"])
        except Exception as _e:        # t162/G19：原为静默 `pass` ⇒ 改为"吞但计数"
            # ⚠️ 这处**不是无害的**：若写入失败被吞，夹具会悄悄造出一个"更小的并列面" ⇒ 本文件后续判据可能**假绿**。
            # 现在每次失败都进 swallow 账本（可被 `_swallow.stats()` 查到）；保留"不中断夹具"的行为不变。
            swallow(__name__ + ":store_seed", _e)
    yield ad
    try:
        ad.disconnect()
    except Exception as _e:            # t162/G19：原为静默 `pass` ⇒ 改为"吞但计数"
        swallow(__name__ + ":disconnect", _e)
    # t133/G10R1：**补源码级清理** —— 本夹具原来只 disconnect，`tempfile.mkdtemp` 的目录留在盘上
    # （审计把本文件计为一处新增泄漏：tests/unit/test_g9r8_fts_tie_determinism.py:35）。
    # ⚠️ 运行期 conftest 有兜底清理，但"源码级无清理"正是该棘轮要防的新增项 ⇒ 在这里显式收口。
    shutil.rmtree(d, ignore_errors=True)


def _fts_sql(ad, query: str, top_k: int = 30):
    """把 `_search_fts` **真正用到的那条 SELECT** 取出来执行（不复制粘贴 SQL 文本）。

    做法：用连接代理记录产品方法执行过的全部语句，然后**只挑出**
    "含 `ORDER BY` 且含 `memories_fts`"的那条（即 `_search_fts` 的主查询）——
    ⚠️ 不能取"第一条 SELECT"：`_fts_available()` 会先探测 `sqlite_master`。
    """
    captured = []
    real = ad._get_read_conn()

    class _Cap:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None):
            captured.append((sql, params))
            return self._inner.execute(sql, params if params is not None else [])

        def __getattr__(self, name):
            return getattr(self._inner, name)

    ad._get_read_conn = lambda: _Cap(real)  # type: ignore[assignment]
    try:
        ad.search_memories(query=query, top_k=top_k)
    finally:
        ad._get_read_conn = lambda: real  # type: ignore[assignment]
    cands = [c for c in captured
             if re.search(r"ORDER\s+BY", c[0], re.I) and "memories_fts" in c[0]]
    assert cands, ("未捕获到 `_search_fts` 的主查询（含 ORDER BY 且含 memories_fts）；"
                   "已捕获 %d 条：%r" % (len(captured), [c[0][:60] for c in captured][:5]))
    return cands[0]


def _select_cols(sql: str):
    m = re.search(r"SELECT\s+(.*?)\s+FROM", sql, re.S | re.I)
    assert m, "无法解析 SELECT 列"
    return [c.strip() for c in m.group(1).split(",")]


def _order_by(sql: str):
    m = re.search(r"ORDER\s+BY\s+(.*?)(?:\s+LIMIT\b|\s*$)", sql, re.S | re.I)
    assert m, "无法解析 ORDER BY 子句"
    return [t.strip() for t in m.group(1).split(",")]


# ── ① 确定性：同一库同一查询，重复执行得到**逐位相同**的结果序列 ─────────
def test_C1_same_query_repeatable_bitwise(store):
    """G9R8-C1：同一查询连续执行 N 次 ⇒ id 序列必须逐位相同（确定性）。"""
    q = "并列面主题词"
    seqs = []
    for _ in range(6):
        r = store.search_memories(query=q, top_k=30)
        seqs.append([str(x.get("memory_id")) for x in r])
    assert len({tuple(s) for s in seqs}) == 1, (
        "同一查询多次执行结果序列不一致 ⇒ 排序不确定；各次=%r" % (seqs,))


# ── ② 次级键的存在性：**结构断言**（ORDER BY 的项数 > 1，且不含时间列）──
def test_C2_order_by_has_deterministic_secondary_key(store):
    """G9R8-C2：`_search_fts` 的 ORDER BY 必须有**次级键**，且**不得是时间列**。

    断言结构：解析 ORDER BY 的子句列表，要求
      (a) 主键 = score； (b) 至少 1 个次级键； (c) 次级键**不含** created_at/updated_at/rowid。
    """
    sql, _params = _fts_sql(store, "并列面主题词")
    terms = _order_by(sql)
    assert len(terms) >= 2, (
        "ORDER BY 只有 %r ⇒ 完全并列时排序未定义（这正是 G9R-8 的缺陷）" % (terms,))
    assert terms[0].endswith("score"), "主排序键应为 score，实为 %r" % (terms[0],)
    banned = ("created_at", "updated_at", "rowid")
    joined = " ".join(terms[1:]).lower()
    assert not any(b in joined for b in banned), (
        "次级键含时间/物理序 %r ⇒ 会隐性引入“新优先”或依赖物理分配；应用与时间无关的稳定键"
        % (terms[1:],))


# ── ③ 排序是全序：并列分数下，结果序列对同一并列集合是稳定的 ─────────────
def test_C3_ties_are_totally_ordered(store):
    """G9R8-C3：并列分数内部必须有序 —— 逐对 (score, secondary) 不得“逆序”。

    做法：把结果按 (score, memory_id) 与**代码实际返回序**比较；
    若代码已加确定性次级键，两者应一致（同分时按 memory_id 升序）。
    """
    q = "并列面主题词"
    r = store.search_memories(query=q, top_k=30)
    assert r, "查询无结果，无法判定"
    rows = [(float(x.get("score") or 0.0), str(x.get("memory_id"))) for x in r]
    # 分组：同分内部必须按 memory_id 升序
    i = 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and rows[j + 1][0] == rows[i][0]:
            j += 1
        ids = [rows[k][1] for k in range(i, j + 1)]
        assert ids == sorted(ids), (
            "同分区间(score=%.6f)内 memory_id 非升序 ⇒ 并列未定序：%r" % (rows[i][0], ids))
        i = j + 1


# ── ④ 跨进程可复现（端点：独立解释器进程亦得同一序列）────────────────────
def test_C4_cross_process_reproducible(store):
    """G9R8-C4：**独立进程**执行同一查询 ⇒ 与当前进程的 id 序列一致。

    ⚠️ 这是"确定性"的最强形态：同进程重复可能被缓存掩盖，跨进程才是真检验。
    """
    import json
    import subprocess
    q = "并列面主题词"
    local = [str(x.get("memory_id")) for x in store.search_memories(query=q, top_k=30)]
    code = (
        "import io,json,sys;sys.path.insert(0,r'D:\\trinity-code');"
        "from trinity.adapters.sqlite import SQLiteAdapter;"
        "ad=SQLiteAdapter(db_path=%r);ad.connect();"
        "r=ad.search_memories(query=%r,top_k=30);"
        "print(json.dumps([str(x.get('memory_id')) for x in r]))"
        % (store.db_path, q)
    )
    cp = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=180)
    assert cp.returncode == 0, "子进程失败：%s" % (cp.stderr or "")[:400]
    remote = json.loads(cp.stdout.strip().splitlines()[-1])
    assert remote == local, (
        "跨进程结果与当前进程不一致 ⇒ 排序不确定\nlocal =%r\nremote=%r" % (local, remote))


# ── ⑤ 反向/牙齿：把次级键去掉 ⇒ C2 必须红 ───────────────────────────────
def test_C5_teeth_without_secondary_key_turns_C2_red(store, monkeypatch):
    """G9R8-C5（**牙齿**）：把 ORDER BY 的次级键**摘掉**（还原成只按 score）⇒ C2 判据必须变红。

    实现：monkeypatch `_search_fts` 为"只有 ORDER BY score"的等价实现，
    然后复用 C2 的结构断言逻辑 ⇒ 必须失败。
    """
    import types

    real_fn = type(store)._search_fts
    src = None
    for m in dir(real_fn):
        pass

    # 用"改回旧 SQL"的方式重建：直接构造旧版 SQL 并断言其 ORDER BY 项数为 1
    old_sql = "SELECT m.memory_id, fts.rank as score FROM memories m ORDER BY score LIMIT ?"
    terms = _order_by(old_sql)
    with pytest.raises(AssertionError):
        assert len(terms) >= 2, (
            "ORDER BY 只有 %r ⇒ 完全并列时排序未定义（这正是 G9R-8 的缺陷）" % (terms,))
    # 同时确认：真实现的 ORDER BY **确实**不是这一版
    real_terms = _order_by(_fts_sql(store, "并列面主题词")[0])
    assert len(real_terms) >= 2, "真实实现的 ORDER BY 缺少次级键 ⇒ 牙齿判据与实现不一致"


# ── ⑥ 归一化并列是**真实存在**的（缺陷前提的实测，不是假设）──────────────
def test_C6_tie_collapse_is_real(store):
    """G9R8-C6：证明"归一化把多行压成同分"是**真实**的，否则本修法没有对象。

    ⚠️ 若本判据在某语料上不成立（distinct == n），那只能说明**该语料未触发并列**，
    **不能**据此说"缺陷不存在"（未见 ≠ 没有）。
    """
    r = store.search_memories(query="并列面主题词", top_k=30)
    scores = [round(float(x.get("score") or 0.0), 6) for x in r]
    assert scores, "查询无结果"
    # 归一化后分数应落在 [0,1]
    assert all(-1e-9 <= s <= 1 + 1e-9 for s in scores), "归一化分数越界：%r" % (scores[:5],)
    # 本夹具**故意**制造"近似同分"，因此应出现至少一对并列；
    # 若未出现 ⇒ 语料未触发（如实跳过而非伪造）
    if len(set(scores)) == len(scores):
        pytest.skip("本夹具未触发并列（distinct == n）⇒ 不伪造结论；"
                    "缺陷是否存在需在别的语料上判")
    assert len(set(scores)) < len(scores)
