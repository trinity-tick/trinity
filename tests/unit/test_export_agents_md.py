"""P0-4: AGENTS.md exporter (COMPARISON_VS_2026_SOTA_R7).

Verifies:
  - build_agents_md() renders the full template with live snapshot
  - --no-live mode renders the template without the snapshot section
  - snapshot is tolerant when structure layer is unavailable
  - CLI writes UTF-8 output file
"""

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.export_agents_md import (
    TEMPLATE,
    _render_snapshot,
    build_agents_md,
)


def test_template_contains_key_sections():
    for section in (
        "## 1. Trinity 是什么",
        "## 2. 如何检索记忆",
        "## 3. 如何写入记忆",
        "## 4. 会话身份与隔离",
        "## 5. 常用命令",
        "## 6. 注意事项（known pitfalls）",
        "memory_search",
    ):
        assert section in TEMPLATE


def test_build_with_live_snapshot():
    md = build_agents_md(include_live=True)
    assert md.startswith("# AGENTS.md — Trinity Memory")
    assert "Trinity 记忆层实时快照" in md
    assert "生成于 20" in md


def test_build_without_live_snapshot():
    md = build_agents_md(include_live=False)
    assert md.startswith("# AGENTS.md — Trinity Memory")
    assert "Trinity 记忆层实时快照" not in md
    assert "## 1. Trinity 是什么" in md


def test_render_snapshot_failure_tolerant():
    out = _render_snapshot({"ok": False, "reason": "boom"})
    assert "实时快照不可用" in out
    assert "boom" in out


def test_render_snapshot_with_data():
    snap = {
        "ok": True,
        "stats": {"sessions": 2, "events": 10, "goals": 1, "todos": 3, "schedules": 0},
        "active_goals": [
            {"objective": "完成优化", "phase": "active", "round": 1, "status": "active"}
        ],
        "recent_sessions": [
            {"title": "测试会话", "status": "active", "updated_at": "1", "session_id": "s1"}
        ],
    }
    out = _render_snapshot(snap)
    assert "| 会话数 | 2 |" in out
    assert "| 目标数 | 1 |" in out
    assert "完成优化" in out
    assert "s1" in out


def test_cli_writes_utf8_file(tmp_path: Path):
    out = tmp_path / "AGENTS.md"
    r = subprocess.run(
        [sys.executable, "scripts/export_agents_md.py", "--out", str(out), "--no-live"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent.parent,
    )
    assert r.returncode == 0
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# AGENTS.md — Trinity Memory")
    assert "Trinity 记忆层实时快照" not in text
