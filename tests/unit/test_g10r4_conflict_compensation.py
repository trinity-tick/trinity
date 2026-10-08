# -*- coding: utf-8 -*-
"""G10R4 / t136 判据：冲突判定的**补偿**（把 G7C 崩溃窗口"接线"）。

背景（G7C §4-B2/B3 实测）：`TRINITY_CONFLICT_ASYNC=on` 时队列只在内存
⇒ 进程在"写入已返回、判定未执行"的 ≈119ms 窗口内结束 ⇒ 判定静默消失。

⭐ **本补偿只做一件事**：把 `status='active' AND conflict_group_id IS NULL` 的行
**重跑 `_assign_conflicts`** ⇒ 把「静默丢失」变成「**重启后可发现、可修复**」。
⚠️ **它【不消除】窗口** —— 补偿发生在下一个进程之后；窗口内那次判定依然丢。

⭐ **断言结构/行为，不断言字符串**（忠实性纪律第 5 条）：
  · 判据①看**行的 `conflict_group_id` 是否真的从 NULL 变成 conf_…**（读 DB，不看日志）；
  · 判据②看**变更行数**（`changed` / 全表 `conflict_group_id` 计数**前后不变**）；
  · 判据③用**同族环境变量**关掉补偿 ⇒ ① 必须红。
⛔ 未加 skip / xfail；未调基线。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import shutil
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
def ad():
    """临时库 + 一条"可判为冲突"的基线行（制造出可命中的候选）。"""
    from trinity.adapters.sqlite import SQLiteAdapter
    import trinity.adapters.sqlite._crud as C
    d = tempfile.mkdtemp(prefix="g10r4crit_")
    a = SQLiteAdapter(db_path=os.path.join(d, "store.db"))
    try:
        a.connect()
        a.store_memory(content="基线行 端口是 5437 分布式一致性 缓存雪崩 索引维护 编号 A",
                       persona_id="g10r4", agent_id="g10r4", category="general",
                       tags=["g10r4"])
        yield a
    finally:
        # G48/t191：临时目录**保证清理**（同 t142 形态）。⚠️ 不能用 `with TemporaryDirectory`：
        # `d` 是**整个夹具**的库目录（测试全程在用）⇒ 用 `try/finally` 才覆盖"正常/断言失败/setup 抛错"三条路径。
        try:
            a.disconnect()
        except Exception as _e:        # t162/G19：原为静默 `pass` ⇒ 改为"吞但计数"
            swallow(__name__ + ":disconnect", _e)
        os.environ.pop("TRINITY_CONFLICT_COMPENSATE", None)
        shutil.rmtree(d, ignore_errors=True)


def _make_undecided(ad, tag: str) -> str:
    """造一条**"写入后未判定"**的行：把 `_assign_conflicts` 打成 no-op 再写。

    ⭐ 这精确复刻 G7C-B2 的语义：「写入已返回（行已落库、可检索），但判定没有执行」。
    """
    from trinity.adapters.sqlite import SQLiteAdapter
    orig = SQLiteAdapter._assign_conflicts
    SQLiteAdapter._assign_conflicts = lambda self, m, c: 0
    try:
        w = ad.store_memory(
            content="未判定行 %s 端口是 5437 分布式一致性 缓存雪崩 索引维护 编号 %s"
                    % (tag, tag),
            persona_id="g10r4", agent_id="g10r4", category="general", tags=["g10r4"])
    finally:
        SQLiteAdapter._assign_conflicts = orig
    return str(w.get("memory_id") or "")


def _group_of(ad, mid: str):
    row = ad._conn.execute("SELECT conflict_group_id FROM memories WHERE memory_id=?",
                           (mid,)).fetchone()
    return row[0] if row else None


def _null_count(ad) -> int:
    return int(ad._conn.execute(
        "SELECT count(*) FROM memories WHERE status='active' AND conflict_group_id IS NULL"
    ).fetchone()[0])


def _nonnull_rows(ad) -> int:
    return int(ad._conn.execute(
        "SELECT count(*) FROM memories WHERE conflict_group_id IS NOT NULL").fetchone()[0])


# ── ① 正向：未判定行 ⇒ 补偿跑完 ⇒ 它拿到 conf_… ────────────────────────
def test_G10R4_C1_compensation_fills_missing_group(ad):
    """G10R4-C1：造一条"写入后未判定"的行 ⇒ 补偿跑完 ⇒ **它拿到 `conf_…`**。"""
    mid = _make_undecided(ad, "C1")
    assert _group_of(ad, mid) is None, "前置条件失败：该行本应没有冲突组"
    assert _null_count(ad) >= 1

    res = ad.compensate_missing_conflicts()
    assert res["scanned"] >= 1, "补偿没有扫到该行：%r" % (res,)
    # ⭐ 核心断言：从 DB 直接读，不看日志
    assert _group_of(ad, mid) is not None, (
        "补偿后该行仍无冲突组 ⇒ 补偿无效：%r" % (res,))
    assert str(_group_of(ad, mid)).startswith("conf_"), (
        "组名不符合产品规则（应为 conf_ 前缀）：%r" % (_group_of(ad, mid),))
    assert res["changed"] >= 1, "补偿报告 changed=0，但该行确实变过：%r" % (res,)


# ── ② 反向：没有需要补偿的行 ⇒ 补偿不产生任何写入（0 变更）──────────────
def test_G10R4_C2_no_targets_produces_zero_changes(ad):
    """G10R4-C2（**反向**）：**没有需要补偿的行** ⇒ 补偿**不产生任何写入**。

    造法：再写一条**走正常路径**的行（它自己会被判定），然后把剩下的 NULL 行
    先补一遍 ⇒ 此时再跑一次补偿 ⇒ 应得 `scanned/changed == 0`，
    且**全表 `conflict_group_id IS NOT NULL` 的行数前后相同**。
    """
    mid = _make_undecided(ad, "C2")
    # 先补掉（含 C1 之外所有 NULL）⇒ 之后应当没有目标
    first = ad.compensate_missing_conflicts()
    assert first["scanned"] >= 1
    assert _null_count(ad) == 0, "补偿后仍有 NULL 行：%d" % _null_count(ad)

    rows_before = _nonnull_rows(ad)
    second = ad.compensate_missing_conflicts()
    rows_after = _nonnull_rows(ad)
    assert second["scanned"] == 0, "无目标时却扫到了行：%r" % (second,)
    assert second["changed"] == 0, "无目标时却发生了变更：%r" % (second,)
    assert second["errors"] == 0, "无目标时却有错误：%r" % (second,)
    assert rows_after == rows_before, (
        "补偿在无目标时仍改变了全表组计数：%d -> %d" % (rows_before, rows_after))
    # ⭐ 并且幂等：那条曾缺失的行仍指向同一个组
    assert _group_of(ad, mid) is not None


# ── ③ 牙齿：关掉补偿（同族环境变量）⇒ ①必须红 ──────────────────────────
def test_G10R4_C3_teeth_disabled_switch_turns_C1_red(ad, monkeypatch):
    """G10R4-C3（**牙齿**）：把补偿**关掉**（`TRINITY_CONFLICT_COMPENSATE=off`，
    与 `TRINITY_CONFLICT_ASYNC` 同族）⇒ **启动补偿入口返回 None**，
    且"不直接调 `compensate_missing_conflicts` 就没有任何补偿发生" ⇒ **①必须红**。

    ⚠️ 这里**不直接调用** `compensate_missing_conflicts`（那是显式入口）；
    只用**开关控制的那条自动入口** `compensate_missing_conflicts_at_startup()`。
    """
    import trinity.adapters.sqlite._crud as C
    mid = _make_undecided(ad, "C3")

    # 关掉 ⇒ 自动入口必须什么都不做
    monkeypatch.setenv("TRINITY_CONFLICT_COMPENSATE", "off")
    assert C._conflict_compensate_enabled() is False
    assert ad.compensate_missing_conflicts_at_startup() is None, (
        "开关为 off 时启动补偿仍在跑 ⇒ 牙齿失效")
    # ⭐ ① 必须红：该行仍然没有组
    assert _group_of(ad, mid) is None, "开关 off 却已经把组补上了 ⇒ 牙齿失效"

    # 打开 ⇒ 自动入口应当跑一批并把它补上（证明"红"是开关造成的，不是别的原因）
    monkeypatch.setenv("TRINITY_CONFLICT_COMPENSATE", "on")
    assert C._conflict_compensate_enabled() is True
    res = ad.compensate_missing_conflicts_at_startup()
    assert isinstance(res, dict) and res.get("scanned", 0) >= 1, (
        "开关 on 时启动补偿没有扫到目标：%r" % (res,))
    assert _group_of(ad, mid) is not None, "开关 on 后仍未补上 ⇒ 补偿不承重"


# ── ④ 限流：绝不一次全表扫描（batch 语义）───────────────────────────────
def test_G10R4_C4_batch_limit_bounds_the_scan(ad, monkeypatch):
    """G10R4-C4（**限流**）：`batch` 必须**限制单次扫描行数**，且剩余量**可读且真实**。

    ⚠️ 本条在实现后**我自己改过一次断言**，理由如实登记（**不是**"改口径救结果"）：
    初版我断言 `remaining_after_cursor > 0`（"batch=2 时不该把剩余扫完"）。
    实测 `scanned=2` 但全局 NULL **归零** ⇒ 我起初以为是 bug，查清后发现是**两个事实**：
      ① ⭐ `_assign_conflicts` 会把**命中的邻居行一并打组** ⇒ 扫 2 行**可能顺带解决全部**；
      ② ⭐ 我原实现的 `remaining_after_cursor` 用 `rowid > last_rowid` 统计
         ⇒ 在这种"顺带解决"下会**报成 0（假·已完成）** ⇒ **那是我实现里的真缺陷，已修**。
    ⇒ **修正后的断言**：限流只约束**本批扫了几行**（`scanned <= limit`），
      **不承诺**"还剩多少行"（那由 `remaining_total` 全局读出，可能为 0）。
    """
    import trinity.adapters.sqlite._crud as C
    for i in range(5):
        _make_undecided(ad, "C4-%d" % i)
    n_null_before = _null_count(ad)
    assert n_null_before >= 5, "前置条件失败：未判定行不足（%d）" % n_null_before

    res = ad.compensate_missing_conflicts(limit=2)
    # ⭐ 限流只约束"本批扫了多少行"
    assert res["scanned"] <= 2, "batch=2 却扫了 %d 行 ⇒ 限流失效" % res["scanned"]
    assert res["batch_limit"] == 2
    # ⭐ 剩余量必须是**全局真实读数**（且与直接查库一致）—— 这是修掉"假 0"后的判据
    assert res["remaining_total"] == _null_count(ad), (
        "remaining_total 与库里的真实 NULL 数不一致：%r vs %d"
        % (res["remaining_total"], _null_count(ad)))
    assert res["budget_exhausted"] == int(res["remaining_total"] > 0)
    # 默认 batch 来自环境变量（同族开关）
    monkeypatch.setenv("TRINITY_CONFLICT_COMPENSATE_BATCH", "7")
    assert C._conflict_compensate_batch() == 7
    # 配置写坏 ⇒ 退回默认限流（更安全），不是不限流
    monkeypatch.setenv("TRINITY_CONFLICT_COMPENSATE_BATCH", "not-a-number")
    assert C._conflict_compensate_batch() == 50, "配置写坏时必须退回默认限流（50）"
    # ⭐ 默认值本身 = 50（实测选的：50 行 ≈ 1.85s 可接受；200 行 ≈ 7.5s 太慢）
    monkeypatch.delenv("TRINITY_CONFLICT_COMPENSATE_BATCH", raising=False)
    assert C._conflict_compensate_batch() == 50, "默认限流应为 50（实测值）"


# ── ⑤ 默认值与统计可读（不恒真）────────────────────────────────────────
def test_G10R4_C5_default_is_off_and_stats_readable(ad):
    """G10R4-C5：⭐ **默认 `off`**（沿用 G7C 裁定二的理由）+ 统计**可读且不恒真**。"""
    import trinity.adapters.sqlite._crud as C
    os.environ.pop("TRINITY_CONFLICT_COMPENSATE", None)
    assert C._CONFLICT_COMPENSATE_DEFAULT == "off", "补偿默认值必须为 off"
    assert C._conflict_compensate_enabled() is False, "不设变量时应为 off"

    # 统计：先跑一次拿到非零读数，再确认字段齐全
    _make_undecided(ad, "C5")
    res = ad.compensate_missing_conflicts()
    st = C.conflict_compensate_stats()
    for k in ("enabled", "batch", "runs", "scanned", "changed", "no_change",
              "errors", "last_run_ts", "last_run_ms", "budget_exhausted"):
        assert k in st, "统计缺字段 %s" % k
    assert st["runs"] >= 1 and st["scanned"] >= 1
    # ⭐ 不恒真：`changed` 不必然等于 `scanned`（"本来就没冲突"的行不会产生变更）
    assert res["changed"] + res["no_change"] == res["scanned"], (
        "changed + no_change 应等于 scanned：%r" % (res,))
