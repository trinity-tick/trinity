# -*- coding: utf-8 -*-
"""t48/G7 判据：**适配器写入边界**的 PII 守卫（下沉层）。

被覆盖的缺陷：客户端层（`_ingestion.py`）只覆盖「经客户端」的写入；**直写适配器**的路径
（daemon / memory 抽取与巩固 / brain / evolution / vms / pipeline —— G1 §5 第 8–11 条）
不过客户端，PII 原文落库。本判据全部在**临时 SQLite** 上、直接调 `adapter.store_memory`
（= 那些路径的写法），**不经过 client 层**。

每条判据都有能杀掉它的变异体（见 `REDACT-ADAPTER-GUARD.md` §5）。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "trinity" / "adapters" / "_pii_guard.py"
CRUD = REPO / "trinity" / "adapters" / "sqlite" / "_crud.py"

PII = "联系人 13800138000，卡号 4111111111111111。"
NO_PII = "今天读了一本书，讲分布式一致性，收获不小。"
HIGH = "我想自杀，准备买安眠药。"


def _adapter(tmp_path, name="t48.db"):
    from trinity.adapters.sqlite import SQLiteAdapter

    ad = SQLiteAdapter(db_path=str(tmp_path / name))
    ad.connect()
    return ad


def _write(ad, content: str, **kw):
    return ad.store_memory(content=content, agent_id="t48", category="general",
                           persona_id="default", **kw)


def _stored(ad, mid: str):
    return (ad.get_memory(mid) or {}) if mid else {}


# ── ① adapter 直写含 PII ⇒ 掩码（这是 G7 的核心：直写路径也掩）────────────────
def test_adapter直写含PII必须掩码_t48(tmp_path):
    ad = _adapter(tmp_path)
    res = _write(ad, PII)
    mid = res.get("memory_id") or ""
    assert mid, "前提失效：直写没落行：%r" % (res,)
    row = _stored(ad, mid)
    stored = row.get("content") or ""
    assert stored != PII, "直写含 PII 却**原文落库**（= G1 §5 第 8–11 条的缺陷）"
    assert "13800138000" not in stored and "4111111111111111" not in stored, (
        "号码/卡号仍在库里：%r" % stored)
    assert res.get("auto_redacted") is True, "响应也必须如实（G3 的账本联动）：%r" % (res,)
    assert res.get("redaction_source") == "adapter", (
        "来源必须标成 adapter（而不是 ingestion）：%r" % res.get("redaction_source"))


# ── ② 反事实：adapter 直写无 PII ⇒ 逐字不变 ─────────────────────────────────
def test_adapter直写无PII必须逐字不变_反事实_t48(tmp_path):
    ad = _adapter(tmp_path)
    res = _write(ad, NO_PII)
    mid = res.get("memory_id") or ""
    row = _stored(ad, mid)
    stored = (row.get("content")) or ""
    assert stored == NO_PII, "无 PII 的文本被改写了（守卫越权）：%r" % stored
    assert res.get("auto_redacted") is False, "无 PII 却报脱敏 ⇒ 恒真：%r" % (res,)
    assert res.get("redaction_source") is None
    # **账本也不许写空条目**：变异实测（M2'：无 PII 也写账本）**不会**被前面的断言杀掉
    # （kinds=[] ⇒ auto_redacted 仍为 False），但会给审计留一条"空账本"= 另一种"字段说假话"。
    md = row.get("metadata") or {}
    if isinstance(md, str):
        md = json.loads(md)
    assert not md.get("pii_redaction"), (
        "没掩码却写下了脱敏账本（空账本会误导审计）：%r" % md.get("pii_redaction"))


# ── ③ 回滚：TRINITY_SENSITIVE_REDACT=0 ⇒ 与改动前一致（**复用 G2 同一开关**）──
def test_回滚档复用G2同一开关_逐字不变_t48(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_SENSITIVE_REDACT", "0")
    ad = _adapter(tmp_path)
    res = _write(ad, PII)
    stored = (_stored(ad, res.get("memory_id") or "").get("content")) or ""
    assert stored == PII, "回滚档必须逐字等于输入（= 改动前行为）：%r" % stored
    assert res.get("auto_redacted") is False


# ── ④ 本边界专用退出（评测语料 / 镜像回填「本就不该掩」）─────────────────────
def test_边界可显式退出_供评测与镜像_t48(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_ADAPTER_GUARD", "0")
    ad = _adapter(tmp_path)
    res = _write(ad, PII)
    stored = (_stored(ad, res.get("memory_id") or "").get("content")) or ""
    assert stored == PII, "TRINITY_ADAPTER_GUARD=0 时应完全不介入：%r" % stored


# ── ⑤ high 档不得因下沉而改变语义：单条通道 **拒存** ────────────────────────
def test_high档仍然拒存_t48(tmp_path):
    ad = _adapter(tmp_path)
    res = _write(ad, HIGH)
    assert res.get("error"), "high 档必须拒存（返回 error），实测：%r" % (res,)
    assert not res.get("memory_id"), "high 档竟然落了行：%r" % (res,)
    assert "refused" in str(res.get("error")), "拒存原因要可读：%r" % res.get("error")


# ── ⑥ 幂等：客户端已掩过 ⇒ 守卫放行，不重扫、不覆盖来源标签 ─────────────────
def test_客户端已掩时守卫放行且来源仍为ingestion_t48(tmp_path, monkeypatch):
    from trinity import Trinity

    db = str(tmp_path / "t48_idem.db")
    mem = Trinity(adapter="sqlite", store_path=db)
    res = mem.ingest(content="我最近很抑郁，电话 13800138000。", agent_id="t48",
                     category="general")
    mid = res.get("memory_id") or ""
    row = mem._adapter.get_memory(mid) or {}
    md = row.get("metadata") or {}
    if isinstance(md, str):
        md = json.loads(md)
    book = md.get("pii_redaction") or {}
    assert book.get("scanner") == "regex_v1", (
        "客户端路径的账本被守卫覆盖了（scanner 应仍是客户端的 regex_v1）：%r" % book)
    assert "adapter_guard" not in str(book.get("scanner")), "守卫不该在客户端已掩时再动手"
    assert res.get("redaction_source") == "ingestion", (
        "来源标签必须是 ingestion：%r" % res.get("redaction_source"))


# ── ⑦ 结构性牙齿：守卫**只转接**，不得自带第二套策略 ────────────────────────
def test_守卫不得自带第二套策略_t48():
    src = GUARD.read_text(encoding="utf-8")
    assert "trinity.security" in src and "sensitive" in src, "守卫必须复用 G2 的策略层"
    assert "scan_sensitive" in src and "redact_identifiers" in src, "必须调用 G2 的判定与掩码"
    assert "sensitive_redact_enabled" in src, "回滚必须复用 G2 的同一开关"
    tree = ast.parse(src)
    compiles = [n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "compile"]
    assert not compiles, "守卫里出现了自建正则（第二套策略会漂移）：行 %r" % [n.lineno for n in compiles]
    for lit in ("手机号", "身份证", "银行卡", "邮箱"):
        assert lit not in src, "守卫里出现了类别字面量 %r（策略应只在 security 层）" % lit


# ── ⑧ 覆盖链上可核：直写路径确实走到守卫（不靠推断）────────────────────────
def test_直写路径确实经过守卫_可核_t48(tmp_path, monkeypatch):
    """把守卫的判定函数换成"必须被调用"的探针 ⇒ 断言它**真的被调用过**。"""
    from trinity.adapters import _pii_guard as G

    calls = []
    real = G.adapter_pii_guard

    def spy(content, metadata=None):
        calls.append(len(content or ""))
        return real(content, metadata)

    monkeypatch.setattr(G, "adapter_pii_guard", spy)
    ad = _adapter(tmp_path)
    _write(ad, PII)
    assert calls, "`store_memory` 没有调用守卫（下沉没生效）"
    # 反向：把**守卫调用**从 `_crud.py` 里挖掉 ⇒ 侦测器必须红
    src = CRUD.read_text(encoding="utf-8")
    assert "adapter_pii_guard" in src, "`_crud.py` 里没有守卫调用"
    mutated = src.replace("_pii_g = adapter_pii_guard(content, metadata)",
                          "_pii_g = {}   # 变异体：守卫被挖掉", 1)
    assert mutated != src, "变异体没变 ⇒ 本用例的插入点失效"
    assert "adapter_pii_guard(content, metadata)" not in mutated, (
        "挖掉后仍能看见守卫调用 ⇒ 侦测器无效")


# ── ⑩ t49：**三开关契约**（G7 回归的正面钉子）──────────────────────────────
# 回归来源：t48 的守卫**只看 `TRINITY_SENSITIVE_REDACT`**、不看 `TRINITY_SENSITIVE_SCAN=off`
# ⇒ 开关名「关掉敏感扫描」**名不副实**（high 仍被拒存、内容仍被掩码）。
# 下表把三个开关的语义钉死：名字说了什么，就必须做到什么。
_SWITCH_MATRIX = [
    # (SCAN, REDACT, ADAPTER_GUARD, 期望介入, 期望原因)
    (None, None, None, True, None),                 # 默认：扫描 + 掩码 + 本层开
    (None, "0", None, False, "redact-off"),         # 扫描但不掩码
    ("off", None, None, False, "scan-off"),         # **主开关：完全不扫描**
    ("off", "0", None, False, "scan-off"),          # 组合：不扫描优先
    (None, None, "0", False, "guard-off"),          # 仅本层退出
    ("off", None, "0", False, "guard-off"),         # guard-off 先短路（原因如实）
]


@pytest.mark.parametrize("scan,redact,guard,expect_on,expect_why", _SWITCH_MATRIX)
def test_三开关组合语义表_t49(scan, redact, guard, expect_on, expect_why, monkeypatch):
    """每个开关都必须**名副其实**；原因字符串要能读出（审计"为什么没管"）。"""
    from trinity.adapters import _pii_guard as G

    for k in ("TRINITY_SENSITIVE_SCAN", "TRINITY_SENSITIVE_REDACT", "TRINITY_ADAPTER_GUARD"):
        monkeypatch.delenv(k, raising=False)
    if scan is not None:
        monkeypatch.setenv("TRINITY_SENSITIVE_SCAN", scan)
    if redact is not None:
        monkeypatch.setenv("TRINITY_SENSITIVE_REDACT", redact)
    if guard is not None:
        monkeypatch.setenv("TRINITY_ADAPTER_GUARD", guard)

    on, why = G.adapter_guard_state()
    assert on is expect_on, "开关组合的介入判定不对：SCAN=%r REDACT=%r GUARD=%r ⇒ %r" % (
        scan, redact, guard, (on, why))
    assert (why or None) == expect_why, "原因字符串不对（审计要能读出为什么没管）：%r" % why

    # 行为面必须与判定一致（不只是元数据对）
    pii = "联系人 13800138000。"
    high = "我想自杀，准备买安眠药。"
    _c, _m, info_pii = G.adapter_pii_guard(pii, None)
    _c2, _m2, info_high = G.adapter_pii_guard(high, None)
    if expect_on:
        assert info_pii["redacted"] is True and info_high["refuse"] is True
    else:
        assert info_pii["redacted"] is False, "不介入时仍掩码了：%r" % info_pii
        assert info_high["refuse"] is False, "不介入时仍然拒存 high：%r" % info_high
        assert info_pii["exempt"] == expect_why


def test_SCAN_off时adapter直写完全不干预_含high落库_t49(tmp_path, monkeypatch):
    """`TRINITY_SENSITIVE_SCAN=off` ⇒ **adapter 层也完全不干预**：
    含 PII 的直写**原文落库**、high **不拒存**（有 `memory_id`）。"""
    monkeypatch.setenv("TRINITY_SENSITIVE_SCAN", "off")
    ad = _adapter(tmp_path, "t49_scan_off.db")
    res_pii = _write(ad, PII)
    assert res_pii.get("memory_id"), "SCAN=off 时不该拒存：%r" % (res_pii,)
    stored = (_stored(ad, res_pii["memory_id"]).get("content")) or ""
    assert stored == PII, "SCAN=off（=完全不扫描）却仍掩码了：%r" % stored
    res_high = _write(ad, HIGH)
    assert res_high.get("memory_id"), "SCAN=off 时 high **也不该**被拒存：%r" % (res_high,)
    assert not res_high.get("error"), "SCAN=off 时不该有 error：%r" % res_high.get("error")


def test_主开关判断必须同源G2_变异必须杀死_t49():
    """**变异自证**：把守卫对 `SCAN=off` 的判断挖掉 ⇒ 组合语义表必须变红。

    （防日后简化守卫时把主开关丢掉；同时钉住"**同源读 G2**、不自己解析字符串"。）
    """
    src = GUARD.read_text(encoding="utf-8")
    assert "sensitive_scan_enabled" in src, (
        "守卫必须**同源**读 G2 的 `sensitive_scan_enabled()`（不要自己解析字符串）")
    anchor = ('        if not _s.sensitive_scan_enabled():\n'
              '            return False, "scan-off"')
    assert anchor in src, "锚点与源码不一致 ⇒ 本用例的插入点失效"
    mutated = src.replace(anchor, '        if False:  # 变异体：丢掉主开关\n'
                                  '            return False, "scan-off"', 1)
    assert mutated != src and "not _s.sensitive_scan_enabled()" not in mutated


# ── ⑨ 幂等**契约**（把 §⑦ 那条语义钉死：客户端已标账本 ⇒ 守卫**一个字节都不动**）──
def test_幂等契约_已标账本时守卫不得改写_t48(tmp_path):
    """**为什么要有这条**：变异实测发现"去掉幂等放行"（M4）**不会**被现有判据杀掉
    （重扫一遍掩码后的文本通常什么也找不到）。⇒ 把契约显式写出来，否则它只是个"顺带成立"的性质。

    同时记录**取舍**：这条意味着"客户端自称掩过就放行" —— 若上游**谎报**（标了账本却没掩），
    守卫不会纠正它。之所以接受：① 重扫**并不能**修好上游的漏掩（同一个 `scan_sensitive` 判定，
    G2 的召回空洞如 G3-R3 的邮箱在两侧一样不触发）；② 不重扫才能保证 G2 的计数不双记、
    `redaction_source` 不被覆盖。⇒ 该风险已在报告 §4 登记为 **G7-R2**。
    """
    from trinity.adapters import _pii_guard as G

    content = "联系人 13800138000，卡号 4111111111111111。"
    ledger = {"pii_redaction": {"policy": "all_pii", "kinds": ["手机号×1"],
                              "scanner": "regex_v1", "ts": "2026-10-06T00:00:00+00:00"}}
    out, md, info = G.adapter_pii_guard(content, dict(ledger))
    assert out == content, "已标账本时守卫改写了正文（破坏幂等契约）：%r" % out
    assert md == ledger, "已标账本时守卫改写了 metadata（会覆盖来源标签）：%r" % md
    assert info["exempt"] == "already-recorded", "未按「已记账」放行：%r" % info
    assert info["redacted"] is False and info["labels"] == []
