# -*- coding: utf-8 -*-
"""语料回填工具（`scripts/corpus_backfill_dryrun.py`）与去重计划的测试（t4 / 2026-10-06）。

## 要守住的三件事

1. **dry-run 是默认**：不带 `--apply` 时一个字节都不能写（用临时库的行数/内容指纹证明）。
2. **三重门**：`--apply` 缺备份、备份没校验过、令牌不对 —— 任一情况都必须**拒绝执行**
   并降级回 dry-run（不是打印警告后照做）。
3. **去重计划的硬边界**：`active` 面一行都不许动；没有指纹的行**不猜**（不参与计划）；
   规范副本选择必须**active 优先**且确定性（同输入同输出）。
"""
import json
import os
import pathlib
import sqlite3
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import corpus_backfill_dryrun as bf  # noqa: E402


# ─────────────────────────────────────────── 夹具：一个最小可用库

SCHEMA = """
CREATE TABLE memories (
  memory_id TEXT PRIMARY KEY, session_id TEXT, persona_id TEXT DEFAULT 'default',
  tenant_id TEXT DEFAULT 'default', content TEXT, role TEXT, importance REAL DEFAULT 0.5,
  tags TEXT DEFAULT '[]', category TEXT DEFAULT 'general', sha256_hash TEXT,
  embedding BLOB, status TEXT DEFAULT 'active', version INTEGER DEFAULT 1,
  created_at TEXT, updated_at TEXT, access_count INTEGER DEFAULT 0,
  last_accessed_at TEXT, search_text TEXT, summary_level INTEGER, summary_text TEXT,
  review_interval_days INTEGER, next_review_at TEXT, merged_into TEXT,
  agent_id TEXT DEFAULT 'default', app_id TEXT, tokenized_content TEXT, ttl_seconds INTEGER,
  importance_score REAL DEFAULT 0.0, content_hash TEXT, conflict_group_id TEXT,
  is_resolved INTEGER DEFAULT 0, modality TEXT DEFAULT 'text', metadata TEXT DEFAULT '{}',
  source_uri TEXT, memory_layer TEXT
);
CREATE TABLE audit_log (id TEXT PRIMARY KEY, action TEXT, timestamp TEXT);
"""


def _mkdb(path: pathlib.Path, rows) -> None:
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    for r in rows:
        con.execute(
            "INSERT INTO memories (memory_id,persona_id,agent_id,content,status,sha256_hash,"
            "content_hash,category,tags,access_count,importance,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["memory_id"], r.get("persona_id", "default"), r.get("agent_id", "default"),
             r["content"], r.get("status", "active"), r.get("sha256_hash"),
             r.get("content_hash"), r.get("category", "general"), r.get("tags", "[]"),
             r.get("access_count", 0), r.get("importance", 0.5),
             r.get("created_at", "2026-08-01T00:00:00")))
    con.execute("INSERT INTO audit_log (id,action,timestamp) VALUES ('a1','create','x')")
    con.commit()
    con.close()


ROWS = [
    # 组 1：同 (persona,agent,hash) 三行 —— 规范副本必须选 **active 优先**
    {"memory_id": "m-active", "content": "重复内容甲", "status": "active",
     "content_hash": "h1", "access_count": 0, "importance": 0.1},
    {"memory_id": "m-arch-hot", "content": "重复内容甲", "status": "archived",
     "content_hash": "h1", "access_count": 999, "importance": 0.9},
    {"memory_id": "m-arch-2", "content": "重复内容甲", "status": "archived",
     "content_hash": "h1", "access_count": 1, "importance": 0.2},
    # 组 2：全归档（可安全改）
    {"memory_id": "m-a2", "content": "重复内容乙", "status": "archived", "content_hash": "h2"},
    {"memory_id": "m-a3", "content": "重复内容乙", "status": "deleted", "content_hash": "h2"},
    # 无指纹的行：**不猜**，不参与计划
    {"memory_id": "m-nohash", "content": "没有指纹的行", "status": "archived",
     "content_hash": None, "sha256_hash": None},
    # 无标签 + 已映射 / 未映射
    {"memory_id": "m-notag1", "content": "无标签一", "status": "active", "tags": "[]",
     "category": "kb_harvested"},
    {"memory_id": "m-notag2", "content": "无标签二", "status": "archived", "tags": None,
     "category": "weird-category"},
    {"memory_id": "m-tagged", "content": "有标签", "status": "active", "tags": '["x"]',
     "category": "kb_harvested"},
]


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    _mkdb(p, ROWS)
    return str(p)


def _plan(db: str, job: str = "dedup"):
    c = sqlite3.connect(db)
    try:
        return bf.plan_dedup(bf._load_rows(c, job, 0))
    finally:
        c.close()


# ─────────────────────────────────────────── 去重计划

def test_plan_dedup_never_picks_active_as_victim(db):
    """**硬边界**：active 行在任何情况下都不得出现在计划里。"""
    plan = _plan(db)
    victims = {a["memory_id"] for a in plan["samples"]}
    assert "m-active" not in victims
    assert plan["skipped_active_rows"] == 0, (
        "计划不该【想动】active 行：规范副本必须 active 优先（未加这一级时实测 4,193 条）")


def test_plan_dedup_canonical_is_active_first(db):
    """规范副本选择：**active 优先**，即使归档行的 access_count 高得多。"""
    plan = _plan(db)
    for a in plan["samples"]:
        if a["group_key"].endswith("h1"):
            assert a["merged_into"] == "m-active"
    assert plan["would_change"] == 3, plan["would_change"]  # h1 两条归档 + h2 一条 deleted
    assert plan["by_old_status"].get("archived") == 2
    assert plan["by_old_status"].get("deleted") == 1
    assert plan["total_redundant_rows_by_hash"] == 3


def test_plan_dedup_skips_rows_without_hash(db):
    """没有指纹的行必须被排除（不猜），并体现在计数里。"""
    plan = _plan(db)
    assert "m-nohash" not in {a["memory_id"] for a in plan["samples"]}
    assert plan["would_change"] == 3


def test_plan_dedup_is_deterministic(db):
    """同输入必须同输出（含并列时的最终 tie-break）。"""
    rows = None
    c = sqlite3.connect(db)
    rows = bf._load_rows(c, "dedup", 0)
    c.close()
    assert json.dumps(bf.plan_dedup(rows), sort_keys=True) == \
        json.dumps(bf.plan_dedup(rows), sort_keys=True)


def test_plan_dedup_marks_merged_not_delete(db):
    """回填只改 status/merged_into（保留审计链），**绝不删行**。"""
    plan = _plan(db)
    assert plan["write_scope"].startswith("仅非 active 行")
    for a in plan["samples"]:
        assert a["new_status"] == "merged" and a["merged_into"]


# ─────────────────────────────────────────── 标签计划

def _plan_tags(db: str, tag_map):
    c = sqlite3.connect(db)
    try:
        return bf.plan_tags(bf._load_rows(c, "tags", 0), tag_map)
    finally:
        c.close()


def test_plan_tags_never_invents_tags(db):
    """映射里没有的 category ⇒ 不出建议（宁可少打，也不把猜的标签写进生产库）。"""
    plan = _plan_tags(db, {"kb_harvested": ["kb", "harvested"]})
    assert plan["would_change"] == 1
    assert plan["samples"][0]["memory_id"] == "m-notag1"
    assert plan["unmapped_categories"].get("weird-category") == 1
    assert "kb_harvested" not in plan["unmapped_categories"], "已映射的类目不该出现在未映射里"


def test_plan_tags_skips_already_tagged(db):
    """已有标签的行一律跳过（哪怕标签只有一个无关值）。"""
    plan = _plan_tags(db, {"kb_harvested": ["kb"]})
    assert "m-tagged" not in {a["memory_id"] for a in plan["samples"]}


def test_plan_tags_empty_map_yields_no_write(db):
    """空映射 ⇒ 0 改动（默认姿态：不知道就不改）。"""
    plan = _plan_tags(db, {})
    assert plan["would_change"] == 0


# ─────────────────────────────────────────── dry-run 默认 + 三重门

def _fingerprint(p):
    c = sqlite3.connect(str(p))
    rows = c.execute("select memory_id,status,tags from memories order by memory_id").fetchall()
    c.close()
    return rows


def test_cli_default_is_dry_run_and_writes_nothing(db, tmp_path):
    """CLI 不带 --apply ⇒ readonly、dry_run、库指纹不变。"""
    before = _fingerprint(db)
    out = tmp_path / "rep.json"
    rc = bf.main(["dedup", "--db", db, "--json", str(out)])
    assert rc == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["readonly"] is True and rep["dry_run"] is True
    assert rep["apply_requested"] is False
    assert _fingerprint(db) == before, "dry-run 动了库"


def test_cli_apply_without_backup_is_refused(db, tmp_path):
    """`--apply` 但没给备份 ⇒ 拒绝执行并降级 dry-run，库不变。"""
    before = _fingerprint(db)
    out = tmp_path / "rep2.json"
    bf.main(["dedup", "--db", db, "--apply", "--confirm-token", bf.APPLY_TOKEN,
             "--json", str(out)])
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["dry_run"] is True and rep["readonly"] is True
    assert "REFUSE" in json.dumps(rep, ensure_ascii=False)
    assert _fingerprint(db) == before


def test_cli_apply_with_bad_token_is_refused(db, tmp_path):
    """令牌不对 ⇒ 拒绝（即使备份给了且校验通过）。"""
    bpath = str(tmp_path / "b.db")
    assert bf.backup_store(db, bpath)["ok"] is True
    before = _fingerprint(db)
    out = tmp_path / "rep3.json"
    bf.main(["dedup", "--db", db, "--apply", "--backup", bpath,
             "--confirm-token", "wrong", "--json", str(out)])
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["dry_run"] is True
    assert _fingerprint(db) == before


def test_cli_apply_with_all_three_gates_executes(db, tmp_path):
    """三重门齐备（apply + 已校验备份 + 正确令牌）⇒ 真的执行，且只改非 active 行。"""
    bpath = str(tmp_path / "b2.db")
    assert bf.backup_store(db, bpath)["ok"] is True
    out = tmp_path / "rep4.json"
    bf.main(["dedup", "--db", db, "--apply", "--backup", bpath,
             "--confirm-token", bf.APPLY_TOKEN, "--json", str(out)])
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["dry_run"] is False
    assert rep["plan"]["_apply_result"]["executed"] == 3
    c = sqlite3.connect(db)
    assert c.execute("select count(*) from memories where status='merged'").fetchone()[0] == 3
    assert c.execute("select count(*) from memories where status='active'").fetchone()[0] == 3, \
        "active 行数不得变化"
    assert c.execute("select count(*) from memories").fetchone()[0] == len(ROWS), "不许删行"
    c.close()


# ─────────────────────────────────────────── 备份

def test_backup_store_refuses_to_overwrite(db, tmp_path):
    """备份不覆盖已有文件（历史备份不能被悄悄替换）。"""
    bpath = str(tmp_path / "b3.db")
    assert bf.backup_store(db, bpath)["ok"] is True
    again = bf.backup_store(db, bpath)
    assert again["ok"] is False and "REFUSE" in again["error"]


def test_backup_verify_checks_integrity_and_rows(db, tmp_path):
    """备份校验必须给出 integrity_check 与行数（不是"文件存在就算过"）。"""
    bpath = str(tmp_path / "b4.db")
    v = bf.backup_store(db, bpath)
    assert v["ok"] is True and v["integrity_check"] == "ok"
    assert v["memories"] == len(ROWS)
    assert bf.verify_backup(str(tmp_path / "missing.db"))["ok"] is False


def test_apply_gate_rejects_stale_backup(db, tmp_path):
    """备份比源库旧（行数少）⇒ 拒绝：防止"备份是三天前的"这种假安全。"""
    bpath = tmp_path / "b5.db"
    old = tmp_path / "old.db"
    _mkdb(old, ROWS[:3])
    assert bf.backup_store(str(old), str(bpath))["ok"] is True
    ok, why = bf._require_backup(str(bpath), db)
    assert ok is False and "比源库旧" in why["error"]


# ─────────────────────────────────────────── guard 回放

def test_plan_guard_reports_would_block_by_code(db):
    """guard 回放：报"会拦什么"并按 code 分桶，且 annotate 档不拦（allow 恒真）。"""
    c = sqlite3.connect(db)
    rows = bf._load_rows(c, "guard", 0)
    plan = bf.plan_guard(rows, mode="annotate")
    assert plan["rows_replayed"] == len(rows)
    assert plan["blocked_now"] == 0, "annotate 档不得真的拦"
    assert plan["would_block"] >= 2, plan["by_code"]
    c.close()


def test_plan_guard_on_mode_blocks_and_flags_false_positive_risk(db):
    """on 档：真的拦；同时报"被拦下但历史被读过"的上界（误杀风险必须可读）。"""
    c = sqlite3.connect(db)
    rows = bf._load_rows(c, "guard", 0)
    for r in rows:
        r["access_count"] = 7
    plan = bf.plan_guard(rows, mode="on")
    assert plan["blocked_now"] == plan["would_block"] > 0
    assert plan["false_positive_check"]["blocked_but_ever_read"] == plan["would_block"]
    assert plan["false_positive_check"]["rate_in_would_block"] == 1.0
    c.close()


def test_plan_guard_off_mode_blocks_nothing(db):
    """off 档 ⇒ 一条都不拦（回滚档必须真的回到"没有本模块"）。"""
    c = sqlite3.connect(db)
    rows = bf._load_rows(c, "guard", 0)
    plan = bf.plan_guard(rows, mode="off")
    assert plan["would_block"] == 0 and plan["blocked_now"] == 0
    c.close()
