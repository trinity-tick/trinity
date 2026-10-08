# -*- coding: utf-8 -*-
"""SQLite 复习调度（Step 4）判据测试。

覆盖的**真实失败形态**（每条都对着一个具体风险，不是对着「函数有返回值」）：

1. **§13.0**：不参与检索的类目不得进排期，且剔除必须**显式报数**；
2. **§16.1**：写库前必须备份（活系统上来就改是已发生过的事故形态）；
3. **不许覆盖既有值**：只补空位 —— 否则会把 186 条真实排期冲掉；
4. **默认不许动 `access_count`**：那是冷率读数所用的列，自动复习 +1 会把冷率做假
   （有意与 `brain_cycle.step_fsrs` 不同，必须钉住，否则将来有人「对齐」时静默丢掉）；
5. **§13.2**：库不可读 / 缺列 ⇒ INCONCLUSIVE 且带原因，不把「取不到」读成「没有」；
6. 预算与 importance DESC 顺序真实生效。
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    path = os.path.join(ROOT, "scripts", "review_schedule_sqlite.py")
    spec = importlib.util.spec_from_file_location("review_schedule_sqlite", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["review_schedule_sqlite"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()
TIERS = [{"min_imp": 0.8, "days": 30}, {"min_imp": 0.6, "days": 60}, {"min_imp": 0.0, "days": 90}]


# ---- 纯函数 ------------------------------------------------------------------

@pytest.mark.parametrize("imp,days", [
    (0.9, 30), (0.8, 30), (0.79, 60), (0.6, 60), (0.59, 90), (0.0, 90),
    (None, 90), ("bad", 90),
])
def test_interval_days_matches_policy_tiers(imp, days):
    assert mod.interval_days(imp, TIERS) == days


def test_floor_uses_default_days_not_last_tier():
    """兜底必须用策略的 `default_days`(=90)，**不是** tiers[-1].days(=60)。

    反事实：真实策略只有 0.8/0.6 两档（没有 0.0 档），若回落到 tiers[-1]，
    importance<0.6 的 21,570 行会被排成 60 天而非 90 天 —— 静默不一致。
    """
    two_tier = [{"min_imp": 0.8, "days": 30}, {"min_imp": 0.6, "days": 60}]
    assert mod.interval_days(0.1, two_tier, default_days=90) == 90, (
        "回落到 tiers[-1] 会得到 60 ⇒ 与 forgetting_policy/brain_cycle 不一致")
    assert mod.interval_days(0.1, two_tier, default_days=77) == 77


def test_agrees_with_canonical_forgetting_policy_fsrs_days():
    """防漂移：本脚本的间隔必须与**策略文件自己的实现**逐点一致。

    与 §13.0 的「清单只从引擎常量取 + 断言同源」同一手法：
    将来有人改 tiers/default_days 而只改了其中一处，本测试当场红。
    """
    scripts = os.path.join(ROOT, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import forgetting_policy  # noqa: E402

    tiers = sorted(forgetting_policy.load_policy().get("fsrs", {}).get("tiers", []),
                   key=lambda t: t["min_imp"], reverse=True)
    default_days = int(forgetting_policy.load_policy()
                       .get("fsrs", {}).get("default_days", 90))
    for imp in [0.0, 0.1, 0.3, 0.59, 0.6, 0.7, 0.79, 0.8, 0.9, 1.0]:
        assert mod.interval_days(imp, tiers, default_days) == \
            forgetting_policy.fsrs_days(imp), "与 canonical fsrs_days 漂移：imp=%s" % imp


def test_interval_days_uses_longest_tier_when_importance_missing():
    """取不到 importance 不能变成 0 天（那会让所有无 importance 的行天天到期）。"""
    assert mod.interval_days(None, TIERS) == 90


@pytest.mark.parametrize("cur,expect", [(30, 45), (60, 90), (120, 180), (200, 180), (0, 1), (None, 1)])
def test_advance_days_matches_brain_cycle_formula(cur, expect):
    """与 brain_cycle 的 `LEAST(interval*1.5, 180)` 同式。"""
    assert mod.advance_days(cur, cap=180, factor=1.5) == expect


# ---- 夹具 -------------------------------------------------------------------

def _mk(tmp_path, rows, columns=None):
    """rows = (memory_id, agent_id, category, importance, interval, next_at, access)"""
    p = os.path.join(str(tmp_path), "store.db")
    con = sqlite3.connect(p)
    cols = columns or ["memory_id", "agent_id", "category", "status", "importance",
                       "review_interval_days", "next_review_at", "access_count"]
    con.execute("CREATE TABLE memories (%s)" % ",".join("%s TEXT" % c for c in cols))
    want = [("memory_id", 0), ("agent_id", 1), ("category", 2), ("importance", 3),
            ("review_interval_days", 4), ("next_review_at", 5), ("access_count", 6)]
    use = [(n, i) for n, i in want if n in cols]
    if "status" in cols:
        use.append(("status", None))
    sql = "INSERT INTO memories (%s) VALUES (%s)" % (
        ",".join(n for n, _ in use), ",".join("?" for _ in use))
    for r in rows:
        vals = [("active" if i is None else r[i]) for _, i in use]
        con.execute(sql, tuple(vals))
    con.commit()
    con.close()
    return p


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """钉住策略与排除清单，并把备份目录改到 tmp（不污染 ~/.trinity）。"""
    monkeypatch.setattr(mod, "load_tiers", lambda: (TIERS, "test"))
    monkeypatch.setattr(mod, "load_engine_exclusions", lambda: (["perception"], "test"))
    monkeypatch.setattr(mod, "load_default_days", lambda: 90)
    monkeypatch.setattr(mod, "load_budget", lambda: 500)
    monkeypatch.setattr(mod, "BACKUP_ROOT", os.path.join(str(tmp_path), "backups"))


# ---- §13.0：排除类目不进排期且显式报数 ---------------------------------------

def test_excluded_category_is_not_scheduled_but_is_reported(tmp_path):
    p = _mk(tmp_path, [
        ("m1", "dsh", "knowledge", 0.9, 0, None, 0),
        ("m2", "dsh", "perception", 0.9, 0, None, 0),
        ("m3", "dsh", "perception", 0.9, 0, None, 0),
    ])
    r = mod.plan(p)
    assert r["verdict"] == "OK", r
    assert r["schedulable_rows"] == 1
    assert r["retrieval_excluded_rows"] == 2, "剔除必须显式报数（§13.0 附带纪律）"

    mod.apply_init(p)
    con = sqlite3.connect(p)
    got = dict(con.execute("SELECT memory_id, next_review_at FROM memories"))
    con.close()
    assert got["m1"] is not None, "可检索行必须被排期"
    assert got["m2"] is None and got["m3"] is None, "排除类目不得进排期"


# ---- 只补空位：不许覆盖既有排期 -----------------------------------------------

def test_apply_init_never_overwrites_existing_schedule(tmp_path):
    keep = "2026-08-06T17:41:58"
    p = _mk(tmp_path, [
        ("m1", "dsh", "knowledge", 0.9, "30", keep, 3),        # 既有排期，必须原样
        ("m2", "dsh", "knowledge", 0.9, 0, None, 0),           # 空位，应被补
    ])
    r = mod.apply_init(p)
    assert r["verdict"] == "OK", r
    con = sqlite3.connect(p)
    assert con.execute("SELECT next_review_at FROM memories WHERE memory_id='m1'"
                       ).fetchone()[0] == keep, "既有排期被覆盖 ⇒ 会冲掉真实数据"
    assert con.execute("SELECT next_review_at FROM memories WHERE memory_id='m2'"
                       ).fetchone()[0] is not None
    con.close()


def test_apply_init_creates_backup(tmp_path):
    p = _mk(tmp_path, [("m1", "dsh", "knowledge", 0.9, 0, None, 0)])
    r = mod.apply_init(p)
    assert r["backup"] and os.path.exists(r["backup"]), "写库前必须留快照（§16.1）"
    assert os.path.getsize(r["backup"]) > 0


def test_backups_are_bounded(tmp_path):
    """本脚本自己引入的缺陷必须自己封顶：库 2.5 GB，每写一次整库拷一份 ⇒ 无界增长。

    反事实：去掉 _prune_backups ⇒ 4 次写后目录里有 4 代，本测试红。
    """
    p = _mk(tmp_path, [("m1", "dsh", "knowledge", 0.9, 0, None, 0)])
    for i in range(4):
        # 手工制造不同的 stamp，模拟多次写（时间戳在同一秒内会相同）
        src = os.path.join(mod.BACKUP_ROOT, mod.BACKUP_PREFIX + "2026010%d-120000" % i)
        os.makedirs(mod.BACKUP_ROOT, exist_ok=True)
        with open(src, "w") as fh:
            fh.write("x" * 10)
    assert len([n for n in os.listdir(mod.BACKUP_ROOT)
                if n.startswith(mod.BACKUP_PREFIX)]) == 4
    mod._prune_backups(keep=2)
    left = sorted(n for n in os.listdir(mod.BACKUP_ROOT)
                  if n.startswith(mod.BACKUP_PREFIX))
    assert len(left) == 2, "同族必须只留最新 2 代"
    assert left[-1].endswith("20260103-120000"), "留下的必须是最新的两代"


def test_prune_does_not_touch_other_backups(tmp_path):
    """只清本家族，不碰目录里其它备份（越界删除是破坏性操作）。"""
    os.makedirs(mod.BACKUP_ROOT, exist_ok=True)
    other = os.path.join(mod.BACKUP_ROOT, "trinity_store.db.pre-sqlcipher-keepme")
    with open(other, "w") as fh:
        fh.write("keep")
    for i in range(3):
        with open(os.path.join(mod.BACKUP_ROOT,
                               mod.BACKUP_PREFIX + "2026010%d-120000" % i), "w") as fh:
            fh.write("x")
    mod._prune_backups(keep=1)
    assert os.path.exists(other), "不得删其它家族的备份"


def test_interval_written_matches_tier(tmp_path):
    p = _mk(tmp_path, [
        ("hi", "dsh", "knowledge", 0.9, 0, None, 0),
        ("mid", "dsh", "knowledge", 0.7, 0, None, 0),
        ("lo", "dsh", "knowledge", 0.1, 0, None, 0),
    ])
    mod.apply_init(p)
    con = sqlite3.connect(p)
    got = dict(con.execute("SELECT memory_id, review_interval_days FROM memories"))
    con.close()
    assert got == {"hi": "30", "mid": "60", "lo": "90"}


# ---- 消费：预算、顺序、以及**默认不动 access_count** -------------------------

def test_consume_does_not_touch_access_count_by_default(tmp_path):
    """核心有意差异：自动复习不得把「机器戳了一下」记成「被读过」。"""
    p = _mk(tmp_path, [("m1", "dsh", "knowledge", 0.9, "30", "2020-01-01T00:00:00", 0)])
    r = mod.consume(p, budget=10)
    assert r["verdict"] == "OK", r
    assert r["reviewed"] == 1
    con = sqlite3.connect(p)
    acc, nxt = con.execute("SELECT access_count, next_review_at FROM memories"
                           " WHERE memory_id='m1'").fetchone()
    con.close()
    assert int(acc) == 0, "默认不得 +1（否则冷率读数做假）"
    assert nxt != "2020-01-01T00:00:00", "到期日必须被推进"


def test_consume_touches_access_count_when_explicitly_asked(tmp_path):
    """反向断言：显式开关必须真的生效，否则这个开关是假的。"""
    p = _mk(tmp_path, [("m1", "dsh", "knowledge", 0.9, "30", "2020-01-01T00:00:00", 5)])
    mod.consume(p, budget=10, touch_access_count=True)
    con = sqlite3.connect(p)
    acc = con.execute("SELECT access_count FROM memories WHERE memory_id='m1'").fetchone()[0]
    con.close()
    assert int(acc) == 6


def test_consume_respects_budget_and_importance_order(tmp_path):
    p = _mk(tmp_path, [
        ("low", "dsh", "knowledge", 0.1, "30", "2020-01-01T00:00:00", 0),
        ("high", "dsh", "knowledge", 0.95, "30", "2020-01-01T00:00:00", 0),
        ("mid", "dsh", "knowledge", 0.7, "30", "2020-01-01T00:00:00", 0),
    ])
    r = mod.consume(p, budget=2)
    assert r["picked"] == 2 and r["reviewed"] == 2
    con = sqlite3.connect(p)
    left = con.execute("SELECT memory_id FROM memories WHERE next_review_at='2020-01-01T00:00:00'"
                       ).fetchone()
    con.close()
    assert left[0] == "low", "预算内必须先消费高 importance"


def test_consume_does_not_touch_not_due_rows(tmp_path):
    p = _mk(tmp_path, [
        ("due", "dsh", "knowledge", 0.9, "30", "2020-01-01T00:00:00", 0),
        ("future", "dsh", "knowledge", 0.9, "30", "2099-01-01T00:00:00", 0),
    ])
    mod.consume(p, budget=10)
    con = sqlite3.connect(p)
    assert con.execute("SELECT next_review_at FROM memories WHERE memory_id='future'"
                       ).fetchone()[0] == "2099-01-01T00:00:00"
    con.close()


# ---- §13.2 取不到 ≠ 不存在 ---------------------------------------------------

def test_missing_store_is_inconclusive(tmp_path):
    r = mod.plan(os.path.join(str(tmp_path), "nope.db"))
    assert r["verdict"] == "INCONCLUSIVE" and "not found" in r["error"]


def test_missing_column_is_inconclusive(tmp_path):
    p = _mk(tmp_path, [("m1", "dsh", "knowledge", 0.9, 0, None, 0)],
            columns=["memory_id", "status", "category"])
    r = mod.plan(p)
    assert r["verdict"] == "INCONCLUSIVE" and "missing columns" in r["error"]


def test_policy_unavailable_is_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "load_tiers", lambda: ([], "UNAVAILABLE: boom"))
    p = _mk(tmp_path, [("m1", "dsh", "knowledge", 0.9, 0, None, 0)])
    r = mod.plan(p)
    assert r["verdict"] == "INCONCLUSIVE" and "policy unavailable" in r["error"]


def test_plan_reports_due_and_backlog(tmp_path):
    p = _mk(tmp_path, [
        ("m1", "dsh", "knowledge", 0.9, "30", "2020-01-01T00:00:00", 0),
        ("m2", "dsh", "knowledge", 0.9, "30", "2099-01-01T00:00:00", 0),
        ("m3", "dsh", "knowledge", 0.9, 0, None, 0),
    ])
    r = mod.plan(p)
    assert r["due_now"] == 1
    assert r["next_unset"] == 1 and r["next_set"] == 2
    assert r["planned_interval_histogram"] == {"30": 3}


# ---- main() 端到端：钉住「写成功但打印崩 ⇒ 退出码 1」这个形态 ------------------

def test_main_apply_exits_zero_and_reports_write(tmp_path, monkeypatch, capsys):
    """回归（2026-10-05 实测）：
    初版把「写操作结果」与「plan 结果」塞进同一个打印分支 ⇒ `apply_init` 已提交成功，
    打印阶段却 `KeyError('engine_exclude_categories')` 崩掉、退出码 1
    —— **数据写好了，命令却报失败**（症状与真相相反）。

    反事实：若把打印分支合并回一条，本测试 rc != 0 当场红。
    """
    p = _mk(tmp_path, [("m1", "dsh", "knowledge", 0.9, 0, None, 0)])
    monkeypatch.setattr(sys, "argv",
                        ["review_schedule_sqlite.py", "--store", p, "--apply"])
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0, "写操作必须 rc=0（否则真写好了也被读成失败）"
    assert "interval_written" in out and "next_written" in out


def test_main_plan_exits_zero(tmp_path, monkeypatch, capsys):
    p = _mk(tmp_path, [("m1", "dsh", "knowledge", 0.9, 0, None, 0)])
    monkeypatch.setattr(sys, "argv", ["review_schedule_sqlite.py", "--store", p])
    assert mod.main() == 0
    assert "可排期 active" in capsys.readouterr().out


def test_main_missing_store_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["review_schedule_sqlite.py", "--store",
                                      os.path.join(str(tmp_path), "nope.db")])
    assert mod.main() == 2, "取不到库必须非零退出（§13.2 不许静默）"
    assert "INCONCLUSIVE" in capsys.readouterr().out


def test_main_consume_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    p = _mk(tmp_path, [("m1", "dsh", "knowledge", 0.9, "30", "2020-01-01T00:00:00", 0)])
    monkeypatch.setattr(sys, "argv",
                        ["review_schedule_sqlite.py", "--store", p, "--consume"])
    assert mod.main() == 0
    con = sqlite3.connect(p)
    assert con.execute("SELECT next_review_at FROM memories WHERE memory_id='m1'"
                       ).fetchone()[0] == "2020-01-01T00:00:00", "没加 --apply 不得改库"
    con.close()
