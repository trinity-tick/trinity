# -*- coding: utf-8 -*-
"""t108/G5 · D-13 判据：**决策记录落盘三件套**（opt-in · 最小化 · 保留策略）+ 消费者判定。

| 判据 | 内容 | 反向/牙齿 |
|---|---|---|
| C1 | **默认可机器核**：不设 `PATH` ⇒ `persist_path() is None`、`enabled False`，且**内存记录照常** | 牙齿：让默认落盘 ⇒ 必红 |
| C2 | 设 `PATH` ⇒ **确实落盘**（文件存在、行数 == 新增记录数、每行是合法 JSON） | — |
| C3 | **反向**：不设 `PATH` ⇒ 目标目录**零文件**（不得产生任何文件） | — |
| C4 | **保留策略**：超限**轮转**（`rotations >= 1`、`.1` 一代在、大小 ≤ max(上限, 单行最大)） | 牙齿：摘掉轮转 ⇒ 必红 |
| C5 | **最小化**：落盘字段 ⊆ 白名单、**不含正文原文**、无 `content` 键 | 牙齿：摘掉最小化 + 记录带正文 ⇒ 必红 |
| C6 | **消费者判定**（与 G8/D-14 **同一条谓词**）：全仓检索只命中 生产者/登记/判据 三类 | —（新增消费者 ⇒ 红，触发"默认策略需重新评估"） |

⚠️ 一律用 `_live()`（当前活着的模块实例）+ fixture 归零环境与计数（t85/t107 的隔离教训）。
⚠️ 全部在**临时目录**上写盘（不碰生产库/生产路径）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import trinity.security.sensitive as S  # noqa: E402

SPOT = "我想自杀，活着太累了。"                 # 一定产生记录（high）
SPOT2 = "我准备自杀，遗书已经写好了。"
BENIGN = "用户偏好暗色模式，使用 VS Code 与 Python。"   # 反事实：不产生记录
ENV_KEYS = ("TRINITY_DECISION_LOG_PATH", "TRINITY_DECISION_LOG_MAX_BYTES",
            "TRINITY_DECISION_LOG_SIZE", "TRINITY_SENSITIVE_SCAN", "TRINITY_SENSITIVE_REDACT",
            "TRINITY_HIGH_PERSONAL_CONTEXT", "TRINITY_HELP_CONTEXT_GATE")


def _live():
    return sys.modules.get("trinity.security.sensitive") or S


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    m = _live()
    m.reset_decision_log()
    m._DECISION_PERSIST_STATS.update({"lines": 0, "bytes": 0, "rotations": 0, "errors": 0})
    yield


def _files(d: Path):
    return sorted(p.name for p in d.iterdir())


# ── C1 ────────────────────────────────────────────────────────────────
def c1_default_no_disk(tmp_path=None, monkeypatch=None) -> bool:
    m = _live()
    d = Path(str(tmp_path))
    stats = m.decision_persistence_stats()
    if stats["enabled"] is not False or stats["path"] is not None:
        return False
    m.scan_sensitive(SPOT)
    m.scan_sensitive(SPOT2)
    if len(m.decision_log(limit=0)) < 2:          # 内存面照常工作
        return False
    return _files(d) == []                         # 且目录一个文件都没有


# ── C2 ────────────────────────────────────────────────────────────────
def c2_opt_in_writes(tmp_path=None, monkeypatch=None) -> bool:
    m = _live()
    d = Path(str(tmp_path))
    p = d / "decisions.jsonl"
    monkeypatch.setenv("TRINITY_DECISION_LOG_PATH", str(p))
    m.scan_sensitive(SPOT)
    m.scan_sensitive(SPOT2)
    m.scan_sensitive(BENIGN)                       # 良性 ⇒ 不得写盘
    if not p.exists():
        return False
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) != 2:
        return False
    return all(isinstance(json.loads(ln), dict) for ln in lines)


# ── C3 反向 ───────────────────────────────────────────────────────────
def c3_no_path_no_files(tmp_path=None, monkeypatch=None) -> bool:
    m = _live()
    d = Path(str(tmp_path))
    m.scan_sensitive(SPOT)
    m.scan_sensitive(SPOT2)
    if _files(d):
        return False
    # 即便把 MAX_BYTES 设得很小，也不该凭空造文件
    monkeypatch.setenv("TRINITY_DECISION_LOG_MAX_BYTES", "10")
    m.scan_sensitive(SPOT)
    return _files(d) == []


# ── C4 保留策略 ───────────────────────────────────────────────────────
def c4_retention_rotation(tmp_path=None, monkeypatch=None) -> bool:
    m = _live()
    d = Path(str(tmp_path))
    p = d / "decisions.jsonl"
    monkeypatch.setenv("TRINITY_DECISION_LOG_PATH", str(p))
    monkeypatch.setenv("TRINITY_DECISION_LOG_MAX_BYTES", "300")
    limit = m.decision_persist_max_bytes()
    if limit != 300:
        return False
    for i in range(12):
        m.scan_sensitive("%s第 %d 次。" % (SPOT, i))
    stats = m.decision_persistence_stats()
    if stats["rotations"] < 1 or not (d / "decisions.jsonl.1").exists():
        return False
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # 诚实上界：**单行**若本身超过上限，则无法再切分 ⇒ 上界 = max(上限, 单行最大 + 换行余量)
    # （实测：单行 ≈525 B > 上限 300 ⇒ 文件恰好 1 行；无轮转时会是 12 行 ≈6312 B ⇒ 必红）
    biggest = max((len(ln.encode("utf-8")) + 8 for ln in lines), default=0)
    return p.stat().st_size <= max(limit, biggest)


# ── C5 最小化 ─────────────────────────────────────────────────────────
def c5_minimized_no_content(tmp_path=None, monkeypatch=None) -> bool:
    m = _live()
    d = Path(str(tmp_path))
    p = d / "decisions.jsonl"
    monkeypatch.setenv("TRINITY_DECISION_LOG_PATH", str(p))
    secret = "我想自杀，我叫张三，手机 13812345678。"
    m.scan_sensitive(secret)
    text = p.read_text(encoding="utf-8")
    allow = set(m._DECISION_PERSIST_FIELDS)
    for ln in [x for x in text.splitlines() if x.strip()]:
        rec = json.loads(ln)
        if not set(rec) <= allow:
            return False
        if "content" in rec:
            return False
    # **正文原文（或其可识别片段）不得出现**
    return "张三" not in text and "13812345678" not in text and secret not in text


# ── C6 消费者判定（与 G8/D-14 同谓词）────────────────────────────────
ALLOWED_MENTIONERS = {
    "trinity/security/sensitive.py",                    # ① 生产者
    "trinity/security/redaction_surface.py",            # ② 登记/审计清单（G3/t106：DECISION_LOG_ARTIFACT）
    "tests/unit/test_decision_persistence_20261006.py",  # ③ 判据本身
}


def c6_no_consumers(tmp_path=None, monkeypatch=None) -> bool:
    """**同谓词**（G8/D-14）：`consumers` 空 **且** `code_readers_sample` 空
    **且** 全仓检索该制品只命中 生产者/审计清单/登记本身 三类。"""
    hits = set()
    for sub in ("trinity", "tests", "scripts"):
        for f in (ROOT / sub).rglob("*.py"):
            rel = f.relative_to(ROOT).as_posix()
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "TRINITY_DECISION_LOG_PATH" in txt or "decision_persistence_stats" in txt:
                hits.add(rel)
    return hits <= ALLOWED_MENTIONERS


CRITERIA = {
    "C1_默认不落盘": c1_default_no_disk,
    "C2_opt_in确实落盘": c2_opt_in_writes,
    "C3_反向零文件": c3_no_path_no_files,
    "C4_保留策略轮转": c4_retention_rotation,
    "C5_最小化无正文": c5_minimized_no_content,
    "C6_消费者判定": c6_no_consumers,
}


@pytest.mark.parametrize("name", sorted(CRITERIA), ids=sorted(CRITERIA))
def test_criteria_pass(name, tmp_path, monkeypatch):
    sub = tmp_path / name
    sub.mkdir()
    assert CRITERIA[name](sub, monkeypatch) is True


# ── 牙齿 ─────────────────────────────────────────────────────────────
def _teeth_no_rotation(mp):
    """① 把轮转摘掉 ⇒ C4 必红（文件会无限增长）。"""
    mp.setattr(_live(), "_rotate_decision_file", lambda path, incoming: False)


def _teeth_no_minimization(mp):
    """② 摘掉最小化 + 让记录带上正文（真实回归形态：加字段 + 忘了白名单）⇒ C5 必红。"""
    m = _live()
    real_build = m._build_decision_record
    mp.setattr(m, "_build_decision_record",
               lambda report, **kw: dict(real_build(report, **kw), content=kw.get("text", "")))
    mp.setattr(m, "_decision_minimize", lambda rec: dict(rec))


def _teeth_default_on(mp):
    """③ 把"默认不落盘"改成默认落盘 ⇒ C1 必红。"""
    m = _live()
    mp.setattr(m, "_decision_persist_path", lambda: os.path.join(str(_TEETH_DIR[0]), "forced.jsonl"))


_TEETH_DIR = [""]


MUTANTS = [
    ("C4_保留策略轮转", _teeth_no_rotation),
    ("C5_最小化无正文", _teeth_no_minimization),
    ("C1_默认不落盘", _teeth_default_on),
]


@pytest.mark.parametrize("name,apply_mutant", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_each_criterion_has_a_killing_mutant(name, apply_mutant, tmp_path, monkeypatch):
    sub = tmp_path / name
    sub.mkdir()
    _TEETH_DIR[0] = str(sub)
    apply_mutant(monkeypatch)
    caught = ""
    try:
        got = CRITERIA[name](sub, monkeypatch)
    except Exception as e:                            # noqa: BLE001 —— 异常=红
        got, caught = False, "%s: %s" % (type(e).__name__, str(e)[:90])
    assert got is False, "变异体 %s 没杀掉判据 %s ⇒ 该判据无判别力" % (name, name)
    if caught:
        print("[teeth] %s 被拦下：%s" % (name, caught))
