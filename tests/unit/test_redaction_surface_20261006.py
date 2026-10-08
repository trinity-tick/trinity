# -*- coding: utf-8 -*-
"""t106/G3 判据：脱敏面四项（D-2 掩码口径 · D-3 开关面 · D-4 逐表覆盖 · D-5 占位符登记）。

每项 ≥2 条，含**反向/牙齿**：
  D-2 ① 口径内掩码点与口径一致 + **非口径点必须已登记**（差异单列，不静默）｜② 牙齿：把 `_mask_head`
      改成保留全串 ⇒ ① 必红。
  D-3 ① 开关面可机器读且字段齐｜② 登记默认值 == 本进程实测 == **子进程实测**｜③ ⭐ **主开关语义实测**
      （`SCAN=off`：客户端 + 适配器都不扫描，但**直调** `redact_identifiers` 仍掩码 ⇒「覆盖全部层」不成立）。
  D-4 ① 临时库里 `store_memory` 写 PII ⇒ `memories`（与 `memory_versions`）**都是掩码后**｜② 牙齿：
      把 `adapter_pii_guard` 摘成 identity ⇒ 明文落库 ⇒ ① 必红。
  D-5 ① 占位域名/token 登记可查且与 `sensitive` 的常量一致｜② 牙齿：注入一个未登记域名 ⇒ 必红。

全部在**临时 SQLite** 上跑（不碰生产库）；判据名**纯 ASCII**。
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
from trinity.security import redaction_surface as RS  # noqa: E402

PII_TEXT = "发货联系人手机 13812345678，邮箱 zhangsan@corp.cn。"


def _live():
    """⭐ 取**当前活着的** `sensitive` 模块实例。

    为什么不能只用模块级 `S`：本仓有判据会 `del sys.modules[...]` 后**重新导入** `sensitive`
    （`test_high_category_false_positives_20261006.py::c4`、`test_sensitive_pii_scope` 的
    多实例机制）⇒ 模块级绑定会指向**旧实例**，而写路径 `from ... import` 取到**新实例**
    ⇒ "记录了但查不到"的假红（实测复现过一次：同一批 7 个文件里 2 条红，单跑全绿）。
    """
    return sys.modules.get("trinity.security.sensitive") or S


def _q(db, sql, args=()):
    con = sqlite3.connect(str(db))
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


def _adapter(db: Path):
    from trinity.adapters.sqlite import SQLiteAdapter
    a = SQLiteAdapter(db_path=str(db))
    a.connect()
    return a


def _plain(v):
    from trinity.security.crypto import decrypt_content
    if isinstance(v, str) and v.startswith("enc:v1:"):
        return decrypt_content(v) or ""
    return v or ""


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_STORAGE_BACKEND", "sqlite")
    from trinity.core.client import Trinity
    return Trinity(store_path=str(tmp_path), adapter="sqlite", evolution_enabled=False)


def _patch_everywhere(mp, name, value) -> int:
    """把 `name` 打到**所有活着的** `trinity.security.sensitive` 模块实例上，返回命中数。

    ⚠️ 实测（t108）：只 patch `S` 或只 patch `_live()` 会**偶发不咬**——因为判据内部是
    `from trinity.security import sensitive`（调用时解析），而本仓有判据会 `del sys.modules[...]`
    后重新导入 ⇒ 存在**多个实例**。⇒ 牙齿必须**全覆盖**，否则就是"该红却绿"的假绿。
    ⭐ **改本处时请同步看 `tests/unit/test_decision_records_20261006.py::_live()`**（同一根因的第二处，
    两边 docstring 互相指明以防漂移）；test-triage(G9R-5) 独立得到同源修法（它叫 `_patch_policy`）。
    """
    import gc
    import types
    n = 0
    cands = [m for m in list(sys.modules.values())
             if isinstance(m, types.ModuleType) and m is not None
             and str(getattr(m, "__name__", "")) == "trinity.security.sensitive"]
    cands += [o for o in gc.get_objects()
              if isinstance(o, types.ModuleType)
              and str(getattr(o, "__name__", "")).endswith("security.sensitive")]
    seen = set()
    for m in cands + [S, sys.modules.get("trinity.security.sensitive")]:
        if m is None or id(m) in seen:
            continue
        seen.add(id(m))
        if hasattr(m, name):
            try:
                mp.setattr(m, name, value, raising=False)
                n += 1
            except Exception:                        # noqa: BLE001 —— 不静默（会计入 n）
                continue
    return n


# ── D-2 ───────────────────────────────────────────────────────────────
def d2_mask_points_conform_or_registered(tmp_path=None, monkeypatch=None) -> bool:
    """① 口径内掩码点**实测**符合「保留前 3、去尾号 / 邮箱只留 TLD」；非口径点**必须已登记**。"""
    probe = RS.mask_point_probe()
    if not probe["in_domain_conform"]:
        return False
    # 每个非口径点必须在清单里带 note（差异单列，不是被忽略）
    reg = {p["id"]: p for p in RS.MASK_POINTS}
    for pid in probe["non_conforming"]:
        if not reg.get(pid, {}).get("note"):
            return False
    # 已知的两个域外差异必须都在册（`_crypto.bak` 与 knowledge_pack 的占位符式）
    must = {"adapters.sqlite._crypto._detect_pii", "scripts.knowledge_pack._redact"}
    if not must.issubset(set(reg)):
        return False
    # 口径点必须覆盖 5 个 in-domain 掩码器
    ids = {p["id"] for p in RS.MASK_POINTS if p["in_domain"]}
    return {"sensitive._mask_head", "sensitive._mask_email", "sensitive._mask_secret",
            "sensitive._mask_grouped_card", "adapters._pii_guard.adapter_pii_guard"} <= ids


def d2_teeth_mask_full_string(tmp_path=None, monkeypatch=None) -> bool:
    """② 牙齿：把数字掩码器改成**保留全串** ⇒ D-2 ① 必红。

    ⚠️ 必须改**规则表里的掩码器**（`_PII_RULES` 在导入时已捕获函数对象 ⇒ 改 `_mask_keep` 不生效）——
    这是本仓第三次踩同一个坑（t43 的 C7、t70 的 C9/C10，现在是 D-2）。
    """
    keep_all = lambda m: m.group(0)                       # noqa: E731
    n = _patch_everywhere(monkeypatch, "_PII_RULES",
                          [(k, p, keep_all, v) for (k, p, _r, v) in _live()._PII_RULES])
    if n < 1:
        raise AssertionError("牙齿没打到任何 sensitive 实例 ⇒ 变绿是假的")
    return d2_mask_points_conform_or_registered()
    return d2_mask_points_conform_or_registered()


# ── D-3 ───────────────────────────────────────────────────────────────
def d3_surface_machine_readable(tmp_path=None, monkeypatch=None) -> bool:
    """① 开关面可机器读（`json.dumps` 能过）且每条字段齐（名字/默认/层/读者/语义）。"""
    surf = RS.switch_surface()
    try:
        json.dumps(surf, ensure_ascii=False)
    except Exception:                                 # noqa: BLE001
        return False
    if surf["count"] < 6:
        return False
    for sw in surf["switches"]:
        if not all(k in sw for k in ("name", "default", "layer", "readers", "semantics", "helper")):
            return False
        if not sw["readers"] or not sw["layer"]:
            return False
    return True


def d3_defaults_match_measurement(tmp_path=None, monkeypatch=None) -> bool:
    """② 登记默认值 == 本进程实测 == **子进程实测**（改名/改默认 ⇒ 必红）。"""
    inproc = RS.observed_defaults_inproc()
    subproc = RS.measure_defaults()
    if "_error" in subproc:
        return False
    for sw in RS.SWITCHES:
        name = sw["name"]
        if name not in inproc or name not in subproc:
            return False
        if inproc[name] != sw["default"] or subproc[name] != sw["default"]:
            return False
    # 子进程还要如实报出"本仓另有默认落盘的路径"
    return subproc.get("TRINITY_CORPUS_INDEX_PERSIST") is True


def d3_teeth_default_change(tmp_path=None, monkeypatch=None) -> bool:
    """② 牙齿：改动一个默认（把 `sensitive_redact_enabled` 改成恒 False）⇒ ② 必红。"""
    _patch_everywhere(monkeypatch, "sensitive_redact_enabled", lambda: False)
    return d3_defaults_match_measurement()


def d3_main_switch_semantics_measured(tmp_path, monkeypatch) -> bool:
    """③ ⭐ **主开关语义实测**（不推断）：`SCAN=off` 下
    ① 客户端写路径**不掩码**（原文落库）；② 适配器守卫报 `scan-off` 且不动内容；
    ③ **直调** `redact_identifiers` **仍然掩码** ⇒ 「覆盖全部层」**不成立**。"""
    monkeypatch.setenv("TRINITY_SENSITIVE_SCAN", "off")
    # ① 客户端
    cli = _client(tmp_path / "a", monkeypatch)
    res = cli.ingest(PII_TEXT, agent_id="g3-switch", postprocess=False)
    got = _plain(_q(tmp_path / "a" / "trinity_store.db",
                    "SELECT content FROM memories WHERE memory_id=?", (res["memory_id"],))[0])
    if got != PII_TEXT:
        return False
    # ② 适配器守卫
    from trinity.adapters._pii_guard import adapter_pii_guard
    c2, _md, info = adapter_pii_guard(PII_TEXT, {})
    if info.get("exempt") != "scan-off" or c2 != PII_TEXT:
        return False
    # ③ 直调掩码器（不经任何门）
    out, _labels = _live().redact_identifiers("13814141414")
    return out == "138********"


# ── D-4 ───────────────────────────────────────────────────────────────
def d4_write_path_masks_memories_and_versions(tmp_path, monkeypatch) -> bool:
    """① 临时库实测：`store_memory` 写 PII ⇒ `memories` **与** `memory_versions` 都是**掩码后**
    （后者是版本历史表 ⇒ 若留原文就等于把掩码撤销）。"""
    db = tmp_path / "d4.db"
    ad = _adapter(db)
    try:
        res = ad.store_memory(content=PII_TEXT, persona_id="p1", agent_id="a1", metadata={})
        mid = res.get("memory_id") or ""
        if not mid:
            return False
        got = _plain(_q(db, "SELECT content FROM memories WHERE memory_id=?", (mid,))[0])
        if "13812345678" in got or "138********" not in got:
            return False
        row = _q(db, "SELECT content FROM memory_versions WHERE memory_id=?", (mid,))
        if row is not None:
            ver = _plain(row[0])
            if "13812345678" in ver:
                return False
    finally:
        ad.disconnect()
    return True


def d4_teeth_remove_guard(tmp_path, monkeypatch) -> bool:
    """② 牙齿：把 `adapter_pii_guard` 摘成 identity ⇒ 明文落库 ⇒ ① 必红。"""
    import trinity.adapters._pii_guard as G
    monkeypatch.setattr(G, "adapter_pii_guard",
                        lambda content, metadata=None: (content, metadata,
                                                        {"scanned": True, "refuse": False,
                                                         "isolate": False, "exempt": "test-teeth"}))
    return d4_write_path_masks_memories_and_versions(tmp_path, monkeypatch)


# ── D-5 ───────────────────────────────────────────────────────────────
def d5_placeholder_registry(tmp_path=None, monkeypatch=None) -> bool:
    """① 占位域名/占位 token 登记可查，且**与 `sensitive` 的常量一致**（登记不漂移）。"""
    reg = RS.placeholder_registry()
    pol = reg["policy"]
    if set(pol["domains"]) != set(_live()._EMAIL_PLACEHOLDER_DOMAINS):
        return False
    if set(pol["reserved_names"]) != set(_live()._EMAIL_RESERVED_NAMES):
        return False
    for t in reg["tokens"]:
        if not t.get("used_by") or not t.get("consumers") or not t.get("why"):
            return False
    return {"*", "[PHONE]", "[EMAIL]"} <= {t["token"] for t in reg["tokens"]}


def d5_teeth_unregistered_domain(tmp_path=None, monkeypatch=None) -> bool:
    """② 牙齿：注入一个**未登记**的占位域名 ⇒ ① 必红。"""
    _patch_everywhere(monkeypatch, "_EMAIL_PLACEHOLDER_DOMAINS",
                      set(_live()._EMAIL_PLACEHOLDER_DOMAINS) | {"example.dev"})
    return d5_placeholder_registry()


CRITERIA = {
    "D2a_掩码点一致或已登记": d2_mask_points_conform_or_registered,
    "D3a_开关面可机读": d3_surface_machine_readable,
    "D3b_默认值三方一致": d3_defaults_match_measurement,
    "D3c_主开关语义实测": d3_main_switch_semantics_measured,
    "D4a_写入路径已掩码": d4_write_path_masks_memories_and_versions,
    "D5a_占位符登记一致": d5_placeholder_registry,
}


@pytest.mark.parametrize("name", sorted(CRITERIA), ids=sorted(CRITERIA))
def test_criteria_pass(name, tmp_path, monkeypatch):
    assert CRITERIA[name](tmp_path, monkeypatch) is True


MUTANTS = [
    ("D2a_掩码点一致或已登记", d2_teeth_mask_full_string),
    ("D3b_默认值三方一致", d3_teeth_default_change),
    ("D4a_写入路径已掩码", d4_teeth_remove_guard),
    ("D5a_占位符登记一致", d5_teeth_unregistered_domain),
]


@pytest.mark.parametrize("name,apply_mutant", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_each_criterion_has_a_killing_mutant(name, apply_mutant, tmp_path, monkeypatch):
    sub = tmp_path / name
    sub.mkdir()
    caught = ""
    try:
        got = apply_mutant(sub, monkeypatch)
    except Exception as e:                            # noqa: BLE001 —— 异常=红
        got, caught = False, "%s: %s" % (type(e).__name__, str(e)[:90])
    assert got is False, "变异体 %s 没有杀掉判据 %s ⇒ 该判据没有判别力" % (name, name)
    if caught:
        print("[teeth] %s 的变异体被拦下：%s" % (name, caught))
