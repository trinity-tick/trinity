# -*- coding: utf-8 -*-
"""t61/I1 判据：冲突检测的 **token 集缓存**（`TRINITY_CONFLICT_TOKEN_CACHE`，**默认 off**）。

纪律：本优化**只动"与表规模无关的固定成本"那一块**（jieba 分词），
`_token_set` 是**纯函数** ⇒ 缓存**不可能改变**任何重叠判定 ⇒ 判据①以"同输入 ⇒ 同冲突判定"为证。
⚠️ 随表规模**线性**增长的那一块（召回里的按词 LIKE 全表扫描）**本轮未动** —— 改它会**改变候选集**
（= 改语义）⇒ 按硬约束上报队长，见 `WRITE-PATH-CONFLICT-COST.md` §5。
"""
from __future__ import annotations

import os
import time

import pytest

A = "服务端端口是 5432，数据库连接池上限 200，缓存雪崩处置方案与向量索引"
B = "服务端端口是 5430，数据库连接池上限 200，缓存雪崩处置方案与向量索引"
OTHER = "今天午饭吃了番茄炒蛋，顺便记录一下天气不错"


def _adapter(tmp_path, name):
    from trinity.adapters.sqlite import SQLiteAdapter

    ad = SQLiteAdapter(db_path=str(tmp_path / name))
    ad.connect()
    return ad


def _conflict_state(ad, ids):
    rows = ad._conn.execute(
        "SELECT memory_id, conflict_group_id FROM memories WHERE memory_id IN (%s)"
        % ",".join("?" * len(ids)), tuple(ids)).fetchall()
    return {r[0]: r[1] for r in rows}


def _write_two(ad):
    a = ad.store_memory(content=A, agent_id="t61", persona_id="t61", category="general")
    b = ad.store_memory(content=B, agent_id="t61", persona_id="t61", category="general")
    return [a["memory_id"], b["memory_id"]]


# ── ① 行为等价：缓存开/关 ⇒ **同冲突判定** ──────────────────────────────────
def test_缓存开关不改冲突判定_t61(tmp_path, monkeypatch):
    from trinity.adapters.sqlite import _crud as C

    monkeypatch.setenv("TRINITY_CONFLICT_DETECT", "on")
    # off
    monkeypatch.setenv("TRINITY_CONFLICT_TOKEN_CACHE", "off")
    C._TOKEN_CACHE.clear()
    ad1 = _adapter(tmp_path, "off.db")
    ids1 = _write_two(ad1)
    st1 = _conflict_state(ad1, ids1)
    # on
    monkeypatch.setenv("TRINITY_CONFLICT_TOKEN_CACHE", "on")
    C._TOKEN_CACHE.clear()
    ad2 = _adapter(tmp_path, "on.db")
    ids2 = _write_two(ad2)
    st2 = _conflict_state(ad2, ids2)

    g1 = set(st1.values())
    g2 = set(st2.values())
    assert None not in g1 and len(g1) == 1, "前提失效：off 档下这两条应当形成一个冲突组：%r" % st1
    assert (None not in g2 and len(g2) == 1), "缓存 on 后冲突判定变了（应仍形成一个冲突组）：%r" % st2
    # 无关内容不该被拉进冲突组（⚠️ 按 memory_id 查 —— 库内 content 是 `enc:v1:` 密文，不能按内容 LIKE）
    o = ad2.store_memory(content=OTHER, agent_id="t61", persona_id="t61", category="general")
    groups = [r[0] for r in ad2._conn.execute(
        "SELECT conflict_group_id FROM memories WHERE memory_id = ?",
        (o["memory_id"],)).fetchall()]
    assert groups and groups[0] is None, "无关内容被误并入冲突组：%r" % groups
    # 逐文本 token 集合完全一致（这是"等价"的**机制**证据）
    monkeypatch.setenv("TRINITY_CONFLICT_TOKEN_CACHE", "off")
    C._TOKEN_CACHE.clear()
    off_sets = [ad1._token_set(t) for t in (A, B, OTHER)]
    monkeypatch.setenv("TRINITY_CONFLICT_TOKEN_CACHE", "on")
    C._TOKEN_CACHE.clear()
    on_sets = [ad2._token_set(t) for t in (A, B, OTHER)]
    assert off_sets == on_sets, "同一文本在缓存开/关下 token 集合不同 ⇒ 不纯"


# ── ② 开关 off ⇒ 走原路径（缓存**完全不被查询**）────────────────────────────
def test_开关off时缓存不被查询_t61(tmp_path, monkeypatch):
    from trinity.adapters.sqlite import _crud as C

    monkeypatch.setenv("TRINITY_CONFLICT_TOKEN_CACHE", "off")
    monkeypatch.setenv("TRINITY_CONFLICT_DETECT", "on")
    C._TOKEN_CACHE.clear()
    C._TOKEN_CACHE_STATS.update(hits=0, misses=0, evictions=0)
    ad = _adapter(tmp_path, "off2.db")
    _write_two(ad)
    assert C._TOKEN_CACHE_STATS["hits"] == 0, "off 档却命中缓存 ⇒ 没走原路径"
    assert len(C._TOKEN_CACHE) == 0, "off 档不应写入缓存"
    # 打开后同样输入必须命中（证明缓存真的在工作）
    monkeypatch.setenv("TRINITY_CONFLICT_TOKEN_CACHE", "on")
    C._TOKEN_CACHE.clear()
    C._TOKEN_CACHE_STATS.update(hits=0, misses=0, evictions=0)
    ad._token_set(A)
    ad._token_set(A)
    assert C._TOKEN_CACHE_STATS["hits"] >= 1, "on 档应命中缓存：%r" % C._TOKEN_CACHE_STATS


# ── ③ 性能：同口径（仅 `_token_set`）p50 改善，且确定可复现 ─────────────────
def test_缓存带来同口径性能改善_t61(monkeypatch):
    from trinity.adapters.sqlite import _crud as C
    from trinity.adapters.sqlite._crud import _CrudMixin

    texts = ["讨论分布式一致性与缓存雪崩的处置方案 %02d，端口 5432，含向量索引与记忆分层" % i
             for i in range(10)]

    def bench(rounds=40):
        ts = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            for t in texts:
                _CrudMixin._token_set(t)
            ts.append((time.perf_counter() - t0) * 1000)
        ts.sort()
        return ts[len(ts) // 2]

    monkeypatch.setenv("TRINITY_CONFLICT_TOKEN_CACHE", "off")
    C._TOKEN_CACHE.clear()
    off = bench()
    monkeypatch.setenv("TRINITY_CONFLICT_TOKEN_CACHE", "on")
    C._TOKEN_CACHE.clear()
    on = bench()
    ratio = off / on if on else 0.0
    assert ratio >= 5.0, (
        "同口径（10 文本 × 40 轮 `_token_set`）p50 改善应 ≥5x：off=%.3fms on=%.3fms (%.2fx)"
        % (off, on, ratio))


# ── ④ 牙齿：把缓存里的 token 集改坏 ⇒ 判据①必须红 ───────────────────────────
def test_牙齿_缓存内容改坏必须判红_t61(tmp_path, monkeypatch):
    from trinity.adapters.sqlite import _crud as C

    monkeypatch.setenv("TRINITY_CONFLICT_DETECT", "on")
    monkeypatch.setenv("TRINITY_CONFLICT_TOKEN_CACHE", "on")
    C._TOKEN_CACHE.clear()
    ad = _adapter(tmp_path, "tooth.db")
    # **人为改坏**：让 A 的 token 集变成一个与 B 完全不相交的集合（模拟"剪枝剪错"）
    _orig = ad._token_set(A)
    C._TOKEN_CACHE[str(A)] = frozenset({"毫不相干"})
    try:
        ids = _write_two(ad)
        st = _conflict_state(ad, ids)
        # 判据①的前提（必须形成冲突组）在坏缓存下**不再成立** ⇒ 判据①本应失败
        with pytest.raises(AssertionError):
            assert None not in set(st.values()) and len(set(st.values())) == 1, (
                "token 集被改坏 ⇒ 冲突判定应当变了（本应让判据①红）：%r" % st)
    finally:
        C._TOKEN_CACHE.clear()
        C._TOKEN_CACHE[str(A)] = _orig if isinstance(_orig, frozenset) else frozenset(_orig)
