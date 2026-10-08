# -*- coding: utf-8 -*-
"""2026-09-13 修复回归：自评 FAIL 可见 + cycle_state status 纯函数化。

背景（实测）：
  - /health 报 status=ok、components 全 healthy，而
    ~/.trinity/state/cognitive_eval_last.json 是 PASS=false（gap_recall=0.25）
    → 与 R9"检索全 0 却报健康"同一种健康假象。
  - ~/.trinity/brain/cycle_state.json 中 fsrs 与 teach 的 ts **完全相同**
    （2026-09-11T11:59:22.508427+00:00）却一个 stale 一个 ok
    → status 不是 (ts, now, threshold) 的纯函数。

覆盖：
  A. _cognitive_eval_probe / /health 的 cognitive_eval 组件与 degradation_note
  B. brain_cycle._derive_step_status 的纯函数性 + last_status 不矛盾
"""

import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "scripts"))


# ══════════════════════════════════════════════════════════════════════
# A. /health 反映认知自评
# ══════════════════════════════════════════════════════════════════════

def _write_cogeval(path, pass_, ts, gap=None):
    path.write_text(json.dumps({
        "ts": ts,
        "PASS": pass_,
        "gap": gap if gap is not None else {"gap_recall": 0.25, "gap_precision": 1.0},
        "wm": {"wm_hit": 1.0},
    }, ensure_ascii=False), encoding="utf-8")


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def test_cog_probe_fails_when_pass_false(tmp_path, monkeypatch):
    """PASS=false 且在时效内 → failing（不是 healthy）。"""
    import trinity.api.server._routers_health as h
    f = tmp_path / "cognitive_eval_last.json"
    _write_cogeval(f, False, _now_iso())
    monkeypatch.setattr(h, "COGEVAL_STATE_FILE", str(f))
    val, note = h._cognitive_eval_probe()
    assert val == "failing"
    assert "cognitive_eval" in note


def test_cog_probe_stale_when_old(tmp_path, monkeypatch):
    """超过**配置的**最大年龄 → stale（不是 healthy）。

    2026-10-06（测试归因轮 T1）：原判据写死"ts 超 **36h** ⇒ stale"，直接吃
    `COGEVAL_MAX_AGE_H_DEFAULT` 的默认值。该默认值已于 **2026-10-03** 由 36.0 改成
    **168.0**（`_routers_health.py:66-82` 有实测依据：调度侧把 `cognitive-eval` 放进
    **周链**，而 `loop_health` 对同一指标的阈值本来就是 7d；36h 会让 /health 每周约 5 天恒 degraded）。
    ⇒ 41h 的样本在新默认下**本来就该是 healthy**，原判据不是发现了回归，而是把
    "当时那个默认值"当成了不变量（**判据依赖默认值 ⇒ 改默认值就假红**）。

    现改为用环境变量**显式钉住**要测的那个阈值（36h），断言的是**机制**：
    "年龄 > 配置阈值 ⇒ stale"。若把机制的比较写反（>` 写成 `<`）或删掉该分支，
    本判据仍会红；而调默认值不再误伤它。
    """
    monkeypatch.setenv("TRINITY_COGEVAL_MAX_AGE_H", "36")
    import trinity.api.server._routers_health as h
    f = tmp_path / "cognitive_eval_last.json"
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(hours=41)).isoformat()
    _write_cogeval(f, True, old)
    monkeypatch.setattr(h, "COGEVAL_STATE_FILE", str(f))
    val, note = h._cognitive_eval_probe()
    assert val == "stale"
    assert "cognitive_eval" in note


def test_cog_probe_healthy_when_pass_true_fresh(tmp_path, monkeypatch):
    """PASS=true 且新鲜 → healthy 且无降级原因（负例）。"""
    import trinity.api.server._routers_health as h
    f = tmp_path / "cognitive_eval_last.json"
    _write_cogeval(f, True, _now_iso())
    monkeypatch.setattr(h, "COGEVAL_STATE_FILE", str(f))
    val, note = h._cognitive_eval_probe()
    assert val == "healthy"
    assert note == ""


def test_cog_probe_missing_file_is_degraded(tmp_path, monkeypatch):
    """状态文件缺失 → missing（不得静默放行）。"""
    import trinity.api.server._routers_health as h
    monkeypatch.setattr(h, "COGEVAL_STATE_FILE", str(tmp_path / "nope.json"))
    val, note = h._cognitive_eval_probe()
    assert val == "missing"
    assert "cognitive_eval" in note


def test_cog_probe_env_escape(tmp_path, monkeypatch):
    """TRINITY_HEALTH_COGNITIVE_EVAL=off → disabled 且不降级。"""
    import trinity.api.server._routers_health as h
    monkeypatch.setattr(h, "COGEVAL_STATE_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setenv("TRINITY_HEALTH_COGNITIVE_EVAL", "off")
    val, note = h._cognitive_eval_probe()
    assert val == "disabled"
    assert note == ""


def test_cog_probe_threshold_configurable(tmp_path, monkeypatch):
    """阈值可配：41h 旧文件在 48h 阈值下不再是 stale。"""
    import trinity.api.server._routers_health as h
    f = tmp_path / "cognitive_eval_last.json"
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(hours=41)).isoformat()
    _write_cogeval(f, True, old)
    monkeypatch.setattr(h, "COGEVAL_STATE_FILE", str(f))
    monkeypatch.setenv("TRINITY_COGEVAL_MAX_AGE_H", "48")
    assert h._cognitive_eval_probe()[0] == "healthy"


def test_health_degrades_on_cog_eval_fail(monkeypatch, tmp_path):
    """PASS=false → /health status=degraded + cognitive_eval 组件 + 指名 note。

    同时守住"HTTP 仍为 200"——降级靠 body 诚实表达，不改状态码语义。
    """
    from fastapi.testclient import TestClient
    from trinity.api.server import app
    import trinity.api.server._routers_health as h

    f = tmp_path / "cognitive_eval_last.json"
    _write_cogeval(f, False, _now_iso())
    monkeypatch.setattr(h, "COGEVAL_STATE_FILE", str(f))

    class _FakeMem:
        _adapter = object()
        _engine_error = None

    monkeypatch.setattr(h, "get_memory", lambda: _FakeMem())
    monkeypatch.setattr(h, "_storage_probe", lambda m: None)

    with TestClient(app) as client:
        r = client.get("/health")
        body = r.json()
        assert r.status_code == 200          # 状态码语义不变
        assert body["status"] == "degraded"
        assert body["components"]["cognitive_eval"] == "failing"
        assert "cognitive_eval" in (body.get("degradation_note") or "")


def test_health_ok_when_cog_eval_passes(monkeypatch, tmp_path):
    """PASS=true + 新鲜 → 不再因自评降级（负例对照）。"""
    from fastapi.testclient import TestClient
    from trinity.api.server import app
    import trinity.api.server._routers_health as h

    f = tmp_path / "cognitive_eval_last.json"
    _write_cogeval(f, True, _now_iso())
    monkeypatch.setattr(h, "COGEVAL_STATE_FILE", str(f))

    class _FakeMem:
        _adapter = object()
        _engine_error = None

    monkeypatch.setattr(h, "get_memory", lambda: _FakeMem())
    monkeypatch.setattr(h, "_storage_probe", lambda m: None)

    with TestClient(app) as client:
        body = client.get("/health").json()
        assert body["components"]["cognitive_eval"] == "healthy"
        assert body.get("degradation_note") is None


# ══════════════════════════════════════════════════════════════════════
# B. cycle_state status 是 (ts, now, threshold) 的纯函数
# ══════════════════════════════════════════════════════════════════════

def _load_brain_cycle():
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location(
        "brain_cycle_test_mod", os.path.join(root, "scripts", "brain_cycle.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TS_SAME = "2026-09-11T11:59:22.508427+00:00"   # 实测 fsrs/teach 完全相同的 ts
NOW_LATE = datetime.datetime(2026, 9, 13, 5, 23, 23, tzinfo=datetime.timezone.utc)  # 41.4h 后


def test_status_no_longer_time_derived_for_identical_ts():
    """回归（实测反例）：fsrs 与 teach 同 ts → **执行结果 status 必然相同**。

    旧实现把"执行结果"与"时效"压在同一个 status 上：同 ts 却一个 stale 一个 ok。
    拆分后 status 只表执行结果、不再随时间变化，时间维度由 freshness 承担，
    因此同 ts 的 status 必然一致。
    """
    bc = _load_brain_cycle()
    fsrs = {"status": "stale", "ts": TS_SAME, "last_status": "ok"}
    teach = {"status": "ok", "ts": TS_SAME}
    a = bc._normalize_step_entry(fsrs, "fsrs", NOW_LATE)
    b = bc._normalize_step_entry(teach, "teach", NOW_LATE)
    assert a["status"] == b["status"] == "ok", (a, b)


def test_freshness_is_pure_function_of_ts_now_threshold():
    """时间维度唯一真源：freshness 对同 ts **且同阈值** 必然同值。"""
    bc = _load_brain_cycle()
    # 同 ts、同阈值（fsrs 与 propose 都是 36h）→ 同 freshness
    a = bc._derive_freshness({"status": "stale", "ts": TS_SAME}, "fsrs", NOW_LATE)
    b = bc._derive_freshness({"status": "ok", "ts": TS_SAME}, "propose", NOW_LATE)
    assert a == b == "stale"
    # 同 ts、不同阈值 → 时效本就应当不同（teach 72h 未到，fsrs 36h 已到）
    assert bc._derive_freshness({"ts": TS_SAME}, "fsrs", NOW_LATE) == "stale"
    assert bc._derive_freshness({"ts": TS_SAME}, "teach", NOW_LATE) == "fresh"


def test_freshness_ignores_entry_status():
    """freshness 不得依赖条目自身的 status —— 对同一 now 恒定（纯函数的关键）。

    两种时点都验证：超阈值(stale)与未超阈值(fresh)下，prev status 变化均不改变结果。
    """
    bc = _load_brain_cycle()
    # 2026-09-14（712）：原用例把 36h 阈值**硬编码**进期望值（24h<36h→fresh / 41.4h>36h→stale），
    # 而 STEP_MAX_AGE_H["fsrs"] 现为 **20h** ⇒ 用例实际测的是"阈值没变"，而不是它声明的不变量。
    # 改为**以代码自己的阈值取相对时点**，测的才是本用例声明的不变量。
    _thr = float(bc._step_threshold_h("fsrs"))
    _ts = datetime.datetime.fromisoformat(TS_SAME)
    for now, expect in ((_ts + datetime.timedelta(hours=_thr * 0.5), "fresh"),
                        (_ts + datetime.timedelta(hours=_thr * 1.5), "stale")):
        for prev in ("ok", "stale", "fresh", "fail", "error", None):
            e = {"status": prev, "ts": TS_SAME}
            assert bc._derive_freshness(e, "fsrs", now) == expect, (prev, now, _thr)


def test_result_status_not_washed_away_by_time():
    """error/fail 是执行结果：时效再陈旧也不得把 status 洗成 stale。"""
    bc = _load_brain_cycle()
    old = "2026-01-01T00:00:00+00:00"
    for st in ("error", "fail"):
        norm = bc._normalize_step_entry({"status": st, "ts": old}, "fsrs", NOW_LATE)
        assert norm["status"] == st, norm
        assert norm["freshness"] == "stale"


def test_missing_ts_is_stale_freshness():
    bc = _load_brain_cycle()
    assert bc._derive_freshness({"status": "ok"}, "fsrs") == "stale"
    assert bc._derive_freshness({"status": "ok", "ts": "not-a-date"}, "fsrs") == "stale"


def test_fresh_entry_is_fresh():
    bc = _load_brain_cycle()
    now = datetime.datetime(2026, 9, 13, 5, 0, 0, tzinfo=datetime.timezone.utc)
    e = {"status": "ok", "ts": "2026-09-13T04:00:00+00:00"}
    assert bc._derive_freshness(e, "fsrs", now) == "fresh"
    assert bc._normalize_step_entry(e, "fsrs", now)["status"] == "ok"


def test_last_status_never_contradicts_status():
    """last_status 与 status 同源 → 不可能矛盾（消费者可安全平滑迁移）。"""
    bc = _load_brain_cycle()
    now = datetime.datetime(2026, 9, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)
    for entry in ({"status": "ok", "ts": TS_SAME},
                  {"status": "stale", "ts": TS_SAME, "last_status": "ok"},
                  {"status": "ok", "ts": "2026-09-12T11:30:00+00:00"},
                  {"status": "stale", "ts": TS_SAME, "last_status": "fail"}):
        norm = bc._normalize_step_entry(entry, "fsrs", now)
        assert norm["status"] == bc._derive_result_status(norm.get("last_status"))
        if "last_status" in norm:
            assert norm["last_status"] in ("ok", "fail", "error", "skipped", "unknown")
        assert norm["freshness"] in ("fresh", "stale")


def test_step_is_fresh_delegates_to_pure_function(monkeypatch):
    """断点新鲜度 = 纯函数判 ok（不再自行拼装 age 逻辑）。"""
    bc = _load_brain_cycle()
    # 2026-09-14（714）：原用例把 ts 写死成 2026-09-13T04:00（相对"真实 now"），
    # 于是① 随日历自然腐烂（过了 fsrs 阈值就必挂）；② 阈值从 36h 调成 **20h** 后立刻失效。
    # 改为**相对 now、相对阈值**构造，测的才是本用例声明的委托关系。
    _thr = float(bc._step_threshold_h("fsrs"))
    _now = datetime.datetime.now(datetime.timezone.utc)
    fresh = {"status": "ok", "ts": (_now - datetime.timedelta(hours=_thr * 0.5)).isoformat()}
    assert bc._step_is_fresh(fresh, "fsrs") is True  # 阈值以内 → 新鲜

    # 把模块内的「现在」整体前推 2×阈值 → 同一条目转陈旧
    base = _now + datetime.timedelta(hours=_thr * 2)
    _freeze_now(bc, monkeypatch, base)
    assert bc._step_is_fresh(fresh, "fsrs") is False


def _freeze_now(bc, monkeypatch, fixed):
    """冻结 brain_cycle 模块内的"现在"（该模块直接调用 datetime.now）。"""
    real = bc.datetime

    class _FrozenDT(real):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(bc, "datetime", _FrozenDT)
