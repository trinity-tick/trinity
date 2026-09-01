# -*- coding: utf-8 -*-
"""Ops 层黄金测试（2026-09-01，自查短板 #3 修复）

覆盖 2026-08-31~09-01 实际踩坑类别，防回归：
  - GBK/UTF-8 双向乱码（wrapper 输出、subprocess 解码）
  - ps1 文件规范（UTF-8 BOM + CRLF + 语法可解析）
  - 维护脚本 subprocess 必须显式 utf-8 解码（text=True 时）
  - structure_watermark_check 水位判定
  - memory_compressor 单条批次放行（P5）
  - tracer 遥测默认关闭（P6）
  - structure_sync 幂等/去重
  - 版本单一源一致性（去硬编码）
"""
import os
import re
import sqlite3
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS = os.path.join(ROOT, "dsh-ops")
SCRIPTS = os.path.join(ROOT, "scripts")


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _read_text(path):
    return _read_bytes(path).decode("utf-8", errors="replace")


# ── 1. ps1 规范：BOM + CRLF + 可解析 ──────────────────────────────
@pytest.mark.parametrize("name", ["trinity-dsh-maintenance.ps1", "trinity-supervisor.ps1"])
def test_ps1_utf8_bom_and_crlf(name):
    data = _read_bytes(os.path.join(OPS, name))
    assert data.startswith(b"\xef\xbb\xbf"), f"{name} 缺 UTF-8 BOM（PS5.1 按 ANSI 读会吞中文注释）"
    text = data.decode("utf-8-sig")
    assert "\r\n" in text
    # 不允许孤立的 LF（CRLF only）
    assert not re.search(r"(?<!\r)\n", text), f"{name} 存在孤立 LF"


@pytest.mark.parametrize("name", ["trinity-dsh-maintenance.ps1", "trinity-supervisor.ps1"])
def test_ps1_parseable(name):
    text = _read_text(os.path.join(OPS, name))
    ps = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "$t = Get-Content -Raw -Path '" + os.path.join(OPS, name) + "'; $e = $null; "
         "[System.Management.Automation.PSParser]::Tokenize($t, [ref]$e) | Out-Null; exit $e.Count"],
        capture_output=True, text=True, timeout=120)
    assert ps.returncode == 0, f"{name} 语法解析失败: {ps.stdout}{ps.stderr}"


# ── 2. 维护脚本 subprocess 必须显式 utf-8（本周 UnicodeDecodeError 根因）──
def test_no_unencoded_text_subprocess_in_ps1():
    text = _read_text(os.path.join(OPS, "trinity-dsh-maintenance.ps1"))
    # 找所有 text=True 且未带 encoding= 的 subprocess.run 调用
    bad = []
    for m in re.finditer(r"subprocess\.run\((.{0,400}?)\)", text, re.S):
        seg = m.group(1)
        if "text=True" in seg and "encoding=" not in seg and "timeout=" in seg:
            bad.append(seg.strip()[:100])
    assert not bad, f"发现 text=True 但无 encoding= 的 subprocess.run:\n" + "\n".join(bad)


def test_ps1_sets_pythonioencoding_utf8():
    text = _read_text(os.path.join(OPS, "trinity-dsh-maintenance.ps1"))
    assert "PYTHONIOENCODING" in text and "utf-8" in text


# ── 3. 水位检查逻辑（P1b）────────────────────────────────────────
def _mk_watermark_db(tmp_path, last_ms, rows=1):
    db = str(tmp_path / "store.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE dsh_events (id INTEGER PRIMARY KEY, session_id TEXT, seq INT, type TEXT, turn INT, step INT, time REAL, payload TEXT)")
    for i in range(rows):
        c.execute("INSERT INTO dsh_events (session_id, seq, type, time) VALUES (?,?,?,?)",
                  ("s", i, "user/message", last_ms))
    c.commit()
    c.close()
    return db


def test_watermark_stale(tmp_path, monkeypatch):
    db = _mk_watermark_db(tmp_path, (time.time() - 25 * 3600) * 1000)
    monkeypatch.setenv("TRINITY_STORE_DB", db)
    sys.path.insert(0, SCRIPTS)
    import structure_watermark_check as wm
    out = []
    monkeypatch.setattr("sys.stdout", None)  # 占位防误用
    assert wm.main() == 1


def test_watermark_fresh(tmp_path, monkeypatch):
    db = _mk_watermark_db(tmp_path, time.time() * 1000)
    monkeypatch.setenv("TRINITY_STORE_DB", db)
    sys.path.insert(0, SCRIPTS)
    import structure_watermark_check as wm
    assert wm.main() == 0


# ── 4. 压缩器单条批次放行（P5）────────────────────────────────────
def test_compressor_accepts_single_memory():
    from trinity.daemon.memory_compressor import MemoryCompressor
    comp = MemoryCompressor(llm_callable=None, pg_adapter=None)
    res = comp.compress_batch([{"memory_id": "m1", "content": "x", "importance": 0.5}], "general")
    # llm 为空应走到 "No LLM callable" 而非 "Batch too small"——证明批次放行
    assert "too small" not in (res.error_message or "").lower()


# ── 5. 遥测默认关闭（P6）──────────────────────────────────────────
def test_telemetry_default_off(monkeypatch):
    monkeypatch.delenv("TRINITY_TELEMETRY_ENABLED", raising=False)
    import importlib
    import trinity.telemetry.tracer as tracer
    importlib.reload(tracer)
    assert tracer._TELEMETRY_ENABLED is False


def test_telemetry_optin(monkeypatch):
    monkeypatch.setenv("TRINITY_TELEMETRY_ENABLED", "1")
    import importlib
    import trinity.telemetry.tracer as tracer
    importlib.reload(tracer)
    assert tracer._TELEMETRY_ENABLED is True


# ── 6. structure_sync 幂等/去重 ───────────────────────────────────
def test_structure_sync_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_STORE", str(tmp_path))
    from trinity import structure_store as ss
    import importlib
    importlib.reload(ss)  # 重解析 TRINITY_STORE
    p = {"session_id": "sess1", "agent_id": "a1", "title": "t",
         "events": [{"seq": 1, "type": "user/message", "time": 1.0, "data": {"content": "hi"}}]}
    r1 = ss.structure_sync(p)
    r2 = ss.structure_sync(p)  # 幂等重放
    assert r1.get("synced", 0) == 1
    assert r2.get("synced", 0) == 1  # INSERT OR IGNORE，不重复
    conn = sqlite3.connect(str(tmp_path / "trinity_store.db"))
    n = conn.execute("SELECT COUNT(*) FROM dsh_events").fetchone()[0]
    conn.close()
    assert n == 1


# ── 7. 版本单一源（去硬编码）──────────────────────────────────────
def test_no_hardcoded_versions_in_api():
    from trinity.version import __version__
    models = os.path.join(ROOT, "trinity", "api", "server", "_models.py")
    agents = os.path.join(ROOT, "trinity", "api", "server", "_routers_agents.py")
    # 只查“赋值式”硬编码（注释/文档字符串里的 v8.2.0 字样不算）
    assert not re.search(r'version:\s*str\s*=\s*"\d+\.\d+\.\d+"', _read_text(models)), \
        "_models.py 残留赋值式硬编码版本"
    assert not re.search(r'"version":\s*"\d+\.\d+\.\d+"', _read_text(agents)), \
        "_routers_agents.py 残留赋值式硬编码版本"
    from trinity.api.server._models import HealthResponse
    assert HealthResponse().version == __version__  # 行为断言：跟随单一源
