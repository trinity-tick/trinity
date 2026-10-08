# -*- coding: utf-8 -*-
"""T43 / G2：「**有 PII 即掩码**」的判据 + **逐条负向（变异体必杀）**（2026-10-06）。

## 背景（G1 闸门 + 队长裁定）
G1（t42）实测：掩码只挂在 `sensitive_report["flagged"]`（**敏感类别**）分支内 ⇒
**纯 PII 文本原文落库**。队长裁定把范围扩到"**有 PII 即掩码**"（D3 采用**校验门**、
D2 不纳入 IP、D1 掩码保留前 3 去尾号、D4 加 `metadata["pii_redaction"]`）。

## 本文件的判据与"牙齿"
每条判据都写成**可独立复核的函数**，并在 `test_每条判据都有能杀掉它的变异体` 里
**人为破坏实现**（monkeypatch）后断言该判据**必须变红** —— 即"判据有判别力"是实测的：

| 判据 | 变异体（人为破坏） |
|---|---|
| C1 纯 PII ⇒ 落库已掩码 | `scan_pii` 恒不命中 |
| C2 无 PII ⇒ 逐字不变 | 让 `scan_pii` 恒命中 |
| C3 类别命中 ⇒ 仍掩码且带 `sensitive_scan` | 让 `scan_sensitive` 永不命中 |
| C4 high ⇒ 仍拒存（不得变"掩码后存"） | `policy_action` 把 high 降级为 redact |
| C5 `TRINITY_SENSITIVE_REDACT=0` ⇒ 完全不掩码 | `sensitive_redact_enabled` 恒 True |
| C6 `metadata["pii_redaction"]` 可审计 | 把 policy 写成错误值（`_ingestion` 的 cause 判据） |
| C7 校验门：Luhn 不通过的 16-19 位数字**不掩码** | `_luhn_ok` 恒 True |
| C8 D1 格式：**尾号不得保留** | 掩码器换回 `_mask_keep(3, 4)`（保留尾 4 位） |

跑法：``python -m pytest tests/unit/test_sensitive_pii_scope_20261006.py -q``
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import trinity.security.sensitive as S  # noqa: E402

AGENT = "t43-pii-scope"
#: monkeypatch 失败时**不静默**（t85/B1：此前的 `except: pass` 会让"没改到任何东西"看起来像成功）
_patch_errors: list = []
PURE_PII = "发货联系人手机 13812345678，邮箱 zhangsan@corp.cn，卡号 4111111111111111。"
BM_ID = "https://www.bilibili.com/opus/443715248901563958"
CLEAN = "用户偏好暗色模式，使用 VS Code 与 Python，关注 WMS 拣货效率。"
CATEGORY_TEXT = "最近有点抑郁，联系方式 13812345678。"
HIGH_TEXT = "我想自杀，活着太累了。联系人 13812345678。"


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_STORAGE_BACKEND", "sqlite")
    from trinity.core.client import Trinity
    return Trinity(store_path=str(tmp_path), adapter="sqlite", evolution_enabled=False)


def _stored(tmp_path, memory_id):
    from trinity.adapters.sqlite import SQLiteAdapter
    ad = SQLiteAdapter(db_path=str(tmp_path / "trinity_store.db"))
    ad.connect()
    try:
        return ad.get_memory(memory_id) or {}
    finally:
        ad.disconnect()


# ── 判据（每条返回 bool，便于变异体复用同一份逻辑）────────────────────
def c1_pure_pii_is_masked(tmp_path, monkeypatch) -> bool:
    cli = _client(tmp_path, monkeypatch)
    mid = cli.ingest(PURE_PII, agent_id=AGENT, postprocess=False).get("memory_id")
    got = _stored(tmp_path, mid).get("content", "")
    return ("13812345678" not in got and "4111111111111111" not in got
            and "zhangsan@corp.cn" not in got and "138********" in got)


def c2_clean_text_is_verbatim(tmp_path, monkeypatch) -> bool:
    cli = _client(tmp_path, monkeypatch)
    mid = cli.ingest(CLEAN, agent_id=AGENT, postprocess=False).get("memory_id")
    return _stored(tmp_path, mid).get("content", "") == CLEAN


def c3_category_text_masked_with_marker(tmp_path, monkeypatch) -> bool:
    cli = _client(tmp_path, monkeypatch)
    mid = cli.ingest(CATEGORY_TEXT, agent_id=AGENT, postprocess=False).get("memory_id")
    row = _stored(tmp_path, mid)
    meta = row.get("metadata") or {}
    if isinstance(meta, str):
        import json
        meta = json.loads(meta or "{}")
    return ("138********" in str(row.get("content", ""))
            and (meta.get("sensitive_scan") or {}).get("severity") == "medium"
            and "psych_health" in ((meta.get("sensitive_scan") or {}).get("categories") or []))


def c4_high_still_refused(tmp_path, monkeypatch) -> bool:
    cli = _client(tmp_path, monkeypatch)
    res = cli.ingest(HIGH_TEXT, agent_id=AGENT, postprocess=False)
    if res.get("error") != "policy_refused_sensitive" or res.get("memory_id", "") != "":
        return False
    # 且**没有任何行**被写入（不得退化成"掩码后存"）
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "trinity_store.db"))
    n = con.execute("SELECT count(*) FROM memories").fetchone()[0]
    con.close()
    return n == 0


def c5_rollback_is_verbatim(tmp_path, monkeypatch) -> bool:
    monkeypatch.setenv("TRINITY_SENSITIVE_REDACT", "0")
    cli = _client(tmp_path, monkeypatch)
    mid = cli.ingest(PURE_PII, agent_id=AGENT, postprocess=False).get("memory_id")
    return _stored(tmp_path, mid).get("content", "") == PURE_PII


def c6_metadata_is_auditable(tmp_path, monkeypatch) -> bool:
    import json
    cli = _client(tmp_path, monkeypatch)
    # ① 纯 PII（未命中类别）⇒ policy=all_pii
    mid1 = cli.ingest(PURE_PII, agent_id=AGENT, postprocess=False).get("memory_id")
    # ② 类别 + PII ⇒ policy=category
    mid2 = cli.ingest(CATEGORY_TEXT, agent_id=AGENT, postprocess=False).get("memory_id")
    m1 = _stored(tmp_path, mid1).get("metadata") or {}
    m2 = _stored(tmp_path, mid2).get("metadata") or {}
    for m in (m1, m2):
        if isinstance(m, str):
            m = json.loads(m or "{}")
    m1 = json.loads(m1) if isinstance(m1, str) else m1
    m2 = json.loads(m2) if isinstance(m2, str) else m2
    a, b = m1.get("pii_redaction") or {}, m2.get("pii_redaction") or {}
    return (a.get("policy") == "all_pii" and b.get("policy") == "category"
            and a.get("count", 0) >= 1 and a.get("scanner") == "regex_v1" and a.get("ts")
            and any("手机号" in k for k in a.get("kinds", [])))


def c7_gate_rejects_non_card_digits(tmp_path, monkeypatch) -> bool:
    """D3 校验门：Luhn 不通过的 18 位数字（B 站 opus ID）**不得**被当卡号掩码。"""
    cli = _client(tmp_path, monkeypatch)
    mid = cli.ingest(BM_ID, agent_id=AGENT, postprocess=False).get("memory_id")
    got = _stored(tmp_path, mid).get("content", "")
    return got == BM_ID


def c8_no_tail_retention() -> bool:
    """D1：掩码**不得保留尾号**（手机尾 4 / 身份证尾 2 / 卡号尾 4 / 邮箱域名）。"""
    out, _labels = S.redact_identifiers(
        "手机 13812345678，身份证 110101199003071233，卡号 4111111111111111，"
        "邮箱 zhangsan@corp.cn。")
    return ("5678" not in out and "1233" not in out and "1111" not in out
            and "corp" not in out and "138********" in out)


# ── G3-R3：邮箱门的"有意排除"必须被登记 + 钉住 ─────────────────────────
def c9_placeholder_domains_intentionally_unmasked(tmp_path=None, monkeypatch=None) -> bool:
    """占位域/保留名 **有意不掩**（RFC 2606/6761 + 内网名）—— G3-R3 登记。

    反事实的另一半：同一段文本把域换成真实域 ⇒ **必掩**（见 C10）。
    """
    # ① 排除清单必须是**具名常量**（后来人能看到"这是设计"而不是"漏了"）
    if not (S._EMAIL_RESERVED_NAMES and S._EMAIL_PLACEHOLDER_DOMAINS):
        return False
    # ② 保留域与"保留 TLD + 子域"都不得被掩
    for dom in ("example.com", "example.org", "example.net",
                "foo.test", "bar.invalid", "baz.example", "qux.local", "localhost"):
        text = "邮箱 zhangsan@%s 请勿外传" % dom
        out, labels = S.redact_identifiers(text)
        if out != text or labels:
            return False
    return True


def c10_real_domains_must_be_masked(tmp_path=None, monkeypatch=None) -> bool:
    """真实域**必掩**（同一条流程，只有域不同）—— 防止"排除清单"扩成"什么都不掩"。"""
    for dom in ("qq.com", "163.com", "gmail.com", "outlook.com", "corp.cn", "wangdian.cn"):
        text = "邮箱 zhangsan@%s 请勿外传" % dom
        out, labels = S.redact_identifiers(text)
        if out == text or "z***@***." not in out or not labels:
            return False
    return True


CRITERIA = {
    "C1_纯PII落库已掩码": c1_pure_pii_is_masked,
    "C2_无PII逐字不变": c2_clean_text_is_verbatim,
    "C3_类别仍掩码且带标记": c3_category_text_masked_with_marker,
    "C4_high仍拒存": c4_high_still_refused,
    "C5_回滚逐字": c5_rollback_is_verbatim,
    "C6_元数据可审计": c6_metadata_is_auditable,
    "C7_校验门拒非卡号": c7_gate_rejects_non_card_digits,
    "C9_占位域有意不掩": c9_placeholder_domains_intentionally_unmasked,
    "C10_真实域必掩": c10_real_domains_must_be_masked,
}


# ── 正例：全部判据通过 ────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(CRITERIA), ids=sorted(CRITERIA))
def test_判据通过(name, tmp_path, monkeypatch):
    assert CRITERIA[name](tmp_path, monkeypatch) is True


def test_C8_格式不保留尾号():
    assert c8_no_tail_retention() is True


# ── 负向（牙齿）：每个变异体必须杀掉对应判据 ──────────────────────────
def _no_pii(_text, **_kw):
    return {"flagged": False, "kinds": [], "hits": [], "count": 0}


def _all_pii(text, **_kw):
    return {"flagged": True, "kinds": ["手机号"], "hits": [{"kind": "手机号", "match": "x"}],
            "count": 1}


def _never_flagged(_content):
    return {"flagged": False, "severity": None, "policy": None, "action": S.ACTION_STORE,
            "categories": [], "hits": [], "truncated": False, "downgraded": False,
            "pii": _no_pii("")}


def _high_downgraded(_content):
    """把 high 降级成"有 PII 即掩码"的报告（用来模拟"high 被降级为掩码后存"）。"""
    return {"flagged": False, "severity": None, "policy": None, "action": S.ACTION_REDACT,
            "categories": [], "hits": [], "truncated": False, "downgraded": False,
            "pii": _all_pii("")}


def _mask_everything(_text, **_kw):
    return "***（被变异体整体掩码）", ["一切×1"]


def _luhn_always_true_rules():
    return [(k, p, r, (lambda _s: True) if k == "银行卡号" else v)
            for (k, p, r, v) in S._PII_RULES]


def _email_validator_rules(fn):
    """把**邮箱规则**的校验器换掉（必须改规则表：`_PII_RULES` 在导入时已捕获函数对象，
    直接 patch `S._email_ok` 对 `redact_identifiers` **无效** —— 这是本文件第二次踩同一坑）。"""
    return [(k, p, r, fn if k == "邮箱" else v) for (k, p, r, v) in S._PII_RULES]


def _c2_mutant(mp):
    """C2 的变异体必须是**组合**：只改「决定」或只改「掩码器」都改写不了干净文本。

    实测教训（本文件第一版就踩了）：把 `scan_pii` 改成恒命中 ⇒ 干净文本仍走
    `redact_identifiers` 而它找不到 PII ⇒ 逐字不变 ⇒ 判据不红。C2 守的是
    「**决定要掩** + **掩码器会改写**」这一对，故变异体也必须成对施加。
    """
    return (_patch_policy(mp, "scan_sensitive", _high_downgraded)        # 一律判 redact
            + _patch_policy(mp, "redact_identifiers", _mask_everything))  # 且掩码器改写任何文本



# ── t93/C1：变异体必须"patch 到底"（否则在长进程里是 no-op）────────────────────
def _all_sensitive_modules():
    """**所有** `trinity.security.sensitive` 实例 —— 含运行时被重新导入出来的**重复实例**。

    实测（`evidence/t93_dup_module_probe.py`）：进程里存在第二个实例时，写路径读到的是那一个，
    只 patch 收集期绑定的 `S` 就完全不起作用 ⇒ 变异体变 no-op、元判据误报"判据没有判别力"。
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
    except Exception as _e:                     # noqa: BLE001 —— 不静默（t85/B1）
        _patch_errors.append("gc scan: %r" % (_e,))
    if S not in cand:
        cand.append(S)
    for m in cand:
        if id(m) not in seen:
            seen.add(id(m))
            out.append(m)
    return out


def _patch_policy(mp, name, new, inplace=False):
    """把 `name` 替换成 `new`，覆盖**所有**持有者；返回被替换的持有者数量。

    返回 0 表示"没改到任何东西" ⇒ 元判据会**直接红**（而不是静默通过）。
    """
    n = 0
    orig = None
    for m in _all_sensitive_modules():
        if getattr(m, "__name__", "") == "trinity.security.sensitive" and hasattr(m, name):
            orig = getattr(m, name)
            break
    # t100/C1R：`inplace=True` 时**原地改容器**（list/dict）——"已经持有该对象"的消费者也能看到
    if inplace and isinstance(orig, (list, dict)):
        try:
            if isinstance(orig, list):
                orig[:] = list(new)
            else:
                orig.clear()
                orig.update(dict(new))
            n = 1
        except Exception as _e:  # noqa: BLE001 —— 不静默（t85/B1）；t162/G19 去掉最后一处静默 pass
            _patch_errors.append("inplace 容器改写: %r" % (_e,))
    targets = list(_all_sensitive_modules())
    for m in targets:
        try:
            if hasattr(m, name):
                mp.setattr(m, name, new, raising=False)
                n += 1
        except Exception as _e:                 # noqa: BLE001 —— 不静默（t85/B1）
            _patch_errors.append("setattr %s@%s: %r" % (name, getattr(m, "__name__", "?"), _e))
    try:
        for mod in list(sys.modules.values()):
            if mod is None or mod in targets:
                continue
            try:
                if orig is not None and getattr(mod, name, None) is orig:
                    mp.setattr(mod, name, new, raising=False)
                    n += 1
            except Exception as _e:             # noqa: BLE001 —— 不静默（t85/B1）
                _patch_errors.append("setattr %s@%s: %r" % (name, getattr(mod, "__name__", "?"), _e))
    except Exception as _e:                     # noqa: BLE001 —— 不静默（t85/B1）
        _patch_errors.append("module sweep: %r" % (_e,))
    return n


MUTANTS = [
    # C1 的变异体：PII 检测整体失效 ⇒ 纯 PII 不再掩码
    ("C1_纯PII落库已掩码", lambda mp: _patch_policy(mp, "scan_pii", _no_pii)),
    # C2 的变异体：组合变异（见 _c2_mutant 的说明）
    ("C2_无PII逐字不变", _c2_mutant),
    # C3 的变异体：类别检测失效 ⇒ 类别文本不再掩码、metadata 也没了
    ("C3_类别仍掩码且带标记", lambda mp: _patch_policy(mp, "scan_sensitive", _never_flagged)),
    # C4 的变异体：把 high 降级成"有 PII 即掩码"（此时拒存分支不会再触发）
    ("C4_high仍拒存", lambda mp: _patch_policy(mp, "scan_sensitive", _high_downgraded)),
    # C5 的变异体：回滚开关恒为开 ⇒ 关不掉了
    ("C5_回滚逐字", lambda mp: _patch_policy(mp, "sensitive_redact_enabled", lambda: True)),
    # C6 的变异体：范围退回 category ⇒ 纯 PII 不掩码 ⇒ 没有 all_pii 标记
    ("C6_元数据可审计", lambda mp: _patch_policy(mp, "sensitive_redact_scope", lambda: "category")),
    # C7 的变异体：校验门恒通过 ⇒ B 站 ID 也会被当卡号掩码
    ("C7_校验门拒非卡号",
     lambda mp: _patch_policy(mp, "_PII_RULES", _luhn_always_true_rules(),
                                     inplace=True)),
    # C9 的变异体：把"占位域有意排除"整条去掉（校验器恒通过）⇒ 占位域会被掩 ⇒ C9 必红
    ("C9_占位域有意不掩",
     lambda mp: mp.setattr(S, "_PII_RULES", _email_validator_rules(lambda _s: True))),
    # C10 的变异体：邮箱校验器恒拒绝（"什么都不掩"）⇒ 真实域也不掩 ⇒ C10 必红
    ("C10_真实域必掩",
     lambda mp: mp.setattr(S, "_PII_RULES", _email_validator_rules(lambda _s: False))),
]


@pytest.mark.parametrize("name,apply_mutant", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_每条判据都有能杀掉它的变异体(name, apply_mutant, tmp_path, monkeypatch):
    crit = CRITERIA[name]
    n_patched = apply_mutant(monkeypatch)
    # t93：变异体必须**确实施加到了持有者**（否则"杀不掉"是伪结论，不得静默通过）
    if n_patched is not None:
        assert n_patched >= 1, (
            "变异体 %s 没有改到任何持有者 ⇒ 结论不可信（不是判据没有判别力）" % name)
    # 用 tmp_path 的子目录（**不要**用 TemporaryDirectory：SQLite 句柄未关，
    # Windows 上清理会抛 WinError 32，那会把"变异体杀判据"的结论污染成环境错误）
    sub = tmp_path / name
    sub.mkdir()
    assert crit(sub, monkeypatch) is False, (
        "变异体 %s 没有杀掉判据 %s ⇒ 该判据没有判别力" % (name, name))


def test_C8_的变异体必杀(monkeypatch):
    """把掩码器换回"保留尾 4 位"（G2 之前的格式）⇒ C8 必须红。"""
    broken = [(k, p, (S._mask_keep(3, 4) if k in ("手机号", "银行卡号") else r), v)
              for (k, p, r, v) in S._PII_RULES]
    monkeypatch.setattr(S, "_PII_RULES", broken)
    assert c8_no_tail_retention() is False, "格式变异体没杀掉 C8 ⇒ 该判据无判别力"


# ── t93/C1：**变异体的深度**必须覆盖"重复模块实例"（否则在长进程里是 no-op）──────
def _dup_sensitive_instance():
    """造出**第二个** `trinity.security.sensitive` 实例（模拟全量里被更早的测试搞出的进程状态）。"""
    import importlib
    saved = sys.modules.get("trinity.security.sensitive")
    sys.modules.pop("trinity.security.sensitive", None)
    try:
        dup = importlib.import_module("trinity.security.sensitive")
    finally:
        if saved is not None:
            sys.modules["trinity.security.sensitive"] = saved
    return dup


def test_重复实例下变异体仍必须生效_t93(tmp_path):
    """复现全量里的 no-op 条件 ⇒ ①**旧做法**（只 patch 收集期的 `S`）是 no-op（独立证据）；
    ②`_patch_policy` 的**深度 patch** 在该条件下仍然生效（本轮的修）。"""
    dup = _dup_sensitive_instance()
    if dup is S:
        pytest.skip("本进程内没能造出第二个 instancesensitive 实例（不可复现）")

    # ① 旧做法：只改收集期绑定的 `S` ⇒ 写路径读到的是另一个实例 ⇒ 判据**仍然通过**（no-op）
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(S, "scan_pii", _no_pii)
        d1 = tmp_path / "old_style"
        d1.mkdir()
        old = CRITERIA["C1_纯PII落库已掩码"](d1, mp)
    # ② 本轮的修：patch 所有实例 ⇒ 判据必须被**杀掉**
    with pytest.MonkeyPatch.context() as mp:
        n = _patch_policy(mp, "scan_pii", _no_pii)
        d2 = tmp_path / "deep"
        d2.mkdir()
        deep = CRITERIA["C1_纯PII落库已掩码"](d2, mp)
    assert n >= 1, "深度 patch 没改到任何持有者：%d" % n
    assert old is True, (
        "旧做法在该条件下**本应是 no-op**（判据仍通过）—— 这是全量 16 条红的机制；"
        "若这里已是 False，说明本进程没有复现出重复实例条件")
    assert deep is False, "深度 patch 后 C1 **本应被杀死**（判据失败）"


@pytest.mark.parametrize("name", ["C1_纯PII落库已掩码", "C5_回滚逐字"])
def test_牙齿_noop变异体必须让元判据红_t93(name, tmp_path, monkeypatch):
    """牙齿：把变异体换成**不改变被测行为**的 no-op ⇒ 元判据的断言**必须失败**。

    这同时证明判据**不是"只会红"**（no-op 下它照样通过）。
    """
    crit = CRITERIA[name]
    monkeypatch.setattr(S, "_t93_noop_probe", object(), raising=False)   # 与被测行为无关
    sub = tmp_path / ("noop_" + name)
    sub.mkdir()
    assert crit(sub, monkeypatch) is True, "no-op 下判据竟然失败 ⇒ 前提失效"
    with pytest.raises(AssertionError):
        assert crit(sub, monkeypatch) is False, (
            "no-op 变异体没有杀掉判据 %s ⇒ 元素判据本应红" % name)
