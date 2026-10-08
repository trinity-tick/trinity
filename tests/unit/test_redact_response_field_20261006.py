# -*- coding: utf-8 -*-
"""t44/G3 判据：写入响应的 `auto_redacted` / `pii_redacted_types` 必须**反映实际发生的脱敏**。

被修缺陷（队长实测 + 本文件复现）：client 层（`_ingestion.py`，G2/t43）确实掩码了，
但适配器响应字段 `auto_redacted=False` / `pii_redacted_types=[]` —— **字段与事实反向**。

判据设计的两个要点：
1. **反事实必须在场**（"无 PII ⇒ 假"）——否则"恒真"也能过；
2. **牙齿是功能性的**：直接以**修前形状**调适配器（不带 ingestion 账本）⇒ 必须为假，
   证明字段是**跟着账本走**的，不是硬编码。
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CRUD = REPO / "trinity" / "adapters" / "sqlite" / "_crud.py"

CATEGORY_PII = "最近很抑郁，一直在做心理治疗；联系电话是 13800138000，邮箱 zhangsan@example.com。"
ONLY_PII = "联系人手机号 13900139000，卡号 4111111111111111。"
NO_PII = "今天读了一本书，讲的是分布式系统的一致性问题，收获不小。"


def _mem(tmp_path):
    from trinity import Trinity
    from trinity.adapters.sqlite import SQLiteAdapter

    db = str(tmp_path / "t44.db")
    ad = SQLiteAdapter(db_path=db)
    ad.connect()
    return Trinity(adapter="sqlite", store_path=db), ad


def _ingest(mem, content: str) -> dict:
    return mem.ingest(content=content, agent_id="t44", category="general") or {}


def _stored(ad, mid: str) -> str:
    try:
        return ((ad.get_memory(mid) or {}).get("content")) or ""
    except Exception:  # noqa: BLE001
        return ""


# ── ① 类别命中 + PII ────────────────────────────────────────────────────────
def test_有PII即字段为真且类别正确_类别命中_t44(tmp_path):
    mem, ad = _mem(tmp_path)
    res = _ingest(mem, CATEGORY_PII)
    stored = _stored(ad, res.get("memory_id", ""))
    assert stored != CATEGORY_PII, "前提失效：入库内容竟然没被掩码"
    assert res.get("auto_redacted") is True, (
        "入库已掩码但响应说没掩（字段说假话）：stored=%r res=%r" % (stored, res))
    assert res.get("pii_redacted_types"), "类别列表为空 ⇒ 没说清掩了哪些类别"
    assert res.get("redaction_source") == "ingestion", (
        "必须能表达**哪套机制掩的**：%r" % res.get("redaction_source"))


# ── ② 纯 PII（G2 之后也应掩码）──────────────────────────────────────────────
def test_纯PII路径字段也为真_t44(tmp_path):
    mem, ad = _mem(tmp_path)
    res = _ingest(mem, ONLY_PII)
    stored = _stored(ad, res.get("memory_id", ""))
    assert stored != ONLY_PII, "前提失效：纯 PII 没被掩码"
    assert res.get("auto_redacted") is True, "纯 PII 被掩了，响应必须为真：%r" % (res,)
    assert any("手机号" in str(k) for k in (res.get("pii_redacted_types") or [])), \
        "类别列表里应出现手机号：%r" % (res.get("pii_redacted_types"),)


# ── ③ 反事实：无 PII ⇒ 必须为假（防"恒真"）──────────────────────────────────
def test_无PII时字段必须为假_反事实_t44(tmp_path):
    mem, ad = _mem(tmp_path)
    res = _ingest(mem, NO_PII)
    stored = _stored(ad, res.get("memory_id", ""))
    assert stored == NO_PII, "前提失效：无 PII 的文本竟被改写"
    assert res.get("auto_redacted") is False, (
        "无 PII 却报已脱敏 ⇒ 字段恒真（本判据就是为防这个）：%r" % (res,))
    assert res.get("pii_redacted_types") == [], "无 PII 时类别列表必须为空：%r" % (res,)
    assert res.get("redaction_source") is None, (
        "无脱敏时来源必须为 None：%r" % res.get("redaction_source"))


# ── ④ 回滚档：TRINITY_SENSITIVE_REDACT=0 ⇒ 不掩码 ⇒ 字段为假 ────────────────
def test_回滚档字段为假_t44(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_SENSITIVE_REDACT", "0")
    mem, ad = _mem(tmp_path)
    res = _ingest(mem, CATEGORY_PII)
    stored = _stored(ad, res.get("memory_id", ""))
    assert stored == CATEGORY_PII, "回滚档应当完全不掩码（G2 的回滚语义）"
    assert res.get("auto_redacted") is False, "回滚档没掩码，字段必须为假：%r" % (res,)
    assert res.get("pii_redacted_types") == []


# ── ⑤ 功能性牙齿：以**修前形状**直调适配器（无 ingestion 账本）⇒ 必须为假 ──
def test_牙齿_没有账本时不得报已脱敏_t44(tmp_path, monkeypatch):
    """直接调 `store_memory`（不给 `metadata["pii_redaction"]`）⇒ 必须 `False/[]`。
    这证明字段**跟着账本走**，不是无条件写 True。

    ⚠️ **t48（G7）后本条形状变了，原位说明（不覆盖历史）**：t48 把 PII 守卫**下沉到适配器
    边界** ⇒ 直接调 `store_memory` 的**含 PII 内容现在会被掩码**（这正是 G7 的目的），
    响应会如实变成 `True`。故本条改用 **`TRINITY_ADAPTER_GUARD=0`** 模拟"t48 之前"的形状
    （那时直写不经任何脱敏），**判据意图不变**：没有脱敏动作 ⇒ 字段必须为假。
    """
    from trinity.adapters.sqlite import SQLiteAdapter

    monkeypatch.setenv("TRINITY_ADAPTER_GUARD", "0")   # t48：回到「直写不经守卫」的形状
    db = str(tmp_path / "t44_teeth.db")
    ad = SQLiteAdapter(db_path=db)
    ad.connect()
    res = ad.store_memory(content="原始文本（未掩码）13800138000", agent_id="t44-teeth",
                          category="general", persona_id="default")
    assert res.get("auto_redacted") is False, (
        "没有脱敏账本却报已脱敏 ⇒ 字段成为常量（牙齿失效）：%r" % (res,))
    assert res.get("pii_redacted_types") == []
    assert res.get("redaction_source") is None


# ── ⑥ 去重早退路径也必须报账（同缺陷类第 2 处）──────────────────────────────
def test_去重早退路径也报账_t44(tmp_path):
    mem, ad = _mem(tmp_path)
    first = _ingest(mem, ONLY_PII)
    second = _ingest(mem, ONLY_PII)          # 同内容 ⇒ 命中 content_hash 去重早退
    assert second.get("dedup") is True, "前提失效：第二次写入没有走去重早退：%r" % (second,)
    assert second.get("auto_redacted") is True, (
        "去重早退路径的响应**缺字段/说假话**（t44 修的第 2 处）：%r" % (second,))
    assert second.get("pii_redacted_types"), "去重早退路径必须列出类别"
    assert second.get("memory_id") == first.get("memory_id")


# ── ⑦ 与库内账本一致性（审计可核）─────────────────────────────────────────
def test_响应字段与库内metadata账本一致_t44(tmp_path):
    mem, ad = _mem(tmp_path)
    res = _ingest(mem, ONLY_PII)
    row = ad.get_memory(res.get("memory_id", "")) or {}
    md = row.get("metadata") or {}
    if isinstance(md, str):
        md = json.loads(md)
    book = md.get("pii_redaction") or {}
    assert book.get("kinds"), "库内没有脱敏账本 ⇒ 响应字段无从核对"
    assert res.get("auto_redacted") is True
    assert set(book["kinds"]).issubset(set(res.get("pii_redacted_types") or [])), (
        "响应类别必须是库内账本的超集（含适配器侧那一份）：%r vs %r"
        % (res.get("pii_redacted_types"), book["kinds"]))
    assert res.get("pii_redaction") == book, "响应应回显同一份账本（可审计）"


# ── ⑧ 结构性牙齿：把"读 ingestion 账本"这一处改掉 ⇒ 侦测器必须红 ─────────────
def test_结构性牙齿_账本读取不得被移除_t44():
    src = CRUD.read_text(encoding="utf-8")
    assert '"pii_redaction"' in src, (
        "`_crud.py` 不再读 `metadata['pii_redaction']` ⇒ 又回到'只认自己的 auto_redact_pii'"
        "（响应字段会重新变成假话）")
    assert '"redaction_source"' in src, "缺少'哪套机制掩的'这一字段"
    # 侦测器本身要能失败：把账本读取那一行挖掉 ⇒ 必须被看见
    mutated = src.replace('metadata or {}).get("pii_redaction")',
                          'metadata or {}).get("_removed_ledger")', 1)
    assert mutated != src, "变异体没变 ⇒ 本用例的插入点失效"
    assert '"pii_redaction"' not in mutated.split("_ing_kinds")[0].split("_ing = ")[-1], \
        "变异后仍能读到账本 ⇒ 侦测器无效"
    # 顺带断言：返回字典里的 `auto_redacted` 不再由参数直接给出（旧写法）
    tree = ast.parse(src)
    bad = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.Dict)
           for k, v in zip(n.keys, n.values)
           if isinstance(k, ast.Constant) and k.value == "auto_redacted"
           and isinstance(v, ast.Name) and v.id == "auto_redact_pii"]
    assert not bad, "还有地方直接把**参数**当结果报出去（行 %r）" % bad
