# -*- coding: utf-8 -*-
"""重复增长的**机制**反事实测试：归档给去重"洗白"（t4 / 2026-10-06）。

## 机制

生产库的唯一索引是**部分索引**：

    CREATE UNIQUE INDEX idx_memories_content_hash
      ON memories(persona_id, agent_id, content_hash)
      WHERE content_hash IS NOT NULL AND status = 'active';

去重契约（`scripts/pg_content_hash_and_dedup.py:120-134` 的重复判定同样是
`WHERE status='active'`）只在 active 域内成立。于是：

    写入 A → 归档 A → 生产者重跑 → 再写 A（**不再撞唯一索引**）→ 重复副本 +1 → 循环

实测指纹：18,807 个重复组里 **5,787 组**是「≥1 active + ≥1 非 active」，
34,897 行重复落在**全归档**组里；单条 self-reflection 内容被写 **166 次**
（165 archived + 1 active，2026-08-30→10-05）。

## 为什么必须写成测试

这条机制是"写入侧要不要拦"的**唯一依据**。若有人把索引改成覆盖全状态的形状
（那正是本文件第二个用例），机制就消失 —— 那时判据必须重新标定，而不是继续沿用
"归档即洗白"的结论。两个方向都必须可核。
"""
import sqlite3

import pytest

DDL_COLUMNS = """
CREATE TABLE memories (
  memory_id TEXT PRIMARY KEY, persona_id TEXT DEFAULT 'default',
  agent_id TEXT DEFAULT 'default', content TEXT, content_hash TEXT,
  status TEXT DEFAULT 'active', created_at TEXT, updated_at TEXT
);
"""

#: 生产库的实际形状（部分索引，只见 active）
IDX_PROD_SHAPE = ("CREATE UNIQUE INDEX idx_memories_content_hash ON memories"
                  "(persona_id, agent_id, content_hash) "
                  "WHERE content_hash IS NOT NULL AND status = 'active'")

#: 仓库 DDL 声明的形状（`trinity/adapters/sqlite/_schema.py:274-276`，**无** status 限定）
IDX_REPO_DDL_SHAPE = ("CREATE UNIQUE INDEX idx_memories_content_hash ON memories"
                      "(persona_id, agent_id, content_hash) "
                      "WHERE content_hash IS NOT NULL")


def _db(index_sql: str):
    con = sqlite3.connect(":memory:")
    con.executescript(DDL_COLUMNS)
    con.execute(index_sql)
    con.commit()
    return con


def _ins(con, mid: str, status: str = "active", ch: str = "H", agent: str = "default"):
    con.execute(
        "INSERT INTO memories (memory_id,agent_id,content_hash,status,content,created_at) "
        "VALUES (?,?,?,?,?,?)", (mid, agent, ch, status, "同一段内容", "2026-08-30T18:00:00"))


def test_prod_index_blocks_second_active_insert():
    """【正例方向】active 内同 hash 第二次写入被唯一索引挡住（契约在 active 域内成立）。"""
    con = _db(IDX_PROD_SHAPE)
    _ins(con, "a")
    with pytest.raises(sqlite3.IntegrityError):
        _ins(con, "b")
    con.close()


def test_prod_index_allows_reinsert_after_archive_the_wash():
    """【机制核心】归档第一个副本后，同内容**可以再次写入** ⇒ 归档把去重洗白了。

    这就是 5,787 个"混合状态重复组"是怎么来的：不是有人故意写重复，
    而是**归档 → 重跑 → 再写**这条日常路径必然产生重复。
    """
    con = _db(IDX_PROD_SHAPE)
    _ins(con, "a")
    con.execute("UPDATE memories SET status='archived' WHERE memory_id='a'")
    _ins(con, "b")                      # ← 不抛异常：去重契约被洗白
    con.execute("UPDATE memories SET status='archived' WHERE memory_id='b'")
    _ins(con, "c")                      # ← 再来一次，仍然成功
    n = con.execute("select count(*) from memories where content_hash='H'").fetchone()[0]
    assert n == 3, "机制未复现 ⇒ 本测试的前提不成立，需重新标定根因"
    con.close()


def test_repo_ddl_shape_would_block_the_reinsert():
    """【反事实】把索引改成"覆盖全状态"（仓库 DDL 的形状）⇒ 重写被挡住，机制消失。

    这是治理杠杆的**可判读证据**：不改写入侧，改索引形状也能治本；
    但两种做法的影响面不同（改索引会让生产者的重复写入**抛错**，需要生产者侧处理
    冲突），故本轮只量测、不实施 —— 阈值/方案交队长决定。
    """
    con = _db(IDX_REPO_DDL_SHAPE)
    _ins(con, "a")
    con.execute("UPDATE memories SET status='archived' WHERE memory_id='a'")
    with pytest.raises(sqlite3.IntegrityError):
        _ins(con, "b")
    con.close()


def test_index_shape_difference_is_real_in_repo_source():
    """哨兵：仓库 DDL 里确实**没有** status 限定，而生产库的索引里有 —— 形状不同是事实。"""
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[2] / "trinity" / "adapters" / "sqlite" / "_schema.py"
    src = p.read_text(encoding="utf-8")
    i = src.index("idx_memories_content_hash")
    snippet = src[i:i + 220]
    assert "WHERE content_hash IS NOT NULL;" in snippet.replace("\n", " ").replace("  ", " ") \
        or "WHERE content_hash IS NOT NULL" in snippet
    assert "status = 'active'" not in snippet, (
        "仓库 DDL 若已带 status 限定，则【生产库索引更宽】的结论需要重测")


def test_guard_w1_targets_exactly_this_mechanism():
    """把机制与写入侧判据接上：W1 拦的正是"同 hash 只剩非 active 副本"这一次写入。"""
    from trinity.memory.corpus_write_guard import evaluate_before_store
    d = evaluate_before_store("同一段内容", existing_statuses=["archived"],
                              content_hash="H", producer="default", mode="on")
    assert d.allow is False and d.code == "dup_archived_copy"
    d2 = evaluate_before_store("同一段内容", existing_statuses=["active"],
                               content_hash="H", producer="default", mode="on")
    assert d2.allow is True, "active 副本在场时不该由 W1 拦（那是唯一索引的职责）"
