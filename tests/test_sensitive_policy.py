# -*- coding: utf-8 -*-
"""Fable 5.1 对照审计 P0：敏感类别写入门控 + provenance_role 强制（2026-09-02）。

覆盖：
- P0-① scan_sensitive 规则（高危拒存 / 中危仅标记 / 策略 quarantine 隔离归档）
- P0-① ingest 接线：POLICY_PURGE 拒存审计（内容零落库）、POLICY_QUARANTINE
- P0-② provenance_role 归一（explicit/inferred/derived + 非法值兜底）
- P0-② 检索输出携带 provenance_role（sqlite FTS/LIKE 两通道）
"""
import json
import os
import sqlite3

import pytest

from trinity.security.sensitive import scan_sensitive

AGENT = "fable-policy-test"


def _db_path(tmp_path):
    return str(tmp_path / "trinity_store.db")


def _new_client(tmp_path, monkeypatch):
    # 强制 sqlite 本地库，避免触碰 PG 生产库（resolve_backend 回退陷阱）
    monkeypatch.setenv("TRINITY_STORAGE_BACKEND", "sqlite")
    from trinity.core.client import Trinity
    return Trinity(store_path=str(tmp_path), adapter="sqlite",
                   evolution_enabled=False)


def _audit_actions(db, action=None):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT action, details, memory_id FROM audit_log ORDER BY timestamp, id").fetchall()
    con.close()
    if action is not None:
        return [dict(r) for r in rows if r["action"] == action]
    return [dict(r) for r in rows]


def _mem_rows(db, agent=AGENT):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT memory_id, status, category, metadata FROM memories WHERE agent_id=?",
        (agent,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ── P0-① 规则层 ────────────────────────────────────────────────────
def test_scan_rules_high():
    for text in ["我想自杀，活着太累了。",
                 "用户有犯罪记录，案底在老家派出所。",
                 "他确诊了抑郁症，正在服用抗抑郁药物。",
                 "我有过一段性史，那是大学时期……",
                 "我的女儿 14 岁，在 XX 中学读初二，身份证号 3101...",
                 "I want to kill myself tonight"]:
        r = scan_sensitive(text)
        assert r["flagged"] and r["severity"] == "high", text


def test_scan_rules_benign():
    for text in ["WMS 拣货方式有哪些类型？",
                 "订单号 DO-20260902-1188 库存锁定失败",
                 "犯罪心理学是研究犯罪行为成因的学科。",
                 "这篇文档讨论了自杀干预热线 400-161-9995 的运营。"]:
        r = scan_sensitive(text)
        assert not r["flagged"] or r["severity"] == "medium", text


# ── P0-① ingest 拒存（默认 refuse）────────────────────────────────
def test_ingest_refuse_high(tmp_path, monkeypatch):
    cli = _new_client(tmp_path, monkeypatch)
    res = cli.ingest("我想自杀，活着太累了。", agent_id=AGENT, postprocess=False)
    assert res.get("error") == "policy_refused_sensitive"
    assert res.get("memory_id", "") == ""
    assert res["policy"]["action"] == "refuse"
    assert "self_harm" in res["policy"]["categories"]
    # 内容零落库
    assert _mem_rows(_db_path(tmp_path)) == []
    # 审计留痕 POLICY_PURGE
    acts = _audit_actions(_db_path(tmp_path), "POLICY_PURGE")
    assert len(acts) == 1
    assert "self_harm" in acts[0]["details"]
    assert acts[0]["memory_id"] is None


def test_ingest_normal_passthrough(tmp_path, monkeypatch):
    cli = _new_client(tmp_path, monkeypatch)
    res = cli.ingest("WMS 拣货需要按波次下发", agent_id=AGENT, postprocess=False)
    assert res.get("memory_id")
    rows = _mem_rows(_db_path(tmp_path))
    assert len(rows) == 1 and rows[0]["status"] == "active"
    meta = json.loads(rows[0]["metadata"] or "{}")
    assert "sensitive_scan" not in meta


def test_ingest_medium_only_marks(tmp_path, monkeypatch):
    cli = _new_client(tmp_path, monkeypatch)
    res = cli.ingest("最近有点抑郁，睡得不好。", agent_id=AGENT, postprocess=False)
    assert res.get("memory_id")
    rows = _mem_rows(_db_path(tmp_path))
    assert len(rows) == 1 and rows[0]["status"] == "active"
    meta = json.loads(rows[0]["metadata"] or "{}")
    assert meta["sensitive_scan"]["severity"] == "medium"
    assert "psych_health" in meta["sensitive_scan"]["categories"]


def test_ingest_quarantine_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_SENSITIVE_POLICY", "quarantine")
    cli = _new_client(tmp_path, monkeypatch)
    res = cli.ingest("他确诊了抑郁症，正在服用抗抑郁药物。",
                     agent_id=AGENT, postprocess=False)
    assert res.get("memory_id")
    rows = _mem_rows(_db_path(tmp_path))
    assert len(rows) == 1 and rows[0]["status"] == "archived"
    acts = _audit_actions(_db_path(tmp_path), "POLICY_QUARANTINE")
    assert len(acts) == 1


def test_ingest_scan_off(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_SENSITIVE_SCAN", "off")
    cli = _new_client(tmp_path, monkeypatch)
    res = cli.ingest("我想自杀，活着太累了。", agent_id=AGENT, postprocess=False)
    assert res.get("memory_id")
    rows = _mem_rows(_db_path(tmp_path))
    assert len(rows) == 1 and rows[0]["status"] == "active"


# ── P0-② provenance_role ───────────────────────────────────────────
def _prov_of(db, agent=AGENT):
    rows = _mem_rows(db, agent)
    return json.loads(rows[0]["metadata"] or "{}").get("provenance_role")


def test_provenance_inferred_default(tmp_path, monkeypatch):
    cli = _new_client(tmp_path, monkeypatch)
    cli.ingest("用户经常夜里加班。", agent_id=AGENT, postprocess=False)
    assert _prov_of(_db_path(tmp_path)) == "inferred"


def test_provenance_explicit_override(tmp_path, monkeypatch):
    cli = _new_client(tmp_path, monkeypatch)
    cli.ingest("我喜欢深色模式。", agent_id=AGENT,
               metadata={"provenance_role": "explicit"}, postprocess=False)
    assert _prov_of(_db_path(tmp_path)) == "explicit"


def test_provenance_assistant_derived(tmp_path, monkeypatch):
    cli = _new_client(tmp_path, monkeypatch)
    cli.ingest("总结：本会话完成三项修复。", agent_id=AGENT, role="assistant",
               postprocess=False)
    assert _prov_of(_db_path(tmp_path)) == "derived"


def test_provenance_invalid_falls_back(tmp_path, monkeypatch):
    cli = _new_client(tmp_path, monkeypatch)
    cli.ingest("测试非法来源值", agent_id=AGENT,
               metadata={"provenance_role": "guessed"}, postprocess=False)
    assert _prov_of(_db_path(tmp_path)) in ("explicit", "inferred", "derived")


# ── P0-② 检索输出携带 provenance_role ───────────────────────────────
def test_search_output_provenance(tmp_path, monkeypatch):
    cli = _new_client(tmp_path, monkeypatch)
    cli.ingest("用户偏好深色主题界面。", agent_id=AGENT, postprocess=False)
    from trinity.adapters.sqlite import SQLiteAdapter
    ad = SQLiteAdapter(db_path=_db_path(tmp_path))
    ad.connect()
    try:
        hits = ad.search_memories(query="深色主题", top_k=5)
        assert hits and hits[0]["provenance_role"] == "inferred"
        assert hits[0]["metadata"]["provenance_role"] == "inferred"
    finally:
        ad.disconnect()


def test_search_output_provenance_explicit(tmp_path, monkeypatch):
    from trinity.adapters.sqlite import SQLiteAdapter
    ad = SQLiteAdapter(db_path=_db_path(tmp_path))
    ad.connect()
    try:
        ad.store_memory("我明确说过喜欢周末爬山。", agent_id=AGENT,
                        metadata={"provenance_role": "explicit"})
        hits = ad.search_memories(query="周末爬山", top_k=5)
        assert hits and hits[0]["provenance_role"] == "explicit"
    finally:
        ad.disconnect()
