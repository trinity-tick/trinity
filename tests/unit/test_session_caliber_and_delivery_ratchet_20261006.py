# -*- coding: utf-8 -*-
"""t88 / B4·A5-04 判据：**会话侧再获取计费**（可机器读）+ **投递/消费棘轮**（可独立复算、基线只降）。

## 口径纪律（本文件的第一原则）

| 侧 | 数据源 | 归属 |
|---|---|---|
| **注入侧** | `~/.trinity/state/opening_surface_deliveries.jsonl` | **t58 的"省 71% token"就是这一侧** |
| **会话侧** | `~/.trinity/state/retrieval_traces.jsonl` | 本任务补的**另一侧**（pull 腿） |

⛔ **两侧不得相加/相减/并列** —— 本文件的判据只断言"两侧各有独立字段 + 账本自带禁止混算声明"，
**绝不**把 `reacquired_tokens` 与任何注入侧数字做算术。

网络依据：arXiv 2608.16370（2026-08-17）逐字"compression can increase an agent's interaction cost by
**forcing it to reacquire dropped state** while leaving completion statistically unchanged."

判据：S1 真实数据非零 · S2 反事实为零（含"能变非零"的反向牙齿）· R1 棘轮退化必红 + 基线只许下调 ·
T1 牙齿（计费写入被改静默 ⇒ S1 必红）。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE = os.path.join(os.path.expanduser("~"), ".trinity", "state")
REAL_TRACES = os.path.join(STATE, "retrieval_traces.jsonl")
REAL_DELIVERIES = os.path.join(STATE, "opening_surface_deliveries.jsonl")
LEDGER_SCRIPT = os.path.join(ROOT, "scripts", "session_pull_ledger.py")
RATCHET_SCRIPT = os.path.join(ROOT, "scripts", "delivery_ratchet.py")


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ledger():
    return _load(os.path.join("scripts", "session_pull_ledger.py"), "_t88_ledger")


def _ratchet():
    return _load(os.path.join("scripts", "delivery_ratchet.py"), "_t88_ratchet")


def _records(path: str):
    out = []
    with io.open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


# ── S1：真实数据上，会话侧计费**非零**（锚点=文件 ⇒ 可复现，不看挂钟）──────────
def test_S1_会话侧再获取计费在真实数据上非零():
    mod = _ledger()
    assert os.path.isfile(REAL_TRACES), "没有 pull 腿轨迹文件 ⇒ 这条测不了（不是'通过'）"
    rec = mod.build_record(REAL_TRACES, REAL_DELIVERIES, hours=24.0, anchor="file")
    assert rec["side"] == "session-pull", rec["side"]
    assert "会话侧" in rec["caliber"] and "禁止相加" in rec["caliber"], rec["caliber"]
    assert rec["pull_calls"] > 0, rec
    assert rec["distinct_items"] > 0, rec
    # ⭐ 核心：**再获取**真的被计到（这正是 arXiv 2608.16370 说的"被压掉的 state 必须被再获取"）
    assert rec["reacquired_items"] > 0, rec
    assert rec["reacquired_tokens"] > 0, rec
    assert rec["token_estimator"] and "估算" in rec["token_estimator"], rec["token_estimator"]
    # 两侧**各自独立**存在（不是合并成一个数）
    assert isinstance(rec["injection_delivered_n"], int)
    assert "禁止与本侧相加" in rec["injection_side_note"], rec["injection_side_note"]


def test_S1_两路口径不混算_账本字段结构可核对():
    rec = _ledger().build_record(REAL_TRACES, REAL_DELIVERIES, hours=1.0, anchor="file")
    # 会话侧字段与注入侧字段**命名空间分开**，且注入侧字段名一律以 injection_ 开头
    inj_keys = [k for k in rec if "injection" in k or k.startswith("delivered_")]
    assert inj_keys, "缺少注入侧只读引用字段"
    assert all(k.startswith(("injection", "delivered_overlap")) for k in inj_keys), inj_keys
    # 禁止存在"合并两侧"的字段名（例如 total_tokens / both_sides）
    for bad in ("total_tokens", "both_sides_tokens", "combined_tokens", "net_saving"):
        assert bad not in rec, "出现了把两侧混算的字段：%s" % bad


# ── S2：反事实 —— 不触发再获取 ⇒ 计数为 0（防恒真）────────────────────────────
def _write_traces(path: str, calls):
    with io.open(path, "w", encoding="utf-8") as fh:
        for ts, ids in calls:
            fh.write(json.dumps({"ts": ts, "query": "q", "agent_id": "",
                                 "chosen": ids, "top_k": len(ids),
                                 "meta": {"strategy": "probe"}}, ensure_ascii=False) + "\n")


def test_S2_反事实_每次都取到不同条目则为零(tmp_path):
    mod = _ledger()
    traces = str(tmp_path / "unique_traces.jsonl")
    base = 1791376000.0
    _write_traces(traces, [(base + i * 60, ["mem-%d" % i]) for i in range(10)])
    rec = mod.build_record(traces, str(tmp_path / "none.jsonl"), hours=24.0, anchor="file")
    assert rec["pull_calls"] == 10, rec
    assert rec["reacquired_items"] == 0, rec
    assert rec["reacquired_tokens"] == 0, rec
    # 反向牙齿：把同一个 id 出现两次 ⇒ **必须**变非零（否则说明计数器坏了/恒 0）
    dup = str(tmp_path / "dup_traces.jsonl")
    _write_traces(dup, [(base + 0, ["mem-X"]), (base + 60, ["mem-X"])])
    rec2 = mod.build_record(dup, str(tmp_path / "none.jsonl"), hours=24.0, anchor="file")
    assert rec2["reacquired_items"] == 1, rec2
    assert rec2["reacquired_tokens"] >= 1, rec2      # 正文取不到时按 0 字算 ⇒ 仍 ≥1（min 1）


# ── R1：棘轮 —— 人为退化必红；基线**只能下调**（代码里拦住）──────────────────
def test_R1_棘轮_退化必红且正常必绿():
    mod = _ratchet()
    base = {"undelivered_bp": 9956}          # t97b 冻结口径下的实测基线
    good = {"undelivered_bp": 9956, "delivery_calls": 270, "distinct_delivered": 124,
            "active_memories": 28448, "denominator_active": 28394, "hours": 168.0}
    bad = dict(good, undelivered_bp=9987, distinct_delivered=37)   # ≈ top_k=1 的形态（实测 9987）
    ok, reasons, detail = mod.evaluate(good, base)
    assert ok is True and reasons == [], (ok, reasons, detail)
    ok2, reasons2, detail2 = mod.evaluate(bad, base)
    assert ok2 is False, (ok2, detail2)
    assert any("undelivered_bp" in r and "退化" in r for r in reasons2), reasons2


def test_R1_棘轮_真实输入可独立复算且与基线一致():
    """⚠️ 2026-10-07（t97b）**口径冻结**后重写：分母 = 基线时刻 active、窗长 = 基线里的 `window_hours`。

    为什么改（见 docs/DELIVERY_RATCHET_BASELINE.json 的 `_caliber_change_20261007`）：
    旧口径（当前 active 当分母 + 24h 窗）在 full6 里首次发火（9987 > 9986，+1），定性后确认是**口径问题**：
    ① 分母自己会长（语料单日 active +179）⇒ 投递不变也推高比值；② 24h 窗只覆盖账本 ~18% ⇒
    **窗口一滑 distinct 就抖**（13 个相邻锚点实测 distinct ∈ [34,45]、bp ∈ [9984,9988]）。
    冻结后：窗 168h、分母 28394 ⇒ bp=9956，三次重跑逐字一致；而"top_k=1 等价降级"落到 9987（**余量 31bp**，此前只有 1bp）。
    """
    mod = _ratchet()
    if not os.path.isfile(REAL_DELIVERIES):
        pytest.fail("没有投递账本 ⇒ 这条测不了（不静默跳过）")
    base = json.loads(io.open(os.path.join(ROOT, "docs", "DELIVERY_RATCHET_BASELINE.json"),
                              encoding="utf-8").read())
    m = base.get("measured") or {}
    hours = float(m["window_hours"])
    denom = int(m["denominator_active"])
    assert hours > 0 and denom > 0, ("基线必须冻结口径（窗长 + 分母）", m)
    stats = mod.metrics(REAL_DELIVERIES, hours=hours, anchor="file", frozen_active=denom)
    assert stats["active_memories"] > 0 and stats["distinct_delivered"] > 0, stats
    assert stats["denominator_active"] == denom, stats          # 分母确实被冻结
    expect = int(round(10000.0 * (denom - stats["distinct_delivered"]) / float(denom)))
    assert stats["undelivered_bp"] == expect, (stats, expect)   # 公式可独立复算
    assert int(base["undelivered_bp"]) <= 10000
    ok, reasons, detail = mod.evaluate(stats, base)
    assert ok, (reasons, detail)         # 当前实测必须**不高于**已登记的基线


def test_R1_牙齿_口径不匹配必须_UNTESTABLE():
    """**牙齿**（t97b 新增）：拿旧窗长跑 ⇒ 必须 **rc=2**（口径混用要被拦住，不许出数）。

    没有这条，冻结口径就只是文档约定；有了它，"随手用 24h 再报个数"会当场变成 UNTESTABLE。
    """
    base = json.loads(io.open(os.path.join(ROOT, "docs", "DELIVERY_RATCHET_BASELINE.json"),
                              encoding="utf-8").read())
    frozen = float((base.get("measured") or {})["window_hours"])
    wrong = 24.0 if abs(frozen - 24.0) > 1e-9 else 12.0
    r = subprocess.run([sys.executable, RATCHET_SCRIPT, "--hours", str(wrong), "--json"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600)
    assert r.returncode == 2, (r.returncode, r.stdout[-300:])
    assert "口径混用" in (r.stdout or ""), r.stdout[-300:]
    # 反向：用**冻结**窗长跑同一入口必须正常出数（rc ∈ {0,1}，不是 UNTESTABLE）
    r2 = subprocess.run([sys.executable, RATCHET_SCRIPT, "--hours", str(frozen), "--json"],
                        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=600)
    assert r2.returncode in (0, 1), (r2.returncode, r2.stdout[-300:])


def test_R1_牙齿_基线只许下调_提高会被拒绝(tmp_path):
    mod = _ratchet()
    bl = str(tmp_path / "baseline.json")
    with io.open(bl, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"undelivered_bp": 10}, ensure_ascii=False))
    r = subprocess.run([sys.executable, RATCHET_SCRIPT, "--write-baseline", "--baseline", bl],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600)
    assert r.returncode == 1, (r.returncode, r.stdout[-300:])
    assert "只能下调" in (r.stdout or ""), r.stdout[-300:]
    after = json.loads(io.open(bl, encoding="utf-8").read())
    assert int(after["undelivered_bp"]) == 10, "被拒绝后基线文件竟被改写：%s" % after


# ── T1：牙齿 —— 把计费写入改成**静默** ⇒ S1 的断言必须红 ─────────────────────
def _billing_visible(mod, tmp_ledger: str) -> bool:
    """走一遍"真跑 + 写入 + 读回"；只有**账本里能读到非零**才返回 True。"""
    rec = mod.build_record(REAL_TRACES, REAL_DELIVERIES, hours=24.0, anchor="file")
    mod.append_record(rec, tmp_ledger)
    if not os.path.isfile(tmp_ledger):
        return False
    rows = _records(tmp_ledger)
    if not rows:
        return False
    return rows[-1]["reacquired_tokens"] > 0


def test_T1_牙齿_计费写入被改静默则判据S1必红(monkeypatch, tmp_path):
    mod = _ledger()
    good = str(tmp_path / "ledger_ok.jsonl")
    assert _billing_visible(mod, good) is True, "正常路径下账本读不到非零 ⇒ 计费形同虚设"
    # 牙齿：把写入改成静默（不写、也不报错——本仓最恨的那种"看着像在记"）
    monkeypatch.setattr(mod, "append_record", lambda rec, ledger=None: None, raising=False)
    silent = str(tmp_path / "ledger_silent.jsonl")
    with pytest.raises(AssertionError):
        assert _billing_visible(mod, silent) is True


# ── T2：**产品侧落点**（t88 phase-2）真的把两个字段写进轨迹 ──────────────────
def test_T2_轨迹字段_pull_calls_delta与reacquired_hit_ids真的落盘(tmp_path):
    """`trinity/retrieval/trace.py` 只加的两个字段必须**真落盘**且语义正确。

    这是 t88 phase-2 的正面判据：① 每行都带 `pull_calls_delta == 1`；
    ② 第一次取回 ⇒ `reacquired_hit_ids == []`（防恒非空）；
    ③ 第二次取回同一条 ⇒ 该 id **必须**出现在 `reacquired_hit_ids` 里（防恒空）。
    """
    spec = importlib.util.spec_from_file_location(
        "_t88_trace", os.path.join(ROOT, "trinity", "retrieval", "trace.py"))
    tr = importlib.util.module_from_spec(spec)
    sys.modules["_t88_trace"] = tr
    spec.loader.exec_module(tr)
    assert getattr(tr, "REACQUIRE_WINDOW", 0) > 0, "滚动窗口口径缺失 ⇒ 计费无法复算"

    p = str(tmp_path / "traces.jsonl")
    t = tr.RetrievalTracer(path=p)
    t.record("q1", chosen=["mem-A", "mem-B"])
    t.record("q2", chosen=["mem-C"])                      # 全新 ⇒ 不得报再获取
    t.record("q3", chosen=["mem-A"])                      # 与第一行重复 ⇒ 必须报
    rows = [json.loads(ln) for ln in io.open(p, encoding="utf-8") if ln.strip()]
    assert len(rows) == 3, rows
    assert all(r.get("pull_calls_delta") == 1 for r in rows), [r.get("pull_calls_delta") for r in rows]
    assert rows[0]["reacquired_hit_ids"] == [], rows[0]    # 首次 ⇒ 空
    assert rows[1]["reacquired_hit_ids"] == [], rows[1]    # 全新 ⇒ 空
    assert rows[2]["reacquired_hit_ids"] == ["mem-A"], rows[2]   # 重复 ⇒ 命中
    # 既有字段语义未被改动（只加不改）
    assert rows[0]["chosen"] == ["mem-A", "mem-B"], rows[0]
    assert "meta" in rows[0] and "latency_ms" in rows[0], sorted(rows[0])
