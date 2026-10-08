# -*- coding: utf-8 -*-
r"""「已登记孤儿」与「故障型陈旧」的分桶判据（G8/t111 · D-14 案 (b)，2026-10-08）

## 背景

4 份周报产物（`agent_flags_report.json` / `ipi_report.json` / `contradiction_resolutions.jsonl` /
`market_drill_report.json`）经实测**没有任何消费者**（`ORGAN_REGISTRY` 的 `consumers` 与
`code_readers_sample` 均空，且全仓检索只命中"生产者 / 审计清单 / 登记本身"三类）。
⇒ 队长裁定 **案 (b)：保留产出 + 登记为"已登记孤儿"**，并把审计的分桶表达改明确：

  · **已登记孤儿**的陈旧 ⇒ `ORPHAN-STALE`（或 `ORPHAN-MISSING`）⇒ **不计入 gaps**；
  · **未登记**的陈旧 ⇒ `STALE` ⇒ **仍计入 gaps**（**牙齿不变**）。

## 判定口径（⭐ 与 G5/D-13 **共用同一条**，不另造）

> `ORGAN_REGISTRY` 的 `consumers` 与 `code_readers_sample` **均空**
> 且 全仓检索该文件名**只命中三类**：生产者 / 审计清单 / 登记本身。

判据 ④ 钉住"这条谓词必须**同时**出现在登记文件与审计源码里"（**口径必须同行**）。

## 为什么用 `monkeypatch(STATE_DIR)` 而不是看真目录

真目录的新鲜度会随下一次周一调度变化（t99 的"修好根因 ≠ 立刻转绿"）
⇒ 若判据直接依赖它，就会在"新鲜"与"陈旧"之间抖。**改成把 `STATE_DIR` 指到临时目录**，
就能**确定性地**压两种分支（已登记/未登记），而把"现场状态"留给审计自己去报。
"""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

ORPHAN_NAMES = ("agent_flags_report.json", "ipi_report.json",
                "contradiction_resolutions.jsonl", "market_drill_report.json")

REGISTRY = ROOT / "docs" / "SILENT_FAILURE_BUDGETS.json"
AUDIT_SRC = ROOT / "scripts" / "maintenance_chain_audit.py"

#: 判据 ④ 要求**两个文件都出现**的口径关键词（口径同行）
PREDICATE_TOKENS = ("consumers", "code_readers_sample", "只命中三类")


def _audit():
    import importlib
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module("scripts.maintenance_chain_audit")


def _run_audit(monkeypatch, tmp_path, stale_names, orphan_names=None, age_days=40.0):
    """把审计的 `STATE_DIR` 指到临时目录后**在进程内**跑一次，返回 (rc, out)。"""
    audit = _audit()
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    for name in stale_names:
        f = state / name
        f.write_text("{}", encoding="utf-8")
        old = time.time() - age_days * 86400.0
        os.utime(f, (old, old))
    log = tmp_path / "dsh-maintenance.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(audit, "LOG", str(log))
    monkeypatch.setattr(audit, "AUTOSTART_LOG", str(tmp_path / "none-autostart.log"))
    monkeypatch.setattr(audit, "STATE_DIR", str(state))
    if orphan_names is not None:
        monkeypatch.setattr(audit, "_registered_orphan_names", lambda: set(orphan_names))
    buf = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(buf):
        rc = audit.main(["--last", "1", "--missing-json"])
    return rc, buf.getvalue()


def _gaps_from(out: str):
    """从 `MISSING-JSON: {...}` 行里取 gaps（判据自己解析，不靠目测）。"""
    for line in out.splitlines():
        if "MISSING-JSON" in line:
            return int(json.loads(line.split("MISSING-JSON:", 1)[1].strip())["gaps"])
    return None


# ── 判据 ──────────────────────────────────────────────────────────────────

def test_registered_orphans_are_reported_as_orphan_not_as_gaps(monkeypatch, tmp_path) -> None:
    """① 处置后**表达明确**：4 份注册孤儿陈旧 ⇒ 报 `ORPHAN-STALE` 且 **gaps = 0**。"""
    rc, out = _run_audit(monkeypatch, tmp_path, ORPHAN_NAMES)
    for name in ORPHAN_NAMES:
        assert name in out, "审计输出里没有 %s：\n%s" % (name, out[-800:])
    assert out.count("ORPHAN-STALE") == len(ORPHAN_NAMES), \
        "4 份注册孤儿应各报一次 ORPHAN-STALE：\n%s" % out[-800:]
    assert _gaps_from(out) == 0, "已登记的孤儿不应计入 gaps：\n%s" % out[-800:]
    assert "已登记" in out and "不计 gaps" in out, "表达不够明确（缺'已登记/不计 gaps'字样）"
    assert rc == 0, "有已登记孤儿时审计不应非零退出（rc=%s）" % rc


def test_broken_registry_fails_closed(monkeypatch, tmp_path) -> None:
    """② **fail-closed**：登记**读不到或损坏** ⇒ 一切陈旧必须回到 `STALE` 并计入 gaps（宁可真红，不假绿）。

    ⚠️ 第一版我把这条写成"造一个**不在 `_arts` 里的**陈旧文件 ⇒ 必须计入 gaps"——
    那是**错的**：审计只遍历 `_arts`（4 个硬编码名字），外来文件名**根本不会被检查**
    ⇒ 判据永远红且与被测行为无关。改成压**登记不可用**这个真实分支。
    """
    audit = _audit()
    missing = tmp_path / "no-such-registry.json"
    monkeypatch.setattr(audit, "REGISTRY_PATH", str(missing))
    assert audit._registered_orphan_names() == set(), "登记不可读时必须 fail-closed 返回空集"

    corrupt = tmp_path / "corrupt-registry.json"
    corrupt.write_text("{ 这不是 JSON", encoding="utf-8")
    monkeypatch.setattr(audit, "REGISTRY_PATH", str(corrupt))
    assert audit._registered_orphan_names() == set(), "登记损坏时必须 fail-closed 返回空集"

    # 登记不可用 ⇒ 4 份注册过的名字也**必须**回到 STALE 并计入 gaps
    rc, out = _run_audit(monkeypatch, tmp_path, ORPHAN_NAMES)   # 不覆盖 loader ⇒ 走真 loader
    assert "ORPHAN-STALE" not in out, "登记不可用时仍报孤儿 ⇒ fail-closed 失效：\n%s" % out[-800:]
    assert _gaps_from(out) == len(ORPHAN_NAMES), \
        "登记不可用时 4 份都必须计入 gaps：\n%s" % out[-800:]
    assert rc == 1, "登记不可用时应以 1 退出（rc=%s）" % rc


def test_produced_but_unregistered_must_go_red(monkeypatch, tmp_path) -> None:
    """③ **反向**（队长要的牙齿）：把**已登记**的名字**取消登记**（模拟"产出但未登记"）⇒ **必红**。

    做法：文件仍叫注册过的名字、也确实陈旧，但把 `_registered_orphan_names()` 收空
    ⇒ 它必须掉回 `STALE` 分支并计入 gaps。
    """
    rc, out = _run_audit(monkeypatch, tmp_path, ORPHAN_NAMES, orphan_names=set())
    assert "ORPHAN-STALE" not in out, "取消登记后仍在报孤儿 ⇒ 登记没起作用：\n%s" % out[-800:]
    assert _gaps_from(out) == len(ORPHAN_NAMES), \
        "取消登记后 4 份都应计入 gaps：\n%s" % out[-800:]
    assert rc == 1, "取消登记后审计应以 1 退出（rc=%s）" % rc


def test_registry_entry_declares_the_shared_consumer_predicate(monkeypatch) -> None:
    """④ **与 G5/D-13 共用同一谓词** + **不得伪造消费者** + 登记元数据齐全。"""
    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    block = reg.get("registered_orphans") or {}
    assert block, "登记文件里没有 registered_orphans 块"
    for key in ("_consumer_predicate", "_why_registered", "_proposed_by", "_date"):
        assert block.get(key), "登记块缺 %s" % key
    items = block.get("items") or {}
    assert set(items) == set(ORPHAN_NAMES), "登记项与实际 4 份不一致：%s" % sorted(items)

    for name, ev in items.items():
        assert ev.get("consumers") == [], "%s 被写了消费者 ⇒ 等于伪造：%r" % (name, ev.get("consumers"))
        assert ev.get("code_readers_sample") == [], "%s 有代码读者 ⇒ 不该按孤儿登记" % name
        assert ev.get("producer"), "%s 缺生产者" % name

    # ⭐ 口径同行：同一条谓词必须**同时**出现在登记文件与审计源码里
    audit_src = AUDIT_SRC.read_text(encoding="utf-8")
    for token in PREDICATE_TOKENS:
        assert token in block["_consumer_predicate"], "谓词缺关键词 %r" % token
        assert token in audit_src, "审计源码未引用同一谓词关键词 %r（口径会分叉）" % token
