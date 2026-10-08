# -*- coding: utf-8 -*-
"""t85 / B1-A5-01 判据：**每条敏感判定都可查询**（承接 t64 审计静默 + t43 不说假话）。

判据：C1 判定必有记录｜C2 反事实·无判定无记录｜C3 动作一致（**不说假话**）｜C4 可机器查询｜
C5 适配器路径也留痕（t85 §1 点名的**第一个空洞**：`_crud.py:191-195` 拒存直接 return，无痕）。
牙齿：M1 改回静默 ⇒ C1 红｜M2 每次必记 ⇒ C2 红｜M3 把 tier 写死（说假话）⇒ C3 红｜M4 过滤器失效 ⇒ C4 红｜
M5 只记 redact 不记 refuse（"只给掩码路径加观测"这种真实写法）⇒ C5 红。

口径：全部在**临时 SQLite** 上跑（不碰生产库）；每条判据先 `reset_decision_log()`；
判据名**纯 ASCII**（t90 的教训：标识符里的带圈数字会让文件 SyntaxError ⇒ collect 报错）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import trinity.security.sensitive as S  # noqa: E402

AGENT = "t85-decisions"
HIGH_TEXT = "我想自杀，活着太累了。"                       # high ⇒ refuse
MEDIUM_TEXT = "最近有点抑郁，但联系方式 13800138000。"       # medium + PII
PII_ONLY = "发货联系人手机 13800138000，邮箱 zhangsan@corp.cn。"  # 纯 PII ⇒ redact
BENIGN = "用户偏好暗色模式，使用 VS Code 与 Python。"          # 无判定 ⇒ 不得有记录
DOWNGRADED = '2. **Arrested Development** (Netflix): a witty sitcom.'  # t70：缺个人语境 ⇒ 降级

REQUIRED_KEYS = ("schema", "policy_version", "rule_ids", "categories", "pii_kinds",
                 "severity", "tier", "action_basis", "switches", "downgraded",
                 "no_context_downgraded", "content_len", "content_fp", "ts")


def _live():
    """⭐ 取**当前活着的** `sensitive` 模块实例（t85 判据的测试隔离缺陷修复，2026-10-07）。

    症状（t106 回归里复现）：**同一批 7 个判据文件一起跑时 C3/C5 红，单跑全绿**。
    机制：本仓有判据会 `del sys.modules[...]` 后**重新导入** `sensitive`
    （`test_high_category_false_positives_20261006.py::c4` 的 env 回滚臂、`test_sensitive_pii_scope` 的多实例机制）
    ⇒ 写路径 `from trinity.security.sensitive import ...` 取到**新实例**，而本文件模块级绑定的 `S`
    仍指向**旧实例** ⇒ 记录写进**新 deque**、`_live().decision_log()` 读的是**旧 deque** ⇒ **"记录了但查不到"的假红**。
    ⚠️ 这是**判据隔离**问题，**不是产品缺陷**（产品行为未见异常）。
    ⭐ **改本处时请同步看 `tests/unit/test_redaction_surface_20261006.py::_patch_everywhere()`**
    （同一根因的第二处：那里要把**牙齿**打到**所有实例**上；两边 docstring 互相指明以防漂移）。
    """
    return sys.modules.get("trinity.security.sensitive") or S


# ── t119/G9R-5：**全实例**读写（沿用 t93/C1 的 `_patch_policy` 手法，逐行照搬）──────
#: ⚠️ 为什么需要这一层（t119 实测，7 文件同跑 **5/5 确定性 2 红**）：
#:   · `test_criteria_pass[C5]` 红 ⇒ `assert False is True`：**适配器路径**（产品代码 `from … import`）
#:     把记录写进了**另一个实例**的 deque，而判据只读 `_live()` 那个 ⇒ "记录了但查不到"；
#:   · `…killing_mutant[C4]` 红 ⇒ "变异体没有杀掉判据"：M4 只 `setattr(S, "decision_log", …)`
#:     —— `S` 是**收集期**绑定，长进程里被别的判据 `del sys.modules[…]` 重导后已**不是活实例**
#:     ⇒ 变异体变 **no-op**、元判据误报"没有判别力"。
#: ⇒ 读写都必须覆盖**所有**实例：写侧（`_live()` 仍是规范实例）+ 读侧（`_all_log()` 合并）
#:   + 变异体（`_patch_policy()` 打到所有实例，并返回 `n` 供 `n >= 1` 守卫）。
_patch_errors = []


def _all_sensitive_modules():
    """**所有** `trinity.security.sensitive` 实例（含运行时被重新导入出来的重复实例）。

    与 `test_sensitive_pii_scope_20261006.py::_all_sensitive_modules`（t93/C1）**同一实现**：
    `sys.modules` 里的那个 + `gc` 扫出的同 `__name__` ModuleType + 本文件收集期绑定的 `S`。
    """
    out, seen = [], set()
    try:
        import importlib
        cand = [importlib.import_module("trinity.security.sensitive")]
    except Exception:  # noqa: BLE001
        cand = []
    try:
        import gc
        import types
        for obj in gc.get_objects():
            if isinstance(obj, types.ModuleType) and str(getattr(obj, "__name__", "")).endswith(
                    "security.sensitive"):
                cand.append(obj)
    except Exception as _e:  # noqa: BLE001 —— 不静默（t85/B1）
        _patch_errors.append("gc scan: %r" % (_e,))
    if S not in cand:
        cand.append(S)
    for m in cand:
        if id(m) not in seen:
            seen.add(id(m))
            out.append(m)
    return out


def _patch_policy(mp, name, new) -> int:
    """把 `name` 替换成 `new`，覆盖**所有**持有者；返回被替换的持有者数量（0 ⇒ 元判据直接红）。

    与 `test_sensitive_pii_scope_20261006.py::_patch_policy`（t93/C1）**同一实现**（去掉本文件用不到的
    `inplace` 分支）；改 `t93` 那份时请同步这里，避免两套漂移。
    """
    n = 0
    orig = None
    for m in _all_sensitive_modules():
        if getattr(m, "__name__", "") == "trinity.security.sensitive" and hasattr(m, name):
            orig = getattr(m, name)
            break
    targets = list(_all_sensitive_modules())
    for m in targets:
        try:
            if hasattr(m, name):
                mp.setattr(m, name, new, raising=False)
                n += 1
        except Exception as _e:  # noqa: BLE001 —— 不静默（t85/B1）
            _patch_errors.append("setattr %s@%s: %r" % (name, getattr(m, "__name__", "?"), _e))
    try:
        for mod in list(sys.modules.values()):
            if mod is None or mod in targets:
                continue
            try:
                if orig is not None and getattr(mod, name, None) is orig:
                    mp.setattr(mod, name, new, raising=False)
                    n += 1
            except Exception as _e:  # noqa: BLE001 —— 不静默（t85/B1）
                _patch_errors.append("setattr %s@%s: %r" % (name, getattr(mod, "__name__", "?"), _e))
    except Exception as _e:  # noqa: BLE001 —— 不静默（t85/B1）
        _patch_errors.append("module sweep: %r" % (_e,))
    assert n >= 1, (
        "`_patch_policy(%s)` 没打到任何持有者 ⇒ 变异体会变 no-op、元判据会误报（t119；"
        "patch 错误：%r）" % (name, _patch_errors[-3:]))
    return n


def _reset_all() -> int:
    """把**所有**实例的判定账本清空（避免上一实例的残留记录污染计数口径）。返回清空的实例数。"""
    n = 0
    for m in _all_sensitive_modules():
        fn = getattr(m, "reset_decision_log", None)
        if callable(fn):
            fn()
            n += 1
    return n


def _all_log(**filters) -> list:
    """**合并所有实例**的账本（按 `ts`+`content_fp`+`tier` 去重）—— 覆盖"写路径用了哪个实例"。

    ⚠️ 这不是放宽：合并只会让 C1/C3/C5 **更严格**（任何一个实例上的记录都能被看到），
    C2 的反事实也更严格（任何一个实例里有记录就算违反）。
    """
    out, seen = [], set()
    for m in _all_sensitive_modules():
        fn = getattr(m, "decision_log", None)
        if not callable(fn):
            continue
        try:
            recs = fn(limit=0, **filters) or []
        except Exception:  # noqa: BLE001 —— 不静默
            continue
        for r in recs:
            if not isinstance(r, dict):
                continue
            key = (r.get("ts"), r.get("content_fp"), r.get("tier"), r.get("action"))
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
    return out


@pytest.fixture(autouse=True)
def _fresh_log(monkeypatch):
    """⭐ 让判据**自己控制前置条件**（t106 回归里抓到的跨文件泄漏）。

    症状：与 `test_knowledge_pack.py` 等文件同跑时，本文件偶发 2 条红（C3/C5 或 C4 的牙齿），单跑全绿。
    机制：开关是**进程级环境变量**，别的判据若在某些臂里改过（即使 monkeypatch 会回滚，
    运行期仍有窗口）⇒ 本文件的判据会继承一个"非默认环境"。⇒ fixture 里**显式清掉**这些开关，
    让判据的前置条件是确定的（这才是判据该有的样子：**不依赖 ambient state**）。
    """
    for _k in ("TRINITY_SENSITIVE_SCAN", "TRINITY_SENSITIVE_REDACT",
               "TRINITY_SENSITIVE_REDACT_SCOPE", "TRINITY_SENSITIVE_POLICY",
               "TRINITY_ADAPTER_GUARD", "TRINITY_HIGH_PERSONAL_CONTEXT"):
        monkeypatch.delenv(_k, raising=False)
    assert _reset_all() >= 1, "没能清空任何 sensitive 实例的账本（隔离前提不成立）"
    yield
    _reset_all()


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_STORAGE_BACKEND", "sqlite")
    from trinity.core.client import Trinity
    return Trinity(store_path=str(tmp_path), adapter="sqlite", evolution_enabled=False)


def _q(db, sql):
    con = sqlite3.connect(str(db))
    try:
        return con.execute(sql).fetchone()
    finally:
        con.close()


# ── 判据 ──────────────────────────────────────────────────────────────
def c1_every_decision_has_a_record(tmp_path=None, monkeypatch=None) -> bool:
    """① 每条 high/medium/PII/降级 判定都能查到**与之对应**的记录，且字段齐。"""
    for text in (HIGH_TEXT, MEDIUM_TEXT, PII_ONLY, DOWNGRADED):
        before = len(_all_log())                               # 全部实例（limit<=0 ⇒ 全部）
        _live().scan_sensitive(text)
        after = _all_log()
        if len(after) <= before:
            return False
        rec = after[0]
        if not all(k in rec for k in REQUIRED_KEYS):
            return False
        if not rec["rule_ids"] or rec["policy_version"] != _live().SENSITIVE_POLICY_VERSION:
            return False
        if not rec["switches"] or "redact" not in rec["switches"]:
            return False
        if rec["tier"] not in ("store", "redact", "refuse", "quarantine"):
            return False
    return True


def c2_no_decision_no_record(tmp_path=None, monkeypatch=None) -> bool:
    """② 反事实（防恒真）：纯良性文本、空文本 ⇒ **不得**产生记录。"""
    _live().scan_sensitive(BENIGN)
    _live().scan_sensitive("")
    _live().scan_sensitive("   ")
    return len(_all_log()) == 0


def c3_record_matches_actual_action(tmp_path, monkeypatch) -> bool:
    """③ **动作与实际一致（不说假话）** —— 三种情形各验一次。"""
    # (a) medium + 开关默认 on ⇒ 记录 tier=redact，且落库确实带掩码账本
    cli = _client(tmp_path / "a", monkeypatch)
    res = cli.ingest(MEDIUM_TEXT, agent_id=AGENT, postprocess=False)
    if not _all_log(action="redact"):
        return False
    db = tmp_path / "a" / "trinity_store.db"
    row = _q(db, "SELECT metadata FROM memories WHERE memory_id='%s'" % res["memory_id"])
    meta = json.loads(row[0] or "{}") if row and row[0] else {}
    if "pii_redaction" not in meta:
        return False

    # (b) 关掉掩码开关 ⇒ 记录必须说 store，且落库**逐字等于原文**
    monkeypatch.setenv("TRINITY_SENSITIVE_REDACT", "0")
    _live().reset_decision_log()
    cli2 = _client(tmp_path / "b", monkeypatch)
    res2 = cli2.ingest(MEDIUM_TEXT, agent_id=AGENT, postprocess=False)
    recs2 = _all_log()
    if not recs2 or recs2[0]["tier"] != "store":
        return False
    db2 = tmp_path / "b" / "trinity_store.db"
    from trinity.security.crypto import decrypt_content
    got = _q(db2, "SELECT content FROM memories WHERE memory_id='%s'" % res2["memory_id"])[0]
    if isinstance(got, str) and got.startswith("enc:v1:"):
        got = decrypt_content(got)
    if got != MEDIUM_TEXT:
        return False
    monkeypatch.delenv("TRINITY_SENSITIVE_REDACT", raising=False)

    # (c) high ⇒ 记录 tier=refuse，且 ingest 报 policy_refused、库里 0 行、审计有 POLICY_PURGE
    _live().reset_decision_log()
    cli3 = _client(tmp_path / "c", monkeypatch)
    res3 = cli3.ingest(HIGH_TEXT, agent_id=AGENT, postprocess=False)
    recs3 = _all_log(action="refuse")
    if not recs3 or recs3[0]["tier"] != "refuse":
        return False
    if res3.get("error") != "policy_refused_sensitive" or res3.get("memory_id", "") != "":
        return False
    db3 = tmp_path / "c" / "trinity_store.db"
    if int(_q(db3, "SELECT count(*) FROM memories")[0]) != 0:
        return False
    return int(_q(db3, "SELECT count(*) FROM audit_log WHERE action='POLICY_PURGE'")[0]) >= 1


def c4_queryable(tmp_path=None, monkeypatch=None) -> bool:
    """④ **可机器查询**：过滤器与聚合统计必须与记录一致（"记录了但没人能查"= 判死）。"""
    _live().scan_sensitive(HIGH_TEXT)
    _live().scan_sensitive(MEDIUM_TEXT)
    _live().scan_sensitive(PII_ONLY)
    all_recs = _all_log()
    if len(all_recs) != 3:
        return False
    if len(_all_log(action="refuse")) != 1:
        return False
    if len(_all_log(action="redact")) != 2:
        return False
    if len(_all_log(category="psych_health")) != 1:
        return False
    # 用**只出现过一次**的规则 ID 验过滤（PII 手机号在两条记录里都有 ⇒ 不能用它）
    refuse_recs = _all_log(action="refuse")
    rid = [x for x in refuse_recs[0]["rule_ids"] if x][0]
    if len(_all_log(rule_id=rid)) != 1:
        return False
    if len(_live().decision_log(limit=2)) != 2:                  # limit 真的生效
        return False
    stats = _live().decision_log_stats()
    if int(stats.get("total", -1)) != 3 or not stats.get("by_rule_id"):
        return False
    if stats.get("by_tier", {}).get("refuse") != 1:
        return False
    return str(stats.get("schema") or "").startswith("trinity.sensitive.decision")


def c5_adapter_path_also_records(tmp_path, monkeypatch) -> bool:
    """⑤ **适配器路径也留痕**（现状 `_crud.py:191-195` 拒存直接 return，**无痕**）。"""
    from trinity.adapters.sqlite import SQLiteAdapter
    _live().reset_decision_log()
    ad = SQLiteAdapter(db_path=str(tmp_path / "adapter.db"))
    ad.connect()
    try:
        res = ad.store_memory(content=HIGH_TEXT, persona_id="p1", agent_id=AGENT, metadata={})
    finally:
        ad.disconnect()
    if res.get("memory_id"):
        return False                        # 不该落库
    recs = _all_log(action="refuse")
    return bool(recs) and recs[0]["tier"] == "refuse"


CRITERIA = {
    "C1_判定必有记录": c1_every_decision_has_a_record,
    "C2_无判定无记录": c2_no_decision_no_record,
    "C3_动作一致": c3_record_matches_actual_action,
    "C4_可查询": c4_queryable,
    "C5_适配器路径留痕": c5_adapter_path_also_records,
}


@pytest.mark.parametrize("name", sorted(CRITERIA), ids=sorted(CRITERIA))
def test_criteria_pass(name, tmp_path, monkeypatch):
    assert CRITERIA[name](tmp_path, monkeypatch) is True


# ── 牙齿 ──────────────────────────────────────────────────────────────
#: t119/G9R-5：**所有变异体都走 `_patch_policy`（打到所有实例）并返回 `n`** ——
#: 旧写法 `monkeypatch.setattr(_live()/S, …)` 在长进程里只改到一个持有者 ⇒ 变异体 no-op ⇒ 元判据误报。
def _silent_recorder(monkeypatch):
    """M1：把记录写入改回**静默**（no-op）。"""
    return _patch_policy(monkeypatch, "_record_decision", lambda *a, **k: None)


def _always_record(monkeypatch):
    """M2：无判定也记（恒真）。"""
    return _patch_policy(monkeypatch, "_decision_should_record", lambda *a, **k: True)


def _lie_about_tier(monkeypatch):
    """M3：把 `tier` 写死成 redact —— 这就是 t43 抓过的**说假话**形态。"""
    real = _live()._build_decision_record

    def fake(report, **kwargs):
        rec = real(report, **kwargs)
        rec["tier"] = "redact"
        return rec

    return _patch_policy(monkeypatch, "_build_decision_record", fake)


def _filters_dead(monkeypatch):
    """M4：过滤器失效（返回全量，忽略所有过滤条件）。"""
    def unfiltered(limit=50, **k):
        out = []
        for m in _all_sensitive_modules():
            out.extend(list(getattr(m, "_decision_log", []) or []))
        return out

    return _patch_policy(monkeypatch, "decision_log", unfiltered)


def _only_redact_recorded(monkeypatch):
    """M5：只记 redact、不记 refuse（"只给掩码路径加观测"这种真实写法）。"""
    return _patch_policy(monkeypatch, "_decision_should_record",
                         lambda report, **k: (report or {}).get("action") == "redact")


MUTANTS = [
    ("C1_判定必有记录", _silent_recorder),
    ("C2_无判定无记录", _always_record),
    ("C3_动作一致", _lie_about_tier),
    ("C4_可查询", _filters_dead),
    ("C5_适配器路径留痕", _only_redact_recorded),
]


@pytest.mark.parametrize("name,apply_mutant", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_each_criterion_has_a_killing_mutant(name, apply_mutant, tmp_path, monkeypatch):
    n_patched = apply_mutant(monkeypatch)
    assert n_patched is None or n_patched >= 1, (
        "变异体没 patch 到任何持有者（n=%s）⇒ 会变成 no-op；这是 t119 修的钝化形态" % n_patched)
    sub = tmp_path / name
    sub.mkdir()
    caught = ""
    try:
        got = CRITERIA[name](sub, monkeypatch)
    except Exception as e:                       # noqa: BLE001 —— 异常=红
        got, caught = False, "%s: %s" % (type(e).__name__, str(e)[:90])
    assert got is False, "变异体 %s 没有杀掉判据 %s ⇒ 该判据没有判别力" % (name, name)
    if caught:
        print("[teeth] %s 的变异体被拦下：%s" % (name, caught))


def test_t119_teeth_single_instance_setattr_is_blinded_but_patch_policy_bites(tmp_path, monkeypatch):
    """⭐ t119 牙齿：**只 patch 一个持有者**会钝化变异体，`_patch_policy` 不会。

    构造（确定性、不依赖跨文件顺序）：人为 `del sys.modules[…sensitive]` 后重导 ⇒ 进程里出现**第二个实例**。
      · 旧手法：把变异体 `setattr` 到**收集期绑定的 `S`**（此时已不是活实例）⇒ 判据 `C4` 读的是活实例
        ⇒ **变异体看不到** ⇒ `C4` 仍通过（这就是"变异体钝化"的可观测形态）；
      · 新手法：`_patch_policy()` 打到**所有**实例 ⇒ `C4` 必须被咬死（`got is False`）。
    ⚠️ 这条判据本身**就是**修法的承重证明：若有人把 `_patch_policy` 换回单实例 `setattr`，它会**红**。
    """
    import importlib
    import types  # noqa: F401  —— 保持与 `_all_sensitive_modules` 相同的导入面
    old = S
    sys.modules.pop("trinity.security.sensitive", None)
    new = importlib.import_module("trinity.security.sensitive")
    try:
        assert new is not old, "没能造出第二个实例 ⇒ 本牙齿无判别力（**不得**静默通过）"

        def unfiltered(limit=50, **k):
            out = []
            for m in _all_sensitive_modules():
                out.extend(list(getattr(m, "_decision_log", []) or []))
            return out

        # ① 旧手法：单实例 setattr（打在"收集期绑定"的那个上）
        with pytest.MonkeyPatch.context() as mp_legacy:
            mp_legacy.setattr(old, "decision_log", unfiltered, raising=False)
            got_legacy = CRITERIA["C4_可查询"](tmp_path / "legacy", monkeypatch)
        # ② 新手法：全实例 patch
        n = _patch_policy(monkeypatch, "decision_log", unfiltered)
        got_new = CRITERIA["C4_可查询"](tmp_path / "patched", monkeypatch)
    finally:
        sys.modules["trinity.security.sensitive"] = old
        _reset_all()
    assert got_legacy is True, (
        "旧手法竟然也咬到了（got=%r）⇒ 牙齿前提不成立：请换一个『活实例 不等于 收集期绑定』的构造"
        % got_legacy)
    assert n >= 1, "全实例 patch 没打到任何持有者（n=%d）" % n
    assert got_new is False, "全实例 patch 必须咬死 C4（got=%r, n=%d）" % (got_new, n)
