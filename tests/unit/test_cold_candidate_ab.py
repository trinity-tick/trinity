# -*- coding: utf-8 -*-
"""冷条候选源 A/B（doc2query vs §818 随机分层）判据测试。

## 钉住的失败形态

1. **判据必须能失败**：`doc2query` 未优于 §818 随机分层 ⇒ FAIL（机制不成立，不铺开）；
   未优于均匀零假设 ⇒ FAIL（选源没带来信息）。
2. **§818 臂必须真的是「分层 + 稳定伪随机 + 层间轮转」**：不能被最大的层垄断，
   且同一 salt 可复现、不同 salt 打散。
3. **候选池必须被尊重**：`doc2query` 选源只能从池内取（池外命中是假命中）。
4. **目标自身默认剔除**：否则三臂都"必中"、读数虚高到没有判别力。
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    path = os.path.join(ROOT, "scripts", "cold_candidate_ab.py")
    spec = importlib.util.spec_from_file_location("cold_candidate_ab", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cold_candidate_ab"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


# ---- hit_rate ----------------------------------------------------------------

def test_hit_rate():
    assert mod.hit_rate(0, 0) == 0.0
    assert mod.hit_rate(1, 2) == 0.5
    assert mod.hit_rate(3, 4) == 0.75


# ---- uniform -----------------------------------------------------------------

def test_uniform_is_deterministic_and_bounded():
    pool = ["m%d" % i for i in range(50)]
    a = mod.pick_uniform(pool, 5, "seed-A")
    b = mod.pick_uniform(pool, 5, "seed-A")
    c = mod.pick_uniform(pool, 5, "seed-B")
    assert a == b, "同一 seed 必须可复现"
    assert a != c, "不同 seed 应打散（否则与查询无关）"
    assert len(a) == 5 and set(a) <= set(pool)
    assert len(mod.pick_uniform(pool, 999, "s")) == 50, "n 超过池子时不得报错"


# ---- §818 同款分层 ------------------------------------------------------------

def test_rand_strat_is_deterministic():
    pbc = {"a": ["a1", "a2", "a3"], "b": ["b1", "b2"]}
    assert mod.pick_rand_strat(pbc, 2, "s1") == mod.pick_rand_strat(pbc, 2, "s1")


def test_rand_strat_spreads_across_strata_not_monopolised():
    """层间轮转：N=2 时不应两票全给最大的层。"""
    pbc = {"big": ["g%d" % i for i in range(50)], "small": ["s1"]}
    picked = mod.pick_rand_strat(pbc, 2, "salt")
    assert len(picked) == 2
    assert len(set(picked)) == 2, "不得重复取同一条"


def test_rand_strat_respects_n_and_pool():
    pbc = {"a": ["a1"], "b": ["b1"]}
    assert sorted(mod.pick_rand_strat(pbc, 5, "s")) == ["a1", "b1"]
    assert mod.pick_rand_strat({}, 2, "s") == []


def test_rand_strat_different_salt_differs():
    """盐必须真的参与选样：**固定 20 个盐，结果至少出现 2 种**。

    2026-10-06（t41/N2）：原实现是
        `assert mod.pick_rand_strat(pbc, 3, "q1") != mod.pick_rand_strat(pbc, 3, "q2") or True`
    —— `or True` 让它**永远不可能失败**（`scripts/fake_green_audit.py --ratchet` 因此判红）。
    当时加 `or True` 大概是为了躲"N=3 从 200 里取，两个盐恰好撞上同一组"的偶发。

    正确做法不是把断言变成恒真，而是换成**确定性判别式**：
    在固定的一组盐上要求"选样结果至少有 2 种" —— 实现若**忽略盐**（真正的缺陷形态）
    ⇒ 只有 1 种 ⇒ 立刻红；而个别盐撞车不影响结论。
    """
    pbc = {"a": ["a%d" % i for i in range(200)]}
    picks3 = {tuple(sorted(mod.pick_rand_strat(pbc, 3, "salt-%02d" % i))) for i in range(20)}
    assert len(picks3) >= 2, (
        "20 个不同盐只产出 1 种 N=3 选样结果 ⇒ 盐没有被用于选样（恒真断言的替代判别式）")
    picks1 = {tuple(mod.pick_rand_strat(pbc, 1, "salt-%02d" % i)) for i in range(20)}
    assert len(picks1) >= 2, "N=1 时 20 个盐只产出同一条 ⇒ 盐未参与同层排序"


# ---- judge：必须能失败，且带效应量地板 ---------------------------------------

def _res(b, a, u, n=200, hits=None):
    return {"n_evaluated": n,
            "arms": {"doc2query": {"hit@n": b, "hits": hits if hits is not None else int(b * n)},
                     "rand_strat": {"hit@n": a}, "uniform": {"hit@n": u}}}


def test_wilson_ci_bounds_and_degenerate():
    lo, hi = mod.wilson_ci(3, 220)
    assert 0.0 < lo < hi < 0.1, "小比例应给出窄的、非零下界"
    assert mod.wilson_ci(0, 0) == (0.0, 0.0)
    lo2, hi2 = mod.wilson_ci(0, 200)
    assert lo2 == 0.0, "零命中时下界为 0"


def test_judge_pass_when_doc2query_wins_with_effect():
    v = mod.judge(_res(0.35, 0.02, 0.01, n=200, hits=70))
    assert v["verdict"] == "PASS", v
    assert "wilson95" in v


def test_judge_fails_when_not_better_than_818():
    v = mod.judge(_res(0.02, 0.02, 0.01, n=200, hits=4))
    assert v["verdict"] == "FAIL" and "机制不成立" in v["why"]
    v2 = mod.judge(_res(0.01, 0.30, 0.01, n=200, hits=2))
    assert v2["verdict"] == "FAIL", "比随机分层还差必须 FAIL"


def test_judge_fails_when_no_better_than_uniform():
    v = mod.judge(_res(0.05, 0.02, 0.05, n=200, hits=10))
    assert v["verdict"] == "FAIL" and "没带来信息" in v["why"]


def test_judge_inconclusive_on_missing_arm():
    assert mod.judge({"arms": {"doc2query": {"hit@n": 0.5}}})["verdict"] == "INCONCLUSIVE"


def test_judge_inconclusive_without_queries():
    v = mod.judge(_res(0.5, 0.0, 0.0, n=0, hits=0))
    assert v["verdict"] == "INCONCLUSIVE" and "n_evaluated" in v["why"]


def test_judge_fails_on_tiny_effect_despite_correct_direction():
    """**回归（2026-10-05 真实读数）**：3/220 = 1.36% vs 基线 0.0。

    方向对、且 95% 下界 > 0，但 1.36% 意味着冷槽位 **98.6% 的时间注入无关内容**
    —— 正是 §818 量到**有害**的形态。初版只有严格不等式 ⇒ 这种读数会被判 PASS，
    把一个实践上无用的机制放行。本测试钉住「必须带效应量地板」。
    """
    v = mod.judge(_res(0.0136, 0.0, 0.0, n=220, hits=3))
    assert v["verdict"] == "FAIL", "方向对但效应量过小，不得 PASS"
    assert "效应量过小" in v["why"]


def test_min_hit_floor_is_configurable_and_bites():
    """地板必须真的生效（可调，但调了要显式写在命令里）。"""
    r = _res(0.0136, 0.0, 0.0, n=220, hits=3)
    assert mod.judge(r, min_hit=0.05)["verdict"] == "FAIL"
    assert mod.judge(r, min_hit=0.01)["verdict"] == "PASS", "地板降到 1% 时该读数才够格"


# ---- doc2query 选源必须限定在池内 --------------------------------------------

def test_pick_by_doc2query_only_returns_pool_members(tmp_path):
    import sqlite3
    p = os.path.join(str(tmp_path), "s.db")
    con = sqlite3.connect(p)
    con.execute("CREATE VIRTUAL TABLE memories_doc2query USING fts5("
                "memory_id UNINDEXED, questions, tokenize='unicode61')")
    con.execute("INSERT INTO memories_doc2query VALUES ('in1', '库存 对账 方法')")
    con.execute("INSERT INTO memories_doc2query VALUES ('out1', '库存 对账 方法')")
    con.commit()
    got = mod.pick_by_doc2query(con, {"in1"}, "库存对账方法", 2)
    con.close()
    assert got == ["in1"], "池外的 out1 不得被返回（那是假命中）"


def test_pick_by_doc2query_empty_tokens(tmp_path):
    import sqlite3
    p = os.path.join(str(tmp_path), "s.db")
    con = sqlite3.connect(p)
    con.execute("CREATE VIRTUAL TABLE memories_doc2query USING fts5("
                "memory_id UNINDEXED, questions, tokenize='unicode61')")
    con.commit()
    assert mod.pick_by_doc2query(con, {"x"}, "   ", 2) == []
    con.close()


# ---- run() 的守门：缺表 / 缺库必须 INCONCLUSIVE --------------------------------

def test_run_inconclusive_without_tables(tmp_path):
    import sqlite3
    p = os.path.join(str(tmp_path), "s.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE memories (memory_id TEXT, category TEXT, status TEXT, "
                "access_count TEXT)")
    con.commit()
    con.close()
    mod.load_exclusions = lambda: (["perception"], "test")
    r = mod.run(p, 2, 10)
    assert r["verdict"] == "INCONCLUSIVE" and "先跑" in r["error"]


def test_run_inconclusive_without_store(tmp_path):
    mod.load_exclusions = lambda: (["perception"], "test")
    r = mod.run(os.path.join(str(tmp_path), "no.db"), 2, 10)
    assert r["verdict"] == "INCONCLUSIVE" and "not found" in r["error"]
