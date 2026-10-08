#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R4 验收测试（§785.6 遗留②）：把 U1 的**读取方**归因做成可信读数。

## 问题（P1-0 就登记了，一直没做）

P1-0 把 U1-a 的**判据**冻在 `memories.agent_id like 'dsh-%'`（**归属**命名空间），
并在同一处写明它的语义边界：**归属不是读取方** —— 104 条里只有 12 条能在
`audit_log` 的检索命中里找到读取证据。当时给的"读取方归因窄读数 = 40"
**只覆盖 `action='search'`**，而**主导通路是 `search_hybrid`**：
S0 实测 24h 内 `search` 1226 行 vs `search_hybrid` 1654 行。

更要紧的是：`search` 行写了 `details.memory_ids`（**读到了哪些**），
而 `search_hybrid` 行只写了 `hits`（**命中几条**）—— **没有 id**。
⇒ "谁读了哪一条"在主导通路上**根本记不下来** ⇒ 读者归因无从谈起。

## R4 的两件事

1. **补记录**：`search_hybrid` 的两处审计写入补上 `memory_ids`（与既有 `search` 同形），
   于是"谁读了哪一条"在**两条通路**上都能算。
2. **补读数**：利用率审计新增 `U1a_reader_attributed_24h`（**报告项**，不动 P1-0 冻结的判据），
   并且**必须连覆盖率一起报**。

## 本文件的 S1 反向线（每条都能证伪"读数是真的"）

1. **覆盖率是判据的一部分**：没有 `memory_ids` 的历史行**永远**是 0 覆盖
   ⇒ 若只报"读取方读数 = 0"，会被读成"没人读"（**假低**）。
   故读数必须同时给出 `rows_with_ids / rows_total` 且在覆盖率为 0 时**显式说明本期不可用**；
2. **不得把覆盖率不足的数当完整数**：`usable` 标志必须为假；
3. `memory_ids` 有**截断**（既有 `search` 取前 10 条）⇒ 读数只能声称**下界**，必须写明；
4. 两个通路的行都要计入（`search` 与 `search_hybrid`），只算一个就是**系统性少算**；
5. 源码头里必须有补记录（否则读数永远停在 0 覆盖 —— "接了线但恒等于 no-op"）。

先跑本文件确认**红的**，再实现，再跑绿。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def _audit():
    import memory_utilization_audit as m  # noqa: E402
    return m


class FakeCur:
    """按 SQL 特征返回罐头值的最小游标替身（判据可复核：记录收到的 SQL）。

    ⚠️ 匹配是**按插入顺序取首个命中** ⇒ 更**具体**的谓词必须写在前面：
    `rows_with_ids` 的 SQL 里也含 `action in ('search','search_hybrid')`
    （本文件首版就因此把 2880 当成 0，是**用例标定错误**不是实现错误）。
    """

    def __init__(self, table):
        self.table = table
        self.sql = []
        self._last = None

    def execute(self, sql, args=None):
        s = str(sql)
        self.sql.append(s)
        self._last = None
        for pred, val in self.table.items():
            if pred in s:
                self._last = (val,)
                return
        self._last = (0,)

    def fetchone(self):
        return self._last

    def fetchall(self):
        return []


class TestReaderReading:
    def test_读数含覆盖率与可用标志(self):
        a = _audit()
        cur = FakeCur({
            "jsonb_array_elements_text": 0,
            "details ? 'memory_ids'": 0,
            "action in ('search','search_hybrid')": 1000,
        })
        out = a._u1_reader_attributed(cur)
        for k in ("value", "rows_total", "rows_with_ids", "coverage", "usable", "note"):
            assert k in out, "缺字段 %s：%r" % (k, out)

    def test_覆盖率0时不得当成没人读(self):
        """核心反向锁：0 覆盖 ⇒ 读数**不可用**，而不是「读取方读了 0 条」。"""
        a = _audit()
        cur = FakeCur({
            "jsonb_array_elements_text": 0,
            "details ? 'memory_ids'": 0,
            "action in ('search','search_hybrid')": 2880,
        })
        out = a._u1_reader_attributed(cur)
        assert out["coverage"] == 0, out
        assert out["usable"] is False, "0 覆盖却标成可用 ⇒ 会把「没记录」读成「没读」：" + str(out)
        assert "不可用" in out["note"] or "覆盖" in out["note"], out

    def test_有覆盖时可用且给出比例(self):
        a = _audit()
        cur = FakeCur({
            "jsonb_array_elements_text": 77,
            "details ? 'memory_ids'": 250,
            "action in ('search','search_hybrid')": 1000,
        })
        out = a._u1_reader_attributed(cur)
        assert out["value"] == 77, out
        assert out["rows_with_ids"] == 250 and out["rows_total"] == 1000, out
        assert abs(out["coverage"] - 0.25) < 1e-9, out
        assert out["usable"] is True, out

    def test_两个通路都要计入(self):
        """只算 `search` 就是系统性少算（主导通路是 search_hybrid）。"""
        a = _audit()
        cur = FakeCur({"jsonb_array_elements_text": 10,
                       "details ? 'memory_ids'": 100,
                       "action in ('search','search_hybrid')": 1000})
        a._u1_reader_attributed(cur)
        joined = " ".join(cur.sql)
        assert "search_hybrid" in joined and "'search'" in joined, joined

    def test_同时给出与归属口径同前缀的读取方读数(self):
        """两个数必须并列：一个问「谁读的」，一个问「记忆归谁」；
        只给一个就会把「归属」与「读取」混为一谈（P1-0 登记的正是这个边界）。"""
        a = _audit()
        cur = FakeCur({"jsonb_array_elements_text": 12,
                       "details ? 'memory_ids'": 250,
                       "action in ('search','search_hybrid')": 1000})
        out = a._u1_reader_attributed(cur)
        assert "reader_side_dsh" in out, out
        assert "归属" in out["note"] and "读取" in out["note"], out
        joined = " ".join(cur.sql)
        assert "agent_id like" in joined, "没有按读取方前缀过滤的查询：" + joined

    def test_截断必须写明是下界(self):
        a = _audit()
        cur = FakeCur({"jsonb_array_elements_text": 5,
                       "details ? 'memory_ids'": 10,
                       "action in ('search','search_hybrid')": 10})
        out = a._u1_reader_attributed(cur)
        assert "下界" in out["note"] or "截断" in out["note"], out


class TestNotInRatchet:
    """P1-0 冻结的判据**不许**被本项悄悄换掉。"""

    def test_读取方读数不得替换归属口径(self):
        """P1-0 冻结的判据**不许**被本项悄悄换掉（意图不变，锚点跟上 2026-10-02 的决定）。

        2026-10-06（测试归因轮 T1）：原判据断言 `U1a_reader_attributed_24h in REPORT_ONLY`。
        该断言的前提**已被明写取代**：2026-10-02（外部审计 ④b）
        `scripts/memory_utilization_audit.py::WINDOW_JUDGED` 的注释与 `REPORT_ONLY` 的
        注释都记了升级依据（前置条件"等覆盖率达到可判水平再议"已满足：实测近 24h
        覆盖率 100.0%（33/33）、近 30d 96.66%；`memory_ids` 自 2026-08-13 起持续记录）
        ⇒ 该键**由报告项升为窗口判据**，且源码明写「**并增、不替换**」P1-0 那个键。
        于是原断言在今天就**不可能**成立 —— 它当时锁的是"状态"，不是"不变量"。

        现锁真正的不变量（三条，任一条被违反都会红）：
          ① 读取方读数**不得**进定值棘轮 `DIRECTIONS`（仍不许被塞进 P1-0 的口径）；
          ② P1-0 的**归属**口径键 `U1a_agent_reads_24h` **仍被判**（在 `WINDOW_JUDGED` 里）
             ⇒ "升级"没有变成"替换"；
          ③ 该升级必须可追溯（源码里"并增"的注记仍在）。
        """
        a = _audit()
        assert "U1a_reader_attributed_24h" not in a.DIRECTIONS, (
            "读取方读数被塞进 ratchet ⇒ 等于把 P1-0 冻结的 U1-a 口径偷偷换掉")
        assert "U1a_reader_attributed_24h" in a.WINDOW_JUDGED, (
            "读取方读数既不在 REPORT_ONLY、也不在 WINDOW_JUDGED ⇒ 判据被整体移除（不是升级）")
        assert "U1a_agent_reads_24h" in a.WINDOW_JUDGED, (
            "P1-0 冻结的归属口径键被判据表移除 ⇒ 那才是「偷偷换掉尺子」")
        src = open(os.path.join(ROOT, "scripts", "memory_utilization_audit.py"),
                   encoding="utf-8").read()
        assert "并增、不替换" in src or "**并增**" in src, (
            "升级为判据这件事必须留下「并增不替换」的注记（可追溯性）")

    def test_归属口径的判据仍在(self):
        """§955 起 U1-a 改由同窗口判据看守（不进定值棘轮）；本项读数仍只能进报告。

        「不许被本项悄悄换掉」的可失败形式：本项读数不在 DIRECTIONS（上一条），
        而 U1-a 的看守**必须够硬** —— 两个方向 + 一个边界。
        """
        a = _audit()
        assert "U1a_agent_reads_24h" not in a.DIRECTIONS
        assert a._u1a_window_compare(50, 104)[0] is False, "U1-a 跌破一半却放行"
        assert a._u1a_window_compare(104, 104)[0] is True, "同窗口持平却判红"


class TestWritePathPatched:
    """补记录必须落在**源码**里（只加读数不加记录 = 读数永远 0 覆盖）。"""

    def _src(self):
        return open(os.path.join(ROOT, "trinity", "core", "client", "_hybrid_search.py"),
                    encoding="utf-8").read()

    def test_两处search_hybrid审计都写了memory_ids(self):
        s = self._src()
        blocks = [b for b in s.split('action="search_hybrid"')[1:]]
        assert len(blocks) >= 2, "search_hybrid 审计点少于 2 处：%d" % len(blocks)
        for i, b in enumerate(blocks[:2]):
            seg = b[:600]
            assert "memory_ids" in seg, (
                "第 %d 处 search_hybrid 审计没写 memory_ids ⇒ 主导通路仍无法归因" % (i + 1))

    def test_不改既有search通路(self):
        """既有 `search` 通路本来就写了 memory_ids，不得被本项动坏。"""
        s = open(os.path.join(ROOT, "trinity", "core", "client", "_search.py"),
                 encoding="utf-8").read()
        assert '"memory_ids": memory_ids[:10]' in s, "既有 search 通路的 memory_ids 被改动了"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
