# -*- coding: utf-8 -*-
"""t69/I9 判据：`scripts/` 搬运/评测脚本的**幂等·等价性·静默丢行可见性**（可失败 + 负向牙齿）。

## 被钉住的 4 处 🔴（t63 实测）
| 脚本 | 症状（修前） |
|---|---|
| `scripts/knowledge_pack.py::unpack_pack` | 用 `sha256(原文)` 做幂等 ⇒ 守卫已把 content 换成掩码形 ⇒ 判定失效（同一包反复导入） |
| `scripts/federation_sync.py::import_snapshot` | 同上；且**冲突分支恒真、永不触发** |
| `scripts/memory_portability.py::import_memories` | 同上 |
| `scripts/bench_ingest_longmemeval_s.py` | 评测语料灌**默认库**；`ok += len(recs)` 把 refuse 静默算成成功 |

## 口径（**关键，别写反**）
「等价」= **落库 == 掩码/隔离后的源**，**不是**「落库 == 原件」。
含 PII 的内容被守卫掩成 `138********` 是**预期**行为；把它判成失败就是口径写反。

## 牙齿
每条判据都配一个**变异体**（把实现改回错的/把决定性的一步拿掉）⇒ 该判据**必须变红**。
唯一没有变异体的是 `J7`：它的变异体需要**真的去写默认库**（生产存储）—— 明文禁止，故不设，
并在此如实登记（这是"没做"而不是"通过"）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AGENT = "t69-eq"
PII_TEXT = "候选人的联系方式是 13800138000，请勿外传。"
PLAIN_TEXT = "这份文档说明了波次拣货的合并策略。"
HIGH_TEXT = "我想自杀，活着太累了。"          # high 档 ⇒ 守卫会**拒存**（静默不落库）
PLAIN_TEXT_2 = "远端改过的另一份内容，波次拣货策略已更新。"


# ── 小工具 ────────────────────────────────────────────────────────────
def _adapter(db: Path):
    from trinity.adapters.sqlite import SQLiteAdapter
    a = SQLiteAdapter(db_path=str(db))
    a.connect()
    return a


def _rows(db: Path) -> int:
    a = _adapter(db)
    try:
        return int(a._conn.execute("SELECT count(*) FROM memories").fetchone()[0])
    finally:
        a.disconnect()


def _default_store() -> str:
    return os.path.expanduser("~/.trinity/store/trinity_store.db")


def _default_store_rows():
    """只读数默认（生产）库行数 —— **只读**，绝不写。"""
    p = _default_store()
    if not os.path.exists(p):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % Path(p).as_posix(), uri=True)
        try:
            return int(con.execute("SELECT count(*) FROM memories").fetchone()[0])
        finally:
            con.close()
    except Exception:
        return None


def _pack_file(tmp_path: Path, items) -> str:
    p = tmp_path / "kb_t69.json"
    p.write_text(json.dumps({"schema_version": "1.0", "title": "t69", "category": "research",
                             "items": items}, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _snap_file(tmp_path: Path, name: str, memories) -> str:
    p = tmp_path / name
    p.write_text(json.dumps({"schema_version": "1.0", "memories": memories},
                            ensure_ascii=False), encoding="utf-8")
    return str(p)


# ── 判据 ──────────────────────────────────────────────────────────────
def j1_knowledge_pack_idempotent(tmp_path, monkeypatch) -> bool:
    """① 幂等：同一个知识包导入两次 ⇒ 第二次**不新增**（且做了等价性断言）。"""
    import scripts.knowledge_pack as KP
    db = tmp_path / "kp.db"
    f = _pack_file(tmp_path, [{"content": PII_TEXT}, {"content": PLAIN_TEXT}])
    r1 = KP.unpack_pack(str(db), f, persona_id="p1")
    r2 = KP.unpack_pack(str(db), f, persona_id="p1")
    return (r1["imported"] == 2 and r1["equivalence_checked"] == 2
            and r2["imported"] == 0 and r2["skipped"] == 2 and _rows(db) == 2)


def j2_federation_idempotent(tmp_path, monkeypatch) -> bool:
    """① 幂等（federation）：同一快照导入两次 ⇒ 第二次不新增。"""
    import scripts.federation_sync as FS
    db = tmp_path / "fs.db"
    f = _snap_file(tmp_path, "s1.json",
                   [{"content": PII_TEXT, "persona_id": "p1", "agent_id": "a1"}])
    r1 = FS.import_snapshot(str(db), f)
    r2 = FS.import_snapshot(str(db), f)
    return (r1["imported"] == 1 and r1["equivalence_checked"] == 1
            and r2["imported"] == 0 and r2["skipped"] == 1
            and r2["conflicts"] == 0 and _rows(db) == 1)


def j3_portability_idempotent(tmp_path, monkeypatch) -> bool:
    """① 幂等（portability）：同一份导出件导入两次 ⇒ 第二次不新增。"""
    import scripts.memory_portability as MP
    db = tmp_path / "mp.db"
    items = [{"content": PII_TEXT, "persona_id": "p1", "agent_id": "a1"},
             {"content": PLAIN_TEXT, "persona_id": "p1", "agent_id": "a1"}]
    r1 = MP.import_memories(items, str(db))
    r2 = MP.import_memories(items, str(db))
    return (r1["imported"] == 2 and r1["equivalence_checked"] == 2
            and r2["imported"] == 0 and r2["skipped"] == 2 and _rows(db) == 2)


def j4_equivalence_assertion_fires(tmp_path, monkeypatch) -> bool:
    """② **等价性断言真的会响**：人为把"将被落库的形态"做偏 ⇒ 必须抛 AssertionError。"""
    import scripts.knowledge_pack as KP
    real = KP._stored_form

    def tampered(content, metadata=None):
        stored, info = real(content, metadata)
        return stored + " [TAMPERED]", info

    monkeypatch.setattr(KP, "_stored_form", tampered)
    db = tmp_path / "ke.db"
    f = _pack_file(tmp_path, [{"content": PLAIN_TEXT}])
    try:
        KP.unpack_pack(str(db), f, persona_id="p1")
    except AssertionError as e:
        return ("等价性断言" in str(e)) and _rows(db) == 1
    return False


def j5_conflict_branch_triggers(tmp_path, monkeypatch) -> bool:
    """③ **冲突分支真被修好**：同来源身份、内容分叉 ⇒ `conflicts==1` 且不覆盖本地。"""
    import scripts.federation_sync as FS
    db = tmp_path / "fc.db"
    src = "urn:test:mem/1"
    f1 = _snap_file(tmp_path, "c1.json", [{"content": PLAIN_TEXT, "persona_id": "p1",
                                           "agent_id": "a1", "source_uri": src}])
    f2 = _snap_file(tmp_path, "c2.json", [{"content": PLAIN_TEXT_2, "persona_id": "p1",
                                           "agent_id": "a1", "source_uri": src}])
    r1 = FS.import_snapshot(str(db), f1)
    r2 = FS.import_snapshot(str(db), f2)
    if not (r1["imported"] == 1 and r2["conflicts"] == 1 and r2["imported"] == 0):
        return False
    a = _adapter(db)
    try:
        n = a._conn.execute("SELECT count(*) FROM memories").fetchone()[0]
        got = a._conn.execute("SELECT content_hash FROM memories").fetchone()[0]
    finally:
        a.disconnect()
    # 本地内容未被覆盖：hash 仍等于"将被落库形态"的 hash（= 源 1 的形态）
    exp1 = FS._hash(FS._stored_form(PLAIN_TEXT)[0])
    return n == 1 and got == exp1


def j6_refuse_is_reported(tmp_path, monkeypatch) -> bool:
    """③ 评测量化：refuse 行数**被报出**；反事实：无 refuse 时必须是 0。"""
    import scripts.bench_ingest_longmemeval_s as BI
    db = tmp_path / "bi.db"
    a = _adapter(db)
    try:
        res = BI._ingest_records(a, "eval-x", [("s1", HIGH_TEXT), ("s2", PLAIN_TEXT)], 10)
    finally:
        a.disconnect()
    db2 = tmp_path / "bi2.db"
    a2 = _adapter(db2)
    try:
        res2 = BI._ingest_records(a2, "eval-x", [("s3", PLAIN_TEXT)], 10)
    finally:
        a2.disconnect()
    return (res["refused"] == 1 and res["inserted"] == 1 and res["equivalence_checked"] == 1
            and res["refuse_rate"] == 0.5
            and res2["refused"] == 0 and res2["inserted"] == 1 and res2["refuse_rate"] == 0.0)


def j7_bench_writes_isolated_store_not_default(tmp_path, monkeypatch) -> bool:
    """② 评测语料**不得灌进默认库**：跑一次 `main()`，默认库行数**不变**。

    ⚠️ 无变异体：它的变异体必须**真的写默认（生产）库** —— 本任务明文禁止，故只登记不实施。
    """
    import scripts.bench_ingest_longmemeval_s as BI
    import trinity.core.client as _client_mod
    captured = {}
    real_trinity = _client_mod.Trinity

    def spy_trinity(*a, **kw):
        captured.update(kw)
        return real_trinity(*a, **kw)

    before = _default_store_rows()
    monkeypatch.setattr(BI, "_load_sessions",
                        lambda: {"t69-s1": PLAIN_TEXT, "t69-s2": PII_TEXT})
    monkeypatch.setattr(_client_mod, "Trinity", spy_trinity)
    monkeypatch.setattr(sys, "argv", ["bench_ingest_longmemeval_s.py", "--limit", "2",
                                      "--report", str(tmp_path / "rep.json")])
    rc = BI.main()
    after = _default_store_rows()
    store = str(captured.get("store_path") or "")
    return (rc == 0 and captured.get("adapter") == "sqlite"
            and store != _default_store() and store.startswith(tempfile.gettempdir())
            and before == after)


CRITERIA = {
    "J1_知识包导入幂等": j1_knowledge_pack_idempotent,
    "J2_联邦快照导入幂等": j2_federation_idempotent,
    "J3_可移植导入幂等": j3_portability_idempotent,
    "J4_等价性断言会响": j4_equivalence_assertion_fires,
    "J5_冲突分支触发": j5_conflict_branch_triggers,
    "J6_拒绝行被报出": j6_refuse_is_reported,
    "J7_评测写隔离库": j7_bench_writes_isolated_store_not_default,
}


@pytest.mark.parametrize("name", sorted(CRITERIA), ids=sorted(CRITERIA))
def test_判据通过(name, tmp_path, monkeypatch):
    assert CRITERIA[name](tmp_path, monkeypatch) is True


# ── 负向（牙齿）：每个变异体必须杀掉对应判据 ────────────────────────────
def _identity_stored_form(content, metadata=None):
    """变异体：把"将被落库形态"改回**原文口径**（= 修前的错法）。"""
    return content, {"available": True, "refuse": False, "isolate": False}


def _noop_assert(adapter, memory_id, expected_stored):
    return None


def _never_conflict(adapter, persona_id, agent_id, source_uri, expected_hash):
    return None


def _always_inserted(r):
    """变异体：把每条结果都当"已插入" ⇒ refuse 又变回静默。"""
    return "inserted"


MUTANTS = [
    ("J1_知识包导入幂等",
     lambda mp: mp.setattr("scripts.knowledge_pack._stored_form", _identity_stored_form)),
    ("J2_联邦快照导入幂等",
     lambda mp: mp.setattr("scripts.federation_sync._stored_form", _identity_stored_form)),
    ("J3_可移植导入幂等",
     lambda mp: mp.setattr("scripts.memory_portability._stored_form", _identity_stored_form)),
    ("J4_等价性断言会响",
     lambda mp: mp.setattr("scripts.knowledge_pack._assert_stored_matches", _noop_assert)),
    ("J5_冲突分支触发",
     lambda mp: mp.setattr("scripts.federation_sync._detect_conflict", _never_conflict)),
    ("J6_拒绝行被报出",
     lambda mp: mp.setattr("scripts.bench_ingest_longmemeval_s._classify_result",
                           _always_inserted)),
]


@pytest.mark.parametrize("name,apply_mutant", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_每条判据都有能杀掉它的变异体(name, apply_mutant, tmp_path, monkeypatch):
    """变异体必须让判据**红**。

    ⚠️ 对 J1–J3：变异体把 hash 口径改回"原文"后，判据**不是**返回 False，而是被
    **等价性断言当场拦下**（抛 `AssertionError`）—— 这正是"断言是主防线"的直接体现，
    所以"抛异常"同样记为**判据红**（并把抓到它的机制写进失败信息）。
    """
    apply_mutant(monkeypatch)
    sub = tmp_path / name
    sub.mkdir()
    caught = ""
    try:
        got = CRITERIA[name](sub, monkeypatch)
    except Exception as e:                       # noqa: BLE001 —— 异常=红（见 docstring）
        got, caught = False, "%s: %s" % (type(e).__name__, str(e)[:80])
    assert got is False, (
        "变异体 %s 没有杀掉判据 %s ⇒ 该判据没有判别力" % (name, name))
    if caught:
        print("[teeth] %s 的变异体被拦下：%s" % (name, caught))
