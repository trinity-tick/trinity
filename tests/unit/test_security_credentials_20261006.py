# -*- coding: utf-8 -*-
"""T5：明文凭据收口 + 安全模块静默降级可见化（2026-10-06）。

钉住三件事（每条都有反事实）：
  ① 维护/导出脚本源码里**不得再有明文口令或带口令的默认 DSN**
     （改前实测：`scripts/sync_sqlite_to_pg.py` 的默认 DSN 里带口令，且**能连** ⇒
      "看起来没问题"的明文凭据）；
  ② 统一凭据入口 **fail-closed**：口令只来自内置弱兜底时 `resolve_pg_dsn()` 返回
     None（改前的等价物是"永远有一个看起来能连的默认值"）；
  ③ `resolve_backend()` 的静默降级可解释：`backend_resolution_trace()` 必须同时
     暴露"文件里有值"与"实际生效值"，并标出静默风险点。

跑法：
    python -m pytest tests/unit/test_security_credentials_20261006.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trinity.security import credentials as C  # noqa: E402

#: 带明文口令的 DSN / 连接串（user:pass@host）
DSN_WITH_SECRET = re.compile(r"://[^/\s\"'@]+:[^/\s\"'@]+@")
#: 维护/导出脚本里绝迹的口令字面量
PASSWORD_LITERAL = re.compile(r"password\s*=\s*[\"'][^\"'{}%]{3,}[\"']")
SCAN_DIRS = [ROOT / "scripts"]


def _iter_scripts():
    for d in SCAN_DIRS:
        if not d.exists():
            continue
        for p in d.rglob("*.py"):
            if "__pycache__" in p.parts or "legacy" in p.parts:
                continue
            yield p


def test_detector_would_catch_the_original_line():
    """反事实：判据必须能抓住**改前那一行**（否则它是空判据）。

    这里用**占位符**而非真实口令复现旧形态（口令不进测试/报告/提交信息）。
    """
    old = ('        PG_URL = os.environ.get("TRINITY_PG_URL",\n'
           '                                "postgresql://someuser:somepass@127.0.0.1:5432/trinity")')
    assert DSN_WITH_SECRET.search(old), "判据抓不到改前的明文 DSN ⇒ 判据无效"
    # 反事实之二：修好之后的写法（走统一入口）不得被判据抓住
    fixed = 'dsn = resolve_pg_dsn() if not os.environ.get("TRINITY_PG_URL") else os.environ["TRINITY_PG_URL"]'
    assert not DSN_WITH_SECRET.search(fixed)


def test_no_plaintext_dsn_in_maintenance_scripts():
    offenders = []
    for p in _iter_scripts():
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if DSN_WITH_SECRET.search(line):
                offenders.append("%s:%d" % (p.relative_to(ROOT), i))
    assert offenders == [], "scripts/ 仍有带明文口令的连接串：%s" % offenders


def test_no_password_literal_in_sync_script():
    p = ROOT / "scripts" / "sync_sqlite_to_pg.py"
    text = p.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        assert not PASSWORD_LITERAL.search(line), "%s:%d 仍含口令字面量" % (p.name, i)
    assert "resolve_pg_dsn" in text, "维护脚本必须走统一凭据入口"


def test_resolve_pg_dsn_fail_closed_when_only_fallback(monkeypatch):
    """口令只来自内置弱兜底 ⇒ 拒绝（不得"看起来能连"）。"""
    for k in C._ENV_MAP:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(C, "_load_yaml", lambda: {})
    assert C.credential_provenance()["password"] == "fallback"
    assert C.resolve_pg_dsn() is None
    # 反事实：显式允许时才回落到兜底值
    assert C.resolve_pg_dsn(allow_fallback=True) is not None


def test_resolve_pg_dsn_env_first(monkeypatch):
    monkeypatch.setenv("TRINITY_PG_PASSWORD", "env-only-secret")
    dsn = C.resolve_pg_dsn()
    assert dsn is not None and "env-only-secret" in dsn
    assert C.credential_provenance()["password"] == "env"


def test_dsn_redacted_never_leaks_password(monkeypatch):
    monkeypatch.setenv("TRINITY_PG_PASSWORD", "env-only-secret")
    dsn = C.resolve_pg_dsn()
    out = C.dsn_redacted(dsn)
    assert "env-only-secret" not in out
    assert "***" in out


def test_pg_dsn_is_quoted(monkeypatch):
    """特殊字符口令必须 URL-quote，否则 DSN 解析会错（静默连错库/连不上）。"""
    monkeypatch.setenv("TRINITY_PG_PASSWORD", "p@ss:w/rd")
    dsn = C.resolve_pg_dsn()
    assert "p%40ss%3Aw%2Frd" in dsn


# ── 静默降级：resolve_backend 的三处必须可见 ─────────────────────────
def test_backend_trace_exposes_env_override(monkeypatch):
    monkeypatch.setenv("TRINITY_STORAGE_BACKEND", "postgresql")
    t = C.backend_resolution_trace()
    assert t["value"] == "postgresql" and t["source"] == "env"
    assert any("环境变量优先" in n for n in t["notes"])


def test_backend_trace_exposes_sqlite_guard(monkeypatch):
    monkeypatch.delenv("TRINITY_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("TRINITY_STORE", "C:/tmp/x.db")
    t = C.backend_resolution_trace()
    assert t["value"] == "" and t["source"] == "sqlite_guard"
    assert t["guard_keys"] == ["TRINITY_STORE"]


def test_backend_trace_exposes_rolled_back_refs(monkeypatch):
    """凭证文件里**有值**（refs 下）而实际生效值为空 ⇒ 必须被标为静默风险。"""
    monkeypatch.delenv("TRINITY_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("TRINITY_STORE", raising=False)
    monkeypatch.delenv("TRINITY_DB_PATH", raising=False)
    monkeypatch.setattr(C.Path, "home", classmethod(lambda cls: Path("C:/__no_such_home__")))
    t = C.backend_resolution_trace()
    assert t["source"] == "default(empty)"
    assert t["value"] == ""
    # 反事实：把 home 指回真实目录后，refs 里的值必须被 trace 看见
    monkeypatch.undo()
    t2 = C.backend_resolution_trace()
    assert t2["yaml_refs"] == "postgresql" or t2["yaml_top_level"] == "postgresql"
    assert "refs_fallback_rolled_back" in t2["silent_risks"] or t2["value"] == "postgresql"


def test_readside_scan_error_is_fail_closed(monkeypatch):
    """读侧标注：扫描失败必须标不可信（改前标成 untrusted=False = 静默漏标）。"""
    from trinity.security import readside
    import trinity.security.injection as inj

    def _boom(_text):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(inj, "scan_injection", _boom)
    r = readside.annotate_readside({"content": "任意内容"})
    assert r["untrusted"] is True
    assert r["untrusted_reason"].startswith("scan_error")
    assert r["untrusted_scan_failed"] is True


def test_adapter_guard_degradation_is_observable(monkeypatch):
    """适配器守卫：扫描关闭 ⇒ 返回值必须显式标 degraded（改前只有一条 warning 日志）。"""
    from trinity.security import injection

    monkeypatch.setenv("TRINITY_INJECTION_SCAN", "off")
    before = injection.degradation_report()["scan_off"]
    g = injection.adapter_write_guard("任意内容", agent_id="u1", category="general")
    assert g["degraded"] is True and g["exempt"] == "scan_off"
    assert injection.degradation_report()["scan_off"] == before + 1
